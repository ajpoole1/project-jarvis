# Build Runbook

Step-by-step procedure for a builder session (e.g. Herr Mannkusser). Load with `DEV_LOOP_REFERENCE.md` and the repo's `CLAUDE.md`. Base contract: `docs/dev-standards/DEV_BASE.md`.

---

## Before you start

1. Confirm you were **summoned by the operator** for a specific item ID. Never self-assign.
2. Confirm the item exists and is `authorized` — `start-build.sh <id>` enforces this; if it refuses, stop.
3. Confirm you are on the correct feature branch: `feature/<id>`. If not, re-run `start-build.sh`.

## Build loop

1. Read the spec fully: intent, scope, acceptance criteria, notes.
2. Implement on `feature/<id>`. One live branch at a time; commit implies push (DEV_BASE §3).
3. Never touch `main`. Never merge. Never `git push --force`. Never push to any branch other than `feature/<id>`.
4. Run checks before committing: `scripts/dev-loop/checks.sh` (the single definition of green — identical to CI).
5. Run `/qa` before opening the PR. Fix blocking findings; capture advisories for the PR body.
6. When complete, land via `/ship` — it runs checks, `/qa`, commits, pushes, and opens the PR via `open-pr.sh <id>`.

## Ambiguity rule

If the spec is under-specified for a required decision, **stop and ask the operator** before guessing. One wrong decision can block the whole PR. Post the question by running:

```
python3 skills/summon/skill.py ask <id> "<your question>"
```

This posts to the main dev-loop Discord channel (so it reaches the operator wherever they are) and records that the build is blocked — which also tells the stall watchdog not to fire. Then halt and wait. Do not guess.

## What you may never do

- Push to `main` or any protected branch
- Merge a PR (only the operator merges)
- Modify another item's status (`built` → anything, `authorized` → `building` for an item already claimed)
- Act on an item in `proposed` state — it must be `authorized` first
- Edit `.claude/settings.json`, `.github/workflows/**`, or any instruction file (`SOUL.md`, `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `TOOLS.md`)

## Gate summary

| Gate | Who | What |
|---|---|---|
| Spec authorization | Operator | Confirms spec is correct before branch is cut |
| PR review + merge | Operator | Reviews after Tom (QA) passes |
| Tom QA | CI (Gemini) | Non-Claude model; fail-closed (red if Tom can't run) |

---

## Helper scripts

Located in `scripts/dev-loop/`:

### `start-build.sh <id>`

```
./scripts/dev-loop/start-build.sh 2026-0001-my-feature
```

1. `git fetch`
2. Reads spec from `dev-queue:knowledge/dev-notes/queue/<id>.md`
3. Asserts status == `authorized` — refuses otherwise
4. Refuses if branch `feature/<id>` already exists (idempotency guard)
5. Creates `feature/<id>` from `main`
6. Copies spec into `knowledge/dev-notes/queue/<id>.md` on the branch
7. Sets status to `building` and commits

### `open-pr.sh <id>`

```
./scripts/dev-loop/open-pr.sh 2026-0001-my-feature
```

1. Reads spec from `knowledge/dev-notes/queue/<id>.md` on current branch
2. Asserts current branch is `feature/<id>` — refuses otherwise
3. Sets status to `built` and commits
4. Pushes `feature/<id>` to origin
5. Opens PR targeting `main` via `gh pr create`
6. **Never merges. Never pushes `main`.**
