# Deploy checklist

Work top to bottom. Each step says what you should see. If you don't see it, stop and use the
troubleshooting table at the bottom before continuing.

Commands assume macOS/Linux shell. Replace `ACCOUNT`, `BUCKET`, `TABLE`, `FUNCTION`, `LOGGROUP` with
your values (the deploy prints the last four).

---

## Which model to use

**Recommended for the first deploy:** `us.anthropic.claude-haiku-4-5-20251001-v1:0` in `us-east-1`.

| Option | Model ID | When to pick it |
| --- | --- | --- |
| **Recommended** | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Open to all Bedrock customers (no access wait), fast, cheap. Inference stays in US regions. Simplest IAM. |
| Same model, more capacity | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | If you hit throttling. May be served from outside the US. The stack adds the extra IAM grant a `global.` profile needs. |
| Upgrade candidate | `us.anthropic.claude-sonnet-5-5` | Stronger judgment on ambiguous matches. Check access first (see below). Switch only if the eval harness shows it helps. |

Why Haiku first: the safety-critical decisions (never a false "clear", hold only on a confirmed
firm/product + lot match) are enforced in code, not by the model. The model only adjudicates
ambiguous matches, so a small fast model is the right starting point, and the eval harness
(`python -m evals.run live`, step 1.4) is how you justify upgrading.

Rules that will bite you:

- Use an **inference profile ID** (`us.` / `global.` prefix), never a bare model ID, for Haiku 4.5.
  AWS documents that its `bedrock-runtime` endpoint requires a geo or global profile.
- Pass an **ID, not an ARN**. The stack rejects anything else at synth time.
- Model availability and access rules change. Verify with step 1.2 before deploying.
  At the time of writing, Haiku 4.5 is open to all Bedrock customers and Sonnet 5.5 is listed as
  "see access" (it may need an extra request).

---

## Phase 0: tools and credentials

- [ ] **0.1** Install: Python 3.12, Node.js 18+, AWS CLI v2, and the CDK CLI: `npm install -g aws-cdk`
- [ ] **0.2** Log in to AWS (`aws configure` or `aws sso login`), then:
  ```
  aws sts get-caller-identity
  export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
  ```
  *Expect:* your account ID and a role/user ARN. Note the account ID.
- [ ] **0.3** From the project root:
  ```
  pip install -r requirements.txt -r infra/requirements.txt
  python -m pytest -q
  ```
  *Expect:* every test passes (100+). If any fail, fix before deploying.

## Phase 1: Bedrock model access

- [ ] **1.1** In the AWS console open **Amazon Bedrock** in `us-east-1`. If it is your first time using
  Anthropic models, submit the **use case details** form when prompted. Anthropic models won't
  respond until that is done.
- [ ] **1.2** Confirm the profile exists in your region:
  ```
  aws bedrock list-inference-profiles --region us-east-1 --type-equals SYSTEM_DEFINED \
    --query "inferenceProfileSummaries[?contains(inferenceProfileId,'haiku-4-5')].inferenceProfileId"
  ```
  *Expect:* a list including `us.anthropic.claude-haiku-4-5-20251001-v1:0`.
- [ ] **1.3** Prove your credentials can actually call it:
  ```
  aws bedrock-runtime converse --region us-east-1 \
    --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \
    --messages '[{"role":"user","content":[{"text":"Reply with the word OK"}]}]'
  ```
  *Expect:* JSON with `"text": "OK"`. If this fails, the deployed Lambda will fail the same way.
  Fix it here.

- [ ] **1.4** **Choose the model with data (optional but recommended; no deploy needed).** Run the live eval
  from your laptop. It uses only Bedrock, never openFDA:
  ```
  python -m evals.run live --repeats 3 \
    --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0
  # add a second --model-id to compare, e.g. us.anthropic.claude-sonnet-5-5 once you have access
  ```
  *Expect:* a report in `evals/results/`. **False CLEAR and False HOLD must be 0.** If they are not, that
  is a bug to fix before deploying, not model noise. Compare accuracy, over-escalation and tokens, and
  switch from Haiku only if the stronger model clearly earns it.

## Phase 2: email addresses

- [ ] **2.1** Pick three real inboxes you can open: a **sender** (the agent), a **coordinator**, and a
  **contact** for hold notices (it can be the same as the coordinator for rehearsal).
- [ ] **2.2** Make sure none of the sender/coordinator addresses is already an SES identity
  (the stack creates them and fails with "already exists" otherwise):
  ```
  aws sesv2 list-email-identities --region us-east-1
  ```
- [ ] **2.3** In `demo/demo_items.csv`, replace every `buyer@example.com` with your contact inbox.

## Phase 3: deploy

