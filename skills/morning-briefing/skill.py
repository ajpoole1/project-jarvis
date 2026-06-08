"""
Morning briefing — structured, salience-ranked brief with multi-source news radar
and a semantic reads coda powered by knowledge/preferences/interests.md.

Three messages:
  msg1 — Sonnet-composed brief (lead + salience-ranked body)
  msg2 — News radar (0-1 story, cross-source corroboration)
  msg3 — Reads coda (0-2 picks, semantic match against interest profile)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import anthropic
import feedparser
import requests
from dotenv import load_dotenv

load_dotenv(Path.home() / ".jarvis.env")

_DISCORD_SCRIPT = Path(__file__).parents[2] / "scripts" / "discord_post.py"
_KNOWLEDGE_ROOT = Path(__file__).parents[2] / "knowledge"
_INTERESTS_PATH = _KNOWLEDGE_ROOT / "preferences" / "interests.md"
_VOICE_PATH = _KNOWLEDGE_ROOT / "preferences" / "voice.md"

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "jarvis.db"
CITY = os.environ.get("JARVIS_CITY", "Montreal")
FETCH_TIMEOUT = 10

_BRIEFING_DEDUP_SECONDS = 3600
_TORONTO_TZ = ZoneInfo("America/Toronto")

# Configurable via env as JSON, e.g. '[["CBC","https://..."],...]'
_NEWS_SOURCES: list[tuple[str, str]] = json.loads(
    os.environ.get("BRIEFING_NEWS_SOURCES", "null") or "null"
) or [
    ("CBC", "https://www.cbc.ca/cmlink/rss-world"),
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
    ("Reuters", "https://feeds.reuters.com/reuters/worldNews"),
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BriefBlock:
    type: str
    salience: int  # 0–100; higher surfaces earlier in the brief
    take: str  # one-line summary for the lead / voice read
    detail: str  # full rendered content for Discord
    action: str = ""  # optional follow-up action hint


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _post_discord(message: str) -> None:
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


def _parse_rss(url: str, source_name: str, max_items: int = 8) -> list[dict]:
    """Return list of {title, link, summary, source} from an RSS feed."""
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "jarvis-briefing/1.0"})
        items = []
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()
            if title:
                items.append(
                    {"title": title, "link": link, "summary": summary, "source": source_name}
                )
        return items
    except Exception:  # noqa: BLE001
        return []


def _strip_json_fences(raw: str) -> str:
    """Remove markdown code fences from an LLM JSON response."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def _toronto_today() -> date:
    return datetime.now(_TORONTO_TZ).date()


def _render_calendar_lines(events: list[dict], today: date) -> list[str]:
    """Render event list as dated lines with Today/Tomorrow labels. No event is dateless."""
    tomorrow = today + timedelta(days=1)
    lines = []
    for e in events:
        start = e.get("start", "")
        summary = e.get("summary", "")
        try:
            if "T" in start:
                dt = datetime.fromisoformat(start)
                dt_toronto = (
                    dt.astimezone(_TORONTO_TZ) if dt.tzinfo else dt.replace(tzinfo=_TORONTO_TZ)
                )
                event_date = dt_toronto.date()
                time_str = dt_toronto.strftime("%H:%M")
            else:
                event_date = date.fromisoformat(start)
                time_str = "All day"
        except (ValueError, KeyError):
            lines.append(f"• {summary}")
            continue
        weekday = event_date.strftime("%a")
        date_str = event_date.isoformat()
        if event_date == today:
            lines.append(f"• Today ({weekday} {date_str}) · {time_str} — {summary}")
        elif event_date == tomorrow:
            lines.append(f"• Tomorrow ({weekday} {date_str}) · {time_str} — {summary}")
        else:
            lines.append(f"• {weekday} {date_str} · {time_str} — {summary}")
    return lines


# ---------------------------------------------------------------------------
# Reads dedup (SQLite)
# ---------------------------------------------------------------------------


