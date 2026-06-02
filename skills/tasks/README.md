# tasks

Deadline tracking against the shared `jarvis.db`. Surfaces upcoming deadlines in the morning briefing automatically.

## What it does

- Stores deadlines with a due date, project tag, and free-text notes
- Exposes `upcoming [days]` as JSON for the morning briefing to consume
- Marks deadlines complete when submitted/done
- The morning briefing queries the `deadlines` table directly — no subprocess needed

## Commands

| Command | Description |
|---|---|
| `python skill.py add "title" YYYY-MM-DD [project] [notes]` | Add a deadline |
| `python skill.py list [project] [days]` | List pending deadlines, optionally filtered by project or days-out |
| `python skill.py complete <id>` | Mark a deadline complete |
| `python skill.py upcoming [days]` | JSON list of deadlines due within N days (default: 14) — used by morning briefing |

### Examples

```bash
# Add a school assignment gate
python skill.py add "COMP 378 Assignment 1" 2026-06-30 school "due after Unit 3"

# List all pending school deadlines
python skill.py list school

# List everything due within 30 days
python skill.py list "" 30

# Mark id=2 complete after submitting
python skill.py complete 2

# JSON output for programmatic use
python skill.py upcoming 14
```

## SQLite schema

Table: `deadlines` in `JARVIS_DATA_DIR/jarvis.db`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment — use in `complete` |
| `title` | TEXT | What the deadline is |
| `due_date` | TEXT | ISO date YYYY-MM-DD |
| `project` | TEXT | Tag for filtering (school, altaforma, jarvis, etc.) |
| `notes` | TEXT | Context notes — what this gates, what to watch for |
| `completed` | INTEGER | 0 = pending, 1 = done |
| `created_at` | TEXT | ISO datetime |

The table is created by `gmail-cleanup`'s `init_db()` so all skills share the same DB without circular dependencies.

## Morning briefing integration

The briefing queries this table directly (no subprocess) and includes an `UPCOMING DEADLINES` section in the Sonnet prompt when any deadline falls within 14 days. Overdue items surface as `OVERDUE by Nd`.

This is what breaks the last-minute-cram pattern — Jarvis surfaces upcoming gates daily without being asked.

## Setup

```bash
cd skills/tasks
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Requires `JARVIS_DATA_DIR/jarvis.db` to exist (created on first `gmail-cleanup` run). No additional env vars or credentials needed.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `JARVIS_DATA_DIR` | `/data` | Path to SQLite DB directory |
