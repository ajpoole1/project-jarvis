"""Tests for the verdict sensor: dedupe, signal classification, and self-retirement.

Acceptance criteria covered:
  AC1/AC2 — dedupe: same verdict reported twice → one Discord post
  AC2     — refix cycle: FAIL→PASS each reported exactly once
  AC2b    — dedup marker set only after confirmed delivery; failed delivery → retry
  AC3     — PASS/FAIL/SKIP all post to Discord with mention=True (high-signal)
  AC3     — PASS/SKIP message names merge gate; FAIL message names fix-authorize gate
  AC4     — self-retire: row removed when PR is merged or closed
"""

from __future__ import annotations

import importlib.util
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "summon" / "skill.py"
_spec = importlib.util.spec_from_file_location("summon_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Isolate each test to its own jarvis.db."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def capture_posts(monkeypatch):
    """Monkeypatch _post_discord to capture calls and return True (delivery success)."""
    posts: list[tuple[str, bool]] = []

    def _fake_post(message: str, mention: bool = False) -> bool:
        posts.append((message, mention))
        return True

    monkeypatch.setattr(_mod, "_post_discord", _fake_post)
    return posts


@pytest.fixture
def failing_post(monkeypatch):
    """Monkeypatch _post_discord to simulate delivery failure (returns False)."""
    calls: list[tuple[str, bool]] = []

    def _fake_post(message: str, mention: bool = False) -> bool:
        calls.append((message, mention))
        return False

    monkeypatch.setattr(_mod, "_post_discord", _fake_post)
    return calls


# ---------------------------------------------------------------------------
# _classify_verdict
# ---------------------------------------------------------------------------


def test_classify_verdict_pass():
    body = "## Tom QA — ✅ PASS\n\n### Architecture notes\n- looks good\n"
    assert _mod._classify_verdict(body) == "PASS"


def test_classify_verdict_fail():
    body = "## Tom QA — 🚫 FAIL\n\n### 🚫 Blocking (must fix before merge)\n- **[bug]** foo: bar\n"
    assert _mod._classify_verdict(body) == "FAIL"


def test_classify_verdict_skip():
    body = "## Tom QA — ⏭️ SKIP\n\nTrivial diff.\n"
    assert _mod._classify_verdict(body) == "SKIP"


def test_classify_verdict_not_tom_comment():
    assert _mod._classify_verdict("Some unrelated PR comment") == ""


def test_classify_verdict_error_comment():
    body = "## Tom QA — ERROR\n\nCould not run."
    assert _mod._classify_verdict(body) == "ERROR"


# ---------------------------------------------------------------------------
# _build_verdict_message — gate naming
# ---------------------------------------------------------------------------

_PR_URL = "https://github.com/owner/repo/pull/42"
_ITEM = "2026-0009-test"
_PERSONA = "Herr Mannkusser"


def test_build_verdict_message_pass_names_merge_gate():
    msg = _mod._build_verdict_message("PASS", _ITEM, _PERSONA, _PR_URL, "")
    assert "merge" in msg.lower()
    assert _ITEM in msg
    assert _PERSONA in msg
    assert _PR_URL in msg


def test_build_verdict_message_skip_names_merge_gate():
    msg = _mod._build_verdict_message("SKIP", _ITEM, _PERSONA, _PR_URL, "")
    assert "merge" in msg.lower()
    assert _ITEM in msg


def test_build_verdict_message_fail_names_fix_authorize_gate():
    body = (
        "## Tom QA — 🚫 FAIL\n\n"
        "### 🚫 Blocking (must fix before merge)\n"
        "- **[bug]** skill.py:42: off-by-one in dedupe\n\n"
        "---\n"
    )
    msg = _mod._build_verdict_message("FAIL", _ITEM, _PERSONA, _PR_URL, body)
    # Must communicate that a fix decision is waiting on AJ
    assert "fix" in msg.lower() or "authorize" in msg.lower()
    assert _ITEM in msg
    assert _PERSONA in msg


def test_build_verdict_message_fail_includes_blocking_summary():
    body = (
        "## Tom QA — 🚫 FAIL\n\n"
        "### 🚫 Blocking (must fix before merge)\n"
        "- **[bug]** skill.py:10: missing rollback\n"
        "- **[conformance]** AC3 not implemented\n\n"
        "---\n"
    )
    msg = _mod._build_verdict_message("FAIL", _ITEM, _PERSONA, _PR_URL, body)
    assert "missing rollback" in msg or "AC3" in msg


# ---------------------------------------------------------------------------
# _comment_identity
# ---------------------------------------------------------------------------


def test_comment_identity_uses_node_id():
    c = {"id": "IC_kwDOABC123", "body": "some body"}
    assert _mod._comment_identity(c) == "IC_kwDOABC123"


def test_comment_identity_falls_back_to_hash():
    c = {"id": "", "body": "unique body content"}
    identity = _mod._comment_identity(c)
    assert identity.startswith("hash:")
    # Same body → same hash
    assert identity == _mod._comment_identity({"id": "", "body": "unique body content"})


def test_comment_identity_different_bodies_differ():
    a = _mod._comment_identity({"id": "", "body": "body A"})
    b = _mod._comment_identity({"id": "", "body": "body B"})
    assert a != b


# ---------------------------------------------------------------------------
# _parse_iso — timezone normalization
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_suffix():
    dt = _mod._parse_iso("2026-06-08T20:15:00Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_handles_plus00_suffix():
    dt = _mod._parse_iso("2026-06-08T20:15:00+00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_z_and_plus00_equal():
    a = _mod._parse_iso("2026-06-08T20:15:00Z")
    b = _mod._parse_iso("2026-06-08T20:15:00+00:00")
    assert a == b


def test_parse_iso_empty_returns_none():
    assert _mod._parse_iso("") is None


# ---------------------------------------------------------------------------
# cmd_verdict_sweep — dedupe (AC2: same verdict twice → one post)
# ---------------------------------------------------------------------------


def test_verdict_sweep_deduplication(tmp_db, monkeypatch, capture_posts):
    """Same verdict received on two consecutive ticks → posted exactly once."""
    item = "2026-0009-dedup"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    def mock_fetch(repo_dir, item_id, summoned_at):
        return "OPEN", _PR_URL, "PASS", "IC_unique42", "## Tom QA — ✅ PASS\n...", False

    monkeypatch.setattr(_mod, "_fetch_active_verdict", mock_fetch)

    # First sweep: new verdict — should post
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 1

    # Second sweep: same comment ID — should be silent (deduped)
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 1  # still 1 — no new post


# ---------------------------------------------------------------------------
# cmd_verdict_sweep — dedup marker only set on confirmed delivery (AC2b)
# ---------------------------------------------------------------------------


def test_verdict_sweep_dedup_not_set_on_delivery_failure(tmp_db, monkeypatch, failing_post):
    """If Discord delivery fails, dedup marker is NOT set — next sweep retries."""
    item = "2026-0009-dedup-fail"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("OPEN", _PR_URL, "PASS", "IC_fail42", "## Tom QA — ✅ PASS\n", False),
    )

    # First sweep: delivery fails → marker NOT updated
    _mod.cmd_verdict_sweep([])
    assert len(failing_post) == 1  # attempted delivery

    conn = _mod._db()
    try:
        row = conn.execute(
            "SELECT last_verdict_reported FROM dev_crew_runs WHERE item = ?", (item,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] is None  # marker not set because delivery failed


def test_verdict_sweep_dedup_retried_after_failure(tmp_db, monkeypatch):
    """After a delivery failure the next sweep re-attempts delivery."""
    item = "2026-0009-retry"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("OPEN", _PR_URL, "PASS", "IC_retry1", "## Tom QA — ✅ PASS\n", False),
    )

    attempt_count = [0]

    def counting_post(message: str, mention: bool = False) -> bool:
        attempt_count[0] += 1
        return attempt_count[0] >= 2  # fail first, succeed second

    monkeypatch.setattr(_mod, "_post_discord", counting_post)

    # First sweep: fails → marker not set
    _mod.cmd_verdict_sweep([])
    assert attempt_count[0] == 1

    # Second sweep: succeeds → marker set
    _mod.cmd_verdict_sweep([])
    assert attempt_count[0] == 2

    conn = _mod._db()
    try:
        row = conn.execute(
            "SELECT last_verdict_reported FROM dev_crew_runs WHERE item = ?", (item,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "IC_retry1"


# ---------------------------------------------------------------------------
# cmd_verdict_sweep — refix cycle (AC2: FAIL then PASS each posted once)
# ---------------------------------------------------------------------------


def test_verdict_sweep_refix_cycle(tmp_db, monkeypatch, capture_posts):
    """FAIL verdict posted, then re-summon, then PASS verdict posted — both appear, no dupes."""
    item = "2026-0009-refix"

    # Initial summon
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    # --- FAIL verdict ---
    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("OPEN", _PR_URL, "FAIL", "IC_fail_001", "## Tom QA — 🚫 FAIL\n", False),
    )
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 1
    assert "FAIL" in capture_posts[0][0]

    # Second tick with same FAIL verdict → silent (deduped)
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 1

    # AJ re-summons for refix (resets last_verdict_reported=NULL, advances summoned_at)
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    # --- PASS verdict (new comment, after re-summon) ---
    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("OPEN", _PR_URL, "PASS", "IC_pass_002", "## Tom QA — ✅ PASS\n", False),
    )
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 2
    assert "PASS" in capture_posts[1][0]

    # Third tick → silent again
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 2


# ---------------------------------------------------------------------------
# cmd_verdict_sweep — all verdicts post with mention=True (AC3, bypasses quiet hours)
# ---------------------------------------------------------------------------


def test_verdict_sweep_mention_true_for_pass(tmp_db, monkeypatch, capture_posts):
    item = "2026-0009-sig-pass"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("OPEN", _PR_URL, "PASS", "IC_sp1", "## Tom QA — ✅ PASS", False),
    )
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 1
    _, mention = capture_posts[0]
    assert mention is True


def test_verdict_sweep_mention_true_for_fail(tmp_db, monkeypatch, capture_posts):
    item = "2026-0009-sig-fail"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("OPEN", _PR_URL, "FAIL", "IC_sf1", "## Tom QA — 🚫 FAIL\n", False),
    )
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 1
    _, mention = capture_posts[0]
    assert mention is True


