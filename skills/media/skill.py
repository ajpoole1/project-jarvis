"""Watched-media store — track titles watched/tried by person, filter seen from recommendations.

No virtualenv needed: stdlib only.

Phase 2 follow-up: wire the `filter` command into live recommendation surfaces
(morning-briefing reads-coda, seasonal docs). Phase 1 = store + filter API + migration only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

_env_path = Path.home() / ".jarvis.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

_DB_PATH = Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"

VERDICTS = frozenset({"loved", "ok", "bounced", "watching"})
KINDS = frozenset({"tv", "movie", "book", "game"})

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def _init_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            person      TEXT NOT NULL,
            title       TEXT NOT NULL,
            title_norm  TEXT NOT NULL,
            verdict     TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'tv',
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    _seed(conn)
    return conn


# ---------------------------------------------------------------------------
# Title normalization
# ---------------------------------------------------------------------------

_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]")


def _normalize(title: str) -> str:
    t = title.lower().strip()
    t = _PUNCT.sub("", t)
    t = _LEADING_ARTICLE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---------------------------------------------------------------------------
# Idempotent seed from knowledge/personal/summer-2026-lake.md "Watched / tried"
# ---------------------------------------------------------------------------

_SEED: list[tuple[str, str, str, str]] = [
    ("Poli", "The Summer I Turned Pretty", "loved", "tv"),
    ("AJ", "Normal People", "bounced", "tv"),
    ("Poli", "Normal People", "bounced", "tv"),
]


def _seed(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC).isoformat()
    for person, title, verdict, kind in _SEED:
        norm = _normalize(title)
        existing = conn.execute(
            "SELECT id FROM media_log WHERE person = ? AND title_norm = ?",
            (person, norm),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO media_log (person, title, title_norm, verdict, kind, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, '', ?)",
                (person, title, norm, verdict, kind, now),
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_add(args: list[str]) -> str:
    if len(args) < 3:
        return "Usage: add <person> <title> <verdict> [--kind tv|movie|book|game] [--note TEXT]"
    person = args[0]
    title = args[1]
    verdict = args[2].lower()
    if verdict not in VERDICTS:
        return f"Invalid verdict '{verdict}'. Must be one of: {', '.join(sorted(VERDICTS))}"

    kind = "tv"
    note = ""
    i = 3
    while i < len(args):
        if args[i] == "--kind" and i + 1 < len(args):
            kind = args[i + 1].lower()
            i += 2
        elif args[i] == "--note" and i + 1 < len(args):
            note = args[i + 1]
            i += 2
        else:
            i += 1

    if kind not in KINDS:
        return f"Invalid kind '{kind}'. Must be one of: {', '.join(sorted(KINDS))}"

    norm = _normalize(title)
    conn = _init_db()
    existing = conn.execute(
        "SELECT id FROM media_log WHERE person = ? AND title_norm = ?",
        (person, norm),
    ).fetchone()
    if existing:
        return f"Already logged for {person}: '{title}' (id={existing[0]}). Use `seen` to check."

    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO media_log (person, title, title_norm, verdict, kind, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (person, title, norm, verdict, kind, note, now),
    )
    conn.commit()
    return f"Logged: {person} — {title} [{verdict}/{kind}]"


def cmd_list(args: list[str]) -> str:
    conn = _init_db()
    person_filter = args[0] if args else None
    if person_filter:
        rows = conn.execute(
            "SELECT person, title, verdict, kind, note FROM media_log"
            " WHERE person = ? ORDER BY created_at DESC",
            (person_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT person, title, verdict, kind, note FROM media_log"
            " ORDER BY person, created_at DESC"
        ).fetchall()

    if not rows:
        label = f"for {person_filter}" if person_filter else ""
        return f"No media entries{' ' + label if label else ''}."

    header = f"**Media log{' — ' + person_filter if person_filter else ''}**\n"
    lines = [header]
    for person, title, verdict, kind, note in rows:
        note_str = f" — {note}" if note else ""
        lines.append(f"  {person}: {title} [{verdict}/{kind}]{note_str}")
    return "\n".join(lines)


def cmd_seen(args: list[str]) -> str:
    if not args:
        return "Usage: seen <title>"
    title = " ".join(args)
    norm = _normalize(title)
    conn = _init_db()
    rows = conn.execute(
        "SELECT person, title, verdict, kind FROM media_log WHERE title_norm = ?",
        (norm,),
    ).fetchall()
    if not rows:
        return f"Not seen: {title}"
    lines = [f"**{title}** (norm: '{norm}')"]
    for person, stored_title, verdict, kind in rows:
        lines.append(f"  {person}: {stored_title} [{verdict}/{kind}]")
    return "\n".join(lines)


def cmd_filter(args: list[str]) -> str:
    """Split candidate titles into fresh vs already-seen. Outputs JSON."""
    titles: list[str] = []

    if "--titles" in args:
        idx = args.index("--titles")
        if idx + 1 < len(args):
            titles = [t.strip() for t in args[idx + 1].split(",") if t.strip()]
    else:
        if not sys.stdin.isatty():
            for line in sys.stdin:
                line = line.strip()
                if line:
                    titles.append(line)

    if not titles:
        return json.dumps({"fresh": [], "seen": []})

    conn = _init_db()
    norm_to_original: dict[str, str] = {_normalize(t): t for t in titles}

    _SQLITE_VAR_LIMIT = 999
    norms = list(norm_to_original.keys())
    seen_norms: set[str] = set()
    for i in range(0, len(norms), _SQLITE_VAR_LIMIT):
        batch = norms[i : i + _SQLITE_VAR_LIMIT]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT DISTINCT title_norm FROM media_log WHERE title_norm IN ({placeholders})",
            batch,
        ).fetchall()
        seen_norms.update(r[0] for r in rows)

    fresh = []
    seen = []
    for norm, original in norm_to_original.items():
        (seen if norm in seen_norms else fresh).append(original)

    return json.dumps({"fresh": fresh, "seen": seen}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "add":
        print(cmd_add(sys.argv[2:]))
    elif cmd == "list":
        print(cmd_list(sys.argv[2:]))
    elif cmd == "seen":
        print(cmd_seen(sys.argv[2:]))
    elif cmd == "filter":
        print(cmd_filter(sys.argv[2:]))
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: add <person> <title> <verdict> [--kind K] [--note N]")
        print("          list [person]")
        print("          seen <title>")
        print("          filter [--titles title1,title2,...]")
        sys.exit(1)
