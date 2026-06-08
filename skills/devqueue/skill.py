#!/usr/bin/env python3
"""
devqueue skill — validate and push dev-notes queue items to the dev-queue branch.

Commands:
  validate <file>          Validate frontmatter schema of a spec file; print errors
  push <file>              Validate then add to knowledge/dev-notes/queue/ and push
                           to the dev-queue branch ONLY (hard-locked)
  list [queue|backlog|archive]  List items in a queue dir from the dev-queue branch

Schema (REFERENCE §8.1):
  Required fields: id, title, status, scope, origin, author, created
  status enum: proposed|authorized|building|built|merged
  scope enum: well-bounded-local|needs-design-pass
  origin enum: brainstorm|iteration-backlog
  id must match filename (without .md extension)

Security: the push subcommand is physically unable to push to any branch or path
other than dev-queue / knowledge/dev-notes/**. Any attempt to override is rejected
before git is invoked.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
DEVNOTES_ROOT = "knowledge/dev-notes"
TARGET_BRANCH = "dev-queue"

REQUIRED_FIELDS = ["id", "title", "status", "scope", "origin", "author", "created"]
STATUS_VALUES = {"proposed", "authorized", "building", "built", "merged"}
SCOPE_VALUES = {"well-bounded-local", "needs-design-pass"}
ORIGIN_VALUES = {"brainstorm", "iteration-backlog"}


# ── frontmatter parser ────────────────────────────────────────────────────────


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter block from Markdown. Returns (fields, body)."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    fm_block, body = m.group(1), m.group(2)
    fields: dict = {}
    for line in fm_block.splitlines():
        kv = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip()
            # strip inline quotes
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            fields[key] = val if val not in ("null", "~", "") else None
    return fields, body


def validate_spec(path: Path) -> list[str]:
    """Return a list of validation errors (empty = valid)."""
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"Cannot read file: {e}"]

    fields, _ = parse_frontmatter(text)
    if not fields:
        return ["No valid frontmatter block found (expected --- ... ---)"]

    for f in REQUIRED_FIELDS:
        if f not in fields or fields[f] is None:
            errors.append(f"Missing required field: {f}")

    if "status" in fields and fields["status"] not in STATUS_VALUES:
        errors.append(
            f"Invalid status '{fields['status']}'; must be one of {sorted(STATUS_VALUES)}"
        )

    if "scope" in fields and fields["scope"] not in SCOPE_VALUES:
        errors.append(f"Invalid scope '{fields['scope']}'; must be one of {sorted(SCOPE_VALUES)}")

    if "origin" in fields and fields["origin"] not in ORIGIN_VALUES:
        errors.append(
            f"Invalid origin '{fields['origin']}'; must be one of {sorted(ORIGIN_VALUES)}"
        )

    # id must match filename
    if "id" in fields and fields["id"] is not None:
        expected_stem = path.stem
        if fields["id"] != expected_stem:
            errors.append(f"id '{fields['id']}' does not match filename '{expected_stem}.md'")

    # created should be a valid date
    if "created" in fields and fields["created"]:
        try:
            date.fromisoformat(str(fields["created"]))
        except ValueError:
            errors.append(f"Invalid date in 'created': {fields['created']}")

    return errors


# ── git helpers ───────────────────────────────────────────────────────────────


def run_git(*args: str, cwd: Path = PROJECT, capture: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=capture,
        text=True,
    )
    if result.returncode != 0:
        stderr_msg = result.stderr.strip() if result.stderr else "(stderr not captured)"
        raise RuntimeError(f"git {' '.join(args)} failed:\n{stderr_msg}")
    return result.stdout.strip() if capture else ""


def current_branch() -> str:
    return run_git("rev-parse", "--abbrev-ref", "HEAD")


# ── pre-push guard ────────────────────────────────────────────────────────────


def _assert_push_target(dest_branch: str, dest_path: str) -> None:
    """Hard-lock: abort if push target is not dev-queue / knowledge/dev-notes/**."""
    if dest_branch != TARGET_BRANCH:
        raise SystemExit(
            f"SECURITY: push refused — target branch must be '{TARGET_BRANCH}', got '{dest_branch}'"
        )
    resolved = Path(dest_path).resolve()
    allowed = (PROJECT / DEVNOTES_ROOT).resolve()
    try:
        resolved.relative_to(allowed)
    except ValueError:
        raise SystemExit(  # noqa: B904
            f"SECURITY: push refused — destination path must be under {DEVNOTES_ROOT}, got '{dest_path}'"
        )


# ── commands ──────────────────────────────────────────────────────────────────


def cmd_validate(args: list[str]) -> int:
    if not args:
        print("Usage: validate <file>", file=sys.stderr)
        return 1
    path = Path(args[0]).resolve()
    errors = validate_spec(path)
    if errors:
        print(f"INVALID: {path.name}", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"OK: {path.name}")
    return 0


def cmd_push(args: list[str]) -> int:
    if not args:
        print("Usage: push <file>", file=sys.stderr)
        return 1

    src = Path(args[0]).resolve()
    if not src.exists():
        print(f"File not found: {src}", file=sys.stderr)
        return 1

    # Validate first
    errors = validate_spec(src)
    if errors:
        print("Spec validation failed — fix before pushing:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    dest_dir = f"{DEVNOTES_ROOT}/queue"
    dest_rel = f"{dest_dir}/{src.name}"

    # Pre-push guard: hard-lock branch + path
    _assert_push_target(TARGET_BRANCH, str(PROJECT / dest_rel))

    # Fetch latest remote state before any worktree operation.
    try:
        run_git("fetch", "origin", TARGET_BRANCH)
    except RuntimeError:
        print(
            f"Warning: could not fetch origin/{TARGET_BRANCH} — proceeding with local state",
            file=sys.stderr,
        )

    # Check branch existence so we know how to create the worktree.
    local_exists = True
    try:
        run_git("rev-parse", "--verify", TARGET_BRANCH)
    except RuntimeError:
        local_exists = False

    remote_exists = True
    try:
        run_git("rev-parse", "--verify", f"origin/{TARGET_BRANCH}")
    except RuntimeError:
        remote_exists = False

    # All mutations happen in an isolated git worktree so the active builder's
    # working tree is never switched away from its feature branch.
    wt_path = Path(tempfile.mkdtemp(prefix="devqueue-wt-"))
    try:
        if local_exists:
            run_git("worktree", "add", str(wt_path), TARGET_BRANCH)
            if remote_exists:
                # Fast-forward to origin so a push never rejects as non-fast-forward.
                try:
                    run_git("merge", "--ff-only", f"origin/{TARGET_BRANCH}", cwd=wt_path)
                except RuntimeError:
                    print(
                        f"Warning: {TARGET_BRANCH} diverged from origin/{TARGET_BRANCH} — continuing without fast-forward",
                        file=sys.stderr,
                    )
        elif remote_exists:
            run_git("worktree", "add", "-b", TARGET_BRANCH, str(wt_path), f"origin/{TARGET_BRANCH}")
        else:
            raise RuntimeError(
                f"Branch '{TARGET_BRANCH}' does not exist locally or remotely. "
                "Bootstrap it manually: git checkout --orphan dev-queue && git push -u origin dev-queue"
            )

        # Write spec into worktree
        dest_dir_path = wt_path / dest_dir
        dest_dir_path.mkdir(parents=True, exist_ok=True)
        (wt_path / dest_rel).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        run_git("add", dest_rel, cwd=wt_path)
        run_git("commit", "-m", f"queue: add {src.stem}", cwd=wt_path)
        run_git("push", "origin", TARGET_BRANCH, capture=False, cwd=wt_path)

        print(f"Pushed {src.name} to {TARGET_BRANCH}:{dest_rel}")
        return 0

    except (RuntimeError, SystemExit) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            run_git("worktree", "remove", "--force", str(wt_path))
        except RuntimeError:
            shutil.rmtree(str(wt_path), ignore_errors=True)


def cmd_list(args: list[str]) -> int:
    subdir = args[0] if args else "queue"
    if subdir not in ("queue", "backlog", "archive"):
        print(f"Unknown subdir '{subdir}'; use queue|backlog|archive", file=sys.stderr)
        return 1

    target_path = f"{DEVNOTES_ROOT}/{subdir}"

    try:
        run_git("fetch", "origin", TARGET_BRANCH)
    except RuntimeError:
        pass

    try:
        output = run_git("ls-tree", "--name-only", f"origin/{TARGET_BRANCH}", f"{target_path}/")
    except RuntimeError:
        try:
            output = run_git("ls-tree", "--name-only", TARGET_BRANCH, f"{target_path}/")
        except RuntimeError:
            print(f"No items found (branch '{TARGET_BRANCH}' may not exist yet)")
            return 0

    files = [f for f in output.splitlines() if f.endswith(".md") and not f.endswith(".gitkeep")]
    if not files:
        print(f"No items in {subdir}")
        return 0

    items = []
    for filepath in files:
        try:
            content = run_git("show", f"origin/{TARGET_BRANCH}:{filepath}")
            fm, _ = parse_frontmatter(content)
            title = fm.get("title") or Path(filepath).stem
            status = fm.get("status") or "?"
            items.append(f"  [{status}] {Path(filepath).stem} — {title}")
        except RuntimeError:
            items.append(f"  {Path(filepath).stem}")

    print(f"{subdir} ({len(items)}):")
    for item in items:
        print(item)
    return 0


# ── entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: skill.py <validate|push|list> [args...]", file=sys.stderr)
        return 1

    cmd, *rest = sys.argv[1:]
    if cmd == "validate":
        return cmd_validate(rest)
    elif cmd == "push":
        return cmd_push(rest)
    elif cmd == "list":
        return cmd_list(rest)
    else:
        print(f"Unknown command '{cmd}'; use validate|push|list", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
