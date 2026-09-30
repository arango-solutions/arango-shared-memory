#!/usr/bin/env python3
"""Stop-hook reconciliation — queue drift for edits the PostToolUse hook cannot see.

`drift_queue.py` fires on Write|Edit|MultiEdit|NotebookEdit and reads
`tool_input.file_path`. A Bash call carries no `file_path`, so `cat > f <<EOF`,
`sed -i`, a generated script, or an edit made outside the session entirely never
queues a marker — and the Stop gate, which only counts markers, then lets the
session end with unaudited changes. That is not an edge case: tool-preference
settings actively steer agents toward the shell, so it is the common path.

This closes the gap from the other side. Rather than guessing which files a shell
command touched — unknowable without parsing arbitrary shell — it asks git what
actually changed and queues whatever the queue is missing. Every write path is
covered, because none of them can hide from the working tree.

Scope is "content changed since the last /prd-sync audited it":

    stamp      a git tree of the working tree as /prd-sync audited it
    now        a git tree of the working tree as it is at Stop time
    changed    git diff-tree -r --name-only <stamp> <now>

Both trees are built in a throwaway index (GIT_INDEX_FILE), so tracked, modified and
untracked files all count, .gitignore is honoured, and the repository's real index is
never touched. Building a tree writes blob objects for untracked files into the object
store, exactly as `git stash -u` does; they are unreachable and `git gc` collects them.

Why a tree, not a commit (the design before 2026-09-30). /prd-sync audits the WORKING
TREE, but the stamp used to record HEAD. Audited work that was still uncommitted
therefore always differed from the stamp: the gate re-queued it the moment the sync
finished, and re-running the sync re-stamped the same HEAD — a loop with zero actual
change, hitting every repo that audits before committing, which is the normal order.
Comparing content makes "audited" and "unchanged since" the same question. Committing
audited work changes no content, so it stays quiet; editing a file after the audit
changes its blob, so it is caught; reverting an edit to the audited content is,
correctly, not drift.

The stamp lives in `.prd-drift-queue/.last-sync`. It is a **dotfile on purpose**: the
gate's `ls` does not count it and the clear step skips it. It is written — after the
queue is cleared, in one command so the order cannot be got wrong — by /prd-sync
Phase 6c:

    python3 .claude/hooks/reconcile_drift_queue.py --mark-synced

Compatibility and failure direction. A legacy stamp holding a commit SHA still works:
`<sha>^{tree}` resolves to that commit's tree, which reproduces the old behaviour
until the next sync writes a tree stamp. With no stamp, or when a snapshot cannot be
built (timeout, a pruned stamp object), it falls back to the old status + commit-range
scan. Every fallback over-reports rather than under-reports: a spurious sync is cheap,
a missed audit is not. On first adoption (no stamp) only uncommitted changes count, so
adoption cannot flood the queue with a repository's entire history.

The reconcile path fails open in every direction: not a git repo, git missing,
unreadable stamp, any exception at all -> queue nothing, exit 0. A broken reconciler
must never trap a session, and must never manufacture drift that isn't there.
`--mark-synced` is the exception: it is an explicit command, so it reports failure
with a non-zero exit instead of hiding it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from drift_queue import classify, enqueue, prd_file_from_claude_md  # noqa: E402

QUEUE_DIR = ".prd-drift-queue"
STAMP = os.path.join(QUEUE_DIR, ".last-sync")

#: Staging a large untracked tree into a scratch index can take longer than the
#: 5 s default; on timeout the snapshot is None and the scan falls back (safe side).
SNAPSHOT_TIMEOUT = 30


def _git(*args, timeout=5, env=None):
    """Run a git command, returning stdout or None. Never raises."""
    try:
        out = subprocess.run(("git",) + args, capture_output=True, text=True,
                             timeout=timeout, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def snapshot_tree():
    """Tree SHA of the working tree as it is right now, or None.

    Seeds a scratch index from the real one, so git reuses its cached stat data instead
    of re-hashing every tracked file, then stages everything into the scratch index —
    tracked, modified and untracked, .gitignore honoured — with the queue directory
    excluded so markers and the stamp can never be their own drift. The real index is
    never written.
    """
    real = _git("rev-parse", "--git-path", "index")
    if real is None:
        return None                       # not a git repo (or git unavailable)
    fd, scratch = tempfile.mkstemp(prefix="drift-snapshot-", suffix=".index")
    os.close(fd)
    try:
        try:
            # copy2, not copyfile: git decides whether to trust an entry's cached stat
            # data by comparing it with the index FILE's own mtime ("racy git"). A fresh
            # mtime on the copy defeats that check, so a file rewritten within one
            # timestamp tick of the last index write is trusted as unchanged and its
            # OLD blob lands in the tree. Preserving the mtime gives the snapshot the
            # same protection `git status` has. (Measured: copyfile failed 2/40 runs of
            # the revert/edit tests; copy2 0/80.)
            shutil.copy2(real.strip(), scratch)
        except OSError:
            # No index yet (fresh `git init`). git refuses an empty file as an index,
            # so remove the placeholder and let `git add` create a fresh one.
            os.remove(scratch)
        env = dict(os.environ, GIT_INDEX_FILE=scratch)
        staged = _git("add", "-A", "--", ":/", f":(top,exclude){QUEUE_DIR}",
                      timeout=SNAPSHOT_TIMEOUT, env=env)
        if staged is None:
            return None
        tree = _git("write-tree", timeout=SNAPSHOT_TIMEOUT, env=env)
        if tree is None:
            return None
        return tree.strip() or None
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass


def _stamp_ref():
    """First token of the stamp file, or None."""
    try:
        with open(STAMP, encoding="utf-8") as fh:
            ref = fh.read().split()[0].strip()
    except (OSError, IndexError):
        return None
    return ref or None


def last_sync_tree():
    """The tree /prd-sync last audited, or None.

    Accepts both stamp formats — a tree SHA (current) and a commit SHA (legacy,
    resolved to that commit's tree). A stamp naming an object that no longer exists
    (pruned by gc, or a commit dropped by rebase/amend/reset) is treated as absent:
    the scan then falls back to the over-reporting legacy path, never to silence.
    """
    ref = _stamp_ref()
    if ref is None:
        return None
    tree = _git("rev-parse", "--verify", "--quiet", ref + "^{tree}")
    return tree.strip() if tree else None


def last_sync_sha():
    """The stamp as a commit SHA, or None — legacy stamps only (used by the fallback)."""
    ref = _stamp_ref()
    if ref is None:
        return None
    return ref if _git("cat-file", "-e", ref + "^{commit}") is not None else None


def _legacy_changed_paths():
    """Pre-tree scan: uncommitted paths plus commits since a commit stamp.

    Used only when there is no usable stamp or no snapshot. It over-reports —
    audited-but-uncommitted work re-queues — which is the safe direction.
    """
    paths = set()

    # Uncommitted. --porcelain columns are fixed-width status + path; a rename
    # reads "R  old -> new" and only the destination is the edited file.
    status = _git("status", "--porcelain", "--untracked-files=all")
    if status is None:
        return paths          # not a git repo (or git unavailable) -> fail open
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path.strip().strip('"'))

    # Committed since a legacy commit stamp. Without one this half is skipped.
    sha = last_sync_sha()
    if sha:
        diff = _git("diff", "--name-only", f"{sha}..HEAD")
        if diff:
            paths.update(p.strip() for p in diff.splitlines() if p.strip())

    return paths


def changed_paths():
    """Every path whose content differs from what the last /prd-sync audited."""
    audited = last_sync_tree()
    if audited:
        now = snapshot_tree()
        if now:
            diff = _git("diff-tree", "-r", "--name-only", audited, now)
            if diff is not None:
                return {p.strip() for p in diff.splitlines() if p.strip()}
    return _legacy_changed_paths()


def mark_synced() -> int:
    """/prd-sync Phase 6c: clear the queue, then stamp the audited working tree.

    One command, so a stamp can never be written before the clear that would delete
    it. Deletes only regular, non-dot files directly inside the queue directory; the
    stamp, subdirectories and symlinks are left alone. Done here rather than with
    `rm -f .prd-drift-queue/*` because a shell glob whose base cannot be resolved
    statically is exactly what agent command-safety checks refuse, which made the
    skill's own clear step unrunnable in those environments.
    """
    os.makedirs(QUEUE_DIR, exist_ok=True)
    cleared = 0
    for name in sorted(os.listdir(QUEUE_DIR)):
        path = os.path.join(QUEUE_DIR, name)
        if name.startswith(".") or os.path.islink(path) or not os.path.isfile(path):
            continue
        os.remove(path)
        cleared += 1

    tree = snapshot_tree()
    head = _git("rev-parse", "--verify", "--quiet", "HEAD")
    head = head.strip() if head else ""
    if tree:
        stamp = f"{tree}\n# audited working tree; HEAD was {head or 'unborn'}\n"
        what = f"audited tree {tree[:12]}"
    elif head:
        stamp = f"{head}\n# working-tree snapshot failed; legacy commit stamp\n"
        what = f"LEGACY commit {head[:12]} (snapshot failed; uncommitted work will re-queue)"
    else:
        print(f"[PRD-SYNC] Cleared {cleared} marker(s); NOT stamped — not a git repo "
              "or git unavailable, so the reconciler cannot run here anyway.")
        return 0
    with open(STAMP, "w", encoding="utf-8") as fh:
        fh.write(stamp)
    print(f"[PRD-SYNC] Cleared {cleared} marker(s); stamped {what}.")
    return 0


def main() -> int:
    if os.path.exists(".no-drift-gate"):
        return 0

    prd = prd_file_from_claude_md()
    queued = []
    for path in sorted(changed_paths()):
        if not os.path.exists(path):
            continue                      # deleted, or a path git reports we can't see
        kind = classify(path, prd=prd)
        if kind is None:
            continue
        if enqueue(path, kind) is not None:
            queued.append((kind, path))

    if queued:
        # Named, not just counted: the whole point is that these edits were
        # invisible, so the message has to say which ones were recovered.
        shown = ", ".join(p for _, p in queued[:8])
        more = f" (+{len(queued) - 8} more)" if len(queued) > 8 else ""
        print(f"[PRD-DRIFT] Reconciled {len(queued)} change(s) edited since the last "
              f"/prd-sync audit: {shown}{more}. Run /prd-sync.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--mark-synced"]:
        # An explicit command, not a hook: surface failure rather than fail open.
        raise SystemExit(mark_synced())
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - fail open
        raise SystemExit(0)
