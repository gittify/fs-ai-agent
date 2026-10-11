"""Synthesizes the CDK stack and asserts on the CloudFormation. Skipped if aws-cdk-lib isn't installed."""
import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("aws_cdk")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "infra"))

import aws_cdk as cdk  # noqa: E402
from aws_cdk.assertions import Match, Template  # noqa: E402

from app import build  # noqa: E402
from recall_agent.metrics import METRIC_NAMES, NAMESPACE  # noqa: E402
from stack import DASHBOARD_METRICS, DURATION_ALARM_SECONDS  # noqa: E402

CTX = {"sender": "agent@example.com", "coordinator": "coordinator@example.com", "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0"}


@pytest.fixture(scope="module")
def synthed():
    app = cdk.App(context=CTX)
    stack = build(app)
    return app.synth(), Template.from_stack(stack)


@pytest.fixture(scope="module")
def template(synthed):
    return synthed[1]


def statements(template):
    out = []
    for pol in template.find_resources("AWS::IAM::Policy").values():
        out += pol["Properties"]["PolicyDocument"]["Statement"]
    return out


def test_bucket_is_private_versioned_and_retained(template):
    template.has_resource("AWS::S3::Bucket", {
        "DeletionPolicy": "Retain",
        "Properties": {
            "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                               "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
            "VersioningConfiguration": {"Status": "Enabled"}}})


def test_state_table_keys_pitr_and_retain(template):
    template.has_resource("AWS::DynamoDB::Table", {
        "DeletionPolicy": "Retain",
        "Properties": {
            "KeySchema": [{"AttributeName": "run_id", "KeyType": "HASH"},
                          {"AttributeName": "item_key", "KeyType": "RANGE"}],
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}}})


def test_lambda_config_and_env(template):
    template.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "recall_agent.handler.lambda_handler", "Runtime": "python3.12", "Timeout": 900,
        "Environment": {"Variables": Match.object_like({
            "BEDROCK_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "DEMO_MODE": "false", "SENDER_EMAIL": "agent@example.com",
            "COORDINATOR_EMAIL": "coordinator@example.com", "SEND_WHEN_DEGRADED": "true"})}})


def test_no_wildcard_resource_on_s3_or_ses_and_no_admin_actions(template):
    for st in statements(template):
        actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        assert not any(a in ("*", "s3:*", "ses:*", "bedrock:*", "dynamodb:*") for a in actions), st
        if any(a.startswith(("s3:", "ses:")) for a in actions):
            assert st["Resource"] != "*", st


def test_lambda_can_send_as_the_sender_only(template):
    ses_stmt = [s for s in statements(template) if s["Action"] == "ses:SendEmail"]
    assert len(ses_stmt) == 1 and ses_stmt[0]["Resource"] != "*"


def bedrock_statements(model_id):
    app = cdk.App(context={**CTX, "model_id": model_id})
    t = Template.from_stack(build(app))
    return [s for s in statements(t) if "bedrock:InvokeModel" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]


