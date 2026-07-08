# SCRAPING_STANDARDS.md — Web scraping convention lens

status: canonical.
scope: **repo-agnostic.** Governs extraction of data from web pages and endpoints
that offer **no structured API contract** — HTML pages, storefront search
endpoints, undocumented JSON. **Boundary rule:** if the provider publishes a
structured API or bulk export for the data, use it, and `API_CLIENT_STANDARDS.md`
governs. A requests-based scraper hitting an uncontracted endpoint is scraping —
governed here — but it **inherits the transport discipline of
`API_CLIENT_STANDARDS.md` wholesale** (timeouts, bounded retries, single
throttle point, credential rules). This lens adds only what extraction adds.
Consumed as a QA convention lens per `QA_LENSES.md` §3.

---

## Prefer the contract that exists

- **Bulk and export endpoints beat per-item requests.** If a target offers a
  bulk file, a sitemap, a feed, or an export, one download replaces thousands of
  page fetches — cheaper for both sides. Per-item scraping of a target with a
  bulk path is a finding.
- Per-target posture is **declared in config, not implied by code**: base URL,
  pacing, concurrency (default 1), coverage window, run budget. Adding or
  retuning a target is a config change.

## Conduct

- **Never bypass access controls.** No CAPTCHA solving or evasion, no login-wall
  or paywall circumvention, no rotating identity to dodge blocks. If a target
  deploys countermeasures, stopping is the default and continuing is an operator
  decision made outside the code — not an engineering workaround inside it.
- Each target's terms posture is a **documented operator decision** recorded
  with the target's config — the scraper's existence is a choice someone made on
  purpose, findable later.
- Pace like a background process, not a burst: configured delays between
  requests, off-peak scheduling, and a runtime budget the run is expected to
  fit. Load-smoothing jitter is fine; pacing exists to be a good citizen, not to
  disguise the client.
- A headless browser is a last resort, used only where static fetching cannot
  render the data — it is an order of magnitude more resource cost per page and
  a larger failure surface.

## Extraction — the page is an interface you don't control

- **Selectors are centralized per target** — one module or config block holds
  every CSS/XPath/JSON-path for a target. When the site redesigns, the fix is
  one file. Selectors inline in loop logic are a finding.
- **Parse defensively, fail loudly.** Every extraction records hit/miss counts.
  A structure change must surface as a failure or WARNING with counts — never as
  a quietly empty run.
- **The zero-result guard:** a target that normally yields many items returning
  zero (or anomalously few, against a configured floor) is a **failure signal,
  not an empty success**. The run goes red or loud; it never writes "nothing
  changed today" over what is actually "the parser broke."
- **Snapshot on parse failure.** When a page or payload fails to parse, persist
  the raw content to a run-scoped location and log the path. Diagnosis must not
  require re-scraping a page that may have already changed. Raw bulk downloads
  are retained per the repo's run-retention policy.

## Normalization — scraped strings are hostile input

- Scraped values pass through a **single canonical normalizer per vocabulary**
  (finishes, conditions, categories): one map, one module, unit-tested.
- **Unrecognized values map to an explicit fallback AND are logged** for
  accretion into the map — never silently coerced to the nearest guess, never
  dropped. The normalizer's log is how the map grows.
- Prices, dates, and quantities are parsed with locale/currency awareness stated
  in code, not assumed; a value that fails to parse is a counted miss, not a
  zero.

## Writing what you scraped

- Storage discipline is `SQL_STANDARDS.md`'s jurisdiction; this lens adds one
  rule: **scrape output always passes through the delta/dedup gate** —
  write-on-change thresholds and dedup keys live in one tested utility, and no
  scraper writes to an append-only table directly.
- Every run ends with a **shape report** logged and surfaced: targets hit, items
  found, parsed, written, skipped, failed. A human reads one line and knows if
  the run was healthy.

## Testing

- **Parsers are tested against committed fixture pages** — small, sanitized,
  saved copies of real target HTML/payloads. A selector change without a fixture
  exercising it is untested.
- Normalizers and delta logic carry unit tests including the unrecognized-value
  and threshold-edge cases.
- **No live network in the test suite.** Fixtures and mocked transport only.
- When a target redesign breaks the parser, the fix lands **with an updated
  fixture** captured from the new structure — the fixture library tracks
  reality.

---

## Review Checklist

- [ ] Transport rules: the `API_CLIENT_STANDARDS.md` checklist applies to all fetch code in the diff.
- [ ] No per-item scraping of a target that offers a bulk/export path.
- [ ] Per-target posture (URL, pacing, concurrency, budget) in config, not code.
- [ ] No CAPTCHA/login/paywall bypassing or block-evasion mechanics in the diff.
- [ ] Target's terms posture documented alongside its config.
- [ ] Headless browser used only where static fetch demonstrably cannot; justification stated.
- [ ] Selectors centralized per target; none inline in extraction loop logic.
- [ ] Extraction paths count hits/misses; counts logged.
- [ ] Zero/anomalously-low result count fails or warns against a configured floor; never written as a normal empty run.
- [ ] Parse failures snapshot the raw content; path logged.
- [ ] Vocabulary values pass through the canonical normalizer; no inline mapping.
- [ ] Unrecognized values hit the explicit fallback and are logged; none silently coerced or dropped.
- [ ] Scrape writes pass through the tested delta/dedup utility; no direct append-only writes.
- [ ] Run ends with a shape report (found/parsed/written/skipped/failed).
- [ ] Parser changes covered by committed fixtures; fixtures updated with the fix on redesigns.
- [ ] No live network calls in tests.
