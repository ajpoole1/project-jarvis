#!/usr/bin/env python3
"""
Knowledge skill — search, update, and append to knowledge/ Markdown files.

Two roots:
  COMMITTED  <repo>/knowledge/        committed to git; generic content (garden, recipes, preferences, projects)
  PRIVATE    ~/.jarvis/knowledge/     local-only, off-git; personal/people/home content (PII tier)

Commands:
  search <query>                      FTS5 search across both roots (graceful if private is absent)
  update <file> <field> <value>       Set a frontmatter field (top-level or dotted nested key)
  append <file> <section> <text>      Append a dated line under a ## section
  stage <file> <op> <target> <text>   Stage a capture for approval (append/update/create)
  pending [n]                         List pending captures
  commit <id|batch_id>                Commit staged captures
  reject <id|batch_id>                Reject staged captures
  undo [n]                            Revert last N committed captures
  capture on|off                      Toggle implicit capture offer

Safety:
  - TIERS.md routes each domain to the correct root; mismatch is rejected
  - All write paths canonicalised with os.path.realpath() to block traversal + symlink escapes
  - Only .md files accepted on writes
  - Atomic writes (temp + os.replace)
  - No delete, no full-file overwrite via agent path
  - Graceful degradation: if PRIVATE root absent, search returns committed results only;
    writes to private domains return a clear error instead of crashing
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

_COMMITTED_ROOT: Path = (Path(__file__).resolve().parent.parent.parent / "knowledge").resolve()
_PRIVATE_ROOT: Path = (Path.home() / ".jarvis" / "knowledge").resolve()
_TIERS_FILE: Path = _COMMITTED_ROOT / "TIERS.md"
_DB_PATH: Path = Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"


# ---------------------------------------------------------------------------
# Tier routing — reads TIERS.md once and caches
# ---------------------------------------------------------------------------

_TIER_CACHE: dict[str, str] | None = None


def _load_tiers() -> dict[str, str]:
    """Parse TIERS.md table and return {domain: 'committed'|'private'}."""
    global _TIER_CACHE
    if _TIER_CACHE is not None:
        return _TIER_CACHE

    tiers: dict[str, str] = {}
    if _TIERS_FILE.exists():
        for line in _TIERS_FILE.read_text(encoding="utf-8").splitlines():
            # Match Markdown table rows: | domain | tier | ... |
            if line.startswith("| ") and not line.startswith("| Domain") and "---" not in line:
                parts = [p.strip() for p in line.strip().strip("|").split("|")]
                if len(parts) >= 2 and parts[1] in ("committed", "private"):
                    tiers[parts[0]] = parts[1]

    _TIER_CACHE = tiers
    return tiers


def _init_capture_db() -> sqlite3.Connection:
    """Initialize the capture staging tables and return a connection."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_pending_writes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            root TEXT NOT NULL,
            file TEXT NOT NULL,
            op TEXT NOT NULL,
            target TEXT NOT NULL,
            proposed_text TEXT NOT NULL,
            prior_value TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            decided_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jarvis_kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _capture_implicit_enabled() -> bool:
    """Check if implicit capture is enabled in jarvis_kv."""
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        row = conn.execute("SELECT value FROM jarvis_kv WHERE key = 'capture_implicit'").fetchone()
        conn.close()
        if row:
            return row[0].lower() in ("true", "on", "1")
        return True  # default: enabled
    except Exception:
        return True  # assume enabled on error


def _domain_of(file_arg: str) -> str:
    """Extract top-level domain component from a relative path like 'people/polina.md'."""
    return Path(file_arg).parts[0] if Path(file_arg).parts else ""


def _root_for_domain(domain: str, tiers: dict[str, str]) -> tuple[str, Path]:
    """Return (tier_name, root_path) for a given top-level domain."""
    tier = tiers.get(domain, "committed")
    root = _PRIVATE_ROOT if tier == "private" else _COMMITTED_ROOT
    return tier, root


# ---------------------------------------------------------------------------
# Path validation (dual-root, tier-routed)
# ---------------------------------------------------------------------------


