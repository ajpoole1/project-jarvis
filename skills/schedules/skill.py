"""
Schedules skill — agent-safe cron for Jarvis.

Jarvis proposes jobs (approved=false); AJ approves via Discord.
The dispatcher runs only allowlisted skills with argv-passed args — no shell
string assembly, no metacharacters, no arbitrary command field.

Supported schedule formats:
  30m, 2h, 7d              — repeat every N minutes / hours / days
  daily@HH:MM              — fire once per day at a local time
  weekly@DOW@HH:MM         — fire once per week (mon/tue/wed/thu/fri/sat/sun)
  once@YYYY-MM-DDTHH:MM   — fire once at a fixed local time, then retire

No virtualenv needed: stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Load .jarvis.env so JARVIS_DATA_DIR resolves correctly whether called from cron or interactively.
_env_path = Path.home() / ".jarvis.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DB_PATH = Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"
_LOCAL_TZ = ZoneInfo("America/Toronto")
_SKILL_ROOT = Path(__file__).parents[2] / "skills"
_DISCORD_SCRIPT = Path(__file__).parents[2] / "scripts" / "discord_post.py"
_HEARTBEAT_GAP_MIN = 30  # alert if gap between ticks exceeds this many minutes

# Quiet-hours gate — shared single source of truth with the followups skill.
# During this window outbound Discord posts are queued and flushed at quiet-end.
_QUIET_START = int(os.environ.get("FOLLOW_UP_QUIET_START", "23"))  # local hour
_QUIET_END = int(os.environ.get("FOLLOW_UP_QUIET_END", "9"))  # local hour

# Discord user ID to @mention on high-signal posts (outages, errors, stall alerts).
# Lives in ~/.jarvis.env as DISCORD_NOTIFY_USER_ID. Empty = no mention.
_NOTIFY_USER_ID = os.environ.get("DISCORD_NOTIFY_USER_ID", "")

# Shell metacharacter pattern — any arg matching this is rejected at propose time
_SHELL_META = re.compile(r"[;&|><`\\]|\$\(|\$\{")

_DOW_MAP = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def _init_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            skill       TEXT NOT NULL,
            args        TEXT NOT NULL DEFAULT '[]',
            schedule    TEXT NOT NULL,
            next_run    TEXT NOT NULL,
            last_run    TEXT,
            last_status TEXT,
            enabled     INTEGER NOT NULL DEFAULT 1,
            approved    INTEGER NOT NULL DEFAULT 0,
            created_by  TEXT NOT NULL DEFAULT 'jarvis',
            description TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS heartbeat (
            id        INTEGER PRIMARY KEY CHECK (id = 1),
            last_tick TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outbound_queue (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            queued_at TEXT NOT NULL,
            message   TEXT NOT NULL,
            signal    TEXT NOT NULL DEFAULT 'ambient'
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Quiet-hours helpers
# ---------------------------------------------------------------------------


def _is_quiet_now() -> bool:
    """True if the current local time is inside the quiet window."""
    h = _now_local().hour
    if _QUIET_START > _QUIET_END:  # wraps midnight, e.g. 23→9
        return h >= _QUIET_START or h < _QUIET_END
    return _QUIET_START <= h < _QUIET_END


def _gap_is_expected_overnight(prev: datetime, now: datetime, gap_min: float) -> bool:
    """True when the gap is explained by an expected overnight machine-off.

    Fires when the last tick was at or within 2 hours of quiet-start (or already
    inside the quiet window) and the gap is at least as long as the quiet window
    minus a 2-hour restart buffer.  This avoids alerting when the PC is simply
    shut down overnight.
    """
    quiet_duration_min = ((_QUIET_END - _QUIET_START + 24) % 24) * 60
    if gap_min < quiet_duration_min - 120:
        return False  # too short to be an overnight pause
    prev_h = prev.astimezone(_LOCAL_TZ).hour
    # Was the last tick inside quiet hours or within 2 hours before quiet-start?
    if _QUIET_START > _QUIET_END:
        in_quiet = prev_h >= _QUIET_START or prev_h < _QUIET_END
    else:
        in_quiet = _QUIET_START <= prev_h < _QUIET_END
    hours_before_quiet = (_QUIET_START - prev_h) % 24
    return in_quiet or hours_before_quiet <= 2


# ---------------------------------------------------------------------------
# Outbound delivery helpers
# ---------------------------------------------------------------------------


def _post_discord_direct(message: str, signal: str = "ambient") -> None:
    """Post immediately to Discord (no quiet-hours check). Never crashes the caller."""
    if not _DISCORD_SCRIPT.exists():
        return
    try:
        cmd = ["python3", str(_DISCORD_SCRIPT)]
        if signal == "high" and _NOTIFY_USER_ID:
            cmd.append("--mention")
        subprocess.run(
            cmd,
            input=message,
            text=True,
            capture_output=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


def _post_discord(conn: sqlite3.Connection, message: str, signal: str = "ambient") -> None:
    """Route a Discord post through the quiet-hours gate.

    During quiet hours: enqueue in outbound_queue (flushed at quiet-end).
    Outside quiet hours: post immediately via _post_discord_direct.
    signal='high' prepends an @mention to alert AJ when flushed/posted.
    """
    if _is_quiet_now():
        conn.execute(
            "INSERT INTO outbound_queue (queued_at, message, signal) VALUES (?, ?, ?)",
            (_now_utc().isoformat(), message, signal),
        )
        conn.commit()
    else:
        _post_discord_direct(message, signal)


def _flush_outbound_queue(conn: sqlite3.Connection) -> None:
    """Post all queued messages in insertion order and clear the queue.

    Only called when not in quiet hours. Messages preserve their original signal
    level so high-signal posts still get the @mention when flushed.
    """
    if _is_quiet_now():
        return
    rows = conn.execute("SELECT id, message, signal FROM outbound_queue ORDER BY id").fetchall()
    if not rows:
        return
    ids = [row[0] for row in rows]
    for _, message, signal in rows:
        _post_discord_direct(message, signal)
    conn.execute(
        f"DELETE FROM outbound_queue WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    )
    conn.commit()


def _tick_heartbeat(conn: sqlite3.Connection, now: datetime) -> None:
    """Record this tick; alert on a real gap (not an expected overnight pause)."""
    row = conn.execute("SELECT last_tick FROM heartbeat WHERE id = 1").fetchone()
    if row:
        prev = datetime.fromisoformat(row[0])
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=UTC)
        gap_min = (now - prev).total_seconds() / 60
        if gap_min > _HEARTBEAT_GAP_MIN and not _gap_is_expected_overnight(prev, now, gap_min):
            _post_discord(
                conn,
                f"⚠️ Jarvis heartbeat: {gap_min:.0f}-min gap detected"
                f" (threshold {_HEARTBEAT_GAP_MIN} min). Dispatcher was offline.",
                signal="high",
            )
    conn.execute(
        "INSERT OR REPLACE INTO heartbeat (id, last_tick) VALUES (1, ?)",
        (now.isoformat(),),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _now_local() -> datetime:
    return datetime.now(_LOCAL_TZ)


def _is_oneoff(schedule: str) -> bool:
    return schedule.strip().lower().startswith("once@")


def _compute_next_run(schedule: str, from_dt: datetime) -> datetime:
    """Compute the next run time given a schedule string and a reference time."""
    s = schedule.strip()

    # Interval: 30m, 2h, 7d
    m = re.match(r"^(\d+)([mhd])$", s, re.IGNORECASE)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[unit]
        return from_dt + delta

    # daily@HH:MM
    m = re.match(r"^daily@(\d{1,2}):(\d{2})$", s, re.IGNORECASE)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        local = from_dt.astimezone(_LOCAL_TZ)
        candidate = local.replace(hour=h, minute=mn, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    # weekly@DOW@HH:MM  e.g. weekly@mon@09:00
    m = re.match(r"^weekly@([a-z]+)@(\d{1,2}):(\d{2})$", s, re.IGNORECASE)
    if m:
        dow_str = m.group(1).lower()
        if dow_str not in _DOW_MAP:
            raise ValueError(f"Unknown day of week: {dow_str!r}. Use mon/tue/wed/thu/fri/sat/sun.")
        target_dow = _DOW_MAP[dow_str]
        h, mn = int(m.group(2)), int(m.group(3))
        local = from_dt.astimezone(_LOCAL_TZ)
        candidate = local.replace(hour=h, minute=mn, second=0, microsecond=0)
        days_ahead = (target_dow - candidate.weekday()) % 7
        if days_ahead == 0 and candidate <= local:
            days_ahead = 7
        candidate += timedelta(days=days_ahead)
        return candidate.astimezone(UTC)

    # once@YYYY-MM-DDTHH:MM — fire at a fixed local time, then retire
    if s.lower().startswith("once@"):
        val = s[5:]
        try:
            naive = datetime.strptime(val, "%Y-%m-%dT%H:%M")
        except ValueError as exc:
            raise ValueError(
                f"Malformed once@ datetime: {val!r}. "
                "Expected format: once@YYYY-MM-DDTHH:MM (e.g. once@2026-06-10T14:30)"
            ) from exc
        return naive.replace(tzinfo=_LOCAL_TZ).astimezone(UTC)

    raise ValueError(
        f"Unsupported schedule format: {s!r}. "
        "Use: 30m, 2h, 7d, daily@HH:MM, weekly@mon@HH:MM, once@YYYY-MM-DDTHH:MM"
    )


def _fmt_local(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_skill(skill_name: str) -> Path:
    """Return the skill.py path, raising ValueError if not allowlisted."""
    if not re.match(r"^[a-z][a-z0-9-]*$", skill_name):
        raise ValueError(f"Invalid skill name: {skill_name!r}")
    skill_py = _SKILL_ROOT / skill_name / "skill.py"
    if not skill_py.exists():
        raise ValueError(f"Skill not found: {skill_name!r} (expected {skill_py})")
    return skill_py


def _validate_args(args: list[str]) -> None:
    """Reject any arg containing shell metacharacters."""
    for arg in args:
        if _SHELL_META.search(arg):
            raise ValueError(f"Arg contains shell metacharacters: {arg!r}")


def _validate_schedule(schedule: str) -> None:
    """Validate schedule format by attempting to compute next_run."""
    _compute_next_run(schedule, _now_utc())


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_propose(argv: list[str]) -> None:
    """
    propose --skill SKILL --schedule SCHEDULE --description DESC [--args JSON_ARRAY]
               [--created-by aj]

    Stages a schedule with approved=false. Validates skill and args immediately.
    """
    params: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv):
            params[argv[i][2:].replace("-", "_")] = argv[i + 1]
            i += 2
        else:
            i += 1

    skill_name = params.get("skill", "").strip()
    schedule = params.get("schedule", "").strip()
    description = params.get("description", "").strip()
    args_raw = params.get("args", "[]").strip()
    created_by = params.get("created_by", "jarvis").strip()

    if not skill_name or not schedule or not description:
        print("Error: --skill, --schedule, and --description are required", file=sys.stderr)
        sys.exit(1)

    try:
        _validate_skill(skill_name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        skill_args: list[str] = json.loads(args_raw)
        if not isinstance(skill_args, list):
            raise ValueError("--args must be a JSON array of strings")
        if not all(isinstance(a, str) for a in skill_args):
            raise ValueError("--args entries must all be strings")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Error parsing --args: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        _validate_args(skill_args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        _validate_schedule(schedule)
    except ValueError as exc:
        print(f"Error: invalid schedule: {exc}", file=sys.stderr)
        sys.exit(1)

    now = _now_utc()
    next_run = _compute_next_run(schedule, now)

    conn = _init_db()
    try:
        cur = conn.execute(
            """INSERT INTO schedules
               (created_at, skill, args, schedule, next_run, created_by, description)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                now.isoformat(),
                skill_name,
                json.dumps(skill_args),
                schedule,
                next_run.isoformat(),
                created_by,
                description,
            ),
        )
        conn.commit()
        job_id = cur.lastrowid

        print(
            json.dumps(
                {
                    "id": job_id,
                    "skill": skill_name,
                    "schedule": schedule,
                    "next_run": _fmt_local(next_run.isoformat()),
                    "description": description,
                    "approved": False,
                    "status": "staged — waiting for AJ approval",
                },
                indent=2,
            )
        )
    finally:
        conn.close()


