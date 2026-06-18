"""Tests for grocery ↔ calendar note bridge (2026-0017).

Covers: _parse_note_items, _render_note, _remove_store_section, cmd_sync_note,
        cmd_plan (store auto-assign), cmd_shopped partial clear, round-trip safety,
        and no regressions on existing grocery commands.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load module without running main()
# ---------------------------------------------------------------------------

_GROCERY_PATH = Path(__file__).parents[1] / "skills" / "grocery" / "skill.py"


def _load_grocery():
    spec = importlib.util.spec_from_file_location("grocery_skill", _GROCERY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_g = _load_grocery()

_parse_note_items = _g._parse_note_items
_render_note = _g._render_note
_remove_store_section = _g._remove_store_section
cmd_sync_note = _g.cmd_sync_note
cmd_plan = _g.cmd_plan
cmd_shopped = _g.cmd_shopped
cmd_add = _g.cmd_add
cmd_list = _g.cmd_list


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isolated in-memory DB wired to the module under test."""
    monkeypatch.setattr(_g, "DB_PATH", tmp_path / "jarvis.db")
    conn = _g._init_db()
    return conn


@pytest.fixture()
def cal_noop(monkeypatch):
    """Suppress all calendar subprocess calls."""
    monkeypatch.setattr(_g, "_get_grocery_event", lambda: (None, "no event"))
    monkeypatch.setattr(_g, "_cal_set_notes", lambda *a: None)


# ===========================================================================
# _parse_note_items
# ===========================================================================


class TestParseNoteItems:
    def test_plain_item(self):
        items = _parse_note_items("peaches")
        assert len(items) == 1
        name, qty, store, explicit = items[0]
        assert name == "peaches"
        assert qty is None
        assert store is None
        assert not explicit

    def test_leading_qty(self):
        name, qty, store, _ = _parse_note_items("2 milk")[0]
        assert name == "milk"
        assert qty == "2"

    def test_trailing_x_qty(self):
        name, qty, store, _ = _parse_note_items("milk x2")[0]
        assert name == "milk"
        assert qty == "2"

    def test_trailing_unicode_times_qty(self):
        name, qty, _, _ = _parse_note_items("paper towels ×2")[0]
        assert name == "paper towels"
        assert qty == "2"

    def test_inline_store_hint(self):
        name, qty, store, explicit = _parse_note_items("- chicken (Costco)")[0]
        assert name == "chicken"
        assert store == "Costco"
        assert explicit is True

    def test_inline_store_hint_case_normalised(self):
        _, _, store, explicit = _parse_note_items("butter (iga)")[0]
        assert store == "Iga"
        assert explicit is True

    def test_section_header_sets_context(self):
        text = "🛒 IGA\n- milk ×2\n- spinach"
        items = _parse_note_items(text)
        assert len(items) == 2
        for _name, _qty, store, explicit in items:
            assert store == "IGA"
            assert not explicit

    def test_section_unassigned_clears_store(self):
        text = "🛒 IGA\n- milk\n🛒 Unassigned\n- mystery item"
        items = _parse_note_items(text)
        assert items[-1][2] is None

    def test_blank_lines_skipped(self):
        items = _parse_note_items("\n\n  peaches  \n\n")
        assert len(items) == 1

    def test_bullet_stripped(self):
        name, *_ = _parse_note_items("• butter")[0]
        assert name == "butter"

    def test_dash_stripped(self):
        name, *_ = _parse_note_items("- butter")[0]
        assert name == "butter"

    def test_empty_string(self):
        assert _parse_note_items("") == []

    def test_multiple_sections(self):
        text = "🛒 IGA\n- milk ×2\n- spinach\n🛒 Costco\n- rice ×10kg\n- paper towels ×2"
        items = _parse_note_items(text)
        assert len(items) == 4
        stores = [store for _, _, store, _ in items]
        assert stores[:2] == ["IGA", "IGA"]
        assert stores[2:] == ["Costco", "Costco"]

    def test_stray_punctuation_in_name(self):
        name, *_ = _parse_note_items("tomatoes!!!")[0]
        assert "tomatoes" in name


