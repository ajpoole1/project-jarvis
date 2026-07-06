#!/usr/bin/env python3
"""
Nightly checkpoint-restart — 2026-0042.

Three phases, run from an external WSL cron at 04:00:
  1. CAPTURE  — read active Discord session transcript, headless Haiku distill,
                stage undocumented threads to knowledge_pending_writes
                (provenance=nightly-checkpoint), write LIVE_STATE summary.
  2. RESTART  — systemctl --user restart openclaw-gateway.service
                (gated: skipped if capture failed, to never lose threads)
  3. VERIFY   — poll until gateway is up + RSS clean, send a bootstrap-confirm
                message via the Discord bot, alert on dirty boot.

Quiet-hours gate: only fires after midnight with no session activity in the
last 30 minutes (checked via sessions.json updatedAt).

Runs OUTSIDE the gateway. Never imported by any skill. Jarvis cannot
self-restart — this script is what triggers the restart.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

PROJECT = Path("/opt/jarvis-live")
DB_PATH = PROJECT / "data/jarvis.db"
DISCORD_SCRIPT = PROJECT / "scripts/discord_post.py"
WORKSPACE = Path.home() / ".openclaw/workspace"
LIVE_STATE_PATH = WORKSPACE / "memory/LIVE_STATE.md"
SESSIONS_JSON = Path.home() / ".openclaw/agents/main/sessions/sessions.json"
SESSIONS_DIR = Path.home() / ".openclaw/agents/main/sessions"

# The Discord channel session key — identifies the active conversation session
DISCORD_SESSION_KEY = "agent:main:discord:channel:1498323405047595181"

GATEWAY_UNIT = "openclaw-gateway.service"
TZ = ZoneInfo("America/Toronto")

# Quiet-hours gate: no activity for this many minutes
QUIET_MINUTES = 30

# After restart, how long to wait for the gateway to come up (seconds)
BOOT_TIMEOUT = 120
BOOT_POLL_INTERVAL = 5

# Haiku sweep settings (reuse compaction_sweep design)
MIN_CONFIDENCE = 0.6
MAX_WINDOW_CHARS = 80_000

SWEEP_PROMPT = """\
You are scanning a conversation session that is about to be restarted overnight.
Your job: find any durable synthesis or locked decisions that exist ONLY in this
session and were never explicitly saved to a knowledge file.

SCOPE — only flag:
- Research or analysis AJ asked to "park", "save", or "remember" but no save
  was confirmed (or the conversation ended before confirming)
- Explicit decisions or conclusions that would be re-derived from scratch in
  the next session (architecture choices, product decisions, resolved ambiguities)
- Factual summaries AJ built up over the session that aren't captured in any
  skill's DB state

DO NOT flag:
- Any item where AJ confirmed a save or the knowledge skill was called
- Gmail/calendar/task/price/grocery actions — those are in the DB
- Ongoing dev tasks in progress — those will re-surface naturally from CONTEXT.md
- General conversation, questions, or exploratory discussion

For each item you find, output a JSON array with objects:
  {
    "title": "Short artifact title (max 60 chars)",
    "summary": "Distilled artifact — the clean reference, NOT transcript. Max 500 chars.",
    "suggested_path": "Where this would live in knowledge/ or ~/.jarvis/knowledge/ (best guess)",
    "confidence": 0.0-1.0
  }