def _validate_write_path(file_arg: str) -> tuple[Path, str]:
    """
    Resolve the write path and validate it is:
      1. Inside the correct root for its domain's tier
      2. Ends in .md

    Returns (resolved_path, tier_name).
    Raises ValueError on any violation.
    """
    tiers = _load_tiers()
    domain = _domain_of(file_arg)
    tier, expected_root = _root_for_domain(domain, tiers)

    if tier == "private" and not _PRIVATE_ROOT.exists():
        raise ValueError(
            f"Private knowledge store not available on this machine "
            f"(expected at {_PRIVATE_ROOT}). "
            f"Domain '{domain}' is tier=private."
        )

    candidate = Path(file_arg)
    if not candidate.is_absolute():
        candidate = expected_root / candidate

    resolved = Path(os.path.realpath(str(candidate)))

    try:
        resolved.relative_to(expected_root)
    except ValueError as exc:
        raise ValueError(
            f"Path resolves outside its expected root for tier='{tier}': {file_arg!r} "
            f"(resolved to {resolved}, expected under {expected_root})"
        ) from exc

    if resolved.suffix != ".md":
        raise ValueError(f"Only .md files are writable (got {resolved.suffix!r}): {file_arg!r}")

    return resolved, tier


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # Apply 600 perms if writing to the private root
        if str(path).startswith(str(_PRIVATE_ROOT)):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Frontmatter helpers (regex-based, preserves original formatting)
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[str, str, str]:
    """Return (open_fence, fm_content, body). open_fence is '' if no frontmatter."""
    if not text.startswith("---\n"):
        return "", "", text
    end = text.find("\n---\n", 4)
    if end < 0:
        return "", "", text
    return "---\n", text[4:end], text[end + 5 :]


def _coerce_yaml_scalar(value: str) -> str:
    low = value.lower()
    if low in ("true", "yes"):
        return "true"
    if low in ("false", "no"):
        return "false"
    if low in ("null", "none", "~", ""):
        return "null"
    return value


def _replace_top_level_field(fm: str, field: str, yaml_value: str) -> str:
    pattern = re.compile(rf"^({re.escape(field)}:)([ \t]*).*$", re.MULTILINE)
    new_fm, count = pattern.subn(rf"\g<1> {yaml_value}", fm)
    if count == 0:
        raise KeyError(f"Field '{field}' not found in frontmatter")
    return new_fm


def _replace_nested_field(fm: str, parent: str, child: str, yaml_value: str) -> str:
    parent_pat = re.compile(rf"^({re.escape(parent)}:\n)((?:[ \t]+[^\n]*\n?)*)", re.MULTILINE)
    m = parent_pat.search(fm)
    if not m:
        raise KeyError(f"Parent key '{parent}' not found in frontmatter")

    parent_body = m.group(2)
    child_pat = re.compile(rf"^([ \t]+{re.escape(child)}:)([ \t]*).*$", re.MULTILINE)
    new_body, count = child_pat.subn(rf"\g<1> {yaml_value}", parent_body)
    if count == 0:
        raise KeyError(f"Child key '{child}' not found under '{parent}'")

    return fm[: m.start()] + m.group(1) + new_body + fm[m.end() :]


def _update_frontmatter(text: str, field: str, value: str) -> str:
    open_fence, fm_content, body = _split_frontmatter(text)
    if not open_fence:
        raise ValueError("File has no YAML frontmatter")

    yaml_value = _coerce_yaml_scalar(value)
    if "." not in field:
        new_fm = _replace_top_level_field(fm_content, field, yaml_value)
    else:
        parent, _, child = field.partition(".")
        new_fm = _replace_nested_field(fm_content, parent, child, yaml_value)

    return open_fence + new_fm + "\n---\n" + body


# ---------------------------------------------------------------------------
# Section-append helper
# ---------------------------------------------------------------------------


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _append_to_section(text: str, section: str, entry: str) -> str:
    dated_line = f"- {_today()} {entry}"
    section_pat = re.compile(rf"^(## {re.escape(section)})\s*$", re.MULTILINE)
    m = section_pat.search(text)

    if not m:
        return text.rstrip() + f"\n\n## {section}\n\n{dated_line}\n"

    section_end = m.end()
    next_heading = re.search(r"^## ", text[section_end:], re.MULTILINE)
    if next_heading:
        insert_at = section_end + next_heading.start()
        before = text[:insert_at].rstrip()
        after = text[insert_at:]
        return before + f"\n{dated_line}\n\n" + after
    else:
        return text.rstrip() + f"\n{dated_line}\n"


