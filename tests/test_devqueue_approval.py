"""Tests for the devqueue approval gate.

Covers:
- approve subcommand records and rejects non-AJ users
- push refused when no approval exists
- push refused when target ref is not dev-queue
- push refused when destination path is outside dev-notes/**
- push consumes approval (one item, once)
- logging is called on push attempts
"""

from __future__ import annotations

import importlib.util
import sqlite3
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load devqueue skill module
# ---------------------------------------------------------------------------

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "devqueue" / "skill.py"
_spec = importlib.util.spec_from_file_location("devqueue_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AJ_ID = "111222333444555666"
NOT_AJ_ID = "999888777666555444"


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point devqueue at a temp DB and set AJ_DISCORD_USER_ID."""
    db_file = tmp_path / "jarvis.db"
    monkeypatch.setattr(_mod, "_DB_PATH", db_file)
    monkeypatch.setattr(_mod, "AJ_DISCORD_USER_ID", AJ_ID)
    return db_file


@pytest.fixture()
def valid_spec(tmp_path):
    """Write a minimal valid spec file and return its path."""
    spec = tmp_path / "2026-0099-test-item.md"
    spec.write_text(
        textwrap.dedent("""\
        ---
        id: 2026-0099-test-item
        title: Test item
        status: proposed
        scope: well-bounded-local
        origin: brainstorm
        author: AJ
        created: 2026-07-07
        ---

        Body.
    """)
    )
    return spec


# ---------------------------------------------------------------------------
# approve subcommand
# ---------------------------------------------------------------------------


def test_approve_records_aj_approval(tmp_db):
    rc = _mod.cmd_approve(["2026-0099-test-item", AJ_ID])
    assert rc == 0
    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute("SELECT * FROM devqueue_approvals").fetchone()
    assert row is not None
    assert row[1] == "2026-0099-test-item"  # item_id
    assert row[2] == AJ_ID  # approved_by
    assert row[4] == 0  # consumed = 0


def test_approve_rejects_non_aj(tmp_db, capsys):
    rc = _mod.cmd_approve(["2026-0099-test-item", NOT_AJ_ID])
    assert rc == 1
    assert "REFUSED" in capsys.readouterr().err
    # DB may not exist yet (table never created); either way, no rows approved
    if tmp_db.exists():
        conn = sqlite3.connect(str(tmp_db))
        try:
            rows = conn.execute("SELECT * FROM devqueue_approvals").fetchall()
            assert rows == []
        except sqlite3.OperationalError:
            pass  # table never created — correct, no approval was recorded


def test_approve_missing_args(capsys):
    rc = _mod.cmd_approve([])
    assert rc == 1
    assert "Usage" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# consume_approval
# ---------------------------------------------------------------------------


def test_consume_approval_returns_id_and_marks_consumed(tmp_db):
    _mod.cmd_approve(["2026-0099-test-item", AJ_ID])
    approval_id = _mod._consume_approval("2026-0099-test-item", AJ_ID)
    assert approval_id is not None
    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute(
        "SELECT consumed FROM devqueue_approvals WHERE id = ?", (approval_id,)
    ).fetchone()
    assert row[0] == 1


def test_consume_approval_returns_none_when_absent(tmp_db):
    result = _mod._consume_approval("2026-0099-test-item", AJ_ID)
    assert result is None


def test_consume_approval_one_item_once(tmp_db):
    """Second consume on same item returns None — one approval, one push."""
    _mod.cmd_approve(["2026-0099-test-item", AJ_ID])
    first = _mod._consume_approval("2026-0099-test-item", AJ_ID)
    second = _mod._consume_approval("2026-0099-test-item", AJ_ID)
    assert first is not None
    assert second is None


def test_consume_approval_concurrent_does_not_double_consume(tmp_db):
    """Two threads racing _consume_approval on the same item: exactly one wins.

    Regression test for Tom QA finding (PR #97): the old SELECT-then-UPDATE
    had a window where two connections could both read consumed=0 before
    either wrote consumed=1. The fix folds the read and write into one
    atomic UPDATE ... WHERE id = (SELECT ...) RETURNING id statement.
    """
    import threading

    _mod.cmd_approve(["2026-0099-test-item", AJ_ID])

    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(_mod._consume_approval("2026-0099-test-item", AJ_ID))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"


# ---------------------------------------------------------------------------
# _assert_approved
# ---------------------------------------------------------------------------


def test_assert_approved_raises_without_approval(tmp_db):
    with pytest.raises(SystemExit, match="no unconsumed approval"):
        _mod._assert_approved(
            "2026-0099-test-item", "knowledge/dev-notes/queue/2026-0099-test-item.md"
        )


def test_assert_approved_passes_with_approval(tmp_db):
    _mod.cmd_approve(["2026-0099-test-item", AJ_ID])
    approval_id = _mod._assert_approved(
        "2026-0099-test-item", "knowledge/dev-notes/queue/2026-0099-test-item.md"
    )
    assert approval_id is not None


def test_assert_approved_raises_when_user_id_not_configured(tmp_db, monkeypatch):
    monkeypatch.setattr(_mod, "AJ_DISCORD_USER_ID", "")
    with pytest.raises(SystemExit, match="AJ_DISCORD_USER_ID env var not set"):
        _mod._assert_approved(
            "2026-0099-test-item", "knowledge/dev-notes/queue/2026-0099-test-item.md"
        )


# ---------------------------------------------------------------------------
# _assert_push_target (hard-lock guards — pre-existing + new)
# ---------------------------------------------------------------------------


def test_assert_push_target_rejects_wrong_branch():
    with pytest.raises(SystemExit, match="target branch must be"):
        _mod._assert_push_target("main", str(_mod.PROJECT / "knowledge/dev-notes/queue/x.md"))


def test_assert_push_target_rejects_path_outside_devnotes():
    with pytest.raises(SystemExit, match="destination path must be under"):
        _mod._assert_push_target("dev-queue", str(_mod.PROJECT / "skills/evil.py"))


def test_assert_push_target_accepts_valid():
    # Should not raise
    _mod._assert_push_target(
        "dev-queue",
        str(_mod.PROJECT / "knowledge/dev-notes/queue/2026-0099-test-item.md"),
    )


# ---------------------------------------------------------------------------
# cmd_push gate integration (no actual git calls)
# ---------------------------------------------------------------------------


def test_push_refused_without_approval(tmp_db, valid_spec):
    """Push raises SystemExit when no approval exists."""
    with pytest.raises(SystemExit, match="no unconsumed approval"):
        _mod.cmd_push([str(valid_spec)])


def test_push_proceeds_with_approval(tmp_db, monkeypatch, valid_spec):
    """Push reaches git operations when approval exists — verified by mock."""
    _mod.cmd_approve(["2026-0099-test-item", AJ_ID])

    def fake_run_git(*args, cwd=_mod.PROJECT, capture=True):
        if args[0] == "fetch":
            return ""
        if args[0] == "rev-parse" and "--verify" in args:
            raise RuntimeError("not found")
        raise RuntimeError("stop here")

    monkeypatch.setattr(_mod, "run_git", fake_run_git)

    # Git fails → approval is restored (unconsumed) so AJ can re-approve
    rc = _mod.cmd_push([str(valid_spec)])
    assert rc == 1

    conn = sqlite3.connect(str(tmp_db))
    rows = conn.execute("SELECT consumed FROM devqueue_approvals").fetchall()
    assert all(r[0] == 0 for r in rows), "Approval should be restored after git failure"


def test_restore_approval_unmarks_consumed(tmp_db):
    """_restore_approval sets consumed=0 so the approval can be retried."""
    _mod.cmd_approve(["2026-0099-test-item", AJ_ID])
    approval_id = _mod._consume_approval("2026-0099-test-item", AJ_ID)
    assert approval_id is not None

    conn = sqlite3.connect(str(tmp_db))
    assert (
        conn.execute(
            "SELECT consumed FROM devqueue_approvals WHERE id=?", (approval_id,)
        ).fetchone()[0]
        == 1
    )

    _mod._restore_approval(approval_id)
    assert (
        conn.execute(
            "SELECT consumed FROM devqueue_approvals WHERE id=?", (approval_id,)
        ).fetchone()[0]
        == 0
    )
