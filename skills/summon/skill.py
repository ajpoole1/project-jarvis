#!/usr/bin/env python3
"""
summon skill — launch and manage named builder sessions via claude remote-control.

Commands:
  summon <persona> <id>             Launch a named RC builder for an authorized item
  summon <persona> <id> --revise    Resume a refix cycle on the existing feature branch,
                                    seeding Tom's QA findings + PR link into the kickoff
  ask <id> <question...>    (builder) post a blocking question to the dev-loop Discord
  watchdog-check <id> [min] (scheduler) stall check + milestone ping; prints if action needed
  verdict-sweep             (scheduler) check all active builds for new Tom QA verdicts
  dismiss <persona>         Kill the tmux session + worktree for a persona
  reaper                    Kill all crew tmux sessions idle past IDLE_MINUTES
  status                    List live crew sessions with idle times

Kickoff model (DEV_LOOP_REFERENCE §8 / BUILD_RUNBOOK):
  The kickoff is delivered as a positional prompt to an interactive Remote-Control
  session — `claude --remote-control "<name>" --permission-mode <mode> "<kickoff>"` —
  which auto-submits it on startup. The builder runs in a dedicated git WORKTREE so it
  never disturbs the operator's checkout; the worktree inherits the repo's trust, so no
  trust dialog blocks the unattended start. There is no `tmux send-keys` to a server
  console: the previous send-keys path raced the session and is the bug this replaces.

Surfacing model (Part 3b/3c):
  - A builder that hits ambiguity runs `ask`, which posts the question to the MAIN
    Discord channel (scripts/discord_post.py → DISCORD_WEBHOOK_URL) so it reaches AJ
    wherever he is, and records that the run is blocked.
  - At summon time a once@ schedule is armed (reusing the schedules skill). N minutes
    later the heartbeat dispatcher runs `watchdog-check`; if the build has neither
    opened a PR/pushed its branch nor posted a question, the check prints a stall alert
    that the dispatcher forwards to Discord. The one-off then retires itself.

Requirements:
  - tmux installed
  - claude CLI v2.1.52+ (remote-control support)
  - scripts/dev-crew/launch.sh (Phase 1 auth wrapper; pins the builder model)
  - knowledge/dev-crew/roster.md (persona definitions)

Security:
  - Only the operator may summon; never called automatically on authorize.
  - Validated argv: persona must match a roster entry; id format checked.
  - Concurrency guard: warns when live builder count exceeds threshold.
  - Auth assertion is delegated to launch.sh (aborts + alerts on mismatch).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[2]
ROSTER_FILE = PROJECT / "knowledge/dev-crew/roster.md"
LAUNCH_SH = PROJECT / "scripts/dev-crew/launch.sh"
DISCORD_SCRIPT = PROJECT / "scripts/discord_post.py"
SCHEDULES_SKILL = PROJECT / "skills/schedules/skill.py"
RUNBOOK = "docs/dev-loop/BUILD_RUNBOOK.md"
DEVNOTES_QUEUE = "knowledge/dev-notes/queue"

# Load ~/.jarvis.env so JARVIS_DATA_DIR / DISCORD_WEBHOOK_URL resolve whether this skill
# is called by the operator, by a builder session, or by the heartbeat dispatcher.
_env_path = Path.home() / ".jarvis.env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# tmux session names are prefixed so we can identify crew sessions
TMUX_PREFIX = "crew-"

# Minimum claude version for remote-control
RC_MIN_VERSION = (2, 1, 52)

# Warn if this many builder sessions are live simultaneously
CONCURRENCY_WARN_THRESHOLD = 2

# Reaper kills sessions idle longer than this
IDLE_MINUTES = 60

# Escalating watchdog: minutes after summon to run each stall/milestone check.
WATCHDOG_CHECKS = [5, 15, 30]

_LOCAL_TZ = ZoneInfo("America/Toronto")


# ── roster parser ─────────────────────────────────────────────────────────────


def load_roster() -> dict[str, dict]:
    """Parse roster.md; return {id: fields}."""
    if not ROSTER_FILE.exists():
        return {}
    text = ROSTER_FILE.read_text(encoding="utf-8")
    personas: dict[str, dict] = {}
    # Each persona block: ## Heading followed by ```yaml ... ```
    for block in re.finditer(r"##\s+(.+?)\n+```yaml\n(.*?)```", text, re.DOTALL):
        raw = block.group(2)
        fields: dict = {}
        for line in raw.splitlines():
            # simple key: value (skip list items for now)
            m = re.match(r"^(\w+):\s*(.+)", line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
        if "id" in fields:
            personas[fields["id"]] = fields
    return personas


def get_persona(persona_id: str) -> dict:
    roster = load_roster()
    if not roster:
        raise SystemExit(f"Roster not found at {ROSTER_FILE}")
    p = roster.get(persona_id)
    if not p:
        available = ", ".join(roster.keys()) or "(none)"
        raise SystemExit(f"Persona '{persona_id}' not in roster. Available: {available}")
    for req in ("name", "repo_dir", "role", "permission_mode"):
        if req not in p:
            raise SystemExit(f"Persona '{persona_id}' missing required field '{req}' in roster")
    return p


# ── version check ─────────────────────────────────────────────────────────────


def check_claude_version() -> None:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        raw = out.stdout.strip()
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
        if not m:
            print(
                f"Warning: could not parse claude version from '{raw}' — proceeding",
                file=sys.stderr,
            )
            return
        actual = tuple(int(x) for x in m.groups())
        if actual < RC_MIN_VERSION:
            raise SystemExit(
                f"claude v{'.'.join(str(x) for x in actual)} < required "
                f"v{'.'.join(str(x) for x in RC_MIN_VERSION)} for remote-control"
            )
    except FileNotFoundError:
        raise SystemExit("claude CLI not found on PATH") from None


# ── tmux helpers ──────────────────────────────────────────────────────────────


def session_name(persona_id: str) -> str:
    return f"{TMUX_PREFIX}{persona_id}"


def list_crew_sessions() -> list[str]:
    """Return list of live crew tmux session names."""
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        return [s for s in out.stdout.splitlines() if s.startswith(TMUX_PREFIX)]
    except FileNotFoundError:
        raise SystemExit("tmux not found — install tmux to use summon") from None


def session_idle_seconds(sname: str) -> int:
    """Return seconds since last activity in the session's active pane."""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-t", sname, "-p", "#{pane_last_used}"],
            capture_output=True,
            text=True,
        )
        last_used = int(out.stdout.strip() or "0")
        return max(0, int(time.time()) - last_used)
    except (ValueError, subprocess.SubprocessError):
        return 0


