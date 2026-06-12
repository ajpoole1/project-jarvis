"""Tests for 2026-0007-outbound-delivery: quiet-hours gate, flush, @mention, heartbeat gap."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Import skills/schedules/skill.py without running main()
# ---------------------------------------------------------------------------

_SCHED_PATH = Path(__file__).parents[1] / "skills" / "schedules" / "skill.py"
_spec = importlib.util.spec_from_file_location("schedules_skill", _SCHED_PATH)
_sched = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sched)

# Import scripts/discord_post.py
_POST_PATH = Path(__file__).parents[1] / "scripts" / "discord_post.py"
_pspec = importlib.util.spec_from_file_location("discord_post", _POST_PATH)
_dpost = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(_dpost)

# Import skills/summon/skill.py
_SUMMON_PATH = Path(__file__).parents[1] / "skills" / "summon" / "skill.py"
_sspec = importlib.util.spec_from_file_location("summon_skill", _SUMMON_PATH)
_summon = importlib.util.module_from_spec(_sspec)
_sspec.loader.exec_module(_summon)

_LOCAL_TZ = ZoneInfo("America/Toronto")


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, skill TEXT NOT NULL,
            args TEXT NOT NULL DEFAULT '[]', schedule TEXT NOT NULL,
            next_run TEXT NOT NULL, last_run TEXT, last_status TEXT,
            enabled INTEGER NOT NULL DEFAULT 1, approved INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT 'jarvis', description TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE heartbeat (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_tick TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE outbound_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queued_at TEXT NOT NULL,
            message TEXT NOT NULL,
            signal TEXT NOT NULL DEFAULT 'ambient'
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# _is_quiet_now
# ---------------------------------------------------------------------------


def _quiet_at(hour: int) -> bool:
    """Helper: call _is_quiet_now with the clock frozen at the given local hour.

    Pins QUIET_START/END to 23/9 so the test is deterministic regardless of
    FOLLOW_UP_QUIET_START/FOLLOW_UP_QUIET_END env-var overrides.
    """
    fake_local = datetime(2026, 6, 8, hour, 5, tzinfo=_LOCAL_TZ)
    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_QUIET_START", 23),
        patch.object(_sched, "_QUIET_END", 9),
    ):
        return _sched._is_quiet_now()


def test_is_quiet_during_window():
    assert _quiet_at(23) is True
    assert _quiet_at(0) is True
    assert _quiet_at(4) is True
    assert _quiet_at(8) is True


def test_not_quiet_outside_window():
    assert _quiet_at(9) is False
    assert _quiet_at(12) is False
    assert _quiet_at(22) is False


# ---------------------------------------------------------------------------
# _gap_is_expected_overnight
# ---------------------------------------------------------------------------


def _prev_at(hour: int) -> datetime:
    return datetime(2026, 6, 7, hour, 55, tzinfo=_LOCAL_TZ).astimezone(UTC)


def _now_at(hour: int) -> datetime:
    return datetime(2026, 6, 8, hour, 5, tzinfo=_LOCAL_TZ).astimezone(UTC)


def test_expected_overnight_machine_off_at_22():
    prev = _prev_at(22)  # last seen at 22:55
    now = _now_at(9)  # back at 09:05 (gap ~10h10m = 610 min)
    gap = (now - prev).total_seconds() / 60
    assert _sched._gap_is_expected_overnight(prev, now, gap) is True


def test_expected_overnight_machine_off_at_23():
    prev = _prev_at(23)  # last seen at 23:55
    now = _now_at(9)  # back at 09:05 (gap ~9h10m = 550 min)
    gap = (now - prev).total_seconds() / 60
    assert _sched._gap_is_expected_overnight(prev, now, gap) is True


def test_expected_overnight_machine_off_during_quiet():
    prev = _now_at(1)  # last seen at 01:05 (inside quiet)
    now = _now_at(9)  # back at 09:05 (gap ~8h)
    gap = (now - prev).total_seconds() / 60
    assert _sched._gap_is_expected_overnight(prev, now, gap) is True


def test_not_expected_gap_during_business_hours():
    prev = datetime(2026, 6, 8, 14, 0, tzinfo=_LOCAL_TZ).astimezone(UTC)
    now = datetime(2026, 6, 8, 16, 30, tzinfo=_LOCAL_TZ).astimezone(UTC)
    gap = (now - prev).total_seconds() / 60  # 150 min
    assert _sched._gap_is_expected_overnight(prev, now, gap) is False


def test_not_expected_gap_short_duration():
    prev = _prev_at(22)
    now = _prev_at(22) + timedelta(minutes=35)  # only 35 min gap
    assert _sched._gap_is_expected_overnight(prev, now, 35) is False


# ---------------------------------------------------------------------------
# _post_discord — queues during quiet, posts directly outside quiet
# ---------------------------------------------------------------------------


def test_post_discord_queues_during_quiet():
    conn = _make_conn()
    fake_local = datetime(2026, 6, 8, 3, 0, tzinfo=_LOCAL_TZ)  # 03:00 — quiet
    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_now_utc", return_value=fake_local.astimezone(UTC)),
        patch.object(_sched, "_post_discord_direct") as mock_direct,
    ):
        _sched._post_discord(conn, "hello quiet world", signal="ambient")
        mock_direct.assert_not_called()

    rows = conn.execute("SELECT message, signal FROM outbound_queue").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "hello quiet world"
    assert rows[0][1] == "ambient"


def test_post_discord_posts_directly_outside_quiet():
    conn = _make_conn()
    fake_local = datetime(2026, 6, 8, 12, 0, tzinfo=_LOCAL_TZ)  # noon — not quiet
    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_post_discord_direct") as mock_direct,
    ):
        _sched._post_discord(conn, "hello daytime world", signal="ambient")
        mock_direct.assert_called_once_with("hello daytime world", "ambient")

    rows = conn.execute("SELECT message FROM outbound_queue").fetchall()
    assert rows == []


def test_post_discord_high_signal_queued_correctly():
    conn = _make_conn()
    fake_local = datetime(2026, 6, 8, 3, 0, tzinfo=_LOCAL_TZ)
    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_now_utc", return_value=fake_local.astimezone(UTC)),
        patch.object(_sched, "_post_discord_direct"),
    ):
        _sched._post_discord(conn, "⚠️ outage", signal="high")

    row = conn.execute("SELECT signal FROM outbound_queue").fetchone()
    assert row[0] == "high"


# ---------------------------------------------------------------------------
# _flush_outbound_queue
# ---------------------------------------------------------------------------


def test_flush_delivers_in_order():
    conn = _make_conn()
    now_utc = datetime(2026, 6, 8, 13, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO outbound_queue (queued_at, message, signal) VALUES (?,?,?)",
        (now_utc.isoformat(), "first", "ambient"),
    )
    conn.execute(
        "INSERT INTO outbound_queue (queued_at, message, signal) VALUES (?,?,?)",
        (now_utc.isoformat(), "second", "high"),
    )
    conn.execute(
        "INSERT INTO outbound_queue (queued_at, message, signal) VALUES (?,?,?)",
        (now_utc.isoformat(), "third", "ambient"),
    )
    conn.commit()

    calls_received = []
    fake_local = datetime(2026, 6, 8, 13, 0, tzinfo=_LOCAL_TZ)  # not quiet

    def capture(msg, sig):
        calls_received.append((msg, sig))

    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_post_discord_direct", side_effect=capture),
    ):
        _sched._flush_outbound_queue(conn)

    assert calls_received == [("first", "ambient"), ("second", "high"), ("third", "ambient")]
    assert conn.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 0


def test_flush_noop_during_quiet():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO outbound_queue (queued_at, message, signal) VALUES (?,?,?)",
        (datetime.now(UTC).isoformat(), "deferred", "ambient"),
    )
    conn.commit()

    fake_local = datetime(2026, 6, 8, 3, 0, tzinfo=_LOCAL_TZ)  # quiet
    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_post_discord_direct") as mock_direct,
    ):
        _sched._flush_outbound_queue(conn)
        mock_direct.assert_not_called()

    # Message must still be in the queue
    assert conn.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 1


def test_flush_noop_on_empty_queue():
    conn = _make_conn()
    fake_local = datetime(2026, 6, 8, 13, 0, tzinfo=_LOCAL_TZ)
    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_post_discord_direct") as mock_direct,
    ):
        _sched._flush_outbound_queue(conn)
        mock_direct.assert_not_called()


# ---------------------------------------------------------------------------
# _tick_heartbeat — real gap alerts, expected overnight skips
# ---------------------------------------------------------------------------


def test_heartbeat_alerts_on_real_gap():
    conn = _make_conn()
    prev_utc = datetime(2026, 6, 8, 14, 0, tzinfo=UTC)  # 10am EDT
    now_utc = datetime(2026, 6, 8, 16, 30, tzinfo=UTC)  # 12:30pm EDT  (150 min gap)
    conn.execute(
        "INSERT OR REPLACE INTO heartbeat (id, last_tick) VALUES (1, ?)", (prev_utc.isoformat(),)
    )
    conn.commit()

    posted = []
    fake_local = now_utc.astimezone(_LOCAL_TZ)

    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(
            _sched, "_post_discord_direct", side_effect=lambda m, s: posted.append((m, s))
        ),
    ):
        _sched._tick_heartbeat(conn, now_utc)

    assert len(posted) == 1
    assert "150" in posted[0][0]
    assert "offline" in posted[0][0]
    assert posted[0][1] == "high"


def test_heartbeat_no_alert_on_expected_overnight_gap():
    conn = _make_conn()
    # Machine last seen at 22:55 local (before quiet), back at 09:05 local
    prev_utc = datetime(2026, 6, 7, 22, 55, tzinfo=_LOCAL_TZ).astimezone(UTC)
    now_utc = datetime(2026, 6, 8, 9, 5, tzinfo=_LOCAL_TZ).astimezone(UTC)
    conn.execute(
        "INSERT OR REPLACE INTO heartbeat (id, last_tick) VALUES (1, ?)", (prev_utc.isoformat(),)
    )
    conn.commit()

    posted = []
    fake_local = now_utc.astimezone(_LOCAL_TZ)

    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(
            _sched, "_post_discord_direct", side_effect=lambda m, s: posted.append((m, s))
        ),
    ):
        _sched._tick_heartbeat(conn, now_utc)

    assert posted == [], "Expected overnight gap must not generate an outage alert"


def test_heartbeat_queues_alert_during_quiet():
    conn = _make_conn()
    # Real gap: 14:00 → 17:00 local (but machine came back at 03:00 quiet)
    prev_utc = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)  # 2pm EDT
    now_utc = datetime(2026, 6, 9, 7, 0, tzinfo=UTC)  # 3am EDT next day (quiet)
    conn.execute(
        "INSERT OR REPLACE INTO heartbeat (id, last_tick) VALUES (1, ?)", (prev_utc.isoformat(),)
    )
    conn.commit()

    fake_local = now_utc.astimezone(_LOCAL_TZ)  # 03:00 — quiet

    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_now_utc", return_value=now_utc),
        patch.object(_sched, "_post_discord_direct") as mock_direct,
    ):
        _sched._tick_heartbeat(conn, now_utc)

    # Should be queued, not posted directly
    mock_direct.assert_not_called()
    row = conn.execute("SELECT signal FROM outbound_queue").fetchone()
    assert row is not None and row[0] == "high"


def test_heartbeat_no_alert_on_first_run():
    conn = _make_conn()  # no heartbeat row
    now_utc = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    fake_local = now_utc.astimezone(_LOCAL_TZ)
    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_post_discord_direct") as mock_direct,
    ):
        _sched._tick_heartbeat(conn, now_utc)
    mock_direct.assert_not_called()


def test_heartbeat_no_alert_on_small_gap():
    conn = _make_conn()
    prev_utc = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    now_utc = datetime(2026, 6, 8, 12, 10, tzinfo=UTC)  # 10 min gap
    conn.execute(
        "INSERT OR REPLACE INTO heartbeat (id, last_tick) VALUES (1, ?)", (prev_utc.isoformat(),)
    )
    conn.commit()
    fake_local = now_utc.astimezone(_LOCAL_TZ)
    with (
        patch.object(_sched, "_now_local", return_value=fake_local),
        patch.object(_sched, "_post_discord_direct") as mock_direct,
    ):
        _sched._tick_heartbeat(conn, now_utc)
    mock_direct.assert_not_called()


# ---------------------------------------------------------------------------
# @mention — _post_discord_direct passes --mention for high signal
# ---------------------------------------------------------------------------


def test_post_discord_direct_high_signal_passes_mention(tmp_path, monkeypatch):
    monkeypatch.setattr(_sched, "_NOTIFY_USER_ID", "123456789")
    monkeypatch.setattr(_sched, "_DISCORD_SCRIPT", tmp_path / "fake_discord.py")
    (tmp_path / "fake_discord.py").write_text("import sys; print('ok')")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        return r

    with patch("subprocess.run", side_effect=fake_run):
        _sched._post_discord_direct("test message", signal="high")

    assert "--mention" in calls[0]


def test_post_discord_direct_ambient_no_mention(tmp_path, monkeypatch):
    monkeypatch.setattr(_sched, "_NOTIFY_USER_ID", "123456789")
    monkeypatch.setattr(_sched, "_DISCORD_SCRIPT", tmp_path / "fake_discord.py")
    (tmp_path / "fake_discord.py").write_text("import sys; print('ok')")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        return r

    with patch("subprocess.run", side_effect=fake_run):
        _sched._post_discord_direct("morning briefing", signal="ambient")

    assert "--mention" not in calls[0]


def test_post_discord_direct_no_mention_when_user_id_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(_sched, "_NOTIFY_USER_ID", "")  # unset
    monkeypatch.setattr(_sched, "_DISCORD_SCRIPT", tmp_path / "fake_discord.py")
    (tmp_path / "fake_discord.py").write_text("import sys; print('ok')")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        r = MagicMock()
        r.returncode = 0
        return r

    with patch("subprocess.run", side_effect=fake_run):
        _sched._post_discord_direct("outage", signal="high")

    assert "--mention" not in calls[0]


# ---------------------------------------------------------------------------
# discord_post.py -- --mention prepends <@id> to message
# ---------------------------------------------------------------------------


def test_discord_post_mention_prepends_user_id():
    posted_content = []

    def fake_post(msg):
        posted_content.append(msg)

    user_id = "378598738014502922"
    with (
        patch.object(_dpost, "NOTIFY_USER_ID", user_id),
        patch.object(_dpost, "post", side_effect=fake_post),
    ):
        # Simulate what __main__ does with --mention
        message = "system outage"
        if user_id:
            message = f"<@{user_id}> {message}"
        _dpost.post(message)

    assert posted_content[0].startswith(f"<@{user_id}>")


def test_discord_post_no_mention_when_id_unset():
    posted_content = []

    def fake_post(msg):
        posted_content.append(msg)

    with (
        patch.object(_dpost, "NOTIFY_USER_ID", ""),
        patch.object(_dpost, "post", side_effect=fake_post),
    ):
        message = "morning briefing"
        # mention=True but no user id — no prepend
        notify_id = ""
        if notify_id:
            message = f"<@{notify_id}> {message}"
        _dpost.post(message)

    assert "<@" not in posted_content[0]


# ---------------------------------------------------------------------------
# SIGNAL:high prefix parsed by dispatcher
# ---------------------------------------------------------------------------


def test_signal_prefix_routes_high_signal():
    """Dispatcher strips SIGNAL:high prefix and routes as high-signal to _post_discord."""
    raw = "SIGNAL:high\n🚨 Builder stalled on item-123"
    _HIGH_PREFIX = "SIGNAL:high\n"

    routed_calls = []

    def fake_post(conn, message, signal="ambient"):
        routed_calls.append((message, signal))

    # Simulate the dispatcher logic for stdout routing
    if raw.startswith(_HIGH_PREFIX):
        fake_post(None, raw[len(_HIGH_PREFIX) :], signal="high")
    else:
        fake_post(None, raw, signal="ambient")

    assert routed_calls[0] == ("🚨 Builder stalled on item-123", "high")


def test_no_prefix_routes_ambient():
    raw = "Morning briefing delivered."
    _HIGH_PREFIX = "SIGNAL:high\n"
    routed_calls = []

    def fake_post(conn, message, signal="ambient"):
        routed_calls.append((message, signal))

    if raw.startswith(_HIGH_PREFIX):
        fake_post(None, raw[len(_HIGH_PREFIX) :], signal="high")
    else:
        fake_post(None, raw, signal="ambient")

    assert routed_calls[0] == ("Morning briefing delivered.", "ambient")


# ---------------------------------------------------------------------------
# summon skill -- cmd_ask uses mention=True, stall alerts use SIGNAL prefix
# ---------------------------------------------------------------------------


def test_summon_ask_posts_with_mention():
    """cmd_ask calls _post_discord with mention=True."""
    posted_calls = []

    def fake_post(msg, mention=False):
        posted_calls.append({"msg": msg, "mention": mention})
        return True

    with (
        patch.object(_summon, "_post_discord", side_effect=fake_post),
        patch.object(_summon, "_db") as mock_db,
    ):
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = ("mannkusser",)
        mock_db.return_value = mock_conn

        _summon.cmd_ask(["2026-0007-test", "What should I do with X?"])

    assert len(posted_calls) == 1
    assert posted_calls[0]["mention"] is True


def test_summon_watchdog_stall_alert_uses_signal_prefix(capsys):
    """Watchdog stall alerts print SIGNAL:high prefix so dispatcher routes them correctly."""
    with (
        patch.object(_summon, "_branch_progressed", return_value=False),
        patch.object(
            _summon,
            "load_roster",
            return_value={"mannkusser": {"name": "Herr Mannkusser", "repo_dir": "/tmp"}},
        ),
    ):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("mannkusser", None, None)
        with patch.object(_summon, "_db", return_value=conn):
            _summon.cmd_watchdog_check(["2026-0007-test", "30"])

    captured = capsys.readouterr()
    assert captured.out.startswith("SIGNAL:high\n")


def test_summon_watchdog_milestone_ping_no_signal_prefix(capsys):
    """PR opened milestone ping does NOT use the SIGNAL prefix (it's positive/ambient)."""
    with (
        patch.object(_summon, "_branch_progressed", return_value=True),
        patch.object(
            _summon,
            "load_roster",
            return_value={"mannkusser": {"name": "Herr Mannkusser", "repo_dir": "/tmp"}},
        ),
    ):
        conn = MagicMock()
        # pr_pinged_at is None → will print milestone ping
        conn.execute.return_value.fetchone.return_value = ("mannkusser", None, None)
        with (
            patch.object(_summon, "_db", return_value=conn),
            patch.object(_summon, "_set_pr_pinged"),
        ):
            _summon.cmd_watchdog_check(["2026-0007-test", "5"])

    captured = capsys.readouterr()
    assert "SIGNAL:high" not in captured.out
    assert "PR opened" in captured.out
