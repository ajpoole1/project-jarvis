# BUILD — Phase 4: Narration

**Load with:** `DEV_LOOP_REFERENCE.md`. Requires Phases 1–3 (there must be queue/PR state to narrate). Smallest phase; closes out the framework.

Covers: the dev-crew standup — the orchestrator narrating loop state from git.

---

## 4.1 Standup narration

**Goal:** the orchestrator reports the loop's state in persona terms, sourced from git, with no live session required.

**Build:** Jarvis reads git/GitHub state for the Jarvis repo (read-only `gh`) — open PRs, their QA check status, dev-queue item statuses, recent merges — maps each to its persona via the roster (REFERENCE §8.2), and emits a "dev-crew standup" line in the daily briefing and on request. Because it reads state, not processes, it works whether or not any builder session is live (REFERENCE §2).

**Acceptance:** Jarvis reports the Jarvis repo's queue/PR state in persona terms (e.g. who's mid-build, what's authorized-and-waiting, what's awaiting the operator's merge), drawn from git, with no session open.

---

**Phase 4 done when:** the standup reflects real git state accurately. The Phase-1 framework is complete.

## After Phase 4 (not now)
- **Fan out:** onboard other repos with `onboard-repo.sh` (Phase 3) + author each persona's `CLAUDE.md`. The shared `qa.yml` and summon skill already cover them.
- **Subagents:** revisit task specialists once the loop has earned trust.
- **Relay / APK:** build the phone ingress when the app exists.
- **Migrate context to the repo:** once the system is proven, bring the broader design/knowledge docs repo-side so the orchestrator and builders share them.
