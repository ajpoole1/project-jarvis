"""Grocery skill — conversational shopping list backed by SQLite."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Unquote and strip inline comments.  Quoted values keep the comment inside.
        for q in ('"', "'"):
            if value.startswith(q):
                end = value.find(q, 1)
                if end != -1:
                    value = value[1:end]
                break
        else:
            # Not quoted: strip trailing inline comment (KEY=VAL # comment)
            for sep in (" #", "\t#"):
                pos = value.find(sep)
                if pos != -1:
                    value = value[:pos].rstrip()
                    break
        if key:
            os.environ.setdefault(key, value)


_load_env(Path.home() / ".jarvis.env")

DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "jarvis.db"
_KNOWLEDGE_ROOT = Path(__file__).parents[2] / "knowledge"
_SKILLS_DIR = Path(__file__).parents[2] / "skills"
_CAL_SKILL = str(_SKILLS_DIR / "calendar" / "skill.py")


def _cal_python() -> str:
    """Resolve the calendar venv interpreter relative to the skills root; fall back to system python3."""
    venv = _SKILLS_DIR / "calendar" / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else "python3"


# ---------------------------------------------------------------------------
# DB — idempotent; does not depend on any other skill's init
# ---------------------------------------------------------------------------


def _init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grocery_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            name_norm  TEXT NOT NULL UNIQUE,
            qty        TEXT,
            store      TEXT,
            note       TEXT,
            status     TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Idempotent migrations
    for _migration in [
        "ALTER TABLE grocery_items ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
        "ALTER TABLE grocery_items ADD COLUMN from_note INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            conn.execute(_migration)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grocery_archive (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id    TEXT NOT NULL,
            name       TEXT NOT NULL,
            qty        TEXT,
            store      TEXT,
            shopped_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS grocery_staples (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            name_norm TEXT NOT NULL UNIQUE
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm(name: str) -> str:
    return name.strip().lower()


def _fmt_item(name: str, qty: str | None, store: str | None) -> str:
    parts = [name]
    if qty:
        parts.append(f"×{qty}")
    if store:
        parts.append(f"({store})")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_add(
    conn: sqlite3.Connection,
    name: str,
    qty: str | None = None,
    store: str | None = None,
    note: str | None = None,
) -> str:
    nn = _norm(name)
    row = conn.execute(
        "SELECT name, qty, store FROM grocery_items WHERE name_norm = ?", (nn,)
    ).fetchone()

    if row:
        # Merge into existing item — only update fields that were explicitly provided
        updates, params = [], []
        if qty is not None:
            updates.append("qty = ?")
            params.append(qty)
        if store is not None:
            updates.append("store = ?")
            params.append(store)
        if note is not None:
            updates.append("note = ?")
            params.append(note)
        if updates:
            params.append(nn)
            conn.execute(
                f"UPDATE grocery_items SET {', '.join(updates)} WHERE name_norm = ?",
                params,
            )
            conn.commit()
        effective_qty = qty if qty is not None else row[1]
        effective_store = store if store is not None else row[2]
        return "✓ " + _fmt_item(row[0], effective_qty, effective_store) + " — updated, next?"

    conn.execute(
        "INSERT INTO grocery_items (name, name_norm, qty, store, note) VALUES (?, ?, ?, ?, ?)",
        (name.strip(), nn, qty, store, note),
    )
    conn.commit()
    return "✓ " + _fmt_item(name.strip(), qty, store) + " — next?"


def cmd_rm(conn: sqlite3.Connection, name: str) -> str:
    nn = _norm(name)
    row = conn.execute("SELECT name FROM grocery_items WHERE name_norm = ?", (nn,)).fetchone()
    if not row:
        return f"'{name}' isn't on the list."
    conn.execute("DELETE FROM grocery_items WHERE name_norm = ?", (nn,))
    conn.commit()
    return f"✓ Removed {row[0]}."


def cmd_set_qty(conn: sqlite3.Connection, name: str, qty: str) -> str:
    nn = _norm(name)
    row = conn.execute("SELECT name FROM grocery_items WHERE name_norm = ?", (nn,)).fetchone()
    if not row:
        return f"'{name}' isn't on the list — add it first."
    conn.execute("UPDATE grocery_items SET qty = ? WHERE name_norm = ?", (qty, nn))
    conn.commit()
    return f"✓ {row[0]} ×{qty}."


def cmd_set_store(conn: sqlite3.Connection, name: str, store: str) -> str:
    nn = _norm(name)
    row = conn.execute("SELECT name FROM grocery_items WHERE name_norm = ?", (nn,)).fetchone()
    if not row:
        return f"'{name}' isn't on the list — add it first."
    conn.execute("UPDATE grocery_items SET store = ? WHERE name_norm = ?", (store, nn))
    conn.commit()
    return f"✓ {row[0]} → {store}."


def cmd_list(conn: sqlite3.Connection, by_store: bool = False) -> str:
    rows = conn.execute("SELECT name, qty, store FROM grocery_items ORDER BY name").fetchall()
    if not rows:
        return "List is empty."

    if not by_store:
        return "\n".join("• " + _fmt_item(n, q, s) for n, q, s in rows)

    groups: dict[str, list[tuple[str, str | None]]] = {}
    for name, qty, store in rows:
        key = store or "Unassigned"
        groups.setdefault(key, []).append((name, qty))

    lines = []
    assigned = sorted(k for k in groups if k != "Unassigned")
    order = assigned + (["Unassigned"] if "Unassigned" in groups else [])
    for store_key in order:
        lines.append(f"**{store_key}**")
        for name, qty in groups[store_key]:
            entry = f"  • {name}"
            if qty:
                entry += f" ×{qty}"
            lines.append(entry)
    return "\n".join(lines)


def cmd_review(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT name, qty, store, note FROM grocery_items ORDER BY name").fetchall()
    if not rows:
        return "List is empty."

    groups: dict[str, list[tuple]] = {}
    for name, qty, store, note in rows:
        key = store or "Unassigned"
        groups.setdefault(key, []).append((name, qty, note))

    lines = []
    needs_attention: list[str] = []
    assigned = sorted(k for k in groups if k != "Unassigned")
    order = assigned + (["Unassigned"] if "Unassigned" in groups else [])
    for store_key in order:
        lines.append(f"**{store_key}**")
        for name, qty, note in groups[store_key]:
            flags = []
            if not qty:
                flags.append("no qty")
            if store_key == "Unassigned":
                flags.append("no store")
            entry = f"  • {name}"
            if qty:
                entry += f" ×{qty}"
            if note:
                entry += f" — _{note}_"
            if flags:
                entry += f"  ⚠️ {', '.join(flags)}"
                needs_attention.append(name)
            lines.append(entry)

    lines.append("")
    if needs_attention:
        lines.append(
            f"⚠️ {len(needs_attention)} item(s) need attention: {', '.join(needs_attention)}"
        )
    else:
        lines.append("✓ All items have store and qty.")
    return "\n".join(lines)


def cmd_shopped(conn: sqlite3.Connection, store: str | None = None) -> str:
    if store:
        rows = conn.execute(
            "SELECT name, qty, store FROM grocery_items WHERE lower(store) = lower(?)",
            (store,),
        ).fetchall()
        if not rows:
            return f"No items assigned to '{store}'."

        trip_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        now_str = datetime.now(UTC).isoformat()
        for name, qty, item_store in rows:
            conn.execute(
                "INSERT INTO grocery_archive (trip_id, name, qty, store, shopped_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (trip_id, name, qty, item_store, now_str),
            )
        conn.execute("DELETE FROM grocery_items WHERE lower(store) = lower(?)", (store,))
        conn.commit()

        note_result = _remove_note_section(store)
        return f"Archived {len(rows)} {store} items, list updated.{note_result}"

    rows = conn.execute("SELECT name, qty, store FROM grocery_items").fetchall()
    if not rows:
        return "List is already empty — nothing to archive."

    trip_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    now_str = datetime.now(UTC).isoformat()
    for name, qty, item_store in rows:
        conn.execute(
            "INSERT INTO grocery_archive (trip_id, name, qty, store, shopped_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (trip_id, name, qty, item_store, now_str),
        )
    conn.execute("DELETE FROM grocery_items")
    conn.commit()

    note_result = _clear_note()
    return f"Archived {len(rows)} items, list cleared — fresh list ready.{note_result}"


def cmd_history(conn: sqlite3.Connection, limit: int = 5) -> str:
    trips = conn.execute(
        """SELECT trip_id, MIN(shopped_at) AS ts, COUNT(*) AS n
           FROM grocery_archive
           GROUP BY trip_id
           ORDER BY ts DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    if not trips:
        return "No shopping history yet."

    lines = []
    for trip_id, ts, count in trips:
        try:
            label = datetime.fromisoformat(ts).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            label = ts[:10]
        items = conn.execute(
            "SELECT name, qty, store FROM grocery_archive WHERE trip_id = ? ORDER BY name",
            (trip_id,),
        ).fetchall()
        lines.append(f"**{label}** — {count} items")
        for name, qty, store in items[:8]:
            lines.append("  • " + _fmt_item(name, qty, store))
        if count > 8:
            lines.append(f"  … +{count - 8} more")
    return "\n".join(lines)


