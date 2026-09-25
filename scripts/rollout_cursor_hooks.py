#!/usr/bin/env python3
"""Install/refresh shared-memory hook enforcement in bootstrapped local projects.

Projects are discovered by any canonical bootstrap marker, so unrelated repositories
under the scan root are untouched. Existing Cursor hooks are
merged by event/command; unrelated hook entries are preserved. The canonical
Claude digest/stop/apply hooks are refreshed at the same time.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "templates" / ".cursor"
CLAUDE_TEMPLATE_ROOT = REPO_ROOT / "templates" / ".claude"
CURSOR_HOOK_FILES = (
    "shared_memory_session_start.py",
    "shared_memory_drift_queue.py",
    "shared_memory_apply_tracker.py",
    "shared_memory_stop_gate.py",
)
# dismiss_surfaced.py and reconcile_drift_queue.py must roll out with drift_stop_gate.sh:
# the gate's block message names the first, and the gate runs the second with `|| true`,
# so shipping the gate alone either points projects at a missing script or silently
# skips shell edits. drift_queue.py rolls out too, because the reconciler imports from it.
CLAUDE_HOOK_FILES = (
    "session_recall.py",
    "drift_queue.py",
    "drift_stop_gate.sh",
    "pattern_apply_tracker.py",
    "dismiss_surfaced.py",
    "reconcile_drift_queue.py",
    "capture_candidates.py",
)
# Skills were never synced by this tool, only placed once by bootstrap_project.sh (which
# skips existing files). The result was silent permanent drift: as of 2026-08-25, 29 of 32
# deployed copies of prd-sync/SKILL.md still had the Phase-5 drift-queue loop bug and the
# pre-AGENTS.md identity path, and one project was 140 lines behind. Unlike hooks these are
# nested paths, so they carry their subdirectory.
CLAUDE_SKILL_FILES = (
    "prd-sync/SKILL.md",
    "prd-sync/check_evidence.py",
    "pattern-save/SKILL.md",
    "pattern-search/SKILL.md",
    "arangodb-visualizer-customizer/SKILL.md",
    "arangodb-visualizer-customizer/examples.md",
)
# Projects this tool must never touch, and why. A rollout here is not a refresh — for a
# repo that TRACKS .claude/ in a public, PR-governed repository it is an unreviewed change
# to published content, which is why contextual-data-fabric was fully restored on
# 2026-08-26 (see docs/scorecard.md, C2 decision). Without this list the very next --apply
# silently re-does what that restore undid.
#
# A project can also opt out on its own by creating a `.no-shared-memory-rollout` file in
# its root — same spirit as the `.no-drift-gate` bypass. Prefer the marker for repos whose
# owners decide for themselves; use this list when the exclusion is ours to remember.
EXCLUDED_PROJECTS = (
    "contextual-data-fabric",   # tracks .claude/ in a public repo; enforcement belongs in a reviewed PR there
)
OPT_OUT_MARKER = ".no-shared-memory-rollout"
IGNORE_LINES = (
    ".cursor/.shared-memory-sessions/",
    ".cursor/hooks.json.pre-update.*",
    ".cursor/hooks/*.pre-update.*",
)
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist"}


# Any ONE of these means "this repo was bootstrapped by us" and is safe to refresh.
# Keying solely on the Claude SessionStart hook silently excluded the projects most
# likely to be stale: a repo bootstrapped before the hook existed, or one that got
# skills but never got hooks, was invisible and therefore unreachable by every
# rollout ever run. contextual-data-fabric sat that way with 8 skills, zero hooks, a
# prd-sync skill 140 lines behind, and no check_evidence.py at all.
#
# Every marker is specific to this system — no unrelated repository has a
# skills/prd-sync/SKILL.md — so widening discovery does not widen blast radius.
BOOTSTRAP_MARKERS = (
    Path(".claude") / "hooks" / "session_recall.py",
    Path(".claude") / "skills" / "prd-sync" / "SKILL.md",
    Path(".claude") / "skills" / "pattern-search" / "SKILL.md",
    Path(".cursor") / "hooks" / "shared_memory_session_start.py",
)


def is_bootstrapped(here: Path) -> bool:
    """True when any canonical marker is present (see BOOTSTRAP_MARKERS)."""
    return any((here / marker).is_file() for marker in BOOTSTRAP_MARKERS)


def discover(root: Path) -> list[Path]:
    projects: list[Path] = []
    for current, dirs, _ in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        here = Path(current)
        if here == REPO_ROOT / "templates":
            dirs[:] = []
            continue
        if is_bootstrapped(here):
            if here.name in EXCLUDED_PROJECTS or (here / OPT_OUT_MARKER).is_file():
                dirs[:] = []          # recognised, deliberately skipped
                continue
            projects.append(here)
            dirs[:] = []
    return sorted(projects)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def merged_hooks(existing: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged["version"] = 1
    hooks = dict(merged.get("hooks") or {})
    for event, wanted_entries in template["hooks"].items():
        entries = list(hooks.get(event) or [])
        by_command = {
            entry.get("command"): i
            for i, entry in enumerate(entries)
            if isinstance(entry, dict) and entry.get("command")
        }
        for wanted in wanted_entries:
            command = wanted["command"]
            if command in by_command:
                entries[by_command[command]] = wanted
            else:
                entries.append(wanted)
        hooks[event] = entries
    merged["hooks"] = hooks
    return merged


def merged_claude_settings(existing: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """Add/refresh only the apply-tracker entry; preserve all other user settings."""
    merged = dict(existing)
    hooks = dict(merged.get("hooks") or {})
    wanted = next(
        entry
        for entry in template["hooks"]["PostToolUse"]
        if "pattern-search" in entry.get("matcher", "")
    )
    entries = list(hooks.get("PostToolUse") or [])
    index = next(
        (i for i, entry in enumerate(entries)
         if isinstance(entry, dict) and "pattern-search" in entry.get("matcher", "")),
        None,
    )
    if index is None:
        entries.append(wanted)
    else:
        entries[index] = wanted
    hooks["PostToolUse"] = entries
    merged["hooks"] = hooks
    return merged


def _backup(path: Path, stamp: str) -> None:
    shutil.copy2(path, path.with_name(f"{path.name}.pre-update.{stamp}"))


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _append_gitignore(project: Path) -> bool:
    path = project / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    present = set(existing.splitlines())
    missing = [line for line in IGNORE_LINES if line not in present]
    if not missing:
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(prefix + "\n".join(missing) + "\n")
    return True


def install(project: Path, *, apply: bool, stamp: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    cursor_dir = project / ".cursor"
    hooks_dir = cursor_dir / "hooks"
    hooks_json = cursor_dir / "hooks.json"
    template_json = _load_json(TEMPLATE_ROOT / "hooks.json")
    claude_dir = project / ".claude"
    claude_hooks_dir = claude_dir / "hooks"
    claude_skills_dir = claude_dir / "skills"
    claude_settings = claude_dir / "settings.json"
    claude_template_settings = _load_json(CLAUDE_TEMPLATE_ROOT / "settings.json")

    try:
        existing = _load_json(hooks_json) if hooks_json.exists() else {}
        existing_claude = _load_json(claude_settings) if claude_settings.exists() else {}
    except (json.JSONDecodeError, ValueError) as exc:
        return "error", [str(exc)]
    merged = merged_hooks(existing, template_json)
    merged_claude = merged_claude_settings(existing_claude, claude_template_settings)
    if not hooks_json.exists() or existing != merged:
        changes.append(".cursor/hooks.json")
    if not claude_settings.exists() or existing_claude != merged_claude:
        changes.append(".claude/settings.json")

    for name in CURSOR_HOOK_FILES:
        src = TEMPLATE_ROOT / "hooks" / name
        dst = hooks_dir / name
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            changes.append(f".cursor/hooks/{name}")
    for name in CLAUDE_HOOK_FILES:
        src = CLAUDE_TEMPLATE_ROOT / "hooks" / name
        dst = claude_hooks_dir / name
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            changes.append(f".claude/hooks/{name}")
    for rel in CLAUDE_SKILL_FILES:
        src = CLAUDE_TEMPLATE_ROOT / "skills" / rel
        dst = claude_skills_dir / rel
        if not src.exists():
            continue
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            changes.append(f".claude/skills/{rel}")

    gitignore = project / ".gitignore"
    current_ignore = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if any(line not in set(current_ignore.splitlines()) for line in IGNORE_LINES):
        changes.append(".gitignore")

    if not changes:
        return "unchanged", []
    if not apply:
        return "would-update", changes

    hooks_dir.mkdir(parents=True, exist_ok=True)
    claude_hooks_dir.mkdir(parents=True, exist_ok=True)
    if ".cursor/hooks.json" in changes:
        if hooks_json.exists():
            _backup(hooks_json, stamp)
        _write_json(hooks_json, merged)
    if ".claude/settings.json" in changes:
        if claude_settings.exists():
            _backup(claude_settings, stamp)
        _write_json(claude_settings, merged_claude)
    for name in CURSOR_HOOK_FILES:
        rel = f".cursor/hooks/{name}"
        if rel not in changes:
            continue
        src = TEMPLATE_ROOT / "hooks" / name
        dst = hooks_dir / name
        if dst.exists():
            _backup(dst, stamp)
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR)
    for name in CLAUDE_HOOK_FILES:
        rel = f".claude/hooks/{name}"
        if rel not in changes:
            continue
        src = CLAUDE_TEMPLATE_ROOT / "hooks" / name
        dst = claude_hooks_dir / name
        if dst.exists():
            _backup(dst, stamp)
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR)
    for rel in CLAUDE_SKILL_FILES:
        if f".claude/skills/{rel}" not in changes:
            continue
        src = CLAUDE_TEMPLATE_ROOT / "skills" / rel
        dst = claude_skills_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            _backup(dst, stamp)
        shutil.copy2(src, dst)
        # deliberately no chmod +x: skills are documents plus check_evidence.py, which is
        # invoked as `python3 .../check_evidence.py`, never executed directly.
    if ".gitignore" in changes:
        _append_gitignore(project)
    return "updated", changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("~/code").expanduser())
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"scan root does not exist: {root}")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    projects = discover(root)
    counts: dict[str, int] = {}
    for project in projects:
        status, changes = install(project, apply=args.apply, stamp=stamp)
        counts[status] = counts.get(status, 0) + 1
        suffix = f": {', '.join(changes)}" if changes else ""
        print(f"{status:12} {project}{suffix}")
    print(f"projects={len(projects)} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
