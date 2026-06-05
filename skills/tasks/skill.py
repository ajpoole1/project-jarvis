"""Tasks skill — deadline tracking against the shared jarvis.db deadlines table."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.home() / ".jarvis.env")

DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "jarvis.db"


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print("jarvis.db not found — run the gmail-cleanup skill once to initialise the DB.")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


def cmd_add(title: str, due_date: str, project: str = "", notes: str = "") -> str:
    try:
        date.fromisoformat(due_date)
    except ValueError:
        return f"Invalid date: {due_date} — use YYYY-MM-DD"
    con = _connect()
    cur = con.execute(
        "INSERT INTO deadlines (title, due_date, project, notes) VALUES (?, ?, ?, ?)",
        (title, due_date, project, notes),
    )
    con.commit()
    row_id = cur.lastrowid
    con.close()
    project_label = f" [{project}]" if project else ""
    return f"Deadline added (id={row_id}): **{title}**{project_label} — due {due_date}"


def cmd_list(project: str = "", days: int = 0) -> str:
    con = _connect()
    query = "SELECT id, title, due_date, project, notes FROM deadlines WHERE completed = 0"
    params: list = []
    if project:
        query += " AND project = ?"
        params.append(project)
    if days > 0:
        cutoff = (date.today() + timedelta(days=days)).isoformat()
        query += " AND due_date <= ?"
        params.append(cutoff)
    query += " ORDER BY due_date ASC"
    rows = con.execute(query, params).fetchall()
    con.close()
    if not rows:
        return "No pending deadlines."
    today = date.today()
    lines = []
    for row_id, title, due_date_str, proj, notes in rows:
        due = date.fromisoformat(due_date_str)
        delta = (due - today).days
        if delta < 0:
            urgency = f"**OVERDUE by {-delta}d**"
        elif delta == 0:
            urgency = "**TODAY**"
        elif delta <= 7:
            urgency = f"in {delta}d ⚠️"
        else:
            urgency = f"in {delta}d"
        proj_label = f" [{proj}]" if proj else ""
        notes_label = f" — {notes}" if notes else ""
        lines.append(f"  [{row_id}] {due_date_str} ({urgency}){proj_label} — {title}{notes_label}")
    return "\n".join(lines)


def cmd_complete(deadline_id: int) -> str:
    con = _connect()
    row = con.execute(
        "SELECT title, due_date FROM deadlines WHERE id = ? AND completed = 0",
        (deadline_id,),
    ).fetchone()
    if not row:
        con.close()
        return f"No pending deadline with id={deadline_id}."
    con.execute(
        "UPDATE deadlines SET completed = 1 WHERE id = ?",
        (deadline_id,),
    )
    con.commit()
    con.close()
    return f"Marked complete: **{row[0]}** (was due {row[1]})"


def cmd_upcoming(days: int = 14) -> str:
    """Return JSON list of upcoming deadlines — used by morning briefing."""
    con = _connect()
    today = date.today()
    cutoff = (today + timedelta(days=days)).isoformat()
    rows = con.execute(
        """SELECT id, title, due_date, project, notes
           FROM deadlines
           WHERE completed = 0 AND due_date <= ?
           ORDER BY due_date ASC""",
        (cutoff,),
    ).fetchall()
    con.close()
    result = []
    for row_id, title, due_date_str, project, notes in rows:
        delta = (date.fromisoformat(due_date_str) - today).days
        result.append(
            {
                "id": row_id,
                "title": title,
                "due_date": due_date_str,
                "days_until": delta,
                "project": project,
                "notes": notes,
            }
        )
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "add":
        if len(sys.argv) < 4:
            print("Usage: skill.py add <title> <YYYY-MM-DD> [project] [notes]")
            sys.exit(1)
        t = sys.argv[2]
        d = sys.argv[3]
        p = sys.argv[4] if len(sys.argv) > 4 else ""
        n = sys.argv[5] if len(sys.argv) > 5 else ""
        print(cmd_add(t, d, p, n))
    elif cmd == "list":
        proj = sys.argv[2] if len(sys.argv) > 2 else ""
        days_arg = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        print(cmd_list(proj, days_arg))
    elif cmd == "complete":
        if len(sys.argv) < 3:
            print("Usage: skill.py complete <id>")
            sys.exit(1)
        print(cmd_complete(int(sys.argv[2])))
    elif cmd == "upcoming":
        days_arg = int(sys.argv[2]) if len(sys.argv) > 2 else 14
        print(cmd_upcoming(days_arg))
    else:
        print(f"Unknown command: {cmd}")
        print(
            "Commands: add <title> <date> [project] [notes], list [project] [days], complete <id>, upcoming [days]"
        )
        sys.exit(1)
