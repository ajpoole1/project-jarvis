"""Tests for the signal skill: retry-budget state machine, schema gating, and the
remediation crash-safety guarantee.

Failure paths covered (the ones QA flagged as untested):
  - schema_version gate: matching → no warning; mismatch → warning/refusal
  - retry claim/resolve state transitions in S3
  - retry budget: a resolved prior attempt escalates instead of re-firing
  - stale in-progress claim halts (crash-recovery) unless --force
  - remediate crash-safety: an early raise inside the try still resolves the
    retry record to 'failed' and invokes workbench-stop (the claim-then-act
    guarantee) — never an UnboundLocalError in the finally clause

The skill imports boto3 lazily inside _boto(), so the module imports fine without
it; tests stub the S3/Lambda seams directly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "signal" / "skill.py"
_spec = importlib.util.spec_from_file_location("signal_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


CFG = {
    "bucket": "test-bucket",
    "lambda_start": "arn:start",
    "lambda_stop": "arn:stop",
    "aws_region": "ca-central-1",
    "airflow_port": 8080,
    "ssm_target": "",  # no port-forward in tests
    "airflow_token_secret": "signal/airflow/jarvis-token",
    "max_remediation_min": 180,
}


# ---------------------------------------------------------------------------
# In-memory S3 double: patches the two JSON seams the skill reads/writes through
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_s3(monkeypatch):
    store: dict[str, dict] = {}

    def _get(bucket, key, cfg):
        return store.get(key)

    def _put(bucket, key, data, cfg):
        store[key] = data

    monkeypatch.setattr(_mod, "_s3_get_json", _get)
    monkeypatch.setattr(_mod, "_s3_put_json", _put)
    return store


# ---------------------------------------------------------------------------
# schema_version gate
# ---------------------------------------------------------------------------


def test_schema_version_match_no_warning():
    assert _mod._check_schema_version({"schema_version": _mod.EXPECTED_SCHEMA_VERSION}) is None


def test_schema_version_mismatch_warns():
    warn = _mod._check_schema_version({"schema_version": 99})
    assert warn is not None
    assert "99" in warn
    assert str(_mod.EXPECTED_SCHEMA_VERSION) in warn


def test_schema_version_missing_warns():
    # A summary with no version field is a mismatch, not a pass.
    assert _mod._check_schema_version({}) is not None


# ---------------------------------------------------------------------------
# schema file is self-consistent — its own bundled examples validate
# ---------------------------------------------------------------------------


def test_schema_examples_validate_against_schema():
    """Guard against schema/example drift: every example bundled in
    run_summary_schema_v1.json (top-level + per-property) must validate against the
    schema itself. Skipped if jsonschema isn't installed (not a hard test dep)."""
    import json

    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).parents[1] / "skills" / "signal" / "run_summary_schema_v1.json"
    schema = json.loads(schema_path.read_text())
    for ex in schema.get("examples", []):
        jsonschema.validate(ex, schema)


def test_schema_null_deploy_validates():
    """The v1 first-run case (deploy: null) must satisfy the schema — key present,
    value null."""
    import json

    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).parents[1] / "skills" / "signal" / "run_summary_schema_v1.json"
    schema = json.loads(schema_path.read_text())
    payload = {
        "schema_version": 1,
        "meta": {
            "run_date": "2026-07-09",
            "overall_status": "success",
            "window_start_utc": "2026-07-09T07:05:00Z",
            "window_end_utc": "2026-07-09T09:00:00Z",
        },
        "deploy": None,
        "dags": [
            {
                "dag_id": "dag_stock_ingest",
                "dag_run_id": "x",
                "logical_date": "2026-07-09T07:05:00Z",
                "state": "success",
                "wall_clock_seconds": 10,
            }
        ],
        "rows_written": {"raw_prices": 0},
        "quarantine_count": 0,
        "wind_down": {"attempted": True, "result": "invoked"},
    }
    jsonschema.validate(payload, schema)


# ---------------------------------------------------------------------------
# retry claim / resolve
# ---------------------------------------------------------------------------