# ===========================================================================
# _render_note
# ===========================================================================


class TestRenderNote:
    def test_empty_db_returns_empty(self, db):
        assert _render_note(db) == ""

    def test_single_unassigned_item(self, db):
        cmd_add(db, "peaches")
        note = _render_note(db)
        assert "🛒 Unassigned" in note
        assert "- peaches" in note

    def test_assigned_items_grouped(self, db):
        cmd_add(db, "milk", qty="2", store="IGA")
        cmd_add(db, "rice", qty="10kg", store="Costco")
        note = _render_note(db)
        assert "🛒 Costco" in note
        assert "🛒 IGA" in note
        assert "- milk ×2" in note
        assert "- rice ×10kg" in note

    def test_sections_sorted_alphabetically(self, db):
        cmd_add(db, "z item", store="Zara")
        cmd_add(db, "a item", store="Aldi")
        note = _render_note(db)
        assert note.index("Aldi") < note.index("Zara")

    def test_unassigned_section_last(self, db):
        cmd_add(db, "milk", store="IGA")
        cmd_add(db, "loose")
        note = _render_note(db)
        assert note.index("IGA") < note.index("Unassigned")

    def test_qty_formatting(self, db):
        cmd_add(db, "eggs", qty="12")
        note = _render_note(db)
        assert "- eggs ×12" in note

    def test_no_qty_no_times_symbol(self, db):
        cmd_add(db, "spinach")
        note = _render_note(db)
        assert "- spinach" in note
        assert "×" not in note


# ===========================================================================
# _remove_store_section
# ===========================================================================


class TestRemoveStoreSection:
    def _note(self):
        return "🛒 IGA\n- milk ×2\n- spinach\n\n" "🛒 Costco\n- rice ×10kg\n- paper towels ×2"

    def test_removes_target_section(self):
        result = _remove_store_section(self._note(), "IGA")
        assert "🛒 IGA" not in result
        assert "milk" not in result

    def test_leaves_other_sections(self):
        result = _remove_store_section(self._note(), "IGA")
        assert "🛒 Costco" in result
        assert "rice" in result

    def test_case_insensitive(self):
        result = _remove_store_section(self._note(), "iga")
        assert "🛒 IGA" not in result

    def test_nonexistent_store_unchanged(self):
        result = _remove_store_section(self._note(), "Walmart")
        assert result.strip() == self._note().strip()

    def test_no_trailing_blank_lines(self):
        result = _remove_store_section(self._note(), "Costco")
        assert not result.endswith("\n")


# ===========================================================================
# cmd_sync_note — idempotency and capture
# ===========================================================================


class TestCmdSyncNote:
    def _mock_event(self, monkeypatch, description: str):
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt123", description))

    def test_adds_new_items(self, db, monkeypatch):
        self._mock_event(monkeypatch, "peaches\nmilk\nspinach")
        cmd_sync_note(db)
        rows = db.execute("SELECT name FROM grocery_items ORDER BY name").fetchall()
        names = [r[0] for r in rows]
        assert "milk" in names
        assert "peaches" in names
        assert "spinach" in names

    def test_idempotent_no_duplicates(self, db, monkeypatch):
        self._mock_event(monkeypatch, "peaches\nmilk")
        cmd_sync_note(db)
        cmd_sync_note(db)
        count = db.execute("SELECT COUNT(*) FROM grocery_items").fetchone()[0]
        assert count == 2

    def test_captures_qty_from_note(self, db, monkeypatch):
        self._mock_event(monkeypatch, "2 milk")
        cmd_sync_note(db)
        row = db.execute("SELECT qty FROM grocery_items WHERE name_norm = 'milk'").fetchone()
        assert row[0] == "2"

    def test_captures_store_from_section(self, db, monkeypatch):
        self._mock_event(monkeypatch, "🛒 IGA\n- milk ×2")
        cmd_sync_note(db)
        row = db.execute("SELECT store FROM grocery_items WHERE name_norm = 'milk'").fetchone()
        assert row[0] == "IGA"

    def test_does_not_override_existing_store_from_section_context(self, db, monkeypatch):
        # Item already in DB with store=Costco; note has it in IGA section (from prepend edge-case)
        cmd_add(db, "milk", store="Costco")
        self._mock_event(monkeypatch, "🛒 IGA\n- milk ×2")
        cmd_sync_note(db)
        row = db.execute("SELECT store FROM grocery_items WHERE name_norm = 'milk'").fetchone()
        # store must NOT have been overridden by section context
        assert row[0] == "Costco"

    def test_explicit_store_hint_updates_missing_store(self, db, monkeypatch):
        cmd_add(db, "chicken")
        self._mock_event(monkeypatch, "chicken (Costco)")
        cmd_sync_note(db)
        row = db.execute("SELECT store FROM grocery_items WHERE name_norm = 'chicken'").fetchone()
        assert row[0] == "Costco"

    def test_no_event_returns_warning(self, db, monkeypatch):
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: (None, "not found"))
        result = cmd_sync_note(db)
        assert "⚠️" in result

    def test_empty_note_returns_message(self, db, monkeypatch):
        # Empty note no longer bails early — deletion still runs; no items means "No changes."
        self._mock_event(monkeypatch, "")
        result = cmd_sync_note(db)
        assert result  # returns a non-empty string (not an exception)


