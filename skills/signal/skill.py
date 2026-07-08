"""Signal skill — AWS stack monitor, workbench controller, and remediation loop.

Sub-capabilities:
  summary    — read the latest (or a specific date) nightly run-summary from S3
  workbench  — start / stop / status via Lambda invoke (never raw EC2/RDS primitives)
  remediate  — one-shot remediation: read summary → start → retrigger → monitor → stop
  schema     — print the run-summary JSON schema v1 (for Signal's writer to review)

IAM posture (Jarvis's personal-stack role — §6.2 of migration plan):
  lambda:InvokeFunction  on workbench-start and workbench-stop Lambdas
  ec2:Describe*, rds:Describe*  read-only (status only)
  s3:GetObject             on signal/run-summaries/*
  s3:GetObject+PutObject   on signal/retry-log/*  (claim-then-act writes)
  s3:ListBucket            on signal/ prefix
  No raw ec2:Start/Stop or rds:Start/Stop — those live solely in the Lambda
  execution role (single-mutation-path rule, §3.6).

Retry budget: enforced via S3 (s3://<bucket>/signal/retry-log/<dag_id>/<date>.json).
  Claim written BEFORE invoking the start Lambda (claim-then-act). A crash mid-
  remediation leaves a claimed record; Jarvis reads it and escalates rather than
  silently double-firing.

  Concurrency model: Jarvis is a single persistent session, so remediate is
  invoked serially — there is no concurrent second remediator by design. The
  claim path is check-then-write (not an atomic S3 CAS), so it is NOT safe under
  concurrent invocation; _claim_retry adds a best-effort read-after-write guard
  that aborts if it observes a racing claim, but the durable guarantee rests on
  the single-invoker assumption above. If remediation ever becomes multi-invoker,
  replace the claim with a conditional put (If-None-Match) on a per-attempt key.

Monitoring leash: remediate polls Airflow REST API via SSM port-forward with a
  hard timeout of MAX_REMEDIATION_MINUTES (default 180, well under the 4h session
  backstop). On timeout Jarvis stops the workbench and escalates — never waits
  indefinitely inside a maintenance-mode session. Note: maintenance mode is an SSM
  Parameter with a TTL; a flag older than its TTL is treated as false by the stop
  Lambda (backstop wins). Jarvis's leash must fire before the TTL expires — the
  default 180m is well within the ≥4h TTL floor (§3.3).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging — durable audit trail for unattended overnight runs. Autonomous
# actions (claim, Lambda invoke, resolve, escalate) are logged here in addition
# to stdout, so the operational trail survives even when nobody is watching live
# and Jarvis's stdout is not captured.
# ---------------------------------------------------------------------------

_LOG_PATH = (
    Path(os.environ.get("JARVIS_LOG_DIR", str(Path.home() / ".jarvis" / "logs"))) / "signal.log"
)


def _setup_logging() -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(_LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s signal %(message)s",
    )


def _say(msg: str, level: int = logging.INFO) -> None:
    """Print to stdout (Jarvis's dispatcher relays this) AND log durably."""
    print(msg)
    logging.log(level, msg.replace("[signal] ", "").strip())


# ---------------------------------------------------------------------------
# Env bootstrap
# ---------------------------------------------------------------------------


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        for q in ('"', "'"):
            if value.startswith(q):
                end = value.find(q, 1)
                if end != -1:
                    value = value[1:end]
                break
        else:
            for sep in (" #", "\t#"):
                pos = value.find(sep)
                if pos != -1:
                    value = value[:pos].rstrip()
                    break
        if key:
            os.environ.setdefault(key, value)


_load_env(Path.home() / ".jarvis.env")

# The run-summary schema version this parser understands. A summary carrying a
# different version is a producer/consumer contract mismatch — gate on it rather
# than silently mis-parsing a future shape (the schema itself documents this
# field as the one the parser gates on).
EXPECTED_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Config — all from env; no defaults that silently point at prod resources
# ---------------------------------------------------------------------------


def _require(var: str) -> str:
    val = os.environ.get(var, "").strip()
    if not val:
        sys.exit(f"[signal] required env var {var!r} is not set — check ~/.jarvis.env")
    return val


def _cfg() -> dict:
    return {
        "bucket": _require("SIGNAL_S3_BUCKET"),
        "lambda_start": _require("SIGNAL_LAMBDA_START_ARN"),
        "lambda_stop": _require("SIGNAL_LAMBDA_STOP_ARN"),
        "aws_region": os.environ.get("SIGNAL_AWS_REGION", "ca-central-1"),
        "airflow_port": int(os.environ.get("SIGNAL_AIRFLOW_PORT", "8080")),
        "ssm_target": os.environ.get("SIGNAL_SSM_TARGET", ""),  # EC2 instance ID for port-forward
        "airflow_token_secret": os.environ.get(
            "SIGNAL_AIRFLOW_TOKEN_SECRET", "signal/airflow/jarvis-token"
        ),
        "max_remediation_min": int(os.environ.get("SIGNAL_MAX_REMEDIATION_MINUTES", "180")),
    }


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------


def _boto(service: str, cfg: dict):
    try:
        import boto3
    except ImportError:
        sys.exit(
            "[signal] boto3 not installed — run: pip install -r skills/signal/requirements.txt"
        )
    return boto3.client(service, region_name=cfg["aws_region"])


def _invoke_lambda(arn: str, payload: dict, cfg: dict) -> dict:
    client = _boto("lambda", cfg)
    resp = client.invoke(
        FunctionName=arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = resp["Payload"].read()
    return json.loads(body) if body else {}


def _workbench_status(cfg: dict) -> dict:
    """Read-only describe of EC2 + RDS state. No start/stop — Describe* only."""
    ec2 = _boto("ec2", cfg)
    rds = _boto("rds", cfg)

    ec2_tag = os.environ.get("SIGNAL_EC2_NAME_TAG", "signal-batch")
    rds_id = os.environ.get("SIGNAL_RDS_IDENTIFIER", "")

    ec2_state = "unknown"
    ec2_id = ""
    try:
        resp = ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [ec2_tag]}])
        reservations = resp.get("Reservations", [])
        if reservations:
            inst = reservations[0]["Instances"][0]
            ec2_state = inst["State"]["Name"]
            ec2_id = inst["InstanceId"]
    except Exception as e:
        ec2_state = f"error:{e}"

    rds_state = "unknown"
    if rds_id:
        try:
            resp = rds.describe_db_instances(DBInstanceIdentifier=rds_id)
            rds_state = resp["DBInstances"][0]["DBInstanceStatus"]
        except Exception as e:
            rds_state = f"error:{e}"
    else:
        rds_state = "not-configured"

    return {"ec2": ec2_state, "ec2_id": ec2_id, "rds": rds_state}


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------


