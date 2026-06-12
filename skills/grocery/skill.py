"""Grocery skill — conversational shopping list backed by SQLite."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        for q in ('"', "'"):
            if value.startswith(q) and value.endswith(q) and len(value) >= 2:
                value = value[1:-1]
                break
        if key:
            os.environ.setdefault(key, value)


_load_env(Path.home() / ".jarvis.env")

DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "jarvis.db"
_KNOWLEDGE_ROOT = Path(__file__).parents[2] / "knowledge"


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
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
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


def cmd_shopped(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT name, qty, store FROM grocery_items").fetchall()
    if not rows:
        return "List is already empty — nothing to archive."

    trip_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    now_str = datetime.now(UTC).isoformat()
    for name, qty, store in rows:
        conn.execute(
            "INSERT INTO grocery_archive (trip_id, name, qty, store, shopped_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (trip_id, name, qty, store, now_str),
        )
    conn.execute("DELETE FROM grocery_items")
    conn.commit()
    return f"Archived {len(rows)} items, list cleared — fresh list ready."


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
    sub.add_parser("shopped", help="Archive active list and clear it")

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

    args = parser.parse_args()
    conn = _init_db()
    try:
        if args.cmd == "add":
            print(cmd_add(conn, args.item, args.qty, args.store, args.note))
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
            print(cmd_shopped(conn))
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
    finally:
        conn.close()


if __name__ == "__main__":
    main()
