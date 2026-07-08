# Project Jarvis — Author Overlay

**This is the at-desk authoring session.** AJ driving via Claude Code.
Not Jarvis. Not Herr Mannkusser. Jarvis reads none of this file.

---

## Contract

Inherit `docs/dev-standards/DEV_BASE.md` (§5 — Author role) + `CHARTER.md`.

**The verbs:**
- `/ship` — the only exit ramp: checks → `/qa` → commit+push → PR
- `/qa` — lens review before any PR (also runs inside `/ship`)
- `/handoff` — scaffold session entry in `CONTEXT.md`; run at every session end

---

## Safety essentials (non-negotiable, never skip)

- **Personal data / secrets never staged.** Before any commit: `git status` + verify no files from `config/personal/`, `~/.jarvis/`, `.env*`, or `*.db` are staged.
- **`.github/workflows/` is out of scope** unless the operator's instruction explicitly names a workflow file. Never touch these speculatively.
- **Private knowledge root is out of bounds** for direct Edit/Write — use the knowledge skill for writes, or the author session explicitly routed to `~/.jarvis/knowledge/`.
- **Push is always a human action.** `/ship` opens the PR; AJ pushes to merge. Never `git push --force`.

---

## Doc map

| What you need | Where to look |
|---|---|
| Why Jarvis exists, architecture, security model, phase status | `docs/ARCHITECTURE.md` |
| Repo values, stack, QA pairing, declared lenses | `CHARTER.md` |
| Dev contract (all roles) | `docs/dev-standards/DEV_BASE.md` |
| QA lens definitions | `docs/dev-standards/QA_LENSES.md` |
| Python conventions | `docs/dev-standards/PY_STANDARDS.md` |
| How to build a skill | `docs/guides/SKILL_GUIDE.md` |
| Multi-repo adoption (stand up a new repo) | `docs/dev-standards/ROLLOUT.md` |
| Build procedure (builder sessions) | `docs/dev-loop/BUILD_RUNBOOK.md` |
| Dev-loop invariants, loop mechanics | `docs/dev-loop/DEV_LOOP_REFERENCE.md` |
| Jarvis runtime context layer (SPINE) | `knowledge/dev-crew/jarvis-memory.md` |
| Session handoff log | `CONTEXT.md` |

---

## Session handoff

End every session with `/handoff`.
