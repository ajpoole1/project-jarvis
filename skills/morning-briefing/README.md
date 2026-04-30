# morning-briefing

Daily 7am Discord briefing. Single Sonnet call covering weather, today's calendar, overnight priority inbox, and personalized news. Fired by cron — always posts verbatim to Discord.

## What it does

1. Fetches weather from `wttr.in` for `JARVIS_CITY`
2. Fetches today's calendar events via the `calendar` skill
3. Runs `gmail heartbeat` to pick up overnight priority emails and advance `last_checked`
4. Reads and clears the Gmail digest queue from SQLite (briefing owns the 7am slot)
5. Fetches BBC World News RSS — top 5 headlines
6. Fetches Google News RSS per interest from `config/personal/briefing_interests.json`
7. Feeds all data into one Sonnet call and posts the result verbatim to Discord

## Scheduling

Fired by cron at 7:00am daily via `scripts/cron_briefing.sh`. Posts directly to Discord via webhook — does not go through OpenClaw. The morning briefing owns the 7am digest slot; do not run `gmail digest` separately in the morning.

## Setup

```bash
cd skills/morning-briefing
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Environment variables

| Var | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key |
| `DISCORD_WEBHOOK_URL` | Yes | Webhook URL for Discord posting |
| `JARVIS_CITY` | No | City for weather lookup (default: `Montreal`) |
| `JARVIS_DATA_DIR` | No | Path to SQLite DB directory (default: `/data`) |
| `JARVIS_CONFIG_DIR` | No | Path to personal config directory (default: `/config/personal`) |

## Interests config

Personalized article topics are loaded from `JARVIS_CONFIG_DIR/briefing_interests.json` (gitignored). Falls back to `config/examples/briefing_interests.json` if absent.

```bash
cp config/examples/briefing_interests.json config/personal/briefing_interests.json
# Edit with your own topics
```

## Dependencies

- `skills/calendar/.venv` must exist and be installed
- `skills/gmail-cleanup/.venv` must exist and be installed
- `JARVIS_DATA_DIR/jarvis.db` must exist (created on first gmail skill run)

## Testing

```bash
source .venv/bin/activate
python skill.py
```

Output is the formatted briefing — paste into Discord to verify formatting, or run via the cron script to test the full webhook path:

```bash
scripts/cron_briefing.sh
```

## Notes

- All data sources are independent — any single source failure is caught and skipped.
- Weather silently drops if `wttr.in` is unreachable.
- Google News RSS links are Google redirect URLs (unresolvable without browser cookies). Links are formatted as `([Publisher Name](url))` Discord markdown using `entry.source.title` from feedparser.
- `DISCORD_WEBHOOK_URL` requires `User-Agent: DiscordBot (...)` header — Cloudflare blocks the default Python urllib agent.
