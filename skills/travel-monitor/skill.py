"""
Travel monitor skill — flexible-date travel cost tracker.

Scans three independent lanes per watch:
  - Flights: Amadeus Flight Cheapest Date Search (triage) + Offers Search (confirm)
  - Hotels: Firecrawl scrape of named property by-provider + date-calendar view
  - Packages: Firecrawl scrape of operator flex-date search (costco/aircanada/westjet/expedia)

Reconciles package vs. DIY (flight + hotel); alerts on deals via Discord.
Alerts only — never books anything. Silent run unless a deal or health alert fires.

Stdlib only; no virtualenv needed.

Phase 0: scaffold only — DB schema, command-surface stub, env wiring.
         Lanes return NotImplementedError stubs; no actual fetching.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DB_PATH = Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"
_LOCAL_TZ = ZoneInfo("America/Toronto")
_DISCORD_SCRIPT = Path(__file__).parents[2] / "scripts" / "discord_post.py"
_SKILL_ROOT = Path(__file__).parents[1]

# Manual .env parse — no dotenv dependency
_env_path = Path.home() / ".jarvis.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

DEAL_DROP_PCT = float(os.environ.get("TRAVEL_MONITOR_DEAL_DROP_PCT", "10"))
N_MEDIAN = int(os.environ.get("TRAVEL_MONITOR_N_MEDIAN", "8"))
N_DAY_LOW = int(os.environ.get("TRAVEL_MONITOR_N_DAY_LOW", "30"))
ESCALATE_DAYS = int(os.environ.get("TRAVEL_MONITOR_ESCALATE_DAYS", "21"))

VALID_OPERATORS = {"costco", "aircanada", "westjet", "expedia"}
VALID_CADENCES = {"weekly", "daily"}
VALID_RESOLVE_ACTIONS = {"booked", "passed"}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def _init_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS travel_watches (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT NOT NULL,
            destination        TEXT NOT NULL,
            origin_iata        TEXT NOT NULL,
            window_start       TEXT NOT NULL,
            window_end         TEXT NOT NULL,
            stay_nights        INTEGER NOT NULL,
            adults             INTEGER NOT NULL DEFAULT 2,
            children           INTEGER NOT NULL DEFAULT 0,
            hotel_targets      TEXT NOT NULL DEFAULT '[]',
            package_operators  TEXT NOT NULL DEFAULT '[]',
            target_price       REAL,
            currency           TEXT NOT NULL DEFAULT 'CAD',
            cadence            TEXT NOT NULL DEFAULT 'weekly',
            status             TEXT NOT NULL DEFAULT 'active',
            fail_count         INTEGER NOT NULL DEFAULT 0,
            active_from        TEXT,
            active_until       TEXT,
            created_at         TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS travel_price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id    INTEGER NOT NULL,
            checked_at  TEXT NOT NULL,
            lane        TEXT NOT NULL,
            best_price  REAL,
            currency    TEXT NOT NULL DEFAULT 'CAD',
            checkin     TEXT,
            checkout    TEXT,
            provider    TEXT,
            raw         TEXT,
            method      TEXT
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def _discord_post(message: str) -> None:
    if not _DISCORD_SCRIPT.exists():
        print(f"[travel-monitor] discord_post.py not found at {_DISCORD_SCRIPT}", file=sys.stderr)
        return
    try:
        result = subprocess.run(
            ["python3", str(_DISCORD_SCRIPT)],
            input=message,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            print(f"[travel-monitor] discord_post failed: {result.stderr.strip()}", file=sys.stderr)
    except Exception as exc:
        print(f"[travel-monitor] discord_post exception: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Follow-ups integration
# ---------------------------------------------------------------------------


def _create_followup(name: str, note: str, trigger_at: str, window_until: str | None) -> None:
    followup_py = _SKILL_ROOT / "followups" / "skill.py"
    if not followup_py.exists():
        print(f"[travel-monitor] followups skill not found at {followup_py}", file=sys.stderr)
        return
    cmd = [
        "python3",
        str(followup_py),
        "add",
        "--subject",
        f"Travel check: {name}",
        "--prompt",
        note,
        "--policy",
        "periodic",
        "--trigger-at",
        trigger_at,
        "--cadence",
        "7",
        "--source",
        "open_loop",
    ]
    if window_until:
        cmd += ["--window-until", window_until]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            print(
                f"[travel-monitor] follow-up creation failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[travel-monitor] follow-up exception: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _today_local() -> str:
    return datetime.now(_LOCAL_TZ).date().isoformat()


def _parse_args(args: list[str]) -> dict[str, list[str] | str]:
    """
    Parse --key value pairs. Keys that appear multiple times accumulate as lists
    (hotel, operator). All other keys take the last value.
    """
    multi_keys = {"hotel", "operator"}
    params: dict[str, list[str] | str] = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            key = args[i][2:].replace("-", "_")
            val = args[i + 1]
            if key in multi_keys:
                existing = params.get(key, [])
                if isinstance(existing, list):
                    existing.append(val)
                else:
                    existing = [existing, val]
                params[key] = existing
            else:
                params[key] = val
            i += 2
        else:
            i += 1
    return params


# ---------------------------------------------------------------------------
# Lane stubs (Phase 1–3 will replace these)
# ---------------------------------------------------------------------------


def _lane_flights(watch: dict) -> dict:
    """
    Phase 1: Amadeus OAuth2 + Flight Cheapest Date Search + Offers Search.
    Returns {lane: 'flight', status: 'unavailable', reason: 'not_implemented'}.
    """
    return {"lane": "flight", "status": "unavailable", "reason": "not_implemented"}


def _lane_hotels(watch: dict) -> dict:
    """
    Phase 2: Firecrawl scrape of named property by-provider + date-calendar view.
    Returns {lane: 'hotel', status: 'unavailable', reason: 'not_implemented'}.
    """
    return {"lane": "hotel", "status": "unavailable", "reason": "not_implemented"}


def _lane_packages(watch: dict, operator: str) -> dict:
    """
    Phase 3: Firecrawl scrape of operator flex-date search.
    Returns {lane: 'package', operator: ..., status: 'unavailable', reason: 'not_implemented'}.
    """
    return {
        "lane": "package",
        "operator": operator,
        "status": "unavailable",
        "reason": "not_implemented",
    }


# ---------------------------------------------------------------------------
# Deal detection
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _detect_deal(
    conn: sqlite3.Connection,
    watch_id: int,
    current_price: float,
    target_price: float | None,
    inserted_at: str,
) -> str | None:
    if target_price is not None and current_price <= target_price:
        return f"at or below target ${target_price:.2f} CAD"

    prior_rows = conn.execute(
        """SELECT best_price FROM travel_price_history
           WHERE watch_id = ? AND lane = 'reconciled' AND checked_at < ? AND best_price IS NOT NULL
           ORDER BY checked_at DESC
           LIMIT ?""",
        (watch_id, inserted_at, N_MEDIAN),
    ).fetchall()
    prior_prices = [row[0] for row in prior_rows]

    if len(prior_prices) < 2:
        return None

    median_price = _median(prior_prices[:N_MEDIAN])
    if median_price > 0:
        drop_pct = (median_price - current_price) / median_price * 100
        if drop_pct >= DEAL_DROP_PCT:
            return f"{drop_pct:.0f}% below {len(prior_prices)}-reading median ${median_price:.2f}"

    cutoff = (datetime.now(UTC) - timedelta(days=N_DAY_LOW)).isoformat()
    period_row = conn.execute(
        """SELECT MIN(best_price) FROM travel_price_history
           WHERE watch_id = ? AND lane = 'reconciled'
             AND checked_at >= ? AND checked_at < ?
             AND best_price IS NOT NULL""",
        (watch_id, cutoff, inserted_at),
    ).fetchone()
    if period_row and period_row[0] is not None:
        prior_min = period_row[0]
        if current_price < prior_min:
            return f"{N_DAY_LOW}-day low (previous min ${prior_min:.2f})"

    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_add(args: list[str]) -> None:
    params = _parse_args(args)

    name = str(params.get("name", "")).strip()
    dest = str(params.get("dest", "")).strip().upper()
    origin = str(params.get("origin", "")).strip().upper()
    window_start = str(params.get("window_start", "")).strip()
    window_end = str(params.get("window_end", "")).strip()
    nights_raw = params.get("nights", "")

    errors = []
    if not name:
        errors.append("--name is required")
    if not dest:
        errors.append("--dest is required (IATA city/airport code, e.g. PUJ)")
    if not origin:
        errors.append("--origin is required (IATA code, e.g. YUL)")
    if not window_start:
        errors.append("--window-start is required (YYYY-MM-DD)")
    if not window_end:
        errors.append("--window-end is required (YYYY-MM-DD)")
    if not nights_raw:
        errors.append("--nights is required")

    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        stay_nights = int(str(nights_raw))
        if stay_nights < 1:
            raise ValueError
    except ValueError:
        print("Error: --nights must be a positive integer", file=sys.stderr)
        sys.exit(1)

    try:
        ws = datetime.fromisoformat(window_start).date()
        we = datetime.fromisoformat(window_end).date()
        if we <= ws:
            print("Error: --window-end must be after --window-start", file=sys.stderr)
            sys.exit(1)
        if (we - ws).days < stay_nights:
            print(
                f"Error: window is only {(we - ws).days} days but --nights is {stay_nights}",
                file=sys.stderr,
            )
            sys.exit(1)
    except ValueError:
        print("Error: dates must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    adults = 2
    if "adults" in params:
        try:
            adults = int(str(params["adults"]))
        except ValueError:
            print("Error: --adults must be an integer", file=sys.stderr)
            sys.exit(1)

    children = 0
    if "children" in params:
        try:
            children = int(str(params["children"]))
        except ValueError:
            print("Error: --children must be an integer", file=sys.stderr)
            sys.exit(1)

    hotels_raw = params.get("hotel", [])
    hotel_targets: list[str] = hotels_raw if isinstance(hotels_raw, list) else [hotels_raw]

    operators_raw = params.get("operator", [])
    operators: list[str] = operators_raw if isinstance(operators_raw, list) else [operators_raw]
    invalid_ops = [o for o in operators if o not in VALID_OPERATORS]
    if invalid_ops:
        print(
            f"Error: unknown operator(s): {invalid_ops}. Valid: {sorted(VALID_OPERATORS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    target_price: float | None = None
    if "target_price" in params:
        try:
            target_price = float(str(params["target_price"]))
        except ValueError:
            print("Error: --target-price must be a number", file=sys.stderr)
            sys.exit(1)

    cadence = str(params.get("cadence", "weekly"))
    if cadence not in VALID_CADENCES:
        print(f"Error: --cadence must be one of {sorted(VALID_CADENCES)}", file=sys.stderr)
        sys.exit(1)

    # Add-time probe: run each enabled lane once, surface failures
    watch_stub = {
        "destination": dest,
        "origin_iata": origin,
        "window_start": window_start,
        "window_end": window_end,
        "stay_nights": stay_nights,
        "adults": adults,
        "children": children,
        "hotel_targets": hotel_targets,
        "package_operators": operators,
    }

    lane_results: list[dict] = []
    if True:  # flights always probed
        lane_results.append(_lane_flights(watch_stub))
    if hotel_targets:
        lane_results.append(_lane_hotels(watch_stub))
    for op in operators:
        lane_results.append(_lane_packages(watch_stub, op))

    # Distinguish real runtime failures from not-yet-implemented stubs.
    # Stubs (reason='not_implemented') are expected in Phases 0–2; they never block
    # watch creation. A real failure (reason != 'not_implemented') means the lane
    # actually tried and couldn't reach the provider.
    runtime_failures = [
        r
        for r in lane_results
        if r.get("status") == "unavailable" and r.get("reason") != "not_implemented"
    ]
    stubs = [r for r in lane_results if r.get("reason") == "not_implemented"]
    available = [r for r in lane_results if r.get("status") != "unavailable"]

    if not available and runtime_failures:
        note = (
            f"All lanes failed for **{name}** ({dest}, {window_start}–{window_end}) — "
            "want me to set a weekly reminder to check manually instead?"
        )
        print(json.dumps({"status": "needs_reminder", "name": name, "message": note}, indent=2))
        return

    now_iso = _now_utc().isoformat()
    conn = _init_db()
    try:
        cur = conn.execute(
            """INSERT INTO travel_watches
               (name, destination, origin_iata, window_start, window_end, stay_nights,
                adults, children, hotel_targets, package_operators, target_price,
                currency, cadence, status, fail_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CAD', ?, 'active', 0, ?)""",
            (
                name,
                dest,
                origin,
                window_start,
                window_end,
                stay_nights,
                adults,
                children,
                json.dumps(hotel_targets),
                json.dumps(operators),
                target_price,
                cadence,
                now_iso,
            ),
        )
        watch_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    msg_parts = [
        f"Watching **{name}** — {origin}→{dest}, {adults}+{children} pax, "
        f"{stay_nights} nights in {window_start}–{window_end}. Cadence: {cadence}."
    ]
    if target_price:
        msg_parts.append(f"Alert target: ${target_price:.0f} CAD.")
    if stubs:
        stub_names = [r.get("operator", r.get("lane", "?")) for r in stubs]
        msg_parts.append(
            f"Note: {len(stubs)} lane(s) not yet implemented "
            f"({', '.join(str(n) for n in stub_names)}) — will activate in later phases."
        )
    if runtime_failures:
        fail_names = [r.get("operator", r.get("lane", "?")) for r in runtime_failures]
        msg_parts.append(
            f"Warning: {len(runtime_failures)} lane(s) unreachable at add-time "
            f"({', '.join(str(n) for n in fail_names)})."
        )

    print(
        json.dumps(
            {
                "id": watch_id,
                "status": "active",
                "name": name,
                "destination": dest,
                "origin": origin,
                "window": f"{window_start}–{window_end}",
                "nights": stay_nights,
                "cadence": cadence,
                "lanes_probed": len(lane_results),
                "lanes_unavailable": len(runtime_failures) + len(stubs),
                "message": " ".join(msg_parts),
            },
            indent=2,
        )
    )