If nothing warrants saving, output an empty array: []
Output ONLY the JSON array, no other text.
"""


# ---------------------------------------------------------------------------
# Env loading
# ---------------------------------------------------------------------------


def _load_env() -> None:
    env_path = Path.home() / ".jarvis.env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------


def _post_discord(message: str, mention: bool = False) -> None:
    """Post to Discord via webhook. Best-effort — never raises."""
    try:
        if mention:
            uid = os.environ.get("DISCORD_NOTIFY_USER_ID", "")
            if uid:
                message = f"<@{uid}> {message}"
        subprocess.run(
            ["python3", str(DISCORD_SCRIPT)],
            input=message,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        pass


def _alert(message: str) -> None:
    """Post a loud alert — with user mention."""
    _post_discord(f"🚨 **Nightly checkpoint:** {message}", mention=True)


def _info(message: str) -> None:
    _post_discord(f"🌙 **Nightly checkpoint:** {message}")


# ---------------------------------------------------------------------------
# Quiet-hours gate
# ---------------------------------------------------------------------------


def _check_quiet_gate() -> tuple[bool, str]:
    """
    Returns (ok, reason). Gate passes if:
    - Current hour is between 00:00 and 06:00 local
    - Last Discord session activity > QUIET_MINUTES ago
    """
    now = datetime.now(TZ)
    if not (0 <= now.hour < 6):
        return False, f"outside quiet window (now {now.strftime('%H:%M')})"

    if not SESSIONS_JSON.exists():
        return True, "no sessions.json — treating as quiet"

    try:
        # sessions.json is a flat dict: { "agent:main:discord:...": {sessionId, updatedAt, ...} }
        sessions_data = json.loads(SESSIONS_JSON.read_text())
    except Exception as e:
        return True, f"sessions.json unreadable ({e}) — treating as quiet"

    entry = sessions_data.get(DISCORD_SESSION_KEY)
    if not entry:
        return True, "no Discord session found — treating as quiet"

    updated_ms = entry.get("updatedAt", 0)
    updated_dt = datetime.fromtimestamp(updated_ms / 1000, tz=UTC)
    age_minutes = (datetime.now(UTC) - updated_dt).total_seconds() / 60
    if age_minutes < QUIET_MINUTES:
        return False, f"session active {age_minutes:.0f}min ago (< {QUIET_MINUTES}min threshold)"
    return True, f"session last active {age_minutes:.0f}min ago"


# ---------------------------------------------------------------------------
# Phase 1: Capture
# ---------------------------------------------------------------------------


def _read_session_transcript() -> str | None:
    """Read the active Discord session JSONL and extract message text."""
    if not SESSIONS_JSON.exists():
        return None

    try:
        # sessions.json is a flat dict: { "agent:main:discord:...": {sessionId, updatedAt, ...} }
        sessions_data = json.loads(SESSIONS_JSON.read_text())
    except Exception:
        return None

    entry = sessions_data.get(DISCORD_SESSION_KEY)
    if not entry:
        return None

    # Prefer the explicit sessionFile path; fall back to sessionId-derived path
    session_file = entry.get("sessionFile")
    jsonl_path = Path(session_file) if session_file else None

    if not jsonl_path or not jsonl_path.exists():
        session_id = entry.get("sessionId")
        if session_id:
            jsonl_path = SESSIONS_DIR / f"{session_id}.jsonl"

    if not jsonl_path or not jsonl_path.exists():
        # Last resort: most recently modified non-churn .jsonl in sessions dir
        candidates = [
            p
            for p in SESSIONS_DIR.glob("*.jsonl")
            if "churn" not in p.name and "trajectory" not in p.name
        ]
        if not candidates:
            return None
        jsonl_path = max(candidates, key=lambda p: p.stat().st_mtime)

    lines = []
    try:
        for raw in jsonl_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            obj = json.loads(raw)
            msg = obj.get("message", {})
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text"
                )
            else:
                text = str(content)
            text = text.strip()
            if text:
                lines.append(f"{role.upper()}: {text}")
    except Exception:
        return None

    return "\n\n".join(lines) if lines else None


_STRICT_SYSTEM = "Output ONLY a raw JSON array, no markdown, no prose, no code fences."


def _extract_json_array(text: str) -> list[dict]:
    """
    Extract the first balanced [...] array from text, tolerating markdown fences
    and any surrounding prose. Raises ValueError if no valid array found.
    """
    import re

    # Strip ```json ... ``` or ``` ... ``` fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)

    # Find the first '[' and its matching ']'
    start = text.find("[")
    if start == -1:
        raise ValueError(f"no JSON array found in output: {text[:200]!r}")

    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                result = json.loads(candidate)
                if not isinstance(result, list):
                    raise ValueError(f"parsed value is not a list: {type(result)}")
                return result

    raise ValueError(f"unbalanced brackets in output: {text[:200]!r}")


def _call_haiku(window_text: str, strict: bool = False) -> str:
    """Make one Haiku API call. Returns the raw text content."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    payload: dict = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2048,
        "messages": [
            {
                "role": "user",
                "content": f"{SWEEP_PROMPT}\n\n<session_window>\n{window_text}\n</session_window>",
            }
        ],
    }
    if strict:
        payload["system"] = _STRICT_SYSTEM

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        response = json.loads(resp.read().decode())

    return response["content"][0]["text"].strip()


