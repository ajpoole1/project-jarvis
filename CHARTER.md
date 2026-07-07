# Project Charter — project-jarvis

<!-- Filled charter for project-jarvis. The blank template lives at
     docs/dev-standards/CHARTER_TEMPLATE.md — edit that file, not this comment, to
     update the template. Invariant sections (build rigor, QA contract) are inherited
     from docs/dev-standards/DEV_BASE.md and referenced here — not restated. -->

---

## Goal

**project-jarvis** is a personal AI assistant and dev-loop orchestrator.

- **Personal assistant:** conversational interface (Discord) + scheduled skills covering
  Gmail, Calendar, Home Assistant, Garden, Knowledge, Tasks, Follow-ups, and Price
  Monitoring.
- **Dev-loop orchestrator:** the repo proving ground for the named-builder loop
  (Herr Mannkusser + Tom QA). Specs are authorized here; PRs are built here; the loop
  fans out to other repos (Bolas, Signal) in Phase 1 once the pattern is stable.

## Stack

| Layer | What |
|---|---|
| Orchestrator | OpenClaw (Node.js) — Jarvis identity, spec brainstorm, summon dispatch |
| Skills | Python — one `skill.py` per capability; stdlib-only skills have no venv |
| Builder persona | Claude Code (claude-sonnet-5, Max OAuth) — Herr Mannkusser |
| QA reviewer | Gemini (Tom) via GitHub Actions — non-Claude by design |
| Data layer | SQLite (`/data/jarvis.db`) — shared state; never preloaded into context |
| Smart home | Home Assistant (WSL2 systemd) + Docker (go2rtc) |
| Surface | Discord (current); Android app (Phase 5, deferred) |

## Build Rigor

Inherited from `docs/dev-standards/DEV_BASE.md`. Summary:

- Spec-literal implementation on `feature/<id>` branches.
- `scripts/dev-loop/checks.sh` green before every commit (single source of truth — identical to CI).
- Two human gates (authorize + merge). No auto-merge.
- Halt-and-ask on ambiguity — never guess.
- Land work via `/ship`; run `/qa` before PR; scaffold handoff with `/handoff`.

## QA Pairing

| Role | Model | Rationale |
|---|---|---|
| Builder | Claude Sonnet 5 | authoring + implementation |
| Reviewer (Tom) | Gemini | QA model ≠ builder model — decorrelates blind spots |

The builder builds; Tom reviews the diff independently. A same-family reviewer would
share the builder's blind spots, so heterogeneous QA is a structural requirement, not
a style preference.

**Fail-closed:** if Tom cannot run, the CI check is red. Merge is blocked until Tom
passes or the operator explicitly overrides.

## QA Parameters

| Parameter | Value |
|---|---|
| Check command | `scripts/dev-loop/checks.sh` (wraps ruff + pytest; identical to CI) |
| QA model | Gemini (via `GOOGLE_API_KEY` secret in CI) |
| Tom entry point | `.github/workflows/qa.yml` → `scripts/tom/run_tom.py` |

## QA Lenses

standards_root: docs/dev-standards
- @std/PY_STANDARDS.md
- docs/guides/SKILL_GUIDE.md

## Cascade

**Builder cascade:** `docs/dev-standards/DEV_BASE.md` (shared base) +
`knowledge/dev-crew/overlays/mannkusser.md` (Herr's persona overlay).

Adding a builder = write an overlay in `knowledge/dev-crew/overlays/<id>.md` + fill
a copy of `docs/dev-standards/CHARTER_TEMPLATE.md`. The base is inherited; don't restate it.
For standing up a new repo on this framework, follow `docs/dev-standards/ROLLOUT.md`.
