# grocery

Conversational shopping list backed by SQLite. Driven by the Grocery Mode conversation layer in SOUL.md — the agent calls subcommands and relays stdout to Discord. No self-posting.

## Setup

```bash
cd skills/grocery
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Tables created (idempotent)

- `grocery_items` — active list (name, name_norm UNIQUE, qty, store, note, created_at)
- `grocery_archive` — trip history (trip_id, name, qty, store, shopped_at)
- `grocery_staples` — persistent recurring items (name, name_norm UNIQUE)

All three tables are created on first run via `CREATE TABLE IF NOT EXISTS`.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `JARVIS_DATA_DIR` | `/data` | Directory containing `jarvis.db` |

## Subcommands

**Adding and editing:**
```bash
python skill.py add "vinegar"
python skill.py add "chicken thighs" --qty 6 --store Costco
python skill.py add "milk"           # re-adding an existing item merges, does not duplicate
python skill.py rm "vinegar"
python skill.py set-qty "milk" 2
python skill.py set-store "milk" IGA
```

**Viewing:**
```bash
python skill.py list                 # flat bullet list, alphabetical
python skill.py list --by-store      # grouped under store headers; unassigned items last
python skill.py review               # full grouped list; ⚠️ flags missing store or qty
```

**Completing a shop:**
```bash
python skill.py shopped              # archives all active items under a new trip_id, clears list
```

**History and recurring items:**
```bash
python skill.py history              # last 5 trips
python skill.py history 10           # last 10 trips
python skill.py usuals               # top 10 items by trip frequency
python skill.py usuals --top 5
python skill.py staples list
python skill.py staples add "olive oil"
python skill.py staples rm "olive oil"
python skill.py seed                 # add all staples to active list (merge-safe)
```

**Recipe integration:**
```bash
python skill.py from-recipe "bone-broth-risoni"
python skill.py from-recipe "butter-macaroni"
```

Reads `## Ingredients` from `knowledge/recipes/<slug>.md`. Best-effort quantity parsing:
`"1½ cups risoni"` → name=`risoni`, qty=`1½ cups`. Items already on the list are skipped.

## Manual test walkthrough

```bash
source .venv/bin/activate

# Add items
python skill.py add "vinegar"
# ✓ vinegar — next?
python skill.py add "chicken thighs" --qty 6
# ✓ chicken thighs ×6 — next?
python skill.py add "milk" --qty 2 --store IGA
# ✓ milk ×2 (IGA) — next?
python skill.py add "paper towels" --store Costco

# Check list
python skill.py list
# • chicken thighs ×6
# • milk ×2 (IGA)
# • paper towels (Costco)
# • vinegar

python skill.py list --by-store
# **Costco**
#   • paper towels
# **IGA**
#   • milk ×2
# **Unassigned**
#   • chicken thighs ×6
#   • vinegar

# Assign missing stores
python skill.py set-store "vinegar" IGA
python skill.py set-store "chicken thighs" Costco

# Review before shopping
python skill.py review
# **Costco**
#   • chicken thighs ×6
#   • paper towels  ⚠️ no qty
# **IGA**
#   • milk ×2
#   • vinegar  ⚠️ no qty
# ⚠️ 2 item(s) need attention: paper towels, vinegar

# Fix missing qtys
python skill.py set-qty "paper towels" 1
python skill.py set-qty "vinegar" 1

# Archive after shopping
python skill.py shopped
# Archived 4 items, list cleared — fresh list ready.

# Check history
python skill.py history
# **2026-06-06** — 4 items
#   • chicken thighs ×6 (Costco)
#   • milk ×2 (IGA)
#   • paper towels ×1 (Costco)
#   • vinegar ×1 (IGA)

# Recipe integration
python skill.py from-recipe "bone-broth-risoni"
# Added 5 from bone-broth-risoni: risoni ×1½ cups, chicken bone broth ×3 cups, ...
```

## Notes

- Case-insensitive dedup: "Milk" and "milk" are the same item.
- `add` on an existing item merges the provided fields (qty/store/note) without duplicating.
- `shopped` archives to `grocery_archive` under a timestamped `trip_id` before clearing — data is never hard-deleted.
- `from-recipe` is best-effort; ingredient lines with unusual formats may not parse qty correctly.
- No Discord posting — the conversational agent relays stdout.
