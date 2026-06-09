# media

Watched-media store — track titles watched/tried by person, filter seen from candidate recommendation lists.

**Phase 1** (this skill): store + filter API + seed migration.
**Phase 2 follow-up**: wire `filter` into live recommendation surfaces (morning-briefing reads-coda, seasonal docs).

## What it does

- Persists watched/tried records per person in `media_log` table (`jarvis.db`)
- Normalizes titles for fuzzy matching: lowercase, strip leading article, strip punctuation
- Seeds historical entries from `knowledge/personal/summer-2026-lake.md` on first init (idempotent)

## Commands

| Command | Description |
|---|---|
| `add <person> <title> <verdict> [--kind K] [--note TEXT]` | Log a watched/tried title |
| `list [person]` | Show all entries, optionally filtered by person |
| `seen <title>` | Look up whether a title has been seen (normalized match) |
| `filter [--titles t1,t2,...]` | Split candidates into `fresh` vs `seen`; outputs JSON |

### Verdicts

`loved` · `ok` · `bounced` · `watching`

### Kinds

`tv` (default) · `movie` · `book` · `game`

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `JARVIS_DATA_DIR` | `/data` | Directory for `jarvis.db` |

## Testing

```bash
# Run with temp DB
JARVIS_DATA_DIR=/tmp/test-media python3 skills/media/skill.py list
JARVIS_DATA_DIR=/tmp/test-media python3 skills/media/skill.py seen "the summer i turned pretty"
JARVIS_DATA_DIR=/tmp/test-media python3 skills/media/skill.py filter --titles "Severance,Normal People,Slow Horses"
```
