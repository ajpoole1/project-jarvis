"""Tests for the devqueue close/archive command (skills/devqueue/skill.py).

Covers:
- close_frontmatter: status flip + closed_reason/closed_at injection
- cmd_close: refused without approval, refused with empty reason, refused
  with too few args
- cmd_close consumes approval and moves queue/ -> archive/ on success (mocked git)
- 'archive' is accepted as an alias for 'close' in main()'s dispatch
- STATUS_VALUES includes 'closed'
"""

from __future__ import annotations

import importlib.util
import sqlite3
import textwrap
from pathlib import Path

import pytest

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "devqueue" / "skill.py"
_spec = importlib.util.spec_from_file_location("devqueue_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

AJ_ID = "111222333444555666"

_SAMPLE_SPEC = textwrap.dedent("""\
    ---
    id: 2026-0035-finance-p2-intelligence-layer
    title: finance P2 intelligence layer
    status: authorized
    scope: well-bounded-local
    origin: brainstorm
    author: jarvis
    created: 2026-06-21
    authorized_by: aj
    authorized_at: 2026-06-21
    ---

    ## Intent

    Body text unaffected by the close mutation.
""")


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "jarvis.db"
    monkeypatch.setattr(_mod, "_DB_PATH", db_file)
    monkeypatch.setattr(_mod, "AJ_DISCORD_USER_ID", AJ_ID)
    return db_file


# ---------------------------------------------------------------------------
# close_frontmatter
# ---------------------------------------------------------------------------


def test_close_frontmatter_flips_status():
    result = _mod.close_frontmatter(_SAMPLE_SPEC, "superseded by finance-p3", "2026-07-07")
    fields, _ = _mod.parse_frontmatter(result)
    assert fields["status"] == "closed"


def test_close_frontmatter_adds_reason_and_date():
    result = _mod.close_frontmatter(_SAMPLE_SPEC, "superseded by finance-p3", "2026-07-07")
    fields, _ = _mod.parse_frontmatter(result)
    assert fields["closed_reason"] == "superseded by finance-p3"
    assert fields["closed_at"] == "2026-07-07"


def test_close_frontmatter_preserves_other_fields():
    result = _mod.close_frontmatter(_SAMPLE_SPEC, "reason", "2026-07-07")
    fields, _ = _mod.parse_frontmatter(result)
    assert fields["id"] == "2026-0035-finance-p2-intelligence-layer"
    assert fields["title"] == "finance P2 intelligence layer"
    assert fields["authorized_by"] == "aj"


def test_close_frontmatter_preserves_body():
    result = _mod.close_frontmatter(_SAMPLE_SPEC, "reason", "2026-07-07")
    assert "Body text unaffected by the close mutation." in result


def test_close_frontmatter_round_trips_embedded_quotes():
    """parse_frontmatter is a hand-rolled parser that strips exactly one
    leading/trailing quote pair with no escape processing — embedded quotes
    must round-trip unescaped, not \\"-escaped (the parser doesn't unescape)."""
    result = _mod.close_frontmatter(_SAMPLE_SPEC, 'reason with "quotes" inside', "2026-07-07")
    fields, _ = _mod.parse_frontmatter(result)
    assert fields["closed_reason"] == 'reason with "quotes" inside'


def test_close_frontmatter_collapses_newlines_in_reason():
    """A newline in reason would inject a bogus extra frontmatter line and
    corrupt the block — must collapse to single-line instead."""
    result = _mod.close_frontmatter(_SAMPLE_SPEC, "line one\nline two\nline three", "2026-07-07")
    fields, _ = _mod.parse_frontmatter(result)
    assert fields["closed_reason"] == "line one line two line three"
    assert fields["status"] == "closed"  # frontmatter block still parses cleanly


def test_close_frontmatter_raises_without_frontmatter_block():
    with pytest.raises(ValueError, match="no frontmatter block"):
        _mod.close_frontmatter("no frontmatter here", "reason", "2026-07-07")


# ---------------------------------------------------------------------------
# STATUS_VALUES
# ---------------------------------------------------------------------------


def test_status_values_includes_closed():
    assert "closed" in _mod.STATUS_VALUES


# ---------------------------------------------------------------------------
# cmd_close — argument handling
# ---------------------------------------------------------------------------


def test_close_missing_args(capsys):
    rc = _mod.cmd_close([])
    assert rc == 1
    assert "Usage" in capsys.readouterr().err


def test_close_missing_reason(capsys):
    rc = _mod.cmd_close(["2026-0035-finance-p2-intelligence-layer"])
    assert rc == 1
    assert "Usage" in capsys.readouterr().err


def test_close_empty_reason(capsys):
    rc = _mod.cmd_close(["2026-0035-finance-p2-intelligence-layer", "   "])
    assert rc == 1
    assert "reason cannot be empty" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_close — approval gate (mirrors push's gate exactly)
# ---------------------------------------------------------------------------


def test_close_refused_without_approval(tmp_db):
    with pytest.raises(SystemExit, match="no unconsumed approval"):
        _mod.cmd_close(["2026-0035-finance-p2-intelligence-layer", "superseded"])


def test_close_proceeds_with_approval_reaches_git(tmp_db, monkeypatch):
    """Approval present -> guards pass -> reaches _acquire_dev_queue_worktree,
    which fails fast here since we don't mock git. Approval should be restored."""
    _mod.cmd_approve(["2026-0035-finance-p2-intelligence-layer", AJ_ID])

    def fake_acquire():
        raise RuntimeError("simulated: dev-queue branch unreachable in test")

    monkeypatch.setattr(_mod, "_acquire_dev_queue_worktree", fake_acquire)

    rc = _mod.cmd_close(["2026-0035-finance-p2-intelligence-layer", "superseded"])
    assert rc == 1

    conn = sqlite3.connect(str(tmp_db))
    rows = conn.execute("SELECT consumed FROM devqueue_approvals").fetchall()
    assert all(r[0] == 0 for r in rows), "Approval should be restored after acquire failure"


def test_close_succeeds_end_to_end_with_mocked_worktree(tmp_db, monkeypatch, tmp_path):
    """Full happy path with a real (temp-dir) worktree standing in for the
    dev-queue checkout, and run_git mocked to operate on plain files instead
    of a real git repo — proves the queue->archive move + frontmatter mutation
    actually happen correctly, without needing a real git remote."""
    _mod.cmd_approve(["2026-0035-finance-p2-intelligence-layer", AJ_ID])

    wt_path = tmp_path / "wt"
    queue_dir = wt_path / "knowledge" / "dev-notes" / "queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "2026-0035-finance-p2-intelligence-layer.md").write_text(_SAMPLE_SPEC)

    monkeypatch.setattr(_mod, "_acquire_dev_queue_worktree", lambda: wt_path)
    monkeypatch.setattr(_mod, "_release_worktree", lambda p: None)

    git_calls = []

    def fake_run_git(*args, cwd=_mod.PROJECT, capture=True):
        git_calls.append(args)
        if args[0] == "rm":
            target = wt_path / args[2]
            target.unlink(missing_ok=True)
        return ""

    monkeypatch.setattr(_mod, "run_git", fake_run_git)

    rc = _mod.cmd_close(["2026-0035-finance-p2-intelligence-layer", "superseded by finance-p3"])
    assert rc == 0

    archive_file = (
        wt_path
        / "knowledge"
        / "dev-notes"
        / "archive"
        / "2026-0035-finance-p2-intelligence-layer.md"
    )
    assert archive_file.exists()
    fields, _ = _mod.parse_frontmatter(archive_file.read_text())
    assert fields["status"] == "closed"
    assert fields["closed_reason"] == "superseded by finance-p3"

    assert not (queue_dir / "2026-0035-finance-p2-intelligence-layer.md").exists()

    # Approval should now be consumed (not restored) — success path.
    conn = sqlite3.connect(str(tmp_db))
    rows = conn.execute("SELECT consumed FROM devqueue_approvals").fetchall()
    assert all(r[0] == 1 for r in rows)

    # Commit message should name the item and reason.
    commit_calls = [c for c in git_calls if c[0] == "commit"]
    assert len(commit_calls) == 1
    assert "2026-0035-finance-p2-intelligence-layer" in commit_calls[0][2]


def test_close_missing_item_in_queue_restores_approval(tmp_db, monkeypatch, tmp_path):
    _mod.cmd_approve(["2026-0099-nonexistent", AJ_ID])

    wt_path = tmp_path / "wt"
    (wt_path / "knowledge" / "dev-notes" / "queue").mkdir(parents=True)
    # No spec file written — item genuinely absent from queue/.

    monkeypatch.setattr(_mod, "_acquire_dev_queue_worktree", lambda: wt_path)
    monkeypatch.setattr(_mod, "_release_worktree", lambda p: None)
    monkeypatch.setattr(_mod, "run_git", lambda *a, cwd=_mod.PROJECT, capture=True: "")

    rc = _mod.cmd_close(["2026-0099-nonexistent", "cleanup"])
    assert rc == 1

    conn = sqlite3.connect(str(tmp_db))
    rows = conn.execute("SELECT consumed FROM devqueue_approvals").fetchall()
    assert all(r[0] == 0 for r in rows)


# ---------------------------------------------------------------------------
# main() dispatch — 'archive' alias
# ---------------------------------------------------------------------------


def test_main_dispatches_archive_alias_to_cmd_close(monkeypatch):
    calls = []
    monkeypatch.setattr(_mod, "cmd_close", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(_mod.sys, "argv", ["skill.py", "archive", "2026-0035-x", "reason"])
    rc = _mod.main()
    assert rc == 0
    assert calls == [["2026-0035-x", "reason"]]


def test_main_dispatches_close_to_cmd_close(monkeypatch):
    calls = []
    monkeypatch.setattr(_mod, "cmd_close", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(_mod.sys, "argv", ["skill.py", "close", "2026-0035-x", "reason"])
    rc = _mod.main()
    assert rc == 0
    assert calls == [["2026-0035-x", "reason"]]