# ===========================================================================
# Round-trip safety
# ===========================================================================


class TestRoundTrip:
    def test_formatted_note_re_parses_without_duplicates(self, db, monkeypatch):
        cmd_add(db, "milk", qty="2", store="IGA")
        cmd_add(db, "rice", qty="10kg", store="Costco")
        note = _render_note(db)

        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt", note))
        result = cmd_sync_note(db)

        count = db.execute("SELECT COUNT(*) FROM grocery_items").fetchone()[0]
        assert count == 2
        assert "Skipped" in result or "No changes" in result

    def test_round_trip_preserves_qty(self, db, monkeypatch):
        cmd_add(db, "paper towels", qty="2", store="Costco")
        note = _render_note(db)
        assert "×2" in note

        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt", note))
        cmd_sync_note(db)

        row = db.execute(
            "SELECT qty FROM grocery_items WHERE name_norm = 'paper towels'"
        ).fetchone()
        assert row[0] == "2"

    def test_unit_qty_round_trip_no_duplicate(self, db, monkeypatch):
        """Bug C (#57): unit-bearing qty like '3 lb' must not create a duplicate row.

        Before the fix, _TRAILING_QTY_RE used \\S+ which stopped at the space before the
        unit suffix — so '×3 lb' was not stripped and the whole 'pork shoulder ×3 lb'
        string became a new item name on re-sync.
        """
        cmd_add(db, "pork shoulder", qty="3 lb", store="Costco")
        note = _render_note(db)
        assert "×3 lb" in note  # rendered correctly

        # First sync — note has "- pork shoulder ×3 lb"
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt", note))
        cmd_sync_note(db)

        count = db.execute("SELECT COUNT(*) FROM grocery_items").fetchone()[0]
        assert count == 1, "first sync must not duplicate the item"

        # Second sync (simulates cmd_plan calling sync twice) — re-render and sync again
        note2 = _render_note(db)
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt2", note2))
        cmd_sync_note(db)

        count2 = db.execute("SELECT COUNT(*) FROM grocery_items").fetchone()[0]
        assert count2 == 1, "second sync must still produce exactly one row"

    def test_parse_trailing_qty_with_unit(self):
        """_TRAILING_QTY_RE must match unit-bearing quantities like '×3 lb'."""
        items = _parse_note_items("pork shoulder ×3 lb")
        assert len(items) == 1
        name, qty, _, _ = items[0]
        assert name == "pork shoulder"
        assert qty == "3 lb"

    def test_parse_trailing_qty_with_gram_unit(self):
        items = _parse_note_items("olive oil ×500 g")
        assert len(items) == 1
        name, qty, _, _ = items[0]
        assert name == "olive oil"
        assert qty == "500 g"

    def test_parse_trailing_qty_plain_int_still_works(self):
        """Existing plain-integer qty must still parse correctly after the regex change."""
        items = _parse_note_items("milk ×2")
        assert len(items) == 1
        name, qty, _, _ = items[0]
        assert name == "milk"
        assert qty == "2"