def cmd_list(args: list[str]) -> None:
    conn = _init_db()
    try:
        rows = conn.execute(
            """SELECT id, name, destination, origin_iata, window_start, window_end,
                      stay_nights, adults, children, cadence, status, target_price,
                      created_at
               FROM travel_watches
               ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'degraded' THEN 1 ELSE 2 END,
                        window_start ASC"""
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No travel watches.")
        return

    headers = ["ID", "Name", "Route", "Window", "Nights", "Pax", "Cadence", "Status", "Target"]
    col_widths = [len(h) for h in headers]

    table_rows: list[list[str]] = []
    for row in rows:
        (
            row_id,
            name,
            dest,
            origin,
            ws,
            we,
            nights,
            adults,
            children,
            cadence,
            status,
            target,
            _,
        ) = row
        name_trunc = name[:28] if len(name) > 28 else name
        route = f"{origin}→{dest}"
        window = f"{ws} – {we}"
        pax = f"{adults}+{children}"
        target_str = f"${target:.0f}" if target is not None else "—"
        r = [
            str(row_id),
            name_trunc,
            route,
            window,
            str(nights),
            pax,
            cadence,
            status,
            target_str,
        ]
        table_rows.append(r)
        for i, cell in enumerate(r):
            col_widths[i] = max(col_widths[i], len(cell))

    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"

    def fmt_row(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(col_widths[i]) for i, c in enumerate(cells)) + " |"

    lines = [sep, fmt_row(headers), sep] + [fmt_row(r) for r in table_rows] + [sep]
    print("\n".join(lines))


def cmd_resolve(args: list[str]) -> None:
    params = _parse_args(args)

    if "id" not in params:
        print("Error: --id is required", file=sys.stderr)
        sys.exit(1)
    action = str(params.get("action", ""))
    if action not in VALID_RESOLVE_ACTIONS:
        print(f"Error: --action must be one of {sorted(VALID_RESOLVE_ACTIONS)}", file=sys.stderr)
        sys.exit(1)

    try:
        watch_id = int(str(params["id"]))
    except ValueError:
        print("Error: --id must be an integer", file=sys.stderr)
        sys.exit(1)

    conn = _init_db()
    try:
        row = conn.execute(
            "SELECT name FROM travel_watches WHERE id = ? AND status != 'resolved'",
            (watch_id,),
        ).fetchone()
        if not row:
            print(f"No active watch with id={watch_id}", file=sys.stderr)
            sys.exit(1)

        conn.execute(
            "UPDATE travel_watches SET status = 'resolved' WHERE id = ?",
            (watch_id,),
        )
        conn.commit()
        print(
            json.dumps(
                {"id": watch_id, "name": row[0], "action": action, "status": "resolved"},
                indent=2,
            )
        )
    finally:
        conn.close()


def cmd_check(args: list[str]) -> None:
    """
    Scheduled run: scan active watches, run lanes, reconcile, detect deals.
    Silent unless a deal or health alert fires.

    Phase 0: stub — loads watches, logs lane stubs, posts nothing (no real data yet).
    """
    conn = _init_db()
    try:
        watches = conn.execute(
            """SELECT id, name, destination, origin_iata, window_start, window_end,
                      stay_nights, adults, children, hotel_targets, package_operators,
                      target_price, cadence, fail_count
               FROM travel_watches WHERE status = 'active'"""
        ).fetchall()
    finally:
        conn.close()

    if not watches:
        return

    today = _today_local()

    for row in watches:
        (
            watch_id,
            name,
            dest,
            origin,
            window_start,
            window_end,
            stay_nights,
            adults,
            children,
            hotel_targets_json,
            operators_json,
            target_price,
            cadence,
            fail_count,
        ) = row

        # Skip if watch window has passed
        if today > window_end:
            print(
                f"[travel-monitor] watch={watch_id} name={name!r} window expired, skipping",
                file=sys.stderr,
            )
            continue

        hotel_targets: list[str] = json.loads(hotel_targets_json or "[]")
        operators: list[str] = json.loads(operators_json or "[]")

        watch_stub = {
            "id": watch_id,
            "name": name,
            "destination": dest,
            "origin_iata": origin,
            "window_start": window_start,
            "window_end": window_end,
            "stay_nights": stay_nights,
            "adults": adults,
            "children": children,
            "hotel_targets": hotel_targets,
            "package_operators": operators,
        }

        lane_results: list[dict] = [_lane_flights(watch_stub)]
        if hotel_targets:
            lane_results.append(_lane_hotels(watch_stub))
        for op in operators:
            lane_results.append(_lane_packages(watch_stub, op))

        for result in lane_results:
            lane = result.get("lane", "unknown")
            status = result.get("status", "unknown")
            reason = result.get("reason", "")
            print(
                f"[travel-monitor] watch={watch_id} name={name!r} lane={lane} status={status}"
                + (f" reason={reason}" if reason else ""),
                file=sys.stderr,
            )

        # Phase 0: no real data — skip reconciliation and deal detection
        # Phase 4 will join flight + hotel results here and run _detect_deal()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: skill.py <command> [args...]\n"
            "Commands:\n"
            "  add --name N --dest IATA --origin IATA\n"
            "      --window-start YYYY-MM-DD --window-end YYYY-MM-DD --nights N\n"
            "      --adults N [--children N] [--hotel NAME]... [--operator NAME]...\n"
            "      [--target-price N] [--cadence weekly|daily]\n"
            "  list\n"
            "  resolve --id N --action booked|passed\n"
            "  check",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1].lower()
    rest = sys.argv[2:]

    dispatch = {
        "add": cmd_add,
        "list": cmd_list,
        "resolve": cmd_resolve,
        "check": cmd_check,
    }

    if cmd not in dispatch:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        sys.exit(1)

    dispatch[cmd](rest)


if __name__ == "__main__":
    main()
