# garden skill

Care reminders, observation logging, and almanac queries for the Zone 4–5 Québec garden.
All data lives in `knowledge/garden/almanac.md` — no database.

## Commands

| Command | What it does |
|---------|-------------|
| `reminders [month]` | Care tasks for the given month (defaults to current month). Month can be a name, abbreviation, or number. |
| `log <zone> <note>` | Append a dated observation to a zone's almanac log. Writes directly to almanac.md. |
| `ask <question>` | Natural-language query answered by Sonnet using the almanac as context. |

**Valid zones for `log`:** `1`, `2`, `3` / `3a` / `3b` / `3c`, `4`, `5`, `6`, `7`, `d` (or `deck`)
Zones 3a/3b/3c share one almanac log (Zone 3 section).

## Env vars

| Var | Where | Notes |
|-----|-------|-------|
| `ANTHROPIC_API_KEY` | `~/.jarvis.env` | Required for `ask` only |

## Setup

```bash
cd skills/garden
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Test

```bash
# Reminders for current month
python skill.py reminders

# Reminders for a specific month
python skill.py reminders august

# Log an observation
python skill.py log 6 "Blackberries starting to bloom"

# Ask a question
python skill.py ask "When should I harvest the Anne raspberries?"
```