def cmd_approve(argv: list[str]) -> None:
    """approve <id>"""
    if not argv:
        print("Usage: approve <id>", file=sys.stderr)
        sys.exit(1)
    job_id = int(argv[0])
    conn = _init_db()
    try:
        row = conn.execute(
            "SELECT skill, schedule, description FROM schedules WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            print(f"No schedule with id={job_id}", file=sys.stderr)
            sys.exit(1)
        skill_name, schedule, description = row
        # Recompute next_run from now on approval so it fires correctly
        next_run = _compute_next_run(schedule, _now_utc())
        conn.execute(
            "UPDATE schedules SET approved = 1, next_run = ? WHERE id = ?",
            (next_run.isoformat(), job_id),
        )
        conn.commit()
        print(
            json.dumps(
                {
                    "id": job_id,
                    "skill": skill_name,
                    "description": description,
                    "approved": True,
                    "next_run": _fmt_local(next_run.isoformat()),
                },
                indent=2,
            )
        )
    finally:
        conn.close()


def cmd_reject(argv: list[str]) -> None:
    """reject <id>  — deletes the staged schedule"""
    if not argv:
        print("Usage: reject <id>", file=sys.stderr)
        sys.exit(1)
    job_id = int(argv[0])
    conn = _init_db()
    try:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (job_id,))
        conn.commit()
        if cur.rowcount == 0:
            print(f"No schedule with id={job_id}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"id": job_id, "status": "rejected and deleted"}))
    finally:
        conn.close()


def cmd_list(argv: list[str]) -> None:
    """list [--all]  — default shows only enabled/pending jobs"""
    show_all = "--all" in argv
    conn = _init_db()
    try:
        if show_all:
            rows = conn.execute(
                """SELECT id, skill, args, schedule, next_run, last_run, last_status,
                          enabled, approved, created_by, description
                   FROM schedules ORDER BY id"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, skill, args, schedule, next_run, last_run, last_status,
                          enabled, approved, created_by, description
                   FROM schedules WHERE enabled = 1 ORDER BY next_run"""
            ).fetchall()

        if not rows:
            print("No schedules.")
            return

        lines = []
        for (
            job_id,
            skill,
            args_s,
            sched,
            next_run,
            last_run,
            last_status,
            enabled,
            approved,
            _created_by,
            desc,
        ) in rows:
            flags = []
            if not approved:
                flags.append("⏳ awaiting approval")
            if not enabled:
                flags.append("disabled")
            flag_str = f" [{', '.join(flags)}]" if flags else ""

            parts = [f"[{job_id}] **{skill}** — {desc}{flag_str}"]
            parts.append(f"schedule: {sched} | next: {_fmt_local(next_run)}")
            if last_run:
                parts.append(f"last: {_fmt_local(last_run)} ({last_status or 'unknown'})")
            try:
                skill_args = json.loads(args_s)
                if skill_args:
                    parts.append(f"args: {skill_args}")
            except json.JSONDecodeError:
                pass
            lines.append("\n  ".join(parts))

        print("\n\n".join(lines))
    finally:
        conn.close()


def cmd_enable(argv: list[str]) -> None:
    """enable <id>"""
    if not argv:
        print("Usage: enable <id>", file=sys.stderr)
        sys.exit(1)
    job_id = int(argv[0])
    conn = _init_db()
    try:
        cur = conn.execute("UPDATE schedules SET enabled = 1 WHERE id = ?", (job_id,))
        conn.commit()
        if cur.rowcount == 0:
            print(f"No schedule with id={job_id}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"id": job_id, "status": "enabled"}))
    finally:
        conn.close()


def cmd_disable(argv: list[str]) -> None:
    """disable <id>"""
    if not argv:
        print("Usage: disable <id>", file=sys.stderr)
        sys.exit(1)
    job_id = int(argv[0])
    conn = _init_db()
    try:
        cur = conn.execute("UPDATE schedules SET enabled = 0 WHERE id = ?", (job_id,))
        conn.commit()
        if cur.rowcount == 0:
            print(f"No schedule with id={job_id}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"id": job_id, "status": "disabled"}))
    finally:
        conn.close()


def cmd_delete(argv: list[str]) -> None:
    """delete <id>"""
    if not argv:
        print("Usage: delete <id>", file=sys.stderr)
        sys.exit(1)
    job_id = int(argv[0])
    conn = _init_db()
    try:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (job_id,))
        conn.commit()
        if cur.rowcount == 0:
            print(f"No schedule with id={job_id}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"id": job_id, "status": "deleted"}))
    finally:
        conn.close()


def cmd_dispatch(argv: list[str]) -> None:
    """
    dispatch — run all due, approved, enabled jobs.
    Called by the heartbeat script. Never by the agent directly.

    For each due job:
      - Validates skill still exists on disk
      - Invokes skills/<skill>/skill.py with args as argv (no shell=True)
      - Forwards non-empty stdout to Discord
      - Posts a Discord error alert on non-zero exit
      - Records exit status and recomputes next_run
    Failures are logged and recorded; they never crash the dispatcher.
    """
    now = _now_utc()
    conn = _init_db()
    try:
        _tick_heartbeat(conn, now)
        # Flush any messages deferred during quiet hours (no-op if still quiet or queue is empty).
        _flush_outbound_queue(conn)

        rows = conn.execute(
            """SELECT id, skill, args, schedule, description
               FROM schedules
               WHERE enabled = 1 AND approved = 1 AND next_run <= ?
               ORDER BY next_run""",
            (now.isoformat(),),
        ).fetchall()

        for job_id, skill_name, args_json, schedule, _desc in rows:
            skill_py = _SKILL_ROOT / skill_name / "skill.py"

            # Re-validate skill exists (defense against a renamed/deleted skill)
            if not skill_py.exists():
                _record_run(conn, job_id, schedule, now, "error:skill-not-found")
                print(
                    f"[schedules] job={job_id} skill={skill_name} error=skill-not-found",
                    file=sys.stderr,
                )
                _post_discord(
                    conn,
                    f"⚠️ Jarvis scheduler: job {job_id} ({skill_name}) — skill file not found",
                    signal="high",
                )
                continue

            try:
                skill_args: list[str] = json.loads(args_json)
            except json.JSONDecodeError:
                _record_run(conn, job_id, schedule, now, "error:invalid-args-json")
                print(
                    f"[schedules] job={job_id} skill={skill_name} error=invalid-args-json",
                    file=sys.stderr,
                )
                continue

            # Use the skill's own venv if present (e.g. gmail-cleanup needs google-auth).
            # Falls back to system python3 for stdlib-only skills (followups, schedules).
            venv_python = _SKILL_ROOT / skill_name / ".venv" / "bin" / "python"
            python_bin = str(venv_python) if venv_python.exists() else "python3"

            # argv list — no shell=True, no string assembly, no metacharacters
            cmd = [python_bin, str(skill_py)] + skill_args
            result = None
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                status = "ok" if result.returncode == 0 else f"exit:{result.returncode}"
            except subprocess.TimeoutExpired:
                status = "error:timeout"
                _post_discord(
                    conn,
                    f"⚠️ Jarvis scheduler: job {job_id} ({skill_name}) timed out after 120s",
                    signal="high",
                )
            except Exception as exc:  # noqa: BLE001
                status = "error:exception"
                print(f"[schedules] job={job_id} exception={exc}", file=sys.stderr)
                _post_discord(
                    conn,
                    f"⚠️ Jarvis scheduler: job {job_id} ({skill_name}) raised exception: {exc}",
                    signal="high",
                )

            if result is not None:
                # Forward any skill output to Discord.
                # SIGNAL:high prefix bypasses the quiet-hours gate — dev-loop verdicts,
                # stall alerts, and errors must reach AJ immediately regardless of hour.
                if result.stdout.strip():
                    raw = result.stdout.strip()
                    if raw == "SILENT":
                        pass  # skill signals nothing to report — do not forward
                    else:
                        _HIGH_PREFIX = "SIGNAL:high\n"
                        if raw.startswith(_HIGH_PREFIX):
                            _post_discord_direct(raw[len(_HIGH_PREFIX) :], signal="high")
                        else:
                            _post_discord(conn, raw, signal="ambient")
                # Alert on failure
                if result.returncode != 0:
                    err_tail = (result.stderr or "").strip()[-400:]
                    _post_discord(
                        conn,
                        f"⚠️ Jarvis scheduler: job {job_id} ({skill_name}) failed"
                        f" ({status})\n```\n{err_tail}\n```",
                        signal="high",
                    )

            _record_run(conn, job_id, schedule, now, status)
            print(f"[schedules] job={job_id} skill={skill_name} status={status}", file=sys.stderr)
    finally:
        conn.close()


def _record_run(
    conn: sqlite3.Connection,
    job_id: int,
    schedule: str,
    ran_at: datetime,
    status: str,
) -> None:
    """Update last_run, last_status, and recompute next_run."""
    if _is_oneoff(schedule):
        conn.execute(
            "UPDATE schedules SET last_run = ?, last_status = ?, enabled = 0 WHERE id = ?",
            (ran_at.isoformat(), f"{status} (fired, one-off retired)", job_id),
        )
        conn.commit()
        return

    try:
        next_run = _compute_next_run(schedule, ran_at)
    except ValueError:
        # Schedule is somehow invalid now — disable to prevent infinite retries
        conn.execute(
            "UPDATE schedules SET last_run = ?, last_status = ?, enabled = 0 WHERE id = ?",
            (ran_at.isoformat(), f"{status} (disabled: bad schedule)", job_id),
        )
        conn.commit()
        return

    conn.execute(
        "UPDATE schedules SET last_run = ?, last_status = ?, next_run = ? WHERE id = ?",
        (ran_at.isoformat(), status, next_run.isoformat(), job_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: skill.py <command> [args...]\n"
            "Commands:\n"
            "  propose --skill SKILL --schedule SCHEDULE --description DESC\n"
            '          [--args \'["arg1","arg2"]\'] [--created-by aj]\n'
            "  approve <id>\n"
            "  reject  <id>\n"
            "  list    [--all]\n"
            "  enable  <id>\n"
            "  disable <id>\n"
            "  delete  <id>\n"
            "  dispatch        (called by heartbeat — not for manual use)\n"
            "\n"
            "Schedule formats:\n"
            "  30m, 2h, 7d              repeat every N minutes/hours/days\n"
            "  daily@HH:MM              once per day at local time\n"
            "  weekly@mon@HH:MM         once per week on given day at local time\n"
            "  once@YYYY-MM-DDTHH:MM   fire once at a fixed local time, then retire",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1].lower()
    rest = sys.argv[2:]

    dispatch_map = {
        "propose": cmd_propose,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "list": cmd_list,
        "enable": cmd_enable,
        "disable": cmd_disable,
        "delete": cmd_delete,
        "dispatch": cmd_dispatch,
    }

    if cmd not in dispatch_map:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        sys.exit(1)

    dispatch_map[cmd](rest)


if __name__ == "__main__":
    main()
