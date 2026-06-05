"""
Follow-ups skill — Jarvis-owned, time-conditioned intentions.

Distinct from deadlines (AJ's obligations) — these are Jarvis's own intentions to
raise a subject at the right time. The anti-nag engine (cmd_fire) evaluates four
gates before any proactive outreach fires.

No virtualenv needed: stdlib only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config (all overridable via env)
# ---------------------------------------------------------------------------

_DB_PATH = Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"
_LOCAL_TZ = ZoneInfo("America/Toronto")

CHANNEL_QUIET_MIN = int(os.environ.get("FOLLOW_UP_CHANNEL_QUIET_MIN", "15"))
SPACING_MIN = int(os.environ.get("FOLLOW_UP_SPACING_MIN", "30"))
DAILY_BUDGET = int(os.environ.get("FOLLOW_UP_DAILY_BUDGET", "2"))
QUIET_START = int(os.environ.get("FOLLOW_UP_QUIET_START", "23"))  # local hour
QUIET_END = int(os.environ.get("FOLLOW_UP_QUIET_END", "9"))  # local hour
WINDOW_DEFAULT_DAYS = 2  # check_once window_until default

POLICIES = ("check_once", "periodic", "persistent", "passive")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def _init_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at       TEXT NOT NULL,
            subject          TEXT NOT NULL,
            prompt           TEXT NOT NULL,
            policy           TEXT NOT NULL DEFAULT 'check_once',
            trigger_at       TEXT NOT NULL,
            window_until     TEXT,
            cadence          INTEGER,
            priority         INTEGER NOT NULL DEFAULT 5,
            status           TEXT NOT NULL DEFAULT 'pending',
            surface_count    INTEGER NOT NULL DEFAULT 0,
            last_surfaced_at TEXT,
            resolved_at      TEXT,
            resolution       TEXT,
            knowledge_target TEXT,
            source           TEXT NOT NULL DEFAULT 'explicit',
            thread_id        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jarvis_kv (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM jarvis_kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO jarvis_kv (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _now_local() -> datetime:
    return datetime.now(_LOCAL_TZ)


def _parse_dt(s: str) -> datetime:
    """Parse ISO date or datetime; bare YYYY-MM-DD = local midnight. Returns UTC-aware."""
    s = s.strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        s += "T00:00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_LOCAL_TZ)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Gate checks (all must pass before cmd_fire speaks)
# ---------------------------------------------------------------------------


def _is_quiet_hours() -> bool:
    h = _now_local().hour
    if QUIET_START > QUIET_END:  # wraps midnight, e.g. 23–9
        return h >= QUIET_START or h < QUIET_END
    return QUIET_START <= h < QUIET_END


def _channel_is_quiet(conn: sqlite3.Connection) -> bool:
    """True when AJ has not sent a message in the last CHANNEL_QUIET_MIN minutes."""
    raw = _kv_get(conn, "follow_up_last_inbound")
    if not raw:
        return True  # no activity on record — treat as quiet
    last = datetime.fromisoformat(raw)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (_now_utc() - last).total_seconds() / 60 >= CHANNEL_QUIET_MIN


def _budget_available(conn: sqlite3.Connection) -> bool:
    today = _now_local().date().isoformat()
    if _kv_get(conn, "follow_up_daily_date") != today:
        return True
    raw = _kv_get(conn, "follow_up_daily_count")
    return (int(raw) if raw else 0) < DAILY_BUDGET


def _spacing_ok(conn: sqlite3.Connection) -> bool:
    raw = _kv_get(conn, "follow_up_last_fired_at")
    if not raw:
        return True
    last = datetime.fromisoformat(raw)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (_now_utc() - last).total_seconds() / 60 >= SPACING_MIN


def _increment_budget(conn: sqlite3.Connection) -> None:
    today = _now_local().date().isoformat()
    if _kv_get(conn, "follow_up_daily_date") != today:
        _kv_set(conn, "follow_up_daily_date", today)
        _kv_set(conn, "follow_up_daily_count", "1")
    else:
        raw = _kv_get(conn, "follow_up_daily_count")
        _kv_set(conn, "follow_up_daily_count", str((int(raw) if raw else 0) + 1))
    _kv_set(conn, "follow_up_last_fired_at", _now_utc().isoformat())


# ---------------------------------------------------------------------------
# Surface helper (handles re-arming for periodic/persistent)
# ---------------------------------------------------------------------------


def _mark_surfaced(conn: sqlite3.Connection, row_id: int, policy: str, cadence: int | None) -> None:
    now_iso = _now_utc().isoformat()
    if policy == "check_once":
        conn.execute(
            """UPDATE follow_ups
               SET status = 'surfaced', surface_count = surface_count + 1, last_surfaced_at = ?
               WHERE id = ?""",
            (now_iso, row_id),
        )
    else:
        # periodic / persistent: re-arm at trigger_at = now + cadence
        cadence_days = cadence or (7 if policy == "periodic" else 3)
        next_trigger = (_now_utc() + timedelta(days=cadence_days)).isoformat()
        conn.execute(
            """UPDATE follow_ups
               SET surface_count = surface_count + 1, last_surfaced_at = ?,
                   trigger_at = ?, status = 'pending'
               WHERE id = ?""",
            (now_iso, next_trigger, row_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Eligibility query (shared by cmd_due and cmd_fire)
# ---------------------------------------------------------------------------

_ELIGIBLE_SQL = """
    SELECT id, subject, prompt, policy, window_until, priority, surface_count, cadence
    FROM follow_ups
    WHERE status = 'pending'
      AND policy != 'passive'
      AND trigger_at <= ?
      AND (window_until IS NULL OR window_until > ?)
    ORDER BY
      CASE policy
        WHEN 'check_once'  THEN 1
        WHEN 'persistent'  THEN 2
        WHEN 'periodic'    THEN 3
        ELSE 99
      END,
      priority ASC,
      trigger_at ASC