def test_verdict_sweep_mention_true_for_skip(tmp_db, monkeypatch, capture_posts):
    item = "2026-0009-sig-skip"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))
    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("OPEN", _PR_URL, "SKIP", "IC_sk1", "## Tom QA — ⏭️ SKIP", False),
    )
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 1
    _, mention = capture_posts[0]
    assert mention is True


# ---------------------------------------------------------------------------
# cmd_verdict_sweep — self-retire on terminal PR state (AC4)
# ---------------------------------------------------------------------------


def test_verdict_sweep_retires_on_merged_pr(tmp_db, monkeypatch, capture_posts):
    """Row is removed from dev_crew_runs when the PR is merged."""
    item = "2026-0009-retire-merge"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("MERGED", _PR_URL, "PASS", "IC_m1", "## Tom QA — ✅ PASS", False),
    )
    _mod.cmd_verdict_sweep([])

    conn = _mod._db()
    try:
        row = conn.execute("SELECT item FROM dev_crew_runs WHERE item = ?", (item,)).fetchone()
    finally:
        conn.close()
    assert row is None


def test_verdict_sweep_retires_on_closed_pr(tmp_db, monkeypatch, capture_posts):
    """Row is removed from dev_crew_runs when the PR is closed (without merge)."""
    item = "2026-0009-retire-close"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("CLOSED", _PR_URL, "", "", "", False),
    )
    _mod.cmd_verdict_sweep([])

    conn = _mod._db()
    try:
        row = conn.execute("SELECT item FROM dev_crew_runs WHERE item = ?", (item,)).fetchone()
    finally:
        conn.close()
    assert row is None


