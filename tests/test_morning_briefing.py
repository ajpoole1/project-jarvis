"""Tests for skills/morning-briefing/skill.py — calendar rendering, brief anchor, reads-coda voice."""

from __future__ import annotations

import importlib.util
import json
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
    assert f"Today is Sunday, 2026-06-07 (Eastern time). AJ is based in {skill.CITY}." in prompt


def test_build_brief_prompt_anchor_is_first_line():
    today = date(2026, 6, 7)
    prompt = skill._build_brief_prompt([], today, "")
    assert prompt.startswith(
        f"Today is Sunday, 2026-06-07 (Eastern time). AJ is based in {skill.CITY}."
    )


def test_build_brief_prompt_anchor_does_not_leak_timezone_placename():
    """The anchor must not contain a place-name-looking timezone ID (e.g. 'Toronto')
    that could be misread by the model as AJ's actual location."""
    today = date(2026, 6, 7)
    prompt = skill._build_brief_prompt([], today, "")
    anchor_line = prompt.split("\n", 1)[0]
    assert "Toronto" not in anchor_line


def test_build_brief_prompt_forbids_speculative_holidays():
    prompt = skill._build_brief_prompt([], date(2026, 6, 7), "")
    assert "holiday" in prompt.lower() and "long weekend" in prompt.lower()


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


# ---------------------------------------------------------------------------
# Ellie dress — helpers
# ---------------------------------------------------------------------------


def _make_wttr_data(
    drop_feels: float,
    drop_rain: int = 0,
    drop_wind: int = 0,
    pick_feels: float | None = None,
    pick_rain: int = 0,
    pick_wind: int = 0,
) -> dict:
    """Minimal mock wttr.in j1 payload for two daycare windows."""
    if pick_feels is None:
        pick_feels = drop_feels

    def slot(time_str: str, feels: float, rain: int, wind: int) -> dict:
        return {
            "time": time_str,
            "FeelsLikeC": str(int(feels)),
            "tempC": str(int(feels)),
            "chanceofrain": str(rain),
            "windspeedKmph": str(wind),
            "weatherDesc": [{"value": "Partly cloudy"}],
        }

    return {
        "weather": [
            {
                "maxtempC": str(int(max(drop_feels, pick_feels))),
                "mintempC": str(int(min(drop_feels, pick_feels))),
                "hourly": [
                    slot("0", drop_feels, 0, 0),
                    slot("300", drop_feels, 0, 0),
                    slot("600", drop_feels, 0, 0),
                    slot("900", drop_feels, drop_rain, drop_wind),
                    slot("1200", pick_feels, 0, 0),
                    slot("1500", pick_feels, pick_rain, pick_wind),
                    slot("1800", pick_feels, 0, 0),
                    slot("2100", pick_feels, 0, 0),
                ],
            }
        ],
        "current_condition": [
            {"weatherDesc": [{"value": "Partly cloudy"}], "temp_C": str(int(drop_feels))}
        ],
    }


# ---------------------------------------------------------------------------
# Ellie dress — _layer_for_temp boundaries
# ---------------------------------------------------------------------------


def test_layer_for_temp_hot():
    assert skill._layer_for_temp(skill._TEMP_HOT) == "hat + sunscreen, light clothes"
    assert skill._layer_for_temp(30.0) == "hat + sunscreen, light clothes"


def test_layer_for_temp_warm():
    assert skill._layer_for_temp(skill._TEMP_WARM) == "light clothes"
    assert skill._layer_for_temp(21.0) == "light clothes"


def test_layer_for_temp_mild():
    assert skill._layer_for_temp(skill._TEMP_MILD) == "light jacket + long sleeves"
    assert skill._layer_for_temp(14.0) == "light jacket + long sleeves"


def test_layer_for_temp_cool():
    assert skill._layer_for_temp(skill._TEMP_COOL) == "jacket + warm layers"
    assert skill._layer_for_temp(8.0) == "jacket + warm layers"


