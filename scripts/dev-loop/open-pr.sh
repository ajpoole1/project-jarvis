#!/usr/bin/env bash
# Open a PR for a completed build.
#
# Usage: open-pr.sh <id>
#
# What it does:
#   1. Asserts current branch is feature/<id>
#   2. Reads spec from knowledge/dev-notes/queue/<id>.md
#   3. Asserts current status is "building"
#   4. Sets status to "built" and commits
#   5. Pushes feature/<id> to origin
#   6. Opens PR targeting main via gh pr create (item id in body)
#
# Never merges. Never pushes main. See BUILD_RUNBOOK.md.

set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
DEVNOTES_DIR="knowledge/dev-notes/queue"
MAIN_BRANCH="main"

# ── args ───────────────────────────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <id>" >&2
    exit 1
fi

ITEM_ID="$1"
SPEC_PATH="$DEVNOTES_DIR/$ITEM_ID.md"
EXPECTED_BRANCH="feature/$ITEM_ID"

cd "$PROJECT"

# ── assert correct branch ──────────────────────────────────────────────────────

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]]; then
    echo "ERROR: expected branch '$EXPECTED_BRANCH', currently on '$CURRENT_BRANCH'." >&2
    echo "Switch to the correct feature branch before opening a PR." >&2
    exit 1
fi

echo "Branch check: on $EXPECTED_BRANCH ✓"

# ── read spec ──────────────────────────────────────────────────────────────────

if [[ ! -f "$SPEC_PATH" ]]; then
    echo "ERROR: spec not found at $SPEC_PATH" >&2
    exit 1
fi

SPEC_CONTENT=$(cat "$SPEC_PATH")
STATUS=$(echo "$SPEC_CONTENT" | grep -m1 '^status:' | sed 's/status:[[:space:]]*//' | tr -d '"' | tr -d "'" | tr -d '[:space:]')
TITLE=$(echo "$SPEC_CONTENT" | grep -m1 '^title:' | sed 's/title:[[:space:]]*//' | sed "s/^['\"]//;s/['\"]$//")

if [[ "$STATUS" != "building" ]]; then
    echo "ERROR: spec status is '$STATUS'; expected 'building'." >&2
    echo "Only items in building state can be promoted to built via this script." >&2
    exit 1
fi

echo "Status check: building ✓"

# ── set status to built ────────────────────────────────────────────────────────

python3 - "$SPEC_PATH" <<'PYEOF'
import re, sys
path = sys.argv[1]
text = open(path).read()

def replace_status(m):
    fm = re.sub(r'^(status:\s*).*$', r'\g<1>built', m.group(0), flags=re.MULTILINE)
    return fm

updated = re.sub(r'^---\n.*?^---\n', replace_status, text, count=1, flags=re.DOTALL | re.MULTILINE)
open(path, 'w').write(updated)
PYEOF

git add "$SPEC_PATH"
git commit -m "build: mark $ITEM_ID built"

# ── push feature branch ────────────────────────────────────────────────────────

echo "Pushing $EXPECTED_BRANCH to origin..."
git push -u origin "$EXPECTED_BRANCH"

# ── open PR ────────────────────────────────────────────────────────────────────

if ! command -v gh &>/dev/null; then
    echo "WARNING: gh CLI not found — PR not created automatically." >&2
    echo "Create manually: gh pr create --base $MAIN_BRANCH --title '$TITLE' --body 'item-id: $ITEM_ID'" >&2
    exit 0
fi

gh pr create \
    --base "$MAIN_BRANCH" \
    --title "$TITLE" \
    --body "$(cat <<EOF
## Dev-loop build

item-id: \`$ITEM_ID\`

Spec: \`$SPEC_PATH\`

---
Built by the Jarvis dev-loop builder. QA (Tom) runs on this PR automatically.

<!-- Do not merge without Tom passing and operator approval. -->
EOF
)"

echo ""
echo "PR opened. Status: built. Waiting for QA (Tom) and operator review."
