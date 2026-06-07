#!/usr/bin/env python3
"""
devloop skill — read-only narration of dev-loop state from git/GitHub.

Commands:
  standup          Emit a dev-crew standup: queue items, open PRs, QA status,
                   recent merges — all mapped to personas via the roster.
                   Called by the morning briefing and on operator request.

Reads:
  - dev-queue branch: knowledge/dev-notes/queue/*.md (items by status)
  - GitHub: open PRs, check status via gh CLI
  - git log main: recent merges (last 7 days)
  - knowledge/dev-crew/roster.md: persona → repo mapping

No writes. Gracefully degrades: if gh is unavailable or dev-queue doesn't
exist, emits whatever state it can read.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
ROSTER_FILE = PROJECT / "knowledge/dev-crew/roster.md"
DEVNOTES_QUEUE = "knowledge/dev-notes/queue"
DEVNOTES_BACKLOG = "knowledge/dev-notes/backlog"
DEV_QUEUE_BRANCH = "dev-queue"
RECENT_MERGE_DAYS = 7


# ── roster loader ─────────────────────────────────────────────────────────────


def load_roster() -> dict[str, dict]:
    """Parse roster.md; return {id: fields}. Empty dict on missing file."""
    if not ROSTER_FILE.exists():
        return {}
    text = ROSTER_FILE.read_text(encoding="utf-8")
    personas: dict[str, dict] = {}
    for block in re.finditer(r"##\s+(.+?)\n+```yaml\n(.*?)```", text, re.DOTALL):
        raw = block.group(2)
        fields: dict = {}
        for line in raw.splitlines():
            m = re.match(r"^(\w+):\s*(.+)", line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
        if "id" in fields:
            personas[fields["id"]] = fields
    return personas


def persona_for_repo(roster: dict[str, dict], repo_name: str) -> str | None:
    """Return persona name for a given repo, or None."""
    for p in roster.values():
        repos_raw = p.get("repos", "")
        if repo_name in repos_raw:
            return p.get("name")
    return None


# ── git helpers ───────────────────────────────────────────────────────────────


def run_git(*args: str, capture: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=PROJECT, capture_output=capture, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip() if capture else ""


def run_gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], cwd=PROJECT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


# ── frontmatter parser (shared with devqueue) ─────────────────────────────────


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.DOTALL)
    if not m:
        return {}
    fields: dict = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip()
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            fields[key] = val if val not in ("null", "~", "") else None
    return fields


# ── queue reader ──────────────────────────────────────────────────────────────


def read_queue_items() -> list[dict]:
    """Read queue items from dev-queue branch. Returns list of frontmatter dicts."""
    items = []
    try:
        run_git("fetch", "origin", DEV_QUEUE_BRANCH)
    except RuntimeError:
        pass

    try:
        listing = run_git(
            "ls-tree", "--name-only", f"origin/{DEV_QUEUE_BRANCH}", f"{DEVNOTES_QUEUE}/"
        )
    except RuntimeError:
        try:
            listing = run_git("ls-tree", "--name-only", DEV_QUEUE_BRANCH, f"{DEVNOTES_QUEUE}/")
        except RuntimeError:
            return []

    for filepath in listing.splitlines():
        if not filepath.endswith(".md") or filepath.endswith(".gitkeep"):
            continue
        try:
            ref = f"origin/{DEV_QUEUE_BRANCH}"
            content = run_git("show", f"{ref}:{filepath}")
        except RuntimeError:
            try:
                content = run_git("show", f"{DEV_QUEUE_BRANCH}:{filepath}")
            except RuntimeError:
                continue
        fm = parse_frontmatter(content)
        if fm:
            fm["_path"] = filepath
            items.append(fm)
    return items


# ── PR reader ─────────────────────────────────────────────────────────────────


def read_open_prs() -> list[dict]:
    """Return open PRs targeting main with their QA check status."""
    try:
        import json

        raw = run_gh(
            "pr",
            "list",
            "--base",
            "main",
            "--state",
            "open",
            "--json",
            "number,title,headRefName,statusCheckRollup,labels",
        )
        prs = json.loads(raw) if raw else []
    except (RuntimeError, Exception):
        return []

    result = []
    for pr in prs:
        qa_status = "unknown"
        for check in pr.get("statusCheckRollup") or []:
            if "tom" in check.get("name", "").lower() or "qa" in check.get("name", "").lower():
                qa_status = check.get("conclusion") or check.get("status") or "pending"
                break
        result.append(
            {
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "branch": pr.get("headRefName", ""),
                "qa_status": qa_status,
                "labels": [lb.get("name", "") for lb in (pr.get("labels") or [])],
            }
        )
    return result


# ── recent merges ─────────────────────────────────────────────────────────────


def read_recent_merges() -> list[str]:
    """Return merge commit subjects from main in the last N days."""
    try:
        run_git("fetch", "origin", "main")
        since = f"--since={RECENT_MERGE_DAYS} days ago"
        log = run_git(
            "log",
            "origin/main",
            "--merges",
            since,
            "--pretty=format:%s",
        )
        return [line for line in log.splitlines() if line.strip()]
    except RuntimeError:
        return []


# ── standup formatter ─────────────────────────────────────────────────────────


def cmd_standup(_args: list[str]) -> int:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    roster = load_roster()
    repo_name = PROJECT.name

    lines = [f"**Dev-crew standup ({today})**"]

    # Queue items
    queue_items = read_queue_items()
    authorized = [i for i in queue_items if i.get("status") == "authorized"]
    building = [i for i in queue_items if i.get("status") == "building"]
    built = [i for i in queue_items if i.get("status") == "built"]
    proposed = [i for i in queue_items if i.get("status") == "proposed"]

    # Open PRs
    open_prs = read_open_prs()
    # Map PR branch → PR info
    pr_by_branch: dict[str, dict] = {pr["branch"]: pr for pr in open_prs}

    # Active builds (building items matched to PRs)
    if building:
        for item in building:
            item_id = item.get("id", "?")
            branch = f"feature/{item_id}"
            persona_name = persona_for_repo(roster, repo_name) or "Builder"

            pr = pr_by_branch.get(branch)
            if pr:
                qa = pr["qa_status"]
                qa_tag = {
                    "success": "✅ QA pass",
                    "failure": "🚫 QA fail",
                    "pending": "⏳ QA running",
                    "unknown": "⏳ QA pending",
                }.get(qa.lower(), f"QA: {qa}")
                lines.append(
                    f"• **{persona_name}**: building `{item_id}` — PR #{pr['number']} ({qa_tag}, awaiting operator merge)"
                )
            else:
                lines.append(f"• **{persona_name}**: building `{item_id}` — no PR yet")

    # Built, awaiting QA/merge
    if built:
        for item in built:
            item_id = item.get("id", "?")
            branch = f"feature/{item_id}"
            pr = pr_by_branch.get(branch)
            if pr:
                qa = pr["qa_status"]
                qa_tag = {
                    "success": "✅ QA pass — awaiting operator merge",
                    "failure": "🚫 QA fail — needs fixes",
                    "pending": "⏳ QA running",
                    "unknown": "⏳ QA pending",
                }.get(qa.lower(), f"QA: {qa}")
                lines.append(f"• PR #{pr['number']} `{item_id}`: {qa_tag}")

    # Authorized, awaiting summon
    if authorized:
        slugs = ", ".join(f"`{i.get('id', '?')}`" for i in authorized)
        lines.append(f"• Authorized, awaiting summon: {slugs}")

    # Proposed, awaiting authorization
    if proposed:
        slugs = ", ".join(f"`{i.get('id', '?')}`" for i in proposed)
        lines.append(f"• Proposed (not yet authorized): {slugs}")

    # Unmatched open PRs (dev-loop PRs not in queue — edge case)
    devloop_prs = [
        pr
        for pr in open_prs
        if "dev-loop" in pr.get("labels", []) or pr["branch"].startswith("feature/")
    ]
    matched_branches = {f"feature/{i.get('id', '')}" for i in building + built}
    unmatched = [pr for pr in devloop_prs if pr["branch"] not in matched_branches]
    for pr in unmatched:
        lines.append(f"• PR #{pr['number']} `{pr['branch']}`: open (no queue item match)")

    # Recent merges
    merges = read_recent_merges()
    if merges:
        lines.append(f"• Recent merges ({RECENT_MERGE_DAYS}d): " + "; ".join(merges[:3]))

    # Nothing active
    if len(lines) == 1:
        lines.append("• Nothing active — queue is clear.")

    print("\n".join(lines))
    return 0


# ── entry point ───────────────────────────────────────────────────────────────


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
