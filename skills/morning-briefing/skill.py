"""Morning briefing — single Sonnet call at 7am covering calendar, weather, news, and inbox."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import quote_plus

import anthropic
import feedparser
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env")

SONNET_MODEL = "claude-sonnet-4-6"
DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "jarvis.db"
CONFIG_DIR = Path(os.environ.get("JARVIS_CONFIG_DIR", "/config/personal"))
CITY = os.environ.get("JARVIS_CITY", "Montreal")

INTERESTS_PATH = CONFIG_DIR / "briefing_interests.json"
INTERESTS_EXAMPLE_PATH = (
    Path(__file__).parents[2] / "config" / "examples" / "briefing_interests.json"
)

FETCH_TIMEOUT = 10  # seconds


def _load_interests() -> list[str]:
    path = INTERESTS_PATH if INTERESTS_PATH.exists() else INTERESTS_EXAMPLE_PATH
    try:
        return json.loads(path.read_text()).get("interests", [])
    except Exception:
        return []


def _get_weather() -> str:
    try:
        resp = requests.get(
            f"https://wttr.in/{quote_plus(CITY)}?format=j1",
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": "jarvis-briefing/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        today = data["weather"][0]
        current = data["current_condition"][0]
        high = today["maxtempC"]
        low = today["mintempC"]
        desc = current["weatherDesc"][0]["value"]
        return f"{CITY}: high {high}°C / low {low}°C, {desc.lower()}"
    except Exception:
        return ""


def _parse_rss(url: str, max_items: int) -> list[tuple[str, str]]:
    """Return list of (title, link) from an RSS feed."""
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "jarvis-briefing/1.0"})
        items = []
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if title:
                items.append((title, link))
        return items
    except Exception:
        return []


def _get_world_news() -> list[tuple[str, str]]:
    return _parse_rss("https://feeds.bbci.co.uk/news/world/rss.xml", max_items=5)


def _get_interest_articles(interests: list[str]) -> list[tuple[str, str, str, str]]:
    """Return (interest, headline, google_news_url, source_name) per interest."""
    results = []
    for interest in interests:
        url = (
            f"https://news.google.com/rss/search?q={quote_plus(interest)}"
            "&hl=en-CA&gl=CA&ceid=CA:en"
        )
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "jarvis-briefing/1.0"})
            if feed.entries:
                entry = feed.entries[0]
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                source = entry.get("source", {}).get("title", "").strip()
                if title:
                    results.append((interest, title, link, source))
        except Exception:
            continue
    return results


def _get_calendar_today() -> str:
    skill_path = Path(__file__).parents[1] / "calendar" / "skill.py"
    python_path = Path(__file__).parents[1] / "calendar" / ".venv" / "bin" / "python"
    if not skill_path.exists() or not python_path.exists():
        return ""
    try:
        result = subprocess.run(
            [str(python_path), str(skill_path), "upcoming", "1"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        events = json.loads(result.stdout)
        if not events:
            return "Nothing scheduled today."
        lines = []
        for e in events:
            start = e.get("start", "")
            summary = e.get("summary", "")
            if "T" in start:
                lines.append(f"• {start.split('T')[1][:5]} — {summary}")
            else:
                lines.append(f"• All day — {summary}")
        return "\n".join(lines)
    except Exception:
        return ""


def _run_gmail_heartbeat() -> str:
    """Run gmail heartbeat; return IMMEDIATE block text if present, else empty string."""
    skill_path = Path(__file__).parents[1] / "gmail-cleanup" / "skill.py"
    python_path = Path(__file__).parents[1] / "gmail-cleanup" / ".venv" / "bin" / "python"
    if not skill_path.exists() or not python_path.exists():
        return ""
    try:
        result = subprocess.run(
            [str(python_path), str(skill_path), "heartbeat"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0 or "IMMEDIATE:" not in result.stdout:
            return ""
        lines, in_block = [], False
        for line in result.stdout.strip().split("\n"):
            if line.startswith("IMMEDIATE:"):
                in_block = True
            elif line.startswith("DIGEST ADDED:"):
                break
            if in_block:
                lines.append(line)
        return "\n".join(lines).strip()
    except Exception:
        return ""


def _pop_digest_queue() -> dict:
    """Read and clear the overnight Gmail digest queue."""
    if not DB_PATH.exists():
        return {"count": 0, "breakdown": {}}
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            "SELECT value FROM gmail_heartbeat_state WHERE key = 'digest_queue'"
        ).fetchone()
        queue = json.loads(row[0]) if row else []
        con.execute(
            "INSERT OR REPLACE INTO gmail_heartbeat_state (key, value) VALUES ('digest_queue', '[]')"
        )
        con.commit()
        con.close()
    except Exception:
        return {"count": 0, "breakdown": {}}
    breakdown: dict[str, int] = {}
    for item in queue:
        action = item.get("action", "other")
        breakdown[action] = breakdown.get(action, 0) + 1
    return {"count": len(queue), "breakdown": breakdown}


def run() -> str:
    client = anthropic.Anthropic()
    interests = _load_interests()

    weather = _get_weather()
    calendar_text = _get_calendar_today()
    world_news = _get_world_news()
    interest_articles = _get_interest_articles(interests)
    priority_text = _run_gmail_heartbeat()
    digest = _pop_digest_queue()

    context_parts = []

    if weather:
        context_parts.append(f"WEATHER: {weather}")

    context_parts.append(
        f"CALENDAR TODAY:\n{calendar_text}"
        if calendar_text
        else "CALENDAR TODAY: Nothing scheduled."
    )

    if priority_text:
        context_parts.append(f"PRIORITY INBOX (needs attention):\n{priority_text}")

    if digest["count"] > 0:
        breakdown_str = ", ".join(
            f"{count} {action}" for action, count in digest["breakdown"].items()
        )
        context_parts.append(
            f"INBOX QUEUE: {digest['count']} emails waiting for cleanup ({breakdown_str}). "
            "Say 'gmail stage' to action."
        )
    else:
        context_parts.append("INBOX QUEUE: Clear.")

    if world_news:
        lines = [f"• {title} | {link}" if link else f"• {title}" for title, link in world_news]
        context_parts.append("WORLD NEWS:\n" + "\n".join(lines))

    if interest_articles:
        lines = []
        for topic, title, link, source in interest_articles:
            if link and source:
                ref = f"([{source}](<{link}>))"
            elif link:
                ref = f"(<{link}>)"
            else:
                ref = ""
            lines.append(f"• [{topic}] {title} {ref}".strip())
        context_parts.append("INTEREST ARTICLES:\n" + "\n".join(lines))

    context = "\n\n".join(context_parts)

    prompt = (
        "You are Jarvis, AJ's personal assistant. Write a brief morning briefing based on the data below. "
        "Format for Discord: **bold** section headers, bullet points. One short greeting line to open. "
        "Keep it tight — AJ reads this first thing, so surface what matters and cut filler. "
        "For world news, write one short sentence per item summarising the story — don't repeat the headline verbatim — "
        "and include the link after in the format: (<url>). "
        "For interest articles, summarise each story in one sentence and preserve the source attribution "
        "at the end exactly as formatted (e.g. ([Tom's Hardware](<url>))).\n\n"
        f"{context}"
    )

    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip()


if __name__ == "__main__":
    print(run())