def test_claim_then_resolve_round_trip(fake_s3):
    _mod._claim_retry("dag_x", "2026-07-08", CFG)
    rec = _mod._read_retry_record("dag_x", "2026-07-08", CFG)
    assert len(rec["attempts"]) == 1
    assert rec["attempts"][0]["outcome"] == "in_progress"

    _mod._resolve_retry("dag_x", "2026-07-08", "success", "dag_run_id=abc", CFG)
    rec = _mod._read_retry_record("dag_x", "2026-07-08", CFG)
    assert rec["attempts"][-1]["outcome"] == "success"
    assert rec["attempts"][-1]["detail"] == "dag_run_id=abc"


def test_claim_persists_before_action(fake_s3):
    # The record must exist in S3 immediately after claim — before any Lambda call.
    _mod._claim_retry("dag_y", "2026-07-08", CFG)
    key = _mod._retry_key("dag_y", "2026-07-08")
    assert key in fake_s3
    assert fake_s3[key]["attempts"][0]["outcome"] == "in_progress"


# ---------------------------------------------------------------------------
# remediate: budget exhaustion + crash safety
# ---------------------------------------------------------------------------


def _summary(state="failed", overall="failed"):
    return {
        "schema_version": _mod.EXPECTED_SCHEMA_VERSION,
        "meta": {"run_date": "2026-07-08", "overall_status": overall},
        "dags": [{"dag_id": "dag_ingest", "state": state, "logical_date": "2026-07-08"}],
        "rows_written": {},
        "quarantine_count": 0,
        "wind_down": {"attempted": True, "result": "invoked"},
    }


@pytest.fixture
def stub_summary(monkeypatch):
    monkeypatch.setattr(
        _mod, "_latest_summary_key", lambda b, c: "signal/run-summaries/2026-07-08.json"
    )


def test_remediate_escalates_after_one_attempt(fake_s3, stub_summary, monkeypatch):
    # Seed a resolved (failed) prior attempt: budget is spent.
    fake_s3[_mod._retry_key("dag_ingest", "2026-07-08")] = {
        "dag_id": "dag_ingest",
        "logical_date": "2026-07-08",
        "attempts": [{"claimed_at": "t0", "outcome": "failed", "resolved_at": "t1"}],
    }
    monkeypatch.setattr(
        _mod, "_s3_get_json", lambda b, k, c: _summary() if "run-summaries" in k else fake_s3.get(k)
    )

    lambda_calls = []
    monkeypatch.setattr(_mod, "_invoke_lambda", lambda arn, p, c: lambda_calls.append(arn))

    args = SimpleNamespace(date=None, dag_id=None, force=False)
    with pytest.raises(SystemExit):
        _mod.cmd_remediate(args, CFG)

    # Escalated without ever invoking the start Lambda.
    assert "arn:start" not in lambda_calls


def test_remediate_stale_claim_halts(fake_s3, stub_summary, monkeypatch):
    # An in-progress claim with no resolution = a prior crash. Halt, don't re-fire.
    fake_s3[_mod._retry_key("dag_ingest", "2026-07-08")] = {
        "dag_id": "dag_ingest",
        "logical_date": "2026-07-08",
        "attempts": [{"claimed_at": "t0", "outcome": "in_progress"}],
    }
    monkeypatch.setattr(
        _mod, "_s3_get_json", lambda b, k, c: _summary() if "run-summaries" in k else fake_s3.get(k)
    )

    lambda_calls = []
    monkeypatch.setattr(_mod, "_invoke_lambda", lambda arn, p, c: lambda_calls.append(arn))

    args = SimpleNamespace(date=None, dag_id=None, force=False)
    with pytest.raises(SystemExit):
        _mod.cmd_remediate(args, CFG)
    assert "arn:start" not in lambda_calls


