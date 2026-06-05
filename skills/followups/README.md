# followups

Jarvis-owned, time-conditioned intentions. The active layer beside passive knowledge — watches that initiate outreach at the right moment, calibrated per subject, silenced by the anti-nag engine most of the time.

Distinct from `deadlines` (AJ's obligations in the morning briefing). These are Jarvis's own intentions to raise something. They live in the `follow_ups` table in `jarvis.db`.

## What it does

- Stores "check in about X on Y" watches with policy, trigger time, and optional expiry
- Evaluates four gates before any proactive Discord message fires (quiet hours, channel-quiet, daily budget, spacing)
- Re-arms `periodic`/`persistent` watches automatically after each surface
- Lapses `check_once` watches whose expiry window passes without contact (`reap`)
- On resolution: emits a capture proposal when a `knowledge_target` is set, routing to Brief 3's stage-then-approve path

## Policies

| Policy | Behavior | Default cadence |
|---|---|---|
| `check_once` | Fires once, then lapses if not resolved within `window_until` (default +2d) | — |
| `periodic` | Re-arms on `cadence` after each surface | 7d |
| `persistent` | Re-arms tighter; AJ must opt in deliberately | 3d |
| `passive` | Never auto-fires; surfaces only via `list` | — |

**Ambiguous requests default to `passive`.** Too quiet is the safe failure mode.

## Anti-nag gates (all must pass for `fire` to speak)

1. **Quiet hours** — 23:00–09:00 local time (configurable via `FOLLOW_UP_QUIET_START` / `FOLLOW_UP_QUIET_END`)
2. **Channel quiet** — AJ has not sent a message in the last 15 min (configurable via `FOLLOW_UP_CHANNEL_QUIET_MIN`)
3. **Daily budget** — max 2 proactive outreaches per calendar day (configurable via `FOLLOW_UP_DAILY_BUDGET`)
4. **Spacing** — at least 30 min since last fire (configurable via `FOLLOW_UP_SPACING_MIN`)

Overflow queues silently — items stay `pending` and surface on future ticks. `check_once` items with a passed `window_until` lapse via `reap`.

## Setup

No virtualenv needed — stdlib only (`sqlite3`, `zoneinfo`, `datetime`, `json`). Python 3.9+ required for `zoneinfo`.

```bash
python3 skills/followups/skill.py list
```

## Scheduling

Add to crontab (`crontab -e`):
```
*/10 9-22 * * * /mnt/c/Users/aaron/Documents/python/project-jarvis/scripts/cron_followups.sh >> /mnt/c/Users/aaron/Documents/python/project-jarvis/logs/cron.log 2>&1
```

## Environment variables

| Var | Default | Description |
|---|---|---|
| `JARVIS_DATA_DIR` | `/data` | Directory containing `jarvis.db` |
| `FOLLOW_UP_QUIET_START` | `23` | Local hour to begin quiet window |
| `FOLLOW_UP_QUIET_END` | `9` | Local hour to end quiet window |
| `FOLLOW_UP_CHANNEL_QUIET_MIN` | `15` | Minutes since last inbound before channel is considered quiet |
| `FOLLOW_UP_SPACING_MIN` | `30` | Minimum minutes between proactive outreaches |
| `FOLLOW_UP_DAILY_BUDGET` | `2` | Max proactive outreaches per calendar day |

## Commands

```
skill.py add --subject S --prompt P --policy POLICY --trigger-at T
             [--window-until W] [--cadence DAYS] [--priority N]
             [--knowledge-target FILE] [--source explicit|open_loop]
skill.py list [status]
skill.py due
skill.py surface <id>
skill.py resolve <id> <resolution text>
skill.py dismiss <id>
skill.py snooze <id> <until YYYY-MM-DD[THH:MM]>
skill.py reap
skill.py ping
skill.py fire
```

## Testing

```bash
# Smoke test add + list
python3 skills/followups/skill.py add \
  --subject "Test" --prompt "Test prompt" \
  --policy check_once --trigger-at 2099-01-01

python3 skills/followups/skill.py list

# Verify fire stays silent (quiet hours / budget / etc.)
python3 skills/followups/skill.py fire  # no output expected unless gates pass

# Manual resolution
python3 skills/followups/skill.py resolve 1 "test resolved"
```

## Acceptance checks (Brief 4)

- [ ] "check in tomorrow how the soup went" → `check_once`, `window_until` ~+2d, confirm shown
- [ ] Next day, channel quiet + budget available → fire produces soup prompt, once
- [ ] AJ mid-conversation → fire produces no output (channel-quiet gate blocks)
- [ ] 3+ eligible items, 2/day budget → exactly 2 surface over the day; rest queue
- [ ] Busy stretch → `check_once` items past `window_until` lapse via `reap`; `periodic`/`persistent` persist
- [ ] "keep me on track with X" → `periodic`; ambiguous request → `passive`
- [ ] `resolve` with `knowledge_target` → `capture_proposal` in output with stage hint
- [ ] `list` shows tracked items; `dismiss` and `snooze` work
- [ ] No follow-up content ever injected into AJ-initiated threads or morning briefing
