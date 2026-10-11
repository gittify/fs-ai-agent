"""CDK app. Deploy with required context, for example:

    cd infra
    pip install -r requirements.txt
    cdk deploy \
      -c sender=recall-agent@yourdomain.com \
      -c coordinator=coordinator@yourdomain.com \
      -c model_id=<Bedrock model or inference-profile ID enabled in your account>

Optional context: schedule_tz (default America/Los_Angeles), send_when_degraded (default true),
demo_mode (default false; true lets an invocation inject faults, for rehearsing the demo only),
guardrails (default true; false skips the Bedrock guardrail that screens recall text for prompt attacks).
"""
import aws_cdk as cdk

from stack import RecallAgentStack

REQUIRED = ("sender", "coordinator", "model_id")


def build(app: cdk.App) -> RecallAgentStack:
    missing = [k for k in REQUIRED if not app.node.try_get_context(k)]
    if missing:
        raise ValueError("missing required CDK context: " + ", ".join(f"-c {k}=..." for k in missing))
    return RecallAgentStack(
        app, "RecallAgentStack",
        sender=app.node.try_get_context("sender"),
        coordinator=app.node.try_get_context("coordinator"),
        model_id=app.node.try_get_context("model_id"),
        schedule_tz=app.node.try_get_context("schedule_tz") or "America/Los_Angeles",
        send_when_degraded=str(app.node.try_get_context("send_when_degraded") or "true").lower(),
        demo_mode=str(app.node.try_get_context("demo_mode") or "false").lower(),
        guardrails=str(app.node.try_get_context("guardrails") or "true").lower(),
    )


if __name__ == "__main__":
    app = cdk.App()
    build(app)
    app.synth()