def test_remediate_crash_safety_resolves_and_stops(fake_s3, stub_summary, monkeypatch):
    """The core guarantee: if the start Lambda raises AFTER the retry is claimed,
    the record must be resolved to 'failed' and the stop Lambda invoked — not an
    UnboundLocalError swallowing the error in finally."""
    monkeypatch.setattr(
        _mod, "_s3_get_json", lambda b, k, c: _summary() if "run-summaries" in k else fake_s3.get(k)
    )

    stop_calls = []

    def _invoke(arn, payload, cfg):
        if arn == "arn:start":
            raise RuntimeError("start Lambda blew up")
        stop_calls.append(arn)

    monkeypatch.setattr(_mod, "_invoke_lambda", _invoke)
    monkeypatch.setattr(_mod, "_print_summary", lambda s: None)

    args = SimpleNamespace(date=None, dag_id=None, force=False)
    with pytest.raises(SystemExit):
        _mod.cmd_remediate(args, CFG)

    # Retry record resolved to failed (budget not silently reset)…
    rec = fake_s3[_mod._retry_key("dag_ingest", "2026-07-08")]
    assert rec["attempts"][-1]["outcome"] == "failed"
    assert "start Lambda blew up" in rec["attempts"][-1]["detail"]
    # …and the workbench was stopped despite the crash.
    assert "arn:stop" in stop_calls


def test_remediate_noop_on_success_summary(fake_s3, stub_summary, monkeypatch):
    monkeypatch.setattr(
        _mod, "_s3_get_json", lambda b, k, c: _summary(state="success", overall="success")
    )
    called = []
    monkeypatch.setattr(_mod, "_invoke_lambda", lambda arn, p, c: called.append(arn))

    args = SimpleNamespace(date=None, dag_id=None, force=False)
    _mod.cmd_remediate(args, CFG)  # returns cleanly, no exit
    assert called == []


# ---------------------------------------------------------------------------
# monitoring leash — the timeout mechanism the README leans on
# ---------------------------------------------------------------------------


def _leash_cfg():
    # max_remediation_min=0 → deadline == start, so the reachability/poll loops
    # exit immediately without any real sleep.
    return {**CFG, "max_remediation_min": 0}


def test_remediate_leash_airflow_never_reachable(fake_s3, stub_summary, monkeypatch):
    """Airflow never comes up → leash fires → record resolved failed + stop invoked."""
    monkeypatch.setattr(
        _mod, "_s3_get_json", lambda b, k, c: _summary() if "run-summaries" in k else fake_s3.get(k)
    )
    monkeypatch.setattr(_mod, "_airflow_token", lambda cfg: "tok")
    monkeypatch.setattr(
        _mod,
        "_airflow_get",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection refused")),
    )
    monkeypatch.setattr(_mod, "_print_summary", lambda s: None)

    stop_calls = []
    monkeypatch.setattr(
        _mod,
        "_invoke_lambda",
        lambda arn, p, c: stop_calls.append(arn) if arn == "arn:stop" else None,
    )

    args = SimpleNamespace(date=None, dag_id=None, force=False)
    with pytest.raises(SystemExit):
        _mod.cmd_remediate(args, _leash_cfg())

    rec = fake_s3[_mod._retry_key("dag_ingest", "2026-07-08")]
    assert rec["attempts"][-1]["outcome"] == "failed"
    assert "reachable" in rec["attempts"][-1]["detail"]
    assert "arn:stop" in stop_calls


def test_remediate_leash_dag_hangs_running(fake_s3, stub_summary, monkeypatch):
    """DAG retriggered but never leaves 'running' before the deadline → leash fires."""
    monkeypatch.setattr(
        _mod, "_s3_get_json", lambda b, k, c: _summary() if "run-summaries" in k else fake_s3.get(k)
    )
    monkeypatch.setattr(_mod, "_airflow_token", lambda cfg: "tok")
    # /health succeeds (ready), the dag run stays 'running'.
    monkeypatch.setattr(
        _mod,
        "_airflow_get",
        lambda path, tok, cfg: {} if path == "/health" else {"state": "running"},
    )
    monkeypatch.setattr(_mod, "_airflow_trigger_dag", lambda *a, **k: {"dag_run_id": "run1"})
    monkeypatch.setattr(_mod, "_print_summary", lambda s: None)

    stop_calls = []
    monkeypatch.setattr(
        _mod,
        "_invoke_lambda",
        lambda arn, p, c: stop_calls.append(arn) if arn == "arn:stop" else None,
    )

    args = SimpleNamespace(date=None, dag_id=None, force=False)
    with pytest.raises(SystemExit):
        _mod.cmd_remediate(args, _leash_cfg())

    rec = fake_s3[_mod._retry_key("dag_ingest", "2026-07-08")]
    assert rec["attempts"][-1]["outcome"] == "failed"
    assert "arn:stop" in stop_calls


