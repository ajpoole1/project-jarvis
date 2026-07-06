#!/usr/bin/env python3
"""
devloop skill — read-only narration of dev state from GitHub across all repos.

Commands:
  standup          Emit a dev standup: open PRs and latest merged PRs across all
                   repos (personal + org), fresh-work indicator. Queue support removed
                   as of 2026-07-04.

Reads:
  - GitHub: open PRs, merged PRs (last 7 days), QA status via gh CLI
  - All repos: ajpoole1/* (personal) + altaforma-conseils/* (org)

No writes. Gracefully degrades: if gh is unavailable, emits empty standup.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

RECENT_MERGE_DAYS = 7
REPO_OWNERS = ["ajpoole1", "altaforma-conseils"]


def run_gh(*args: str) -> str:
    """Run gh CLI command, raise on error."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def read_open_prs() -> list[dict]:
    """Read open PRs across all owners in one gh search call."""
    owner_query = " ".join(f"owner:{o}" for o in REPO_OWNERS)
    try:
        raw = run_gh(
            "search",
            "prs",
            "--state",
            "open",
            "--base",
            "main",
            "--limit",
            "50",
            "--json",
            "repository,number,title,headRefName,statusCheckRollup,labels",
            owner_query,
        )
    except RuntimeError:
        return []

    try:
        items = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []

    prs = []
    for pr in items:
        repo_name = (pr.get("repository") or {}).get("nameWithOwner", "")
        qa_status = "unknown"
        for check in pr.get("statusCheckRollup") or []:
            name = check.get("name", "") if isinstance(check, dict) else ""
            if "tom" in name.lower() or "qa" in name.lower():
                qa_status = check.get("conclusion") or check.get("status") or "pending"
                break
        prs.append(
            {
                "repo": repo_name,
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "branch": pr.get("headRefName", ""),
                "qa_status": qa_status,
                "labels": [lb.get("name", "") for lb in (pr.get("labels") or [])],
            }
        )
    return prs


def read_recent_merges() -> list[dict]:
    """Read merged PRs (last N days) across all owners in one gh search call."""
    owner_query = " ".join(f"owner:{o}" for o in REPO_OWNERS)
    cutoff_dt = datetime.now(UTC) - timedelta(days=RECENT_MERGE_DAYS)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        raw = run_gh(
            "search",
            "prs",
            "--state",
            "merged",
            "--base",
            "main",
            "--limit",
            "50",
            "--json",
            "repository,number,title,mergedAt",
            f"merged:>={cutoff_str}",
            owner_query,
        )
    except RuntimeError:
        return []

    try:
        items = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []

    merges = []
    for pr in items:
        merged_at = pr.get("mergedAt") or ""
        if not merged_at:
            continue
        repo_name = (pr.get("repository") or {}).get("nameWithOwner", "")
        merges.append(
            {
                "repo": repo_name,
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "mergedAt": merged_at,
            }
        )
    return sorted(merges, key=lambda x: x["mergedAt"], reverse=True)


def cmd_standup(_args: list[str]) -> int:
    """Emit standup: open PRs + latest merged PRs across all repos."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"**Dev standup ({today})**"]

    # Open PRs
    try:
        open_prs = read_open_prs()
    except RuntimeError:
        open_prs = []

    if open_prs:
        lines.append(f"\n**Open PRs ({len(open_prs)})**")
        for pr in sorted(open_prs, key=lambda x: (x["repo"], -x["number"])):
            qa_tag = {
                "success": "✅",
                "failure": "🚫",
                "pending": "⏳",
                "unknown": "⏳",
            }.get(pr["qa_status"].lower(), f"({pr['qa_status']})")
            lines.append(f"• `{pr['repo']}` PR #{pr['number']} {qa_tag}: {pr['title']}")

    # Recent merges
    try:
        merges = read_recent_merges()
    except RuntimeError:
        merges = []

    if merges:
        latest_merge = merges[0]
        lines.append("\n**Latest work**")
        lines.append(
            f"• `{latest_merge['repo']}` PR #{latest_merge['number']}: "
            f"{latest_merge['title']} (merged {latest_merge['mergedAt'][:10]})"
        )
        if len(merges) > 1:
            lines.append(f"  ...{len(merges) - 1} other merges in last {RECENT_MERGE_DAYS}d")

    # Nothing
    if len(lines) == 1:
        lines.append("• Nothing active — no open PRs.")

    print("\n".join(lines))
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: skill.py standup", file=sys.stderr)
        return 1

    cmd, *rest = sys.argv[1:]
    if cmd == "standup":
        return cmd_standup(rest)
    print(f"Unknown command '{cmd}'; use standup", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
