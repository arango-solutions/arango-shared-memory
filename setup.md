# Multi-Project Workflow Automation — Setup & Onboarding

This system gives Claude Code (and Cursor) three capabilities across all your projects:

1. **PRD drift detection, both directions** (`/prd-sync`) — audits code against its PRD, classifies
   every requirement IMPLEMENTED / PARTIAL / MISSING / TEST-ONLY / OUTDATED-PRD, writes open gaps to
   `drift_alerts`, closes them when fixed, and proposes reviewed PRD patches (`prd_patches`) when the
   code has legitimately outgrown the spec. Evidence for IMPLEMENTED claims is mechanically verified
   by `check_evidence.py` before anything is persisted. A PostToolUse hook queues a marker whenever
   an implementation file — or the PRD itself — is edited; a Stop **gate** blocks session end (once)
   while the queue is non-empty (`.no-drift-gate` bypasses; `stop_hook_active` prevents loops).
2. **Shared memory with a taxonomy** (`/pattern-search`, `/pattern-save`) — before solving a
   non-trivial problem, search verified solutions from *any* project; after solving something
   reusable, save it. Memory types: `pattern`, `feedback` (with why + how_to_apply), `user`,
   `project`, `reference`. Retrieval is **hybrid** (semantic vector + BM25 keyword, RRF-fused;
   relevance is then multiplicatively boosted by importance · recency · usage and a learned
   per-pattern **success rate** from recorded apply outcomes — salience modulates relevance,
   never substitutes for it) with a **graph** layer whose edge weights learn from observed
   co-application — all executed server-side, so agents pass text and get ranked results.
   Applying a surfaced pattern is recorded via the `pattern-applied` tool (with an optional
   worked/failed outcome), which feeds both ranking and the reuse funnel. Ranking is
   **eval-backed**: a golden query set + `scripts/eval_retrieval.py` measure recall@k/MRR per
   mode (see "Retrieval evaluation" below).
3. **Automatic recall + capture** — a SessionStart hook injects a per-project digest at session
   start: open drift gaps, last sync, PRD staleness (content hash vs the stored baseline), the
   project's feedback memories, and the top-ranked relevant patterns. At session end a Stop hook
   mines the transcript for **candidate memories** (a Bash/MCP tool that failed then later
   succeeded; user corrections) into `.pattern-capture-queue/` — the next digest nags until `/pattern-save`
   triages them (LLM judgment saves the real lessons, deletes the noise; the hook itself never
   writes to shared memory). Cursor-native hooks mirror the digest, edit queue, and Stop gate.
   Both clients track searched keys and request one `pattern-applied` attribution pass when
   needed, without treating every surfaced result as used. A surfaced key is resolved by *either*
   an apply or an explicit dismissal (`.claude/hooks/dismiss_surfaced.py`, local audit state that
   never writes to `shared_patterns`) — counting only applies made the gate unsatisfiable, since
   its own message correctly forbids marking every result as applied. Fail-open: an unreachable
   database never breaks a session.
4. **Project registry + read-path analytics with attribution** — `project_registry` tracks each
   project (including `prd_sha256`); `search_log` records interactive searches and automatic
   SessionStart recalls as distinct modes (query, hit, project, **who**); every write is stamped
   with its author (`saved_by`, `detected_by`/`closed_by`, a
   capped `apply_log` of who applied what) from the developer's own scoped DB account — so reuse
   and contribution are measurable per person, not assumed.

All skills degrade gracefully when ArangoDB / the MCP is unreachable, and fall back to keyword-only
(BM25) when embeddings aren't configured.

> **Single source of truth:** the CLAUDE.md, hooks, and three skills live in `templates/` and are
> installed by `scripts/bootstrap_project.sh`. **Do not hand-copy skill bodies** — that is exactly how
> earlier docs drifted out of sync. This document references the templates rather than duplicating them.

---

## Prerequisites
- **Python 3.11+ and Poetry**, and **Claude Code and/or Cursor** — everyone.
- **Shared-cluster credentials** (from your team lead) — for the common "join the shared memory" path.
- **Your own OpenAI API key** for embeddings (hybrid/graph). Optional — without it the system is keyword-only.
- **Docker** — *admin only*, for standing up a new local backend (STEP 0). Not needed to join the shared cluster.
- The ArangoDB server **must be started with `--experimental-vector-index`** for hybrid/graph search
  (the shared cluster already is; relevant only if you stand up a new backend).

