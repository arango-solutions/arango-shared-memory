"""Every artifact something references must be shipped by BOTH installers.

This is the class-level guard for a defect this repo has now shipped three times:

  - dismiss_surfaced.py (2026-08-25) — the stop gate's block message named a script that
    was in no installer list, so it pointed 30 projects at a file that was not there.
  - the prd-sync skills (2026-08-25) — placed once by bootstrap, never refreshed, so 28
    projects ran month-old protocol files including a too-permissive evidence gate.
  - reconcile_drift_queue.py (2026-09-05, found by Lokesh) — added to drift_stop_gate.sh
    in the same commit that touched neither installer. 30 of 31 projects never got it,
    and the gate invokes it as `... 2>/dev/null || true`, so the reconciliation that was
    supposed to catch shell edits silently did not run ANYWHERE. The same audit found
    capture_candidates.py shipped by bootstrap but absent from the rollout list: present
    in every project, frozen at whatever version it was bootstrapped with.

The pattern is always the same and always invisible: a correct fix, committed, that never
reaches the fleet. Nothing compares what is referenced against what is delivered. That is
what this file does.

Two installers, two different jobs, and an artifact needs both:
  bootstrap_project.sh      places files into a NEW project (skips existing files)
  rollout_cursor_hooks.py   REFRESHES files in existing projects
Missing from the first, new projects never get it. Missing from the second, it can never
be updated after bootstrap — silent permanent drift.

Stdlib-only and source-level, like the other guards here: CI installs no dependencies.
"""

import importlib.util
import json
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(REPO, "templates", ".claude")
BOOTSTRAP = os.path.join(REPO, "scripts", "bootstrap_project.sh")
ROLLOUT = os.path.join(REPO, "scripts", "rollout_cursor_hooks.py")

# Referenced-but-deliberately-not-installed. Keep empty unless there is a real reason,
# and state it — an exemption here is how the next instance of this bug hides.
EXEMPT: set[str] = set()

# Capture the hooks/ or skills/ segment TOO — without it every path resolves to
# templates/.claude/<file> instead of templates/.claude/hooks/<file>, and the
# existence check reports the entire fleet missing. (It did, on first run.)
REF_RE = re.compile(r"\.claude/((?:hooks|skills)/[A-Za-z0-9_/-]+\.(?:py|sh))")

# A hook can also depend on a sibling by IMPORTING it — no path string anywhere, so the
# reference scan above is blind to it. reconcile_drift_queue.py does exactly this
# (`from drift_queue import classify, enqueue, ...`), and the failure mode is nastier:
# the file installs fine, then raises ImportError at runtime on any project whose
# drift_queue.py predates the functions it wants — swallowed by the gate's `|| true`.
# Found by Lokesh while testing PR #1, against a revision of this guard that stayed
# green straight through it.
IMPORT_RE = re.compile(r"^\s*(?:from\s+([a-z_][a-z0-9_]*)\s+import|import\s+([a-z_][a-z0-9_]*))",
                       re.MULTILINE)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _imported_siblings():
    """hooks/<x>.py for every sibling module a hook imports.

    Only names resolving to a real file in templates/.claude/hooks/ count, so stdlib
    imports are ignored without maintaining a denylist.
    """
    hooks_dir = os.path.join(TEMPLATES, "hooks")
    if not os.path.isdir(hooks_dir):
        return set()
    found: set[str] = set()
    for name in os.listdir(hooks_dir):
        if not name.endswith(".py"):
            continue
        for a, b in IMPORT_RE.findall(_read(os.path.join(hooks_dir, name))):
            mod = a or b
            if mod and mod + ".py" != name and os.path.isfile(
                    os.path.join(hooks_dir, mod + ".py")):
                found.add(f"hooks/{mod}.py")
    return found


def _referenced_artifacts():
    """Every .claude/hooks|skills file named by a hook, settings.json, or a skill."""
    found: set[str] = set()
    for root, dirs, files in os.walk(TEMPLATES):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith((".py", ".sh", ".md", ".json")):
                continue
            path = os.path.join(root, name)
            for rel in REF_RE.findall(_read(path)):
                # A file referencing itself (a docstring example) is not a dependency.
                if os.path.basename(rel) != name:
                    found.add(rel)
    return found


