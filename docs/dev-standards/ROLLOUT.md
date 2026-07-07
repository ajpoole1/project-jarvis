# ROLLOUT.md — Stand up a new repo on the dev-standards framework

This recipe is written for a cold reader: an agent or developer in a repo that has never seen
project-jarvis. Follow the steps in order. The acceptance test in Step 7 is the gate — the rollout
is not done until it passes.

The hub repo is `project-jarvis`. Its `docs/dev-standards/` directory is the shared standards
library. Until a standalone dev-standards repo exists (Phase D), consuming repos point at the
jarvis checkout directly.

---

## Step 1 — Fill the charter

Copy `docs/dev-standards/CHARTER_TEMPLATE.md` from the hub to the consuming repo's root as
`CHARTER.md`. Fill every placeholder value:

- `<REPO-NAME>` — the repo's name.
- `<canonical-check-command>` — the single command that runs linting + tests, used identically by
  agents and CI (defined in Step 3).
- `standards_root` — the absolute path to the hub's `docs/dev-standards/` directory on this
  machine (e.g. `/mnt/c/Users/aaron/Documents/python/project-jarvis/docs/dev-standards`). This
  stays absolute until Phase D; do not make it relative to the consuming repo.
- QA pairing, stack, goal, cascade — repo-specific values; fill from context.

Do not restate content from `DEV_BASE.md` — the charter points at it, never copies it.

## Step 2 — Write the root `CLAUDE.md`

Create a thin `CLAUDE.md` at the repo root. It is the interactive-author overlay only; the agent's
CLAUDE.md (if the repo has one) is separate. Minimum structure:

```markdown
# <Repo Name> — Author Overlay

**This is the at-desk authoring session.** <Who> driving via Claude Code.

---

## Contract

Inherit `<standards_root>/DEV_BASE.md` (§5 — Author role) + `CHARTER.md`.

**The verbs:**
- `/ship` — the only exit ramp: checks → `/qa` → commit+push → PR
- `/qa` — lens review before any PR (also runs inside `/ship`)
- `/handoff` — scaffold session entry in `CONTEXT.md`; run at every session end

---

## Safety essentials (non-negotiable, never skip)

- **Personal data / secrets never staged.** Before any commit: verify no sensitive files are staged.
- **Push is always a human action.** `/ship` opens the PR; merge is the operator's action.

---

## Doc map

| What you need | Where to look |
|---|---|
| Dev contract (all roles) | `<standards_root>/DEV_BASE.md` |
| QA lens definitions | `<standards_root>/QA_LENSES.md` |
| Python conventions | `<standards_root>/PY_STANDARDS.md` |
| <Repo-specific guide> | `<path>` |
| Session handoff log | `CONTEXT.md` |

---

## Session handoff

End every session with `/handoff`.
```

Replace `<standards_root>` with the absolute path from Step 1.

## Step 3 — Create the canonical check script

The check script is the single definition of green. It must be invoked identically by agents and
CI. Agents run it locally; CI runs the same file.

Minimum pattern:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "--- ruff check ---"
ruff check .

echo "--- ruff format ---"
ruff format --check .

echo "--- pytest ---"
python -m pytest