def _init_reads_dedup() -> None:
    if not DB_PATH.exists():
        return
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS briefing_reads_dedup (
                url          TEXT PRIMARY KEY,
                title        TEXT,
                surfaced_at  TEXT NOT NULL
            )
        """)
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


def _is_reads_dedup(url: str) -> bool:
    if not DB_PATH.exists() or not url:
        return False
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute(
            "SELECT 1 FROM briefing_reads_dedup WHERE url = ?"
            " AND julianday('now') - julianday(surfaced_at) < 7",
            (url,),
        ).fetchone()
        con.close()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def _mark_reads_surfaced(picks: list[dict]) -> None:
    if not DB_PATH.exists() or not picks:
        return
    try:
        con = sqlite3.connect(DB_PATH)
        now = datetime.now(UTC).isoformat()
        for pick in picks:
            url = pick.get("link", "")
            if url:
                con.execute(
                    "INSERT OR REPLACE INTO briefing_reads_dedup (url, title, surfaced_at)"
                    " VALUES (?, ?, ?)",
                    (url, pick.get("title", ""), now),
                )
        # Prune entries older than 14 days
        con.execute(
            "DELETE FROM briefing_reads_dedup"
            " WHERE julianday('now') - julianday(surfaced_at) > 14"
        )
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Interests profile
# ---------------------------------------------------------------------------


def _load_interests_profile() -> str:
    if _INTERESTS_PATH.exists():
        return _INTERESTS_PATH.read_text()
    return ""


def _extract_interest_queries(profile: str) -> list[str]:
    """
    Parse '### N. Interest — subtitle' headers from the profile to build
    Google News search queries. Returns interest names without subtitles.
    """
    headers = re.findall(r"###\s+\d+\.\s+(.+?)(?:\n|$)", profile)
    queries = []
    for h in headers:
        # Strip subtitle (after em-dash or encoding variant)
        for sep in (" — ", " â ", " - "):
            if sep in h:
                h = h.split(sep)[0]
                break
        h = h.replace("*", "").replace("`", "").strip()
        if h:
            queries.append(h)
    return queries


def _profile_for_prompt(profile: str, max_chars: int = 2500) -> str:
    """Trim the profile for inclusion in Haiku/Sonnet prompts."""
    return profile[:max_chars]


# ---------------------------------------------------------------------------
# Dedup / stamp
# ---------------------------------------------------------------------------


def _check_and_stamp_run() -> bool:
    """Return True if briefing already ran within the dedup window."""
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
            "INSERT OR REPLACE INTO gmail_heartbeat_state (key, value)"
            " VALUES ('briefing_last_run', ?)",
            (datetime.now(UTC).isoformat(),),
        )
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------------
# Data sources — each returns a BriefBlock or None
# ---------------------------------------------------------------------------


def _get_weather() -> BriefBlock | None:
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
        raw = f"{CITY}: high {high}°C / low {low}°C, {desc.lower()}"
        return BriefBlock(type="weather", salience=35, take=raw, detail=raw)
    except Exception:  # noqa: BLE001
        return None


def _get_calendar() -> BriefBlock | None:
    skill_path = Path(__file__).parents[1] / "calendar" / "skill.py"
    python_path = Path(__file__).parents[1] / "calendar" / ".venv" / "bin" / "python"
    if not skill_path.exists() or not python_path.exists():
        return None
    try:
        # upcoming 1 = today + tomorrow (leading edge)
        result = subprocess.run(
            [str(python_path), str(skill_path), "upcoming", "1"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        events = json.loads(result.stdout)
        if not events:
            return BriefBlock(
                type="calendar",
                salience=30,
                take="Nothing scheduled.",
                detail="Nothing scheduled today.",
            )
        lines = _render_calendar_lines(events, _toronto_today())
        detail = "\n".join(lines)
        return BriefBlock(
            type="calendar",
            salience=55,
            take=f"{len(events)} calendar event(s) today/tomorrow",
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        return None


def _get_deadlines() -> BriefBlock | None:
    if not DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            """
            SELECT title, due_date, project,
                   CAST(julianday(due_date) - julianday('now', 'localtime') AS INTEGER) AS days_until
            FROM deadlines
            WHERE completed = 0
              AND CAST(julianday(due_date) - julianday('now', 'localtime') AS INTEGER) <= 14
            ORDER BY due_date ASC
            """
        ).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return None

    if not rows:
        return None

    lines = []
    max_salience = 0
    for title, due_date_str, project, delta in rows:
        proj = f" [{project}]" if project else ""
        if delta < 0:
            label = f"OVERDUE by {-delta}d"
            salience = 95
        elif delta == 0:
            label = "due TODAY"
            salience = 90
        elif delta == 1:
            label = "due TOMORROW"
            salience = 80
        else:
            label = f"due in {delta}d ({due_date_str})"
            salience = 50
        max_salience = max(max_salience, salience)
        lines.append(f"• {title}{proj} — {label}")

    most_urgent = rows[0]
    delta = most_urgent[3]
    urgency_short = "OVERDUE" if delta < 0 else ("today" if delta == 0 else f"in {delta}d")
    take = f"{most_urgent[0]} — {urgency_short}"
    return BriefBlock(
        type="deadline",
        salience=max_salience,
        take=take,
        detail="\n".join(lines),
    )


def _get_gmail_priority() -> BriefBlock | None:
    """Surface IMMEDIATE-flagged priority emails only. No inbox count."""
    skill_path = Path(__file__).parents[1] / "gmail-cleanup" / "skill.py"
    python_path = Path(__file__).parents[1] / "gmail-cleanup" / ".venv" / "bin" / "python"
    if not skill_path.exists() or not python_path.exists():
        return None
    try:
        result = subprocess.run(
            [str(python_path), str(skill_path), "heartbeat"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0 or "IMMEDIATE:" not in result.stdout:
            return None
        lines, in_block = [], False
        for line in result.stdout.strip().split("\n"):
            if line.startswith("IMMEDIATE:"):
                in_block = True
            elif line.startswith("DIGEST ADDED:"):
                break
            if in_block:
                lines.append(line)
        text = "\n".join(lines).strip()
        if not text:
            return None
        return BriefBlock(
            type="gmail-priority",
            salience=80,
            take="Priority email needs attention",
            detail=text,
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# News radar — multi-source, cross-corroboration (msg2)
# ---------------------------------------------------------------------------


def _get_news_radar(client: anthropic.Anthropic) -> BriefBlock | None:
    all_items: list[dict] = []
    for source_name, url in _NEWS_SOURCES:
        all_items.extend(_parse_rss(url, source_name, max_items=6))

    if not all_items:
        return None

    candidates_text = "\n".join(
        f"{i}. [{item['source']}] {item['title']}" for i, item in enumerate(all_items)
    )

    prompt = f"""You are the news radar for AJ's morning briefing. You have headlines from {len(_NEWS_SOURCES)} independent sources.