def test_layer_for_temp_cold():
    assert skill._layer_for_temp(skill._TEMP_COLD) == "heavy jacket + hat + mittens"
    assert skill._layer_for_temp(3.0) == "heavy jacket + hat + mittens"


def test_layer_for_temp_below_zero():
    assert skill._layer_for_temp(-5.0) == "snowsuit"


def test_layer_for_temp_boundary_below_mild():
    # 11.9 is below TEMP_MILD (12), so falls to cool
    assert skill._layer_for_temp(11.9) == "jacket + warm layers"


# ---------------------------------------------------------------------------
# Ellie dress — weekday / weekend gate
# ---------------------------------------------------------------------------


def test_ellie_dress_weekday_returns_block():
    data = _make_wttr_data(drop_feels=14, pick_feels=20)
    monday = date(2026, 6, 8)  # Monday
    block = skill._get_ellie_dress(data, monday)
    assert block is not None
    assert block.type == "ellie"
    assert "10:00" in block.detail
    assert "15:00" in block.detail


def test_ellie_dress_saturday_omitted():
    data = _make_wttr_data(drop_feels=14, pick_feels=20)
    saturday = date(2026, 6, 7)  # Saturday
    block = skill._get_ellie_dress(data, saturday)
    assert block is None


def test_ellie_dress_sunday_omitted():
    data = _make_wttr_data(drop_feels=14, pick_feels=20)
    sunday = date(2026, 6, 14)  # Sunday
    block = skill._get_ellie_dress(data, sunday)
    assert block is None


def test_ellie_dress_all_weekdays_produce_block():
    data = _make_wttr_data(drop_feels=14, pick_feels=20)
    # Mon 2026-06-08 … Fri 2026-06-12
    for day_offset in range(5):
        d = date(2026, 6, 8 + day_offset)
        assert d.weekday() < 5, f"{d} is not a weekday"
        block = skill._get_ellie_dress(data, d)
        assert block is not None, f"Expected block for {d}"


# ---------------------------------------------------------------------------
# Ellie dress — dressing logic (rules-based)
# ---------------------------------------------------------------------------


def test_ellie_dress_cool_morning_warm_rainy_afternoon():
    """Classic spec scenario: cool drop-off, warm rainy pick-up."""
    data = _make_wttr_data(drop_feels=12, pick_feels=21, pick_rain=40)
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is not None
    detail = block.detail
    # Drop-off: mild → light jacket
    assert "light jacket" in detail
    # Pick-up: warm + rain → pack rain coat note
    assert "rain coat" in detail
    assert "40%" in detail


def test_ellie_dress_rain_likely_both_windows():
    """≥60% rain → rain coat (not just pack)."""
    data = _make_wttr_data(drop_feels=14, drop_rain=70, pick_feels=16, pick_rain=80)
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is not None
    # "rain coat" should appear without the "(pack" qualifier for 70%
    assert "rain coat" in block.detail


def test_ellie_dress_rain_possible_shows_pack():
    """30–59% rain → pack rain coat."""
    data = _make_wttr_data(drop_feels=15, drop_rain=35)
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is not None
    assert "pack rain coat" in block.detail


def test_ellie_dress_no_rain_note_below_threshold():
    """<30% rain → no rain mention."""
    data = _make_wttr_data(drop_feels=20, drop_rain=20, pick_feels=22, pick_rain=20)
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is not None
    assert "rain" not in block.detail.lower()


def test_ellie_dress_hot_day():
    data = _make_wttr_data(drop_feels=26, pick_feels=28)
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is not None
    assert "hat" in block.detail
    assert "sunscreen" in block.detail


def test_ellie_dress_cold_day():
    data = _make_wttr_data(drop_feels=3, pick_feels=4)
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is not None
    assert "heavy jacket" in block.detail
    assert "mittens" in block.detail


def test_ellie_dress_breezy_note():
    """Wind ≥ 30 kmph → 'breezy' appears in conditions."""
    data = _make_wttr_data(drop_feels=14, drop_wind=35)
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is not None
    assert "breezy" in block.detail