echo "--- all green ---"
```

Place it at a consistent path (e.g. `scripts/dev-loop/checks.sh`), make it executable
(`chmod +x`), and declare its path in the charter's `QA Parameters` table.

If the repo has no Python, replace the body with the appropriate linter + test runner.

## Step 4 — Vendor the verb skills

The three verb skills (`/ship`, `/qa`, `/handoff`) travel with the repo as vendored copies. They
are never installed at user scope (`~/.claude/skills/`) — that would leak them into other sessions.

Copy each skill from the hub:

```
hub: .claude/skills/{ship,qa,handoff}/SKILL.md
→ consuming repo: .claude/skills/{ship,qa,handoff}/SKILL.md
```

Add a provenance comment as the **first line** of each copied `SKILL.md`:

```
<!-- vendored-from: project-jarvis @ YYYY-MM-DD -->
```

Replace `YYYY-MM-DD` with today's date.

**Only adjust repo-specific tool paths** — for example, the check script path referenced in
`/ship`. Never change the procedure, output format, or constraint blocks. The vendored skill must
be byte-identical in behavior to the hub original; only data (paths, commands) differs.

When the hub updates a skill, re-vendor by copying again and updating the date in the provenance
comment.

## Step 5 — Grant Claude Code read access to the standards root

The standards root lives outside the consuming repo. Claude Code needs explicit permission to read
it.

**Recommended:** add to the consuming repo's `.claude/settings.json`:

```json
{
  "permissions": {
    "additionalDirectories": [
      "/mnt/c/Users/aaron/Documents/python/project-jarvis/docs/dev-standards"
    ]
  }
}
```

This avoids per-session prompts. The tradeoff: the path is machine-specific and hardcoded until
Phase D. Commit the settings file; each developer on a different machine updates the path locally
(or uses a gitignored override).

**Alternative:** accept the per-session prompt when Claude Code first attempts to read a
`@std/`-resolved path. This works but interrupts automated builder sessions.

## Step 6 — Declare lenses in the charter

In `CHARTER.md` → `## QA Lenses`, list every lens the repo needs:

```
## QA Lenses
standards_root: /absolute/path/to/project-jarvis/docs/dev-standards
- @std/PY_STANDARDS.md
- docs/guides/LOCAL_GUIDE.md
```

- Use `@std/` for every shared lens from the hub (resolved via `standards_root`).
- Use unprefixed repo-root-relative paths for repo-local guides.
- Every declared lens document must exist and must end with a `## Review Checklist` section.

**Lens contribution loop.** If a repo-local guide develops checklist lines that would apply to
other repos, propose them back to the hub: open a session in project-jarvis, present the candidate
lines with rationale, and the operator carries them into a jarvis session for review and merge.
This is lens accretion — the same pattern as Tom's external QA feedback loop.

## Step 7 — Acceptance test

Run `/qa` on any trivial diff (e.g. add a blank line to `CHARTER.md`, stage it):

```bash
git add CHARTER.md
```

Then invoke `/qa`. Observe:

1. The skill reads `CHARTER.md → ## QA Lenses`.
2. It resolves each `@std/` path against `standards_root`.
3. It reads the `## Review Checklist` tail of each resolved file.
4. The verdict JSON is emitted with no configuration defects.

**If `/qa` emits a blocking `convention` defect at `CHARTER.md`:** the resolution failed. Check:
- `standards_root` is set and the path exists on disk.
- Every `@std/` path resolves to an existing file with a `## Review Checklist` section.
- Claude Code has read access to the standards root (Step 5).

The rollout is not done until the acceptance test produces a clean resolution. A configuration
defect here is a hard stop — do not open a PR until it is resolved.

---

## Notes

**Phase D.** When the standalone dev-standards repo exists, migration is one line per consuming
repo: update `standards_root` to the new path. The `@std/` declarations, vendored skills, and
checklist tails are unchanged.

**Lens documents.** The current shared library: `PY_STANDARDS.md`. Forthcoming (blocked on
authoring sessions with the operator): `SQL_STANDARDS.md`, `AIRFLOW_STANDARDS.md`,
`API_CLIENT_STANDARDS.md`, `SCRAPING_STANDARDS.md`. Phase B rollouts are blocked on
`AIRFLOW_STANDARDS.md` and `API_CLIENT_STANDARDS.md` landing.

**Vendoring cadence.** Re-vendor skills when the hub ships a breaking procedure change. The
provenance date is the signal — if a consuming repo's date is behind a significant hub commit,
re-vendor. Patch-level changes (wording tweaks) do not require immediate re-vendoring.
