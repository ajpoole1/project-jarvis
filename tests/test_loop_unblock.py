"""Tests for 2026-0005-loop-unblock:
#25 — Tom .github visibility (no code path to test; covered by qa.yml diff command change)
#23 — Tom JSON-parse robustness (inline retry helper mirrors qa.yml logic)
#26 — QA-feedback refix cycle: build_revise_kickoff + --revise flag behaviour
#24 — Responsive relay: escalating stall messages + PR milestone ping
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import skills/summon/skill.py without running main()
# ---------------------------------------------------------------------------

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "summon" / "skill.py"
_spec = importlib.util.spec_from_file_location("summon_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Point the skill's DB at an isolated temp jarvis.db."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Fix #23: Tom JSON-parse robustness
#
# The retry logic lives inline in qa.yml. Mirror it here so it can be
# unit-tested without running the full workflow.
# ---------------------------------------------------------------------------


def _tom_parse_with_retry(raw_responses: list[str], max_attempts: int = 3):
    """Mirror the JSON parse-retry logic from qa.yml Run Tom step.

    Returns (findings_dict, None) on success, or (None, last_raw) on total failure.
    raw_responses[i] is the model output returned on attempt i+1.
    """
    findings = None
    last_raw = ""
    for json_attempt in range(1, max_attempts + 1):
        last_raw = (
            raw_responses[json_attempt - 1] if json_attempt <= len(raw_responses) else last_raw
        )
        try:
            m = re.search(r"\{.*\}", last_raw, re.DOTALL)
            findings = json.loads(m.group(0) if m else last_raw)
            return findings, None
        except json.JSONDecodeError:
            if json_attempt >= max_attempts:
                return None, last_raw
    return findings, None


def test_tom_parse_clean_json():
    raw = '{"spec_conformance": [], "defects": [], "architecture_notes": []}'
    f, err = _tom_parse_with_retry([raw])
    assert f is not None
    assert err is None
    assert f["spec_conformance"] == []


def test_tom_parse_extracts_json_from_prose():
    raw = (
        "Sure, here is my review:\n"
        '{"spec_conformance": [], "defects": [], "architecture_notes": ["looks good"]}\n'
        "Hope that helps."
    )
    f, err = _tom_parse_with_retry([raw])
    assert f is not None
    assert err is None
    assert f["architecture_notes"] == ["looks good"]


def test_tom_parse_retries_on_bad_json_succeeds_second_attempt():
    bad = "This is not JSON at all"
    good = '{"spec_conformance": [], "defects": [], "architecture_notes": []}'
    f, err = _tom_parse_with_retry([bad, good])
    assert f is not None
    assert err is None
    assert f["defects"] == []


def test_tom_parse_persistent_failure_returns_none_and_raw():
    bad = "This is not JSON"
    f, err = _tom_parse_with_retry([bad, bad, bad])
    assert f is None
    assert err == bad


def test_tom_parse_truncated_json_retried():
    truncated = '{"spec_conformance": [], "defects": [{"type": "bug"'
    good = '{"spec_conformance": [], "defects": [], "architecture_notes": []}'
    f, err = _tom_parse_with_retry([truncated, good])
    assert f is not None


def test_tom_parse_markdown_fenced_json():
    raw = '```json\n{"spec_conformance": [], "defects": [], "architecture_notes": []}\n```'
    f, err = _tom_parse_with_retry([raw])
    assert f is not None
    assert f["spec_conformance"] == []


# ---------------------------------------------------------------------------
# Fix #26: build_revise_kickoff
# ---------------------------------------------------------------------------


def test_build_revise_kickoff_is_single_line():
    k = _mod.build_revise_kickoff("Herr Mannkusser", "2026-0005-x", "", "")
    assert "\n" not in k


def test_build_revise_kickoff_warns_not_to_run_start_build():
    # The kickoff should actively warn the builder NOT to run start-build.sh
    # (it can mention it, but must tell the builder to avoid it).
    k = _mod.build_revise_kickoff("Herr Mannkusser", "2026-0005-x", "", "")
    assert "start-build.sh" in k
    assert "do NOT" in k or "not run" in k.lower()


def test_build_revise_kickoff_mentions_item_and_persona():
    k = _mod.build_revise_kickoff("Herr Mannkusser", "2026-0005-x", "", "")
    assert "Herr Mannkusser" in k
    assert "2026-0005-x" in k


def test_build_revise_kickoff_includes_tom_findings():
    k = _mod.build_revise_kickoff(
        "Herr Mannkusser", "2026-0005-x", "https://github.com/r/p/123", "Bug: missing test"
    )
    assert "Bug: missing test" in k
    assert "https://github.com/r/p/123" in k


def test_build_revise_kickoff_mentions_push():
    k = _mod.build_revise_kickoff("Herr Mannkusser", "2026-0005-x", "", "")
    assert "push" in k.lower()


def test_build_revise_kickoff_fallback_when_no_findings():
    k = _mod.build_revise_kickoff("Herr Mannkusser", "2026-0005-x", "", "")
    assert "unavailable" in k


def test_build_revise_kickoff_truncates_long_findings():
    long_findings = "x" * 3000
    k = _mod.build_revise_kickoff("Herr Mannkusser", "2026-0005-x", "", long_findings)
    assert len(k) < 10000
    assert "…" in k


def test_build_revise_kickoff_directs_questions_to_ask():
    k = _mod.build_revise_kickoff("Herr Mannkusser", "2026-0005-x", "", "")
    assert "skills/summon/skill.py ask 2026-0005-x" in k


# ---------------------------------------------------------------------------
# Fix #24: escalating stall messages + PR milestone ping
# ---------------------------------------------------------------------------


def test_watchdog_check_stall_at_5m_fires_soft_alert(tmp_db, monkeypatch, capsys):
    from datetime import UTC, datetime

    item = "2026-0020-stall5"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: False)

    rc = _mod.cmd_watchdog_check([item, "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert item in out
    assert "5" in out
    # Soft alert should NOT yet say "Intervene"
    assert "Intervene" not in out


def test_watchdog_check_stall_at_15m_fires_medium_alert(tmp_db, monkeypatch, capsys):
    from datetime import UTC, datetime

    item = "2026-0021-stall15"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: False)

    rc = _mod.cmd_watchdog_check([item, "15"])
    out = capsys.readouterr().out
    assert rc == 0
    assert item in out
    assert "15" in out
    assert "attention" in out.lower()
    assert "Intervene" not in out


def test_watchdog_check_stall_at_30m_fires_urgent_alert(tmp_db, monkeypatch, capsys):
    from datetime import UTC, datetime

    item = "2026-0022-stall30"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: False)

    rc = _mod.cmd_watchdog_check([item, "30"])
    out = capsys.readouterr().out
    assert rc == 0
    assert item in out
    assert "Intervene" in out


def test_watchdog_check_default_minutes_is_30(tmp_db, monkeypatch, capsys):
    """Backward compat: old once@ schedules that don't pass minutes default to 30."""
    from datetime import UTC, datetime

    item = "2026-0023-default"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: False)

    rc = _mod.cmd_watchdog_check([item])  # no minutes arg
    out = capsys.readouterr().out
    assert rc == 0
    assert "Intervene" in out


def test_watchdog_check_pr_milestone_ping_fires_when_pr_opened(tmp_db, monkeypatch, capsys):
    from datetime import UTC, datetime

    item = "2026-0024-pr"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: True)

    rc = _mod.cmd_watchdog_check([item, "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PR" in out
    assert item in out


def test_watchdog_check_pr_milestone_fires_only_once(tmp_db, monkeypatch, capsys):
    from datetime import UTC, datetime

    item = "2026-0025-pr2"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: True)

    # First check: should ping
    _mod.cmd_watchdog_check([item, "5"])
    capsys.readouterr()

    # Second check: PR already pinged — should be silent
    rc = _mod.cmd_watchdog_check([item, "15"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == ""


def test_watchdog_check_silent_when_progressed_and_already_pinged(tmp_db, monkeypatch, capsys):
    from datetime import UTC, datetime

    item = "2026-0026-silent"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    _mod._set_pr_pinged(item, datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: True)

    rc = _mod.cmd_watchdog_check([item, "30"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_watchdog_checks_constant_has_three_escalating_levels():
    checks = _mod.WATCHDOG_CHECKS
    assert len(checks) == 3
    assert checks == sorted(checks)
    assert checks[0] < checks[1] < checks[2]
