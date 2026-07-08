# SQL_STANDARDS.md — SQL & schema convention lens

status: canonical.
scope: **repo-agnostic, Postgres-first.** Postgres is the reference dialect (all
current server-backed repos run it; the migration target is managed Postgres).
Repos on embedded SQLite follow the same principles; dialect-forced divergences
(type affinity, text dates, `INSERT OR IGNORE`) are declared in that repo's
guide. **Precedence: a repo guide's specific rule beats this lens's general rule
on conflict.** Consumed as a QA convention lens per `QA_LENSES.md` §3 — the
`## Review Checklist` tail is the enforced surface; the prose teaches.

---

## Schema design

- **Primary keys are deliberate.** Use the natural composite key when the domain
  has one (`(ticker, date)`, `(card_id, source_id)`); a surrogate key when it
  doesn't. Never both a surrogate PK *and* an unenforced natural key — if the
  natural key must be unique, say so with a constraint.
- **Nullability is a decision, not a default.** Columns are `NOT NULL` unless
  NULL carries meaning — and when it does, the meaning is documented at the
  column (e.g. outcome fields NULL until resolved).
- **`TIMESTAMPTZ`, never bare `TIMESTAMP`.** A timestamp without a zone is a
  bug that waits for the first machine in a different zone — or the RDS
  migration. Server-side defaults (`DEFAULT NOW()`) for audit columns.
- **`NUMERIC` for money and prices — never `REAL`/`FLOAT`.** Declare scale
  where the domain fixes it (`NUMERIC(10,2)` for currency); bare `NUMERIC`
  where precision requirements vary.
- **Lifecycle columns on stateful tables.** `created_at` on everything;
  `updated_at` / `resolved_at` / `status` where rows have a lifecycle. A row
  whose history can't be reconstructed from its own columns is a support
  ticket in waiting.
- **Enumerated vocabularies are constrained or documented.** A
  `VARCHAR(16)` holding a closed set (`status`, regime labels) gets a `CHECK`
  constraint, a lookup table, or — minimum — the legal values documented at the
  column definition. Free-text enums drift silently.
- **Foreign keys are declared, and indexed.** `REFERENCES` on every relational
  link; Postgres does not auto-index FK columns, so every FK gets an explicit
  index.
- **Index what the queries filter.** Every hot predicate column carries an
  index; partial indexes for hot filtered scans
  (`WHERE resolved_at IS NULL`) where the filtered subset is the working set.
- Naming: `snake_case` throughout; constraint and index names prefixed and
  descriptive (`uq_current_price`, `idx_signal_predictions_unresolved`) — an
  error message naming the constraint should locate the rule without grepping.

## Migrations

- **Schema changes ship as numbered, append-only migration files.** An applied
  migration is never edited — corrections are new migrations. Numbering is the
  ordering contract.
- **Migrations are idempotent-safe.** `IF NOT EXISTS` /
  `ADD COLUMN IF NOT EXISTS` so re-application against an already-migrated
  database is harmless, not fatal.
- **The clean-install schema and the migration land together.** The repo's
  `schema.sql` (truth for a fresh database) is updated in the same PR as the
  migration that brings existing databases forward. Divergence between the two
  is a defect.
- A migration adding columns states **who populates them** (which task, from
  which source) in a comment, and carries the indexes its query patterns need —
  indexes are not a follow-up.

## Writes

- **Every pipeline write is idempotent, and the mechanism is chosen, not
  defaulted.** `ON CONFLICT DO NOTHING` for immutable facts that must never be
  overwritten; `ON CONFLICT DO UPDATE` for deterministic recomputation that is
  safe to re-resolve. The choice is stated where the write lives — the two have
  opposite failure modes.
- **Append-only tables are written only behind an explicit gate** — a delta
  threshold, an existence check, a hash comparison. "Append-only" without a
  gate is "duplicates-on-rerun."
- **Batch writes batch.** Multi-row inserts go through `execute_values`, `COPY`,
  or the dialect's batch mechanism — never a Python loop of single-row inserts.
  Batch size is config.
- **Multi-statement units are transactional.** Related writes commit together
  or not at all; no autocommit sequences that can die halfway and leave a
  half-written state a rerun can't detect.

## Queries

- **Parameterized, always.** No string-formatted or f-string SQL anywhere, in
  any context, against any database — the habit is the vulnerability.
- **No `SELECT *` in production code paths.** Name the columns: it pins the
  contract, survives column additions, and makes schema drift a visible diff
  instead of a silent shape change.
- **Reads from unbounded tables are bounded** — a window, a `LIMIT`, a keyed
  range. A query whose result size scales with table age is a slow-motion
  outage.
- **When the domain's calendar lives in the data, count from the data.**
  Trading days, business days, publication cycles: derive from rows
  (`ORDER BY date LIMIT n`), never calendar arithmetic that guesses at
  holidays.

## Environment & portability

- **One connection utility per repo**, environment-aware (local container vs.
  host vs. managed target). No module-level ad-hoc connects.
- **Stay inside standard, managed-Postgres-compatible SQL.** No
  superuser-dependent features, no server-filesystem dependencies
  (`COPY FROM '/local/path'`) in production paths — the local→RDS move must be
  a connection-string change, not a rewrite.

---

## Review Checklist

- [ ] New tables: deliberate PK; `NOT NULL` default posture; NULL-meaning documented where allowed.
- [ ] `TIMESTAMPTZ` used; no bare `TIMESTAMP` in the diff (Postgres).
- [ ] Money/price columns are `NUMERIC`; no `REAL`/`FLOAT`/`DOUBLE` for monetary values.
- [ ] Stateful tables carry lifecycle columns (`created_at` minimum).
- [ ] Closed-vocabulary columns constrained (`CHECK`/lookup) or values documented at the column.
- [ ] Every FK declared with `REFERENCES` and covered by an index.
- [ ] Hot query predicates indexed; partial index considered for filtered working sets.
- [ ] Schema changes are new numbered migration files; no edits to applied migrations.
- [ ] Migrations use `IF NOT EXISTS` guards; safe to re-apply.
- [ ] `schema.sql` updated in the same PR as any migration.
- [ ] New columns' populating task/source named in a migration comment.
- [ ] Writes use `ON CONFLICT` with a deliberate `DO NOTHING` vs `DO UPDATE` choice, stated at the write site.
- [ ] Append-only writes sit behind an explicit gate (delta/existence/hash check).
- [ ] Multi-row inserts batched (`execute_values`/`COPY`); no row-loop inserts.
- [ ] Multi-statement write units wrapped in a transaction.
- [ ] All SQL parameterized; no string-built SQL in the diff.
- [ ] No `SELECT *` in production code paths.
- [ ] Reads from unbounded tables carry a bound (window/`LIMIT`/keyed range).
- [ ] Domain-calendar counting derives from data rows, not calendar arithmetic.
- [ ] No superuser-only features or server-filesystem paths in production SQL.
