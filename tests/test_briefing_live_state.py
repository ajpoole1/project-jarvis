"""Tests for _write_live_state() and its parser helpers in skills/morning-briefing/skill.py."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load the skill with heavy third-party deps mocked (mirrors test_morning_briefing.py)
# ---------------------------------------------------------------------------

_MOCK_PKGS = ("anthropic", "feedparser", "requests", "dotenv")
for _pkg in _MOCK_PKGS:
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "morning-briefing" / "skill.py"
_spec = importlib.util.spec_from_file_location("morning_briefing_skill", _SKILL_PATH)
skill = importlib.util.module_from_spec(_spec)
sys.modules["morning_briefing_skill"] = skill
_spec.loader.exec_module(skill)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block(block_type: str, detail: str, salience: int = 50) -> skill.BriefBlock:
    return skill.BriefBlock(type=block_type, salience=salience, take="take", detail=detail)


def _calendar_block(*lines: str) -> skill.BriefBlock:
    return _block("calendar", "\n".join(lines))


def _gmail_block(*lines: str) -> skill.BriefBlock:
    return _block("gmail-priority", "\n".join(lines))


def _deadline_block(*lines: str) -> skill.BriefBlock:
    return _block("deadline", "\n".join(lines))


def _ellie_block(detail: str) -> skill.BriefBlock:
    return _block("ellie", detail)


# ---------------------------------------------------------------------------
# 1. Creates MEMORY.md when it does not exist
# ---------------------------------------------------------------------------


def test_creates_memory_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))
    (tmp_path / "memory").mkdir()

    cal = _calendar_block("• Today (Sat 2026-06-20) · 10:00 — Morning standup")
    skill._write_live_state(cal, None, None, None, None)

    memory_file = tmp_path / "memory" / "MEMORY.md"
    assert memory_file.exists()
    content = memory_file.read_text()
    assert "## Today" in content
    assert "10:00 Morning standup" in content


# ---------------------------------------------------------------------------
# 2. Replaces ## Today section, preserves content before and after it
# ---------------------------------------------------------------------------


def test_replaces_today_preserves_surrounding_content(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    memory_file = memory_dir / "MEMORY.md"

    existing = (
        "# Memory Index\n\n"
        "- [Some entry](some.md) — a useful note\n\n"
        "## Today\n\n"
        "_Refreshed 07:00 EDT — Saturday, June 20_\n\n"
        "- **Calendar:** 09:00 Old Meeting\n\n"
        "## userEmail\n"
        "The user's email is test@example.com.\n"
    )
    memory_file.write_text(existing)

    cal = _calendar_block("• Today (Sat 2026-06-20) · 11:00 — New Meeting")
    skill._write_live_state(cal, None, None, None, None)

    content = memory_file.read_text()
    # New calendar entry present
    assert "11:00 New Meeting" in content
    # Old calendar entry gone
    assert "09:00 Old Meeting" not in content
    # Content before Today preserved
    assert "# Memory Index" in content
    assert "Some entry" in content
    # Content after Today preserved
    assert "## userEmail" in content
    assert "test@example.com" in content


# ---------------------------------------------------------------------------
# 3. None BriefBlocks handled gracefully — omits sections, does not crash
# ---------------------------------------------------------------------------


def test_all_none_blocks_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))
    (tmp_path / "memory").mkdir()

    skill._write_live_state(None, None, None, None, None)

    content = (tmp_path / "memory" / "MEMORY.md").read_text()
    assert "## Today" in content
    assert "**Calendar:**" not in content
    assert "**Ellie:**" not in content
    assert "**Inbox:**" not in content
    assert "**Due soon:**" not in content


def test_partial_blocks_omits_missing_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))
    (tmp_path / "memory").mkdir()

    ellie = _ellie_block("👶 **Ellie:** 10:00 feels 12°C → light jacket + long sleeves.")
    skill._write_live_state(None, None, None, None, ellie)

    content = (tmp_path / "memory" / "MEMORY.md").read_text()
    assert "**Ellie:**" in content
    assert "light jacket" in content
    assert "**Calendar:**" not in content
    assert "**Inbox:**" not in content


def test_gmail_block_shows_clear_when_no_bullets(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))
    (tmp_path / "memory").mkdir()

    gmail = _gmail_block("IMMEDIATE:", "No bullets here — just header")
    skill._write_live_state(None, gmail, None, None, None)

    content = (tmp_path / "memory" / "MEMORY.md").read_text()
    assert "**Inbox:** clear" in content


def test_deadline_block_omits_due_soon_when_all_far(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))
    (tmp_path / "memory").mkdir()

    dead = _deadline_block(
        "• Far Task — due in 10d (2026-06-30)",
        "• Another Task [proj] — due in 14d (2026-07-04)",
    )
    skill._write_live_state(None, None, dead, None, None)

    content = (tmp_path / "memory" / "MEMORY.md").read_text()
    assert "**Due soon:**" not in content


# ---------------------------------------------------------------------------
# 4. Atomic write — writes to .tmp first, then os.replace
# ---------------------------------------------------------------------------


def test_atomic_write_uses_tmp_then_replace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))
    (tmp_path / "memory").mkdir()

    captured: dict = {}
    real_replace = os.replace

    def spy_replace(src, dst):
        captured["src"] = str(src)
        captured["dst"] = str(dst)
        captured["tmp_existed"] = Path(src).exists()
        real_replace(src, dst)

    cal = _calendar_block("• Today (Sat 2026-06-20) · 09:00 — Team sync")

    with patch.object(skill.os, "replace", side_effect=spy_replace):
        skill._write_live_state(cal, None, None, None, None)

    assert "src" in captured, "os.replace was never called"
    assert captured["src"].endswith(".tmp"), f"Expected .tmp source, got {captured['src']}"
    assert captured["dst"].endswith("MEMORY.md"), f"Expected MEMORY.md dest, got {captured['dst']}"
    assert captured["tmp_existed"], ".tmp file must exist before os.replace is called"
    # After replace: MEMORY.md exists, .tmp is gone
    assert (tmp_path / "memory" / "MEMORY.md").exists()
    assert not Path(captured["src"]).exists()


# ---------------------------------------------------------------------------
# 5. JARVIS_WORKSPACE_PATH env var is respected
# ---------------------------------------------------------------------------


def test_workspace_path_env_var(tmp_path, monkeypatch):
    custom = tmp_path / "custom_ws"
    custom.mkdir()
    (custom / "memory").mkdir()

    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(custom))

    skill._write_live_state(None, None, None, None, None)

    assert (custom / "memory" / "MEMORY.md").exists()
    # Default workspace should not have been written
    assert not (tmp_path / "memory" / "MEMORY.md").exists()


# ---------------------------------------------------------------------------
# Parser helper unit tests
# ---------------------------------------------------------------------------


def test_parse_calendar_for_state_timed_events():
    block = _calendar_block(
        "• Today (Sat 2026-06-20) · 10:00 — Morning standup",
        "• Tomorrow (Sun 2026-06-21) · 14:30 — Dentist",
    )
    entries = skill._parse_calendar_for_state(block)
    assert entries == ["10:00 Morning standup", "14:30 Dentist"]


def test_parse_calendar_for_state_caps_at_five():
    lines = [f"• Today (Sat 2026-06-20) · 0{i}:00 — Event {i}" for i in range(7)]
    block = _calendar_block(*lines)
    assert len(skill._parse_calendar_for_state(block)) == 5


def test_parse_gmail_for_state_extracts_bullets():
    block = _gmail_block(
        "IMMEDIATE:",
        "• alice@example.com — Invoice due",
        "• bob@example.com — Meeting request",
        "• extra@example.com — Should be excluded",
    )
    threads = skill._parse_gmail_for_state(block)
    assert len(threads) == 2
    assert "alice@example.com — Invoice due" in threads
    assert "bob@example.com — Meeting request" in threads


def test_parse_deadlines_for_state_filters_to_3_days():
    block = _deadline_block(
        "• Overdue Task — OVERDUE by 1d",
        "• Today Task — due TODAY",
        "• Tomorrow Task — due TOMORROW",
        "• Two Days Task — due in 2d (2026-06-22)",
        "• Three Days Task — due in 3d (2026-06-23)",
        "• Far Task — due in 7d (2026-06-27)",
        "• Very Far Task [proj] — due in 14d (2026-07-04)",
    )
    items = skill._parse_deadlines_for_state(block)
    assert len(items) == 5
    assert not any("7d" in i or "14d" in i for i in items)


def test_replace_today_section_replaces_and_preserves():
    existing = "# Index\n\n" "## Today\n\n" "_Old content_\n\n" "## Other\n" "stays\n"
    new_block = "## Today\n\n_New content_\n\n"
    result = skill._replace_today_section(existing, new_block)
    assert "## Today\n\n_New content_" in result
    assert "_Old content_" not in result
    assert "## Other\nstays" in result
    assert "# Index" in result


def test_replace_today_section_prepends_when_absent():
    existing = "## Other\nsome content\n"
    new_block = "## Today\n\n_Fresh_\n\n"
    result = skill._replace_today_section(existing, new_block)
    assert result.startswith("## Today")
    assert "## Other" in result
    assert "_Fresh_" in result
