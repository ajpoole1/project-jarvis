# morning-briefing

Daily 7am Discord briefing. Structured, salience-ranked brief with a multi-source news radar and a semantic reads coda. Posts three messages to Discord. Fired by the schedules dispatcher.

## What it does

**msg1 — Morning brief** (Sonnet-composed, salience-ranked)
1. Fetches weather from `wttr.in` for `JARVIS_CITY`
2. Fetches today + tomorrow's calendar via the `calendar` skill (`upcoming 1`)
3. Queries `deadlines` table — surfaces incomplete deadlines due within 14 days
4. Runs `gmail heartbeat` to pick up IMMEDIATE-priority emails (no inbox count in the brief)
5. Feeds all blocks into one Sonnet call; brief opens with a single lead line naming the most important thing, then a salience-ranked body

**msg2 — News radar** (Haiku-clustered, 0–1 stories)
- Pulls top headlines from 5 configured sources (CBC, BBC, Al Jazeera, The Guardian, Reuters)
- Haiku clusters stories by cross-source corroboration — importance rises with how many independent outlets cover the same event
- Surfaces 0–1 stories with a so-what / personal-impact bridge
- On a quiet day, shows its work ("nothing consequential — say 'news' for headlines")

**msg3 — Reads coda** (0–2 picks, semantic matching)
- Interest profile loaded from `knowledge/preferences/interests.md` (committed, non-sensitive)
- Google News RSS used as a rough prefilter per interest topic
- Haiku scores all candidates (0–10) against the full profile + both cross-cutting filters (tonal: grounded/applied; practitioner: applied over academic)
- Sonnet picks top 1–2 globally (no per-interest quota) and writes a one-line "why you'd care" per pick
- Deduped across 7 days via `briefing_reads_dedup` SQLite table
- Zero picks is allowed — nothing surfaced beats a forced pick

## Email handling

The inbox status block is removed. Routine triage is on-demand (`gmail stage`). IMMEDIATE-priority exceptions surface in the brief body; all other queue items flow to the 13:00 and 18:00 digest runs as normal.

## Scheduling

Fired daily at 07:00 via the `schedules` dispatcher. Posts directly to Discord via webhook. Owns the 7am digest slot — do not run `gmail digest` separately in the morning.

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
| `BRIEFING_NEWS_SOURCES` | No | JSON array of `[["Name","rss_url"],...]` to override default news sources |

## Interest profile

The reads engine reads from `knowledge/preferences/interests.md`. The file is committed (non-sensitive). Edit it to add, remove, or reshape interests — the engine picks up changes automatically on the next run. Never hardcode interest topics in the skill.

## Dependencies

- `skills/calendar/.venv` must exist and be installed
- `skills/gmail-cleanup/.venv` must exist and be installed
- `JARVIS_DATA_DIR/jarvis.db` must exist (created on first gmail skill run)
- `deadlines` table must exist — created by the `tasks` skill

## Testing

```bash
source .venv/bin/activate
python skill.py
```

Output is the formatted briefing — paste into Discord to verify formatting, or run via the cron script:

```bash
scripts/cron_briefing.sh
```

## Notes

- All data sources are independent — any single source failure is caught and skipped.
- Reads dedup table (`briefing_reads_dedup`) is auto-created on first run; pruned to 14-day window.
- `BRIEFING_NEWS_SOURCES` env var overrides the default 5-source list for the news radar.
- News radar and reads coda are separate Discord messages so Discord can render links cleanly.
- The briefing blocks carry `type / salience / take / detail / action` fields — ready for a future app tile render without code changes.
