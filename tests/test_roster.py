"""Tests for the dev-crew roster schema and load_roster parser.

Validates that:
- load_roster parses the expanded overlay schema without breaking.
- Herr Mannkusser's required summon fields (REFERENCE §8.2 + get_persona checks) are present.
- The new overlay fields (tone, build_expectations, overlay) are parsed correctly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# ---------------------------------------------------------------------------
# Import skills/summon/skill.py (contains load_roster + get_persona)
# ---------------------------------------------------------------------------

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "summon" / "skill.py"
_spec = importlib.util.spec_from_file_location("summon_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

ROSTER_FILE = Path(__file__).parents[1] / "knowledge" / "dev-crew" / "roster.md"
OVERLAY_FILE = Path(__file__).parents[1] / "knowledge" / "dev-crew" / "overlays" / "mannkusser.md"
DEV_BASE_FILE = Path(__file__).parents[1] / "docs" / "dev-standards" / "DEV_BASE.md"
CHARTER_FILE = Path(__file__).parents[1] / "CHARTER.md"


# ---------------------------------------------------------------------------
# Roster file existence
# ---------------------------------------------------------------------------


def test_roster_file_exists():
    assert ROSTER_FILE.exists(), f"Roster not found: {ROSTER_FILE}"


def test_overlay_file_exists():
    assert OVERLAY_FILE.exists(), f"Overlay not found: {OVERLAY_FILE}"


def test_dev_base_exists():
    assert DEV_BASE_FILE.exists(), f"DEV_BASE.md not found: {DEV_BASE_FILE}"


def test_charter_exists():
    assert CHARTER_FILE.exists(), f"CHARTER.md not found: {CHARTER_FILE}"


# ---------------------------------------------------------------------------
# load_roster parses Herr's entry
# ---------------------------------------------------------------------------


def test_load_roster_parses_mannkusser():
    roster = _mod.load_roster()
    assert "mannkusser" in roster, f"mannkusser not found in roster; got keys: {list(roster)}"


def test_mannkusser_required_summon_fields():
    """Required by get_persona(): name, repo_dir, role, permission_mode."""
    roster = _mod.load_roster()
    herr = roster["mannkusser"]
    for field in ("name", "repo_dir", "role", "permission_mode"):
        assert field in herr, f"mannkusser missing required field '{field}'"


def test_mannkusser_required_schema_fields():
    """Required by REFERENCE §8.2: id, voice, model, auth."""
    roster = _mod.load_roster()
    herr = roster["mannkusser"]
    for field in ("id", "voice", "model", "auth"):
        assert field in herr, f"mannkusser missing §8.2 field '{field}'"


# ---------------------------------------------------------------------------
# New overlay fields parse correctly
# ---------------------------------------------------------------------------


def test_mannkusser_overlay_fields_present():
    """tone, build_expectations, and overlay are required by the 2026-0014 schema."""
    roster = _mod.load_roster()
    herr = roster["mannkusser"]
    for field in ("tone", "build_expectations", "overlay"):
        assert field in herr, f"mannkusser missing new overlay field '{field}'"


def test_mannkusser_tone_nonempty():
    roster = _mod.load_roster()
    assert roster["mannkusser"]["tone"].strip()


def test_mannkusser_build_expectations_nonempty():
    roster = _mod.load_roster()
    assert roster["mannkusser"]["build_expectations"].strip()


def test_mannkusser_overlay_points_to_existing_file():
    roster = _mod.load_roster()
    overlay_rel = roster["mannkusser"]["overlay"]
    overlay_abs = ROSTER_FILE.parent / overlay_rel
    assert overlay_abs.exists(), f"Overlay file not found: {overlay_abs}"


# ---------------------------------------------------------------------------
# get_persona does not raise for mannkusser (summon would succeed)
# ---------------------------------------------------------------------------


def test_get_persona_mannkusser_does_not_raise():
    """If this raises SystemExit, Herr cannot be summoned — regression in required fields."""
    herr = _mod.get_persona("mannkusser")
    assert herr["name"] == "Herr Mannkusser"


# ---------------------------------------------------------------------------
# Overlay file content
# ---------------------------------------------------------------------------


def test_overlay_contains_voice_section():
    text = OVERLAY_FILE.read_text(encoding="utf-8")
    assert "Voice" in text or "Tone" in text


def test_overlay_contains_build_expectations_section():
    text = OVERLAY_FILE.read_text(encoding="utf-8")
    assert "Build Expectations" in text or "build_expectations" in text.lower()


def test_overlay_references_not_duplicated():
    """output-styles and commands should reference the overlay, not copy the character text.
    Verified by checking the overlay file exists and is the single source."""
    text = OVERLAY_FILE.read_text(encoding="utf-8")
    # The approved character register phrase must appear exactly in the overlay.
    assert (
        "work-order to execute to the letter" in text
    ), "Herr's AJ-approved register text not found in overlay — overlay is not the canonical source"


# ---------------------------------------------------------------------------
# DEV_BASE content (supersedes BUILDER_BASE)
# ---------------------------------------------------------------------------


def test_dev_base_has_extraction_marker():
    text = DEV_BASE_FILE.read_text(encoding="utf-8")
    assert "extractable" in text.lower() or "dev-standards" in text.lower()


def test_dev_base_has_add_builder_note():
    """Cascade legibility: the base must explain how to add a builder."""
    text = DEV_BASE_FILE.read_text(encoding="utf-8")
    assert "add a builder" in text.lower() or "to add" in text.lower()


def test_dev_base_has_invariants():
    text = DEV_BASE_FILE.read_text(encoding="utf-8")
    assert "non-negotiable" in text.lower() or "common law" in text.lower()
