"""Morning briefing — single Sonnet call at 7am covering calendar, weather, news, and inbox."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus

import anthropic
import feedparser
import requests
from dotenv import load_dotenv

load_dotenv(Path.home() / ".jarvis.env")

_DISCORD_SCRIPT = Path(__file__).parents[2] / "scripts" / "discord_post.py"


def _post_discord(message: str) -> None:
    """Post one message to Discord. Runs discord_post.py as a subprocess — no crash on failure."""
    try:
        subprocess.run(
            ["python3", str(_DISCORD_SCRIPT)],
            input=message,
            text=True,
            capture_output=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


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
            [str(python_path), str(skill_path), "upcoming", "0"],
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


_BRIEFING_DEDUP_SECONDS = 3600  # refuse to run twice within one hour


def _check_and_stamp_run() -> bool:
    """Return True if briefing already ran within the dedup window; stamp DB if not."""
    if not DB_PATH.exists():
        return False
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            "SELECT value FROM gmail_heartbeat_state WHERE key = 'briefing_last_run'"
        ).fetchone()
        if row:
            last = datetime.fromisoformat(row[0])
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (datetime.now(UTC) - last).total_seconds() < _BRIEFING_DEDUP_SECONDS:
                con.close()
                return True
        con.execute(
            "INSERT OR REPLACE INTO gmail_heartbeat_state (key, value) VALUES ('briefing_last_run', ?)",
            (datetime.now(UTC).isoformat(),),
        )
        con.commit()
        con.close()
    except Exception:
        pass
    return False


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


def _get_upcoming_deadlines(days: int = 14) -> str:
    """Return a formatted string of deadlines due within the next N days."""
    if not DB_PATH.exists():
        return ""
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            """SELECT title, due_date, project, days_until
               FROM (
                   SELECT title, due_date, project,
                          CAST(julianday(due_date) - julianday('now', 'localtime') AS INTEGER) AS days_until
                   FROM deadlines
                   WHERE completed = 0
               )
               WHERE days_until <= ?
               ORDER BY due_date ASC""",
            (days,),
        ).fetchall()
        con.close()
        if not rows:
            return ""
        lines = []
        for title, due_date_str, project, delta in rows:
            proj_label = f" [{project}]" if project else ""
            if delta < 0:
                urgency = f"OVERDUE by {-delta}d"
            elif delta == 0:
                urgency = "due TODAY"
            elif delta == 1:
                urgency = "due TOMORROW"
            else:
                urgency = f"due in {delta}d ({due_date_str})"
            lines.append(f"• {title}{proj_label} — {urgency}")
        return "\n".join(lines)
    except Exception:
        return ""


def run() -> list[str]:
    if _check_and_stamp_run():
        return []  # already ran within the last hour — suppress duplicate

    client = anthropic.Anthropic()
    interests = _load_interests()

    weather = _get_weather()
    calendar_text = _get_calendar_today()
    world_news = _get_world_news()
    interest_articles = _get_interest_articles(interests)
    priority_text = _run_gmail_heartbeat()
    digest = _pop_digest_queue()
    deadlines_text = _get_upcoming_deadlines(days=14)

    # --- msg1: Sonnet-generated opening (greeting + weather + calendar + inbox) ---
    context_parts = []
    if weather:
        context_parts.append(f"WEATHER: {weather}")
    context_parts.append(
        f"CALENDAR TODAY:\n{calendar_text}"
        if calendar_text
        else "CALENDAR TODAY: Nothing scheduled."
    )
    if deadlines_text:
        context_parts.append(f"UPCOMING DEADLINES (next 14 days):\n{deadlines_text}")
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

    prompt = (
        "You are Jarvis, AJ's personal assistant. Write a brief morning briefing based on the data below. "
        "Format for Discord: **bold** section headers, bullet points. One short greeting line to open. "
        "Keep it tight — AJ reads this first thing, surface what matters and cut filler.\n\n"
        + "\n\n".join(context_parts)
    )
    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    msg1 = response.content[0].text.strip()

    # --- msg2: world news (direct format) ---
    msg2 = ""
    if world_news:
        lines = ["**World News**"]
        for title, link in world_news:
            lines.append(f"• [{title}](<{link}>)" if link else f"• {title}")
        msg2 = "\n".join(lines)

    # --- msg3: interest articles (direct format) ---
    msg3 = ""
    if interest_articles:
        lines = ["**Today's Reading**"]
        for topic, title, link, source in interest_articles:
            if link and source:
                ref = f"([{source}](<{link}>))"
            elif link:
                ref = f"(<{link}>)"
            else:
                ref = ""
            lines.append(f"• **[{topic}]** {title} {ref}".strip())
        msg3 = "\n".join(lines)

    return [m for m in [msg1, msg2, msg3] if m]


if __name__ == "__main__":
    for part in run():
        _post_discord(part)