## Two repositories (clone both under `~/code/`)
```bash
git clone https://github.com/arango-solutions/arango-solutions-mcp.git ~/code/arango-solutions-mcp-server
# and this repo:
git clone <arango-shared-memory remote> ~/code/arango-shared-memory
```
- **arango-solutions-mcp-server** — the FastMCP server exposing the `arangodb-memory-mcp` tools
  (`pattern-search`, `save-pattern`, `embed-document`, `execute-aql-query`, …).
- **arango-shared-memory** (this repo) — setup/phase scripts, project templates, docs.

---

## Which path are you on?
- **Joining the team's shared memory (most people):** the database already exists on the shared
  cluster — get credentials from your team lead. Do **STEP 1, 3, 4, 5** and **skip STEP 0 and STEP 2**.
  Do **not** run `install.py` / `setup_*` / `phase*` against the shared cluster (those stand up a *new*
  backend, and need admin/root). The teammate happy-path is **[ONBOARDING.md](ONBOARDING.md)**.
- **Admin, standing up a NEW backend** (a fresh shared cluster, or a private local one for solo/offline
  use): do **STEP 0 → 1 → 2 → 3 → 4**, then provision teammates (see "Shared deployment").

---

## STEP 0 (admin / new backend only) — ArangoDB (Docker, run once)
The shared memory uses its own ArangoDB CE container on host port **8539** (so it never collides with
another ArangoDB on 8529). **The `arangod --experimental-vector-index` flag is required** — without it,
vector-index creation fails and the system silently stays keyword-only.

```bash
docker run -d --name shared-memory-arangodb --restart unless-stopped \
  -p 8539:8529 -e ARANGO_ROOT_PASSWORD=openSesame \
  -v shared-memory-arango-data:/var/lib/arangodb3 \
  arangodb/arangodb:latest arangod --experimental-vector-index
```
Confirm: `curl -s -u root:openSesame http://localhost:8539/_api/version`

## STEP 1 — Install the server
```bash
# Run with no virtualenv active, or Poetry installs into that one instead of creating .venv.
cd ~/code/arango-solutions-mcp-server
poetry config virtualenvs.in-project true --local   # put the venv at ./.venv, which STEP 3 launches
poetry install
```
This creates the server's virtualenv at `.venv/` (has `python-arango`, etc.). The `poetry config` line
asks for a project-local venv; without it Poetry uses its own cache, and the `.venv/bin/python` in
STEP 3 does not exist. All scripts below run via `poetry run python …` from this directory.

## STEP 2 (admin / new backend only) — Create the schema (run once)
> **Teammates joining the existing shared memory SKIP this** — the schema is already there, and
> `install.py` needs admin/root anyway. This is only for standing up a new backend.