# ---------------------------------------------------------------------------
# remediate happy path — retrigger to success
# ---------------------------------------------------------------------------


def test_remediate_success_resolves_and_stops(fake_s3, stub_summary, monkeypatch):
    """Full happy path: workbench up, DAG retriggered, reaches success → retry
    resolved success, workbench stopped."""
    monkeypatch.setattr(
        _mod, "_s3_get_json", lambda b, k, c: _summary() if "run-summaries" in k else fake_s3.get(k)
    )
    monkeypatch.setattr(_mod, "_airflow_token", lambda cfg: "tok")
    monkeypatch.setattr(
        _mod,
        "_airflow_get",
        lambda path, tok, cfg: {} if path == "/health" else {"state": "success"},
    )
    monkeypatch.setattr(_mod, "_airflow_trigger_dag", lambda *a, **k: {"dag_run_id": "run1"})
    monkeypatch.setattr(_mod, "_print_summary", lambda s: None)
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)  # skip the 60s monitor poll wait

    calls = []
    monkeypatch.setattr(_mod, "_invoke_lambda", lambda arn, p, c: calls.append(arn) or {})

    args = SimpleNamespace(date=None, dag_id=None, force=False)
    _mod.cmd_remediate(args, CFG)  # runs to success, no exit

    rec = fake_s3[_mod._retry_key("dag_ingest", "2026-07-08")]
    assert rec["attempts"][-1]["outcome"] == "success"
    assert "arn:start" in calls and "arn:stop" in calls


def test_claim_race_guard_aborts_on_readback_mismatch(fake_s3, monkeypatch):
    """If the read-back after a claim doesn't show our stamp (a racing writer
    overwrote it), _claim_retry aborts rather than proceeding to double-fire."""
    # Put a record whose last attempt has a different stamp than what we'll write:
    # simulate this by making the read-back return a record with a foreign tail.
    calls = {"n": 0}

    def racing_get(bucket, key, cfg):
        calls["n"] += 1
        # First read (inside claim) returns empty; the verify read returns a
        # record whose last claim belongs to someone else.
        if calls["n"] >= 2:
            return {
                "dag_id": "dag_z",
                "logical_date": "2026-07-08",
                "attempts": [{"claimed_at": "someone-else", "outcome": "in_progress"}],
            }
        return None

    monkeypatch.setattr(_mod, "_s3_get_json", racing_get)

    with pytest.raises(SystemExit):
        _mod._claim_retry("dag_z", "2026-07-08", CFG)


# ---------------------------------------------------------------------------
# --force stale-claim recovery + schema-needs-no-config
# ---------------------------------------------------------------------------


