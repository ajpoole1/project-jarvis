"""Google Calendar skill — query, create events, and sync logistics stubs to shared Home calendar."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv(Path.home() / ".jarvis.env")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

CONFIG_DIR = Path(os.environ.get("JARVIS_CONFIG_DIR", "/config/personal"))
CREDENTIALS_PATH = CONFIG_DIR / "gmail_credentials.json"
TOKEN_PATH = CONFIG_DIR / "calendar_token.json"

TIMEZONE = os.environ.get("JARVIS_TIMEZONE", "America/Toronto")
_raw_ids = os.environ.get("GOOGLE_CALENDAR_IDS", "primary")
CALENDAR_IDS = [c.strip() for c in _raw_ids.split(",") if c.strip()]

_PAGINATION_SAFETY_CAP = int(os.environ.get("JARVIS_CALENDAR_EVENT_CAP", "2500"))

# Shared Home calendar — stubs are written here
HOME_CALENDAR_ID = os.environ.get("GOOGLE_HOME_CALENDAR_ID", "")

_DB_PATH = Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"

# Keywords that mark an event as a non-logistic project/work block — never stub these
_NEVER_STUB = frozenset(
    {
        "babbel",
        "altaforma",
        "school",
        "jarvis",
        "study",
        "work block",
        "focus time",
        "planning",
        "webinar",
        "online meeting",
        "zoom",
        "teams meeting",
        "standup",
        "stand-up",
        "1:1",
        "sync",
    }
)

_STUB_MARKER_PREFIX = "[jarvis-stub:"


# ---------------------------------------------------------------------------
# DB (shared jarvis.db)
# ---------------------------------------------------------------------------


def _init_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_stubs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_event_id TEXT NOT NULL UNIQUE,
            stub_event_id   TEXT NOT NULL,
            source_calendar TEXT NOT NULL DEFAULT 'primary',
            home_calendar   TEXT NOT NULL,
            category        TEXT NOT NULL,
            source_start    TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_providers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            domain      TEXT NOT NULL DEFAULT '',
            keyword     TEXT NOT NULL DEFAULT '',
            category    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _load_providers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, domain, keyword, category FROM calendar_providers ORDER BY id"
    ).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "domain": r[2].lower(),
            "keyword": r[3].lower(),
            "category": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Calendar service
# ---------------------------------------------------------------------------


def _paginate_events(
    service,
    calendar_id: str,
    *,
    safety_cap: int = _PAGINATION_SAFETY_CAP,
    **list_kwargs,
) -> tuple[list[dict], bool]:
    """Paginate through all pages of a Calendar events.list call.

    Returns (events, truncated). truncated=True only when the safety cap was hit
    with more pages remaining — never on a natural end of results.
    """
    events: list[dict] = []
    page_token: str | None = None
    while True:
        req = {**list_kwargs, "calendarId": calendar_id, "maxResults": 250}
        if page_token:
            req["pageToken"] = page_token
        try:
            result = service.events().list(**req).execute()
        except Exception:
            return events, True
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            return events, False
        if len(events) >= safety_cap:
            return events, True


def get_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0, open_browser=False)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def _tz() -> ZoneInfo:
    return ZoneInfo(TIMEZONE)


def _day_bounds(d: date) -> tuple[str, str]:
    tz = _tz()
    start = datetime(d.year, d.month, d.day, tzinfo=tz).isoformat()
    end = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz).isoformat()
    return start, end


def _range_bounds(start_date: date, end_date: date) -> tuple[str, str]:
    tz = _tz()
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz).isoformat()
    end = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=tz).isoformat()
    return start, end


def fetch_events(service, time_min: str, time_max: str) -> list[dict]:
    """Fetch events across all configured calendars in a time range."""
    events = []
    for cal_id in CALENDAR_IDS:
        page_events, truncated = _paginate_events(
            service,
            cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        )
        if truncated:
            print(
                f"[calendar] ⚠️ safety cap ({_PAGINATION_SAFETY_CAP}) hit for calendar"
                f" '{cal_id}': results are incomplete",
                file=sys.stderr,
            )
        for e in page_events:
            e["_calendarId"] = cal_id
            events.append(e)
    events.sort(key=lambda e: e["start"].get("dateTime", e["start"].get("date", "")))
    return events


def format_event(e: dict) -> str:
    summary = e.get("summary", "(no title)")
    start = e["start"]
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"]).astimezone(_tz())
        time_str = dt.strftime("%H:%M")
    else:
        time_str = "all day"
    location = e.get("location", "")
    loc_str = f" @ {location}" if location else ""
    return f"  • {time_str} — {summary}{loc_str}"


def format_day_block(d: date, events: list[dict]) -> str:
    label = d.strftime("%A, %B %-d")
    if not events:
        return f"**{label}** — nothing scheduled"
    lines = [f"**{label}**"] + [format_event(e) for e in events]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stub classifier
# ---------------------------------------------------------------------------


def _classify_event(event: dict, providers: list[dict]) -> tuple[str | None, bool]:
    """
    Return (category, auto_stub) or (None, False) if this event should not be stubbed.
    auto_stub=True: high-confidence, stub immediately.
    auto_stub=False: uncertain, surface for AJ to confirm.
    """
    summary = event.get("summary", "").lower()
    organizer_email = (event.get("organizer") or {}).get("email", "").lower()
    attendees = [(a.get("email") or "").lower() for a in event.get("attendees") or []]
    location = (event.get("location") or "").lower().strip()
    has_location = bool(location)

    # Never stub: project/work blocks and online meetings
    if any(kw in summary for kw in _NEVER_STUB):
        return None, False

    # Step 1: Provider domain or keyword match (high confidence → auto)
    for provider in providers:
        domain = provider["domain"]
        keyword = provider["keyword"]
        if domain and (domain in organizer_email or any(domain in a for a in attendees)):
            return provider["category"], True
        if keyword and keyword in summary:
            return provider["category"], True

    # Step 2: Title keywords — off-site medical / therapy heuristic
    _offsite_kw = {
        "dentist",
        "dental",
        "dr.",
        "doctor",
        "physician",
        "therapy",
        "therapist",
        "physio",
        "physiotherapy",
        "chiro",
        "chiropractor",
        "optometrist",
        "ophthalmologist",
        "dermatologist",
        "appointment",
        "clinic",
        "hospital",
        "specialist",
        "bloodwork",
        "lab",
        "medical",
        "checkup",
        "check-up",
    }
    _unavailable_kw = {
        "therapy",
        "therapist",
        "counselling",
        "counseling",
        "psychiatrist",
        "psychologist",
    }

    matched_kw = next((kw for kw in _offsite_kw if kw in summary), None)
    if matched_kw:
        if any(kw in summary for kw in _unavailable_kw):
            category = "therapy"
        else:
            category = "appointment"

        if has_location:
            # Physical address found — high confidence
            return category, True
        # Keyword matched but no location — uncertain
        return category, False

    return None, False


def _stub_marker(source_event_id: str) -> str:
    return f"{_STUB_MARKER_PREFIX}{source_event_id}]"


def _find_existing_home_stub(service, home_cal_id: str, source_event: dict) -> dict | None:
    """
    Search the Home calendar for a pre-existing stub matching this source event.
    Checks our marker first, then falls back to title+time proximity (for manual pre-existing stubs).
    """
    source_id = source_event["id"]
    start = source_event["start"].get("dateTime", source_event["start"].get("date", ""))
    date_str = start[:10]

    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return None

    t_min, t_max = _day_bounds(d)
    items, truncated = _paginate_events(
        service,
        home_cal_id,
        timeMin=t_min,
        timeMax=t_max,
        singleEvents=True,
    )
    if truncated:
        print(
            f"[calendar] ⚠️ safety cap hit scanning home calendar for stub {source_id}",
            file=sys.stderr,
        )

    marker = _stub_marker(source_id)
    for event in items:
        description = event.get("description") or ""
        # Exact marker match
        if marker in description:
            return event
        # Fuzzy: "AJ —" in title + same 10-minute window (for manual pre-created stubs)
        ev_summary = event.get("summary", "").lower()
        ev_start = event["start"].get("dateTime", event["start"].get("date", ""))
        if "aj" in ev_summary and ev_start[:16] == start[:16]:
            return event

    return None


# ---------------------------------------------------------------------------
# Sync stubs — the main reconcile loop
# ---------------------------------------------------------------------------


def cmd_sync_stubs(args: list[str]) -> str:
    if not HOME_CALENDAR_ID:
        return "Error: GOOGLE_HOME_CALENDAR_ID is not set in ~/.jarvis.env"

    service = get_service()
    conn = _init_db()
    providers = _load_providers(conn)

    today = date.today()
    end_date = today + timedelta(days=14)
    t_min, _ = _day_bounds(today)
    _, t_max = _day_bounds(end_date)

    # Fetch personal calendar events in the 14-day window
    personal_events, _truncated = _paginate_events(
        service,
        "primary",
        timeMin=t_min,
        timeMax=t_max,
        singleEvents=True,
        orderBy="startTime",
    )
    if _truncated:
        print(
            "[calendar] ⚠️ safety cap hit on personal calendar: sync-stubs results are incomplete",
            file=sys.stderr,
        )

    # Load existing stubs mapping (source_event_id → {stub_id, category, source_start})
    stub_rows = conn.execute(
        "SELECT source_event_id, stub_event_id, category, source_start FROM calendar_stubs"
    ).fetchall()
    stub_map: dict[str, dict] = {
        r[0]: {"stub_id": r[1], "category": r[2], "source_start": r[3]} for r in stub_rows
    }

    now_iso = datetime.now(UTC).isoformat()
    auto_created: list[str] = []
    auto_updated: list[str] = []
    auto_deleted: list[str] = []
    proposals: list[dict] = []
    seen_source_ids: set[str] = set()

    for event in personal_events:
        source_id = event["id"]
        seen_source_ids.add(source_id)

        category, auto = _classify_event(event, providers)
        if category is None:
            continue

        start = event["start"].get("dateTime", event["start"].get("date", ""))
        has_location = bool((event.get("location") or "").strip())
        stub_summary = f"AJ — {category.title()}" + (" (out)" if has_location else "")

        existing = stub_map.get(source_id)

        if not auto:
            # Uncertain — propose only if we don't already have a stub for this
            if not existing:
                proposals.append(
                    {
                        "id": source_id,
                        "summary": event.get("summary", "(no title)"),
                        "date": start[:10],
                        "category": category,
                    }
                )
            continue

        if existing:
            # Update stub if source event time changed
            if existing["source_start"] != start:
                try:
                    service.events().patch(
                        calendarId=HOME_CALENDAR_ID,
                        eventId=existing["stub_id"],
                        body={
                            "summary": stub_summary,
                            "start": event["start"],
                            "end": event["end"],
                        },
                    ).execute()
                    conn.execute(
                        "UPDATE calendar_stubs SET source_start = ?, updated_at = ? WHERE source_event_id = ?",
                        (start, now_iso, source_id),
                    )
                    conn.commit()
                    auto_updated.append(stub_summary)
                except Exception as exc:
                    print(f"[calendar] update stub failed for {source_id}: {exc}", file=sys.stderr)
        else:
            # Look for a pre-existing manual stub to adopt, or create a new one
            existing_on_home = _find_existing_home_stub(service, HOME_CALENDAR_ID, event)
            try:
                if existing_on_home:
                    stub_id = existing_on_home["id"]
                    # Patch description marker if not already there (adoption)
                    description = existing_on_home.get("description") or ""
                    marker = _stub_marker(source_id)
                    if marker not in description:
                        service.events().patch(
                            calendarId=HOME_CALENDAR_ID,
                            eventId=stub_id,
                            body={"description": description + f"\n{marker}".strip()},
                        ).execute()
                    conn.execute(
                        """INSERT OR REPLACE INTO calendar_stubs
                           (source_event_id, stub_event_id, source_calendar, home_calendar,
                            category, source_start, created_at, updated_at)
                           VALUES (?, ?, 'primary', ?, ?, ?, ?, ?)""",
                        (source_id, stub_id, HOME_CALENDAR_ID, category, start, now_iso, now_iso),
                    )
                    conn.commit()
                    auto_created.append(f"{stub_summary} (adopted)")
                else:
                    stub_event = (
                        service.events()
                        .insert(
                            calendarId=HOME_CALENDAR_ID,
                            body={
                                "summary": stub_summary,
                                "start": event["start"],
                                "end": event["end"],
                                "description": _stub_marker(source_id),
                            },
                        )
                        .execute()
                    )
                    conn.execute(
                        """INSERT INTO calendar_stubs
                           (source_event_id, stub_event_id, source_calendar, home_calendar,
                            category, source_start, created_at, updated_at)
                           VALUES (?, ?, 'primary', ?, ?, ?, ?, ?)""",
                        (
                            source_id,
                            stub_event["id"],
                            HOME_CALENDAR_ID,
                            category,
                            start,
                            now_iso,
                            now_iso,
                        ),
                    )
                    conn.commit()
                    auto_created.append(stub_summary)
            except Exception as exc:
                print(f"[calendar] create stub failed for {source_id}: {exc}", file=sys.stderr)

    # Delete stubs whose source event is in the future window but no longer exists
    for source_id, stub_info in stub_map.items():
        if source_id in seen_source_ids:
            continue
        source_start = stub_info["source_start"]
        # Only actively delete future-dated stubs (past stubs expire naturally)
        try:
            source_date = date.fromisoformat(source_start[:10])
        except ValueError:
            source_date = date.today()

        if source_date >= today:
            # Source event was in window but is gone — source was cancelled
            try:
                stub_event = (
                    service.events()
                    .get(calendarId=HOME_CALENDAR_ID, eventId=stub_info["stub_id"])
                    .execute()
                )
                marker = _stub_marker(source_id)
                if marker in (stub_event.get("description") or ""):
                    service.events().delete(
                        calendarId=HOME_CALENDAR_ID, eventId=stub_info["stub_id"]
                    ).execute()
                    auto_deleted.append(stub_info["category"])
            except Exception:
                pass

        conn.execute("DELETE FROM calendar_stubs WHERE source_event_id = ?", (source_id,))

    conn.commit()

    lines = ["**Calendar stub sync**"]
    if auto_created:
        lines.append(f"  Created: {', '.join(auto_created)}")
    if auto_updated:
        lines.append(f"  Updated: {len(auto_updated)} stub{'s' if len(auto_updated) != 1 else ''}")
    if auto_deleted:
        lines.append(
            f"  Deleted: {len(auto_deleted)} stub{'s' if len(auto_deleted) != 1 else ''} (source cancelled)"
        )
    if proposals:
        lines.append(
            f"\n**Needs your review ({len(proposals)} event{'s' if len(proposals) != 1 else ''})**"
        )
        for p in proposals:
            lines.append(
                f"  `calendar stub {p['id']}` — {p['summary']} on {p['date']}"
                f" (→ {p['category']}?)"
            )
    if not any([auto_created, auto_updated, auto_deleted, proposals]):
        lines.append("  No changes needed.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manual stub / unstub
# ---------------------------------------------------------------------------


def cmd_stubs(args: list[str]) -> str:
    sub = args[0] if args else "list"

    if sub == "list" or not args:
        conn = _init_db()
        rows = conn.execute(
            """SELECT s.id, s.category, s.source_start, s.stub_event_id, s.source_event_id
               FROM calendar_stubs s ORDER BY s.source_start ASC"""
        ).fetchall()
        if not rows:
            return "No active stub mappings."
        lines = ["**Calendar stubs**\n"]
        for db_id, category, source_start, stub_id, source_id in rows:
            date_str = source_start[:10]
            lines.append(
                f"  #{db_id} [{category}] {date_str}  source={source_id[:8]}…  stub={stub_id[:8]}…"
            )
        return "\n".join(lines)

    elif sub == "stub":
        if len(args) < 2:
            return "Usage: stubs stub <source_event_id> [--category C]"
        if not HOME_CALENDAR_ID:
            return "Error: GOOGLE_HOME_CALENDAR_ID is not set in ~/.jarvis.env"
        source_id = args[1]
        category = None
        for i, a in enumerate(args[2:], 2):
            if a == "--category" and i + 1 < len(args):
                category = args[i + 1]

        service = get_service()
        try:
            event = service.events().get(calendarId="primary", eventId=source_id).execute()
        except Exception as exc:
            return f"Could not fetch event {source_id}: {exc}"

        if not category:
            conn = _init_db()
            providers = _load_providers(conn)
            cat, _ = _classify_event(event, providers)
            category = cat or "appointment"

        start = event["start"].get("dateTime", event["start"].get("date", ""))
        has_location = bool((event.get("location") or "").strip())
        stub_summary = f"AJ — {category.title()}" + (" (out)" if has_location else "")

        conn = _init_db()
        existing = conn.execute(
            "SELECT stub_event_id FROM calendar_stubs WHERE source_event_id = ?", (source_id,)
        ).fetchone()
        if existing:
            return f"Already stubbed (stub_id={existing[0][:12]}…). Use `stubs list` to see it."

        now_iso = datetime.now(UTC).isoformat()
        stub_event = (
            service.events()
            .insert(
                calendarId=HOME_CALENDAR_ID,
                body={
                    "summary": stub_summary,
                    "start": event["start"],
                    "end": event["end"],
                    "description": _stub_marker(source_id),
                },
            )
            .execute()
        )
        conn.execute(
            """INSERT INTO calendar_stubs
               (source_event_id, stub_event_id, source_calendar, home_calendar,
                category, source_start, created_at, updated_at)
               VALUES (?, ?, 'primary', ?, ?, ?, ?, ?)""",
            (source_id, stub_event["id"], HOME_CALENDAR_ID, category, start, now_iso, now_iso),
        )
        conn.commit()
        return f"Stub created: **{stub_summary}** on {start[:10]} → Home calendar."

    elif sub == "unstub":
        if len(args) < 2:
            return "Usage: stubs unstub <db_id>"
        try:
            db_id = int(args[1])
        except ValueError:
            return "db_id must be a number."

        conn = _init_db()
        row = conn.execute(
            "SELECT stub_event_id, source_event_id, category FROM calendar_stubs WHERE id = ?",
            (db_id,),
        ).fetchone()
        if not row:
            return f"No stub mapping #{db_id}."

        stub_event_id, source_id, category = row
        if HOME_CALENDAR_ID:
            try:
                service = get_service()
                stub_event = (
                    service.events()
                    .get(calendarId=HOME_CALENDAR_ID, eventId=stub_event_id)
                    .execute()
                )
                marker = _stub_marker(source_id)
                if marker in (stub_event.get("description") or ""):
                    service.events().delete(
                        calendarId=HOME_CALENDAR_ID, eventId=stub_event_id
                    ).execute()
                else:
                    return (
                        f"Stub #{db_id} does not carry the Jarvis marker — "
                        "refusing to delete an event not created by this skill."
                    )
            except Exception as exc:
                return f"Failed to delete Home calendar event: {exc}"

        conn.execute("DELETE FROM calendar_stubs WHERE id = ?", (db_id,))
        conn.commit()
        return f"Stub #{db_id} ({category}) removed from Home calendar and mapping table."

    else:
        return f"Unknown subcommand '{sub}'. Use: list, stub <event_id>, unstub <db_id>"


# ---------------------------------------------------------------------------
# Provider management
# ---------------------------------------------------------------------------


def cmd_providers(args: list[str]) -> str:
    conn = _init_db()
    sub = args[0] if args else "list"

    if sub == "list" or not args:
        rows = conn.execute(
            "SELECT id, name, domain, keyword, category FROM calendar_providers ORDER BY id"
        ).fetchall()
        if not rows:
            return (
                "No providers configured.\n"
                "Add one: `calendar providers add --name N --domain D --category C`\n"
                "  or:     `calendar providers add --name N --keyword K --category C`"
            )
        lines = ["**Calendar providers** (maps provider → stub category)\n"]
        for p_id, name, domain, keyword, category in rows:
            match = f"domain={domain}" if domain else f"keyword={keyword}"
            lines.append(f"  #{p_id} {name} ({match}) → {category}")
        return "\n".join(lines)

    elif sub == "add":
        params: dict[str, str] = {}
        i = 1
        while i < len(args):
            if args[i].startswith("--") and i + 1 < len(args):
                params[args[i][2:]] = args[i + 1]
                i += 2
            else:
                i += 1
        name = params.get("name", "").strip()
        domain = params.get("domain", "").strip().lower()
        keyword = params.get("keyword", "").strip().lower()
        category = params.get("category", "").strip()

        if not name or not category:
            return "Error: --name and --category are required"
        if not domain and not keyword:
            return "Error: at least one of --domain or --keyword is required"

        conn.execute(
            "INSERT INTO calendar_providers (name, domain, keyword, category, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, domain, keyword, category, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        match = f"domain={domain}" if domain else f"keyword={keyword}"
        return f"Provider added: **{name}** ({match}) → {category}"

    elif sub == "remove":
        if len(args) < 2:
            return "Usage: providers remove <id>"
        try:
            p_id = int(args[1])
        except ValueError:
            return "Provider ID must be a number."
        cur = conn.execute("DELETE FROM calendar_providers WHERE id = ?", (p_id,))
        conn.commit()
        return f"Provider #{p_id} removed." if cur.rowcount else f"No provider #{p_id}."

    else:
        return f"Unknown subcommand '{sub}'. Use: list, add, remove"


# ---------------------------------------------------------------------------
# Existing read/write commands (unchanged)
# ---------------------------------------------------------------------------


def cmd_today(service) -> str:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    t_min, _ = _day_bounds(today)
    _, t_max = _day_bounds(tomorrow)
    events = fetch_events(service, t_min, t_max)

    today_events = [
        e
        for e in events
        if e["start"].get("dateTime", e["start"].get("date", "")).startswith(str(today))
    ]
    tomorrow_events = [
        e
        for e in events
        if e["start"].get("dateTime", e["start"].get("date", "")).startswith(str(tomorrow))
    ]

    blocks = [
        format_day_block(today, today_events),
        format_day_block(tomorrow, tomorrow_events),
    ]
    return "\n\n".join(blocks)


def cmd_week(service) -> str:
    today = date.today()
    week_end = today + timedelta(days=6)
    t_min, _ = _day_bounds(today)
    _, t_max = _day_bounds(week_end)
    events = fetch_events(service, t_min, t_max)

    blocks = []
    for i in range(7):
        d = today + timedelta(days=i)
        day_events = [
            e
            for e in events
            if e["start"].get("dateTime", e["start"].get("date", "")).startswith(str(d))
        ]
        blocks.append(format_day_block(d, day_events))
    return "\n\n".join(blocks)


def cmd_check(service, date_str: str) -> str:
    """Return JSON list of events on a given date — used by Gmail classifier."""
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return json.dumps({"error": f"invalid date: {date_str}"})
    t_min, t_max = _day_bounds(d)
    events = fetch_events(service, t_min, t_max)
    result = [
        {
            "summary": e.get("summary", ""),
            "start": e["start"].get("dateTime", e["start"].get("date", "")),
            "location": e.get("location", ""),
            "calendar": e.get("_calendarId", ""),
        }
        for e in events
    ]
    return json.dumps(result, ensure_ascii=False)


def cmd_upcoming(service, days: int = 30) -> str:
    """Return JSON list of events in the next N days — used by Gmail classifier for context."""
    today = date.today()
    end = today + timedelta(days=days)
    t_min, _ = _day_bounds(today)
    _, t_max = _day_bounds(end)
    events = fetch_events(service, t_min, t_max)
    result = [
        {
            "summary": e.get("summary", ""),
            "start": e["start"].get("dateTime", e["start"].get("date", "")),
            "location": e.get("location", ""),
            "calendar": e.get("_calendarId", ""),
        }
        for e in events
    ]
    return json.dumps(result, ensure_ascii=False)


def cmd_add(
    service,
    title: str,
    date_str: str,
    time_str: str = "",
    duration_min: int = 60,
    calendar_id: str = "primary",
    recurrence: str = "",
) -> str:
    tz = _tz()
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return f"Invalid date: {date_str}"

    if time_str:
        try:
            h, m = map(int, time_str.split(":"))
        except ValueError:
            return f"Invalid time: {time_str} — use HH:MM"
        start_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
        end_dt = start_dt + timedelta(minutes=duration_min)
        body = {
            "summary": title,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        }
    else:
        next_day = d + timedelta(days=1)
        body = {
            "summary": title,
            "start": {"date": d.isoformat()},
            "end": {"date": next_day.isoformat()},
        }

    if recurrence:
        rule = recurrence if recurrence.startswith("RRULE:") else f"RRULE:{recurrence}"
        body["recurrence"] = [rule]

    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    start_label = event["start"].get("dateTime", event["start"].get("date", ""))
    recur_label = f" (recurring: {recurrence})" if recurrence else ""
    return f"Event created: **{title}** on {start_label}{recur_label}\n{event.get('htmlLink', '')}"


def cmd_calendars(service) -> str:
    """List all calendars — use this to find IDs for GOOGLE_CALENDAR_IDS."""
    result = service.calendarList().list().execute()
    lines = ["**Your calendars** (add IDs to GOOGLE_CALENDAR_IDS in ~/.jarvis.env)\n"]
    for cal in result.get("items", []):
        primary = " ← primary" if cal.get("primary") else ""
        lines.append(f"  • {cal['summary']}{primary}")
        lines.append(f"    ID: `{cal['id']}`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "today"

    if cmd in ("sync-stubs", "sync_stubs"):
        print(cmd_sync_stubs(sys.argv[2:]))
    elif cmd == "stubs":
        print(cmd_stubs(sys.argv[2:]))
    elif cmd == "providers":
        print(cmd_providers(sys.argv[2:]))
    else:
        service = get_service()
        if cmd == "today":
            print(cmd_today(service))
        elif cmd == "week":
            print(cmd_week(service))
        elif cmd == "check":
            date_arg = sys.argv[2] if len(sys.argv) > 2 else str(date.today())
            print(cmd_check(service, date_arg))
        elif cmd == "upcoming":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            print(cmd_upcoming(service, days))
        elif cmd == "add":
            if len(sys.argv) < 4:
                print(
                    "Usage: skill.py add <title> <YYYY-MM-DD> [HH:MM] [duration_min] [calendar_id] [RRULE]"
                )
                sys.exit(1)
            title = sys.argv[2]
            date_arg = sys.argv[3]
            time_arg = sys.argv[4] if len(sys.argv) > 4 else ""
            dur = int(sys.argv[5]) if len(sys.argv) > 5 else 60
            cal = sys.argv[6] if len(sys.argv) > 6 else "primary"
            rrule = sys.argv[7] if len(sys.argv) > 7 else ""
            print(cmd_add(service, title, date_arg, time_arg, dur, cal, rrule))
        elif cmd == "calendars":
            print(cmd_calendars(service))
        else:
            print(f"Unknown command: {cmd}")
            print(
                "Commands: today, week, check <date>, upcoming [days], "
                "add <title> <date> [time] [duration] [cal_id] [RRULE], calendars, "
                "sync-stubs, stubs [list|stub|unstub], providers [list|add|remove]"
            )
            sys.exit(1)
