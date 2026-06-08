"""Tests for the summon skill: kickoff construction, worktree paths, stall logic,
question marker, and the one-shot watchdog check."""

from __future__ import annotations

import importlib.util
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
# build_kickoff
# ---------------------------------------------------------------------------


def test_build_kickoff_is_single_line():
    # The kickoff is passed as a positional CLI arg; embedded newlines would break it.
    k = _mod.build_kickoff("Herr Mannkusser", "2026-0002-thing")
    assert "\n" not in k


def test_build_kickoff_mentions_item_and_scripts():
    k = _mod.build_kickoff("Herr Mannkusser", "2026-0002-thing")
    assert "Herr Mannkusser" in k
    assert "2026-0002-thing" in k
    assert "scripts/dev-loop/start-build.sh" in k
    assert "scripts/dev-loop/open-pr.sh" in k


def test_build_kickoff_directs_blocking_questions_to_ask():
    k = _mod.build_kickoff("Herr Mannkusser", "2026-0002-thing")
    assert "skills/summon/skill.py ask 2026-0002-thing" in k
    assert "do NOT guess" in k


# ---------------------------------------------------------------------------
# worktree_path
# ---------------------------------------------------------------------------


def test_worktree_path_is_sibling_not_nested():
    repo = "/home/u/code/project-jarvis"
    wt = _mod.worktree_path(repo, "2026-0003-x")
    assert wt == Path("/home/u/code/jarvis-build-2026-0003-x")
    # Must not be nested inside the repo (that would pollute the working tree).
    assert Path(repo) not in wt.parents


# ---------------------------------------------------------------------------
# is_stalled
# ---------------------------------------------------------------------------


def test_is_stalled_true_when_no_progress_and_no_question():
    assert _mod.is_stalled({"question_at": None}, progressed=False) is True


def test_is_stalled_false_when_progressed():
    assert _mod.is_stalled({"question_at": None}, progressed=True) is False


def test_is_stalled_false_when_question_posted():
    assert _mod.is_stalled({"question_at": "2026-06-07T12:00:00+00:00"}, progressed=False) is False


# ---------------------------------------------------------------------------
# DB-backed: summon record + question marker + watchdog-check
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Point the skill's DB at an isolated temp jarvis.db."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    return tmp_path


def _question_at(item_id: str) -> str | None:
    conn = _mod._db()
    try:
        row = conn.execute(
            "SELECT question_at FROM dev_crew_runs WHERE item = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def test_record_summon_then_question_marker(tmp_db):
    from datetime import UTC, datetime

    item = "2026-0004-y"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    assert _question_at(item) is None

    _mod._set_question(item, datetime.now(UTC))
    assert _question_at(item) is not None


def test_resummon_resets_question_marker(tmp_db):
    from datetime import UTC, datetime

    item = "2026-0005-z"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    _mod._set_question(item, datetime.now(UTC))
    assert _question_at(item) is not None

    # A fresh summon for the same item clears the blocked state.
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    assert _question_at(item) is None


def test_watchdog_check_alerts_on_stall(tmp_db, monkeypatch, capsys):
    from datetime import UTC, datetime

    item = "2026-0006-stall"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: False)

    rc = _mod.cmd_watchdog_check([item])
    out = capsys.readouterr().out
    assert rc == 0
    assert "stalled" in out
    assert item in out


def test_watchdog_check_silent_when_progressed_and_already_pinged(tmp_db, monkeypatch, capsys):
    from datetime import UTC, datetime

    item = "2026-0007-prog"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    # Mark PR already pinged so the milestone doesn't re-fire
    _mod._set_pr_pinged(item, datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: True)

    rc = _mod.cmd_watchdog_check([item])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_watchdog_check_silent_when_question_posted(tmp_db, monkeypatch, capsys):
    from datetime import UTC, datetime

    item = "2026-0008-asked"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    _mod._set_question(item, datetime.now(UTC))
    monkeypatch.setattr(_mod, "_branch_progressed", lambda *a, **k: False)

    rc = _mod.cmd_watchdog_check([item])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_watchdog_check_silent_when_no_record(tmp_db, capsys):
    rc = _mod.cmd_watchdog_check(["2026-0009-unknown"])
    assert rc == 0
    assert capsys.readouterr().out == ""