def test_force_resolves_orphaned_stale_claim(fake_s3, stub_summary, monkeypatch):
    """--force past a crashed in-progress claim must resolve the orphan (not leave
    it stuck at in_progress) before claiming a fresh attempt."""
    fake_s3[_mod._retry_key("dag_ingest", "2026-07-08")] = {
        "dag_id": "dag_ingest",
        "logical_date": "2026-07-08",
        "attempts": [{"claimed_at": "crashed-t0", "outcome": "in_progress"}],
    }
    monkeypatch.setattr(
        _mod, "_s3_get_json", lambda b, k, c: _summary() if "run-summaries" in k else fake_s3.get(k)
    )
    # Fail fast right after the claim so we only exercise the stale-resolve + claim.
    monkeypatch.setattr(
        _mod,
        "_invoke_lambda",
        lambda arn, p, c: (_ for _ in ()).throw(RuntimeError("stop here"))
        if arn == "arn:start"
        else None,
    )
    monkeypatch.setattr(_mod, "_print_summary", lambda s: None)

    args = SimpleNamespace(date=None, dag_id=None, force=True)
    with pytest.raises(SystemExit):
        _mod.cmd_remediate(args, CFG)

    rec = fake_s3[_mod._retry_key("dag_ingest", "2026-07-08")]
    outcomes = [a["outcome"] for a in rec["attempts"]]
    # The orphan is resolved (no lingering in_progress from the crash) …
    assert "superseded" in outcomes
    # … and there is no stuck in_progress left behind except possibly the final
    # failed one from this forced run.
    assert outcomes[0] == "superseded"