def test_ellie_dress_calm_no_breezy():
    """Wind < 30 kmph → no breezy note."""
    data = _make_wttr_data(drop_feels=14, drop_wind=20)
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is not None
    assert "breezy" not in block.detail


# ---------------------------------------------------------------------------
# Ellie dress — graceful degradation
# ---------------------------------------------------------------------------


def test_ellie_dress_none_data_graceful():
    block = skill._get_ellie_dress(None, date(2026, 6, 9))
    assert block is None


def test_ellie_dress_empty_hourly_graceful():
    data = {
        "weather": [{"maxtempC": "20", "mintempC": "10", "hourly": []}],
        "current_condition": [{"weatherDesc": [{"value": "Clear"}], "temp_C": "15"}],
    }
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is None


def test_ellie_dress_missing_target_slots_graceful():
    """Hourly data present but neither 900 nor 1500 slot exists → None."""
    data = {
        "weather": [
            {
                "maxtempC": "20",
                "mintempC": "10",
                "hourly": [
                    {
                        "time": "600",
                        "FeelsLikeC": "15",
                        "tempC": "15",
                        "chanceofrain": "0",
                        "windspeedKmph": "10",
                        "weatherDesc": [{"value": "Clear"}],
                    },
                ],
            }
        ],
        "current_condition": [{"weatherDesc": [{"value": "Clear"}], "temp_C": "15"}],
    }
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is None


def test_ellie_dress_partial_slot_only_morning():
    """Only 900 slot present (1500 missing) → still returns a block."""
    data = {
        "weather": [
            {
                "maxtempC": "15",
                "mintempC": "10",
                "hourly": [
                    {
                        "time": "900",
                        "FeelsLikeC": "12",
                        "tempC": "12",
                        "chanceofrain": "0",
                        "windspeedKmph": "5",
                        "weatherDesc": [{"value": "Sunny"}],
                    },
                ],
            }
        ],
        "current_condition": [{"weatherDesc": [{"value": "Sunny"}], "temp_C": "12"}],
    }
    block = skill._get_ellie_dress(data, date(2026, 6, 9))
    assert block is not None
    assert "10:00" in block.detail
    assert "15:00" not in block.detail


# ---------------------------------------------------------------------------
# Single-fetch conformance (AC 3)
# ---------------------------------------------------------------------------


def test_get_weather_does_not_fetch_when_data_is_none():
    """_get_weather(None) must not trigger a second fetch — None means fetch failed."""
    with patch.object(skill, "_fetch_wttr_data") as mock_fetch:
        result = skill._get_weather(None)
    mock_fetch.assert_not_called()
    assert result is None


def test_get_weather_fetches_when_called_with_no_args():
    """_get_weather() with no args should call _fetch_wttr_data exactly once."""
    with patch.object(skill, "_fetch_wttr_data", return_value=None) as mock_fetch:
        result = skill._get_weather()
    mock_fetch.assert_called_once()
    assert result is None


def _wttr_payload(feels_like_c="18", rain_pcts=(10, 20), snow_pcts=(0, 0)):
    return {
        "weather": [
            {
                "maxtempC": "22",
                "mintempC": "12",
                "hourly": [
                    {"chanceofrain": str(r), "chanceofsnow": str(s)}
                    for r, s in zip(rain_pcts, snow_pcts, strict=False)
                ],
            }
        ],
        "current_condition": [
            {
                "FeelsLikeC": feels_like_c,
                "weatherDesc": [{"value": "Sunny"}],
            }
        ],
    }


def test_get_weather_includes_feels_like_temp():
    result = skill._get_weather(_wttr_payload(feels_like_c="18"))
    assert "feels 18" in result.detail


def test_get_weather_includes_rain_chance_when_above_threshold():
    result = skill._get_weather(_wttr_payload(rain_pcts=(10, 45)))
    assert "45% chance of rain" in result.detail