Your job:
1. Cluster stories that cover the same real-world event across multiple sources.
2. Rank clusters by cross-source corroboration — more independent sources = more important.
3. Surface the top story only if a cluster has 2+ independent sources. Output null if nothing clears that bar.
4. If there is a top story, write a one-line "so-what / personal-impact" hook for AJ:
   - Canadian, follows Canada/Europe/Ukraine/AI/trade closely
   - Wants the consequence (what it means for his week, his wallet, his world), not the event summary
   - Anti-doomscroll: consequential signal only, zero amplification

Output JSON only:
{{
  "top_story": {{
    "headline": "concise headline in your own words",
    "sources": ["source names that independently covered it"],
    "so_what": "one sentence — the consequence for AJ"
  }} | null,
  "quiet_summary": "one sentence about what's making noise but not consequential (or null if nothing notable)"
}}

Headlines:
{candidates_text}"""

    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(_strip_json_fences(resp.content[0].text))
    except Exception:  # noqa: BLE001
        return None

    top = data.get("top_story")
    quiet_summary = data.get("quiet_summary") or "Nothing consequential today."

    if top and top.get("headline"):
        sources_str = " / ".join(top.get("sources", []))
        detail_lines = [
            f"**{top['headline']}** ({sources_str})",
            top.get("so_what", ""),
        ]
        return BriefBlock(
            type="news-radar",
            salience=65,
            take=top["headline"],
            detail="\n".join(line for line in detail_lines if line),
            action="Say 'news' for full headlines.",
        )

    return BriefBlock(
        type="news-radar",
        salience=15,
        take=f"Quiet — {quiet_summary}",
        detail=f"World's quiet for you today — {quiet_summary} Say 'news' for headlines.",
    )


# ---------------------------------------------------------------------------
# Reads coda — semantic interest matching (msg3)
# ---------------------------------------------------------------------------


def _get_reads_coda(client: anthropic.Anthropic, profile: str) -> BriefBlock | None:
    if not profile:
        return None

    _init_reads_dedup()

    interest_queries = _extract_interest_queries(profile)
    if not interest_queries:
        return None

    # Prefilter: Google News RSS per interest (rough keyword prefilter)
    candidates: list[dict] = []
    for query in interest_queries:
        url = (
            f"https://news.google.com/rss/search?q={quote_plus(query)}" "&hl=en-CA&gl=CA&ceid=CA:en"
        )
        for item in _parse_rss(url, "Google News", max_items=3):
            if not _is_reads_dedup(item.get("link", "")):
                candidates.append(item)

    if not candidates:
        return None

    # Title-dedup
    seen: set[str] = set()
    unique: list[dict] = []
    for c in candidates:
        key = c["title"].lower()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(c)

    candidates = unique[:20]

    profile_ctx = _profile_for_prompt(profile)

    # Haiku bulk scoring
    candidates_text = "\n".join(
        f"{i}. [{c['source']}] {c['title']}"
        + (f" — {c['summary'][:100]}" if c.get("summary") else "")
        for i, c in enumerate(candidates)
    )

    score_prompt = f"""Score these articles for AJ's interest profile. Score each 0–10.

