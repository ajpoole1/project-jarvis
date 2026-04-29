# morning-briefing

Daily 7am Discord briefing. Single Sonnet call covering today's calendar events, overnight priority inbox items, and a summary of emails queued for cleanup. Clears the Gmail digest queue on each run (owns the morning slot).

## What it does

1. Fetches today's calendar events via the `calendar` skill
2. Runs `gmail heartbeat` to pick up overnight priority emails and advance `last_checked`
3. Reads and clears the Gmail digest queue from SQLite
4. Feeds all data into one Sonnet call and posts the formatted result to Discord

## Setup

```bash
cd skills/morning-briefing
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Environment variables

Loaded from `.env` at project root via python-dotenv.

| Var | Required | Description |
|-----|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key |
| `JARVIS_DATA_DIR` | No | Path to SQLite DB directory (default: `/data`) |

## Dependencies

- `skills/calendar/.venv` must exist and be installed
- `skills/gmail-cleanup/.venv` must exist and be installed
- `JARVIS_DATA_DIR/jarvis.db` must exist (created on first gmail skill run)

## Testing

```bash
source .venv/bin/activate
python skill.py
```

Output is the formatted briefing string — paste into Discord to verify formatting.
