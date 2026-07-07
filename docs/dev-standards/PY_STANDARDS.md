# PY_STANDARDS.md — Python conventions

status: canonical. Convention lens — see `QA_LENSES.md` §3 for how this file is consumed.
scope: **repo-agnostic.** No project-specific paths, names, or frameworks. Ground rules for any Python codebase that adopts this standard.

---

## Module anatomy

Every module opens in this order, with no deviation:

1. Module-level docstring (triple-quoted, one or two sentences — what the module does and its key constraint, e.g. "stdlib only").
2. `from __future__ import annotations`
3. Standard-library imports (alphabetical)
4. Third-party imports (alphabetical)
5. Local imports (alphabetical)
6. Module-level constants and config (private names `_UPPER_CASE` for internal constants, `UPPER_CASE` for externally meaningful ones)
7. Functions and classes

Blank line between each group. No imports after module-level code has run.

## Datetime

Use `datetime.now(UTC)` — never `datetime.utcnow()`. `utcnow()` returns a naive datetime that is invisibly wrong when mixed with timezone-aware code. Import `UTC` from `datetime` directly.

```python
from datetime import UTC, datetime

now = datetime.now(UTC)        # correct
now = datetime.utcnow()        # never — naive datetime, wrong under TZ
```

## Never-fail-silent

Errors must surface somewhere a human can see them. Concretely:

- **No bare `except`** — always name the exception class. Catch `Exception` only when re-raising or logging, never to swallow.
- **No swallowed errors** — a caught exception that is not re-raised, logged, or converted to a user-visible message is a latent bug.
- **No success exit on failure paths** — `sys.exit(0)` or a truthy return on a path that represents failure deceives callers.
- **Loud degraded paths** — when a skill cannot do its full job (API down, config missing, partial data), it logs the degraded state and surfaces it to the caller. It does not silently return an empty result.

```python
# correct — specific, logged, re-raised or surfaced
try:
    result = fetch()
except urllib.error.URLError as exc:
    logging.error("fetch failed: %s", exc)
    raise

# wrong — swallows the error
try:
    result = fetch()
except Exception:
    result = None
```

## Naming

- Functions and variables: `snake_case`
- Classes: `PascalCase`
- Module-level private names: `_leading_underscore`
- Constants: `UPPER_SNAKE_CASE`
- No single-letter names outside loop counters (`i`, `j`, `k`) and well-known math variables.
- Boolean names start with `is_`, `has_`, `should_`, `can_`.

## Typing posture

- Use type annotations on all public function signatures (params + return type).
- Private helpers: annotate when the type is non-obvious; skip when it adds noise to a three-line function.
- Prefer `list[T]`, `dict[K, V]`, `tuple[A, B]` over `List`, `Dict`, `Tuple` (PEP 585, Python 3.9+).
- Use `X | None` over `Optional[X]` (PEP 604, Python 3.10+).
- Do not annotate `self` or `cls`.

## Docstrings

One-line docstrings for simple functions: a single imperative sentence, no period.
Multi-line only when the *why* or contract is non-obvious to a reader. Never restate the function name or parameter types — the signature already shows those.

```python
def parse_rule(text: str) -> Rule:
    """Parse a rule string into a Rule object."""  # fine

def parse_rule(text: str) -> Rule:
    """
    Parse a rule string into a Rule object.
    Returns a Rule object.  # wrong — restates the return type annotation
    """
```

## Test conventions

- Test behavior, not implementation. A test that breaks when an internal variable is renamed is testing the wrong thing.
- One concept per test function. Long test bodies with multiple `assert` chains should be split.
- Test failure paths explicitly — a function that raises on bad input needs a test that exercises that raise.
- Name tests `test_<what>_<condition>`, e.g. `test_parse_rule_empty_string`.
- No production-behavior tests deleted or commented out to achieve green. If a test is wrong, fix or replace it; if it is right and failing, fix the code.

## Stdlib-first bias

Prefer the standard library over third-party packages when the stdlib covers the use case adequately. A third-party dependency adds a virtualenv, a pin, and a supply-chain surface. The bar for adding one is: the stdlib alternative would be substantially more code or substantially less correct.

## Classes vs functions

Default to plain functions. Add a class when there is **state** that travels with **behavior**, or when there is a genuine interface with two or more implementations (use `Protocol` or `ABC` for the interface).

A dataclass is appropriate for grouped configuration or structured output — it is not a class with behavior, just named fields. A class that holds one value and has one method is usually better as a function that returns a named tuple.

Protocol earns its keep when:
- Multiple concrete implementations exist (or will concretely exist in this PR).
- The caller depends on the interface, not a concrete type.

Everything else: functions.

---

## Review Checklist

- [ ] Module opens with docstring → `from __future__ import annotations` → stdlib → third-party → local → constants.
- [ ] `datetime.now(UTC)` used; `datetime.utcnow()` absent from the diff.
- [ ] No bare `except` clause.
- [ ] No caught exception that is silently discarded (not re-raised, not logged, not surfaced).
- [ ] No `sys.exit(0)` or truthy return on a path that represents a failure.
- [ ] Degraded/partial-success paths log or surface the degradation — they do not return silently.
- [ ] Public function signatures have type annotations (params + return).
- [ ] No `Optional[X]`, `List[T]`, `Dict[K, V]` — use `X | None`, `list[T]`, `dict[K, V]`.
- [ ] Test functions are named `test_<what>_<condition>`.
- [ ] Every new failure path has a corresponding test.
- [ ] No test deleted or skipped to achieve green.
- [ ] Third-party import added only when stdlib alternative would be substantially worse.
- [ ] New class has state + behavior, or backs a Protocol/ABC with ≥2 implementations; otherwise plain function.