def test_verdict_sweep_silent_with_no_active_builds(tmp_db, monkeypatch, capture_posts):
    """No rows in dev_crew_runs → no Discord post."""
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 0


def test_verdict_sweep_silent_when_no_pr_yet(tmp_db, monkeypatch, capture_posts):
    """No PR yet for the build → no Discord post."""
    item = "2026-0009-no-pr"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("", "", "", "", "", False),
    )
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 0


def test_verdict_sweep_silent_when_no_verdict_yet(tmp_db, monkeypatch, capture_posts):
    """PR exists but no Tom verdict yet → no Discord post."""
    item = "2026-0009-no-verdict"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("OPEN", _PR_URL, "", "", "", False),
    )
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 0


# ---------------------------------------------------------------------------
# cmd_verdict_sweep — gh failure surfacing (AC2: #34 follow-up)
# ---------------------------------------------------------------------------


def test_verdict_sweep_gh_fail_surfaces_post(tmp_db, monkeypatch, tmp_path, capture_posts):
    """A gh failure posts a notice to Discord."""
    item = "2026-0010-gh-fail"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("", "", "", "", "", True),
    )
    sentinel = tmp_path / "gh-fail-sentinel"
    monkeypatch.setattr(_mod, "_GH_FAIL_SENTINEL", sentinel)

    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 1
    msg, mention = capture_posts[0]
    assert "gh" in msg.lower()
    assert mention is True