def _s3_get_json(bucket: str, key: str, cfg: dict) -> dict | None:
    s3 = _boto("s3", cfg)
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        sys.exit(f"[signal] S3 read failed s3://{bucket}/{key}: {e}")


def _s3_put_json(bucket: str, key: str, data: dict, cfg: dict) -> None:
    s3 = _boto("s3", cfg)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, indent=2).encode(),
        ContentType="application/json",
    )


def _latest_summary_key(bucket: str, cfg: dict) -> str | None:
    s3 = _boto("s3", cfg)
    prefix = "signal/run-summaries/"
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    objects = resp.get("Contents", [])
    if not objects:
        return None
    return max(objects, key=lambda o: o["LastModified"])["Key"]


# ---------------------------------------------------------------------------
# Retry log (S3-durable, claim-then-act)
# ---------------------------------------------------------------------------

_RETRY_PREFIX = "signal/retry-log/"


def _retry_key(dag_id: str, logical_date: str) -> str:
    return f"{_RETRY_PREFIX}{dag_id}/{logical_date}.json"


def _read_retry_record(dag_id: str, logical_date: str, cfg: dict) -> dict:
    key = _retry_key(dag_id, logical_date)
    record = _s3_get_json(cfg["bucket"], key, cfg)
    return record or {"dag_id": dag_id, "logical_date": logical_date, "attempts": []}


def _claim_retry(dag_id: str, logical_date: str, cfg: dict) -> dict:
    """Write a 'claimed' attempt record BEFORE invoking the start Lambda.

    Returns the updated record. Caller must check len(record['attempts']) after
    claiming — if this would be attempt #2+, abort before invoking.

    Best-effort race guard: re-read after write and confirm our claim is the last
    attempt with our claimed_at. If a concurrent invocation interleaved, the
    read-back won't match and we abort (SystemExit). This is not a true CAS — see
    the module docstring's single-invoker assumption — but it closes the common
    check-then-act window rather than silently double-claiming.
    """
    stamp = datetime.now(UTC).isoformat()
    record = _read_retry_record(dag_id, logical_date, cfg)
    record["attempts"].append({"claimed_at": stamp, "outcome": "in_progress"})
    _s3_put_json(cfg["bucket"], _retry_key(dag_id, logical_date), record, cfg)

    verify = _read_retry_record(dag_id, logical_date, cfg)
    if not verify["attempts"] or verify["attempts"][-1].get("claimed_at") != stamp:
        sys.exit(
            "[signal] retry claim lost a race (read-back mismatch) — another "
            "remediation may be in flight. Refusing to double-fire; escalate to AJ."
        )
    return verify


