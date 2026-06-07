# devqueue skill

Validates and transports dev-notes queue items to the `dev-queue` branch. Part of the Jarvis dev-loop framework (Phase 1).

## Commands

```
python3 skill.py validate <file>         Validate spec frontmatter; exit 0 = valid
python3 skill.py push <file>             Validate + commit + push to dev-queue:knowledge/dev-notes/queue/
python3 skill.py list [queue|backlog|archive]   List items from dev-queue branch
```

## Security model

`push` is hard-locked: it physically cannot push to any branch other than `dev-queue`, or to any path outside `knowledge/dev-notes/**`. The guard runs before git is invoked; violation aborts with an error.

## Spec schema (REFERENCE §8.1)

Required frontmatter fields: `id`, `title`, `status`, `scope`, `origin`, `author`, `created`

- `id` must match the filename stem (e.g. `2026-0001-my-feature` in `2026-0001-my-feature.md`)
- `status`: `proposed | authorized | building | built | merged`
- `scope`: `well-bounded-local | needs-design-pass`
- `origin`: `brainstorm | iteration-backlog`
- `created`: ISO date (`YYYY-MM-DD`)

## No venv required

Stdlib-only. Invoke directly with system `python3`.

## Env vars

None required. Uses git credentials already configured on the system.