def cmd_usuals(conn: sqlite3.Connection, top: int = 10) -> str:
    rows = conn.execute(
        """SELECT name, COUNT(DISTINCT trip_id) AS trips
           FROM grocery_archive
           GROUP BY lower(name)
           ORDER BY trips DESC
           LIMIT ?""",
        (top,),
    ).fetchall()
    if not rows:
        return "No history yet — shop a few times to see your usuals."
    lines = [f"**Top {top} usuals (by trips)**"]
    lines.extend(f"  • {name} ({trips}×)" for name, trips in rows)
    return "\n".join(lines)


def cmd_staples(conn: sqlite3.Connection, action: str, item: str | None = None) -> str:
    if action == "list":
        rows = conn.execute("SELECT name FROM grocery_staples ORDER BY name").fetchall()
        if not rows:
            return "No staples yet. Add with: staples add <item>"
        lines = ["**Staples**"]
        lines.extend(f"  • {r[0]}" for r in rows)
        return "\n".join(lines)

    if item is None:
        return f"Usage: staples {action} <item>"

    nn = _norm(item)
    if action == "add":
        if conn.execute("SELECT 1 FROM grocery_staples WHERE name_norm = ?", (nn,)).fetchone():
            return f"'{item}' is already a staple."
        conn.execute(
            "INSERT INTO grocery_staples (name, name_norm) VALUES (?, ?)",
            (item.strip(), nn),
        )
        conn.commit()
        return f"✓ '{item}' added to staples."

    if action == "rm":
        row = conn.execute("SELECT name FROM grocery_staples WHERE name_norm = ?", (nn,)).fetchone()
        if not row:
            return f"'{item}' isn't a staple."
        conn.execute("DELETE FROM grocery_staples WHERE name_norm = ?", (nn,))
        conn.commit()
        return f"✓ Removed '{row[0]}' from staples."

    return f"Unknown staples action: {action}"