"""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_add(args: list[str]) -> None:
    """
    add --subject S --prompt P --policy POLICY --trigger-at T
        [--window-until W] [--cadence DAYS] [--priority N]
        [--knowledge-target FILE] [--source explicit|open_loop]
    """
    params: dict[str, str] = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            key = args[i][2:].replace("-", "_")
            params[key] = args[i + 1]
            i += 2
        else:
            i += 1

    subject = params.get("subject", "").strip()
    prompt = params.get("prompt", "").strip()
    policy = params.get("policy", "check_once").strip()
    trigger_raw = params.get("trigger_at", "").strip()

    if not subject or not prompt or not trigger_raw:
        print("Error: --subject, --prompt, and --trigger-at are required", file=sys.stderr)
        sys.exit(1)

    if policy not in POLICIES:
        print(f"Error: --policy must be one of {POLICIES}", file=sys.stderr)
        sys.exit(1)

    try:
        trigger_at = _parse_dt(trigger_raw)
    except ValueError as exc:
        print(f"Error parsing --trigger-at: {exc}", file=sys.stderr)
        sys.exit(1)

    window_until: datetime | None = None
    if "window_until" in params:
        try:
            window_until = _parse_dt(params["window_until"])
        except ValueError as exc:
            print(f"Error parsing --window-until: {exc}", file=sys.stderr)
            sys.exit(1)
    elif policy == "check_once":
        window_until = trigger_at + timedelta(days=WINDOW_DEFAULT_DAYS)

    cadence: int | None = None
    if "cadence" in params:
        try:
            cadence = int(params["cadence"])
        except ValueError:
            print("Error: --cadence must be an integer (days)", file=sys.stderr)
            sys.exit(1)
    elif policy == "periodic":
        cadence = 7
    elif policy == "persistent":
        cadence = 3

    priority = int(params.get("priority", "5"))
    knowledge_target = params.get("knowledge_target") or None
    source = params.get("source", "explicit")

    conn = _init_db()
    try:
        now_iso = _now_utc().isoformat()
        cur = conn.execute(
            """INSERT INTO follow_ups
               (created_at, subject, prompt, policy, trigger_at, window_until,
                cadence, priority, knowledge_target, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now_iso,
                subject,
                prompt,
                policy,
                trigger_at.isoformat(),
                window_until.isoformat() if window_until else None,
                cadence,
                priority,
                knowledge_target,
                source,
            ),
        )
        conn.commit()
        row_id = cur.lastrowid

        if policy == "check_once" and window_until:
            expires = window_until.astimezone(_LOCAL_TZ).strftime("%b %d")
            confirm = f"check in once (expires {expires})"
        elif policy == "periodic":
            confirm = f"nudge periodically (every {cadence}d)"
        elif policy == "persistent":
            confirm = f"stay on it (every {cadence}d until done)"
        else:
            confirm = "track silently"

        print(
            json.dumps(
                {
                    "id": row_id,
                    "subject": subject,
                    "policy": policy,
                    "trigger_at": trigger_at.astimezone(_LOCAL_TZ).isoformat(),
                    "confirm": f"Got it — I'll {confirm}.",
                }
            )
        )
    finally:
        conn.close()