def test_geo_profile_gets_the_profile_and_the_one_model_and_no_regionless_grant():
    [st] = bedrock_statements("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    blob = str(st["Resource"])
    assert "inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0" in blob
    assert "foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0" in blob
    assert "bedrock:::foundation-model" not in blob and "anthropic.*" not in blob  # scoped to one model
    assert "Condition" not in st


def test_global_profile_also_gets_the_regionless_model_arn_only_via_that_profile():
    sts = bedrock_statements("global.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert len(sts) == 2
    regionless = next(s for s in sts if "bedrock:::foundation-model/anthropic.claude-haiku" in str(s["Resource"]))
    assert regionless["Condition"]["StringEquals"] == {"aws:RequestedRegion": "unspecified"}
    assert "inference-profile/global.anthropic.claude-haiku" in str(regionless["Condition"]["ArnLike"])


def test_plain_model_id_gets_only_the_regional_model_arn():
    [st] = bedrock_statements("anthropic.claude-sonnet-5-5")
    assert "foundation-model/anthropic.claude-sonnet-5-5" in str(st["Resource"]) and "inference-profile" not in str(st["Resource"])


@pytest.mark.parametrize("bad", ["test-model", "gpt-4", "arn:aws:bedrock:us-east-1::foundation-model/x", "us.meta.llama"])
def test_a_mistyped_model_id_fails_at_synth_not_at_runtime(bad):
    with pytest.raises(ValueError) as e:
        build(cdk.App(context={**CTX, "model_id": bad}))
    assert "inference-profile ID" in str(e.value)


def test_demo_mode_is_off_by_default_and_can_be_turned_on_explicitly():
    off = Template.from_stack(build(cdk.App(context=CTX)))
    on = Template.from_stack(build(cdk.App(context={**CTX, "demo_mode": "true"})))
    for t, want in ((off, "false"), (on, "true")):
        t.has_resource_properties("AWS::Lambda::Function", {"Environment": {"Variables": Match.object_like({"DEMO_MODE": want})}})


def test_s3_access_is_split_by_prefix(template):
    blob = str([s for s in statements(template) if any(str(a).startswith("s3:") for a in
                (s["Action"] if isinstance(s["Action"], list) else [s["Action"]]))])
    assert "inventory/*" in blob and "records/*" in blob


def test_monday_6am_schedule_with_timezone_and_no_flex(template):
    template.has_resource_properties("AWS::Scheduler::Schedule", {
        "ScheduleExpression": "cron(0 6 ? * MON *)",
        "ScheduleExpressionTimezone": "America/Los_Angeles",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": Match.object_like({"Input": "{}"})})


def test_errors_alarm_notifies_the_coordinator(template):
    template.has_resource_properties("AWS::CloudWatch::Alarm", {
        "MetricName": "Errors", "Namespace": "AWS/Lambda", "Threshold": 1,
        "AlarmActions": Match.any_value()})
    template.has_resource_properties("AWS::SNS::Subscription", {
        "Protocol": "email", "Endpoint": "coordinator@example.com"})


def test_both_ses_identities_are_created(template):
    template.resource_count_is("AWS::SES::EmailIdentity", 2)


def lambda_asset(assembly) -> str:
    """The synthesized asset directory that holds the agent code."""
    dirs = [os.path.join(assembly.directory, d) for d in os.listdir(assembly.directory) if d.startswith("asset.")]
    [asset] = [d for d in dirs if os.path.isdir(os.path.join(d, "recall_agent"))]
    return asset


def test_lambda_package_has_the_agent_and_its_pinned_dependencies_and_nothing_else(synthed):
    asset = lambda_asset(synthed[0])
    top = set(os.listdir(asset))
    assert {"recall_agent", "openpyxl", "et_xmlfile"} <= top
    assert not top & {"tests", "infra", "sample_output", "sample_items.csv", "cdk.out", ".build", "boto3"}
    assert os.path.exists(os.path.join(asset, "recall_agent", "templates", "Weekly_Supplier_Recall_Check_Log.xlsx"))


def test_lambda_package_is_pure_python(synthed):
    asset = lambda_asset(synthed[0])
    compiled = [f for _, _, files in os.walk(asset) for f in files if f.endswith((".so", ".pyd", ".dylib", ".dll"))]
    assert compiled == []


def test_staged_package_builds_a_workbook_with_only_stdlib_plus_its_own_dependencies(synthed):
    """python -S disables site-packages, so this only passes if the package is self-sufficient."""
    asset = lambda_asset(synthed[0])
    code = (
        "import openpyxl, sys\n"
        "from recall_agent.workbook import build_workbook, load_bundled_template\n"
        "r = {'item': 'Acme', 'kind': 'supplier', 'decision': 'CLEAR', 'override_reason': None,\n"
        "     'model_decision': None, 'matcher': {'verdict': 'none', 'matches': []}, 'evidence': [],\n"
        "     'tool_calls': [], 'model_error': None}\n"
        "data = build_workbook(load_bundled_template(), '2026-10-05', [r])\n"
        "print(data[:2].decode(), openpyxl.__file__)\n")
    out = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": asset, "PATH": os.environ.get("PATH", "")})
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("PK") and asset in out.stdout  # a real xlsx, openpyxl imported from the package


# ---- staging logic, with pip faked so these run offline and fast ----

class FakePip:
    def __init__(self, payload=("openpyxl/__init__.py",), fail=False):
        self.calls, self.payload, self.fail = 0, payload, fail

    def __call__(self, cmd, **kw):
        self.calls += 1
        if self.fail:
            raise subprocess.CalledProcessError(1, cmd, stderr="No matching distribution")
        target = cmd[cmd.index("-t") + 1]
        for rel in self.payload:
            path = os.path.join(target, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").write("")


def test_dependencies_are_cached_but_the_agent_code_is_reassembled_every_time(tmp_path, monkeypatch):
    import package
    pip = FakePip()
    monkeypatch.setattr(package.subprocess, "run", pip)
    first = package.stage_lambda_package(build_dir=tmp_path)
    (first / "stale.txt").write_text("x")
    second = package.stage_lambda_package(build_dir=tmp_path)
    assert pip.calls == 1 and not (second / "stale.txt").exists() and (second / "recall_agent" / "handler.py").exists()


def test_a_folder_that_cannot_be_removed_is_never_reused_so_old_and_new_files_never_mix(tmp_path, monkeypatch):
    import package
    monkeypatch.setattr(package.subprocess, "run", FakePip())
    first = package.stage_lambda_package(build_dir=tmp_path)
    (first / "stale.txt").write_text("old")
    monkeypatch.setattr(package.shutil, "rmtree", lambda *a, **k: None)  # simulate a removal that silently fails
    second = package.stage_lambda_package(build_dir=tmp_path)
    assert second != first and not (second / "stale.txt").exists() and (second / "recall_agent" / "handler.py").exists()


def test_a_compiled_dependency_is_rejected(tmp_path, monkeypatch):
    import package
    monkeypatch.setattr(package.subprocess, "run", FakePip(payload=("lxml/etree.cpython-312.so",)))
    with pytest.raises(RuntimeError) as e:
        package.stage_lambda_package(build_dir=tmp_path)
    assert "pure Python" in str(e.value) and not (tmp_path / "deps").exists()


def test_pip_failure_gives_a_clear_error_and_leaves_no_half_built_cache(tmp_path, monkeypatch):
    import package
    monkeypatch.setattr(package.subprocess, "run", FakePip(fail=True))
    with pytest.raises(RuntimeError) as e:
        package.stage_lambda_package(build_dir=tmp_path)
    assert "network access" in str(e.value) and "No matching distribution" in str(e.value)
    assert not (tmp_path / "deps").exists() and not (tmp_path / "deps.stamp").exists()


def test_changing_the_pinned_requirements_triggers_a_reinstall(tmp_path, monkeypatch):
    import shutil
    import package
    root = tmp_path / "root"
    (root / "infra").mkdir(parents=True)
    shutil.copytree(package.ROOT / "recall_agent", root / "recall_agent")
    (root / "infra" / "requirements-lambda.txt").write_text("openpyxl==3.1.5\n")
    pip = FakePip()
    monkeypatch.setattr(package.subprocess, "run", pip)
    package.stage_lambda_package(root, tmp_path / "b")
    (root / "infra" / "requirements-lambda.txt").write_text("openpyxl==3.1.6\n")
    package.stage_lambda_package(root, tmp_path / "b")
    assert pip.calls == 2


def test_missing_context_fails_with_a_clear_message():
    with pytest.raises(ValueError) as e:
        build(cdk.App(context={"sender": "agent@example.com"}))
    assert "-c coordinator=" in str(e.value) and "-c model_id=" in str(e.value)


# ------------------------------------------------------------------ Bedrock Guardrails

def test_the_guardrail_has_prompt_attack_topic_and_word_filters_and_a_published_version(template):
    template.has_resource_properties("AWS::Bedrock::Guardrail", {
        "ContentPolicyConfig": {"FiltersConfig": [{"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"}]},
        "TopicPolicyConfig": {"TopicsConfig": [Match.object_like({"Name": "AgentInstructionOverride", "Type": "DENY"})]},
        "WordPolicyConfig": {"WordsConfig": Match.array_with([Match.object_like({"Text": "NOTE TO AI AGENT"})])}})
    template.resource_count_is("AWS::Bedrock::GuardrailVersion", 1)


def test_the_lambda_gets_the_guardrail_id_and_version_and_may_apply_only_that_guardrail(template):
    template.has_resource_properties("AWS::Lambda::Function", {"Environment": {"Variables": Match.object_like({
        "GUARDRAIL_ID": Match.any_value(), "GUARDRAIL_VERSION": Match.any_value()})}})
    stmts = [s for s in statements(template) if s["Action"] == "bedrock:ApplyGuardrail"]
    assert len(stmts) == 1 and stmts[0]["Resource"] != "*" and "GuardrailArn" in str(stmts[0]["Resource"])


def test_guardrails_can_be_switched_off_cleanly():
    t = Template.from_stack(build(cdk.App(context={**CTX, "guardrails": "false"})))
    t.resource_count_is("AWS::Bedrock::Guardrail", 0)
    t.has_resource_properties("AWS::Lambda::Function", {"Environment": {"Variables": Match.object_like({"GUARDRAIL_ID": ""})}})
    assert not [s for s in statements(t) if s["Action"] == "bedrock:ApplyGuardrail"]


# ------------------------------------------------------------------ metrics dashboard and duration alarm

def test_every_dashboard_metric_is_one_the_code_actually_emits():
    assert set(DASHBOARD_METRICS) <= set(METRIC_NAMES)


def test_the_dashboard_reads_the_custom_namespace_and_shows_the_lambda_limit(template):
    [dash] = template.find_resources("AWS::CloudWatch::Dashboard").values()
    blob = json.dumps(dash["Properties"]["DashboardBody"])
    assert NAMESPACE in blob and all(name in blob for name in DASHBOARD_METRICS)
    assert "Lambda limit" in blob and "900" in blob  # the 15-minute limit is drawn on the duration graph


def test_a_run_nearing_the_lambda_limit_raises_an_alarm_to_the_coordinator(template):
    template.has_resource_properties("AWS::CloudWatch::Alarm", {
        "MetricName": "RunDurationSeconds", "Namespace": NAMESPACE, "Threshold": DURATION_ALARM_SECONDS,
        "AlarmActions": Match.any_value()})
    assert DURATION_ALARM_SECONDS == 720  # 80% of 900 seconds


def test_the_dashboard_url_and_guardrail_id_are_outputs(template):
    outputs = template.to_json()["Outputs"]
    assert "DashboardUrl" in outputs and "GuardrailId" in outputs
