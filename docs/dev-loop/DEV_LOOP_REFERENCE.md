# Dev-Loop Framework — Reference

**What this is:** the single durable source of truth for the Jarvis dev-loop / crew framework. The build briefs (`BUILD_phase-1..4`) reference this for the *why* and the shared contracts; they don't restate them. A constructor agent (Claude Code) should load **this file plus the one phase brief it's working on** — nothing else is required.

**Self-contained by design:** this doc does not depend on any external master plan or design doc. Everything the build needs is here. (Design rationale and broader project context live on the brainstorming side and are not needed to construct the framework.)

---

## 1. What we're building

A semi-autonomous development loop: an orchestrator brainstorms a change, a human authorizes it, a named builder agent implements it on a branch, a non-Claude reviewer gates the PR, and the human merges. Built **in the Jarvis repo first** as the proving ground; fanned out to other repos later.

## 2. Actors

- **Jarvis** — the orchestrator (runs on OpenClaw). Brainstorms specs, dispatches work, narrates state. **Never writes code.** Its only write surface is Markdown knowledge files (§5).
- **The builder persona** — a Claude Code session that builds the repo's code. One per repo/group. The Jarvis repo's builder is named **Herr Mannkusser**. Identity, voice, and context come from the repo's `CLAUDE.md` (§ contract 6).
- **Tom** — the QA reviewer. A **non-Claude** model (Gemini) running in CI on the PR diff. Deliberately a different model family from the builder so it catches what same-family review can't.
- **The operator** — the human. Holds both gates.

The crew is a **projection of git state plus a launcher** — personas are config, visibility is reading GitHub, execution is a session summoned per item. Nothing must be permanently running except the host and the orchestrator.

## 3. Invariants (non-negotiable)