AJ's interest profile (condensed):
{profile_ctx}

Scoring:
- 10 = excellent semantic match AND passes both cross-cutting filters:
    Tonal: grounded/grey/analytical (NOT hype/camp/boosterism/"10 things...")
    Practitioner: applied/usable/concrete (NOT pure academic theory or market noise)
- 6–9 = good match, minor concerns
- 1–5 = weak match or borderline
- 0 = fails filters (hype, clickbait, wrong topic)

Output JSON only — array:
[{{"index": 0, "score": 7}}, ...]

Articles:
{candidates_text}"""

    try:
        score_resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": score_prompt}],
        )
        scored = json.loads(_strip_json_fences(score_resp.content[0].text))
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        top_indices = [s["index"] for s in scored if s.get("score", 0) >= 6][:4]
    except Exception:  # noqa: BLE001
        return None

    if not top_indices:
        return None

    top_candidates = [candidates[i] for i in top_indices if i < len(candidates)]
    if not top_candidates:
        return None

    # Sonnet picks final 1–2 and writes why-lines
    top_text = "\n".join(
        f"{i}. {c['title']} ({c['source']})"
        + (f" — {c['summary'][:200]}" if c.get("summary") else "")
        for i, c in enumerate(top_candidates)
    )

    pick_prompt = f"""Pick 1–2 articles for AJ's morning reading coda. Zero is fine — don't force a pick to fill space.

AJ's interest profile:
{profile_ctx}

Rules:
- Genuine semantic match to AJ's interests AND both cross-cutting filters (grounded, applied)
- Convergence pieces (hitting 2+ interests) are high-value
- Write one direct "why this matters to you" line per pick — addressed to AJ in second person ("you"/"your"). Never write in third person; never use "AJ is…" framing. Jarvis's voice: short, confident, no openers.
- Prefer where "interesting" and "useful" merge

Output JSON only:
[{{"index": 0, "why": "one sentence"}}]