One idempotent command creates the `memory` database, collections
(`shared_patterns`, `project_registry`, `drift_alerts`, `search_log`, `prd_patches`,
`sync_observations`, `schema_migrations`), indexes, JSON schema validation, and the
`patterns_search` BM25 view + graded-scoring fields:
```bash
poetry run python ~/code/arango-shared-memory/scripts/install.py
```
(`install.py` runs `setup_schema.py` → `migrate.py` → `phase1_setup.py` → `verify.py`. It is safe to
re-run. Pass `--with-embeddings` to also run `phase1b`/`phase2` once you have a key + at least one
pattern. On an **existing** database the embedded `migrate.py` step auto-detects and applies only
what's missing — see "Maintenance & migration" below.)

## STEP 3 — Register the MCP server (globally, once per tool)
Register under the id **`arangodb-memory-mcp`** in *both* Claude Code (`~/.claude.json`) and Cursor
(`~/.cursor/mcp.json`), under a top-level `"mcpServers"` key:
```json
{
  "command": "bash",
  "args": ["-c", "cd /Users/<you>/code/arango-solutions-mcp-server && exec .venv/bin/python main.py"],
  "cwd": "/Users/<you>/code/arango-solutions-mcp-server",
  "env": {
    "ARANGO_HOSTS": "https://<shared-cluster-host>:8529",
    "ARANGO_ROOT_USERNAME": "<your username — from your team lead>",
    "ARANGO_ROOT_PASSWORD": "<your password — DO NOT COMMIT>",
    "ARANGO_DEFAULT_DB_NAME": "memory",
    "ARANGO_VERIFY_SSL": "true",
    "OPENAI_API_KEY": "sk-...your own key...",
    "EMBEDDING_MODEL": "text-embedding-3-small"
  }
}
```
- Values above are for **joining the shared cluster** (the common case). For a **new local backend**
  instead, use `"ARANGO_HOSTS": "http://localhost:8539"`, `root` / `openSesame`, and omit `ARANGO_VERIFY_SSL`.
- `OPENAI_API_KEY` enables hybrid/graph. Omit it to run keyword-only. **Never commit this file / key.**
- This launch line matches the server's `main` branch: Poetry with `package-mode = false` and
  `main.py` at the repo root. Its `pyproject.toml` declares an `arangodb-mcp` script, but with
  `package-mode = false` Poetry never installs the project, so that command is never created.
  Launch `main.py` with the venv's Python instead. The `bash -c` wrapper avoids depending on
  `poetry` being on the launcher's PATH.
- Every tool category, `memory` included, is registered on `main`; there are no profiles or
  toolsets to set. What you can write is governed by your database user's permissions.
- **After a `git pull` of the server, re-check the launch line.** If the server moves to a
  packaged layout (a `src/` package with `[project.scripts]`, as on `phase1/platform-spine`), the
  entry point changes. A wrong launch line leaves the memory tools unavailable, and because the
  skills fail open that is easy to miss: check the client's MCP logs.
- Reload Cursor / restart Claude Code so the tools load — a running client keeps its dead connection.

## STEP 4 — Verify
```bash
cd ~/code/arango-solutions-mcp-server
poetry run python ~/code/arango-shared-memory/scripts/verify.py
```
Green across connectivity, collections, indexes, round-trip, and the `patterns_search` view; the
**read-path scorecard** shows searches/hit-rate once you start using it. Exit 0 = healthy.

## STEP 5 — Bootstrap each project
From the project you want to instrument:
```bash
~/code/arango-shared-memory/scripts/bootstrap_project.sh --target ~/code/my-api \
  --project-name "My API" --project-id my-api \
  --project-type web-api --prd-file docs/PRD.md --tech-stack "TypeScript, Node.js"
```
This installs (from `templates/`, filling placeholders) and git-ignores the personal infra:
- `CLAUDE.md` — project identity + the mandatory `/pattern-search → solve → /pattern-save → /prd-sync` protocol
- `.claude/settings.json` — hook wiring + permission allowlist
- `.claude/hooks/` — `session_recall.py` (SessionStart digest), `drift_queue.py` (PostToolUse:
  queues code AND PRD edits), `drift_stop_gate.sh` (Stop gate: blocks once while the queue is
  non-empty), and `pattern_apply_tracker.py` (searched→applied attribution; all hooks fail open)
- `.claude/skills/{prd-sync,pattern-save,pattern-search}/` — the skills (current versions),
  including `prd-sync/check_evidence.py` (the mechanical evidence gate)
- `.cursor/rules/workflow.mdc` — Cursor workflow guidance
- `.cursor/hooks.json` + `.cursor/hooks/` — Cursor-native digest, drift queue, apply-attribution,
  and one-pass Stop enforcement

Re-running is safe: without `--force` existing files are skipped; with `--force`, byte-identical
files are skipped (`unchanged`) and any file that actually changes is first backed up as
`<file>.pre-update.<timestamp>` — the undo for local customizations (e.g. `permissions.allow`
entries you added to `settings.json`), since `.claude/` is gitignored and has no git history.
For existing bootstrapped projects, use `scripts/rollout_cursor_hooks.py` (dry-run by default,
`--apply` to merge-refresh); unrelated Cursor/Claude hook entries are preserved.
Then create the project's `PRD.md` and run
`/prd-sync` to establish its drift baseline. Because `arangodb-memory-mcp` is registered *globally*,
every bootstrapped project can reach shared memory with no per-project MCP wiring.

## Enabling hybrid + graph (if you skipped it in STEP 2)
1. Put `OPENAI_API_KEY` + `EMBEDDING_MODEL` in the MCP env (STEP 3) and reload.
2. Save at least one pattern (`/pattern-save`).
3. `poetry run python .../phase1b_setup.py` (embeddings + vector index) then `.../phase2_setup.py`
   (graph edges). `phase2b_extract.py` (gpt-4o LLM edges) and `phase3_lifecycle.py` (supersede/TTL) are
   periodic maintenance, not required for daily use.

## Maintenance & migration (admin)

**Migrating an existing database** — `scripts/migrate.py` brings any older `memory` database to the
current schema by **auto-detection**: each migration inspects the live database and applies only
when actually needed (collections/indexes added later, all graph edge definitions,
`memory_type: "pattern"` backfill, edge `weight`/`co_applied` backfill, JSON schema validation).
Non-destructive by construction — it only adds, never deletes or rewrites. Applied migrations are
recorded in `schema_migrations`; re-runs self-heal even if the ledger disagrees with reality.
```bash
poetry run python ~/code/arango-shared-memory/scripts/migrate.py --dry-run   # preview
poetry run python ~/code/arango-shared-memory/scripts/migrate.py            # apply
```

**Periodic maintenance** — `scripts/maintain.py` runs every upkeep pass in the right order:
embedding backfill (incl. `embedding_pending` re-embeds after edits) → graph rebuild → edge-weight
recompute (folds co-application into ranking) → patch/observation provenance → supersede/TTL/stale
→ health check. `--with-llm` adds the gpt-4o edge-extraction pass (costs money); `--dry-run`
previews. Schedule it once so it runs without attention:
```bash
scripts/install_maintenance_schedule.sh                 # weekly launchd job (macOS), 03:00 Sunday
scripts/install_maintenance_schedule.sh --interval daily
scripts/install_maintenance_schedule.sh --uninstall
```
On non-macOS it prints the crontab line to add. Log: `~/.arango-shared-memory/maintain.log`.

**Retrieval evaluation (the golden set)** — ranking quality is measured, never assumed. The golden
set lives in `eval/golden_queries.json` (developer-phrased queries → the pattern key(s) that should
surface, tagged `paraphrase` / `keyword` / `cross-project`); `scripts/eval_retrieval.py` syncs it
into `eval_queries` and scores every retrieval mode side-effect-free (its AQL mirrors the server's
but never logs to `search_log` or bumps `surfaced_count`):
```bash
poetry run python ~/code/arango-shared-memory/scripts/eval_retrieval.py            # sync + evaluate
poetry run python ~/code/arango-shared-memory/scripts/eval_retrieval.py --run-only # skip the sync
```
It prints recall@1/3/5, MRR, and per-category breakdowns for `bm25`, `hybrid`, and `hybrid+graph`,
and appends each run to `eval_runs` so quality is trendable. **Run it after any change to the
ranking AQL or scoring weights** (keep the server AQL and the eval AQL in sync — the file headers
say so on both ends), and grow the golden set as important patterns are saved. This harness caught
a real regression on its first run: the old additive salience scoring buried exact matches
(hybrid MRR 0.25 vs BM25 0.93); multiplicative scoring + RRF k=10 lifted hybrid to MRR 0.975.

---

## Shared deployment (team) — local → shared ArangoDB
The value multiplier is a **single shared `memory` DB** so patterns/drift are visible across the whole
team, not just across one person's projects. Because every script and the MCP tool resolve the host
from env, switching is a **config change**: set each teammate's `arangodb-memory-mcp` env
`ARANGO_HOSTS` (and credentials) to the shared host, then run `install.py` once against it.

Checklist for the shared server:
- Start ArangoDB with `--experimental-vector-index` (ops-owned).
- Real credentials + TLS (`https://…`, `ARANGO_VERIFY_SSL=true`) — retire `openSesame`.
- Run `install.py` once against the shared host to create schema + view.
- Keep the OpenAI key in each teammate's MCP env (or centralize embedding behind the shared server).

**Rehearsed against a real 3.12.9 cluster (2026-07-16)** — `install.py` (schema + BM25 view +
round-trip) worked over TLS with real auth on the first try. Two remote-only gotchas surfaced that do
**not** appear on fast local single-server, so plan for them when running `phase1b_setup.py` on the shared box:
- **Vector-index build outlasts the client timeout.** `add_index` for the cosine index exceeds
  python-arango's default 60 s read timeout (FAISS training + latency); the client raises `ReadTimeout`
  but the index *is* created server-side. Use a longer `request_timeout` and verify with `list-indexes`
  rather than trusting the call to return.
- **"Not yet trained" window.** Immediately after, `APPROX_NEAR_COSINE` can fail with
  `ERR 1555 (not yet trained)` briefly — poll until a trivial vector query succeeds before relying on it.
  (`phase1b_setup.py` should be given a longer timeout / retry when pointed at a shared cluster.)

### Provisioning teammates (per-developer users)
Give each developer **their own** least-privilege user — attribution, clean offboarding, and no
shared password to rotate. The connection identity IS the attribution: the server stamps every
write with the connected username (`saved_by` on memories, `detected_by`/`closed_by` on drift
alerts, `by` on searches, a capped `apply_log` of who applied each pattern), so per-developer
scorecards fall straight out of the data. Docs written before attribution shipped were backfilled
and carry `attribution_backfilled: true` to stay distinguishable from organic stamps. The admin
runs (root creds resolved from their own MCP config):
```bash
poetry run python scripts/add_teammate.py <username>          # create: rw on `memory` only, prints creds once
poetry run python scripts/add_teammate.py <username> --revoke # offboard: deactivate + revoke
```
Hand the printed credentials to the developer **out-of-band** (never commit them); they go only in
that developer's local MCP config. The user gets `rw` on `memory` and **no access** to other databases
on the shared cluster (verified at creation).

**Recommended:** direct shared writes (everyone's MCP → the one shared DB). **Not recommended:**
local-arango-per-person *syncing* into a shared one — it adds sync lag and cross-instance
merge/dedup complexity for no benefit on a networked team. If you want private experimentation, use a
separate local `memory` DB and switch to the shared one via env — two databases, not a sync pipeline.

---

## Troubleshooting
| Symptom | Cause | Fix |
|---|---|---|
| `pattern-search` errors / returns nothing | `patterns_search` view missing | run `install.py` (or `phase1_setup.py`) |
| Everything keyword-only; no semantic hits | no `OPENAI_API_KEY`, or arangod lacks `--experimental-vector-index` | add the key (STEP 3) + recreate the container with the flag (STEP 0), then `phase1b_setup.py` |
| Vector index creation fails (`ERR 10`) | server not started with `--experimental-vector-index` | recreate the container with the flag; data persists in the named volume |
| `add_index` raises `ReadTimeout` on a remote cluster | FAISS training + latency exceeds the 60s client timeout | raise `request_timeout`; the index is still created server-side — verify with `list-indexes` |
| `ERR 1555 vector index is not yet trained` | queried a just-created index before training finished (remote) | poll/retry `APPROX_NEAR_COSINE` until it succeeds |
| Drift hook never fires | stale hook reading `$CLAUDE_TOOL_INPUT` | re-bootstrap (current hook reads stdin/`tool_input`); Cursor doesn't run Claude Code hooks — expected |
| No session digest at start | placeholders unrendered in CLAUDE.md, or DB unreachable (hook fails open by design) | check `PROJECT_ID:` is a real value; run `verify.py`; re-bootstrap with `--force` for the hook files |
| Session end blocked by the drift gate | `.prd-drift-queue/` non-empty | run `/prd-sync` (clears the queue); it blocks at most once per stop; per-repo bypass: `touch .no-drift-gate` |
| Insert rejected: "schema validation failed" | document violates the collection's JSON schema (missing project_id, bad enum value) | fix the document — the schema is the guard, not the bug; rules live in `setup_schema.py` |
| `prd_patches`/`sync_observations` missing | database predates the migration round | `poetry run python .../scripts/migrate.py` |
| MCP server won't start / memory tools silently missing | config launches `.venv/bin/arangodb-mcp`, which `main` never creates; or the venv is in Poetry's cache, so `.venv/` is missing | launch `.venv/bin/python main.py` (see STEP 3). If `.venv/` is missing, run `poetry env info --path` in the server dir; if it points outside the repo, `poetry env remove --all` and redo STEP 1. Then fully restart the client — a running one keeps its dead connection |
| `ERR 1521 collection not known to traversal` | cluster traversal missing `WITH` | add `WITH <all reachable vertex collections>` (needed on cluster, hidden on single-server) |
| `ERR 1579 access after data-modification` | one AQL reads a collection after modifying it | split into separate statements |
| Saving a pattern fails: `Expecting type Array` | inserting into a vector-indexed collection without the embedding | use `save-pattern` (embeds then inserts); don't insert-then-embed |