1. **Two human gates:** the operator authorizes the spec (gate #1) and reviews + merges the PR (gate #2). Neither is a rubber stamp; they are the outside checks.
2. **No auto-merge.** Merge is the only thing that touches `main`, and only the operator does it.
3. **No orchestrator-autonomous builder spawn.** Summoning a builder is an explicit operator command, never automatic on authorize. A human is in every spawn.
4. **Heterogeneous QA.** Tom's primary reviewer is non-Claude (Gemini). Never swap the primary to a Claude model — the decorrelation is the entire point; two same-family models share blind spots. **Documented exception:** if Gemini is unavailable after full retry exhaustion (§8.5 retry budget), Tom may fall back to a Claude model for that run only, so merges aren't blocked by a third-party outage. Every fallback verdict is labeled "Claude fallback — Gemini unavailable" in the PR comment and Discord notification — it is never presented as an ordinary Tom pass. The fallback is a resilience valve, not a routine path; if it fires often, that's a signal to revisit the primary provider, not to make Claude the new default.
5. **Authorize and summon are separate actions.** Authorize = "this spec is correct." Summon = "build it now." The operator may authorize many and summon few.

## 4. Guiding principles (carry into every build)

- **Stage then approve** — actions are staged for human review before they take effect.
- **Graceful degradation** — components fail without taking the system down; failures are loud, never silent.
- **Single surface** — interactions flow through one coherent channel (Discord for now).
- **Security by design** — bounds are architectural, enforced at the agent/tool level, not by convention.
- **Idempotent and config-driven** — re-running is safe; behavior changes via config, not rewrites.

## 5. The two permission boundaries (do not conflate)

These are two different agents with two different mandates.

**Jarvis (orchestrator):** deny-by-default. May write only Markdown under the knowledge tree; proposes everything else. It does not edit code, push to `main`, or run arbitrary shell.

**The builder (Claude Code):** broad **repo write on a feature branch** — it must edit code, configs, tests, or it can't build. Bounded by its `.claude/settings.json`:
- **Allow:** repo edits; the helper scripts; `git` branch/add/commit; push to `feature/*`; `gh pr create`; test/lint runners.
- **Deny (hard):** push to `main`; any merge; `git push --force`; branch/data deletion; secret access; arbitrary shell.

The gates are enforced at the agent level here, not just by convention — even a confused builder physically cannot reach `main` or merge.

## 6. Topology (all-local, for context)

The brain runs on the home machine: the orchestrator, the builders, the datastore, and all credentials stay local. A phone client and public relay are **deferred** until a custom app exists; for now the single surface is Discord (the orchestrator dials out to it, so no inbound exposure is needed). Because the host is now a dependency, anything that bridges to it (and the host itself) must auto-reconnect and fail loud rather than hang.

## 7. Auth & cost model

**Policy reality (contested — verify before relying on it):** subscription-OAuth use in third-party tools has flip-flopped repeatedly through 2026. The current read: the Agent SDK and always-on/server/multi-user deployments require an API key, while **personal, single-user, own-machine use is treated as ordinary individual usage and is permitted.** Running this on the home machine for one operator is the defensible side of that line; a server/VPS would not be. Treat subscription auth as working-but-unstable and keep the API fallback wired (the toggle below).

**Per-function auth:**

| Function | Tool | Auth | Notes |
| --- | --- | --- | --- |
| Builder(s) + operator hand-coding | Claude Code | **Max OAuth** (`setup-token`) | most-permitted case; flat-rate |
| Orchestrator interactive | OpenClaw | **Max OAuth** (`setup-token`) | works today; contested; fallback wired |
| Headless classifier | Agent-SDK-style | **API key** | Agent SDK requires API; subscription can't serve headless |
| Tom (QA) | Gemini | **Google API** | non-Claude by requirement |

**The env mechanism (fail-safe to OAuth):** OAuth must be the default, so the environment a launch inherits must **not** contain `ANTHROPIC_API_KEY` — a session with no key falls back to the Max OAuth credentials and *cannot* bill API by accident. The key lives in secure storage (keychain/secrets file), never a global export. The launch wrapper builds each child env **explicitly from a clean base** and **injects the key only for `api`-profile launches** (the classifier). Subscription launches get no key — OAuth by construction. So API is opt-in per launch; it is never the inherited default. The classifier still gets the key it needs — scoped to its own launch env, not the global one.

**Silent-billing guard:** Claude Code bills API whenever the key is present in a session, so the design keeps the key *absent* unless explicitly injected — that's the primary protection. Cross-checks on top: `env -u ANTHROPIC_API_KEY` on subscription launches as belt-and-suspenders; a post-launch `claude /status` assertion (abort + alert on mismatch); a monthly charge check for leaks. Fail-loud, not optional. (Verify the orchestrator routes its *interactive* agent to the OAuth setup-token and only the classifier to the key — a single global key in the daemon would reintroduce the very default this design removes.)

**The toggle:** a config maps each function → `subscription | api`; the launch wrapper builds the child env from it. Claude Code functions + the orchestrator genuinely toggle; the headless classifier is pinned to `api`.

**Cost:** Max 5x carries the Claude Code work + orchestrator interactive (flat-rate, buys back the throttle that hurt on Pro). API (metered, hard-capped) covers only the classifier + Tom. Re-evaluate Max 5x→20x only if the shared bucket saturates.

---

## 8. Shared contracts (defined once; briefs reference these)

### 8.1 Dev-notes queue item (front-matter schema)
Lives on a `dev-queue` branch under `knowledge/dev-notes/{queue,backlog,archive}/`, one Markdown file per item; `id` = `YYYY-NNNN-slug`, matches filename.

```yaml
---
id: 2026-0001-example
title: …
status: proposed        # proposed|authorized|building|built|merged
scope: well-bounded-local   # | needs-design-pass
origin: brainstorm      # | iteration-backlog
author: jarvis
created: 2026-06-06
think_level: high
branch: null
pr: null
authorized_by: null
authorized_at: null
qa_artifact: null
---
## Intent / ## Scope / ## Acceptance criteria / ## Notes
```
**Lifecycle:** `proposed → authorized → building → built → merged`. **Idempotency:** a builder acts only on `authorized`; it sets `building` (the claim), then `built`. It never touches `proposed`, an already-`building` item, `built`, or `merged`. `building`/`built` live on the feature branch; the queue on `dev-queue` shows `authorized` until merge.

### 8.2 Roster (`knowledge/dev-crew/roster.md`)
One entry per persona: `id`, `name`, `role`, `repos`, `repo_dir`, `voice`, `model`, `auth` (`subscription|api`). The launcher and the narration read this.

### 8.3 Auth-profile config
Maps each function → `subscription | api`. Consumed by the launch wrapper to construct the child environment (§7).

### 8.4 Builder permission set (`.claude/settings.json`)
The allow/deny from §5. Background/unattended sessions auto-deny anything not allow-listed *silently*, so the allow-list must cover every tool a build needs end to end.

### 8.5 QA contract (Tom's output)
Tom returns JSON with three arrays: `spec_conformance` (deviations, severity), `defects` (blocking: bug/edge/security/missing-test, with location), `architecture_notes` (advisory). **Gate:** any blocking defect or blocking conformance deviation fails the workflow → blocks the (protected-branch) merge. Advisory notes flow to the backlog. **Fail-closed:** if Tom can't run, the check is red and an alert fires — never green.

**Retry budget & fallback (per invariant #4):** the primary reviewer (Gemini) gets 5 attempts with exponential backoff on transient HTTP 429/503 or network errors, plus up to 3 attempts to recover a parseable JSON response. Only after that full budget is exhausted does the workflow attempt the Claude fallback — a single call, same prompt and schema, clearly labeled in the PR comment and Discord ping as a fallback verdict. If the fallback call also fails, the check stays red (fail-closed still holds — there is no third tier).

### 8.6 Persona `CLAUDE.md` (five blocks)
1. **Identity** — name, role, repos owned.
2. **Voice** — one-line descriptor + 3–4 do/don't examples; short. Voice colors reporting/tone, never overrides the build contract.
3. **Project context** — the repo's architecture and conventions, so the builder isn't globally blind. Point to docs that exist *in this repo* (this reference, the repo's own `CLAUDE.md`, the codebase) — do **not** assume any external master plan is present.
4. **Build contract** — follow the build runbook; use the helper scripts; act only on `authorized` items; never push `main` or merge; stop and ping on ambiguity.
5. **Delegation map** — empty for now (task specialists are out of scope; see §9).

---

## 9. Scope

**In:** the framework above, built in the Jarvis repo.
**Out (do not build):** task-specialist **subagents** (parked); the **phone relay / APK** ingress (deferred); **fan-out to other repos** (later phase — the onboarding script is built and dogfooded on Jarvis, applied elsewhere afterward); Agent Teams; Cowork Dispatch.

## 10. Bootstrap caveat & self-hosting milestone

You cannot use the loop to build the loop. Phases 1–2 (and most of 3) are built in **ordinary Claude Code sessions** — there is no summon/queue/QA to route them through yet. **The self-hosting milestone is in Phase 3:** once the builder persona exists and a clean unattended `summon → build → PR → QA → merge` has run once on a real feature, all subsequent Jarvis features go through the loop.

## 11. Build order

Phase 1 (foundation) → Phase 2 (summon + QA) → Phase 3 (loop close + persona = self-hosting) → Phase 4 (narration). Do not parallelize; each phase gates the next on its acceptance criteria.
