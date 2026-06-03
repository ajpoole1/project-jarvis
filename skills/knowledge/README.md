# knowledge skill

Full-text search and structured updates for `knowledge/**/*.md` files.

## Commands

### `search <query>`
FTS5 full-text search across all Markdown files. Returns ranked JSON results with file, section (nearest `##` heading), and a snippet.

```
python3 skills/knowledge/skill.py search "Ellie texture rules"
python3 skills/knowledge/skill.py search "raspberry pruning"
```

FTS5 query syntax: bare words match anywhere; `"quoted phrase"` for phrase match; `word*` for prefix.

### `update <file> <field> <value>`
Set a single YAML frontmatter field. Preserves original formatting — only the target field's value is changed.

```
python3 skills/knowledge/skill.py update family/recipes/butter-macaroni.md last_made 2026-06-01
python3 skills/knowledge/skill.py update family/recipes/butter-macaroni.md rating 5
python3 skills/knowledge/skill.py update family/recipes/butter-macaroni.md ellie.approved true
```

`<file>` is relative to `knowledge/`. Supports top-level keys and one-level dotted keys (e.g. `ellie.approved`).

### `append <file> <section> <text>`
Append a dated bullet (`- YYYY-MM-DD <text>`) under the named `## section`. Creates the file or section if it doesn't exist.

```
python3 skills/knowledge/skill.py append garden/almanac.md "Observations" "Zone 2 raspberry canes showing new growth"
python3 skills/knowledge/skill.py append people/polina.md "Preferences" "Loved the ramen place on St-Laurent"
```

## Safety

- All write paths canonicalised with `os.path.realpath()` — path traversal blocked
- Writes restricted to `*.md` files inside `knowledge/`
- Atomic writes (temp file + `os.replace`)
- No delete command; no full-file overwrite
- All writes produce a git diff that AJ can review and revert

## Setup

No virtualenv needed — stdlib only.

```bash
python3 skills/knowledge/skill.py search "test"
```

## Env vars

None required.