def _resolve_retry(dag_id: str, logical_date: str, outcome: str, detail: str, cfg: dict) -> None:
    record = _read_retry_record(dag_id, logical_date, cfg)
    if record["attempts"]:
        record["attempts"][-1]["outcome"] = outcome
        record["attempts"][-1]["resolved_at"] = datetime.now(UTC).isoformat()
        record["attempts"][-1]["detail"] = detail
    _s3_put_json(cfg["bucket"], _retry_key(dag_id, logical_date), record, cfg)


# ---------------------------------------------------------------------------
# Airflow REST API via SSM port-forward
# ---------------------------------------------------------------------------


def _airflow_token(cfg: dict) -> str:
    """Fetch jarvis Airflow token from Secrets Manager."""
    sm = _boto("secretsmanager", cfg)
    resp = sm.get_secret_value(SecretId=cfg["airflow_token_secret"])
    return resp["SecretString"].strip()


def _ssm_portforward_cmd(cfg: dict) -> list[str]:
    if not cfg["ssm_target"]:
        sys.exit(
            "[signal] SIGNAL_SSM_TARGET (EC2 instance ID) is not set — required for Airflow API access"
        )
    return [
        "aws",
        "ssm",
        "start-session",
        "--target",
        cfg["ssm_target"],
        "--document-name",
        "AWS-StartPortForwardingSession",
        "--parameters",
        json.dumps(
            {
                "portNumber": [str(cfg["airflow_port"])],
                "localPortNumber": [str(cfg["airflow_port"])],
            }
        ),
        "--region",
        cfg["aws_region"],
    ]


