# AIRFLOW_STANDARDS.md — Airflow convention lens

status: canonical.
scope: **repo-agnostic.** Applies to any repo in which Airflow DAGs are authored.
Consumed as a QA convention lens per `QA_LENSES.md` §3 — the `## Review Checklist`
tail is the enforced surface; the prose teaches. No repo names, paths, or
schedules appear here; those live in each repo's own docs.

---

## DAG files are orchestration only

A DAG file declares structure: which tasks exist and in what order. Nothing else.

- **No business logic in DAG files.** Task implementations live in a dedicated
  package (e.g. `dag_components/`, `plugins/`, `src/`); DAG files import and wire.
- **Pure calculation modules carry no Airflow imports.** The layering is
  `calculations` (pure functions, unit-testable without Airflow) → `tasks`
  (thin `@task` wrappers: read context, call calculations, write results) →
  DAG file (wiring). Test the math without standing up a scheduler.
- DAG files parse on every scheduler heartbeat. Keep them import-light: no heavy
  libraries at module level, no network or database calls at module level, no
  work at parse time. Heavy imports belong inside task functions or in
  implementation modules the DagBag never scans.
- Path manipulation belongs in deployment config (container `PYTHONPATH`, env),
  not per-file relative-path arithmetic. One documented shim at the top of a DAG
  file is tolerable; `os.path.dirname(__file__)` gymnastics are not.

## DAG construction

- DAGs are built through the repo's DAG constructor / factory — one place where
  structural defaults live — never ad-hoc `DAG(...)` blocks with copy-pasted
  `default_args`. Adding a pipeline means passing values, not restating defaults.
  (Behavior as data: project defaults are a subclass or config, not boilerplate.)
- `start_date` is **static and in the past** — never `datetime.now()`, never a
  moving target. Changing a `dag_id` or `start_date` orphans run history; treat
  both as append-only identifiers.
- `catchup=False` unless backfill is the explicit intent of the DAG, in which
  case the choice is documented in the DAG docstring.
- `max_active_runs=1` is the default posture for pipelines writing to a shared
  store. Concurrency above 1 requires the writes to be provably safe under it.
- `default_args` always carries `owner`, `retries`, and `retry_delay`. Retries
  exist for transient faults (network, API blips) — a retry count is not a
  license for flaky logic.
- Schedules are cron strings with a comment stating the human intent
  (`"0 5 * * 1-5"  # 5am UTC weekdays, after market close data lands`).
- Orchestrator-driven sub-DAGs have `schedule=None` and are triggered explicitly.
  Cross-DAG triggers set `wait_for_completion` and `failed_states` so a failed
  child fails the parent — a fire-and-forget trigger is a silent failure path.

## Tasks: TaskFlow, idempotent, interval-anchored

- **TaskFlow (`@task`) for all Python logic.** Classic operators are for
  non-Python actions: triggers, sensors, branches, empties. `PythonOperator`
  wrapping a callable is legacy style; do not write new ones.
- **Every task is safely re-runnable.** A rerun after partial failure must never
  duplicate or corrupt data: inserts are conflict-guarded (`ON CONFLICT DO
  NOTHING` for immutable facts, `DO UPDATE` for deterministic recomputation),
  appends are delta- or existence-checked, file outputs are overwritten
  atomically. State the idempotency mechanism in the task docstring.
- **Data windows come from the logical date** (`data_interval_start` /
  `logical_date` via context), never `datetime.now()`. Wall-clock windows make
  reruns and catchups compute different answers than the original run — the
  quiet death of reproducibility.
- Config that can change between deploys is read **at task execution time, not
  DAG parse time**, when the freshest value matters (import inside the task
  function). Parse-time reads freeze values until the next scheduler restart.

## XCom discipline

XCom is a message channel, not a data plane.

- Payloads are small, bounded, and JSON-serializable: counts, paths, keys,
  status dicts, deliberately-windowed working sets.
- **Never push an unbounded external result set through XCom** (a scrape run, a
  full API dump). Persist it to the store or a file and pass the reference.
- If a payload's size scales with something outside the repo's control (catalog
  size, result count), it is unbounded — restructure.

## Failure behavior — never fail silent, applied to pipelines

- A task that cannot do its job **raises**. No caught-and-logged exceptions that
  let the task exit green; no returning empty results to keep the DAG chart
  clean. Green means the work happened.
- Empty input is an explicit decision, made per task: either a legitimate no-op
  (logged at INFO with the reason, e.g. market holiday) or a raise. Silence is
  never the third option.
- Downstream tasks do not paper over upstream gaps. Missing expected data from a
  prior task is a failure, not a default.
- A degraded path (partial fetch, skipped subset, fallback source) must be loud
  somewhere a human will see it — log at WARNING minimum, and surfaced through
  whatever alerting the repo declares.

## Logging

- Module logger (`log = logging.getLogger(__name__)`); **no `print` in DAG or
  task code.** Print goes to the task log without level, timestamp discipline,
  or filterability.
- Log the shape of the work at INFO: row counts, ticker/item counts, window
  boundaries, durations for long stages. A task log should let a human reconstruct
  what the run did without reading code.
- No secrets, credentials, or PII in log statements — Airflow task logs persist
  and are broadly readable.

## Connections, SQL, and external services

- Credentials via Airflow connections or environment — never in DAG code, task
  code, or the repo.
- SQL through hooks with **parameterized queries**. String-formatted SQL is a
  defect even against a private database — the habit is the vulnerability.
- **Single throttle point** for rate-limited external services: pacing lives in
  the shared client layer, once. `sleep()` calls scattered through task code are
  a smell — they encode a rate limit nobody can find or tune.

---

## Review Checklist

- [ ] DAG files contain wiring only; no business logic, no module-level network/DB calls, no heavy module-level imports.
- [ ] Calculation modules imported by tasks contain no Airflow imports.
- [ ] New DAGs use the repo's DAG constructor/factory; no ad-hoc `DAG(...)` with restated defaults.
- [ ] `start_date` is static and in the past; `catchup=False` or the backfill intent is documented in the DAG docstring.
- [ ] `default_args` carries `owner`, `retries`, `retry_delay`.
- [ ] Cron schedules carry a comment stating human intent.
- [ ] Cross-DAG triggers set `wait_for_completion` and `failed_states`; orchestrated sub-DAGs have `schedule=None`.
- [ ] New Python tasks use `@task` (TaskFlow); no new `PythonOperator` wrapping Python callables.
- [ ] Every new/changed task is idempotent: writes conflict-guarded or delta-checked; mechanism named in the task docstring.
- [ ] Data windows derive from `logical_date` / `data_interval_start`, not `datetime.now()`.
- [ ] Deploy-variable config is read at task execution time where freshness matters.
- [ ] No unbounded external result set passed through XCom; large payloads persisted with a reference passed instead.
- [ ] No exception caught and discarded on a path that exits the task green; empty-input handling is explicit (loud no-op or raise).
- [ ] Module logger used; no `print` in DAG or task code; no secrets/PII in log statements.
- [ ] SQL is parameterized via hooks; no string-formatted SQL in the diff.
- [ ] No `sleep()` in DAG or task code; rate pacing lives in the shared client layer.
