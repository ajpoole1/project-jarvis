#!/usr/bin/env python3
"""
Knowledge skill — search, update, and append to knowledge/ Markdown files.

Two roots:
  COMMITTED  <repo>/knowledge/        committed to git; generic content (garden, recipes, preferences, projects)
  PRIVATE    ~/.jarvis/knowledge/     local-only, off-git; personal/people/home content (PII tier)

Commands:
  search <query>                  FTS5 search across both roots (graceful if private is absent)
  update <file> <field> <value>   Set a frontmatter field (top-level or dotted nested key)
  append <file> <section> <text>  Append a dated line under a ## section

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
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_COMMITTED_ROOT: Path = (Path(__file__).resolve().parent.parent.parent / "knowledge").resolve()
_PRIVATE_ROOT: Path = (Path.home() / ".jarvis" / "knowledge").resolve()
_TIERS_FILE: Path = _COMMITTED_ROOT / "TIERS.md"


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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: skill.py <search|update|append> [args...]\n"
            "  search <query>\n"
            "  update <file> <field> <value>\n"
            "  append <file> <section> <text>",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1].lower()
    args = sys.argv[2:]

    dispatch = {"search": cmd_search, "update": cmd_update, "append": cmd_append}
    if cmd not in dispatch:
        print(f"Unknown command: {cmd!r}. Use: search, update, append", file=sys.stderr)
        sys.exit(1)

    dispatch[cmd](args)


if __name__ == "__main__":
    main()
