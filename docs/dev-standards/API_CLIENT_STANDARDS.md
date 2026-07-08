# API_CLIENT_STANDARDS.md — External API client convention lens

status: canonical.
scope: **repo-agnostic.** Applies to any code that calls an external HTTP API —
data providers, SaaS APIs, LLM APIs, sync targets. Consumed as a QA convention
lens per `QA_LENSES.md` §3 — the `## Review Checklist` tail is the enforced
surface; the prose teaches. Scraping (HTML extraction from pages not offering an
API contract) is governed by its own lens; this one covers structured APIs.

---

## One client per provider, one interface per role

- **All calls to a provider go through its client module.** No raw
  `requests.get` scattered through task, skill, or pipeline code — the client is
  where auth, throttling, retries, and normalization live, once.
- **Clients return normalized shapes, not raw provider payloads.** The pipeline
  consumes a domain interface (`fetch_ohlcv(...) -> list[dict]` with named,
  typed fields); provider-specific field names and quirks die at the client
  boundary. Raw-response methods may exist internally, but nothing outside the
  client parses provider JSON.
- **Interchangeable providers implement the same interface.** Swapping or adding
  a source changes the client wiring, never the consumers. Where multiple clients
  exist, shared transport mechanics (session construction, retry adapter) live in
  a common base.
- **Behavior is config, not call sites.** Base URLs, tier/rate parameters,
  timeouts, model names, batch sizes — declared in the repo's config layer.
  Changing a tier or a model is a config edit, never a code hunt.

## Transport discipline

- **Every request carries an explicit timeout.** A request without a timeout is
  a hang waiting for a quiet night — this is a blocking finding, no exceptions.
- **Retries are bounded, backed off, and transient-only.** Retry on 429 and
  5xx; respect `Retry-After`; exponential backoff with a hard attempt ceiling.
  Never retry 400/401/403/404 — those are bugs or auth failures and must fail
  loud immediately, not burn the retry budget.
- Prefer the session-level retry adapter (configure once, applies to every call)
  over hand-rolled retry loops. A hand-rolled loop is acceptable only when it
  meets the same contract: bounded attempts, backoff, transient-only, and
  **raises on exhaustion** — it never falls out the bottom returning nothing.
- **Every response's status is handled.** `raise_for_status()` or explicit
  status branching on every call path; no consuming a body without checking what
  kind of body it is.
- **Single throttle point per provider.** Pacing lives in the client (decorator
  or session), config-driven, with a no-op path for tiers that don't need it.
  `sleep()` calls at call sites encode a rate limit nobody can find or tune.

## Pagination

- Follow the provider's cursor or next-link mechanism; never reconstruct page
  URLs by arithmetic when the API hands you the link.
- **Every pagination loop is bounded by a safety cap.** An unbounded
  `while next_page:` against an external system is an infinite loop with extra
  steps. Hitting the cap is logged loudly (WARNING) with what was truncated —
  never a silent partial result.

## Auth and secrets

- Credentials enter at client construction, from environment or the repo's
  secrets mechanism. Constructing a client **without a credential raises** — no
  placeholder defaults that let an unauthenticated client limp along until a
  confusing 401 three layers deeper.
- Credentials never appear in logs, exception messages, or error reports. When a
  provider forces credentials into URLs or params, log the endpoint, never the
  full URL.
- Error bodies included in exceptions are **truncated** (a few hundred chars) —
  provider error pages can be huge and can echo request contents.

## Failure semantics — never fail silent, applied to clients

- **"No data" and "failure" are different values.** An empty result for a
  legitimate, known reason (holiday, empty page, no matches) is an explicit,
  documented return. A failed call raises. Returning an empty result from an
  exception handler is the canonical silent failure — it converts an outage into
  quietly missing data.
- **Schema drift fails loud.** Required fields missing or retyped in a response
  raise or surface a defect signal; they are never silently defaulted into
  plausible-looking rows. `.get()` chains with defaults are for genuinely
  optional fields only.
- Partial success is reported as partial: counts of fetched/failed/skipped
  surfaced to the caller and the log, never rolled up into an unqualified
  success.

## Writes to external systems

- **Write calls are re-run safe.** Reconcile-by-key (fetch existing, compare,
  create/update/skip) or provider idempotency keys — a re-run after partial
  failure must not duplicate remote entities.
- **Destructive operations are opt-in, never a side effect.** A sync's default
  posture is additive; deletion/pruning of remote data requires an explicit flag
  and is reported item-by-item.
- **Consequential external writes support a dry-run or staging mode**
  (stage-then-approve): the client can report what it *would* change without
  changing it. Bulk writes without a preview path are a finding.

## Metered APIs

- Cost-bearing calls (LLM APIs, paid data tiers) pin their model/tier/plan in
  config, sized to the task — classification does not ride the flagship model.
  Escalating the default tier is an operator decision, not a convenience edit.
- Quota-consuming batch work is windowed deliberately and the window is logged,
  so a runaway loop is visible in the bill *and* the log.

## Testing

- Clients are tested against **mocked transport** (mock session, `responses`, or
  equivalent) — no live calls, no real keys in the test suite.
- The failure machinery is itself tested: retry-on-transient, no-retry-on-4xx,
  raise-on-exhaustion, throttle engagement, and the documented empty-result
  cases each have an assertion. Untested retry logic is untested error handling.

---

## Review Checklist

- [ ] No raw HTTP calls to a provider outside its client module.
- [ ] Client public methods return normalized domain shapes; no provider JSON parsed outside the client.
- [ ] Base URLs, rate/tier parameters, timeouts, and model names live in config, not call sites.
- [ ] Every request has an explicit timeout.
- [ ] Retries: bounded attempts, exponential backoff, transient statuses (429/5xx) only, `Retry-After` respected.
- [ ] No retry on 400/401/403/404; these raise immediately.
- [ ] Hand-rolled retry loops raise on exhaustion; no path exits the loop without a result or an exception.
- [ ] Rate pacing lives in the client's single throttle point; no `sleep()` at call sites.
- [ ] Pagination loops carry a safety cap; hitting the cap logs a WARNING naming the truncation.
- [ ] Client construction without a credential raises; no placeholder credential defaults.
- [ ] No credentials or full credentialed URLs in logs or exception text; error bodies truncated.
- [ ] No exception handler returns an empty/default result on a failure path.
- [ ] Legitimate empty-result cases are documented at the method and distinguishable from failure.
- [ ] Missing/retyped required response fields raise or surface a defect signal; not silently defaulted.
- [ ] External write calls are re-run safe (reconcile-by-key or idempotency keys); mechanism named in the docstring.
- [ ] Remote deletion/pruning is behind an explicit opt-in flag; never a default sync behavior.
- [ ] Consequential bulk writes have a dry-run/staging path.
- [ ] Metered API calls pin model/tier in config, sized to the task.
- [ ] Client tests mock the transport; retry, throttle, and empty-result behaviors each have assertions.
