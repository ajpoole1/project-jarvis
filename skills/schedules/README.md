# schedules skill

Agent-safe scheduled automation for Jarvis. Jarvis proposes jobs; AJ approves via Discord; a fixed heartbeat dispatcher executes them.

## Design contract

- **Jarvis never writes crontab.** This skill manages a `schedules` table in `jarvis.db`. A fixed, AJ-authored cron calls `dispatch` every 10 minutes.
- **Approved-before-fire.** Every job starts `approved=0`. Nothing runs until AJ reacts ✅.
- **Argv only.** The dispatcher calls `python3 skills/<skill>/skill.py <args>` — no `shell=True`, no string concatenation, no metacharacters.
- **Skill allowlist = `skills/` directory.** Any `skills/<name>/skill.py` that exists is schedulable. Skills are read-only to the agent, so Jarvis cannot expand the allowlist.

## Supported schedule formats

| Format | Meaning |
|---|---|
| `30m` | every 30 minutes |
| `2h` | every 2 hours |
| `7d` | every 7 days |
| `daily@09:00` | once per day at 09:00 local (America/Toronto) |
| `weekly@mon@09:00` | once per week on Monday at 09:00 local |

## Commands

```bash
# Propose a new schedule (approved=false until AJ approves)
python3 skills/schedules/skill.py propose \
    --skill price-monitor \
    --schedule daily@09:00 \
    --description "Daily price check for tracked items" \
    --args '["check", "--quiet"]'

# Approve / reject
python3 skills/schedules/skill.py approve 1
python3 skills/schedules/skill.py reject 1

# List active schedules
python3 skills/schedules/skill.py list
python3 skills/schedules/skill.py list --all

# Control
python3 skills/schedules/skill.py enable 1
python3 skills/schedules/skill.py disable 1
python3 skills/schedules/skill.py delete 1

# Dispatcher — called by cron_followups.sh, not manually
python3 skills/schedules/skill.py dispatch
```

## Discord UX (propose → approve flow)

When Jarvis proposes a schedule it outputs JSON. The agent formats it as:

```
⏰ Schedule this?
  skill: price-monitor
  schedule: daily@09:00 (next: 2026-06-05 09:00)
  "Daily price check for tracked items"
✅ approve · ❌ reject
```

React ✅ → `approve <id>`. React ❌ → `reject <id>`.

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `JARVIS_DATA_DIR` | `/data` | Directory for `jarvis.db` |

## Logs

Dispatch activity logs to stderr → captured by `logs/cron.log`. No PII; only `job_id`, `skill_name`, and exit status are logged.
