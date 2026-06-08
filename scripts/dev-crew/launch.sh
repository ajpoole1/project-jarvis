#!/usr/bin/env bash
# Launch a dev-crew Claude Code session with the correct auth profile.
#
# Usage: launch.sh <profile> [-- <claude-args...>]
#
# Profiles (defined in config/dev-crew/auth-profiles.json):
#   builder       Claude Code session — Max OAuth (no API key)
#   orchestrator  OpenClaw interactive — Max OAuth (no API key)
#   classifier    Headless classifier — API key injected from key_source
#
# Auth safety model:
#   - Child envs are built from a CLEAN BASE (env -i) — no ambient vars inherited.
#   - subscription profile: key is never present → Claude Code defaults to Max OAuth.
#   - api profile: key read from ~/.config/dev-crew/api.key and injected explicitly.
#   - env -u ANTHROPIC_API_KEY is added as belt-and-suspenders on subscription launches.
#   - A /status assertion runs BEFORE the session opens; mismatch → abort + Discord alert.
#
# OPERATOR NOTE (verify once manually):
#   The orchestrator profile assumes OpenClaw is configured to use OAuth (setup-token),
#   not a daemon-level ANTHROPIC_API_KEY. If OpenClaw only supports a single global key,
#   flag it: a global key in the daemon reintroduces the ambient-API-billing risk.

set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
PROFILES_FILE="$PROJECT/config/dev-crew/auth-profiles.json"
DISCORD_SCRIPT="$PROJECT/scripts/discord_post.py"
LOG="$PROJECT/logs/dev-crew-launch.log"
mkdir -p "$(dirname "$LOG")"

# ── helpers ────────────────────────────────────────────────────────────────────

usage() {
    echo "Usage: $0 <profile> [-- <claude-args...>]" >&2
    echo "Profiles: builder | orchestrator | classifier" >&2
    exit 1
}

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"; }

discord_alert() {
    local msg="$1"
    log "ALERT: $msg"
    if [ -f "$DISCORD_SCRIPT" ] && command -v python3 &>/dev/null; then
        echo "$msg" | python3 "$DISCORD_SCRIPT" 2>>"$LOG" || true
    fi
}

abort() {
    local msg="$1"
    discord_alert "dev-crew launch ABORTED: $msg"
    exit 1
}

# ── parse args ─────────────────────────────────────────────────────────────────

[[ $# -lt 1 ]] && usage
PROFILE="$1"
shift
# Consume optional '--' separator
[[ "${1:-}" == "--" ]] && shift

# ── read auth config ───────────────────────────────────────────────────────────

if ! command -v python3 &>/dev/null; then
    abort "python3 not found; cannot parse auth-profiles.json"
fi

AUTH_TYPE=$(python3 - "$PROFILES_FILE" "$PROFILE" <<'EOF'
import json, sys
path, profile = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
p = data.get("profiles", {}).get(profile)
if not p:
    print("UNKNOWN")
    sys.exit(1)
print(p["auth"])
EOF
) || abort "Profile '$PROFILE' not found in $PROFILES_FILE"

KEY_SOURCE=$(python3 -c "
import json
with open('$PROFILES_FILE') as f:
    print(json.load(f).get('key_source','~/.config/dev-crew/api.key'))
")
KEY_SOURCE="${KEY_SOURCE/#\~/$HOME}"

# ── build child environment ────────────────────────────────────────────────────

# Always start from a CLEAN base: inherit only PATH, HOME, TERM, LANG, USER.
# This guarantees no ambient ANTHROPIC_API_KEY leaks in.
#
# ANTHROPIC_MODEL pins the model for every launched `claude` session. This is the
# authoritative pin for RC-spawned builder sessions, which otherwise inherit the
# account default (Opus): the env var outranks both project .claude/settings.json
# and the account default, so it holds regardless of whether settings.json
# propagates into an RC --spawn worktree session. The settings.json "model" key is
# kept as belt-and-suspenders. Note: this default also applies to the `claude` CLI
# for the api/classifier profile — skill classifiers that hit the API SDK with an
# explicit model= argument are unaffected (the env var only sets the CLI default).
CLEAN_ENV=(
    "PATH=$PATH"
    "HOME=$HOME"
    "TERM=${TERM:-xterm}"
    "LANG=${LANG:-en_US.UTF-8}"
    "USER=${USER:-$(whoami)}"
    "ANTHROPIC_MODEL=claude-sonnet-4-6"
)

case "$AUTH_TYPE" in
    subscription)
        log "Profile='$PROFILE' auth=subscription — Max OAuth; API key absent by construction"
        LAUNCH_ENV=("${CLEAN_ENV[@]}")
        ;;
    api)
        log "Profile='$PROFILE' auth=api — reading key from $KEY_SOURCE"
        if [[ ! -f "$KEY_SOURCE" ]]; then
            abort "API key file not found at $KEY_SOURCE — provision it (chmod 600)"
        fi
        API_KEY=$(cat "$KEY_SOURCE")
        if [[ -z "$API_KEY" ]]; then
            abort "API key file $KEY_SOURCE is empty"
        fi
        LAUNCH_ENV=("${CLEAN_ENV[@]}" "ANTHROPIC_API_KEY=$API_KEY")
        ;;
    *)
        abort "Unknown auth type '$AUTH_TYPE' for profile '$PROFILE'"
        ;;
esac

# ── pre-launch /status assertion ───────────────────────────────────────────────

log "Running pre-launch auth assertion for profile='$PROFILE'"

STATUS_OUT=$(env -i "${LAUNCH_ENV[@]}" claude auth status 2>&1) || true

if [[ -z "$STATUS_OUT" ]]; then
    abort "auth status returned empty output — cannot verify auth. Confirm 'claude auth status' works for this profile before using launch.sh."
fi

case "$AUTH_TYPE" in
    subscription)
        # Expect first-party OAuth (Pro/Max subscription), not an API key
        if ! echo "$STATUS_OUT" | grep -q '"apiProvider": *"firstParty"'; then
            abort "auth status does not show first-party OAuth for '$PROFILE'. Output: $STATUS_OUT"
        fi
        log "Auth assertion passed: first-party OAuth confirmed"
        ;;
    api)
        # For API profile we just need claude to be reachable; key presence is the signal
        log "Auth assertion: API profile — key injected, no Max-check required"
        ;;
esac

# ── launch ─────────────────────────────────────────────────────────────────────

log "Launching Claude Code — profile='$PROFILE' auth='$AUTH_TYPE'"

if [[ "$AUTH_TYPE" == "subscription" ]]; then
    # Belt-and-suspenders: explicit unset even though clean env has no key
    exec env -i "${LAUNCH_ENV[@]}" env -u ANTHROPIC_API_KEY claude "$@"
else
    exec env -i "${LAUNCH_ENV[@]}" claude "$@"
fi
