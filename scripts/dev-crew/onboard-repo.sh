#!/usr/bin/env bash
# Onboard a repository into the Jarvis dev-loop framework.
#
# Usage: onboard-repo.sh <repo-dir> <persona-id>
#
# What it stamps (idempotently):
#   1. .claude/settings.json — builder permission set (§8.4); skips if already present
#   2. docs/dev-loop/BUILD_RUNBOOK.md — copied from this repo
#   3. .github/workflows/qa-caller.yml — Tom QA caller; skips if already present
#   4. dev-queue branch — creates it (from main) if absent; pushes
#   5. knowledge/dev-crew/roster.md — adds persona entry stub if absent
#   6. docs/dev-crew/persona-<persona>.md — persona CLAUDE.md skeleton (5 blocks)
#
# After running:
#   - Hand-author the VOICE block in docs/dev-crew/persona-<persona>.md
#   - Add the repo to the persona entry in knowledge/dev-crew/roster.md
#   - Provision QA_MODEL_API_KEY + DISCORD_DEVLOOP_WEBHOOK as Actions secrets
#   - Enable branch protection: require 'QA Gate / Tom QA' on main
#
# See: docs/dev-loop/DEV_LOOP_REFERENCE.md §8.4, §8.6

set -euo pipefail

THIS_REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE_SETTINGS="$THIS_REPO/config/dev-crew/settings-builder-template.json"
RUNBOOK_SRC="$THIS_REPO/docs/dev-loop/BUILD_RUNBOOK.md"
QA_CALLER_SRC="$THIS_REPO/.github/workflows/qa-caller.yml"

# ── args ───────────────────────────────────────────────────────────────────────

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <repo-dir> <persona-id>" >&2
    exit 1
fi

REPO_DIR="$(realpath "$1")"
PERSONA_ID="$2"

if [[ ! -d "$REPO_DIR/.git" ]]; then
    echo "ERROR: $REPO_DIR is not a git repository" >&2
    exit 1
fi

echo "Onboarding $REPO_DIR as persona '$PERSONA_ID' ..."

# ── 1. .claude/settings.json ──────────────────────────────────────────────────

SETTINGS_FILE="$REPO_DIR/.claude/settings.json"
mkdir -p "$REPO_DIR/.claude"
if [[ -f "$SETTINGS_FILE" ]]; then
    echo "  [skip] .claude/settings.json already exists — not overwriting"
else
    cp "$TEMPLATE_SETTINGS" "$SETTINGS_FILE"
    echo "  [done] .claude/settings.json — builder permission set"
fi

# ── 2. BUILD_RUNBOOK.md ───────────────────────────────────────────────────────

RUNBOOK_DEST="$REPO_DIR/docs/dev-loop/BUILD_RUNBOOK.md"
mkdir -p "$REPO_DIR/docs/dev-loop"
if [[ -f "$RUNBOOK_DEST" ]]; then
    echo "  [skip] docs/dev-loop/BUILD_RUNBOOK.md already present"
else
    cp "$RUNBOOK_SRC" "$RUNBOOK_DEST"
    echo "  [done] docs/dev-loop/BUILD_RUNBOOK.md"
fi

# ── 3. qa-caller.yml ──────────────────────────────────────────────────────────

QA_DEST="$REPO_DIR/.github/workflows/qa-caller.yml"
mkdir -p "$REPO_DIR/.github/workflows"
if [[ -f "$QA_DEST" ]]; then
    echo "  [skip] .github/workflows/qa-caller.yml already present"
else
    cp "$QA_CALLER_SRC" "$QA_DEST"
    echo "  [done] .github/workflows/qa-caller.yml"
fi

# ── 4. dev-queue branch ───────────────────────────────────────────────────────

cd "$REPO_DIR"
ORIG_BRANCH=$(git rev-parse --abbrev-ref HEAD)

if git show-ref --verify --quiet refs/heads/dev-queue || \
   git show-ref --verify --quiet refs/remotes/origin/dev-queue; then
    echo "  [skip] dev-queue branch already exists"
else
    git checkout -b dev-queue main
    mkdir -p knowledge/dev-notes/queue knowledge/dev-notes/backlog knowledge/dev-notes/archive
    touch knowledge/dev-notes/queue/.gitkeep
    touch knowledge/dev-notes/backlog/.gitkeep
    touch knowledge/dev-notes/archive/.gitkeep
    git add knowledge/dev-notes/
    git commit -m "init: dev-notes queue dirs (queue/backlog/archive)"
    git push -u origin dev-queue
    git checkout "$ORIG_BRANCH"
    echo "  [done] dev-queue branch created and pushed"