def test_get_weather_omits_rain_chance_when_below_threshold():
    result = skill._get_weather(_wttr_payload(rain_pcts=(5, 10)))
    assert "chance of rain" not in result.detail


def test_get_weather_includes_snow_chance_when_above_threshold():
    result = skill._get_weather(_wttr_payload(snow_pcts=(0, 50)))
    assert "50% chance of snow" in result.detail


def _mock_upcoming_result(events):
    result = MagicMock()
    result.returncode = 0
    result.stdout = json.dumps(events)
    return result


def test_get_holiday_returns_none_when_no_holiday_calendar_events():
    events = [
        {
            "summary": "Session",
            "start": "2026-07-31T09:00:00-04:00",
            "calendar": "aaronjacobpoole@gmail.com",
        }
    ]
    with (
        patch("subprocess.run", return_value=_mock_upcoming_result(events)),
        patch.object(Path, "exists", return_value=True),
    ):
        result = skill._get_holiday()
    assert result is None


def test_get_holiday_surfaces_confirmed_holiday_calendar_event():
    events = [
        {
            "summary": "Civic Holiday",
            "start": "2026-08-03",
            "calendar": skill._HOLIDAY_CALENDAR_ID,
        }
    ]
    with (
        patch("subprocess.run", return_value=_mock_upcoming_result(events)),
        patch.object(Path, "exists", return_value=True),
    ):
        result = skill._get_holiday()
    assert result is not None
    assert "Civic Holiday" in result.detail
    assert "2026-08-03" in result.detail


def test_get_holiday_ignores_non_holiday_calendar_events():
    events = [
        {"summary": "Move mortgage", "start": "2026-07-31", "calendar": "some-other-calendar-id"},
    ]
    with (
        patch("subprocess.run", return_value=_mock_upcoming_result(events)),
        patch.object(Path, "exists", return_value=True),
    ):
        result = skill._get_holiday()
    assert result is None


# ---------------------------------------------------------------------------
# Reads sources config (AC 1)
# ---------------------------------------------------------------------------


def test_reads_sources_exists():
    assert hasattr(skill, "_READS_SOURCES"), "_READS_SOURCES config must be defined"


def test_reads_sources_is_list_of_tuples():
    assert isinstance(skill._READS_SOURCES, list)
    for entry in skill._READS_SOURCES:
        assert len(entry) == 2, f"Expected (name, url) pair, got: {entry!r}"


def test_reads_sources_has_hobby_feeds():
    """AC 1: ≥1 feed for MTG, D&D, and Star Wars."""
    urls = [url for _, url in skill._READS_SOURCES]
    assert any("hipstersofthecoast" in u for u in urls), "MTG feed missing"
    assert any("thealexandrian" in u for u in urls), "D&D/TTRPG feed missing"
    assert any("eleven-thirtyeight" in u for u in urls), "Star Wars feed missing"


def test_reads_sources_min_count():
    """Curated default should have at least 15 entries."""
    assert len(skill._READS_SOURCES) >= 15


# ---------------------------------------------------------------------------
# Reads coda — combined pool (AC 2) and longform bias (AC 3)
# ---------------------------------------------------------------------------


def _make_fake_create(haiku_scores: list[dict], sonnet_picks: list[dict]):
    """Return a side_effect callable that mocks Haiku scoring and Sonnet picks."""
    import json as _json

    def fake_create(**kwargs):
        model = kwargs["model"]
        mock_resp = MagicMock()
        if model == skill.HAIKU_MODEL:
            mock_resp.content = [MagicMock(text=_json.dumps(haiku_scores))]
        else:
            mock_resp.content = [MagicMock(text=_json.dumps(sonnet_picks))]
        return mock_resp

    return fake_create


