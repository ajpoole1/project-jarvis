"""Tests for skills/morning-briefing/skill.py — calendar rendering, brief anchor, reads-coda voice."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load the skill with its heavy third-party dependencies mocked so pytest can
# import it from the root venv (which only has ruff/pytest/pytest-cov).
# ---------------------------------------------------------------------------

_MOCK_PKGS = ("anthropic", "feedparser", "requests", "dotenv")
for _pkg in _MOCK_PKGS:
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "morning-briefing" / "skill.py"
_spec = importlib.util.spec_from_file_location("morning_briefing_skill", _SKILL_PATH)
skill = importlib.util.module_from_spec(_spec)
# Register before exec_module so @dataclass can resolve cls.__module__
sys.modules["morning_briefing_skill"] = skill
_spec.loader.exec_module(skill)


# ---------------------------------------------------------------------------
# Calendar rendering
# ---------------------------------------------------------------------------


def test_render_calendar_all_day_today():
    today = date(2026, 6, 7)  # Sunday
    events = [{"start": "2026-06-07", "summary": "Stand-up"}]
    lines = skill._render_calendar_lines(events, today)
    assert lines == ["• Today (Sun 2026-06-07) · All day — Stand-up"]


def test_render_calendar_all_day_tomorrow():
    today = date(2026, 6, 7)
    events = [{"start": "2026-06-08", "summary": "Fridge people"}]
    lines = skill._render_calendar_lines(events, today)
    assert lines == ["• Tomorrow (Mon 2026-06-08) · All day — Fridge people"]


def test_render_calendar_timed_event_tomorrow():
    today = date(2026, 6, 7)
    events = [{"start": "2026-06-08T14:30:00-04:00", "summary": "Doctor appointment"}]
    lines = skill._render_calendar_lines(events, today)
    assert lines == ["• Tomorrow (Mon 2026-06-08) · 14:30 — Doctor appointment"]


def test_render_calendar_no_dateless_lines():
    today = date(2026, 6, 7)
    events = [
        {"start": "2026-06-07", "summary": "Alpha"},
        {"start": "2026-06-08", "summary": "Beta"},
        {"start": "2026-06-08T09:00:00-04:00", "summary": "Gamma"},
    ]
    lines = skill._render_calendar_lines(events, today)
    assert len(lines) == 3
    for line in lines:
        assert "2026-06-0" in line, f"Line is dateless: {line!r}"


# ---------------------------------------------------------------------------
# Brief composition — date anchor
# ---------------------------------------------------------------------------


def test_build_brief_prompt_contains_anchor():
    today = date(2026, 6, 7)  # Sunday
    blocks = [skill.BriefBlock(type="weather", salience=35, take="sunny", detail="Sunny, 22°C")]
    prompt = skill._build_brief_prompt(blocks, today, "")
    assert "Today is Sunday, 2026-06-07 (America/Toronto)." in prompt


def test_build_brief_prompt_anchor_is_first_line():
    today = date(2026, 6, 7)
    prompt = skill._build_brief_prompt([], today, "")
    assert prompt.startswith("Today is Sunday, 2026-06-07 (America/Toronto).")


# ---------------------------------------------------------------------------
# Reads coda — second-person voice in pick prompt
# ---------------------------------------------------------------------------


def test_reads_coda_pick_prompt_second_person():
    captured: dict[str, str] = {}

    def fake_create(**kwargs):
        model = kwargs["model"]
        captured[model] = kwargs["messages"][0]["content"]
        mock_resp = MagicMock()
        if model == skill.HAIKU_MODEL:
            mock_resp.content = [MagicMock(text='[{"index": 0, "score": 8}]')]
        else:
            mock_resp.content = [
                MagicMock(text='[{"index": 0, "why": "You will find this useful."}]')
            ]
        return mock_resp

    client_mock = MagicMock()
    client_mock.messages.create.side_effect = fake_create

    profile = "### 1. Technology — AI and software"

    with (
        patch.object(
            skill,
            "_parse_rss",
            return_value=[
                {
                    "title": "AI update",
                    "link": "https://example.com/1",
                    "source": "Test",
                    "summary": "details",
                }
            ],
        ),
        patch.object(skill, "_is_reads_dedup", return_value=False),
        patch.object(skill, "_init_reads_dedup"),
        patch.object(skill, "_mark_reads_surfaced"),
    ):
        skill._get_reads_coda(client_mock, profile)

    assert skill.SONNET_MODEL in captured, "Sonnet pick prompt was never called"
    pick_prompt = captured[skill.SONNET_MODEL]
    assert "second person" in pick_prompt.lower(), "pick prompt must instruct second-person voice"
    assert "AJ is" in pick_prompt, "pick prompt must explicitly forbid 'AJ is…' framing"
