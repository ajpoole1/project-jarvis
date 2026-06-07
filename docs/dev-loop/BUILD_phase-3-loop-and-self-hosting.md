# BUILD — Phase 3: Close the Loop + Self-Hosting

**Load with:** `DEV_LOOP_REFERENCE.md`. Requires Phases 1–2. This phase closes the merge loop and mints the first builder persona — the point where the framework becomes **self-hosting** (REFERENCE §10).

Covers: merge/backlog automation → the builder persona + onboarding script.

---

## 3.1 Merge / backlog loop

**Goal:** merging closes the item cleanly and nothing evaporates.

**Build:** `merged.yml` on push-to-`main`: detect the merged item id; flip `built → merged`; move the spec to `archive/`; reconcile `dev-queue` (fast-forward/rebase onto main so it doesn't drift); create `backlog/` stubs from the PR's `qa-findings` advisory notes (REFERENCE §8.5).

**Acceptance:** merging a PR flips the item to `merged`, archives the spec, reconciles `dev-queue`, and seeds backlog stubs for the advisory notes.

---

## 3.2 Builder persona + onboarding script  *(self-hosting milestone)*

**Goal:** a named builder for the Jarvis repo, minted via the same template every future repo will use.

**Build:**
- `scripts/dev-crew/onboard-repo.sh <repo> <persona>` — stamps the shared pieces idempotently: `.claude/settings.json` baseline (the allow/deny in REFERENCE §5/§8.4), helper scripts + `BUILD_RUNBOOK.md`, the `qa.yml` caller, an empty `dev-queue` branch, and a roster entry; leaves the persona `CLAUDE.md` identity/voice/context for the operator to author.
- **Dogfood it on the Jarvis repo**, then hand-author the builder persona `CLAUDE.md` — the five blocks in REFERENCE §8.6. Critical: the **project-context block** must point only to docs present *in this repo* (this reference, the repo's own `CLAUDE.md`, the codebase) — **do not reference any external master plan**, the builder won't have it. Register the persona in `knowledge/dev-crew/roster.md`.

**Guard:** the project-context block is load-bearing — without the repo's conventions, the builder writes off-pattern code that passes its own eyes and fails QA. Point to canonical in-repo docs, don't duplicate or invent.

**Acceptance — the milestone:** the operator writes a real `proposed` Jarvis feature → authorizes it on git → `summon <persona> <id>` → it builds end-to-end with no hand-holding → Tom gates → operator merges → the merge loop closes the item. From here, Jarvis features flow through the loop.

**Stop-and-ask:** the persona **voice** block is the operator's to author — the script stamps everything else and pauses for it. The Jarvis repo's builder **name is decided: Herr Mannkusser** — use it; do not invent or substitute a name.

---

**Phase 3 done when:** a full unattended `summon → build → PR → QA → merge → close` cycle has run once on a real feature. The framework is now self-hosting. Then proceed to Phase 4.
