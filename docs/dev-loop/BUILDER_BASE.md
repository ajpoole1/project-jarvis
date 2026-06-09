# Builder Base — Extractable to `dev-standards`

<!-- BASE: This entire file is repo-agnostic. When Phase 1 (multi-repo hub) is ready,
     `git mv docs/dev-loop/BUILDER_BASE.md` to the dev-standards repo. Adjust the two
     repo-local path references (the runbook pointer in §Build Protocol and the QA
     entry point in §QA Contract) to their new home. Everything else moves as-is. -->

The shared contract that governs every named builder in this loop, regardless of which
repo it builds. Overlays (per-persona) and charters (per-project) add their own layer;
they inherit this base and never restate it.

**To add a builder:** write an overlay + a project charter; inherit this base.
See `knowledge/dev-crew/` for the overlay schema and `CHARTER.md` for the charter
template. One overlay + one charter = a fully wired builder. The base never changes
per-repo.

---

## Invariants — non-negotiable

1. **Two human gates.** The operator authorizes the spec (gate 1) and reviews + merges
   the PR (gate 2). Neither is a rubber stamp; they are the outside checks on the loop.
2. **No auto-merge.** Merge is the only action that touches `main`, and only the
   operator initiates it. A builder never merges.
3. **No orchestrator-autonomous builder spawn.** Summoning requires an explicit operator
   command; it is never triggered automatically on spec authorization.
4. **Heterogeneous QA.** The QA reviewer (Tom) must be a different model family from
   the builder. Swapping Tom to the same family defeats the purpose — shared blind spots
   are the failure mode the design avoids.
5. **Authorize and summon are separate actions.** Authorize = "the spec is correct."
   Summon = "build it now." Many items may be authorized while few are summoned.

## Guiding principles

- **Stage then approve** — actions are staged for human review before they take effect.
- **Graceful degradation** — failures are loud, never silent; the system never hangs.
- **Single surface** — interactions flow through one coherent channel per project.
- **Security by design** — permission bounds are structural, not conventional.
- **Idempotent and config-driven** — re-running a build is safe; behavior changes via
  config, not rewrites.

## Build protocol

1. Read the spec fully — intent, scope, acceptance criteria, notes — before touching any
   file.
2. Implement on `feature/<id>`. Commit atomically; messages describe *why*, not what.
3. Run the repo's test/lint suite before every commit. For this repo:
   `ruff check . && ruff format --check . && pytest`. Green is a hard gate, not advisory.
4. When all acceptance criteria are met, open the PR via the project's helper script.
   Never push to `main`. Never merge. Full steps: `docs/dev-loop/BUILD_RUNBOOK.md`.

## Ambiguity protocol

If the spec is under-specified for a required decision, **stop and ask the operator**
before guessing. One wrong decision can block the entire PR. Post the question via the
project's `ask` command (e.g. `python3 skills/summon/skill.py ask <id> "<question>"`).
This posts to the dev-loop Discord and records the blocked state — the stall watchdog
does not fire while a question is pending. Then halt and wait. **Never guess.**

## What a builder may never do

- Push to `main` or any protected branch
- Merge a PR (only the operator merges)
- Modify another item's queue status
- Act on a `proposed` item — it must be `authorized` first
- Edit CI workflow files (`.github/workflows/`), instruction files (`SOUL.md`, `AGENTS.md`,
  `CLAUDE.md`, `CONTEXT.md`, `TOOLS.md`), or agent security config

## QA contract (§8.5)

Tom returns JSON with three arrays: `spec_conformance` (deviations + severity),
`defects` (blocking: bug/edge/security/missing-test, with location),
`architecture_notes` (advisory). **Gate:** any blocking defect or deviation fails the
workflow and blocks merge. Advisory notes flow to the backlog. **Fail-closed:** if Tom
cannot run, the check is red — never green.

## Persona `CLAUDE.md` contract (§8.6)

Every builder persona's `CLAUDE.md` has five blocks:

1. **Identity** — name, role, repos owned.
2. **Voice** — tone descriptor + 3–4 do/don't examples (short). Colors reporting; never
   overrides the build contract.
3. **Project context** — repo architecture, conventions, and docs pointers. Grounded in
   what actually exists in the repo.
4. **Build contract** — pointer to this base + the project runbook. No restatement.
5. **Delegation map** — empty for now (task specialists are deferred).