def test_verdict_sweep_gh_fail_throttled(tmp_db, monkeypatch, tmp_path, capture_posts):
    """A gh failure within the cooldown window produces no Discord post (throttled)."""
    item = "2026-0010-gh-throttle"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("", "", "", "", "", True),
    )
    sentinel = tmp_path / "gh-fail-sentinel"
    sentinel.write_text(str(time.time()))  # written just now — within cooldown
    monkeypatch.setattr(_mod, "_GH_FAIL_SENTINEL", sentinel)

    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 0


def test_verdict_sweep_gh_fail_multiple_items_one_alert(
    tmp_db, monkeypatch, tmp_path, capture_posts
):
    """Multiple items with gh failures emit a single alert, not one per item."""
    for i in range(3):
        _mod._record_summon(f"2026-0010-multi-{i}", "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("", "", "", "", "", True),
    )
    sentinel = tmp_path / "gh-fail-sentinel"
    monkeypatch.setattr(_mod, "_GH_FAIL_SENTINEL", sentinel)

    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 1
    msg, _ = capture_posts[0]
    assert "verdict sensor" in msg


def test_verdict_sweep_no_pr_yet_silent_on_gh_ok(tmp_db, monkeypatch, capture_posts):
    """'No PR yet' (pr_state='', gh_failed=False) stays silent — distinct from gh failure."""
    item = "2026-0010-no-pr-ok"
    _mod._record_summon(item, "mannkusser", "/tmp/wt", datetime.now(UTC))

    monkeypatch.setattr(
        _mod,
        "_fetch_active_verdict",
        lambda *a: ("", "", "", "", "", False),
    )
    _mod.cmd_verdict_sweep([])
    assert len(capture_posts) == 0


# ---------------------------------------------------------------------------
# _should_emit_gh_fail_alert / _record_gh_fail_alert — throttle helpers
# ---------------------------------------------------------------------------


def test_should_emit_gh_fail_alert_true_when_no_sentinel(tmp_path):
    """Returns True when no sentinel file exists (first failure ever)."""
    sentinel = tmp_path / "nonexistent"
    assert _mod._should_emit_gh_fail_alert(sentinel) is True


def test_should_emit_gh_fail_alert_false_within_cooldown(tmp_path):
    """Returns False when sentinel was written recently (within cooldown)."""
    sentinel = tmp_path / "recent"
    sentinel.write_text(str(time.time()))
    assert _mod._should_emit_gh_fail_alert(sentinel) is False


def test_should_emit_gh_fail_alert_true_after_cooldown(tmp_path):
    """Returns True when sentinel is older than the cooldown window."""
    sentinel = tmp_path / "old"
    old_ts = time.time() - _mod.GH_FAIL_ALERT_COOLDOWN_SECONDS - 1
    sentinel.write_text(str(old_ts))
    assert _mod._should_emit_gh_fail_alert(sentinel) is True


def test_record_gh_fail_alert_writes_sentinel(tmp_path):
    """Stamps the sentinel file so subsequent checks see a recent timestamp."""
    sentinel = tmp_path / "stamp"
    before = time.time()
    _mod._record_gh_fail_alert(sentinel)
    assert sentinel.exists()
    recorded = float(sentinel.read_text().strip())
    assert recorded >= before