# ===========================================================================
# cmd_plan — store auto-assign from history
# ===========================================================================


class TestCmdPlan:
    def test_auto_assigns_store_from_archive_history(self, db, monkeypatch):
        # Seed archive: chicken → IGA previously
        db.execute(
            "INSERT INTO grocery_archive (trip_id, name, qty, store, shopped_at)"
            " VALUES ('t1', 'chicken', '1', 'IGA', '2026-01-01')"
        )
        db.commit()

        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt", "chicken"))
        monkeypatch.setattr(_g, "_cal_set_notes", lambda *a: None)

        cmd_plan(db)

        row = db.execute("SELECT store FROM grocery_items WHERE name_norm = 'chicken'").fetchone()
        assert row[0] == "IGA"

    def test_items_marked_planned(self, db, monkeypatch):
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt", "milk"))
        monkeypatch.setattr(_g, "_cal_set_notes", lambda *a: None)

        cmd_plan(db)

        row = db.execute("SELECT status FROM grocery_items WHERE name_norm = 'milk'").fetchone()
        assert row[0] == "planned"

    def test_writes_formatted_note(self, db, monkeypatch):
        written: list[str] = []

        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt", "milk"))
        monkeypatch.setattr(_g, "_cal_set_notes", lambda eid, text: written.append(text) or None)

        cmd_plan(db)

        assert written
        assert "🛒" in written[0]

    def test_plan_result_flags_no_event(self, db, monkeypatch):
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: (None, "not found"))

        cmd_add(db, "milk")
        result = cmd_plan(db)
        assert "⚠️" in result


# ===========================================================================
# cmd_shopped — partial clear
# ===========================================================================


class TestShoppedPartial:
    def test_partial_archives_only_target_store(self, db, monkeypatch):
        monkeypatch.setattr(
            _g, "_get_grocery_event", lambda: ("evt", "🛒 IGA\n- milk\n🛒 Costco\n- rice")
        )
        monkeypatch.setattr(_g, "_cal_set_notes", lambda *a: None)

        cmd_add(db, "milk", store="IGA")
        cmd_add(db, "rice", store="Costco")

        cmd_shopped(db, store="IGA")

        remaining = db.execute("SELECT name FROM grocery_items").fetchall()
        names = [r[0] for r in remaining]
        assert "rice" in names
        assert "milk" not in names

    def test_partial_archives_to_history(self, db, monkeypatch):
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt", ""))
        monkeypatch.setattr(_g, "_cal_set_notes", lambda *a: None)

        cmd_add(db, "milk", store="IGA")
        cmd_shopped(db, store="IGA")

        archived = db.execute("SELECT name FROM grocery_archive").fetchall()
        assert any(r[0] == "milk" for r in archived)

    def test_partial_no_items_for_store(self, db, cal_noop):
        cmd_add(db, "milk", store="IGA")
        result = cmd_shopped(db, store="Costco")
        assert "No items" in result

    def test_full_shopped_archives_all(self, db, monkeypatch):
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt", ""))
        monkeypatch.setattr(_g, "_cal_set_notes", lambda *a: None)

        cmd_add(db, "milk", store="IGA")
        cmd_add(db, "rice", store="Costco")
        cmd_shopped(db)

        count = db.execute("SELECT COUNT(*) FROM grocery_items").fetchone()[0]
        assert count == 0
        archived = db.execute("SELECT COUNT(*) FROM grocery_archive").fetchone()[0]
        assert archived == 2

    def test_full_shopped_no_items(self, db, cal_noop):
        result = cmd_shopped(db)
        assert "empty" in result.lower()


# ===========================================================================
# Regression — existing commands unaffected
# ===========================================================================


