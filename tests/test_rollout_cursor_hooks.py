"""DB-free tests for Cursor hook rollout/merge behavior."""

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "rollout_cursor_hooks.py"


def _load():
    spec = importlib.util.spec_from_file_location("rollout_cursor_hooks", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRolloutCursorHooks(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        (self.project / ".claude" / "hooks").mkdir(parents=True)
        (self.project / ".claude" / "hooks" / "session_recall.py").write_text(
            "#!/usr/bin/env python3\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_discovery_requires_bootstrapped_session_hook(self):
        other = Path(self.temp.name) / "unrelated"
        other.mkdir()
        self.assertEqual(self.mod.discover(Path(self.temp.name)), [self.project])

    def test_discovers_a_skills_only_project(self):
        """A project with skills but no hooks must still be reachable.

        The old predicate required .claude/hooks/session_recall.py, so a half-installed
        project — skills placed, hooks never — was invisible to every rollout and could
        never be refreshed. That is precisely the population most likely to be stale.
        """
        skills_only = Path(self.temp.name) / "skills-only"
        (skills_only / ".claude" / "skills" / "prd-sync").mkdir(parents=True)
        (skills_only / ".claude" / "skills" / "prd-sync" / "SKILL.md").write_text(
            "# stale\n", encoding="utf-8")
        found = self.mod.discover(Path(self.temp.name))
        self.assertIn(skills_only, found)
        self.assertIn(self.project, found)

    def test_excluded_projects_are_never_discovered(self):
        """A named exclusion must survive every future --apply.

        contextual-data-fabric tracks .claude/ in a public, PR-governed repo, so a
        rollout there is an unreviewed change to published content; it was fully
        restored on 2026-08-26 for exactly that reason. Without this, the next
        --apply silently redoes what that restore undid.
        """
        excluded = Path(self.temp.name) / self.mod.EXCLUDED_PROJECTS[0]
        (excluded / ".claude" / "hooks").mkdir(parents=True)
        (excluded / ".claude" / "hooks" / "session_recall.py").write_text("x\n", encoding="utf-8")
        found = self.mod.discover(Path(self.temp.name))
        self.assertNotIn(excluded, found, "excluded project was discovered")
        self.assertIn(self.project, found, "exclusion must not suppress other projects")

    def test_a_project_can_opt_out_with_a_marker_file(self):
        """Self-service opt-out, mirroring the .no-drift-gate bypass convention."""
        optout = Path(self.temp.name) / "opted-out"
        (optout / ".claude" / "hooks").mkdir(parents=True)
        (optout / ".claude" / "hooks" / "session_recall.py").write_text("x\n", encoding="utf-8")
        self.assertIn(optout, self.mod.discover(Path(self.temp.name)))
        (optout / self.mod.OPT_OUT_MARKER).write_text("", encoding="utf-8")
        self.assertNotIn(optout, self.mod.discover(Path(self.temp.name)))

    def test_discovery_still_ignores_unrelated_repos(self):
        """Widening the predicate must not widen the blast radius."""
        for name, rel in (("plain", None),
                          ("has-git", ".git/config"),
                          ("other-claude-skills", ".claude/skills/some-other-skill/SKILL.md")):
            repo = Path(self.temp.name) / name
            if rel is None:
                repo.mkdir()
            else:
                (repo / rel).parent.mkdir(parents=True)
                (repo / rel).write_text("x\n", encoding="utf-8")
            self.assertNotIn(repo, self.mod.discover(Path(self.temp.name)),
                             f"{name} should not be treated as bootstrapped")

    def test_merge_preserves_unrelated_cursor_hooks(self):
        existing = {
            "version": 1,
            "hooks": {
                "afterFileEdit": [{"command": ".cursor/hooks/format.sh"}],
                "beforeShellExecution": [{"command": ".cursor/hooks/network-gate.sh"}],
            },
        }
        template = json.loads(
            (REPO / "templates" / ".cursor" / "hooks.json").read_text(encoding="utf-8")
        )
        merged = self.mod.merged_hooks(existing, template)
        edit_commands = [entry["command"] for entry in merged["hooks"]["afterFileEdit"]]
        self.assertIn(".cursor/hooks/format.sh", edit_commands)
        self.assertIn("python3 .cursor/hooks/shared_memory_drift_queue.py", edit_commands)
        self.assertEqual(
            merged["hooks"]["beforeShellExecution"], existing["hooks"]["beforeShellExecution"]
        )

    def test_merge_preserves_unrelated_claude_settings(self):
        existing = {
            "hooks": {
                "PostToolUse": [{"matcher": "Write", "hooks": [{"command": "format"}]}]
            },
            "permissions": {"allow": ["Bash(pytest*)"]},
        }
        template = json.loads(
            (REPO / "templates" / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        merged = self.mod.merged_claude_settings(existing, template)
        self.assertEqual(merged["permissions"], existing["permissions"])
        self.assertEqual(len(merged["hooks"]["PostToolUse"]), 2)
        self.assertIn(
            "pattern-search",
            merged["hooks"]["PostToolUse"][1]["matcher"],
        )

    def test_apply_syncs_skills_into_subdirectories(self):
        """Skills must refresh, and must land in their nested paths.

        This tool synced only hooks for its whole life; bootstrap_project.sh places
        skills once and skips existing files, so skills drifted silently and
        permanently — 29 of 32 deployed prd-sync/SKILL.md copies were stale, one by
        140 lines. Unlike hooks, skills carry a subdirectory, so a flat copy would
        put them in the wrong place or crash on a missing parent.
        """
        stamp = "20260825_000000"
        status, changes = self.mod.install(self.project, apply=True, stamp=stamp)
        self.assertEqual(status, "updated")

        skills = self.project / ".claude" / "skills"
        for rel in self.mod.CLAUDE_SKILL_FILES:
            src = self.mod.CLAUDE_TEMPLATE_ROOT / "skills" / rel
            if not src.exists():
                continue
            dst = skills / rel
            self.assertTrue(dst.is_file(), f"{rel} not installed")
            self.assertEqual(dst.read_bytes(), src.read_bytes(), f"{rel} content differs")
            self.assertIn(f".claude/skills/{rel}", changes)

        # a second pass must be a no-op, not a re-copy
        status2, changes2 = self.mod.install(self.project, apply=True, stamp=stamp)
        self.assertNotIn(status2, ("error",))
        self.assertEqual([c for c in changes2 if c.startswith(".claude/skills/")], [])

    def test_apply_backs_up_a_drifted_skill_before_overwriting(self):
        """A stale skill is replaced, but never without a recoverable copy."""
        stamp = "20260825_000001"
        rel = "prd-sync/SKILL.md"
        dst = self.project / ".claude" / "skills" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("# an old, drifted skill\n", encoding="utf-8")

        self.mod.install(self.project, apply=True, stamp=stamp)
        backup = dst.with_name(f"{dst.name}.pre-update.{stamp}")
        self.assertTrue(backup.is_file(), "no backup written before overwrite")
        self.assertEqual(backup.read_text(encoding="utf-8"), "# an old, drifted skill\n")
        self.assertNotEqual(dst.read_text(encoding="utf-8"), "# an old, drifted skill\n")

    def test_apply_installs_hooks_and_is_idempotent(self):
        status, _ = self.mod.install(self.project, apply=True, stamp="test")
        self.assertEqual(status, "updated")
        self.assertTrue((self.project / ".cursor" / "hooks.json").is_file())
        mode = (
            self.project / ".cursor" / "hooks" / self.mod.CURSOR_HOOK_FILES[0]
        ).stat().st_mode
        self.assertTrue(mode & 0o100)
        self.assertIn(
            ".cursor/.shared-memory-sessions/",
            (self.project / ".gitignore").read_text(encoding="utf-8"),
        )

        status, changes = self.mod.install(self.project, apply=True, stamp="test2")
        self.assertEqual((status, changes), ("unchanged", []))

    def test_every_hook_the_stop_gate_names_is_shipped(self):
        """The gate runs its helper scripts with `|| true`, so a missing one fails silently.

        reconcile_drift_queue.py was added to templates and to the gate but to neither
        installer, so projects set up by them never received it.
        """
        gate = (REPO / "templates/.claude/hooks/drift_stop_gate.sh").read_text(encoding="utf-8")
        bootstrap = (REPO / "scripts/bootstrap_project.sh").read_text(encoding="utf-8")
        named = set(re.findall(r"\.claude/hooks/(\w+\.py)", gate))
        self.assertIn("reconcile_drift_queue.py", named)
        for name in sorted(named):
            self.assertIn(name, self.mod.CLAUDE_HOOK_FILES)
            self.assertTrue(f'place ".claude/hooks/{name}"' in bootstrap,
                            f"bootstrap_project.sh never places {name}")
        # The reconciler imports classify/enqueue, which older drift_queue.py copies lack.
        self.assertIn("drift_queue.py", self.mod.CLAUDE_HOOK_FILES)


if __name__ == "__main__":
    unittest.main()
