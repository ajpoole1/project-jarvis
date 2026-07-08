# QA_LENSES.md — Local review lenses

status: canonical. Defines what `/qa` reviews and how it reports.
scope: repo-agnostic. The *structural* lenses below apply to every repo; *convention* lenses are data, declared per-repo in `CHARTER.md`. Adding a standard never modifies this file or the `/qa` skill — behavior as data.

---

## §1. The model

`/qa` is a fixed procedure fed a variable lens set:

- **Structural lenses (§2)** — hardcoded here, stable, apply everywhere. They require no standards doc to exist.
- **Convention lenses (§3)** — each is a standards document whose **checklist tail** is the injectable review criteria. The repo's charter declares which apply.

The reviewer reads: the work item spec (when one exists), the full diff of the feature branch against `main`, and the checklist tails of the declared convention lenses. It does not read whole standards documents — the checklist tail is the contract surface, sized for context budget.

## §2. Structural lenses

### 2.1 Spec conformance
Diff versus the written intent. Every acceptance criterion met; nothing required omitted; deviations named with severity. *Skipped* when no queued spec exists (author ad-hoc work) — the remaining lenses still run.

### 2.2 Correctness & edges
Logic defects, boundary conditions, unhandled states, off-by-ones, concurrency and ordering assumptions, resource lifecycle (files, connections, locks).

### 2.3 Failure behavior
The never-fail-silent principle as a code lens: bare or over-broad `except`; swallowed errors; silent fallbacks that mask degradation; success exit codes on failure paths; log lines that lie about what happened. A degraded path must be loud somewhere a human will see it.

### 2.4 Security & boundary
Secrets or credentials in the diff; PII in log statements; writes outside the repo's sanctioned paths; unvalidated external input (argv, env, network, file contents); shell construction from strings; new egress.

### 2.5 Test adequacy
New logic carries tests; changed behavior has a changed or new assertion; tests assert *behavior*, not implementation details; failure paths are tested, not just happy paths; no tests deleted or skipped to get to green.

### 2.6 Scope & simplicity
Did the build exceed the spec, and is any mechanism more elaborate than the problem demands: speculative abstraction, unused parameters/hooks, config for things with one value, indirection with a single caller. Over-building is a finding, not a virtue. (Severity: advisory unless the excess creates a defect risk.)

## §3. Convention lenses (data)

**Format contract.** A convention lens is any standards document ending in a section titled exactly `## Review Checklist` — terse, one check per line, each independently verifiable against a diff. Prose above the tail is for the builder to learn from; the tail is what the reviewer enforces. One file, two consumers.

**Declaration.** The repo's `CHARTER.md` carries a `## QA Lenses` section. The first line declares the standards root; subsequent lines list the applicable lens documents:

```
## QA Lenses
standards_root: <path>
- @std/PY_STANDARDS.md
- docs/guides/SKILL_GUIDE.md
```

`standards_root` is the directory holding the shared standards library. In the hub repo (`project-jarvis`) it is a repo-relative path (`docs/dev-standards`). In a consuming repo it is the absolute path to the hub checkout's `docs/dev-standards/` directory. When the standalone dev-standards repo exists (Phase D), migration is one `standards_root` line update per consuming repo.

Lens paths prefixed `@std/` resolve against `standards_root` — `@std/PY_STANDARDS.md` resolves to `<standards_root>/PY_STANDARDS.md`. Unprefixed paths remain repo-root-relative and are used for repo-local guides.

**Fail loud.** An `@std/` path with no `standards_root` declared, or a resolved path that does not exist on disk, is a **blocking configuration finding** in the `/qa` verdict — type `convention`, location `CHARTER.md`. It is never silently skipped.

**Lifecycle.** New standard → write the doc with a checklist tail → add one `@std/` line to the charter. Nothing else changes. A future repo (SQL/Airflow-heavy) declares different lenses in the same slot; the QA apparatus lifts across unmodified.

**Precedence:** where a repo guide's rule conflicts with a shared lens rule, the repo guide wins. Every such override must be declared in the repo guide, naming the superseded lens line and the reason. An undeclared conflict between a repo guide and a declared lens is itself a review finding.

## §4. Output contract

Findings are emitted as JSON in the external-QA vocabulary, so local and external review are commensurate and processed identically:

```json
{
  "spec_conformance": [ {"deviation": "...", "severity": "blocking|advisory"} ],
  "defects":          [ {"type": "bug|edge|security|missing-test|convention", "location": "file:line", "description": "..."} ],
  "architecture_notes": [ "advisory observation ..." ]
}
```

- `defects` are **blocking** by definition. Convention-checklist violations are defects of type `convention`.
- `architecture_notes` are advisory: recorded in the PR body / backlog, never a gate.
- Zero findings is a valid, expected outcome — the reviewer does not manufacture findings to appear thorough.
- After the JSON, a one-paragraph plain-language verdict for the human.

## §5. Verdict semantics inside `/ship`

- Any `defects` entry or blocking `spec_conformance` deviation — the ship **halts**; the session fixes, re-runs checks, and re-invokes `/qa`. Two consecutive failed cycles on the same finding — stop and ask the operator rather than looping.
- Advisory-only — ship proceeds; advisories are appended to the PR description under `## Local QA notes`.

## §6. The `/qa` skill wrapper (implementation spec)

- Location: `.claude/skills/qa/SKILL.md`.
- Frontmatter: `context: fork` (isolated subagent — review deliberation never pollutes the working session), model pinned to the current Sonnet (verify the exact model string against the installed CLI; Sonnet 5 at time of writing), model-invocable (default) so builders self-run it.
- Procedure: locate the spec (queue item for the current branch id, if any) → collect `git diff main...HEAD` → read charter lens declarations → read each checklist tail → review through §2 + declared §3 lenses → emit §4 JSON + verdict.
- No write tools required; the reviewer reads and reports. Fixing is the calling session's job.

## §7. Relationship to external QA (Tom)

Local QA is a **cost and latency filter, not a substitute**. Same-family review shares the builder's blind spots; the different-family CI reviewer remains the fail-closed structural gate on every PR. The success metric for `/qa` is external-QA convergence in one cycle — if Tom routinely finds classes of defect that `/qa` missed, that class becomes a new structural lens or checklist line: the feedback loop is *lens accretion*, mirroring the exemplar-accretion pattern used elsewhere in the stack.
