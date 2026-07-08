# Project Charter — <REPO-NAME>

<!-- This template is maintained only in the hub (project-jarvis/docs/dev-standards/CHARTER_TEMPLATE.md).
     To adopt: copy this file to the consuming repo's root as CHARTER.md and fill the placeholder
     values. Do not restate invariant sections (build rigor, QA contract) — they are inherited from
     DEV_BASE.md via the standards_root. Follow ROLLOUT.md for the full stand-up procedure. -->

---

## Goal

**<repo-name>** is <one-sentence description of what this repo does and why it exists>.

- **<Capability 1>:** <brief description>
- **<Capability 2>:** <brief description>

## Stack

| Layer | What |
|---|---|
| <Layer name> | <Technology + brief role> |
| <Layer name> | <Technology + brief role> |

## Build Rigor

Inherited from `<standards_root>/DEV_BASE.md`. Summary:

- Spec-literal implementation on `feature/<id>` branches.
- `<canonical-check-command>` green before every commit (single source of truth — identical to CI).
- Two human gates (authorize + merge). No auto-merge.
- Halt-and-ask on ambiguity — never guess.
- Land work via `/ship`; run `/qa` before PR; scaffold handoff with `/handoff`.

## QA Pairing

| Role | Model | Rationale |
|---|---|---|
| Builder | <model> | authoring + implementation |
| Reviewer | <model/tool> | QA model ≠ builder model — decorrelates blind spots |

The builder builds; the reviewer reviews the diff independently. Heterogeneous QA is a structural
requirement, not a style preference.

**Fail-closed:** if the reviewer cannot run, the CI check is red. Merge is blocked until it passes
or the operator explicitly overrides.

## QA Parameters

| Parameter | Value |
|---|---|
| Check command | `<path-to-checks-script>` (wraps <linter> + <test-runner>; identical to CI) |
| QA model | <model/tool> (via `<SECRET>` secret in CI) |
| Reviewer entry point | `<workflow-file>` → `<script-path>` |

## QA Lenses

standards_root: <sibling-relative-or-repo-relative-path>
- @std/PY_STANDARDS.md
- <repo-relative-path/LOCAL_GUIDE.md>

<!-- standards_root examples:
     - Hub repo (project-jarvis): standards_root: docs/dev-standards
     - Consuming repo (preferred): standards_root: ../project-jarvis/docs/dev-standards
       (sibling-relative — stable across machines; all local repos share one parent directory)
     - Consuming repo (fallback): standards_root: /absolute/path/to/project-jarvis/docs/dev-standards
     Add one @std/ line per shared lens; add unprefixed lines for repo-local guides only.
     Note: settings.json additionalDirectories must use the absolute form — Claude Code does not
     resolve relative entries there. CHARTER carries relative; settings carries absolute. -->

## Cascade

**Builder cascade:** `<standards_root>/DEV_BASE.md` (shared base) +
`<path/to/persona-overlay.md>` (builder persona overlay).

Adding a builder = write an overlay in `<overlay-dir>/<id>.md` + fill a copy of this charter
template. The base is inherited; don't restate it.
