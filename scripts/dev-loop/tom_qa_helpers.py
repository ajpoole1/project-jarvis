"""Shared helpers for Tom QA — extracted so they can be unit-tested independently."""

from __future__ import annotations


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
