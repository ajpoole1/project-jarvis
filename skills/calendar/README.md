# calendar

Reads and writes Google Calendar events across configured calendars. Used standalone from Discord and as a context source for the Gmail classifier.

## What it does

- Queries one or more Google Calendars (personal, shared/family)
- Returns formatted event lists for Discord (today, this week)
- Returns JSON event lists for machine consumption (Gmail classifier, morning briefing)
- Creates new events with optional time and duration

## Commands

| Command | Description |
|---|---|
| `python skill.py` / `python skill.py today` | Events today and tomorrow, Discord-formatted |
| `python skill.py week` | Events for the next 7 days |
| `python skill.py check YYYY-MM-DD` | Events on a specific date, returns JSON |
| `python skill.py upcoming [days]` | Events in the next N days as JSON (default: 30) |
| `python skill.py add "title" YYYY-MM-DD [HH:MM] [duration_min] [calendar_id]` | Create an event |
| `python skill.py calendars` | List all calendars with their IDs (setup helper) |

## Setup

### 1. Google Cloud credentials

Reuses the same OAuth client as `gmail-cleanup`. No new GCP project needed.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) on the existing project
2. Enable the **Google Calendar API**
3. The existing `gmail_credentials.json` in `JARVIS_CONFIG_DIR` is reused

### 2. Environment variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_CALENDAR_IDS` | `primary` | Comma-separated list of calendar IDs to query |
| `JARVIS_TIMEZONE` | `America/Toronto` | IANA timezone for event display and creation |
| `JARVIS_CONFIG_DIR` | `/config/personal` | Path to credentials directory |

To find your calendar IDs, run `python skill.py calendars` after the first auth flow.

### 3. Virtualenv and first run

```bash
cd skills/calendar
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
python skill.py calendars
```

The first run opens a browser for Calendar OAuth consent. Token saved to `JARVIS_CONFIG_DIR/calendar_token.json` — gitignored. This is a separate token from Gmail with its own scope.

## OAuth scope

`https://www.googleapis.com/auth/calendar` — full read/write access. Read-only scope is sufficient if you never use `add`.

## Integration with gmail-cleanup

The Gmail classifier calls `python skill.py upcoming 30` via subprocess and injects the result into the Haiku prompt. This allows appointment-related emails (confirmations, reminders) to be archived automatically when the appointment is already on the calendar.

No direct import — each skill has its own virtualenv. Communication is via subprocess JSON.