class TestRegressions:
    def test_add_still_works(self, db, cal_noop):
        result = cmd_add(db, "bananas", qty="3", store="IGA")
        assert "bananas" in result

    def test_list_still_works(self, db, cal_noop):
        cmd_add(db, "milk")
        result = cmd_list(db)
        assert "milk" in result

    def test_list_by_store_still_works(self, db, cal_noop):
        cmd_add(db, "milk", store="IGA")
        result = cmd_list(db, by_store=True)
        assert "IGA" in result

    def test_set_qty_still_works(self, db):
        cmd_add(db, "eggs")
        result = _g.cmd_set_qty(db, "eggs", "12")
        assert "×12" in result

    def test_set_store_still_works(self, db):
        cmd_add(db, "butter")
        result = _g.cmd_set_store(db, "butter", "IGA")
        assert "IGA" in result

    def test_review_still_works(self, db):
        cmd_add(db, "cheese")
        result = _g.cmd_review(db)
        assert "cheese" in result

    def test_usuals_still_works(self, db):
        result = _g.cmd_usuals(db)
        assert "No history" in result or "Top" in result

    def test_staples_still_works(self, db):
        result = _g.cmd_staples(db, "list")
        assert "No staples" in result or "Staples" in result


# ===========================================================================
# B1 — _load_env: UTF-8 encoding + inline comment stripping
# ===========================================================================