def test_schema_runs_without_aws_config(monkeypatch, capsys):
    """`schema` must not require SIGNAL_S3_BUCKET etc. — the Signal team runs it
    standalone for contract review."""
    # Ensure the required AWS env vars are absent.
    for var in ("SIGNAL_S3_BUCKET", "SIGNAL_LAMBDA_START_ARN", "SIGNAL_LAMBDA_STOP_ARN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("sys.argv", ["signal", "schema"])

    _mod.main()  # must not SystemExit on missing AWS config
    out = capsys.readouterr().out
    assert '"schema_version"' in out


# ---------------------------------------------------------------------------
# cmd_workbench + _workbench_status
# ---------------------------------------------------------------------------


def test_workbench_start_invokes_start_lambda(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        _mod, "_invoke_lambda", lambda arn, p, c: calls.append((arn, p)) or {"ok": 1}
    )
    _mod.cmd_workbench(SimpleNamespace(action="start", mode="on-demand"), CFG)
    assert calls[0][0] == "arn:start"
    assert calls[0][1]["mode"] == "on-demand"


def test_workbench_stop_invokes_stop_lambda(monkeypatch):
    calls = []
    monkeypatch.setattr(_mod, "_invoke_lambda", lambda arn, p, c: calls.append(arn) or {})
    _mod.cmd_workbench(SimpleNamespace(action="stop", mode="on-demand"), CFG)
    assert calls == ["arn:stop"]


def test_workbench_status_reads_describe(monkeypatch, capsys):
    monkeypatch.setattr(
        _mod,
        "_workbench_status",
        lambda c: {"ec2": "running", "ec2_id": "i-abc", "rds": "available"},
    )
    _mod.cmd_workbench(SimpleNamespace(action="status", mode="on-demand"), CFG)
    out = capsys.readouterr().out
    assert "running" in out and "available" in out


def test_workbench_unknown_action_exits(monkeypatch):
    monkeypatch.setattr(_mod, "_invoke_lambda", lambda *a, **k: {})
    with pytest.raises(SystemExit):
        _mod.cmd_workbench(SimpleNamespace(action="bogus", mode="on-demand"), CFG)


def test_workbench_status_degrades_on_describe_error(monkeypatch):
    """A describe failure must surface as an error string, not crash the status call."""

    class _Boom:
        def describe_instances(self, **k):
            raise RuntimeError("access denied")

        def describe_db_instances(self, **k):
            raise RuntimeError("access denied")

    monkeypatch.setattr(_mod, "_boto", lambda service, cfg: _Boom())
    monkeypatch.setenv("SIGNAL_RDS_IDENTIFIER", "signal-db")
    status = _mod._workbench_status(CFG)
    assert status["ec2"].startswith("error:")
    assert status["rds"].startswith("error:")


# ---------------------------------------------------------------------------
# cmd_summary
# ---------------------------------------------------------------------------


def test_summary_no_summaries(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_latest_summary_key", lambda b, c: None)
    _mod.cmd_summary(SimpleNamespace(date=None, raw=False), CFG)
    assert "no run summaries" in capsys.readouterr().out


def test_summary_raw_dumps_json(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_s3_get_json", lambda b, k, c: _summary())
    _mod.cmd_summary(SimpleNamespace(date="2026-07-08", raw=True), CFG)
    out = capsys.readouterr().out
    assert '"schema_version"' in out


def test_summary_renders_null_deploy_and_winddown_result(monkeypatch, capsys):
    """Producer-shape contract: deploy may be null (pre-writer), and wind_down
    carries attempted+result, not a 'status' enum."""
    s = _summary()
    s["deploy"] = None
    s["wind_down"] = {"attempted": True, "result": "skipped_other_dags_running"}
    monkeypatch.setattr(_mod, "_s3_get_json", lambda b, k, c: s)
    _mod.cmd_summary(SimpleNamespace(date="2026-07-08", raw=False), CFG)
    out = capsys.readouterr().out
    assert "no status emitted" in out  # null deploy branch
    assert "skipped_other_dags_running" in out  # wind_down.result rendered


def test_summary_renders_populated_deploy(monkeypatch, capsys):
    """The non-null deploy branch renders exit/tier/commit (v1.1 populated case)."""
    s = _summary()
    s["deploy"] = {
        "exit_status": 0,
        "tier": "code-only",
        "commit_hash": "a0b7a3a",
        "timestamp_utc": "2026-07-08T07:02:11Z",
    }
    monkeypatch.setattr(_mod, "_s3_get_json", lambda b, k, c: s)
    _mod.cmd_summary(SimpleNamespace(date="2026-07-08", raw=False), CFG)
    out = capsys.readouterr().out
    assert "code-only" in out
    assert "a0b7a3a" in out  # commit_hash rendered


def test_summary_renders_winddown_not_reached(monkeypatch, capsys):
    s = _summary()
    s["wind_down"] = {"attempted": False, "result": "invoke_failed"}
    monkeypatch.setattr(_mod, "_s3_get_json", lambda b, k, c: s)
    _mod.cmd_summary(SimpleNamespace(date="2026-07-08", raw=False), CFG)
    assert "not reached" in capsys.readouterr().out


def test_summary_warns_on_schema_mismatch(monkeypatch, capsys):
    bad = _summary()
    bad["schema_version"] = 99
    monkeypatch.setattr(_mod, "_s3_get_json", lambda b, k, c: bad)
    _mod.cmd_summary(SimpleNamespace(date="2026-07-08", raw=False), CFG)
    assert "WARNING" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# remediate --dag-id targeting
# ---------------------------------------------------------------------------


def _multi_summary():
    s = _summary()
    s["dags"] = [
        {"dag_id": "dag_a", "state": "success", "logical_date": "2026-07-08"},
        {"dag_id": "dag_b", "state": "failed", "logical_date": "2026-07-08"},
    ]
    return s


def test_remediate_dag_id_not_in_failed_list_exits(fake_s3, stub_summary, monkeypatch):
    monkeypatch.setattr(
        _mod,
        "_s3_get_json",
        lambda b, k, c: _multi_summary() if "run-summaries" in k else fake_s3.get(k),
    )
    monkeypatch.setattr(_mod, "_invoke_lambda", lambda *a, **k: {})
    args = SimpleNamespace(date=None, dag_id="dag_a", force=False)  # dag_a succeeded
    with pytest.raises(SystemExit):
        _mod.cmd_remediate(args, CFG)


def test_remediate_missing_logical_date_exits(fake_s3, stub_summary, monkeypatch):
    """A schema-required logical_date absent from the summary must halt, not fall
    back to a coarser key."""
    s = _summary()
    s["dags"] = [{"dag_id": "dag_ingest", "state": "failed"}]  # no logical_date
    monkeypatch.setattr(
        _mod, "_s3_get_json", lambda b, k, c: s if "run-summaries" in k else fake_s3.get(k)
    )
    monkeypatch.setattr(_mod, "_invoke_lambda", lambda *a, **k: {})
    args = SimpleNamespace(date=None, dag_id=None, force=False)
    with pytest.raises(SystemExit):
        _mod.cmd_remediate(args, CFG)
