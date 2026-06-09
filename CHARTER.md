# Project Charter — project-jarvis

<!-- Template: copy to each project repo and fill the values in this file.
     Invariant sections (build rigor, QA contract) are defined in
     `docs/dev-loop/BUILDER_BASE.md` and referenced here — not restated.
     Update the project-specific values; leave the inherited sections as pointers. -->

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
| Builder persona | Claude Code (claude-sonnet-4-6, Max OAuth) — Herr Mannkusser |
| QA reviewer | Gemini (Tom) via GitHub Actions — non-Claude by design |
| Data layer | SQLite (`/data/jarvis.db`) — shared state; never preloaded into context |
| Smart home | Home Assistant (WSL2 systemd) + Docker (go2rtc) |
| Surface | Discord (current); Android app (Phase 5, deferred) |

## Build Rigor

Inherited from `docs/dev-loop/BUILDER_BASE.md`. Summary:

- Spec-literal implementation on `feature/<id>` branches.
- `ruff check . && ruff format --check . && pytest` green before every commit.
- Two human gates (authorize + merge). No auto-merge.
- Halt-and-ask on ambiguity — never guess.

## QA Pairing

| Role | Model | Rationale |
|---|---|---|
| Builder | Claude Sonnet 4.6 | authoring + implementation |
| Reviewer (Tom) | Gemini | QA model ≠ builder model — decorrelates blind spots |

The builder builds; Tom reviews the diff independently. A same-family reviewer would
share the builder's blind spots, so heterogeneous QA is a structural requirement, not
a style preference.

**Fail-closed:** if Tom cannot run, the CI check is red. Merge is blocked until Tom
passes or the operator explicitly overrides.

## QA Parameters

| Parameter | Value |
|---|---|
| Test command | `ruff check . && ruff format --check . && pytest` |
| QA model | Gemini (via `GOOGLE_API_KEY` secret in CI) |
| Tom entry point | `.github/workflows/qa.yml` → `scripts/tom/run_tom.py` |

## Cascade

**Builder cascade:** `docs/dev-loop/BUILDER_BASE.md` (shared base) +
`knowledge/dev-crew/overlays/mannkusser.md` (Herr's persona overlay).

Adding a builder = write an overlay in `knowledge/dev-crew/overlays/<id>.md` + fill
a copy of this charter template. The base is inherited; don't restate it. The base
extracts to `dev-standards` in Phase 1 (multi-repo hub); overlays and charters stay
per-repo.
