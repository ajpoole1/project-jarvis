"""Tests for grocery ↔ calendar note bridge (2026-0017).

Covers: _parse_note_items, _render_note, _remove_store_section, cmd_sync_note,
        cmd_plan (store auto-assign), cmd_shopped partial clear, round-trip safety,
        and no regressions on existing grocery commands.
"""

from __future__ import annotations

import importlib.util
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
        self._mock_event(monkeypatch, "")
        result = cmd_sync_note(db)
        assert "empty" in result.lower()


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