class TestLoadEnv:
    def _write_env(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def _read_env(self, path: Path, key: str, monkeypatch) -> str | None:
        monkeypatch.delenv(key, raising=False)
        _g._load_env(path)
        return os.environ.get(key)

    def test_plain_value(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        self._write_env(p, "TEST_PLAIN_VAL=hello\n")
        assert self._read_env(p, "TEST_PLAIN_VAL", monkeypatch) == "hello"

    def test_inline_comment_stripped(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        self._write_env(p, "TEST_INLINE_CMT=world # ignore this\n")
        assert self._read_env(p, "TEST_INLINE_CMT", monkeypatch) == "world"

    def test_quoted_value_unquoted(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        self._write_env(p, 'TEST_QUOTED="quoted_val"\n')
        assert self._read_env(p, "TEST_QUOTED", monkeypatch) == "quoted_val"

    def test_quoted_value_with_trailing_comment(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        self._write_env(p, 'TEST_QUOTED_CMT="the_value" # comment outside\n')
        assert self._read_env(p, "TEST_QUOTED_CMT", monkeypatch) == "the_value"

    def test_quoted_value_preserves_inner_hash(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        self._write_env(p, 'TEST_INNER_HASH="val#ue"\n')
        assert self._read_env(p, "TEST_INNER_HASH", monkeypatch) == "val#ue"

    def test_utf8_value(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        self._write_env(p, "TEST_UTF8=café\n")
        assert self._read_env(p, "TEST_UTF8", monkeypatch) == "café"

    def test_comment_line_ignored(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        self._write_env(p, "# this is a comment\nTEST_AFTER_CMT=ok\n")
        assert self._read_env(p, "TEST_AFTER_CMT", monkeypatch) == "ok"

    def test_export_prefix_stripped(self, tmp_path, monkeypatch):
        p = tmp_path / ".env"
        self._write_env(p, "export TEST_EXPORT=exported\n")
        assert self._read_env(p, "TEST_EXPORT", monkeypatch) == "exported"

    def test_missing_file_is_noop(self, tmp_path):
        _g._load_env(tmp_path / "nonexistent.env")  # must not raise


# ===========================================================================
# B2 — _cal_python: resolves venv dynamically, falls back to system python3
# ===========================================================================


class TestCalPython:
    def test_returns_system_python3_when_venv_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_g, "_SKILLS_DIR", tmp_path)
        result = _g._cal_python()
        assert result == "python3"

    def test_returns_venv_python_when_present(self, tmp_path, monkeypatch):
        venv_python = tmp_path / "calendar" / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()
        monkeypatch.setattr(_g, "_SKILLS_DIR", tmp_path)
        result = _g._cal_python()
        assert result == str(venv_python)


# ===========================================================================
# B3 — cmd_sync_note: reconciles deletions
# ===========================================================================


class TestSyncNoteDeletion:
    def _mock_event(self, monkeypatch, description: str):
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt123", description))

    def test_removes_item_deleted_from_note(self, db, monkeypatch):
        """Item present in note on first sync, removed from note, then sync removes it from DB."""
        # First sync: bring milk into DB from note (from_note=1)
        self._mock_event(monkeypatch, "milk\ncoffee")
        cmd_sync_note(db)
        assert (
            db.execute("SELECT COUNT(*) FROM grocery_items WHERE name_norm='milk'").fetchone()[0]
            == 1
        )

        # Second sync: milk removed from note → should be removed from DB
        self._mock_event(monkeypatch, "coffee")
        result = cmd_sync_note(db)
        assert (
            db.execute("SELECT COUNT(*) FROM grocery_items WHERE name_norm='milk'").fetchone()[0]
            == 0
        )
        assert "Removed" in result

    def test_keeps_item_added_by_other_route(self, db, monkeypatch):
        """Item added via cmd_add (from_note=0) is NOT removed even if absent from note."""
        cmd_add(db, "oranges")  # from_note defaults to 0
        self._mock_event(monkeypatch, "milk")
        cmd_sync_note(db)
        # oranges should still be present (from_note=0 protects it)
        assert (
            db.execute("SELECT COUNT(*) FROM grocery_items WHERE name_norm='oranges'").fetchone()[0]
            == 1
        )

    def test_removal_scoped_to_note_sourced_items(self, db, monkeypatch):
        """Only items with from_note=1 are subject to deletion; from_note=0 items are safe."""
        # Seed: bananas from note, butter added directly
        self._mock_event(monkeypatch, "bananas")
        cmd_sync_note(db)
        cmd_add(db, "butter")

        # Empty the note — bananas should vanish, butter should survive
        self._mock_event(monkeypatch, "")
        cmd_sync_note(db)
        assert (
            db.execute("SELECT COUNT(*) FROM grocery_items WHERE name_norm='bananas'").fetchone()[0]
            == 0
        )
        assert (
            db.execute("SELECT COUNT(*) FROM grocery_items WHERE name_norm='butter'").fetchone()[0]
            == 1
        )

    def test_idempotent_present_items_not_removed(self, db, monkeypatch):
        """Items still in the note after sync are not removed."""
        self._mock_event(monkeypatch, "eggs\nbread")
        cmd_sync_note(db)
        cmd_sync_note(db)
        assert db.execute("SELECT COUNT(*) FROM grocery_items").fetchone()[0] == 2


# ===========================================================================
# B5 — _append_item_to_note: case-insensitive dedup
# ===========================================================================


class TestAppendItemNoteDedup:
    def _note_with(self, monkeypatch, description: str, written: list):
        monkeypatch.setattr(_g, "_get_grocery_event", lambda: ("evt1", description))
        monkeypatch.setattr(_g, "_cal_set_notes", lambda eid, text: written.append(text) or None)

    def test_no_duplicate_exact_match(self, monkeypatch):
        written: list[str] = []
        self._note_with(monkeypatch, "coffee", written)
        result = _g._append_item_to_note("coffee", None, None)
        assert result is None  # already present, no update
        assert len(written) == 0

    def test_no_duplicate_case_mismatch(self, monkeypatch):
        written: list[str] = []
        self._note_with(monkeypatch, "Coffee", written)
        result = _g._append_item_to_note("coffee", None, None)
        assert result is None
        assert len(written) == 0

    def test_no_duplicate_uppercase_in_note(self, monkeypatch):
        written: list[str] = []
        self._note_with(monkeypatch, "MILK", written)
        result = _g._append_item_to_note("milk", None, None)
        assert result is None

    def test_different_item_appended(self, monkeypatch):
        written: list[str] = []
        self._note_with(monkeypatch, "coffee", written)
        _g._append_item_to_note("tea", None, None)
        assert len(written) == 1
        assert "tea" in written[0]

    def test_empty_note_allows_add(self, monkeypatch):
        written: list[str] = []
        self._note_with(monkeypatch, "", written)
        _g._append_item_to_note("sugar", None, None)
        assert len(written) == 1
        assert "sugar" in written[0]