def cmd_list(args: list[str]) -> None:
    status_filter = args[0] if args else None
    conn = _init_db()
    try:
        if status_filter:
            rows = conn.execute(
                """SELECT id, subject, policy, status, trigger_at, window_until, priority,
                          last_surfaced_at, knowledge_target
                   FROM follow_ups WHERE status = ?
                   ORDER BY priority ASC, trigger_at ASC""",
                (status_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, subject, policy, status, trigger_at, window_until, priority,
                          last_surfaced_at, knowledge_target
                   FROM follow_ups
                   WHERE status NOT IN ('resolved', 'lapsed', 'dismissed')
                   ORDER BY priority ASC, trigger_at ASC"""
            ).fetchall()

        if not rows:
            print("No follow-ups.")
            return

        now_utc = _now_utc()
        lines = []
        for row_id, subject, policy, status, trigger_s, window_s, _priority, last_s, kt in rows:
            trigger = datetime.fromisoformat(trigger_s)
            if trigger.tzinfo is None:
                trigger = trigger.replace(tzinfo=UTC)
            trigger_local = trigger.astimezone(_LOCAL_TZ).strftime("%b %d %H:%M")
            overdue = trigger < now_utc and status == "pending"

            parts = [f"[{row_id}] **{subject}** ({policy}/{status})"]
            parts.append(f"fires: {trigger_local}{'⚠️' if overdue else ''}")
            if window_s:
                wu = datetime.fromisoformat(window_s).astimezone(_LOCAL_TZ).strftime("%b %d")
                parts.append(f"expires: {wu}")
            if last_s:
                ls_local = (
                    datetime.fromisoformat(last_s).astimezone(_LOCAL_TZ).strftime("%b %d %H:%M")
                )
                parts.append(f"last: {ls_local}")
            if kt:
                parts.append(f"→ {kt}")
            lines.append(" | ".join(parts))

        print("\n".join(lines))
    finally:
        conn.close()


def cmd_due(args: list[str]) -> None:
    """Return JSON list of eligible items. Used by heartbeat and conversation layer."""
    now_iso = _now_utc().isoformat()
    conn = _init_db()
    try:
        rows = conn.execute(_ELIGIBLE_SQL, (now_iso, now_iso)).fetchall()
        result = [
            {
                "id": r[0],
                "subject": r[1],
                "prompt": r[2],
                "policy": r[3],
                "window_until": r[4],
                "priority": r[5],
                "surface_count": r[6],
                "cadence": r[7],
            }
            for r in rows
        ]
        print(json.dumps(result))
    finally:
        conn.close()


def cmd_surface(args: list[str]) -> None:
    if not args:
        print("Usage: surface <id>", file=sys.stderr)
        sys.exit(1)
    row_id = int(args[0])
    conn = _init_db()
    try:
        row = conn.execute(
            "SELECT policy, cadence FROM follow_ups WHERE id = ? AND status = 'pending'",
            (row_id,),
        ).fetchone()
        if not row:
            print(f"No pending follow-up with id={row_id}", file=sys.stderr)
            sys.exit(1)
        _mark_surfaced(conn, row_id, row[0], row[1])
        print(json.dumps({"id": row_id, "status": "surfaced"}))
    finally:
        conn.close()


def cmd_resolve(args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: resolve <id> <resolution text>", file=sys.stderr)
        sys.exit(1)
    row_id = int(args[0])
    resolution = " ".join(args[1:])

    conn = _init_db()
    try:
        row = conn.execute(
            """SELECT subject, knowledge_target
               FROM follow_ups
               WHERE id = ? AND status NOT IN ('resolved', 'dismissed', 'lapsed')""",
            (row_id,),
        ).fetchone()
        if not row:
            print(f"No active follow-up with id={row_id}", file=sys.stderr)
            sys.exit(1)

        subject, knowledge_target = row
        now_iso = _now_utc().isoformat()
        conn.execute(
            "UPDATE follow_ups SET status = 'resolved', resolved_at = ?, resolution = ? WHERE id = ?",
            (now_iso, resolution, row_id),
        )
        conn.commit()

        result: dict = {
            "id": row_id,
            "subject": subject,
            "status": "resolved",
            "resolution": resolution,
        }

        if knowledge_target:
            result["capture_proposal"] = {
                "file": knowledge_target,
                "op": "append",
                "text": resolution,
                "hint": (
                    f'python3 skills/knowledge/skill.py stage "{knowledge_target}" '
                    f'append Notes "{resolution}" --source explicit'
                ),
            }

        print(json.dumps(result, indent=2))
    finally:
        conn.close()


def cmd_dismiss(args: list[str]) -> None:
    if not args:
        print("Usage: dismiss <id>", file=sys.stderr)
        sys.exit(1)
    row_id = int(args[0])
    conn = _init_db()
    try:
        cur = conn.execute(
            "UPDATE follow_ups SET status = 'dismissed' WHERE id = ? AND status NOT IN ('resolved', 'lapsed')",
            (row_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            print(f"No active follow-up with id={row_id}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"id": row_id, "status": "dismissed"}))
    finally:
        conn.close()


def cmd_snooze(args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: snooze <id> <until YYYY-MM-DD[THH:MM]>", file=sys.stderr)
        sys.exit(1)
    row_id = int(args[0])
    try:
        until = _parse_dt(args[1])
    except ValueError as exc:
        print(f"Error parsing until: {exc}", file=sys.stderr)
        sys.exit(1)

    conn = _init_db()
    try:
        cur = conn.execute(
            """UPDATE follow_ups SET trigger_at = ?, status = 'pending'
               WHERE id = ? AND status NOT IN ('resolved', 'lapsed', 'dismissed')""",
            (until.isoformat(), row_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            print(f"No active follow-up with id={row_id}", file=sys.stderr)
            sys.exit(1)
        until_local = until.astimezone(_LOCAL_TZ).strftime("%b %d %H:%M")
        print(json.dumps({"id": row_id, "snoozed_until": until_local}))
    finally:
        conn.close()


def cmd_reap(args: list[str]) -> None:
    """Lapse items whose window_until has passed without being surfaced."""
    now_iso = _now_utc().isoformat()
    conn = _init_db()
    try:
        cur = conn.execute(
            """UPDATE follow_ups
               SET status = 'lapsed'
               WHERE status IN ('pending', 'eligible')
                 AND window_until IS NOT NULL
                 AND window_until <= ?""",
            (now_iso,),
        )
        conn.commit()
        count = cur.rowcount
        if count:
            print(json.dumps({"reaped": count}), file=sys.stderr)
    finally:
        conn.close()


def cmd_ping(args: list[str]) -> None:
    """Update last_inbound timestamp. Call at the start of every response to AJ."""
    conn = _init_db()
    try:
        _kv_set(conn, "follow_up_last_inbound", _now_utc().isoformat())
    finally:
        conn.close()
    # No output — this is a silent bookkeeping call.


def cmd_fire(args: list[str]) -> None:
    """
    Evaluate all four gates and fire one proactive follow-up if green.
    Outputs the prompt text (for Discord) or nothing.

    Gate order:
      1. Quiet hours — don't fire during sleep / early morning
      2. Channel quiet — AJ not mid-conversation
      3. Daily budget — max 2 proactive outreaches per calendar day
      4. Spacing — at least SPACING_MIN minutes since last fire

    The critical invariant: eligibility alone never triggers speech.
    All four gates must pass. Overflow queues silently; time-sensitive
    items lapse via cmd_reap when window_until expires.
    """
    if _is_quiet_hours():
        return

    conn = _init_db()
    try:
        if not _channel_is_quiet(conn):
            return
        if not _budget_available(conn):
            return
        if not _spacing_ok(conn):
            return

        now_iso = _now_utc().isoformat()
        rows = conn.execute(_ELIGIBLE_SQL + " LIMIT 1", (now_iso, now_iso)).fetchall()
        if not rows:
            return

        row_id, subject, prompt_text, policy, _, _, _, cadence = rows[0]

        _mark_surfaced(conn, row_id, policy, cadence)
        _increment_budget(conn)

        print(prompt_text)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: skill.py <command> [args...]\n"
            "Commands:\n"
            "  add --subject S --prompt P --policy POLICY --trigger-at T\n"
            "      [--window-until W] [--cadence DAYS] [--priority N]\n"
            "      [--knowledge-target FILE] [--source explicit|open_loop]\n"
            "  list [status]\n"
            "  due\n"
            "  surface <id>\n"
            "  resolve <id> <resolution text>\n"
            "  dismiss <id>\n"
            "  snooze <id> <until YYYY-MM-DD[THH:MM]>\n"
            "  reap\n"
            "  ping\n"
            "  fire",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1].lower()
    args = sys.argv[2:]

    dispatch = {
        "add": cmd_add,
        "list": cmd_list,
        "due": cmd_due,
        "surface": cmd_surface,
        "resolve": cmd_resolve,
        "dismiss": cmd_dismiss,
        "snooze": cmd_snooze,
        "reap": cmd_reap,
        "ping": cmd_ping,
        "fire": cmd_fire,
    }

    if cmd not in dispatch:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        sys.exit(1)

    dispatch[cmd](args)


if __name__ == "__main__":
    main()
