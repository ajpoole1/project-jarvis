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
| `once@2026-06-10T14:30` | fire once at 14:30 local on 2026-06-10, then retire |

**`once@` note:** After a one-off fires the row is kept in the DB with `enabled=0` for
audit but will never re-fire. Use `list --all` to see retired one-offs.

> **Shell metacharacter constraint for `once@` reminders:** The `_validate_args()`
> check rejects args containing `& | > < \ $( ${`. Reminder messages must avoid
> these — use textual alternatives (e.g. `and` instead of `&`, `greater than`
> instead of `>`). See `skills/reminder/README.md` for the full substitution table.

## Commands

```bash
# Propose a recurring schedule (approved=false until AJ approves)
python3 skills/schedules/skill.py propose \
    --skill price-monitor \
    --schedule daily@09:00 \
    --description "Daily price check for tracked items" \
    --args '["check", "--quiet"]'

# Propose a one-off reminder (fires once at fixed local time, then retires)
python3 skills/schedules/skill.py propose \
    --skill reminder \
    --schedule once@2026-06-10T14:30 \
    --description "Reminder: dentist appointment" \
    --args '["--message","dentist appointment at 3pm"]'

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