def _airflow_get(path: str, token: str, cfg: dict) -> dict:
    import urllib.request

    url = f"http://localhost:{cfg['airflow_port']}/api/v1{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _airflow_trigger_dag(dag_id: str, logical_date: str, token: str, cfg: dict) -> dict:
    import urllib.request

    url = f"http://localhost:{cfg['airflow_port']}/api/v1/dags/{dag_id}/dagRuns"
    payload = json.dumps({"logical_date": logical_date}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------


def _check_schema_version(summary: dict) -> str | None:
    """Return a warning string if the summary's schema_version is not the one this
    parser understands, else None. A mismatch means the producer moved ahead of the
    consumer (or vice versa) — the fields below may be shaped differently."""
    version = summary.get("schema_version")
    if version == EXPECTED_SCHEMA_VERSION:
        return None
    return (
        f"[signal] WARNING: run-summary schema_version={version!r} but this parser "
        f"expects {EXPECTED_SCHEMA_VERSION}. Fields may be misread — update skills/signal/."
    )


def cmd_summary(args: argparse.Namespace, cfg: dict) -> None:
    bucket = cfg["bucket"]
    if args.date:
        key = f"signal/run-summaries/{args.date}.json"
    else:
        key = _latest_summary_key(bucket, cfg)
        if not key:
            print("[signal] no run summaries found in S3")
            return

    summary = _s3_get_json(bucket, key, cfg)
    if not summary:
        print(f"[signal] no summary at {key}")
        return

    if args.raw:
        print(json.dumps(summary, indent=2))
        return

    warning = _check_schema_version(summary)
    if warning:
        print(warning)
    _print_summary(summary)


def _print_summary(s: dict) -> None:
    meta = s.get("meta", {})
    print(
        f"\n=== Signal run summary — {meta.get('run_date', '?')} (schema v{s.get('schema_version','?')}) ==="
    )
    print(f"  Status:      {meta.get('overall_status', '?').upper()}")
    print(f"  Window:      {meta.get('window_start_utc','?')} → {meta.get('window_end_utc','?')}")

    deploy = s.get("deploy", {})
    print(f"\n  Deploy:      exit={deploy.get('exit_status','?')}  tier={deploy.get('tier','?')}")

    print("\n  DAGs:")
    for dag in s.get("dags", []):
        wall = dag.get("wall_clock_seconds")
        wall_str = f"{wall}s" if wall is not None else "?"
        print(f"    {dag['dag_id']:35s}  {dag['state']:10s}  {wall_str}")
        if dag.get("failed_tasks"):
            for ft in dag["failed_tasks"]:
                print(f"      ↳ FAILED task: {ft['task_id']}")
                if ft.get("log_excerpt"):
                    for line in ft["log_excerpt"][:3]:
                        print(f"         {line}")

    print("\n  Rows written:")
    for table, count in s.get("rows_written", {}).items():
        print(f"    {table:35s}  {count:>10,}")

    dg = s.get("data_gates", {})
    if dg:
        print("\n  Data gates:")
        for check, result in dg.items():
            mark = "✓" if result.get("passed") else "✗"
            print(f"    {mark} {check}: {result.get('detail','')}")

    print(f"\n  Quarantine count:  {s.get('quarantine_count', '?')}")
    wd = s.get("wind_down", {})
    print(f"  Wind-down:         {wd.get('status','?')}")
    print()


def cmd_workbench(args: argparse.Namespace, cfg: dict) -> None:
    action = args.action

    if action == "status":
        status = _workbench_status(cfg)
        print(f"  EC2 ({status['ec2_id'] or 'unknown'}): {status['ec2']}")
        print(f"  RDS:                  {status['rds']}")
        return

    if action == "start":
        _say(f"[signal] invoking workbench-start Lambda (mode={args.mode}) …")
        result = _invoke_lambda(cfg["lambda_start"], {"source": "jarvis", "mode": args.mode}, cfg)
        print(f"[signal] Lambda response: {json.dumps(result, indent=2)}")
        print(
            f"[signal] Workbench starting (max session: {cfg['max_remediation_min']}m backstop applies)"
        )
        return

    if action == "stop":
        _say("[signal] invoking workbench-stop Lambda …")
        result = _invoke_lambda(cfg["lambda_stop"], {"source": "jarvis"}, cfg)
        print(f"[signal] Lambda response: {json.dumps(result, indent=2)}")
        return

    sys.exit(f"[signal] unknown workbench action: {action!r}")


def cmd_remediate(args: argparse.Namespace, cfg: dict) -> None:
    """Full remediation loop for a single failed DAG.

    Flow:
      1. Read run summary (from S3 — works box-off)
      2. Identify failed DAG(s); pick one if not specified
      3. Check retry record — abort if already attempted
      4. Write 'claimed' retry record to S3 BEFORE invoking Lambda (claim-then-act)
      5. Invoke workbench-start Lambda (maintenance-mode session)
      6. Open SSM port-forward; poll Airflow API until workbench ready
      7. Retrigger failed DAG via Airflow REST API
      8. Monitor run with hard timeout ≤ MAX_REMEDIATION_MINUTES
      9. On success: resolve retry record, invoke workbench-stop
     10. On failure or timeout: resolve record, invoke workbench-stop, escalate to AJ
    """
    bucket = cfg["bucket"]
    max_seconds = cfg["max_remediation_min"] * 60

    # Step 1 — load summary
    if args.date:
        key = f"signal/run-summaries/{args.date}.json"
    else:
        key = _latest_summary_key(bucket, cfg)
        if not key:
            sys.exit("[signal] no run summaries found — cannot remediate")

    summary = _s3_get_json(bucket, key, cfg)
    if not summary:
        sys.exit(f"[signal] no summary at {key}")

    # Gate hard here: remediation drives autonomous Lambda invocations and DAG
    # retriggers off these fields. A schema mismatch means we may be misreading
    # which DAG failed — refuse rather than act on it (override with --force).
    warning = _check_schema_version(summary)
    if warning:
        print(warning)
        if not args.force:
            sys.exit(
                "[signal] refusing to remediate on a schema mismatch — use --force to override"
            )

    run_date = summary.get("meta", {}).get("run_date", args.date or "unknown")
    overall = summary.get("meta", {}).get("overall_status", "unknown")
    if overall == "success" and not args.force:
        print(
            f"[signal] run {run_date} reports success — nothing to remediate (use --force to override)"
        )
        return

    # Step 2 — identify failed DAG
    failed_dags = [d for d in summary.get("dags", []) if d["state"] not in ("success", "skipped")]
    if not failed_dags:
        print("[signal] no failed DAGs found in summary")
        return

    if args.dag_id:
        target_dags = [d for d in failed_dags if d["dag_id"] == args.dag_id]
        if not target_dags:
            sys.exit(
                f"[signal] DAG {args.dag_id!r} not in failed list: {[d['dag_id'] for d in failed_dags]}"
            )
    else:
        target_dags = failed_dags[:1]  # one at a time per retry budget

    dag = target_dags[0]
    dag_id = dag["dag_id"]
    # logical_date is schema-required and keys the retry-log. Don't silently fall
    # back to the coarser run_date — a producer that omits it would corrupt the
    # budget key. Refuse instead so the contract breach is visible.
    logical_date = dag.get("logical_date")
    if not logical_date:
        sys.exit(
            f"[signal] dag {dag_id!r} in summary has no logical_date (schema-required) — "
            "cannot key the retry budget. Escalate to AJ / check the run-summary writer."
        )

    print(f"[signal] target: {dag_id}  logical_date: {logical_date}  state: {dag['state']}")

    # Step 3 — check retry budget
    record = _read_retry_record(dag_id, logical_date, cfg)
    prior_attempts = [a for a in record.get("attempts", []) if a.get("outcome") != "in_progress"]
    if prior_attempts:
        _say(
            f"[signal] ESCALATE — {dag_id} on {logical_date} has already been retried once.",
            logging.WARNING,
        )
        print(f"  Prior attempt: {prior_attempts[-1]}")
        _print_summary(summary)
        sys.exit(1)

    # Check for a stale in_progress claim (crash recovery)
    in_progress = [a for a in record.get("attempts", []) if a.get("outcome") == "in_progress"]
    if in_progress:
        claimed_at = in_progress[-1].get("claimed_at", "unknown")
        if not args.force:
            sys.exit(
                f"[signal] stale in-progress claim found (claimed at {claimed_at}). "
                "A prior remediation may have crashed. Review s3://…/signal/retry-log/ "
                "and use --force to override, or escalate to AJ."
            )
        # --force: resolve the orphaned claim before we append a fresh one, so it
        # doesn't stay stuck at in_progress and corrupt the audit trail. It still
        # counts as a spent attempt (a crashed run consumed the budget) — the
        # budget check above already ran, so this forced run proceeds, but a
        # FUTURE run will correctly see this as a prior attempt and escalate.
        _resolve_retry(
            dag_id, logical_date, "superseded", f"forced past stale claim from {claimed_at}", cfg
        )
        _say(
            f"[signal] --force: resolved orphaned in-progress claim from {claimed_at}",
            logging.WARNING,
        )

    # Step 4 — claim retry slot in S3 BEFORE starting the workbench
    _say(f"[signal] claiming retry slot for {dag_id} / {logical_date} …")
    record = _claim_retry(dag_id, logical_date, cfg)
    _say(f"[signal] claimed (total attempts now: {len(record['attempts'])})")

    remediation_start = time.monotonic()

    # Bind BEFORE the try: the finally clause references pf_proc, and the first
    # statements inside the try (start Lambda, token fetch) can raise. If pf_proc
    # were only bound partway through the try, an early raise would trip an
    # UnboundLocalError in finally — masking the real error and skipping the
    # except block's retry-resolve + workbench-stop, i.e. the exact crash-safety
    # guarantee claim-then-act promises.
    pf_proc = None

    try:
        # Step 5 — start workbench
        _say(
            f"[signal] remediating {dag_id} / {logical_date} — invoking workbench-start (maintenance) …"
        )
        _invoke_lambda(cfg["lambda_start"], {"source": "jarvis", "mode": "maintenance"}, cfg)
        print(f"[signal] workbench starting — hard session ceiling: {cfg['max_remediation_min']}m")

        # Step 6 — wait for workbench ready (deploy.sh must exit 0)
        token = _airflow_token(cfg)
        print("[signal] waiting for Airflow to become reachable …")

        if cfg["ssm_target"]:
            pf_proc = subprocess.Popen(
                _ssm_portforward_cmd(cfg),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        ready = False
        deadline = remediation_start + max_seconds
        while time.monotonic() < deadline:
            try:
                _airflow_get("/health", token, cfg)
                ready = True
                break
            except Exception:
                time.sleep(30)

        if not ready:
            raise TimeoutError("Airflow did not become reachable within the timeout")

        print("[signal] Airflow is up — retriggering DAG …")

        # Step 7 — retrigger
        run_resp = _airflow_trigger_dag(dag_id, logical_date, token, cfg)
        new_run_id = run_resp.get("dag_run_id", "?")
        print(f"[signal] triggered dag_run_id={new_run_id}")

        # Step 8 — monitor with leash
        state = "queued"
        poll_path = f"/dags/{dag_id}/dagRuns/{new_run_id}"
        while time.monotonic() < deadline and state not in ("success", "failed"):
            time.sleep(60)
            try:
                run_data = _airflow_get(poll_path, token, cfg)
                state = run_data.get("state", state)
                elapsed = int(time.monotonic() - remediation_start)
                print(f"[signal] {dag_id} state={state} elapsed={elapsed}s")
            except Exception as e:
                print(f"[signal] poll error (will retry): {e}")

        if time.monotonic() >= deadline and state not in ("success", "failed"):
            raise TimeoutError(
                f"DAG {dag_id} still in state={state!r} after {cfg['max_remediation_min']}m — hit leash"
            )

        if state == "success":
            _resolve_retry(dag_id, logical_date, "success", f"dag_run_id={new_run_id}", cfg)
            _say(f"[signal] {dag_id} succeeded — resolving retry record")
            _say("[signal] invoking workbench-stop Lambda …")
            _invoke_lambda(cfg["lambda_stop"], {"source": "jarvis"}, cfg)
            _say("[signal] done.")
        else:
            raise RuntimeError(f"DAG {dag_id} ended in state={state!r}")

    except Exception as exc:
        detail = str(exc)
        _resolve_retry(dag_id, logical_date, "failed", detail, cfg)
        _say(f"\n[signal] ESCALATE — remediation failed: {detail}", logging.WARNING)
        _say("[signal] invoking workbench-stop Lambda …")
        try:
            _invoke_lambda(cfg["lambda_stop"], {"source": "jarvis"}, cfg)
        except Exception as stop_err:
            _say(f"[signal] WARNING: stop Lambda also failed: {stop_err}", logging.ERROR)
        print("\n--- Summary for AJ ---")
        _print_summary(summary)
        print(f"Retry record: s3://{cfg['bucket']}/{_retry_key(dag_id, logical_date)}")
        sys.exit(1)
    finally:
        if pf_proc is not None:
            pf_proc.terminate()
            # aws ssm start-session spawns a session-manager-plugin child; wait for
            # it so we don't leave an orphaned port-forward tunnel into the box.
            try:
                pf_proc.wait(timeout=10)
            except Exception:
                pf_proc.kill()


def cmd_schema(_args: argparse.Namespace, _cfg: dict) -> None:
    """Print the run-summary JSON schema v1."""
    schema_path = Path(__file__).parent / "run_summary_schema_v1.json"
    if not schema_path.exists():
        sys.exit(
            "[signal] schema file not found — expected run_summary_schema_v1.json alongside skill.py"
        )
    print(schema_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(prog="signal", description="Signal AWS stack skill")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # summary
    p_sum = sub.add_parser("summary", help="Read a nightly run-summary from S3")
    p_sum.add_argument("--date", help="YYYY-MM-DD (default: latest)")
    p_sum.add_argument(
        "--raw", action="store_true", help="Print raw JSON instead of formatted brief"
    )

    # workbench
    p_wb = sub.add_parser("workbench", help="Control the AWS workbench via Lambda")
    p_wb.add_argument("action", choices=["start", "stop", "status"])
    p_wb.add_argument(
        "--mode",
        default="on-demand",
        help="start mode: on-demand | maintenance (default: on-demand)",
    )

    # remediate
    p_rem = sub.add_parser("remediate", help="Run the one-shot remediation loop")
    p_rem.add_argument("--date", help="YYYY-MM-DD of the run to remediate (default: latest)")
    p_rem.add_argument("--dag-id", help="Specific DAG to retrigger (default: first failed)")
    p_rem.add_argument("--force", action="store_true", help="Override stale in-progress claim")

    # schema
    sub.add_parser("schema", help="Print the run-summary JSON schema v1")

    args = parser.parse_args()

    dispatch = {
        "summary": cmd_summary,
        "workbench": cmd_workbench,
        "remediate": cmd_remediate,
        "schema": cmd_schema,
    }
    # schema is a pure local file dump — the Signal team uses it for contract
    # review without any AWS setup, so don't require AWS config for it. Every
    # other command needs a validated cfg.
    cfg = {} if args.cmd == "schema" else _cfg()
    dispatch[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
