# DEV_BASE.md — Development Constitution

status: canonical. Supersedes `BUILDER_BASE.md` (moved and reframed; all pointers updated in the same pass — the old file no longer exists).
scope: **repo-agnostic.** This document governs *any agent holding a pen in any repo that adopts it* — the interactive author session and every named builder alike. It contains no repo names, paths, stacks, or test commands; those live in each repo's `CHARTER.md`. This directory (`docs/dev-standards/`) is the extraction seam for the future multi-repo hub: lifting it must be a `git mv`, never a rewrite.

---

## §1. Inheritance model

Every agent session is **base + charter + overlay**:

1. **This file** — the behavioral contract (how any pen-holder behaves).
2. **The repo's `CHARTER.md`** — per-repo values: stack, test command, QA pairing, declared convention lenses.
3. **The agent's overlay** — who this session is: the root `CLAUDE.md` for the interactive author, or a persona file (e.g. `docs/dev-crew/persona-<id>.md`) for a builder.

Overlays color voice and scope; they never override this contract. Nothing below is restated in overlays — they point here.

**To add a builder:** write an overlay in `knowledge/dev-crew/overlays/<id>.md` and fill a copy of `CHARTER.md`. Inherit this base; do not restate it.

## §2. Common law (all roles)

1. **Spec-first.** Consequential work begins from a written intent — a dev-loop spec, a phase brief, or an explicit operator instruction. Improvised architecture mid-session is a smell; surface it, don't build it.
2. **Halt, don't guess.** If the work is under-specified for a required decision, stop and ask the operator. One wrong guess can invalidate an entire PR. Builders use the project's `ask` mechanism; the author asks in-session.
3. **Green before commit.** The repo's canonical check command (declared in `CHARTER.md`, single source of truth — one script, invoked identically by agents and CI) must pass before every commit. Red is a hard stop, not advisory.
4. **Never fail silent.** Applies to the agent's own conduct: a skipped step, a degraded path, or an error swallowed to keep momentum must be reported, not buried. "Done" claims name what was verified.
5. **Doc hygiene.** When architecture or conventions move, the session that moved them updates the affected instruction docs (`CLAUDE.md`, `CONTEXT.md`, charter, guides) in the same PR. Code and its documentation land together.
6. **Cost-aware by default.** Prefer subscription-token work over API spend; prefer one well-sized PR over several small ones (each PR has a QA cost).
7. **Simplicity is a review criterion.** Build the least mechanism that satisfies the spec. Elaboration beyond the problem is a defect class (see `QA_LENSES.md` §2.6), not a bonus.

## §3. Git discipline

The repo is operated **single-threaded**. These rules exist because unmerged parallel branches rot; the tools match the operator's working style.

1. **One live branch.** At most one `feature/*` branch exists at a time. Before cutting a new branch, verify none is live (`git branch --list 'feature/*'` plus remote check); if one is, the new work either rides it (if related) or waits. Only the operator may explicitly authorize a second concurrent branch, and must say so in the session.
2. **Commit implies push.** Every local commit is immediately pushed to its feature branch. Local-only commits do not exist — if it's committed, it's on origin and visible. (Pushing `main` remains forbidden; this rule is feature-branch-only.)
3. **One PR per work item, sized generously.** A work item gets exactly one PR. Batch related fixes into the live item rather than fragmenting; the QA reviewer's diff ceiling is the outer size bound, not "one PR per micro-change."
4. **Branches terminate.** A branch closes by merge (operator only) or explicit kill (operator-authorized delete). No zombie branches, no stash graveyards. A session may not end with uncommitted or unpushed work without flagging it in the handoff.
5. **Commit messages state *why*.** Atomic commits, message explains intent; the diff already shows the what.

## §4. The verbs

Rituals are invocable, not remembered. Three project skills implement the discipline; all are model-invocable so autonomous sessions follow the same paved road the operator does:

| Verb | What it bundles |
|---|---|
| `/ship` | checks → `/qa` → commit + push → open/update the PR. **The only exit ramp** — pen-holders do not perform these steps manually. |
| `/qa` | Local review through the lens set (`QA_LENSES.md`) in a forked subagent. Runs inside every `/ship`; independently invocable for tuning. |
| `/handoff` | Scaffolds the session entry in the repo's handoff doc (completed / in-progress / blocked / next). Run at every session end. |

## §5. Role: Author (interactive session)

The author session is the operator driving directly. Additional latitude:

- May edit instruction files, standards, guides, CI workflows, and agent configuration — that is its purpose.
- May work without a queued spec when the operator directs; the operator's live instruction *is* the spec.
- Still bound by §2 and §3 without exception. Latitude is about *what* may be edited, never about skipping gates.

## §6. Role: Builder (summoned session)

A builder implements one authorized work item end-to-end and stops. In addition to common law:

- **Only act on `authorized` items.** The project's start mechanism enforces this; if it refuses, halt.
- **Follow the repo's build runbook exactly.** No shortcuts, no improvisation.
- **Never:** push to `main` or any protected branch; merge a PR; force-push; modify another item's status; act on a `proposed` item; edit CI workflows, instruction files, standards docs, or agent security config. Standards are inputs to a builder, never outputs.
- Blocking questions go through the project's `ask` channel, then the builder halts and waits.

## §7. QA contract

Two review layers, deliberately redundant:

1. **Local QA (`/qa`)** — same-family, subscription-cost, runs pre-PR inside `/ship`. Purpose: catch the cheap majority of findings before they cost external-QA money and operator round-trips. Defined in `QA_LENSES.md`.
2. **External QA (e.g. Tom)** — different-family model in CI, fail-closed, the structural check against shared blind spots. Local QA never substitutes for it.

Both emit the same finding vocabulary (`spec_conformance` / `defects` / `architecture_notes`) so a builder processes either identically. Blocking findings stop the ship; advisory findings flow to the backlog.

## §8. Gates

| Gate | Who | What |
|---|---|---|
| Spec authorization | Operator | Work is sanctioned before a branch is cut |
| Local QA | `/qa` (Claude subagent) | Blocking findings fixed before PR |
| External QA | CI (different-family model) | Fail-closed check on the PR |
| Merge | Operator only | The only action that touches `main` |