def kill_session(sname: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", sname], capture_output=True)


# ── URL capture ───────────────────────────────────────────────────────────────


def capture_rc_url(url_file: Path, timeout: int = 15) -> str | None:
    """Poll url_file for the RC URL printed by claude. Best-effort."""
    deadline = time.time() + timeout
    url_pattern = re.compile(r"https://claude\.ai/code[^\s\"']+")
    while time.time() < deadline:
        if url_file.exists():
            content = url_file.read_text(errors="ignore")
            m = url_pattern.search(content)
            if m:
                return m.group(0)
        time.sleep(0.5)
    return None


def _fetch_pr_info(repo_dir: str, item_id: str) -> tuple[str, str]:
    """Return (pr_url, tom_findings_comment_body). Best-effort; ('', '') on failure."""
    branch = f"feature/{item_id}"
    try:
        pr = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number,url"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if pr.returncode != 0 or not pr.stdout.strip():
            return "", ""
        data = json.loads(pr.stdout or "[]")
        if not isinstance(data, list) or not data:
            return "", ""
        pr_number = str(data[0]["number"])
        pr_url = data[0]["url"]
        comments = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "comments"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if comments.returncode != 0:
            return pr_url, ""
        cdata = json.loads(comments.stdout or "{}")
        for c in reversed(cdata.get("comments", [])):
            body = c.get("body", "")
            if "Tom QA" in body:
                return pr_url, body
        return pr_url, ""
    except Exception:  # noqa: BLE001
        return "", ""


# ── concurrency guard ─────────────────────────────────────────────────────────


def concurrency_check() -> None:
    live = list_crew_sessions()
    if len(live) >= CONCURRENCY_WARN_THRESHOLD:
        names = ", ".join(live)
        print(
            f"Warning: {len(live)} builder session(s) already live ({names}). "
            "Proceeding — dismiss idle sessions with: summon dismiss <persona>",
            file=sys.stderr,
        )


# ── worktree + kickoff (pure helpers, unit-tested) ────────────────────────────


def worktree_path(repo_dir: str, item_id: str) -> Path:
    """Dedicated build worktree for an item — a sibling of the repo, never nested in it."""
    return Path(repo_dir).resolve().parent / f"jarvis-build-{item_id}"


def build_kickoff(persona_name: str, item_id: str) -> str:
    """Single-line kickoff prompt auto-submitted into the RC session.

    Kept to one line: it is passed as a positional CLI argument, shell-quoted by the
    caller. No embedded newlines.
    """
    spec_path = f"{DEVNOTES_QUEUE}/{item_id}.md"
    return (
        f"You are {persona_name}, a Jarvis dev-loop builder. "
        f"Load {RUNBOOK}, then run: scripts/dev-loop/start-build.sh {item_id} "
        f"(the spec is at {spec_path} on the dev-queue branch). "
        f"Follow the runbook exactly. Build on the feature branch, run tests and lint, "
        f"then open a PR with scripts/dev-loop/open-pr.sh {item_id}. "
        f"If the spec is under-specified for a required decision, do NOT guess: post your "
        f'question with `python3 skills/summon/skill.py ask {item_id} "<your question>"` '
        f"(this posts to the dev-loop Discord and records that you are blocked), then stop."
    )


def build_revise_kickoff(persona_name: str, item_id: str, pr_url: str, tom_findings: str) -> str:
    """Single-line refix kickoff: resume on the existing feature branch with Tom's feedback.

    Kept to one line for the same reason as build_kickoff.
    """
    spec_path = f"{DEVNOTES_QUEUE}/{item_id}.md"
    if tom_findings:
        findings_summary = (tom_findings[:2000] + "…") if len(tom_findings) > 2000 else tom_findings
    else:
        findings_summary = "(Tom findings unavailable — check the PR directly)"
    pr_ref = pr_url if pr_url else f"(find open PR for feature/{item_id})"
    return (
        f"You are {persona_name}, a Jarvis dev-loop builder resuming a refix cycle for {item_id}. "
        f"The branch feature/{item_id} already exists — do NOT run start-build.sh (it will abort). "
        f"The spec is at {spec_path}. "
        f"Tom QA findings (PR: {pr_ref}): {findings_summary}. "
        f"Fix all blocking issues on feature/{item_id}, run `ruff check . && ruff format --check . && pytest`, "
        f"commit, then push with: git push origin feature/{item_id}. "
        f"If you need a decision, post: "
        f'`python3 skills/summon/skill.py ask {item_id} "<question>"` then stop.'
    )


def is_stalled(row: dict, progressed: bool) -> bool:
    """A run is stalled iff there is no progress (PR/branch) and no blocking question."""
    return not progressed and not row.get("question_at")


# ── dev-crew run tracking (SQLite, shared jarvis.db) ──────────────────────────


def _db_path() -> Path:
    return Path(os.environ.get("JARVIS_DATA_DIR", "/data")) / "jarvis.db"


def _db() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dev_crew_runs (
            item         TEXT PRIMARY KEY,
            persona      TEXT NOT NULL,
            worktree     TEXT NOT NULL DEFAULT '',
            summoned_at  TEXT NOT NULL,
            question_at  TEXT,
            pr_pinged_at TEXT
        )
    """)
    conn.commit()
    try:
        conn.execute("ALTER TABLE dev_crew_runs ADD COLUMN pr_pinged_at TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE dev_crew_runs ADD COLUMN last_verdict_reported TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    return conn


def _record_summon(item_id: str, persona_id: str, worktree: str, now: datetime) -> None:
    conn = _db()
    try:
        # A fresh summon for an item resets its blocked state.
        conn.execute(
            """INSERT INTO dev_crew_runs (item, persona, worktree, summoned_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(item) DO UPDATE SET
                   persona=excluded.persona,
                   worktree=excluded.worktree,
                   summoned_at=excluded.summoned_at,
                   question_at=NULL,
                   last_verdict_reported=NULL""",
            (item_id, persona_id, worktree, now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _set_question(item_id: str, now: datetime) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE dev_crew_runs SET question_at = ? WHERE item = ? AND question_at IS NULL",
            (now.isoformat(), item_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_pr_pinged(item_id: str, now: datetime) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE dev_crew_runs SET pr_pinged_at = ? WHERE item = ? AND pr_pinged_at IS NULL",
            (now.isoformat(), item_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── verdict sensor helpers ────────────────────────────────────────────────────

# Throttle: emit at most one gh-fail alert per this window (across all verdict-sweep ticks).
GH_FAIL_ALERT_COOLDOWN_SECONDS = 3600

_GH_FAIL_SENTINEL = Path("/tmp/jarvis-verdict-gh-fail-last")


def _should_emit_gh_fail_alert(sentinel: Path | None = None) -> bool:
    """True if enough time has passed since the last gh-fail alert was emitted."""
    if sentinel is None:
        sentinel = _GH_FAIL_SENTINEL
    try:
        if sentinel.exists():
            last = float(sentinel.read_text().strip())
            if time.time() - last < GH_FAIL_ALERT_COOLDOWN_SECONDS:
                return False
    except (ValueError, OSError):
        pass
    return True


def _record_gh_fail_alert(sentinel: Path | None = None) -> None:
    """Stamp the sentinel file so the next alert is throttled."""
    if sentinel is None:
        sentinel = _GH_FAIL_SENTINEL
    try:
        sentinel.write_text(str(time.time()))
    except OSError:
        pass


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO datetime string (Z or +00:00 suffix) to a UTC-aware datetime."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _comment_identity(comment: dict) -> str:
    """Stable dedupe key for a PR comment: node ID if present, else body hash."""
    node_id = comment.get("id", "").strip()
    if node_id:
        return node_id
    body = comment.get("body", "")
    return "hash:" + hashlib.sha256(body.encode()).hexdigest()[:16]


def _classify_verdict(body: str) -> str:
    """Extract PASS, FAIL, SKIP, or ERROR from a Tom QA PR comment body."""
    if "## Tom QA" not in body:
        return ""
    if "✅ PASS" in body or "— PASS" in body:
        return "PASS"
    if "🚫 FAIL" in body or "— FAIL" in body:
        return "FAIL"
    if "⏭️ SKIP" in body or "— SKIP" in body:
        return "SKIP"
    if "— ERROR" in body:
        return "ERROR"
    return ""


def _extract_blocking_summary(body: str) -> str:
    """Extract bullet lines from the blocking section of a Tom QA FAIL comment."""
    m = re.search(r"### 🚫 Blocking.*?\n(.*?)(?=###|---)", body, re.DOTALL)
    if not m:
        return ""
    lines = [ln.strip() for ln in m.group(1).strip().splitlines() if ln.strip()]
    if len(lines) > 5:
        lines = lines[:5] + [f"… (+{len(lines) - 5} more)"]
    return "\n".join(lines)


def _build_verdict_message(
    verdict: str, item_id: str, persona_name: str, pr_url: str, comment_body: str
) -> str:
    """Build the Discord verdict notification message for a Tom QA outcome."""
    if verdict == "PASS":
        return (
            f"✅ Tom QA **PASS** on `{item_id}` — ready for your merge.\n"
            f"PR: {pr_url} | Builder: {persona_name}"
        )
    if verdict == "SKIP":
        return (
            f"⏭️ Tom QA **SKIP** on `{item_id}` — trivial diff, ready for your merge.\n"
            f"PR: {pr_url} | Builder: {persona_name}"
        )
    if verdict == "FAIL":
        summary = _extract_blocking_summary(comment_body)
        base = (
            f"🚫 Tom QA **FAIL** on `{item_id}` — needs a fix. Authorize a refix?\n"
            f"PR: {pr_url} | Builder: {persona_name}"
        )
        return base + (f"\nBlocking:\n{summary}" if summary else "")
    if verdict == "ERROR":
        return (
            f"⚠️ Tom QA **ERROR** on `{item_id}` — Tom could not run, merge is blocked.\n"
            f"Check the QA workflow logs.\nPR: {pr_url} | Builder: {persona_name}"
        )
    return f"⚠️ Tom QA unknown verdict on `{item_id}` — check PR: {pr_url}"


def _fetch_active_verdict(
    repo_dir: str, item_id: str, summoned_at: str
) -> tuple[str, str, str, str, str, bool]:
    """Return (pr_state, pr_url, verdict, comment_id, comment_body, gh_failed).

    pr_state: 'OPEN', 'MERGED', 'CLOSED', or '' (no PR found)
    verdict:  'PASS', 'FAIL', 'SKIP', or '' (no verdict after summoned_at)
    gh_failed: True when a gh subprocess call failed — distinct from the normal
               empty state when no PR exists yet (which returns gh_failed=False).
    Only considers Tom QA comments posted *after* summoned_at so that a re-summon
    for a refix cycle does not re-report the FAIL that triggered the refix.
    """
    branch = f"feature/{item_id}"
    try:
        pr_result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,url,state",
                "--limit",
                "1",
            ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if pr_result.returncode != 0:
            print(
                f"verdict sensor: gh pr list failed for {item_id} "
                f"(rc={pr_result.returncode}): {pr_result.stderr.strip()[:200]}",
                file=sys.stderr,
            )
            return "", "", "", "", "", True
        try:
            prs = json.loads(pr_result.stdout or "[]")
        except json.JSONDecodeError as exc:
            print(
                f"verdict sensor: gh pr list JSON parse failed for {item_id}: {exc}",
                file=sys.stderr,
            )
            return "", "", "", "", "", True
        if not isinstance(prs, list) or not prs:
            return "", "", "", "", "", False  # normal: no PR yet
        pr = prs[0]
        pr_state = str(pr.get("state", "")).upper()
        pr_url = pr.get("url", "")
        pr_number = str(pr.get("number", ""))
        if not pr_number:
            return "", "", "", "", "", False

        comments_result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "comments"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if comments_result.returncode != 0:
            print(
                f"verdict sensor: gh pr view failed for {item_id} PR#{pr_number} "
                f"(rc={comments_result.returncode}): {comments_result.stderr.strip()[:200]}",
                file=sys.stderr,
            )
            return pr_state, pr_url, "", "", "", True
        try:
            cdata = json.loads(comments_result.stdout or "{}")
        except json.JSONDecodeError as exc:
            print(
                f"verdict sensor: gh pr view JSON parse failed for {item_id}: {exc}",
                file=sys.stderr,
            )
            return pr_state, pr_url, "", "", "", True

        summoned_dt = _parse_iso(summoned_at)
        tom_after_summon = []
        for c in cdata.get("comments", []):
            if "## Tom QA" not in c.get("body", ""):
                continue
            created_dt = _parse_iso(c.get("createdAt", ""))
            if summoned_dt and created_dt and created_dt <= summoned_dt:
                continue  # predates or coincides with this summon — skip
            tom_after_summon.append(c)

        if not tom_after_summon:
            return pr_state, pr_url, "", "", "", False

        latest = tom_after_summon[-1]  # gh returns in chronological order
        comment_id = _comment_identity(latest)
        comment_body = latest.get("body", "")
        verdict = _classify_verdict(comment_body)
        return pr_state, pr_url, verdict, comment_id, comment_body, False
    except FileNotFoundError:
        print(
            "verdict sensor: gh not found on PATH — install gh CLI for verdict tracking",
            file=sys.stderr,
        )
        return "", "", "", "", "", True
    except Exception as exc:  # noqa: BLE001
        print(f"verdict sensor: unexpected error for {item_id}: {exc}", file=sys.stderr)
        return "", "", "", "", "", True


# ── Discord ───────────────────────────────────────────────────────────────────


def _post_discord(message: str, mention: bool = False) -> bool:
    """Post to the MAIN Discord channel via scripts/discord_post.py (stdin). Best-effort.

    discord_post.py routes to DISCORD_WEBHOOK_URL — the main channel — deliberately, so a
    blocked build reaches AJ wherever he is. DISCORD_DEVLOOP_WEBHOOK stays for QA verdicts.
    Pass mention=True for high-signal posts (blocked questions, critical alerts) so Discord
    push-notifies AJ via the @mention.
    """
    if not DISCORD_SCRIPT.exists():
        print(f"discord_post.py not found at {DISCORD_SCRIPT}", file=sys.stderr)
        return False
    try:
        cmd = ["python3", str(DISCORD_SCRIPT)]
        if mention:
            cmd.append("--mention")
        result = subprocess.run(
            cmd,
            input=message,
            text=True,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            print(f"discord_post.py failed: {result.stderr.strip()}", file=sys.stderr)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"discord_post.py error: {exc}", file=sys.stderr)
        return False


# ── progress detection ────────────────────────────────────────────────────────


def _branch_progressed(repo_dir: str, item_id: str) -> bool:
    """True if the builder has opened a PR or pushed its feature branch.

    Tries `gh pr list` first; falls back to a remote-branch check so it still works when
    gh is unauthenticated in the dispatcher environment.
    """
    branch = f"feature/{item_id}"
    try:
        pr = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if pr.returncode == 0:
            data = json.loads(pr.stdout or "[]")
            if isinstance(data, list) and data:
                return True
    except Exception:  # noqa: BLE001
        pass
    try:
        ls = subprocess.run(
            ["git", "-C", repo_dir, "ls-remote", "--heads", "origin", branch],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return ls.returncode == 0 and bool(ls.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


# ── watchdog arming (reuses the schedules skill's once@ one-shot) ─────────────


def _arm_single_watchdog(item_id: str, minutes: int) -> None:
    """Stage + approve a one-shot schedule that checks for stall/milestones N minutes from now.

    Reuses the schedules skill's once@ format: the heartbeat dispatcher fires it once,
    runs `summon watchdog-check <item> <minutes>`, forwards any stdout (alert/ping) to
    Discord, then the one-off retires itself. Best-effort: a scheduling failure never
    blocks the (already-launched) build.
    """
    if not SCHEDULES_SKILL.exists():
        print(f"schedules skill not found — watchdog (+{minutes}m) not armed", file=sys.stderr)
        return
    fire_at = datetime.now(_LOCAL_TZ) + timedelta(minutes=minutes)
    schedule = "once@" + fire_at.strftime("%Y-%m-%dT%H:%M")
    try:
        proposed = subprocess.run(
            [
                "python3",
                str(SCHEDULES_SKILL),
                "propose",
                "--skill",
                "summon",
                "--schedule",
                schedule,
                "--description",
                f"dev-crew stall watchdog for {item_id} (+{minutes}m)",
                "--args",
                json.dumps(["watchdog-check", item_id, str(minutes)]),
                "--created-by",
                "summon",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proposed.returncode != 0:
            print(
                f"watchdog (+{minutes}m) propose failed: {proposed.stderr.strip()}", file=sys.stderr
            )
            return
        job_id = json.loads(proposed.stdout)["id"]
        approved = subprocess.run(
            ["python3", str(SCHEDULES_SKILL), "approve", str(job_id)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if approved.returncode != 0:
            print(
                f"watchdog (+{minutes}m) approve failed: {approved.stderr.strip()}", file=sys.stderr
            )
            return
        print(f"Watchdog armed: check at {schedule} local (in ~{minutes} min).")
    except Exception as exc:  # noqa: BLE001
        print(f"watchdog (+{minutes}m) arming error: {exc}", file=sys.stderr)


def _arm_watchdog(item_id: str) -> None:
    """Arm escalating stall/milestone checks at each interval in WATCHDOG_CHECKS."""
    for minutes in WATCHDOG_CHECKS:
        _arm_single_watchdog(item_id, minutes)


def _arm_verdict_sweep_if_needed() -> None:
    """Arm a recurring 10m verdict-sweep schedule if one isn't already active.

    Best-effort: a scheduling failure never blocks the (already-launched) build.
    Idempotent: if a verdict-sweep schedule already exists and is enabled, no new
    schedule is created so multiple summons don't accumulate duplicate sweeps.
    """
    if not SCHEDULES_SKILL.exists():
        print("schedules skill not found — verdict-sweep not armed", file=sys.stderr)
        return
    try:
        db_path = _db_path()
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute(
                    "SELECT id FROM schedules "
                    "WHERE skill='summon' AND enabled=1 AND args LIKE '%verdict-sweep%'"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            finally:
                conn.close()
            if rows:
                return  # already armed

        proposed = subprocess.run(
            [
                "python3",
                str(SCHEDULES_SKILL),
                "propose",
                "--skill",
                "summon",
                "--schedule",
                "10m",
                "--description",
                "dev-crew verdict sweep — watch for Tom QA verdicts on active builds",
                "--args",
                json.dumps(["verdict-sweep"]),
                "--created-by",
                "summon",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proposed.returncode != 0:
            print(f"verdict-sweep arming failed: {proposed.stderr.strip()}", file=sys.stderr)
            return
        job_id = json.loads(proposed.stdout)["id"]
        approved = subprocess.run(
            ["python3", str(SCHEDULES_SKILL), "approve", str(job_id)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if approved.returncode != 0:
            print(f"verdict-sweep approve failed: {approved.stderr.strip()}", file=sys.stderr)
            return
        print("Verdict-sweep schedule armed (10m recurring).")
    except Exception as exc:  # noqa: BLE001
        print(f"verdict-sweep arming error: {exc}", file=sys.stderr)


# ── commands ──────────────────────────────────────────────────────────────────


def cmd_summon(args: list[str]) -> int:
    revise = "--revise" in args
    args = [a for a in args if a != "--revise"]

    if len(args) < 2:
        print("Usage: summon <persona> <id> [--revise]", file=sys.stderr)
        return 1

    persona_id, item_id = args[0], args[1]

    # Validate item id format (YYYY-NNNN-slug)
    if not re.match(r"^\d{4}-\d{4}-.+", item_id):
        print(
            f"Warning: item id '{item_id}' doesn't match expected format YYYY-NNNN-slug",
            file=sys.stderr,
        )

    check_claude_version()
    persona = get_persona(persona_id)
    concurrency_check()

    sname = session_name(persona_id)
    live = list_crew_sessions()
    if sname in live:
        print(
            f"Session '{sname}' already running. Dismiss it first: summon dismiss {persona_id}",
            file=sys.stderr,
        )
        return 1

    repo_dir = persona["repo_dir"]
    auth_profile = persona["role"]
    permission_mode = persona.get("permission_mode", "auto")
    display_name = f"{persona['name']} — {item_id}"

    # ── isolated build worktree ────────────────────────────────────────────────
    # The builder must not disturb the operator's checkout, so it works in a dedicated
    # worktree. A worktree of the (trusted) repo inherits its trust, so the unattended
    # session is not blocked by the workspace-trust dialog.
    wt = worktree_path(repo_dir, item_id)
    subprocess.run(["git", "-C", repo_dir, "fetch", "origin"], capture_output=True, text=True)
    if wt.exists():
        # Remove a stale worktree from a previous summon of the same item.
        subprocess.run(
            ["git", "-C", repo_dir, "worktree", "remove", "--force", str(wt)],
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "-C", repo_dir, "worktree", "prune"], capture_output=True, text=True)

    if revise:
        # Refix mode: check out the existing feature branch rather than cutting from main.
        branch = f"feature/{item_id}"
        add = subprocess.run(
            ["git", "-C", repo_dir, "worktree", "add", str(wt), branch],
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            # Local tracking branch doesn't exist yet; create it from origin.
            add = subprocess.run(
                [
                    "git",
                    "-C",
                    repo_dir,
                    "worktree",
                    "add",
                    "--track",
                    "-b",
                    branch,
                    str(wt),
                    f"origin/{branch}",
                ],
                capture_output=True,
                text=True,
            )
        if add.returncode != 0:
            print(
                f"Failed to create revise worktree for {branch}: {add.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
    else:
        add = subprocess.run(
            ["git", "-C", repo_dir, "worktree", "add", "--detach", str(wt), "origin/main"],
            capture_output=True,
            text=True,
        )
        if add.returncode != 0:
            print(f"Failed to create build worktree at {wt}: {add.stderr.strip()}", file=sys.stderr)
            return 1

    url_file = Path(f"/tmp/claude-rc-{persona_id}.out")
    url_file.unlink(missing_ok=True)

    # Build the RC launch command via the Phase 1 auth wrapper. The kickoff is delivered
    # as a POSITIONAL prompt — the interactive RC session auto-submits it on startup.
    # No tmux send-keys: every argument is shell-quoted into one sh -c string.
    if revise:
        pr_url, tom_findings = _fetch_pr_info(repo_dir, item_id)
        kickoff = build_revise_kickoff(persona["name"], item_id, pr_url, tom_findings)
    else:
        kickoff = build_kickoff(persona["name"], item_id)

    launch_args = [
        str(LAUNCH_SH),
        auth_profile,
        "--",
        "--remote-control",
        display_name,
        "--permission-mode",
        permission_mode,
        kickoff,
    ]
    rc_cmd = " ".join(shlex.quote(a) for a in launch_args)
    rc_cmd += f" > {shlex.quote(str(url_file))} 2>&1"

    mode_label = "refix" if revise else "build"
    print(f"Launching {persona['name']} on {item_id} ({mode_label}) in {wt} ...")
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", sname, "-c", str(wt), rc_cmd],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Failed to start tmux session: {exc}", file=sys.stderr)
        subprocess.run(
            ["git", "-C", repo_dir, "worktree", "remove", "--force", str(wt)],
            capture_output=True,
        )
        return 1

    _record_summon(item_id, persona_id, str(wt), datetime.now(UTC))
    _arm_watchdog(item_id)
    _arm_verdict_sweep_if_needed()

    url = capture_rc_url(url_file, timeout=15)
    if url:
        print(f"RC session URL: {url}")
    else:
        print(
            "Warning: RC URL not captured within timeout — session is running; "
            "connect via claude.ai/code or the Claude mobile app",
            file=sys.stderr,
        )

    if revise:
        _post_discord(
            f"🔁 Re-summoned **{persona['name']}** on `{item_id}` (refix) — applying Tom's feedback"
        )
    else:
        _post_discord(f"🔨 Summoned **{persona['name']}** on `{item_id}` — building")

    print(f"Kickoff delivered to session '{sname}' (auto-submitted; zero paste).")
    print(f"\nSession '{sname}' live. Link: {url or '(connect via claude.ai/code)'}")
    return 0


def cmd_ask(args: list[str]) -> int:
    """ask <id> <question...>  — builder posts a blocking question to the dev-loop Discord."""
    if len(args) < 2:
        print('Usage: ask <id> "<question>"', file=sys.stderr)
        return 1
    item_id = args[0]
    question = " ".join(args[1:]).strip()
    if not question:
        print("Error: empty question", file=sys.stderr)
        return 1

    conn = _db()
    try:
        row = conn.execute(
            "SELECT persona FROM dev_crew_runs WHERE item = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    persona = row[0] if row else "builder"

    message = f"🟡 **{persona}** is blocked on `{item_id}` and needs a decision:\n> {question}"
    posted = _post_discord(message, mention=True)
    _set_question(item_id, datetime.now(UTC))

    if not posted:
        print("Warning: question recorded but Discord post failed — check webhook", file=sys.stderr)
        return 1
    print(f"Question posted to Discord for {item_id}.")
    return 0


def cmd_watchdog_check(args: list[str]) -> int:
    """watchdog-check <id> [minutes]  — stall check + milestone ping for the dispatcher.

    Prints to stdout only when action is needed (the dispatcher forwards stdout to
    Discord). Armed as escalating once@ schedules at summon time (+5, +15, +30 min).

    milestone ping: fires once when a PR is opened (progressed=True, pr_pinged_at=None).
    stall alert: fires when no progress and no question, with urgency scaled to minutes.
    """
    if not args:
        print("Usage: watchdog-check <id> [minutes]", file=sys.stderr)
        return 1
    item_id = args[0]
    check_minutes = int(args[1]) if len(args) > 1 else 30

    conn = _db()
    try:
        row = conn.execute(
            "SELECT persona, question_at, pr_pinged_at FROM dev_crew_runs WHERE item = ?",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        # No record — nothing to watch (e.g. the persona was dismissed). Silent.
        return 0

    persona_id, question_at, pr_pinged_at = row
    persona = load_roster().get(persona_id, {})
    repo_dir = persona.get("repo_dir", str(PROJECT))
    progressed = _branch_progressed(repo_dir, item_id)

    # ── PR milestone ping (fires once when PR is opened) ──────────────────────
    if progressed and pr_pinged_at is None:
        name = persona.get("name", persona_id)
        print(f"🚀 PR opened for `{item_id}` — QA (Tom) running. Builder: {name}")
        _set_pr_pinged(item_id, datetime.now(UTC))
        return 0

    # ── escalating stall alert ────────────────────────────────────────────────
    # SIGNAL:high prefix tells the dispatcher to @mention AJ on these posts.
    if is_stalled({"question_at": question_at}, progressed):
        name = persona.get("name", persona_id)
        if check_minutes <= 5:
            print(
                f"SIGNAL:high\n"
                f"⏱️ Builder {name} on `{item_id}` — {check_minutes} min, no PR yet "
                f"(may still be setting up or working fast)."
            )
        elif check_minutes <= 15:
            print(
                f"SIGNAL:high\n"
                f"⚠️ Builder {name} quiet for {check_minutes} min on `{item_id}` — "
                f"no PR and no question yet. May need attention."
            )
        else:
            print(
                f"SIGNAL:high\n"
                f"🚨 Builder {name} appears stalled on `{item_id}` — no PR and no posted "
                f"question after {check_minutes} min. Intervene: reconnect via claude.ai/code, "
                f"or `summon dismiss {persona_id}` and re-summon."
            )
    return 0


def cmd_verdict_sweep(_args: list[str]) -> int:
    """verdict-sweep  — check all active builds for new Tom QA verdicts and report them.

    Called by the heartbeat dispatcher on a 10m recurring schedule (armed at summon time).
    For each active dev_crew_runs row:
      - Fetches the PR's Tom QA comments posted after summoned_at
      - Reports new verdicts (PASS/FAIL/SKIP) to Discord via SIGNAL:high stdout prefix
      - Dedupes by comment node ID so each verdict posts exactly once
      - Self-retires (removes the row) when the PR is merged or closed
    Produces no output when there are no active builds or no new verdicts — silent no-op.
    """
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT item, persona, summoned_at, last_verdict_reported FROM dev_crew_runs"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return 0

    gh_failed_any = False
    messages: list[str] = []
    for item_id, persona_id, summoned_at, last_reported in rows:
        persona = load_roster().get(persona_id, {})
        repo_dir = persona.get("repo_dir", str(PROJECT))
        persona_name = persona.get("name", persona_id)

        pr_state, pr_url, verdict, comment_id, comment_body, gh_failed = _fetch_active_verdict(
            repo_dir, item_id, summoned_at or ""
        )

        if gh_failed:
            gh_failed_any = True
            continue  # already logged to stderr in _fetch_active_verdict

        if not pr_state:
            continue  # no PR yet — nothing to watch

        # Report new verdict (deduped by comment identity).
        # Belt-and-suspenders: any new Tom comment with an unrecognized verdict is
        # surfaced as ERROR rather than silently dropped (e.g. future new verdict tokens).
        if comment_id and comment_id != last_reported:
            effective_verdict = verdict or "ERROR"
            msg = _build_verdict_message(
                effective_verdict, item_id, persona_name, pr_url, comment_body
            )
            messages.append(msg)
            upd = _db()
            try:
                upd.execute(
                    "UPDATE dev_crew_runs SET last_verdict_reported = ? WHERE item = ?",
                    (comment_id, item_id),
                )
                upd.commit()
            finally:
                upd.close()

        # Self-retire when PR is in a terminal state
        if pr_state in ("MERGED", "CLOSED"):
            del_conn = _db()
            try:
                del_conn.execute("DELETE FROM dev_crew_runs WHERE item = ?", (item_id,))
                del_conn.commit()
            finally:
                del_conn.close()

    if gh_failed_any and _should_emit_gh_fail_alert():
        _record_gh_fail_alert()
        messages.insert(
            0,
            "⚠️ verdict sensor can't reach GitHub (`gh` failing) — verdicts may be missed",
        )

    if messages:
        print("SIGNAL:high\n" + "\n\n".join(messages))

    return 0


def cmd_dismiss(args: list[str]) -> int:
    """dismiss <persona>  — kill the tmux session and remove any build worktree(s)."""
    if not args:
        print("Usage: dismiss <persona>", file=sys.stderr)
        return 1
    persona_id = args[0]
    sname = session_name(persona_id)
    live = list_crew_sessions()
    killed = False
    if sname in live:
        kill_session(sname)
        killed = True

    # Remove worktrees recorded for this persona, then forget the runs.
    persona = load_roster().get(persona_id, {})
    repo_dir = persona.get("repo_dir", str(PROJECT))
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT worktree FROM dev_crew_runs WHERE persona = ?", (persona_id,)
        ).fetchall()
        for (wt,) in rows:
            if wt:
                subprocess.run(
                    ["git", "-C", repo_dir, "worktree", "remove", "--force", wt],
                    capture_output=True,
                )
        subprocess.run(["git", "-C", repo_dir, "worktree", "prune"], capture_output=True)
        conn.execute("DELETE FROM dev_crew_runs WHERE persona = ?", (persona_id,))
        conn.commit()
    finally:
        conn.close()

    if killed:
        print(f"Dismissed session '{sname}' and cleaned its worktree(s)")
    else:
        print(f"No live session for '{persona_id}' (worktrees, if any, cleaned)")
    return 0


def cmd_reaper(_args: list[str]) -> int:
    live = list_crew_sessions()
    if not live:
        return 0
    reaped = []
    for sname in live:
        idle = session_idle_seconds(sname)
        if idle >= IDLE_MINUTES * 60:
            kill_session(sname)
            reaped.append(f"{sname} (idle {idle // 60}m)")
    if reaped:
        print(f"Reaped: {', '.join(reaped)}")
    return 0


def cmd_status(_args: list[str]) -> int:
    live = list_crew_sessions()
    if not live:
        print("No live crew sessions")
        return 0
    print(f"Live crew sessions ({len(live)}):")
    for sname in live:
        idle = session_idle_seconds(sname)
        persona_id = sname.removeprefix(TMUX_PREFIX)
        print(f"  {sname}  idle={idle // 60}m  persona={persona_id}")
    return 0


# ── entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: skill.py <summon|ask|watchdog-check|dismiss|reaper|status> [args...]",
            file=sys.stderr,
        )
        return 1

    cmd, *rest = sys.argv[1:]
    dispatch = {
        "summon": cmd_summon,
        "ask": cmd_ask,
        "watchdog-check": cmd_watchdog_check,
        "verdict-sweep": cmd_verdict_sweep,
        "dismiss": cmd_dismiss,
        "reaper": cmd_reaper,
        "status": cmd_status,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(
            f"Unknown command '{cmd}'; use summon|ask|watchdog-check|verdict-sweep|dismiss|reaper|status",
            file=sys.stderr,
        )
        return 1
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
