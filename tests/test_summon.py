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


# ---------------------------------------------------------------------------
# A — #50: _cancel_item_watchdogs disables matching schedules
# ---------------------------------------------------------------------------


def _seed_watchdog_schedules(tmp_path: object, item_id: str, count: int = 3) -> list[int]:
    """Insert fake watchdog schedule rows for item_id; return their IDs."""
    import sqlite3 as _sqlite3

    db = _mod._db_path()
    conn = _sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT '',
                skill TEXT NOT NULL,
                args TEXT NOT NULL DEFAULT '[]',
                schedule TEXT NOT NULL DEFAULT '',
                next_run TEXT NOT NULL DEFAULT '',
                last_run TEXT,
                last_status TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                approved INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT 'summon',
                description TEXT NOT NULL DEFAULT ''
            )
        """)
        ids = []
        for minutes in [5, 15, 30][:count]:
            import json as _json

            cur = conn.execute(
                "INSERT INTO schedules (skill, args, schedule, next_run, enabled, approved, description) "
                "VALUES (?, ?, ?, ?, 1, 1, ?)",
                (
                    "summon",
                    _json.dumps(["watchdog-check", item_id, str(minutes)]),
                    "once@2026-06-13T12:00",
                    "2026-06-13T12:00:00+00:00",
                    f"dev-crew stall watchdog for {item_id} (+{minutes}m)",
                ),
            )
            ids.append(cur.lastrowid)
        conn.commit()
        return ids
    finally:
        conn.close()


def _schedule_enabled(tmp_path: object, row_id: int) -> bool:
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(str(_mod._db_path()))
    try:
        row = conn.execute("SELECT enabled FROM schedules WHERE id = ?", (row_id,)).fetchone()
        return bool(row[0]) if row else False
    finally:
        conn.close()


def test_cancel_item_watchdogs_disables_matching_rows(tmp_db):
    item = "2026-0050-cancel"
    ids = _seed_watchdog_schedules(tmp_db, item)
    assert all(_schedule_enabled(tmp_db, i) for i in ids)

    _mod._cancel_item_watchdogs(item)

    assert all(not _schedule_enabled(tmp_db, i) for i in ids)


def test_cancel_item_watchdogs_does_not_affect_other_items(tmp_db):
    item_a = "2026-0050-cancel-a"
    item_b = "2026-0050-cancel-b"
    ids_a = _seed_watchdog_schedules(tmp_db, item_a, count=1)
    ids_b = _seed_watchdog_schedules(tmp_db, item_b, count=1)

    _mod._cancel_item_watchdogs(item_a)

    assert not _schedule_enabled(tmp_db, ids_a[0])
    assert _schedule_enabled(tmp_db, ids_b[0])


def test_cancel_item_watchdogs_noop_when_no_schedules_table(tmp_db):
    """No crash when schedules table does not yet exist."""
    _mod._cancel_item_watchdogs("2026-0050-no-table")
    # Success = no exception raised


# ---------------------------------------------------------------------------
# B — #51: --revise with explicit findings bypasses _fetch_pr_info
# ---------------------------------------------------------------------------


def test_revise_kickoff_with_explicit_findings_contains_findings():
    """build_revise_kickoff with explicit text embeds the findings."""
    kickoff = _mod.build_revise_kickoff(
        "Herr Mannkusser",
        "2026-0051-revise",
        "",
        "AC1 failed: missing guard in skill.py:42",
    )
    assert "AC1 failed" in kickoff
    assert "(Tom findings unavailable" not in kickoff


def test_revise_kickoff_with_empty_findings_uses_placeholder():
    """build_revise_kickoff with no findings uses the unavailable placeholder."""
    kickoff = _mod.build_revise_kickoff(
        "Herr Mannkusser",
        "2026-0051-revise",
        "",
        "",
    )
    assert "unavailable" in kickoff


def test_fetch_pr_info_falls_back_to_review_shaped_comment(monkeypatch):
    """_fetch_pr_info falls back to a ## -headed comment when no Tom QA exists."""
    import json as _json
    import subprocess as _subprocess

    review_body = "## Findings\n\nFix the null guard.\n## Notes\n\nSee line 42."

    fake_prs = _json.dumps([{"number": 7, "url": "https://github.com/x/y/pull/7"}])
    fake_comments = _json.dumps(
        {"comments": [{"body": "Some unrelated comment."}, {"body": review_body}]}
    )

    def _fake_run(cmd, **kwargs):
        if "pr" in cmd and "list" in cmd:
            return _subprocess.CompletedProcess(cmd, 0, stdout=fake_prs)
        if "pr" in cmd and "view" in cmd:
            return _subprocess.CompletedProcess(cmd, 0, stdout=fake_comments)
        return _subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)

    pr_url, body = _mod._fetch_pr_info("/fake/repo", "2026-0051-test")
    assert pr_url == "https://github.com/x/y/pull/7"
    assert "Findings" in body
    assert "Fix the null guard" in body


