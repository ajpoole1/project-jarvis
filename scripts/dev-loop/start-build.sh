#!/usr/bin/env bash
# Start a build session for an authorized dev-notes item.
#
# Usage: start-build.sh <id>
#
# What it does:
#   1. git fetch
#   2. Reads spec from dev-queue:knowledge/dev-notes/queue/<id>.md
#   3. Asserts status == authorized (refuses otherwise)
#   4. Refuses if feature/<id> already exists (idempotency guard — no double-builds)
#   5. Creates feature/<id> from main
#   6. Copies spec into knowledge/dev-notes/queue/<id>.md on the branch
#   7. Sets status to "building" and commits
#
# Acceptance: yields a clean feature/<id> branch carrying the spec at building state.
#
# See: docs/dev-loop/BUILD_RUNBOOK.md, docs/dev-loop/DEV_LOOP_REFERENCE.md §8.1

set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
DEVNOTES_DIR="knowledge/dev-notes/queue"
DEV_QUEUE_BRANCH="dev-queue"
MAIN_BRANCH="main"

# ── args ───────────────────────────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <id>" >&2
    echo "  id: item id matching filename in dev-queue:$DEVNOTES_DIR/<id>.md" >&2
    exit 1
fi

ITEM_ID="$1"
SPEC_PATH="$DEVNOTES_DIR/$ITEM_ID.md"
FEATURE_BRANCH="feature/$ITEM_ID"

cd "$PROJECT"

# ── fetch ──────────────────────────────────────────────────────────────────────

echo "Fetching origin..."
git fetch origin

# ── read spec from dev-queue ───────────────────────────────────────────────────

echo "Reading spec from $DEV_QUEUE_BRANCH:$SPEC_PATH ..."
if ! SPEC_CONTENT=$(git show "origin/$DEV_QUEUE_BRANCH:$SPEC_PATH" 2>/dev/null); then
    echo "ERROR: spec not found at origin/$DEV_QUEUE_BRANCH:$SPEC_PATH" >&2
    echo "Has the item been pushed via 'devqueue push'?" >&2
    exit 1
fi

# ── assert status == authorized ────────────────────────────────────────────────

STATUS=$(echo "$SPEC_CONTENT" | grep -m1 '^status:' | sed 's/status:[[:space:]]*//' | tr -d '"' | tr -d "'" | tr -d '[:space:]')

if [[ "$STATUS" != "authorized" ]]; then
    echo "ERROR: item '$ITEM_ID' has status '$STATUS'; must be 'authorized' to start a build." >&2
    echo "Operator must authorize the spec before summoning a builder." >&2
    exit 1
fi

echo "Status check: authorized ✓"

# ── idempotency guard ──────────────────────────────────────────────────────────

if git show-ref --verify --quiet "refs/heads/$FEATURE_BRANCH" 2>/dev/null || \
   git show-ref --verify --quiet "refs/remotes/origin/$FEATURE_BRANCH" 2>/dev/null; then
    echo "ERROR: branch '$FEATURE_BRANCH' already exists — refusing to double-build." >&2
    echo "If this was unintentional, delete the branch first (ask the operator)." >&2
    exit 1
fi

echo "Idempotency check: branch does not exist ✓"

# ── create feature branch from main ───────────────────────────────────────────

echo "Creating $FEATURE_BRANCH from origin/$MAIN_BRANCH ..."
git checkout -b "$FEATURE_BRANCH" "origin/$MAIN_BRANCH"

# ── copy spec into branch ──────────────────────────────────────────────────────

mkdir -p "$DEVNOTES_DIR"
echo "$SPEC_CONTENT" > "$SPEC_PATH"

# ── set status to building ─────────────────────────────────────────────────────

# Sed-replace the status field in the frontmatter block only
python3 - "$SPEC_PATH" <<'PYEOF'
import re, sys
path = sys.argv[1]
text = open(path).read()

def replace_status(m):
    fm = re.sub(r'^(status:\s*).*$', r'\g<1>building', m.group(0), flags=re.MULTILINE)
    return fm

updated = re.sub(r'^---\n.*?^---\n', replace_status, text, count=1, flags=re.DOTALL | re.MULTILINE)
open(path, 'w').write(updated)
PYEOF

# ── commit ─────────────────────────────────────────────────────────────────────

git add "$SPEC_PATH"
git commit -m "build: claim $ITEM_ID (building)"

echo ""
echo "Ready. You are on branch $FEATURE_BRANCH."
echo "Spec is at $SPEC_PATH (status: building)."
echo "Build the feature, then run: scripts/dev-loop/open-pr.sh $ITEM_ID"