class TestInstallerCompleteness(unittest.TestCase):
    def setUp(self):
        self.bootstrap = _read(BOOTSTRAP)
        spec = importlib.util.spec_from_file_location("rollout_cursor_hooks", ROLLOUT)
        self.rollout_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.rollout_mod)
        # A dependency is a dependency whether named by path or by import.
        self.referenced = _referenced_artifacts() | _imported_siblings()

    def test_scanner_finds_the_known_references(self):
        """Guard the guard: a regex that silently matches nothing would pass everything.

        An earlier version of this audit dropped most entries and reported a clean
        bill of health for a fleet with two orphans in it.
        """
        self.assertGreaterEqual(
            len(self.referenced), 6,
            f"reference scan found only {len(self.referenced)} artifacts — the scanner "
            f"is broken, not the installers: {sorted(self.referenced)}")
        self.assertIn("hooks/drift_queue.py", self.referenced,
                      "import-dependency scan is broken: reconcile_drift_queue.py "
                      "imports drift_queue, which must count as a dependency")
        for expected in ("reconcile_drift_queue.py", "session_recall.py",
                         "prd-sync/check_evidence.py"):
            self.assertTrue(
                any(r.endswith(expected) for r in self.referenced),
                f"scanner missed a known reference: {expected}")

    def test_every_referenced_artifact_exists_in_templates(self):
        for rel in sorted(self.referenced):
            if rel in EXEMPT:
                continue
            self.assertTrue(
                os.path.isfile(os.path.join(TEMPLATES, rel)),
                f"{rel} is referenced but does not exist in templates/.claude/")

    def _rollout_ships(self, rel):
        """Membership in the real tuples — NOT a substring search of the source.

        Substring matching gave a false negative that let a real orphan through:
        "drift_queue.py" is a substring of "shared_memory_drift_queue.py" in
        CURSOR_HOOK_FILES, so the Claude hook looked shipped while CLAUDE_HOOK_FILES
        did not list it at all.
        """
        kind, _, name = rel.partition("/")
        if kind == "hooks":
            return name in self.rollout_mod.CLAUDE_HOOK_FILES
        return name in self.rollout_mod.CLAUDE_SKILL_FILES

    def _bootstrap_places(self, rel):
        """An exact `place ".claude/<rel>"` line, for the same reason."""
        return re.search(rf'place\s+"\.claude/{re.escape(rel)}"', self.bootstrap) is not None

    def test_every_referenced_artifact_is_placed_by_bootstrap(self):
        """New projects must receive it."""
        missing = [rel for rel in sorted(self.referenced)
                   if rel not in EXEMPT and not self._bootstrap_places(rel)]
        self.assertEqual(
            missing, [],
            "referenced but not placed by bootstrap_project.sh — new projects will be "
            f"pointed at files that are not there: {missing}")

    def test_every_referenced_artifact_is_refreshed_by_rollout(self):
        """Existing projects must be able to RECEIVE UPDATES to it.

        Absence here is the silent one: the file exists everywhere, at whatever version
        it was bootstrapped with, and no rollout will ever correct it.
        """
        missing = [rel for rel in sorted(self.referenced)
                   if rel not in EXEMPT and not self._rollout_ships(rel)]
        self.assertEqual(
            missing, [],
            "referenced but not refreshed by rollout_cursor_hooks.py — permanently "
            f"frozen at bootstrap version in every project: {missing}")

    def test_settings_hook_commands_resolve_to_shipped_files(self):
        """Anything settings.json registers as a hook must also be installed."""
        settings = json.loads(_read(os.path.join(TEMPLATES, "settings.json")))
        for event, groups in (settings.get("hooks") or {}).items():
            for group in groups:
                for hook in group.get("hooks") or []:
                    for rel in REF_RE.findall(hook.get("command", "")):
                        with self.subTest(event=event, artifact=rel):
                            self.assertTrue(
                                os.path.isfile(os.path.join(TEMPLATES, rel)),
                                f"{event} hook points at missing {rel}")
                            self.assertTrue(self._bootstrap_places(rel),
                                            f"{rel} not placed by bootstrap")
                            self.assertTrue(self._rollout_ships(rel),
                                            f"{rel} not refreshed by rollout")


if __name__ == "__main__":
    unittest.main()