def _get_prior_value(path: Path, op: str, target: str) -> str:
    """Get the current value before write (for undo). Returns '' if file/section/field doesn't exist."""
    if not path.exists():
        return ""

    original = path.read_text(encoding="utf-8")

    if op == "append":
        # For append, capture the current section body (lines under the heading)
        section_pat = re.compile(rf"^(## {re.escape(target)})\s*$", re.MULTILINE)
        m = section_pat.search(original)
        if not m:
            return ""
        section_end = m.end()
        next_heading = re.search(r"^## ", original[section_end:], re.MULTILINE)
        if next_heading:
            return original[section_end : section_end + next_heading.start()].strip()
        else:
            return original[section_end:].strip()
    elif op == "update":
        # For update, capture the current field value from frontmatter
        open_fence, fm_content, _ = _split_frontmatter(original)
        if not open_fence:
            return ""

        if "." not in target:
            pattern = re.compile(rf"^({re.escape(target)}:)([ \t]*)(.*)$", re.MULTILINE)
            m = pattern.search(fm_content)
            return m.group(3).strip() if m else ""
        else:
            parent, _, child = target.partition(".")
            parent_pat = re.compile(
                rf"^({re.escape(parent)}:\n)((?:[ \t]+[^\n]*\n?)*)", re.MULTILINE
            )
            m = parent_pat.search(fm_content)
            if not m:
                return ""
            parent_body = m.group(2)
            child_pat = re.compile(rf"^([ \t]+{re.escape(child)}:)([ \t]*)(.*)$", re.MULTILINE)
            cm = child_pat.search(parent_body)
            return cm.group(3).strip() if cm else ""

    return ""


