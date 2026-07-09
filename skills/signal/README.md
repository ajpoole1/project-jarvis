# signal

AWS Signal stack skill — monitor, control, and remediate the nightly batch pipeline.

## Stage-then-approve override (declared, not an oversight)

SKILL_GUIDE's default for consequential actions is stage-then-approve (propose,
wait for AJ's ✅). This skill **deliberately supersedes that** for `workbench` and
`remediate`: they invoke Lambdas and retrigger DAGs autonomously, with no
interactive approval step. That is the point of an operator-of-record — Jarvis acts
on a red run-summary while the box is off and AJ is asleep.

The substitute for interactive approval is a bounded, auditable envelope (plan v1.2
§6.4–6.5):
- **claim-then-act** — every action is written to `signal/retry-log/` in S3 *before*
  it fires, so the S3 log is a complete audit trail of every autonomous action;
- **single retry budget** — one retrigger per `(dag_id, logical_date)`; a second
  failure STOPs and escalates to AJ with logs;
- **hard timeout leash** — remediation self-terminates well inside the maintenance-
  mode TTL and stops the workbench on timeout;
- **read-only elsewhere** — no raw start/stop primitives (single-mutation-path rule).

This note exists so the supersession is explicit per QA_LENSES §3, not read as a
missing approval gate on the next review.

## Setup

```bash
cd skills/signal
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Audit log

Every autonomous action (retry claim, workbench start/stop, retry resolve,
escalation) is written to `~/.jarvis/logs/signal.log` (override dir with
`JARVIS_LOG_DIR`) in addition to stdout. This is the durable operational trail for
unattended overnight runs — the S3 `retry-log/` is the per-DAG budget record; this
file is the chronological action log. Matches the devqueue skill's logging pattern.

## Required env vars (add to ~/.jarvis.env)

| Var | Description |
|---|---|
| `SIGNAL_S3_BUCKET` | Platform S3 bucket name (e.g. `altaforma-personal-projects`) |
| `SIGNAL_LAMBDA_START_ARN` | ARN of the workbench-start Lambda |
| `SIGNAL_LAMBDA_STOP_ARN` | ARN of the workbench-stop Lambda |
| `SIGNAL_AWS_REGION` | AWS region (default: `ca-central-1`) |
| `SIGNAL_SSM_TARGET` | EC2 instance ID for SSM port-forward (required for `remediate`) |
| `SIGNAL_AIRFLOW_TOKEN_SECRET` | Secrets Manager secret name for the jarvis Airflow token (default: `signal/airflow/jarvis-token`) |
| `SIGNAL_AIRFLOW_PORT` | Airflow webserver port (default: `8080`) |
| `SIGNAL_EC2_NAME_TAG` | EC2 Name tag for status lookup (default: `signal-batch`) |
| `SIGNAL_RDS_IDENTIFIER` | RDS DB instance identifier for status lookup |
| `SIGNAL_MAX_REMEDIATION_MINUTES` | Hard timeout for remediation sessions (default: `180`; must be ≤ 4h session backstop) |

## IAM posture (Jarvis's personal-stack role — §6.2)

- `lambda:InvokeFunction` on `SIGNAL_LAMBDA_START_ARN` and `SIGNAL_LAMBDA_STOP_ARN`
- `ec2:Describe*`, `rds:Describe*` read-only (status only)
- `s3:GetObject` on `signal/run-summaries/*`
- `s3:GetObject` + `s3:PutObject` on `signal/retry-log/*` (claim-then-act writes)
- `s3:ListBucket` on `signal/` prefix
- **No raw `ec2:Start/Stop` or `rds:Start/Stop`** — those live solely in the Lambda execution role (single-mutation-path rule, §3.6). One implementation, many invokers.

## Subcommands

### `summary` — read a nightly run-summary from S3

```bash
python skill.py summary                  # latest run
python skill.py summary --date 2026-07-08
python skill.py summary --raw            # print raw JSON
```

Box does not need to be running — S3 is always reachable.

### `workbench` — start / stop / status via Lambda

```bash
python skill.py workbench status         # Describe-only; no start/stop
python skill.py workbench start          # on-demand session
python skill.py workbench start --mode maintenance   # for remediation
python skill.py workbench stop
```

Start and stop invoke the shared Lambdas — the same ones the nightly Scheduler uses.
There is exactly one start path; Jarvis is a second invoker, not a second implementation.
Max session backstop (4h) applies to all Jarvis-initiated sessions.

### `remediate` — one-shot remediation loop

```bash
python skill.py remediate                        # latest red summary, first failed DAG
python skill.py remediate --date 2026-07-08
python skill.py remediate --dag-id dag_stock_ingest
python skill.py remediate --force                # override stale in-progress claim
```

**Retry budget:** Jarvis retriggers a failed DAG exactly once. A second failure escalates
to AJ with logs attached. The retry record lives at `s3://<bucket>/signal/retry-log/`
and is written **before** the start Lambda is invoked (claim-then-act), so a crash
mid-remediation leaves a visible claimed record rather than silently resetting the budget.

**Monitoring leash:** the remediation loop polls Airflow with a hard timeout of
`SIGNAL_MAX_REMEDIATION_MINUTES` (default 180m). On timeout, the workbench is stopped
and AJ is escalated — the loop never waits indefinitely inside a maintenance-mode
session that has disabled the 5am backstop.

### `schema` — print the run-summary JSON schema v1

```bash
python skill.py schema
```

Prints `run_summary_schema_v1.json`. This is the contract Signal's orchestrator writes
to and Jarvis's parser reads from. Signal reviews the schema for "can I populate every
field cheaply from orchestrator context" — Jarvis drafted it as the consumer.

## S3 layout (signal/ prefix)

```
signal/
  run-summaries/          # nightly run-summary JSON (written by Signal)
    2026-07-08.json
    …
  retry-log/              # Jarvis's autonomous action audit trail
    dag_stock_ingest/
      2026-07-08.json     # retry claim + outcome for this dag/date
    …
```

## Run-summary schema

See `run_summary_schema_v1.json` for the full JSON Schema. Floor fields (§6.3 of the
migration plan):

- `meta` — run_date, overall_status, window_start_utc, window_end_utc
- `deploy` — exit_status, tier, commit_hash, timestamp_utc (deploy.sh outcome; **nullable in v1** — key always present, value null until deploy.sh writes `deploy_status.json` in v1.1; tier ∈ {no-change, code-only, compose-rebuild, migration-applied, unknown})
- `dags[]` — dag_id, dag_run_id, logical_date, state, wall_clock_seconds, failed_tasks
- `rows_written` — table → rows-present-for-run_date map; **fixed table set always present, zeros never elided** (a present zero is the holiday-vs-outage discriminator)
- `data_gates` — plausibility check pass/fail (populated by hardening pass; absent = not yet implemented)
- `quarantine_count` — integer
- `wind_down` — **producer-facts only**: attempted (bool), result ∈ {invoked, skipped_other_dags_running, invoke_failed}, detail. NOT a `status` enum — `completed`/`backstop` are Jarvis-side inferences (stop-event + summary presence; missing summary by ~6am = backstop), never fields.

`schema_version` is required; Jarvis's parser gates on it. Bump for any breaking change.