Candidates:
{top_text}"""

    try:
        pick_resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": pick_prompt}],
        )
        picks = json.loads(_strip_json_fences(pick_resp.content[0].text))
    except Exception:  # noqa: BLE001
        return None

    if not picks:
        return None

    final: list[dict] = []
    for p in picks[:2]:
        idx = p.get("index", 0)
        if idx < len(top_candidates):
            final.append(
                {
                    "title": top_candidates[idx]["title"],
                    "link": top_candidates[idx]["link"],
                    "source": top_candidates[idx]["source"],
                    "why": p.get("why", ""),
                }
            )

    if not final:
        return None

    _mark_reads_surfaced(final)

    lines = ["**Worth a look**"]
    for pick in final:
        link = pick.get("link", "")
        source = pick.get("source", "")
        title = pick.get("title", "")
        why = pick.get("why", "")
        ref = f"[{source}](<{link}>)" if link and source else (f"<{link}>" if link else source)
        lines.append(f"• {title} ({ref}) — {why}")

    return BriefBlock(
        type="reads-coda",
        salience=30,
        take=f"{len(final)} read(s) queued",
        detail="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Brief composition (msg1)
# ---------------------------------------------------------------------------


def _build_brief_prompt(blocks: list[BriefBlock], today: date, voice_text: str) -> str:
    anchor = f"Today is {today.strftime('%A')}, {today.isoformat()} (America/Toronto)."
    ranked = sorted(blocks, key=lambda b: b.salience, reverse=True)
    context_parts = [f"[{b.type.upper()} | salience={b.salience}]\n{b.detail}" for b in ranked]
    return (
        f"{anchor}\n\n"
        "You are Jarvis, AJ's personal assistant. Write AJ's morning briefing.\n\n"
        "Rules:\n"
        "- Open with ONE lead sentence naming the single most important thing today."
        " If nothing is urgent, say so plainly.\n"
        "- Salience-ranked body — surface what changes a decision today."
        " Compress or drop what recurs and changes nothing (e.g. a standing daily session at the same time every day).\n"
        "- Frame weather as the *decision it drives*"
        " (how to dress Ellie, whether to bring an umbrella), not raw numbers.\n"
        "- No inbox status, no email count — those are on-demand only.\n"
        "- Voice: direct, dry, no filler openers, no trailing affirmations."
        " Short confident sentences. Occasional dry wit is fine.\n"
        "- Format for Discord: **bold** headers where useful, bullet points. Tight.\n\n"
        f"Voice and tone reference:\n{voice_text}\n\n"
        "Data blocks (salience-ranked):\n\n" + "\n\n".join(context_parts)
    )


def _compose_brief(
    client: anthropic.Anthropic, blocks: list[BriefBlock], today: date | None = None
) -> str:
    """Sonnet composes the salience-ranked morning brief from the block data."""
    if today is None:
        today = _toronto_today()
    voice_text = _VOICE_PATH.read_text() if _VOICE_PATH.exists() else ""
    prompt = _build_brief_prompt(blocks, today, voice_text)
    resp = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ---------------------------------------------------------------------------
# Dev-crew standup
# ---------------------------------------------------------------------------


def _get_dev_crew_standup() -> BriefBlock | None:
    devloop_skill = Path(__file__).parents[1] / "devloop" / "skill.py"
    if not devloop_skill.exists():
        return None
    try:
        result = subprocess.run(
            ["python3", str(devloop_skill), "standup"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        text = result.stdout.strip()
    except Exception:  # noqa: BLE001
        return None
    if not text or "Nothing active" in text:
        return None
    return BriefBlock(type="dev-crew", salience=20, take="Dev-crew has active items.", detail=text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run() -> list[str]:
    if _check_and_stamp_run():
        return []

    client = anthropic.Anthropic()
    profile = _load_interests_profile()

    # Gather body blocks
    blocks: list[BriefBlock] = []
    for fn in (
        _get_weather,
        _get_calendar,
        _get_deadlines,
        _get_gmail_priority,
        _get_dev_crew_standup,
    ):
        block = fn()
        if block:
            blocks.append(block)

    # msg1 — composed brief
    msg1 = (
        _compose_brief(client, blocks)
        if blocks
        else "Good morning, sir. Nothing notable to report."
    )

    # msg2 — news radar (separate section, always attempted)
    news_block = _get_news_radar(client)
    msg2 = news_block.detail if news_block else ""

    # msg3 — reads coda (separate section, profile-gated)
    reads_block = _get_reads_coda(client, profile)
    msg3 = reads_block.detail if reads_block else ""

    return [m for m in [msg1, msg2, msg3] if m]


if __name__ == "__main__":
    for part in run():
        _post_discord(part)