# ---------------------------------------------------------------------------
# cmd_summon — preflight (spec absent on origin/dev-queue)
# ---------------------------------------------------------------------------


def test_summon_preflight_fails_when_spec_not_on_dev_queue(tmp_db, monkeypatch, capsys):
    """cmd_summon returns 1 and creates no worktree, spawns no session, arms no watchdog
    when git cat-file -e origin/dev-queue:<spec> fails (spec not published)."""
    import subprocess as _subprocess

    monkeypatch.setattr(_mod, "check_claude_version", lambda: None)
    monkeypatch.setattr(_mod, "concurrency_check", lambda: None)
    monkeypatch.setattr(_mod, "list_crew_sessions", lambda: [])
    monkeypatch.setattr(
        _mod,
        "get_persona",
        lambda _: {
            "name": "Herr Mannkusser",
            "role": "builder",
            "repo_dir": "/fake/repo",
            "permission_mode": "auto",
        },
    )

    spawned: list[list] = []

    def _fake_run(cmd, **kwargs):
        spawned.append(list(cmd))
        rc = 1 if "cat-file" in cmd else 0
        return _subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)

    rc = _mod.cmd_summon(["mannkusser", "2026-0999-fake"])

    assert rc == 1
    # Only git fetch and git cat-file should have been called; nothing beyond the preflight.
    substantive = [c for c in spawned if "cat-file" not in c and "fetch" not in c]
    assert substantive == [], f"Unexpected subprocess calls after preflight failure: {substantive}"


# ---------------------------------------------------------------------------
# _classify_verdict — ERROR branch
# ---------------------------------------------------------------------------


def test_classify_verdict_error():
    """Tom posts '## Tom QA — ERROR' when it cannot run; sweep must surface it."""
    body = (
        "## Tom QA — ERROR\n\n"
        "Tom could not produce findings. This check is **red** by design (fail-closed)."
    )
    assert _mod._classify_verdict(body) == "ERROR"


def test_classify_verdict_non_tom_comment_returns_empty():
    assert _mod._classify_verdict("some unrelated PR comment") == ""


def test_classify_verdict_unknown_format_returns_empty():
    """A Tom comment with no recognized verdict keyword returns ''; the sweep converts it
    to ERROR via belt-and-suspenders so it is never silently dropped."""
    body = "## Tom QA\n\nFuture format with no recognized verdict token."
    assert _mod._classify_verdict(body) == ""


# ---------------------------------------------------------------------------
# _build_verdict_message — ERROR branch
# ---------------------------------------------------------------------------


def test_build_verdict_message_error_contains_url_and_item():
    msg = _mod._build_verdict_message(
        "ERROR",
        "2026-0013-foo",
        "Herr Mannkusser",
        "https://github.com/x/y/pull/43",
        "## Tom QA — ERROR\n\nTom could not produce findings.",
    )
    assert "ERROR" in msg
    assert "2026-0013-foo" in msg
    assert "https://github.com/x/y/pull/43" in msg


# ---------------------------------------------------------------------------
# cmd_verdict_sweep — ERROR verdict and belt-and-suspenders
# ---------------------------------------------------------------------------


def test_verdict_sweep_surfaces_error_verdict(tmp_db, monkeypatch):
    """A Tom ERROR verdict is posted via _post_discord (high-signal path), not stdout."""
    from datetime import UTC, datetime

    item = "2026-0013-err"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    posts: list[tuple[str, bool]] = []

    def _fake_post(message: str, mention: bool = False) -> bool:
        posts.append((message, mention))
        return True

    monkeypatch.setattr(_mod, "_post_discord", _fake_post)
    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: (
            "OPEN",
            "https://github.com/x/y/pull/43",
            "ERROR",
            "comment-node-001",
            "## Tom QA — ERROR\n\nTom could not produce findings.",
            False,
        ),
    )

    rc = _mod.cmd_verdict_sweep([])
    assert rc == 0
    assert len(posts) == 1
    msg, mention = posts[0]
    assert "ERROR" in msg
    assert item in msg
    assert mention is True


def test_verdict_sweep_surfaces_unknown_verdict_as_error(tmp_db, monkeypatch):
    """Belt-and-suspenders: a Tom comment that classifies to '' is never silently dropped;
    it surfaces as ERROR so AJ always sees unexpected Tom output."""
    from datetime import UTC, datetime

    item = "2026-0013-unk"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    posts: list[tuple[str, bool]] = []

    def _fake_post(message: str, mention: bool = False) -> bool:
        posts.append((message, mention))
        return True

    monkeypatch.setattr(_mod, "_post_discord", _fake_post)
    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: (
            "OPEN",
            "https://github.com/x/y/pull/99",
            "",  # unrecognized verdict — sweep must not drop this
            "comment-node-002",
            "## Tom QA\n\nFuture format with no recognized verdict token.",
            False,
        ),
    )

    rc = _mod.cmd_verdict_sweep([])
    assert rc == 0
    assert len(posts) == 1
    msg, mention = posts[0]
    assert item in msg
    assert mention is True
