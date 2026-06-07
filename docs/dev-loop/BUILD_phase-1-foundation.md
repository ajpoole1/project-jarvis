# BUILD — Phase 1: Foundation

**Load with:** `DEV_LOOP_REFERENCE.md`. Build the three pieces below in order; each gates the next. Nothing here is routed through the loop — these are hand-built in an ordinary Claude Code session (bootstrap caveat, REFERENCE §10).

Covers: auth plumbing → dev-notes queue + transport → build mechanics.

---

## 1.1 Auth plumbing  *(build first — highest blast radius)*

**Goal:** any Code session launches on the intended account, and a misconfiguration is caught loudly, never billed silently.

**Build:**
- Auth-profile config (REFERENCE §8.3): each function → `subscription | api`. Defaults: builder + persona = `subscription`; classifier = `api`.
- Launch wrapper `scripts/dev-crew/launch.sh`: builds each child env **explicitly from a clean base** — it does not inherit an ambient key. `api` profile → inject `ANTHROPIC_API_KEY` read from secure storage (this is how the classifier gets its key). `subscription` profile → inject no key, so Claude Code defaults to the Max OAuth creds; add `env -u ANTHROPIC_API_KEY` as belt-and-suspenders. The key is never globally exported — **OAuth is the fail-safe default, API is opt-in per launch** (REFERENCE §7).
- Post-launch auth assertion: run `claude /status`, assert Max for `subscription` profiles; on mismatch, abort + Discord-alert. Do not build on unverified auth.
- Monthly Console-charge check surfaced wherever the operator will see it (e.g. the daily briefing).

**Prereq (manual, operator):** `claude setup-token` on the host; provision the classifier `ANTHROPIC_API_KEY`.

**Guard:** silent API billing on a session that should be subscription-backed. Primary protection is the clean-base env (no key unless injected); the `/status` assertion is the tripwire on top. Without both, this piece is not done.

**Acceptance:** a `subscription` launch verifiably reports Max auth; a launch built from the wrapper's clean base with no profile/key defaults to OAuth (never API); the classifier (api profile) gets its injected key and bills API; a deliberately mis-set profile aborts with an alert rather than billing.

**Stop-and-ask:** (a) if `/status` output isn't cleanly machine-parseable, confirm the assertion method with the operator. (b) Confirm the orchestrator routes its interactive agent to the OAuth setup-token and only the classifier to the API key — if it only supports a single global daemon key, flag it before proceeding, because that reintroduces the ambient-key default this design removes.

---

## 1.2 Dev-notes queue + transport

**Goal:** a structured, idempotent, remotely-visible handoff medium inside the orchestrator's write boundary.

**Build:**
- `dev-queue` branch carrying only `knowledge/dev-notes/**`.
- Dirs `knowledge/dev-notes/{queue,backlog,archive}/`; items per the schema in REFERENCE §8.1.
- Schema-validator CI check on push to `dev-queue`: valid `status` enum, required fields, `id` == filename.
- `devqueue` skill (self-contained venv): validates a proposed spec and pushes `knowledge/dev-notes/**` to `dev-queue` **only**. Validated argv; branch + path hard-locked; a pre-push guard refuses any other target.

**Guard:** the orchestrator gaining a general push capability. The skill must be physically unable to push anywhere but `dev-queue` / `knowledge/dev-notes/**`.

**Acceptance:** a proposed spec written by the orchestrator reaches `dev-queue` and is visible on GitHub mobile; a malformed spec fails CI; the skill rejects any out-of-scope push target.

---

## 1.3 Build mechanics

**Goal:** the deterministic git plumbing a build session follows (so a phone-driven session doesn't improvise).

**Build:**
- `docs/dev-loop/BUILD_RUNBOOK.md` — the procedure a builder follows; referenced from the persona `CLAUDE.md` (Phase 3).
- `scripts/dev-loop/start-build.sh <id>` — `git fetch`; **assert item is `authorized`** (refuse otherwise); **refuse if `feature/<id>` exists**; branch `feature/<id>` from `main`; copy the authorized spec into the branch; set `building`.
- `scripts/dev-loop/open-pr.sh <id>` — set `built`; commit; push `feature/<id>`; `gh pr create` → `main` with the item id in the body. **Never merges, never pushes `main`.**

**Guard:** double-builds and `main` contamination. The two refusals in `start-build.sh` are the idempotency enforcement (REFERENCE §8.1).

**Acceptance:** `start-build.sh` refuses non-authorized and duplicate items and yields a clean feature branch carrying the spec; `open-pr.sh` opens a PR containing the spec and touches nothing on `main`.

---

**Phase 1 done when:** all three acceptances pass. Then proceed to Phase 2.