def cmd_seed(conn: sqlite3.Connection) -> str:
    staples = conn.execute("SELECT name, name_norm FROM grocery_staples").fetchall()
    if not staples:
        return "No staples defined. Add some with: staples add <item>"

    added, skipped = [], []
    for name, nn in staples:
        if conn.execute("SELECT 1 FROM grocery_items WHERE name_norm = ?", (nn,)).fetchone():
            skipped.append(name)
        else:
            conn.execute("INSERT INTO grocery_items (name, name_norm) VALUES (?, ?)", (name, nn))
            added.append(name)
    conn.commit()

    parts = []
    if added:
        parts.append(f"Added {len(added)}: {', '.join(added)}")
    if skipped:
        parts.append(f"Already on list: {', '.join(skipped)}")
    return (". ".join(parts) + ".") if parts else "Nothing to seed."


# ---------------------------------------------------------------------------
# Calendar bridge — subprocess helpers
# ---------------------------------------------------------------------------


def _cal_run(*args: str) -> str:
    try:
        result = subprocess.run(
            [_cal_python(), _CAL_SKILL, *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()
    except Exception as exc:
        return f"Error: {exc}"


def _get_grocery_event() -> tuple[str | None, str]:
    """Return (event_id, description) or (None, error_message)."""
    out = _cal_run("grocery-event")
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None, f"calendar skill returned non-JSON: {out!r}"
    if not data.get("found"):
        return None, data.get("error", "Grocery event not found")
    return data["event_id"], data.get("description", "")


def _cal_set_notes(event_id: str, text: str) -> str | None:
    """Patch the grocery calendar note. Returns error string or None."""
    out = _cal_run("set-notes", event_id, text)
    if out.startswith("Error") or out.startswith("✗"):
        return out
    return None


# ---------------------------------------------------------------------------
# Grocery note format — render and parse
# ---------------------------------------------------------------------------

# Section header prefix in the canonical Jarvis-written note format
_SECTION_PREFIX = "🛒"

# Trailing qty: "item ×2", "item x2", "item ×3 lb", "item ×500 g" — explicit digit pattern
# handles unit-bearing qtys; \S+ would stop at the space before the unit suffix.
_TRAILING_QTY_RE = re.compile(r"^(.+?)\s*[×xX](\d+(?:\.\d+)?(?:\s*[a-zA-Z]+)?)\s*$")

# Leading qty: "2 item" or "500g item" — leading token is digit(s) with optional unit
_LEADING_QTY_RE = re.compile(
    r"^(\d+(?:\.\d+)?(?:kg|g|L|ml|lb|oz|pack|packs)?)\s+(.+)$",
    re.IGNORECASE,
)

# Store hint in trailing parens: "chicken (Costco)" — only accepts alpha + spaces
_STORE_PAREN_RE = re.compile(r"\(([A-Za-z][A-Za-z ]*)\)\s*$")


def _parse_note_items(text: str) -> list[tuple[str, str | None, str | None, bool]]:
    """
    Parse a grocery note (freeform or Jarvis-formatted) into (name, qty, store, store_explicit).

    store_explicit=True: store came from an inline (Store) hint on the line.
    store_explicit=False: store inherited from the nearest preceding 🛒 section header.

    Handles:
      "peaches"              → ("peaches", None, None, False)
      "2 milk"               → ("milk", "2", None, False)
      "milk x2" / "milk ×2" → ("milk", "2", None, False)
      "- chicken (Costco)"   → ("chicken", None, "Costco", True)
      "🛒 IGA"               → section header, sets store context to "IGA"
      "- milk ×2"  (in IGA section) → ("milk", "2", "IGA", False)
    """
    items: list[tuple[str, str | None, str | None, bool]] = []
    current_store: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # Section header
        if line.startswith(_SECTION_PREFIX):
            section = line.lstrip(_SECTION_PREFIX).strip()
            current_store = None if section.lower() == "unassigned" else section
            continue

        # Strip leading bullet/dash/asterisk
        line = re.sub(r"^[-*•]\s*", "", line).strip()
        if not line:
            continue

        # Inline store hint: "chicken (Costco)"
        store: str | None = current_store
        store_explicit = False
        paren_m = _STORE_PAREN_RE.search(line)
        if paren_m:
            hint = paren_m.group(1).strip().title()
            # Accept single-word or known short names; exclude food qualifiers with commas
            if "," not in paren_m.group(1):
                store = hint
                store_explicit = True
                line = line[: paren_m.start()].strip()

        # Trailing qty: "milk ×2" or "milk x2"
        qty: str | None = None
        name = line
        trailing_m = _TRAILING_QTY_RE.match(line)
        if trailing_m:
            name = trailing_m.group(1).strip()
            qty = trailing_m.group(2).strip()
        else:
            leading_m = _LEADING_QTY_RE.match(line)
            if leading_m:
                qty = leading_m.group(1)
                name = leading_m.group(2).strip()

        if not name:
            continue

        items.append((name, qty, store, store_explicit))

    return items


def _render_note(conn: sqlite3.Connection) -> str:
    """Render the current active grocery list as a store-grouped formatted note."""
    rows = conn.execute("SELECT name, qty, store FROM grocery_items ORDER BY name").fetchall()
    if not rows:
        return ""

    groups: dict[str, list[tuple[str, str | None]]] = {}
    for name, qty, store in rows:
        key = store or "Unassigned"
        groups.setdefault(key, []).append((name, qty))

    lines: list[str] = []
    assigned = sorted(k for k in groups if k != "Unassigned")
    order = assigned + (["Unassigned"] if "Unassigned" in groups else [])

    for store_key in order:
        lines.append(f"{_SECTION_PREFIX} {store_key}")
        for name, qty in groups[store_key]:
            lines.append(f"- {name} ×{qty}" if qty else f"- {name}")
        lines.append("")

    return "\n".join(lines).strip()


def _remove_store_section(text: str, store: str) -> str:
    """Remove the named store's section from a formatted note, leaving all others intact."""
    result: list[str] = []
    in_target = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_SECTION_PREFIX):
            section = stripped.lstrip(_SECTION_PREFIX).strip()
            in_target = section.lower() == store.lower()
        if not in_target:
            result.append(line)

    while result and not result[-1].strip():
        result.pop()

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Calendar note side-effect helpers (called from shopped / plan / add dispatch)
# ---------------------------------------------------------------------------


def _remove_note_section(store: str) -> str:
    """Remove store's section from the calendar note. Returns status suffix for caller."""
    event_id, description = _get_grocery_event()
    if event_id is None:
        return f"\n⚠️ Calendar note not updated: {description}"
    if not description:
        return ""
    new_desc = _remove_store_section(description, store)
    err = _cal_set_notes(event_id, new_desc)
    return f"\n⚠️ Note update failed: {err}" if err else "\n✓ Note updated."


def _clear_note() -> str:
    """Clear the grocery calendar note. Returns status suffix for caller."""
    event_id, description = _get_grocery_event()
    if event_id is None:
        return f"\n⚠️ Calendar note not cleared: {description}"
    err = _cal_set_notes(event_id, "")
    return f"\n⚠️ Note clear failed: {err}" if err else "\n✓ Calendar note cleared."


def _append_item_to_note(name: str, qty: str | None, store: str | None) -> str | None:
    """Prepend item to grocery note (before any section headers). Returns error or None."""
    event_id, description = _get_grocery_event()
    if event_id is None:
        return description

    # Case-insensitive dedup: skip if name already present in note (any case)
    name_lower = name.strip().lower()
    if any(
        pname.strip().lower() == name_lower for pname, _, _, _ in _parse_note_items(description)
    ):
        return None

    item_text = name.strip()
    if qty:
        item_text += f" ×{qty}"
    if store:
        item_text += f" ({store})"

    # Prepend: freeform items appear before section headers, keeping store context = None on parse
    new_desc = (item_text + "\n" + description).strip() if description else item_text
    return _cal_set_notes(event_id, new_desc)


# ---------------------------------------------------------------------------
# Sync-note — note → DB capture (heartbeat target)
# ---------------------------------------------------------------------------


def cmd_sync_note(conn: sqlite3.Connection) -> str:
    """Read the current month's grocery note and upsert items into the active list (idempotent).

    Items present in the note are upserted and marked from_note=1.
    Active items previously synced from the note (from_note=1) that no longer appear in the
    note are removed — the note is canonical for this set.  Items added by other routes
    (from-recipe, seed; from_note=0) are never removed by this command.
    """
    event_id, description = _get_grocery_event()
    if event_id is None:
        return f"⚠️ {description}"

    items = _parse_note_items(description) if description.strip() else []

    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    note_norms: set[str] = set()

    for name, qty, store, store_explicit in items:
        nn = _norm(name)
        note_norms.add(nn)
        row = conn.execute(
            "SELECT name, qty, store FROM grocery_items WHERE name_norm = ?", (nn,)
        ).fetchone()

        if row:
            updates, params = [], []
            if qty is not None and row[1] is None:
                updates.append("qty = ?")
                params.append(qty)
            # Only write store from section context if item has no store; explicit hints always win
            if store is not None and (store_explicit or row[2] is None):
                updates.append("store = ?")
                params.append(store)
            updates.append("from_note = 1")
            params.append(nn)
            conn.execute(
                f"UPDATE grocery_items SET {', '.join(updates)} WHERE name_norm = ?",
                params,
            )
            if len(updates) > 1:  # something besides from_note changed
                updated.append(name)
            else:
                skipped.append(name)
        else:
            conn.execute(
                "INSERT INTO grocery_items (name, name_norm, qty, store, from_note)"
                " VALUES (?, ?, ?, ?, 1)",
                (name.strip(), nn, qty, store),
            )
            added.append(name)

    # Remove active items that came from the note but are no longer in it
    removed_rows = conn.execute(
        "SELECT name_norm FROM grocery_items WHERE from_note = 1"
    ).fetchall()
    removed: list[str] = []
    for (nn,) in removed_rows:
        if nn not in note_norms:
            conn.execute("DELETE FROM grocery_items WHERE name_norm = ?", (nn,))
            removed.append(nn)

    conn.commit()

    parts: list[str] = []
    if added:
        parts.append(f"Added {len(added)}: {', '.join(added)}")
    if updated:
        parts.append(f"Updated {len(updated)}: {', '.join(updated)}")
    if removed:
        parts.append(f"Removed {len(removed)} (no longer in note): {', '.join(removed)}")
    if skipped:
        parts.append(f"Skipped {len(skipped)} (unchanged)")
    return "; ".join(parts) if parts else "No changes."


# ---------------------------------------------------------------------------
# Plan — sync + auto-suggest stores + write formatted note back
# ---------------------------------------------------------------------------


def cmd_plan(conn: sqlite3.Connection) -> str:
    """Sync note → DB, auto-assign stores from history, render formatted note, write it back."""
    sync_result = cmd_sync_note(conn)

    # Auto-suggest stores for unassigned items using archive history
    unassigned = conn.execute(
        "SELECT name, name_norm FROM grocery_items WHERE store IS NULL ORDER BY name"
    ).fetchall()

    suggested: list[str] = []
    for name, nn in unassigned:
        hist = conn.execute(
            """SELECT store, COUNT(*) AS c FROM grocery_archive
               WHERE lower(name) = ? AND store IS NOT NULL
               GROUP BY store ORDER BY c DESC LIMIT 1""",
            (nn,),
        ).fetchone()
        if hist:
            conn.execute("UPDATE grocery_items SET store = ? WHERE name_norm = ?", (hist[0], nn))
            suggested.append(f"{name} → {hist[0]}")

    if suggested:
        conn.commit()

    # Mark all items planned
    conn.execute("UPDATE grocery_items SET status = 'planned'")
    conn.commit()

    # Render and write back
    note_text = _render_note(conn)
    event_id, _ = _get_grocery_event()
    note_err: str | None = None
    if event_id:
        note_err = _cal_set_notes(event_id, note_text)
    else:
        note_err = "Grocery calendar event not found"

    lines = [f"Sync: {sync_result}"]
    if suggested:
        lines.append(f"Store auto-assigned: {', '.join(suggested)}")

    still_unresolved = conn.execute(
        "SELECT name, qty, store FROM grocery_items WHERE qty IS NULL OR store IS NULL ORDER BY name"
    ).fetchall()
    if still_unresolved:
        lines.append(f"\n⚠️ {len(still_unresolved)} item(s) need attention:")
        for name, qty, store in still_unresolved:
            flags = []
            if not qty:
                flags.append("no qty")
            if not store:
                flags.append("no store")
            lines.append(f"  • {name}  [{', '.join(flags)}]")

    if note_err:
        lines.append(f"\n⚠️ Note write failed: {note_err}")
    else:
        lines.append("\n✓ Formatted list written to calendar note.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recipe ingredient parser
# ---------------------------------------------------------------------------

_UNITS_RE = re.compile(
    r"^(tsp|tbsp|cups?|g|kg|ml|l|oz|lb|pinch|handful|bunch"
    r"|cloves?|slices?|pieces?|cans?|tins?|packets?|heads?|sticks?|sprigs?)$",
    re.IGNORECASE,
)
_QTY_TOKEN_RE = re.compile(r"^[\d½¼¾⅓⅔⅛⅜⅝⅞/.\-]+$")


def _parse_ingredient(raw: str) -> tuple[str, str | None]:
    """
    Best-effort parse of a recipe ingredient line into (name, qty_or_None).

    Handles:
      "1½ cups risoni"         → ("risoni", "1½ cups")
      "3 eggs"                 → ("eggs", "3")
      "½ zucchini, grated"     → ("zucchini, grated", "½")
      "Parmesan cheese, to serve" → ("Parmesan cheese, to serve", None)
    """
    # Strip trailing parenthetical notes and em-dash clauses
    raw = re.sub(r"\s*\(.*?\)", "", raw).strip()
    raw = re.sub(r"\s*—.*$", "", raw).strip()

    tokens = raw.split()
    if not tokens:
        return raw, None

    if _QTY_TOKEN_RE.match(tokens[0]):
        if len(tokens) >= 3 and _UNITS_RE.match(tokens[1]):
            # "1½ cups risoni" → qty="1½ cups", name="risoni ..."
            return " ".join(tokens[2:]), f"{tokens[0]} {tokens[1]}"
        # "3 eggs" or "½ zucchini, grated"
        return " ".join(tokens[1:]), tokens[0]

    return raw, None


def cmd_from_recipe(conn: sqlite3.Connection, slug: str) -> str:
    slug = slug.removesuffix(".md")
    recipe_path = _KNOWLEDGE_ROOT / "recipes" / f"{slug}.md"
    if not recipe_path.exists():
        return f"Recipe not found: knowledge/recipes/{slug}.md"

    text = recipe_path.read_text()
    in_section = False
    added: list[str] = []
    skipped: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^##\s+Ingredients", stripped):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("## "):
                break
            if stripped.startswith(("- ", "* ")):
                raw = stripped.lstrip("-* ").strip()
                if not raw:
                    continue
                name, qty = _parse_ingredient(raw)
                if not name:
                    continue
                nn = _norm(name)
                if conn.execute(
                    "SELECT 1 FROM grocery_items WHERE name_norm = ?", (nn,)
                ).fetchone():
                    skipped.append(name)
                else:
                    conn.execute(
                        "INSERT INTO grocery_items (name, name_norm, qty) VALUES (?, ?, ?)",
                        (name, nn, qty),
                    )
                    added.append(name + (f" ×{qty}" if qty else ""))

    if not added and not skipped:
        return f"No ingredients found in knowledge/recipes/{slug}.md"

    conn.commit()
    parts = []
    if added:
        parts.append(f"Added {len(added)} from {slug}: {', '.join(added)}")
    if skipped:
        parts.append(f"Already on list: {', '.join(skipped)}")
    return ". ".join(parts) + "."


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(prog="grocery", description="Grocery list manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Add or merge item")
    p_add.add_argument("item")
    p_add.add_argument("--qty", default=None)
    p_add.add_argument("--store", default=None)
    p_add.add_argument("--note", default=None)

    p_rm = sub.add_parser("rm", help="Remove item")
    p_rm.add_argument("item")

    p_sq = sub.add_parser("set-qty", help="Set quantity for existing item")
    p_sq.add_argument("item")
    p_sq.add_argument("qty")

    p_ss = sub.add_parser("set-store", help="Assign store to item")
    p_ss.add_argument("item")
    p_ss.add_argument("store")

    p_list = sub.add_parser("list", help="Show active list")
    p_list.add_argument("--by-store", action="store_true")

    sub.add_parser("review", help="Full list with missing-field flags")

    p_shopped = sub.add_parser("shopped", help="Archive active list and clear it")
    p_shopped.add_argument(
        "--store",
        default=None,
        metavar="STORE",
        help="Partial clear: archive only this store's items and remove its note section",
    )

    p_hist = sub.add_parser("history", help="Show recent trips")
    p_hist.add_argument("limit", nargs="?", type=int, default=5, metavar="N")

    p_usuals = sub.add_parser("usuals", help="Most-frequent items across trips")
    p_usuals.add_argument("--top", type=int, default=10, metavar="N")

    p_staples = sub.add_parser("staples", help="Manage the staples set")
    p_staples.add_argument("action", choices=["list", "add", "rm"])
    p_staples.add_argument("item", nargs="?", default=None)

    sub.add_parser("seed", help="Add all staples to active list (merge-safe)")

    p_fr = sub.add_parser("from-recipe", help="Add ingredients from a recipe file")
    p_fr.add_argument("slug", help="Recipe slug (filename without .md)")

    sub.add_parser("sync-note", help="Capture current grocery calendar note into DB (idempotent)")
    sub.add_parser(
        "plan", help="Sync note, assign stores, render formatted list, write to calendar"
    )
    sub.add_parser(
        "propose-schedule",
        help="Print the schedules propose command to register the daily sync-note job",
    )

    args = parser.parse_args()
    conn = _init_db()
    try:
        if args.cmd == "add":
            result = cmd_add(conn, args.item, args.qty, args.store, args.note)
            print(result)
            note_err = _append_item_to_note(args.item, args.qty, args.store)
            if note_err:
                print(f"⚠️ Calendar note update failed: {note_err}")
        elif args.cmd == "rm":
            print(cmd_rm(conn, args.item))
        elif args.cmd == "set-qty":
            print(cmd_set_qty(conn, args.item, args.qty))
        elif args.cmd == "set-store":
            print(cmd_set_store(conn, args.item, args.store))
        elif args.cmd == "list":
            print(cmd_list(conn, args.by_store))
        elif args.cmd == "review":
            print(cmd_review(conn))
        elif args.cmd == "shopped":
            print(cmd_shopped(conn, args.store))
        elif args.cmd == "history":
            print(cmd_history(conn, args.limit))
        elif args.cmd == "usuals":
            print(cmd_usuals(conn, args.top))
        elif args.cmd == "staples":
            print(cmd_staples(conn, args.action, args.item))
        elif args.cmd == "seed":
            print(cmd_seed(conn))
        elif args.cmd == "from-recipe":
            print(cmd_from_recipe(conn, args.slug))
        elif args.cmd == "sync-note":
            print(cmd_sync_note(conn))
        elif args.cmd == "plan":
            print(cmd_plan(conn))
        elif args.cmd == "propose-schedule":
            print(
                "Run this to register the daily sync-note job (staged, AJ must approve):\n"
                "  python3 skills/schedules/skill.py propose"
                " --skill grocery"
                " --args '[\"sync-note\"]'"
                " --schedule daily@09:00"
                " --description 'Daily grocery note capture from shared calendar'"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