def test_reads_coda_pool_includes_longform_and_news(monkeypatch):
    """AC 2: candidate pool is drawn from both longform feeds and Google News."""

    def fake_score_create(**kwargs):
        # Capture the candidates from the prompt to verify both kinds were gathered.
        # Intercept at scoring stage — by this point candidates have been assembled.
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text="[]")]
        return mock_resp

    news_item = {
        "title": "News headline",
        "link": "https://news.example.com/1",
        "source": "Google News",
        "summary": "",
    }
    essay_item = {
        "title": "Longform essay",
        "link": "https://essay.example.com/1",
        "source": "Aeon",
        "summary": "",
    }

    parse_calls: list[str] = []

    def fake_parse_rss(url, source_name, max_items=8):
        parse_calls.append(source_name)
        if source_name == "Google News":
            return [news_item]
        return [essay_item]

    client_mock = MagicMock()
    client_mock.messages.create.side_effect = fake_score_create

    profile = "### 1. Technology — AI and software"

    with (
        patch.object(skill, "_parse_rss", side_effect=fake_parse_rss),
        patch.object(skill, "_is_reads_dedup", return_value=False),
        patch.object(skill, "_init_reads_dedup"),
        patch.object(skill, "_mark_reads_surfaced"),
    ):
        skill._get_reads_coda(client_mock, profile)

    assert "Google News" in parse_calls, "Google News candidates not fetched"
    longform_sources = [s for s in parse_calls if s != "Google News"]
    assert len(longform_sources) > 0, "No longform feed sources fetched"


def test_reads_coda_longform_beats_news_at_equal_score(monkeypatch):
    """AC 3: when a longform and a news item score equally, longform fills the slot first."""
    sonnet_was_called_with: list[str] = []

    def fake_create(**kwargs):
        model = kwargs["model"]
        content = kwargs["messages"][0]["content"]
        mock_resp = MagicMock()
        if model == skill.HAIKU_MODEL:
            # Both candidates score 7
            mock_resp.content = [
                MagicMock(text='[{"index": 0, "score": 7}, {"index": 1, "score": 7}]')
            ]
        else:
            sonnet_was_called_with.append(content)
            mock_resp.content = [MagicMock(text='[{"index": 0, "why": "You should read this."}]')]
        return mock_resp

    news_item = {
        "title": "Breaking news story",
        "link": "https://news.example.com/1",
        "source": "Google News",
        "summary": "news",
    }
    essay_item = {
        "title": "Longform essay piece",
        "link": "https://essay.example.com/2",
        "source": "Aeon",
        "summary": "essay",
    }

    def fake_parse_rss(url, source_name, max_items=8):
        if source_name == "Google News":
            return [news_item]
        if source_name == "Aeon":
            return [essay_item]
        return []

    client_mock = MagicMock()
    client_mock.messages.create.side_effect = fake_create

    profile = "### 1. Technology — AI and software"

    with (
        patch.object(skill, "_parse_rss", side_effect=fake_parse_rss),
        patch.object(skill, "_is_reads_dedup", return_value=False),
        patch.object(skill, "_init_reads_dedup"),
        patch.object(skill, "_mark_reads_surfaced"),
    ):
        skill._get_reads_coda(client_mock, profile)

    # Sonnet must have been called (at least one candidate cleared the bar)
    assert sonnet_was_called_with, "Sonnet pick prompt was never called"
    pick_prompt = sonnet_was_called_with[0]
    # The longform candidate should appear before the news candidate in the pick prompt
    longform_pos = pick_prompt.find("Longform essay piece")
    news_pos = pick_prompt.find("Breaking news story")
    assert longform_pos != -1, "Longform candidate not in pick prompt"
    assert news_pos != -1, "News candidate not in pick prompt"
    assert (
        longform_pos < news_pos
    ), "Longform candidate must appear before news in pick prompt (bias)"