- [ ] **3.1** One-time per account/region: `cdk bootstrap aws://ACCOUNT/us-east-1`
- [ ] **3.2** Preview (the first run downloads `openpyxl` from PyPI, so it needs internet):
  ```
  cd infra
  cdk synth -c sender=agent@yourdomain.com -c coordinator=coord@yourdomain.com \
    -c model_id=us.anthropic.claude-haiku-4-5-20251001-v1:0 -c demo_mode=true > /dev/null
  ```
  *Expect:* no errors. A bad `model_id` fails here with a clear message.
- [ ] **3.3** Deploy (`demo_mode=true` is for rehearsal only; production uses `false`):
  ```
  cdk deploy -c sender=agent@yourdomain.com -c coordinator=coord@yourdomain.com \
    -c model_id=us.anthropic.claude-haiku-4-5-20251001-v1:0 -c demo_mode=true \
    -c schedule_tz=America/Los_Angeles
  ```
  Add `-c guardrails=false` to skip the Bedrock guardrail (it needs permission to create one). Otherwise a
  guardrail that screens recall text for prompt attacks is created and wired in.
  *Expect:* approve the IAM changes (`y`). Outputs: `BucketName`, `TableName`, `FunctionName`, `DashboardUrl`, `GuardrailId`,
  `LogGroupName`, `DemoMode`, `UploadInventory`. **Copy them.**
  Set `schedule_tz` to the coordinator's real time zone.

## Phase 4: verify email (do not skip)

- [ ] **4.1** AWS emails a verification link to the sender and the coordinator. Click both.
- [ ] **4.2** AWS SNS emails the coordinator to confirm the alarm subscription. Click it.
- [ ] **4.3** If the hold-notice contact is a different address, verify it too (new SES accounts are
  in the **sandbox** and can only send to verified addresses):
  ```
  aws sesv2 create-email-identity --email-address contact@yourdomain.com --region us-east-1
  ```
  then click the link.
- [ ] **4.4** Check all of them:
  ```
  aws sesv2 get-email-identity --email-identity agent@yourdomain.com --region us-east-1 \
    --query VerifiedForSendingStatus
  ```
  *Expect:* `true` for each address.
- [ ] **4.5** To email real, unverified recipients, request **SES production access** in the SES
  console. This can take a day or more, so start early if you need it.

## Phase 5: first real run

- [ ] **5.1** Upload the inventory: `aws s3 cp demo/demo_items.csv s3://BUCKET/inventory/items.csv`
- [ ] **5.2** Invoke with a date you will **not** use in the demo:
  ```
  aws lambda invoke --function-name FUNCTION --region us-east-1 \
    --cli-binary-format raw-in-base64-out --cli-read-timeout 900 \
    --payload '{"run_date":"2026-10-06"}' out.json && cat out.json
  ```
  *Expect:* JSON with a `summary`, `workbook_key`, and `notices`. With the demo inventory and no
  injected faults, most items come back clear. Items that don't are worth reading in the record.
  Each item now makes several openFDA queries (name, variants, UPCs) before the model decides, so runs take
  longer than model time alone. Note the duration in the logs on this first run.
- [ ] **5.3** The coordinator inbox received **"Recall check 2026-10-06: ..."**. Check spam too.
- [ ] **5.4** Download and open the workbook:
  ```
  aws s3 cp s3://BUCKET/records/2026-10-06/weekly-recall-check.xlsx .
  ```
  *Expect:* your template, filled, plus an "Agent Audit Trail" sheet. "Signoff (Name & Date)" is blank.
- [ ] **5.5** Read the logs once: `aws logs tail LOGGROUP --since 15m`
- [ ] **5.7** Open the `DashboardUrl` output. Metrics appear a few minutes after a run. Check items by decision,
  tokens, openFDA failures, and **run duration against the 15-minute limit**. Take the tokens and price them for a
  real cost per run.