def _private_git_commit(file_path: Path, message: str) -> bool:
    """Commit a file change to the private tree. Returns True on success."""
    if not _PRIVATE_ROOT.exists():
        return False

    try:
        subprocess.run(
            ["git", "-C", str(_PRIVATE_ROOT), "add", str(file_path)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(_PRIVATE_ROOT), "commit", "-m", message],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _private_git_undo(file_path: Path, prior_value: str) -> bool:
    """Revert a file to prior_value and commit the revert. Returns True on success."""
    if not _PRIVATE_ROOT.exists():
        return False

    try:
        file_path.write_text(prior_value, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(_PRIVATE_ROOT), "add", str(file_path)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(_PRIVATE_ROOT), "commit", "-m", f"undo: revert {file_path.name}"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


# ---------------------------------------------------------------------------
# FTS5 indexing helper
# ---------------------------------------------------------------------------


def _index_root(conn: sqlite3.Connection, root: Path, tier: str) -> None:
    """Index all .md files under root into the FTS5 table."""
    for md_file in sorted(root.rglob("*.md")):
        # Skip TIERS.md and example files from indexing
        rel = md_file.relative_to(root)
        if str(rel) in ("TIERS.md",) or str(rel).startswith("examples/"):
            continue
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        _, _, body_or_all = _split_frontmatter(raw)
        indexable = body_or_all if body_or_all else raw

        chunks = re.split(r"(?m)^(?=## )", indexable)
        for chunk in chunks:
            heading_m = re.match(r"## (.+)", chunk)
            section = heading_m.group(1).strip() if heading_m else "(intro)"
            conn.execute(
                "INSERT INTO docs VALUES (?, ?, ?, ?)",
                (str(rel), section, tier, chunk),
            )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_search(args: list[str]) -> None:
    if not args:
        print("Usage: search <query>", file=sys.stderr)
        sys.exit(1)

    query = " ".join(args)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE docs USING fts5(file UNINDEXED, section UNINDEXED, tier UNINDEXED, content)"
    )

    _index_root(conn, _COMMITTED_ROOT, "committed")

    private_available = _PRIVATE_ROOT.exists()
    if private_available:
        _index_root(conn, _PRIVATE_ROOT, "private")

    conn.commit()

    try:
        rows = conn.execute(
            "SELECT file, section, tier, snippet(docs, 3, '>>>', '<<<', '…', 24) "
            "FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT 10",
            (query,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(json.dumps({"error": str(exc), "query": query}))
        return

    results = [{"file": r[0], "section": r[1], "tier": r[2], "snippet": r[3]} for r in rows]
    output: dict = {"results": results, "query": query, "count": len(results)}
    if not private_available:
        output["warning"] = (
            "Private knowledge store not available on this machine — results from committed tier only."
        )
    print(json.dumps(output, indent=2))


def cmd_update(args: list[str]) -> None:
    if len(args) < 3:
        print("Usage: update <file> <field> <value>", file=sys.stderr)
        sys.exit(1)

    file_arg, field = args[0], args[1]
    value = " ".join(args[2:])

    try:
        path, tier = _validate_write_path(file_arg)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    original = path.read_text(encoding="utf-8")
    try:
        updated = _update_frontmatter(original, field, value)
    except (KeyError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    _atomic_write(path, updated)

    # Build display path relative to its root
    root = _PRIVATE_ROOT if tier == "private" else _COMMITTED_ROOT
    print(
        json.dumps(
            {
                "updated": str(path.relative_to(root)),
                "tier": tier,
                "field": field,
                "new_value": _coerce_yaml_scalar(value),
            }
        )
    )


def cmd_append(args: list[str]) -> None:
    if len(args) < 3:
        print("Usage: append <file> <section> <text>", file=sys.stderr)
        sys.exit(1)

    file_arg, section = args[0], args[1]
    entry = " ".join(args[2:])

    try:
        path, tier = _validate_write_path(file_arg)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    root = _PRIVATE_ROOT if tier == "private" else _COMMITTED_ROOT

    if not path.exists():
        content = f"## {section}\n\n- {_today()} {entry}\n"
        _atomic_write(path, content)
        print(
            json.dumps(
                {
                    "created": str(path.relative_to(root)),
                    "tier": tier,
                    "section": section,
                    "entry": entry,
                }
            )
        )
        return

    original = path.read_text(encoding="utf-8")
    updated = _append_to_section(original, section, entry)
    _atomic_write(path, updated)
    print(
        json.dumps(
            {
                "appended": str(path.relative_to(root)),
                "tier": tier,
                "section": section,
                "entry": entry,
            }
        )
    )


# ---------------------------------------------------------------------------
# Capture staging commands
# ---------------------------------------------------------------------------


def cmd_stage(args: list[str]) -> None:
    if len(args) < 4:
        print(
            "Usage: stage <file> <op> <target> <text> [--source explicit|implicit] [--batch batch_id]",
            file=sys.stderr,
        )
        sys.exit(1)

    file_arg, op, target = args[0], args[1], args[2]

    # Parse text, source, batch from args
    text_args = []
    source = "explicit"
    batch_id = None
    i = 3
    while i < len(args):
        if args[i] == "--source" and i + 1 < len(args):
            source = args[i + 1]
            i += 2
        elif args[i] == "--batch" and i + 1 < len(args):
            batch_id = args[i + 1]
            i += 2
        else:
            text_args.append(args[i])
            i += 1

    proposed_text = " ".join(text_args)
    if not proposed_text:
        print("Error: proposed text required", file=sys.stderr)
        sys.exit(1)

    try:
        path, tier = _validate_write_path(file_arg)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if op not in ("append", "update", "create"):
        print(f"Error: op must be append|update|create, got {op!r}", file=sys.stderr)
        sys.exit(1)

    # Get prior value for undo
    prior_value = _get_prior_value(path, op, target)

    # Initialize DB and insert
    conn = _init_capture_db()
    if not batch_id:
        batch_id = str(uuid.uuid4())[:8]

    created_at = datetime.now(UTC).isoformat()

    try:
        conn.execute(
            """INSERT INTO knowledge_pending_writes
               (batch_id, created_at, source, root, file, op, target, proposed_text, prior_value, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (batch_id, created_at, source, tier, file_arg, op, target, proposed_text, prior_value),
        )
        conn.commit()
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        print(
            json.dumps(
                {
                    "id": row_id,
                    "batch_id": batch_id,
                    "file": file_arg,
                    "op": op,
                    "target": target,
                    "proposed_text": proposed_text,
                }
            )
        )
    finally:
        conn.close()


def cmd_pending(args: list[str]) -> None:
    n = int(args[0]) if args and args[0].isdigit() else None
    conn = _init_capture_db()

    try:
        query = "SELECT id, batch_id, file, op, target, proposed_text, prior_value, status, created_at FROM knowledge_pending_writes WHERE status = 'pending' ORDER BY created_at DESC"
        if n:
            query += f" LIMIT {n}"

        rows = conn.execute(query).fetchall()

        if not rows:
            print("No pending captures.")
            return

        results = []
        for (
            row_id,
            batch_id,
            file,
            op,
            target,
            proposed_text,
            prior_value,
            status,
            created_at,
        ) in rows:
            item = {
                "id": row_id,
                "batch_id": batch_id,
                "file": file,
                "op": op,
                "target": target,
                "proposed_text": proposed_text,
                "status": status,
                "created_at": created_at,
            }
            if op == "update" and prior_value:
                item["prior_value"] = prior_value
            results.append(item)

        print(json.dumps(results, indent=2))
    finally:
        conn.close()


def cmd_commit(args: list[str]) -> None:
    if not args:
        print("Usage: commit <id|batch_id>", file=sys.stderr)
        sys.exit(1)

    id_or_batch = args[0]
    conn = _init_capture_db()

    try:
        # Find all rows matching id or batch_id
        if id_or_batch.isdigit():
            rows = conn.execute(
                "SELECT id, batch_id, root, file, op, target, proposed_text, prior_value FROM knowledge_pending_writes WHERE id = ? AND status = 'pending'",
                (int(id_or_batch),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, batch_id, root, file, op, target, proposed_text, prior_value FROM knowledge_pending_writes WHERE batch_id = ? AND status = 'pending'",
                (id_or_batch,),
            ).fetchall()

        if not rows:
            print(f"No pending captures with id or batch_id {id_or_batch!r}", file=sys.stderr)
            sys.exit(1)

        now = datetime.now(UTC).isoformat()
        results = []

        for row_id, _, tier, file_arg, op, target, proposed_text, _ in rows:
            try:
                path, _ = _validate_write_path(file_arg)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)

            # Execute the write
            if op == "create":
                if path.exists():
                    results.append(
                        {"id": row_id, "status": "error", "reason": "file already exists"}
                    )
                    continue
                content = f"## {target}\n\n- {_today()} {proposed_text}\n"
                _atomic_write(path, content)
            elif op == "append":
                if not path.exists():
                    content = f"## {target}\n\n- {_today()} {proposed_text}\n"
                    _atomic_write(path, content)
                else:
                    original = path.read_text(encoding="utf-8")
                    updated = _append_to_section(original, target, proposed_text)
                    _atomic_write(path, updated)
            elif op == "update":
                if not path.exists():
                    results.append(
                        {"id": row_id, "status": "error", "reason": "file not found for update"}
                    )
                    continue
                original = path.read_text(encoding="utf-8")
                try:
                    updated = _update_frontmatter(original, target, proposed_text)
                    _atomic_write(path, updated)
                except (KeyError, ValueError) as exc:
                    results.append({"id": row_id, "status": "error", "reason": str(exc)})
                    continue

            # If private tier, auto-commit
            commit_ok = True
            if tier == "private":
                if not _private_git_commit(path, f"capture: {file_arg} ({target})"):
                    # Graceful degradation: still mark as committed but note failure
                    commit_ok = False

            # Mark as committed
            conn.execute(
                "UPDATE knowledge_pending_writes SET status = 'committed', decided_at = ? WHERE id = ?",
                (now, row_id),
            )
            conn.commit()

            result = {
                "id": row_id,
                "file": file_arg,
                "status": "committed",
                "op": op,
                "target": target,
            }
            if not commit_ok:
                result["warning"] = "private tree unavailable — file updated but not git-committed"
            results.append(result)

        print(json.dumps(results, indent=2))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def cmd_reject(args: list[str]) -> None:
    if not args:
        print("Usage: reject <id|batch_id>", file=sys.stderr)
        sys.exit(1)

    id_or_batch = args[0]
    conn = _init_capture_db()

    try:
        now = datetime.now(UTC).isoformat()

        if id_or_batch.isdigit():
            cursor = conn.execute(
                "UPDATE knowledge_pending_writes SET status = 'rejected', decided_at = ? WHERE id = ? AND status = 'pending'",
                (now, int(id_or_batch)),
            )
        else:
            cursor = conn.execute(
                "UPDATE knowledge_pending_writes SET status = 'rejected', decided_at = ? WHERE batch_id = ? AND status = 'pending'",
                (now, id_or_batch),
            )

        conn.commit()
        count = cursor.rowcount

        print(json.dumps({"status": "rejected", "count": count, "id_or_batch": id_or_batch}))
    finally:
        conn.close()


def cmd_undo(args: list[str]) -> None:
    n = int(args[0]) if args and args[0].isdigit() else 1
    conn = _init_capture_db()

    try:
        rows = conn.execute(
            "SELECT id, root, file, op, target, prior_value FROM knowledge_pending_writes WHERE status = 'committed' ORDER BY decided_at DESC LIMIT ?",
            (n,),
        ).fetchall()

        if not rows:
            print("No committed captures to undo.")
            return

        results = []
        now = datetime.now(UTC).isoformat()

        for row_id, tier, file_arg, op, target, prior_value in reversed(rows):
            try:
                path, _ = _validate_write_path(file_arg)
            except ValueError as exc:
                results.append({"id": row_id, "status": "error", "reason": str(exc)})
                continue

            if op == "create":
                try:
                    if path.exists():
                        path.unlink()
                except OSError as exc:
                    results.append(
                        {"id": row_id, "status": "error", "reason": f"could not delete: {exc}"}
                    )
                    continue
            elif op == "append":
                try:
                    if path.exists():
                        original = path.read_text(encoding="utf-8")
                        # Remove the dated line matching the proposed text
                        # Build the pattern we expect
                        dated_pattern = f"- {_today()} "
                        lines = original.splitlines(keepends=True)
                        new_lines = [
                            line
                            for line in lines
                            if not (dated_pattern in line and prior_value in line)
                        ]
                        if len(new_lines) < len(lines):  # something was removed
                            updated = "".join(new_lines)
                            _atomic_write(path, updated)
                except OSError as exc:
                    results.append(
                        {"id": row_id, "status": "error", "reason": f"could not undo append: {exc}"}
                    )
                    continue
            elif op == "update":
                try:
                    if path.exists():
                        original = path.read_text(encoding="utf-8")
                        updated = _update_frontmatter(original, target, prior_value)
                        _atomic_write(path, updated)
                except (OSError, KeyError, ValueError) as exc:
                    results.append(
                        {"id": row_id, "status": "error", "reason": f"could not undo update: {exc}"}
                    )
                    continue

            # If private tier, commit the undo
            undo_ok = True
            if tier == "private":
                undo_ok = _private_git_undo(path, prior_value if op in ("update", "append") else "")

            # Mark as undone
            conn.execute(
                "UPDATE knowledge_pending_writes SET status = 'undone', decided_at = ? WHERE id = ?",
                (now, row_id),
            )
            conn.commit()

            result = {"id": row_id, "file": file_arg, "status": "undone", "op": op}
            if not undo_ok and tier == "private":
                result["warning"] = "private tree unavailable — file reverted but not git-committed"
            results.append(result)

        print(json.dumps(results, indent=2))
    finally:
        conn.close()


def cmd_capture(args: list[str]) -> None:
    if not args:
        print("Usage: capture on|off", file=sys.stderr)
        sys.exit(1)

    setting = args[0].lower()
    if setting not in ("on", "off"):
        print("Error: use 'on' or 'off'", file=sys.stderr)
        sys.exit(1)

    conn = _init_capture_db()
    try:
        value = "true" if setting == "on" else "false"
        conn.execute(
            "INSERT OR REPLACE INTO jarvis_kv (key, value) VALUES ('capture_implicit', ?)", (value,)
        )
        conn.commit()

        print(
            json.dumps(
                {
                    "capture_implicit": setting,
                    "message": f"Implicit capture offers are now {setting}",
                }
            )
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: skill.py <command> [args...]\n"
            "Commands:\n"
            "  search <query>\n"
            "  update <file> <field> <value>\n"
            "  append <file> <section> <text>\n"
            "  stage <file> <op> <target> <text> [--source explicit|implicit] [--batch batch_id]\n"
            "  pending [n]\n"
            "  commit <id|batch_id>\n"
            "  reject <id|batch_id>\n"
            "  undo [n]\n"
            "  capture on|off",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1].lower()
    args = sys.argv[2:]

    dispatch = {
        "search": cmd_search,
        "update": cmd_update,
        "append": cmd_append,
        "stage": cmd_stage,
        "pending": cmd_pending,
        "commit": cmd_commit,
        "reject": cmd_reject,
        "undo": cmd_undo,
        "capture": cmd_capture,
    }
    if cmd not in dispatch:
        print(
            f"Unknown command: {cmd!r}. Use: search, update, append, stage, pending, commit, reject, undo, capture",
            file=sys.stderr,
        )
        sys.exit(1)

    dispatch[cmd](args)


if __name__ == "__main__":
    main()
