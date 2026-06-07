# BUILD — Phase 2: Summon + QA

**Load with:** `DEV_LOOP_REFERENCE.md`. Requires Phase 1 complete (auth wrapper, queue, mechanics). Build both pieces; they're independent of each other but both build on Phase 1.

Covers: the summon skill (the launcher) → the QA layer (Tom, the non-Claude gate).

---

## 2.1 Summon skill

**Goal:** the operator summons a named builder from Discord; it launches on the right repo/auth and runs to PR unattended.

**Build:** the `summon` skill (self-contained), fired **only on the operator's explicit command** — never on authorize (REFERENCE §3, invariant 3). On `summon <persona> <id>`:
1. Look up persona in the roster (REFERENCE §8.2): repo_dir, name, model, auth profile. Validated argv (persona id only).
2. Launch via the Phase 1 wrapper (env constructed per auth profile): `tmux new-session -d -s <persona> -c <repo_dir> 'claude'`.
3. Enable + name remote control (`/remote-control "<Name> — <repo>"`); scrape the session URL (best-effort — see guard).
4. Run the Phase 1 `/status` Max-auth assertion; abort + alert on mismatch.
5. Seed the kickoff prompt (pointer to `BUILD_RUNBOOK.md` + the item id).
6. Reply in Discord with the session link.
- Lifecycle: idle **reaper** (close tmux sessions idle past N minutes) + `dismiss <persona>` command. **Concurrency guard:** warn past a live-builder threshold.

**Guard:** the URL scrape is the flaky part and **nothing depends on it** — the build runs to PR on pre-approved perms regardless; the QA Discord ping (2.2) is the completion signal. Don't block the build on the scrape.

**Acceptance:** `summon` launches a named session in the correct repo on Max auth, seeds the build, returns a link; `dismiss` and the reaper work; the concurrency guard fires past the threshold.

**Stop-and-ask:** confirm the exact `claude remote-control` invocation and URL-capture behavior against the live CLI before hardening step 3.

---

## 2.2 QA layer (Tom — the non-Claude gate)

**Goal:** a non-Claude, spec-aware reviewer gates every PR; fail-closed.

**Build:**
- `qa.yml` as a **reusable workflow** (`workflow_call`): on PR → `main`, scoped to code-path / `dev-loop`-labelled PRs. Steps: compute the diff; load the authorized spec from the PR; load the repo `CLAUDE.md` + relevant context (cached); call **Gemini 3 Flash** requiring the JSON contract in REFERENCE §8.5; **fail** on any blocking defect or blocking conformance deviation; post a grouped PR comment; write a `qa-findings` artifact.
- The Jarvis-repo **caller** workflow that invokes it.
- Secrets: `QA_MODEL_API_KEY`, `DISCORD_DEVLOOP_WEBHOOK` (Actions secrets, never in repo). Provider **hard spend cap** ($5/mo) on the key.
- **Branch protection:** require the `qa` check + existing `security`/`quality`/`docker` checks before merge. Auto-merge stays **off** (REFERENCE §3).
- Discord webhook on completion (verdict + estimated cost); **fail-loud alert** if Tom errors.

**Guard:** a QA that can't run must never produce a green check (REFERENCE §8.5). Workflow-fails-on-error + required-check = merge blocked; alert fires.

**Acceptance:** a PR with a planted bug goes red and blocks the merge button; a clean PR goes green and pings Discord; a forced QA outage goes red + alerts, never green.

**Stop-and-ask:** confirm the current Gemini model id + endpoint before wiring; keep them config-driven. Do **not** substitute a Claude model — the non-Claude decorrelation is the requirement (REFERENCE §3, invariant 4).

---

**Phase 2 done when:** both acceptances pass. Then proceed to Phase 3.