def _run_haiku_sweep(window_text: str) -> list[dict]:
    """
    Run headless Haiku sweep. Retries once with a strict system prompt on parse
    failure. Returns list of artifact dicts (may be empty).
    Raises only on HTTP/auth errors or total parse failure after retry.
    """
    # Attempt 1 — standard call
    text = _call_haiku(window_text, strict=False)
    try:
        return _extract_json_array(text)
    except (ValueError, json.JSONDecodeError) as first_exc:
        print(
            f"[nightly-checkpoint] haiku parse attempt 1 failed ({first_exc}); "
            f"raw[:500]={text[:500]!r}; retrying with strict system prompt",
            file=sys.stderr,
        )

    # Attempt 2 — strict system prompt
    text2 = _call_haiku(window_text, strict=True)
    try:
        return _extract_json_array(text2)
    except (ValueError, json.JSONDecodeError) as second_exc:
        print(
            f"[nightly-checkpoint] haiku parse attempt 2 failed ({second_exc}); "
            f"raw[:500]={text2[:500]!r}",
            file=sys.stderr,
        )
        raise ValueError(f"Haiku output unparseable after 2 attempts: {second_exc}") from second_exc


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_pending_writes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            suggested_path TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK(provenance IN ("explicit-capture","compaction-rescue","nightly-checkpoint","nightly-checkpoint-raw")),
            confidence REAL NOT NULL DEFAULT 1.0,
            filed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_pending_writes)")}
    for col, defn in [
        ("provenance", "TEXT NOT NULL DEFAULT 'explicit-capture'"),
        ("confidence", "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE knowledge_pending_writes ADD COLUMN {col} {defn}")
    conn.commit()


def _stage_raw_fallback(transcript: str) -> None:
    """
    Write the full session transcript as a raw pending-write row so no thread
    is lost when the Haiku sweep output is unparseable.
    Provenance 'nightly-checkpoint-raw' flags it for manual review.
    """
    conn = sqlite3.connect(str(DB_PATH))
    try:
        _ensure_schema(conn)
        now = datetime.now(TZ).isoformat()
        date_tag = datetime.now(TZ).strftime("%Y-%m-%d")
        conn.execute(
            """INSERT INTO knowledge_pending_writes
               (title, summary, suggested_path, provenance, confidence, filed, created_at)
               VALUES (?, ?, ?, 'nightly-checkpoint-raw', 0.0, 0, ?)""",
            (
                f"[RAW] Session transcript {date_tag}",
                transcript,
                "~/.jarvis/knowledge/personal/",
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _stage_artifacts(artifacts: list[dict]) -> int:
    """Stage artifacts to knowledge_pending_writes. Returns count staged."""
    to_stage = [a for a in artifacts if float(a.get("confidence", 0)) >= MIN_CONFIDENCE]
    if not to_stage:
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    try:
        _ensure_schema(conn)
        now = datetime.now(TZ).isoformat()
        for artifact in to_stage:
            conn.execute(
                """INSERT INTO knowledge_pending_writes
                   (title, summary, suggested_path, provenance, confidence, filed, created_at)
                   VALUES (?, ?, ?, 'nightly-checkpoint', ?, 0, ?)""",
                (
                    artifact.get("title", "Untitled")[:60],
                    artifact.get("summary", "")[:500],
                    artifact.get("suggested_path", ""),
                    float(artifact.get("confidence", 0)),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return len(to_stage)


def _update_live_state(summary_line: str) -> None:
    """Append/replace the nightly-checkpoint line in LIVE_STATE.md."""
    LIVE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = LIVE_STATE_PATH.read_text(encoding="utf-8") if LIVE_STATE_PATH.exists() else ""

    marker = "**Nightly checkpoint:**"
    new_line = f"{marker} {summary_line}"

    lines = content.splitlines()
    # Replace existing checkpoint line if present
    replaced = False
    for i, line in enumerate(lines):
        if marker in line:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append("")
        lines.append(new_line)

    tmp = LIVE_STATE_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(LIVE_STATE_PATH)


def phase_capture() -> tuple[bool, str]:
    """
    Run capture phase. Returns (success, description).
    success=False means restart must be skipped.
    """
    transcript = _read_session_transcript()

    if not transcript:
        _update_live_state("no session transcript found — nothing to capture.")
        return True, "no transcript"

    if len(transcript) > MAX_WINDOW_CHARS:
        # Too large to sweep safely — do NOT block the restart, just warn
        _alert(
            f"session transcript too large for auto-sweep ({len(transcript):,} chars). "
            "Proceeding with restart — eyeball the session for anything worth saving."
        )
        _update_live_state("transcript too large for sweep — manual review needed.")
        return True, "transcript too large, warned"

    try:
        artifacts = _run_haiku_sweep(transcript)
    except Exception as exc:
        # Transcript was read successfully — data-loss risk is low.
        # Write the raw transcript as a fallback so nothing is lost, then
        # proceed with the restart rather than silently skipping it forever.
        try:
            _stage_raw_fallback(transcript)
            _alert(
                f"Haiku sweep unparseable ({exc}). "
                "Raw transcript saved to `knowledge pending` (provenance=nightly-checkpoint-raw) "
                "for manual review. Proceeding with restart."
            )
        except Exception as db_exc:
            # Raw write also failed — transcript at genuine risk of loss, skip restart.
            _alert(
                f"Haiku sweep failed ({exc}) AND raw fallback failed ({db_exc}). "
                "Skipping restart to preserve session."
            )
            return False, f"haiku failed + raw fallback failed: {db_exc}"
        return True, f"haiku parse failed ({exc}), raw fallback written, restart proceeding"

    if not artifacts:
        _update_live_state("sweep found nothing undocumented — clean slate.")
        return True, "nothing to capture"

    try:
        count = _stage_artifacts(artifacts)
    except Exception as exc:
        _alert(f"Failed to stage {len(artifacts)} artifact(s): {exc}. Skipping restart.")
        return False, f"stage failed: {exc}"

    if count > 0:
        titles = "; ".join(a.get("title", "untitled") for a in artifacts[:3])
        if len(artifacts) > 3:
            titles += f" (+{len(artifacts) - 3} more)"
        _update_live_state(
            f"{count} item(s) captured before restart: {titles}. "
            "Use `knowledge pending` to review."
        )
        _info(f"captured {count} item(s) before restart — {titles}.")
    else:
        _update_live_state("sweep found items but none met confidence threshold.")

    return True, f"captured {count} item(s)"


# ---------------------------------------------------------------------------
# Phase 2: Restart
# ---------------------------------------------------------------------------


def phase_restart() -> tuple[bool, str]:
    """Restart the gateway. Returns (success, description)."""
    env = os.environ.copy()
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    result = subprocess.run(
        ["systemctl", "--user", "restart", GATEWAY_UNIT],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        return False, f"systemctl restart failed: {result.stderr.strip()[:200]}"
    return True, "restart issued"


# ---------------------------------------------------------------------------
# Phase 3: Verify boot
# ---------------------------------------------------------------------------


def _gateway_rss_mb() -> int | None:
    try:
        result = subprocess.run(
            ["ps", "-eo", "rss,comm"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and "openclaw-gatewa" in parts[1]:
                return int(parts[0]) // 1024
    except Exception:
        pass
    return None


def _send_bootstrap_confirm() -> None:
    """Send a message via Discord webhook prompting the agent to confirm bootstrap."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return
    msg = (
        "🌙 Nightly checkpoint complete. Confirm bootstrap: "
        "acknowledge this message and confirm your spine (SOUL.md, persona, LIVE_STATE) is loaded."
    )
    try:
        data = json.dumps({"content": msg}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/project-jarvis, 1.0)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def phase_verify() -> tuple[bool, str]:
    """Poll for clean boot. Returns (success, description)."""
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(BOOT_POLL_INTERVAL)
        rss = _gateway_rss_mb()
        if rss is not None:
            # Process is up — send bootstrap confirm message
            _send_bootstrap_confirm()
            return True, f"gateway up at {rss} MB RSS"

    return False, f"gateway did not come up within {BOOT_TIMEOUT}s"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    _load_env()

    # Quiet-hours gate
    gate_ok, gate_reason = _check_quiet_gate()
    if not gate_ok:
        # Silent exit — not an error, just not the right time
        print(f"[nightly-checkpoint] gate not met: {gate_reason}", file=sys.stderr)
        return

    print(f"[nightly-checkpoint] starting — {gate_reason}", file=sys.stderr)

    # Phase 1: Capture
    capture_ok, capture_desc = phase_capture()
    print(f"[nightly-checkpoint] capture: {capture_desc}", file=sys.stderr)

    if not capture_ok:
        # Alert already posted inside phase_capture
        print("[nightly-checkpoint] capture failed — skipping restart", file=sys.stderr)
        return

    # Phase 2: Restart
    restart_ok, restart_desc = phase_restart()
    print(f"[nightly-checkpoint] restart: {restart_desc}", file=sys.stderr)

    if not restart_ok:
        _alert(f"restart failed: {restart_desc}. Gateway still running on old process.")
        return

    # Phase 3: Verify
    verify_ok, verify_desc = phase_verify()
    print(f"[nightly-checkpoint] verify: {verify_desc}", file=sys.stderr)

    if not verify_ok:
        _alert(
            f"boot verification failed: {verify_desc}. "
            "Gateway may not be up — check before relying on Jarvis at 7am. "
            f"`systemctl --user status {GATEWAY_UNIT}`"
        )
        # One retry
        print("[nightly-checkpoint] retrying restart...", file=sys.stderr)
        restart_ok2, restart_desc2 = phase_restart()
        if restart_ok2:
            verify_ok2, verify_desc2 = phase_verify()
            if verify_ok2:
                _info(f"retry succeeded — {verify_desc2}.")
            else:
                _alert(f"retry also failed: {verify_desc2}. Manual intervention needed.")
        else:
            _alert(f"retry restart also failed: {restart_desc2}.")
        return

    _info(f"complete — {verify_desc}. Capture: {capture_desc}.")


if __name__ == "__main__":
    main()