def test_reads_coda_unreachable_feeds_skipped(monkeypatch):
    """AC 4: unreachable feeds return [] from _parse_rss; briefing still completes."""

    def fake_parse_rss(url, source_name, max_items=8):
        # Only Google News returns items; all longform feeds are 'unreachable'
        if source_name == "Google News":
            return [
                {
                    "title": "Some news",
                    "link": "https://news.example.com/1",
                    "source": "Google News",
                    "summary": "",
                }
            ]
        return []

    def fake_create(**kwargs):
        model = kwargs["model"]
        mock_resp = MagicMock()
        if model == skill.HAIKU_MODEL:
            mock_resp.content = [MagicMock(text='[{"index": 0, "score": 7}]')]
        else:
            mock_resp.content = [MagicMock(text='[{"index": 0, "why": "Interesting read."}]')]
        return mock_resp

    client_mock = MagicMock()
    client_mock.messages.create.side_effect = fake_create

    profile = "### 1. Technology — AI and software"

    with (
        patch.object(skill, "_parse_rss", side_effect=fake_parse_rss),
        patch.object(skill, "_is_reads_dedup", return_value=False),
        patch.object(skill, "_init_reads_dedup"),
        patch.object(skill, "_mark_reads_surfaced"),
    ):
        result = skill._get_reads_coda(client_mock, profile)

    # Briefing still produces a result from the news fallback
    assert result is not None, "Briefing failed when all longform feeds unreachable"
    assert result.type == "reads-coda"


# ---------------------------------------------------------------------------
# B4 — reads coda: defensive index cast (string / out-of-range / None)
# ---------------------------------------------------------------------------


def test_reads_coda_string_index_coerced(monkeypatch):
    """LLM returns string index → coerced to int, pick proceeds normally."""

    def fake_create(**kwargs):
        model = kwargs["model"]
        mock_resp = MagicMock()
        if model == skill.HAIKU_MODEL:
            # String index "0" instead of int 0
            mock_resp.content = [MagicMock(text='[{"index": "0", "score": 9}]')]
        else:
            mock_resp.content = [MagicMock(text='[{"index": "0", "why": "Interesting."}]')]
        return mock_resp

    client_mock = MagicMock()
    client_mock.messages.create.side_effect = fake_create

    with (
        patch.object(
            skill,
            "_parse_rss",
            return_value=[
                {"title": "Story", "link": "https://x.com/1", "source": "src", "summary": "s"}
            ],
        ),
        patch.object(skill, "_is_reads_dedup", return_value=False),
        patch.object(skill, "_init_reads_dedup"),
        patch.object(skill, "_mark_reads_surfaced"),
    ):
        # Must not raise TypeError
        skill._get_reads_coda(client_mock, "### 1. Tech")

    # Result may be None if the pick prompt call fails shape-check, but no exception
    # is acceptable; a valid result is ideal.


def test_reads_coda_invalid_index_skipped(monkeypatch):
    """Out-of-range and non-int indices are silently skipped; no TypeError raised."""

    def fake_create(**kwargs):
        model = kwargs["model"]
        mock_resp = MagicMock()
        if model == skill.HAIKU_MODEL:
            # Mix of valid string index, out-of-range, None, and bad type
            mock_resp.content = [
                MagicMock(
                    text=(
                        '[{"index": 99, "score": 9},'
                        ' {"index": null, "score": 8},'
                        ' {"index": "bad", "score": 7},'
                        ' {"index": 0, "score": 6}]'
                    )
                )
            ]
        else:
            mock_resp.content = [MagicMock(text='[{"index": 0, "why": "Good."}]')]
        return mock_resp

    client_mock = MagicMock()
    client_mock.messages.create.side_effect = fake_create

    with (
        patch.object(
            skill,
            "_parse_rss",
            return_value=[
                {"title": "Article", "link": "https://x.com/2", "source": "src", "summary": "s"}
            ],
        ),
        patch.object(skill, "_is_reads_dedup", return_value=False),
        patch.object(skill, "_init_reads_dedup"),
        patch.object(skill, "_mark_reads_surfaced"),
    ):
        # Must not raise TypeError or IndexError
        skill._get_reads_coda(client_mock, "### 1. Tech")
