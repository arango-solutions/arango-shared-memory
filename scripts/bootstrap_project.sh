#!/usr/bin/env bash
#
# bootstrap_project.sh — scaffold a target project with the workflow-automation
# artifacts (CLAUDE.md, .claude/ hooks + skills, .cursor rules + hooks) from
# the templates in this repo, filling in the per-project placeholder values.
#
# Usage:
#   scripts/bootstrap_project.sh --target <dir> \
#       --project-name "My API" \
#       --project-id my-api \
#       --project-type web-api \
#       --prd-file docs/PRD.md \
#       [--tech-stack "TypeScript, Node.js, PostgreSQL"] \
#       [--force]
#
# Re-running is safe: existing files are skipped unless --force is given.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATES_DIR="$REPO_ROOT/templates"

TARGET=""
PROJECT_NAME=""
PROJECT_ID=""
PROJECT_TYPE=""
PRD_FILE=""
TECH_STACK=""
FORCE=0

die() { echo "error: $*" >&2; exit 1; }

usage() {
  sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --target)       TARGET="${2:-}"; shift 2;;
    --project-name) PROJECT_NAME="${2:-}"; shift 2;;
    --project-id)   PROJECT_ID="${2:-}"; shift 2;;
    --project-type) PROJECT_TYPE="${2:-}"; shift 2;;
    --prd-file)     PRD_FILE="${2:-}"; shift 2;;
    --tech-stack)   TECH_STACK="${2:-}"; shift 2;;
    --force)        FORCE=1; shift;;
    -h|--help)      usage 0;;
    *) die "unknown argument: $1 (use --help)";;
  esac
done

[ -n "$TARGET" ]       || die "--target is required"
[ -n "$PROJECT_NAME" ] || die "--project-name is required"
[ -n "$PROJECT_ID" ]   || die "--project-id is required"
[ -n "$PROJECT_TYPE" ] || die "--project-type is required"
[ -n "$PRD_FILE" ]     || die "--prd-file is required"
[ -d "$TEMPLATES_DIR" ] || die "templates directory not found at $TEMPLATES_DIR"
mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd)"

# Render placeholders in a copied file (in place).
render() {
  local file="$1"
  # Use python for safe, delimiter-agnostic substitution.
  PROJECT_NAME="$PROJECT_NAME" PROJECT_ID="$PROJECT_ID" \
  PROJECT_TYPE="$PROJECT_TYPE" PRD_FILE="$PRD_FILE" TECH_STACK="$TECH_STACK" \
  python3 - "$file" <<'PY'
import os, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    text = fh.read()
repl = {
    "<PROJECT_NAME>": os.environ["PROJECT_NAME"],
    "<unique-kebab-case-id>": os.environ["PROJECT_ID"],
    "<web-api|frontend-react|cli-tool|microservice|mobile|full-stack>": os.environ["PROJECT_TYPE"],
    "<relative path to your PRD, e.g. docs/PRD.md>": os.environ["PRD_FILE"],
    "<PRD_FILE>": os.environ["PRD_FILE"],
    '<e.g. "TypeScript, Node.js, PostgreSQL">': os.environ.get("TECH_STACK", "") or "TBD",
}
for needle, value in repl.items():
    text = text.replace(needle, value)
with open(path, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
}

# One timestamp per run, so all backups from a single --force re-run group together.
RUN_STAMP="$(date +%Y%m%d_%H%M%S)"

# Copy one file from templates to target, optionally rendering placeholders.
# On --force over an existing file, keep a timestamped backup first: .claude/ and
# CLAUDE.md are gitignored in target projects, so the backup is the only undo for
# local customizations (e.g. permissions.allow entries in settings.json).
place() {
  local rel="$1" do_render="$2"
  local src="$TEMPLATES_DIR/$rel"
  local dst="$TARGET/$rel"
  [ -f "$src" ] || die "missing template: $src"
  if [ -e "$dst" ] && [ "$FORCE" -ne 1 ]; then
    echo "  skip (exists): $rel"
    return
  fi
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ]; then
    # Rendered files never byte-match their template, so the unchanged shortcut
    # applies only to files copied verbatim.
    if [ "$do_render" -ne 1 ] && cmp -s "$src" "$dst"; then
      echo "  unchanged: $rel"
      return
    fi
    cp "$dst" "$dst.pre-update.$RUN_STAMP"
    echo "  backup: $rel -> $rel.pre-update.$RUN_STAMP"
  fi
  cp "$src" "$dst"
  if [ "$do_render" -eq 1 ]; then render "$dst"; fi
  echo "  wrote: $rel"
}

echo "Bootstrapping workflow automation into: $TARGET"
place "CLAUDE.md" 1
place ".claude/settings.json" 0
place ".claude/hooks/session_recall.py" 0
place ".claude/hooks/drift_queue.py" 0
place ".claude/hooks/drift_stop_gate.sh" 0
# The stop gate runs this before counting the queue, and hides any failure with
# `|| true`, so a project that got the gate without it would silently miss shell edits.
place ".claude/hooks/reconcile_drift_queue.py" 0
place ".claude/hooks/capture_candidates.py" 0
place ".claude/hooks/pattern_apply_tracker.py" 0
# Ships alongside the stop gate on purpose: the gate's block message names this
# script, so a project that got the gate without it would be told to run something
# that is not there.
place ".claude/hooks/dismiss_surfaced.py" 0
place ".claude/skills/prd-sync/SKILL.md" 0
place ".claude/skills/prd-sync/check_evidence.py" 0
place ".claude/skills/pattern-save/SKILL.md" 0
place ".claude/skills/pattern-search/SKILL.md" 0
place ".claude/skills/arangodb-visualizer-customizer/SKILL.md" 0
place ".claude/skills/arangodb-visualizer-customizer/examples.md" 0
place ".cursor/rules/workflow.mdc" 1
place ".cursor/hooks.json" 0
place ".cursor/hooks/shared_memory_session_start.py" 0
place ".cursor/hooks/shared_memory_drift_queue.py" 0
place ".cursor/hooks/shared_memory_apply_tracker.py" 0
place ".cursor/hooks/shared_memory_stop_gate.py" 0

# Ensure the personal-infra entries are git-ignored in the target project.
# The two *.pre-update.* globs cover --force backups of files living OUTSIDE the
# already-ignored .claude/ tree (project-root CLAUDE.md, committed .cursor rules).
GI="$TARGET/.gitignore"
for entry in ".prd-drift-queue/" ".pattern-capture-queue/" ".claude/" "CLAUDE.md" ".no-drift-gate" \
             ".cursor/.shared-memory-sessions/" "CLAUDE.md.pre-update.*" \
             ".cursor/rules/workflow.mdc.pre-update.*" ".cursor/hooks.json.pre-update.*" \
             ".cursor/hooks/*.pre-update.*"; do
  if [ ! -f "$GI" ] || ! grep -qxF "$entry" "$GI"; then
    echo "$entry" >> "$GI"
    echo "  gitignore += $entry"
  fi
done

cat <<EOF

Done. Next steps for this project:
  1. Edit CLAUDE.md and fill in TECH_STACK / any remaining <PLACEHOLDER> values.
  2. Ensure the arangodb-memory-mcp MCP server is registered (see setup.md STEP 3).
  3. Run /prd-sync to establish the initial drift baseline.
EOF
