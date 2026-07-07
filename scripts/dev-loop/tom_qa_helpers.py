"""Shared helpers for Tom QA — extracted so they can be unit-tested independently."""

from __future__ import annotations

import json
import re
import sys
import time

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# Pinned explicitly — do not remove the pin or let this fall through to an
# API default, which could silently resolve to an older Sonnet (4.6) family
# model on a client/version bump. AJ specified sonnet-5 for this fallback.
ANTHROPIC_MODEL = "claude-sonnet-5"
RETRY_STATUSES = {429, 503}
MAX_ATTEMPTS = 5
MAX_JSON_ATTEMPTS = 3


def count_changed_lines(diff: str) -> int:
    """Count lines in a unified diff that represent actual additions or deletions.

    Excludes file-header lines (+++ / ---) and context/chunk lines (@@ ... @@).
    """
    count = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            count += 1
    return count


def parse_min_diff_lines(raw: str, default: int = 20) -> int:
    """Parse QA_MIN_DIFF_LINES env var safely, falling back to default on empty or non-integer."""
    stripped = (raw or "").strip()
    if not stripped:
        return default
    try:
        return int(stripped)
    except ValueError:
        return default


def make_skip_findings(changed_lines: int, threshold: int) -> dict:
    """Return a findings dict for a trivial-diff QA skip (no model call made)."""
    return {
        "spec_conformance": [],
        "defects": [],
        "architecture_notes": [
            f"QA skipped — trivial diff ({changed_lines} changed lines, threshold {threshold}). No model call made."
        ],
    }


def is_spec_only_diff(diff: str) -> bool:
    """Return True if every changed file in the diff is a spec/knowledge/doc file."""
    SPEC_PATHS = ("knowledge/", "docs/")
    SPEC_EXTENSIONS = (".spec.md", ".plan.md")
    changed_files = re.findall(r"^(?:---|\+\+\+) [ab]/(.+)$", diff, re.MULTILINE)
    if not changed_files:
        return False
    real_files = [f for f in changed_files if not f.startswith("/dev/null")]
    if not real_files:
        return False
    return all(
        any(f.startswith(p) for p in SPEC_PATHS) or any(f.endswith(e) for e in SPEC_EXTENSIONS)
        for f in real_files
    )


def extract_json_object(raw: str) -> dict | None:
    """Extract and parse the first top-level JSON object found in raw text.

    Returns None if no valid JSON object can be parsed — never raises.
    """
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        return json.loads(m.group(0) if m else raw)
    except json.JSONDecodeError:
        return None