fi

# ── 5. roster.md entry ────────────────────────────────────────────────────────

ROSTER="$THIS_REPO/knowledge/dev-crew/roster.md"
if grep -q "^id: $PERSONA_ID" "$ROSTER" 2>/dev/null; then
    echo "  [skip] roster entry for '$PERSONA_ID' already present"
else
    REPO_NAME=$(basename "$REPO_DIR")
    cat >> "$ROSTER" <<EOF

## ${PERSONA_ID^}

\`\`\`yaml
id: $PERSONA_ID
name: # TODO: author name
role: builder
repos:
  - $REPO_NAME
repo_dir: $REPO_DIR
voice: # set by operator
model: claude-sonnet-4-6
auth: subscription
permission_mode: auto
rc_spawn: worktree
\`\`\`
EOF
    echo "  [done] roster entry stub for '$PERSONA_ID' added — fill in name and voice"
fi

# ── 6. persona CLAUDE.md skeleton ────────────────────────────────────────────

PERSONA_FILE="$THIS_REPO/docs/dev-crew/persona-${PERSONA_ID}.md"
if [[ -f "$PERSONA_FILE" ]]; then
    echo "  [skip] docs/dev-crew/persona-${PERSONA_ID}.md already present"
else
    REPO_NAME=$(basename "$REPO_DIR")
    cat > "$PERSONA_FILE" <<EOF
# Persona: ${PERSONA_ID^} — builder for ${REPO_NAME}

This file is the five-block persona CLAUDE.md (DEV_LOOP_REFERENCE §8.6).
Load it at the start of every builder session. Seeded by \`summon\` in the kickoff prompt.

---

## 1. Identity

I am **${PERSONA_ID^}**, a builder agent for the \`${REPO_NAME}\` repository.
My role is to implement authorized dev-loop items end-to-end: read the spec, build on a feature branch, run tests and lint, open a PR, and stop.
I never push to \`main\`, never merge, and never take actions outside my build mandate.

---

## 2. Voice

<!-- TODO: OPERATOR — author this block. One-line descriptor + 3-4 do/don't examples.
     Voice colors tone in reporting; it does not override the build contract below.
     Example format:
       Terse and precise. Reports in short declarative sentences.
       DO: "Built. 3 tests passing. PR open."
       DON'T: "I've gone ahead and implemented the feature, which I believe..."
-->
**[VOICE NOT YET AUTHORED — operator must fill this in before first summon]**

---

## 3. Project context

The repo I build is **\`${REPO_NAME}\`**. To understand its conventions and architecture, read:
- \`CLAUDE.md\` (root) — conventions, skill patterns, security rules, write boundary
- \`docs/dev-loop/DEV_LOOP_REFERENCE.md\` — the loop invariants and shared contracts I operate under
- \`docs/dev-loop/BUILD_RUNBOOK.md\` — the exact procedure I follow each build
- The codebase itself — treat existing patterns as the canonical style guide

Do **not** assume any external master plan or design doc exists. Everything I need is in this repo.

---

## 4. Build contract

1. **Only act on \`authorized\` items.** Run \`scripts/dev-loop/start-build.sh <id>\` — it enforces this.
2. **Follow BUILD_RUNBOOK.md exactly.** No shortcuts.
3. **Never push \`main\`.** Never merge. Never \`git push --force\`.
4. **Stop and ask on ambiguity.** If the spec is under-specified for a decision, stop. Do not guess.
5. **Run tests + lint before every commit**: \`ruff check . && ruff format --check . && pytest\`.
6. **When done, run \`open-pr.sh <id>\`** — it sets \`built\`, pushes, and opens the PR.

---

## 5. Delegation map

*(Empty — task specialists are out of scope. See DEV_LOOP_REFERENCE §9.)*
EOF
    echo "  [done] docs/dev-crew/persona-${PERSONA_ID}.md skeleton created"
fi

# ── summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Onboarding complete for '$PERSONA_ID' on $REPO_DIR."
echo ""
echo "NEXT — operator actions required:"
echo "  1. Author the VOICE block in docs/dev-crew/persona-${PERSONA_ID}.md"
echo "  2. Fill in 'name' in knowledge/dev-crew/roster.md for '$PERSONA_ID'"
echo "  3. Add QA_MODEL_API_KEY + DISCORD_DEVLOOP_WEBHOOK to repo Actions secrets"
echo "  4. Enable branch protection: require 'QA Gate / Tom QA' on main"