- [ ] **5.8** Measure the real guardrail (it can only be done against your account). Three injection phrasings should
  be flagged and two legitimate-but-alarming recalls must **not** be:
  ```
  python -m evals.run live --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 --repeats 1 \
    --cases evals/injection.json --guardrail-id GUARDRAIL_ID --guardrail-version 1
  ```
  *Expect (as measured on the author's account):* INJ1 and INJ3 flagged, INJ2 (a fake "SYSTEM" message) **not** flagged, and
  **0 false alarms** on CTL1 and CTL2. The report now lists the guardrail action per case. Your account may differ slightly.
  The set now has 15 cases. Only INJ1 to INJ3 and CTL1, CTL2 have known results; INJ4 to INJ10 and CTL3 to CTL5 are new, so
  record what you see (the report lists the filter's action for every case).
  If it misses an injection, the code still stops it (see the README), but note it. If it flags the controls, edit the filter
  strength in `infra/stack.py`.
- [ ] **5.6** Re-run the same command. *Expect:* `"resumed"` equals the number of items. Nothing is
  re-queried and no second email goes out for any hold.

## Phase 6: failure scenes (needs `demo_mode=true`)

Each scene uses its own run date. Payloads and expected outcomes are in
[`docs/DEMO_SCRIPT.md`](DEMO_SCRIPT.md). Between rehearsals, reset state:

- [ ] **6.1**
  ```
  python scripts/reset_demo_state.py --table TABLE \
    --run-dates 2026-10-13 2026-10-14 2026-10-15 --yes
  ```
  *Expect:* `deleted N row(s)`. It removes only the listed dates and the hold-notice ledger.
- [ ] **6.2** Confirm injected faults are refused when demo mode is off (do this after step 7.1):
  invoke with `"demo_fault"` and expect an error containing `demo_mode=true`.

## Phase 7: switch to production behavior

- [ ] **7.1** Redeploy **without** `-c demo_mode=true`. Check the `DemoMode` output says `false`.
- [ ] **7.2** Replace the demo inventory with the coordinator's real export (see the format in
  `recall_agent/inventory.py`). A bad file fails loudly with row numbers.
- [ ] **7.3** Confirm the schedule: Mondays 06:00 in your `schedule_tz`. The Monday summary email is
  the heartbeat. No email means the agent didn't run.

## Phase 8: cost and cleanup

- [ ] **8.1** After a few runs, look at AWS Cost Explorer, filtered to Bedrock, Lambda, SES, S3 and
  DynamoDB. Put the real **cost per weekly run** in the README. (Not measured yet.)
- [ ] **8.2** To remove everything: `cdk destroy` (same `-c` flags). The S3 bucket and DynamoDB table are
  **retained on purpose** (they hold audit records). Delete them by hand if you really want them gone.
  The bucket is versioned, so empty all versions first.

---

## Troubleshooting

| You see | Likely cause | Fix |
| --- | --- | --- |
| `AccessDeniedException` on `bedrock:InvokeModel` | Wrong/typo'd `model_id`, model access not granted, or an SCP blocks region `unspecified` (global profiles) | Re-run step 1.3. Make sure the Lambda's `BEDROCK_MODEL_ID` equals the ID you tested. Use the `us.` profile if your org uses region SCPs. |
| `on-demand throughput isn't supported` | Used a bare model ID | Use the `us.` or `global.` inference profile ID |
| `ResourceNotFoundException` / model not found | Wrong region for that profile | Step 1.2, and set `AWS_REGION` to match |
| `MessageRejected: Email address is not verified` | SES sandbox | Verify the recipient (4.3) or get production access (4.5). The record shows the notice as **NOT sent, contact manually**. |
| Deploy fails: identity `AlreadyExists` | Sender/coordinator already an SES identity | Delete that identity or use another address |
| Summary says "Workbook could not be generated" | Stale package missing `openpyxl` | `python infra/package.py`, then `cdk deploy` again |
| CLI says `Read timeout` | Default CLI timeout is 60 s | Add `--cli-read-timeout 900` |
| Invocation error: `demo_fault is only honored when...` | Demo mode is off | Expected in production. Redeploy with `-c demo_mode=true` for rehearsal only. |
| `AccessDeniedException` on `bedrock:ApplyGuardrail` | The deploy was done with `guardrails=false`, or the stack was created before guardrails were added | Redeploy with the default. The result is a flagged item, never a silent pass: the check fails closed |
| Items flagged "Bedrock Guardrails could not check the recall text" | Guardrail unreachable, throttled or misconfigured | Safe by design (they go to review). Check `GUARDRAIL_ID`/`GUARDRAIL_VERSION` on the Lambda and the IAM policy |
| Guardrail flags a legitimate recall | The Prompt Attack filter is at HIGH strength | Step 5.8 shows how often. Lower `input_strength` in `infra/stack.py` if it is noisy |
| Dashboard is empty | No run yet, or the first run is minutes old | Run the Lambda once and wait a few minutes |
| No summary email at all | Not verified, in spam, or the run failed | Check spam, step 4.4, the error alarm email, then `aws logs tail LOGGROUP` |
| Summary note "Circuit breaker: 3 items in a row could not be queried" | openFDA was down or rate-limiting | Safe by design: nothing is marked clear. Rerun the same date later. Finished items are reused. |
| Every item NOT CHECKED | Real openFDA throttling or outage | Safe by design: nothing is marked clear. Re-run the same date later. Only unverified items are retried. |
