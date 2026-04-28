"""Google Calendar skill — query and create events across configured calendars."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv(Path(__file__).parents[2] / ".env")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

CONFIG_DIR = Path(os.environ.get("JARVIS_CONFIG_DIR", "/config/personal"))
CREDENTIALS_PATH = CONFIG_DIR / "gmail_credentials.json"
TOKEN_PATH = CONFIG_DIR / "calendar_token.json"

TIMEZONE = os.environ.get("JARVIS_TIMEZONE", "America/Toronto")
# Comma-separated calendar IDs; "primary" always included
_raw_ids = os.environ.get("GOOGLE_CALENDAR_IDS", "primary")
CALENDAR_IDS = [c.strip() for c in _raw_ids.split(",") if c.strip()]


def get_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
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
        result = (
            service.events()
            .list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )
        for e in result.get("items", []):
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


def cmd_today(service) -> str:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    t_min, _ = _day_bounds(today)
    _, t_max = _day_bounds(tomorrow)
    events = fetch_events(service, t_min, t_max)

    today_events = [e for e in events if e["start"].get("dateTime", e["start"].get("date", "")).startswith(str(today))]
    tomorrow_events = [e for e in events if e["start"].get("dateTime", e["start"].get("date", "")).startswith(str(tomorrow))]

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
            e for e in events
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


def cmd_add(service, title: str, date_str: str, time_str: str = "", duration_min: int = 60, calendar_id: str = "primary") -> str:
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

    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    start_label = event["start"].get("dateTime", event["start"].get("date", ""))
    return f"Event created: **{title}** on {start_label}\n{event.get('htmlLink', '')}"


def cmd_calendars(service) -> str:
    """List all calendars — use this to find IDs for GOOGLE_CALENDAR_IDS."""
    result = service.calendarList().list().execute()
    lines = ["**Your calendars** (add IDs to GOOGLE_CALENDAR_IDS in .env)\n"]
    for cal in result.get("items", []):
        primary = " ← primary" if cal.get("primary") else ""
        lines.append(f"  • {cal['summary']}{primary}")
        lines.append(f"    ID: `{cal['id']}`")
    return "\n".join(lines)


if __name__ == "__main__":
    service = get_service()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "today"

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
            print("Usage: skill.py add <title> <YYYY-MM-DD> [HH:MM] [duration_min] [calendar_id]")
            sys.exit(1)
        title = sys.argv[2]
        date_arg = sys.argv[3]
        time_arg = sys.argv[4] if len(sys.argv) > 4 else ""
        dur = int(sys.argv[5]) if len(sys.argv) > 5 else 60
        cal = sys.argv[6] if len(sys.argv) > 6 else "primary"
        print(cmd_add(service, title, date_arg, time_arg, dur, cal))
    elif cmd == "calendars":
        print(cmd_calendars(service))
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: today, week, check <date>, upcoming [days], add <title> <date> [time] [duration] [cal_id], calendars")
        sys.exit(1)
