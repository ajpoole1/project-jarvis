# knowledge skill

Full-text search, structured updates, and staged memory captures for `knowledge/**/*.md` files.

## Commands

### `search <query>`
FTS5 full-text search across all Markdown files. Returns ranked JSON results with file, section (nearest `##` heading), and a snippet.

```bash
python3 skills/knowledge/skill.py search "Ellie texture rules"
python3 skills/knowledge/skill.py search "raspberry pruning"
```

FTS5 query syntax: bare words match anywhere; `"quoted phrase"` for phrase match; `word*` for prefix.

### `update <file> <field> <value>`
Set a single YAML frontmatter field. Preserves original formatting — only the target field's value is changed.

```bash
python3 skills/knowledge/skill.py update family/recipes/butter-macaroni.md last_made 2026-06-01
python3 skills/knowledge/skill.py update family/recipes/butter-macaroni.md rating 5
python3 skills/knowledge/skill.py update family/recipes/butter-macaroni.md ellie.approved true
```

`<file>` is relative to `knowledge/`. Supports top-level keys and one-level dotted keys (e.g. `ellie.approved`).

### `append <file> <section> <text>`
Append a dated bullet (`- YYYY-MM-DD <text>`) under the named `## section`. Creates the file or section if it doesn't exist.

```bash
python3 skills/knowledge/skill.py append garden/almanac.md "Observations" "Zone 2 raspberry canes showing new growth"
python3 skills/knowledge/skill.py append people/polina.md "Preferences" "Loved the ramen place on St-Laurent"
```

---

## Capture Staging Commands

Memory facts are staged for approval before writing, enforcing a stage-then-approve workflow.

### `stage <file> <op> <target> <text> [--source explicit|implicit] [--batch <batch_id>]`

Stage a memory capture for approval. Returns id and batch_id to group multi-fact proposals.

**Operations:**
- `append`: add a dated line under a section
- `update`: modify a frontmatter field
- `create`: create a new file

**Source:** `explicit` (user said "remember...") or `implicit` (agent detected high-signal fact)

**Batch:** optional batch_id to group multiple facts from one conversation turn. First call auto-generates a batch_id; pass `--batch <id>` on subsequent calls to group them.

```bash
python3 skills/knowledge/skill.py stage people/polina.md append "Preferences" "Loves the ramen place on St-Laurent"
python3 skills/knowledge/skill.py stage preferences/media.md update "favorite_podcast" "Stuff You Should Know" --source implicit --batch abc123
```

Returns JSON with id, batch_id, file, op, target, proposed_text.

### `pending [n]`

List pending staged captures (default all, or last N). Shows status, file, operation, and prior values for updates.

```bash
python3 skills/knowledge/skill.py pending
python3 skills/knowledge/skill.py pending 5
```

### `commit <id|batch_id>`

Approve and write one or more staged captures. Validates paths, executes via existing `append`/`update` operations, auto-commits private-tier files to local git.

```bash
python3 skills/knowledge/skill.py commit 1
python3 skills/knowledge/skill.py commit abc123
```

Returns JSON with id, file, status, op. On private-tier writes, includes auto-commit result.

### `reject <id|batch_id>`

Discard one or more staged captures without writing.

```bash
python3 skills/knowledge/skill.py reject 1
python3 skills/knowledge/skill.py reject abc123
```

### `undo [n]`

Revert the last N committed captures by restoring prior values. For private-tier files, also commits the revert to local git.

```bash
python3 skills/knowledge/skill.py undo
python3 skills/knowledge/skill.py undo 3
```

### `capture on|off`

Toggle implicit capture offers. When off, the agent will not propose capturing facts automatically — only explicit "remember..." triggers are honored.

```bash
python3 skills/knowledge/skill.py capture off
python3 skills/knowledge/skill.py capture on
```

---

## Safety

- All write paths canonicalised with `os.path.realpath()` — path traversal blocked
- Writes restricted to `*.md` files inside `knowledge/`
- Atomic writes (temp file + `os.replace`)
- Tier routing enforced via `TIERS.md` — private files go to private root, committed files to public root
- Graceful degradation: if private root unavailable at commit time, leaves capture pending with a note instead of erroring
- No delete command; no full-file overwrite

## Setup

No virtualenv needed — stdlib only.

```bash
python3 skills/knowledge/skill.py search "test"
```

## Env vars

- `JARVIS_DATA_DIR` — where `jarvis.db` lives (default: `/data`)