def extract_array(raw: str, key: str) -> list | None:
    """Extract a JSON array for `key` from potentially-truncated JSON text."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*(\[)', raw, re.DOTALL)
    if not m:
        return None
    start = m.start(1)
    depth = 0
    for i, ch in enumerate(raw[start:]):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : start + i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def recover_partial_findings(raw: str) -> dict | None:
    """Attempt partial recovery of spec_conformance + defects from truncated JSON.

    architecture_notes is commonly the last field and gets cut off when a
    model's response exceeds its output budget; the key findings are earlier.
    Returns None if recovery isn't possible.
    """
    sc = extract_array(raw, "spec_conformance")
    df = extract_array(raw, "defects")
    if sc is None or df is None:
        return None
    return {
        "spec_conformance": sc,
        "defects": df,
        "architecture_notes": [
            "[Partial response — model output truncated before architecture_notes; "
            "spec_conformance and defects recovered]"
        ],
    }


def call_gemini_qa(
    api_key: str, system_prompt: str, user_content: str, requests_mod
) -> tuple[dict | None, str | None]:
    """Call Gemini (via the OpenAI-compatible endpoint) with the full Tom retry budget.

    Retries transient HTTP 429/503 and network errors up to MAX_ATTEMPTS times with
    exponential backoff; retries a full model call up to MAX_JSON_ATTEMPTS times if
    the response can't be parsed as JSON.

    Returns (findings, failure_reason). On success, findings is a dict and
    failure_reason is None. On exhaustion, findings is None and failure_reason
    is a short human-readable string naming what actually happened (e.g.
    "5 consecutive HTTP 503 from Gemini") — callers surface this verbatim in
    the PR comment / Discord ping so a fallback is never a silent swap; it is
    a loud, explained degradation.
    """
    payload = {
        "model": "gemini-3.5-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 65536,
    }

    for json_attempt in range(1, MAX_JSON_ATTEMPTS + 1):
        resp = None
        last_status = None
        net_err_desc = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = requests_mod.post(
                    GEMINI_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=120,
                )
            except requests_mod.exceptions.RequestException as net_err:
                net_err_desc = str(net_err)
                delay = 2 * (2 ** (attempt - 1))
                if attempt < MAX_ATTEMPTS:
                    print(
                        f"Tom network error (attempt {attempt}/{MAX_ATTEMPTS}): {net_err} "
                        f"— retrying in {delay}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                reason = f"{MAX_ATTEMPTS} consecutive network errors from Gemini ({net_err_desc})"
                print(
                    f"Tom network error persisted after {MAX_ATTEMPTS} attempts: {net_err} "
                    "— exhausting Gemini retry budget",
                    file=sys.stderr,
                )
                return None, reason
            last_status = resp.status_code
            if resp.status_code not in RETRY_STATUSES:
                break
            delay = 2 * (2 ** (attempt - 1))
            if attempt < MAX_ATTEMPTS:
                print(
                    f"Tom transient {resp.status_code} (attempt {attempt}/{MAX_ATTEMPTS}) "
                    f"— retrying in {delay}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
            else:
                print(
                    f"Tom transient {resp.status_code} persisted after {MAX_ATTEMPTS} attempts "
                    "— exhausting Gemini retry budget",
                    file=sys.stderr,
                )

        if resp is None or resp.status_code != 200:
            status_label = last_status if resp is not None else "no response"
            reason = f"{MAX_ATTEMPTS} consecutive HTTP {status_label} from Gemini"
            print(
                f"Tom API error {resp.status_code if resp else 'no response'}: "
                f"{resp.text[:500] if resp else ''}",
                file=sys.stderr,
            )
            return None, reason

        try:
            raw = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            if json_attempt < MAX_JSON_ATTEMPTS:
                print(
                    f"Tom unexpected response shape (attempt {json_attempt}/{MAX_JSON_ATTEMPTS}) "
                    "— retrying",
                    file=sys.stderr,
                )
                continue
            print(
                f"Tom ERROR: unexpected response shape after {MAX_JSON_ATTEMPTS} attempts",
                file=sys.stderr,
            )
            return (
                None,
                f"Gemini returned an unrecognized response shape after {MAX_JSON_ATTEMPTS} attempts",
            )

        findings = extract_json_object(raw)
        if findings is not None:
            return findings, None

        if json_attempt < MAX_JSON_ATTEMPTS:
            print(
                f"Tom JSON parse failed (attempt {json_attempt}/{MAX_JSON_ATTEMPTS}) "
                "— re-requesting model",
                file=sys.stderr,
            )
            continue

        print(
            f"Tom JSON parse failed after {MAX_JSON_ATTEMPTS} attempts — attempting partial recovery.",
            file=sys.stderr,
        )
        print(f"Raw output (first 2000 chars): {raw[:2000]}", file=sys.stderr)
        recovered = recover_partial_findings(raw)
        if recovered is not None:
            print(
                "Tom WARNING: JSON truncated but spec_conformance+defects recovered "
                "— proceeding with partial results.",
                file=sys.stderr,
            )
            return recovered, None
        print(
            f"Tom ERROR: JSON parse and partial recovery both failed after {MAX_JSON_ATTEMPTS} "
            "attempts.",
            file=sys.stderr,
        )
        return (
            None,
            f"Gemini's response could not be parsed as JSON after {MAX_JSON_ATTEMPTS} attempts",
        )

    return None, "Gemini retry budget exhausted"


def call_claude_fallback(
    api_key: str, system_prompt: str, user_content: str, requests_mod
) -> tuple[dict | None, str | None]:
    """Single-attempt Claude fallback call, used only after Gemini's retry budget is exhausted.

    Per DEV_LOOP_REFERENCE §3 invariant #4: this is a resilience valve for outages,
    not a routine reviewer path. No retry loop here by design — if Claude also
    fails, the check stays red (fail-closed holds; there is no third tier, and the
    workflow must not bounce back to Gemini).

    Returns (findings, failure_reason), same contract as call_gemini_qa.
    """
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 8192,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    try:
        resp = requests_mod.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
    except requests_mod.exceptions.RequestException as net_err:
        print(f"Tom fallback (Claude) network error: {net_err}", file=sys.stderr)
        return None, f"Claude fallback network error: {net_err}"

    if resp.status_code != 200:
        print(
            f"Tom fallback (Claude) API error {resp.status_code}: {resp.text[:500]}",
            file=sys.stderr,
        )
        return None, f"Claude fallback returned HTTP {resp.status_code}"

    try:
        raw = resp.json()["content"][0]["text"]
    except (KeyError, IndexError, ValueError):
        print("Tom fallback (Claude) unexpected response shape", file=sys.stderr)
        return None, "Claude fallback returned an unrecognized response shape"

    findings = extract_json_object(raw)
    if findings is not None:
        return findings, None

    print("Tom fallback (Claude) JSON parse failed — attempting partial recovery.", file=sys.stderr)
    recovered = recover_partial_findings(raw)
    if recovered is not None:
        return recovered, None
    return None, "Claude fallback's response could not be parsed as JSON"
