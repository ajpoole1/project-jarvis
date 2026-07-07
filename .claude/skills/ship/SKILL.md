---
name: ship
description: The only exit ramp for repo changes — run canonical checks, local QA, commit and push to the feature branch, then open or update the PR. Invoke whenever work is ready to land, in full or as an incremental checkpoint.
allowed-tools: Bash(git *), Bash(gh pr *), Bash(scripts/dev-loop/*)
---

# /ship — checks → QA → commit+push → PR

You are landing work under the git discipline of `docs/dev-standards/DEV_BASE.md` §3.
This skill is the paved road: never perform these steps individually outside it.

## 0. Preconditions

1. **Branch check.** You must be on a `feature/*` branch.
   - On `main` with changes — STOP. Verify no other `feature/*` branch exists locally
     or on origin (one-live-branch rule, DEV_BASE §3.1). If one exists, halt and ask
     the operator whether this work rides it or waits. If none exists, cut
     `feature/<id>` (dev-loop items: via `scripts/dev-loop/start-build.sh <id>`,
     which enforces authorization; ad-hoc author work: `git checkout -b feature/<slug>`).
   - On a feature branch — proceed.
2. **Nothing staged that shouldn't be.** Review `git status` for personal-data paths,
   secrets, or files outside the work's scope before anything else.

## 1. Checks

Run the repo's canonical check command — `scripts/dev-loop/checks.sh` (the single
definition of green; identical to CI). Red — fix and re-run. Do not proceed red.
Do not narrow, skip, or reinterpret the command.

## 2. Local QA

Invoke `/qa`. Process its JSON verdict per `QA_LENSES.md` §5:
- Blocking findings — fix, re-run step 1, re-invoke `/qa`. If the same finding
  survives two fix cycles, stop and ask the operator instead of looping.
- Advisory-only — capture the notes for step 4, proceed.

## 3. Commit + push (atomic pair)

1. Commit with a *why* message (DEV_BASE §3.5).
2. Immediately `git push origin feature/<id>` — commit implies push (DEV_BASE §3.2).
   A commit without its push is an incomplete step, not an option.
3. Never push `main`. Never force-push.

## 4. PR

- **PR already open for this branch** — the push in step 3 updated it. Append any
  new advisory notes to the PR description under `## Local QA notes`. Done.
- **No PR yet, dev-loop item** — run `scripts/dev-loop/open-pr.sh <id>` (it sets
  status, pushes, opens the PR against `main`).
- **No PR yet, ad-hoc author work** — `gh pr create` against `main`, body containing
  a summary, the advisory notes section, and what was verified (checks + QA passed).

## 5. Close out

Report in one short block: branch, commit(s) landed, checks result, QA verdict
(counts by category), PR link. If this ship ends the session, invoke `/handoff`.

## Refusals

- Refuse to ship with red checks, unresolved blocking findings, or from `main`.
- Refuse to create a second concurrent `feature/*` branch without explicit
  operator authorization in this session.
- Refuse `--no-verify`, test skips, or check narrowing as a path to green.
