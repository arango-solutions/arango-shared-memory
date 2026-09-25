# arango-shared-memory

[![CI](https://github.com/arango-solutions/arango-shared-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/arango-solutions/arango-shared-memory/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A multi-project workflow-automation system that gives Claude Code and Cursor these
cross-project capabilities, backed by ArangoDB:

1. **PRD drift detection, both directions** (`/prd-sync`) — audits a codebase against its PRD,
   classifies every requirement IMPLEMENTED / PARTIAL / MISSING / TEST-ONLY / OUTDATED-PRD,
   tracks open gaps in `drift_alerts`, and proposes reviewed PRD patches (`prd_patches`) when the
   code has legitimately outgrown the spec. Every IMPLEMENTED claim is mechanically verified
   (`check_evidence.py`) before it is persisted.
2. **Shared memory with a taxonomy** (`/pattern-search`, `/pattern-save`) — a `shared_patterns`
   store of verified solutions plus `feedback` / `user` / `project` / `reference` memories,
   usable from any project and filterable by `memory_type`. Retrieval is **hybrid** (semantic vector + BM25 keyword, RRF-fused,
   relevance multiplicatively boosted by importance/recency/usage and a learned per-pattern
   success rate) with a **graph** layer whose edge weights learn from real co-application.
   Ranking quality is **measured, not asserted** — against a golden query set
   (`scripts/eval_retrieval.py`), which caught a real regression on its first day.
   **Read the caveat before trusting the number.** The current set is *saturated*:
   MRR 0.98 / R@5 1.00, **identical across bm25, hybrid, and hybrid+graph**. It
   therefore cannot distinguish the three modes, and does **not** establish that the
   hybrid or graph layers earn their place — and the corpus it was built against has
   since grown ~43% (68 → 97 patterns) without a re-measure. Rebuilding it — harder
   queries, per-mode deltas allowed to diverge, and an isolation run that attributes
   any gain to a named component — is the top-ranked open item in
   [docs/scorecard.md](docs/scorecard.md) §6. The *reuse* funnel, by contrast, is now
   measured post-fix: after two attribution defects were removed, conversion settled at
   40% and applies-per-search at 1.01 (scorecard, Round 6) — lower than the old
   inflated 44%, and finally believable.
3. **Automatic recall, capture, and enforcement** — a SessionStart hook injects a per-project
   digest (open gaps, PRD staleness, feedback memories, top patterns); a PostToolUse hook queues
   drift markers (code *and* PRD edits, incl. MultiEdit/NotebookEdit); a Stop hook mines the
   session transcript for **candidate memories** (resolved tool failures — Bash or MCP — and user
   corrections → `.pattern-capture-queue/`, triaged by `/pattern-save`); a Stop gate blocks session
   end (once) while the drift queue is non-empty. Cursor-native project hooks now mirror the
   Claude hooks. Both clients track searched pattern keys and issue one attribution follow-up when
   `pattern-applied` was omitted; they never equate "surfaced" with "applied." Everything is
   **fail-open but never fail-silent**: an unreachable backend cannot break a session, and it
   announces itself instead of impersonating a quiet day (three multi-day outages hid behind
   silent fail-open before that rule existed).
4. **Project registry + read-path analytics with attribution** — `project_registry` tracks each
   project's state (including the PRD content hash); `search_log` records interactive searches
   and automatic SessionStart recalls as distinct modes; every
   write is stamped with **who did it** (`saved_by` / `detected_by` / apply log, from each
   developer's own scoped DB account) so reuse and contribution are measurable *per person*,
   not assumed.
5. **Graph Visualizer assets** (`scripts/install_visualizer.py`) — a status-coloured theme
   (alerts red/amber/grey by `status`, patches by `review_state`, observations by `state`),
   status-scoped load queries, and canvas actions for the `memory_graph`. **Advisory:** on large
   canvases a known product bug renders nodes as attribute-less stubs (colours/labels degrade,
   and the Properties panel must not be used to edit — see
   [docs/visualizer/BUG-REPORT-node-hydration.md](docs/visualizer/BUG-REPORT-node-hydration.md));
   prefer the filtered "Load: OPEN drift gaps only" query.

The PRD for this system itself is [docs/PRD.md](docs/PRD.md) (yes, `/prd-sync` can audit this
repo against it). The current change round is documented in
[docs/implementation-plan.md](docs/implementation-plan.md); the latest assessment, live metrics,
and next actions are in [docs/scorecard.md](docs/scorecard.md).

**New teammate? Start with [ONBOARDING.md](ONBOARDING.md)** — cold start to live in ~10 minutes.
Full design, shared-deployment guidance, and troubleshooting live in **[setup.md](setup.md)**.

## Two repositories (you need both)

| Repo | Role | Get it |
|---|---|---|
| **arango-solutions-mcp-server** | The MCP server (the `arangodb-memory-mcp` tools: `pattern-search`, `save-pattern`, `embed-*`, AQL, etc.) | `git clone https://github.com/arango-solutions/arango-solutions-mcp.git ~/code/arango-solutions-mcp-server` |
| **arango-shared-memory** (this repo) | Setup/phase scripts, project templates, docs | clone alongside it under `~/code/` |

> **The team runs one shared ArangoDB** (the memory already exists on it). Joining that shared memory
> is the common case — you do **not** stand up your own database or run any schema setup. The
> local/standalone path is the *admin* section below.

## Quick start — join the team's shared memory (the common case)
Prereqs: Python 3.11+ & Poetry, Claude Code and/or Cursor, your own OpenAI API key, and the shared-cluster
credentials (get these from your team lead — never from a repo). Then:
```bash
# 1. Clone both repos under ~/code (see table above); install the server (no virtualenv active):
cd ~/code/arango-solutions-mcp-server
poetry config virtualenvs.in-project true --local && poetry install

# 2. Register the MCP server (id `arangodb-memory-mcp`) in ~/.claude.json AND ~/.cursor/mcp.json,
#    pointing ARANGO_HOSTS at the shared cluster, with your creds + your own OpenAI key.
#    Exact JSON: setup.md STEP 3. Then reload Cursor / restart Claude Code.

# 3. Verify you're connected to the shared memory (should show a non-zero pattern count):
cd ~/code/arango-solutions-mcp-server && poetry run python ~/code/arango-shared-memory/scripts/verify.py

# 4. Bootstrap each project (installs the CURRENT skills/hooks from templates/ — never hand-copy them):
~/code/arango-shared-memory/scripts/bootstrap_project.sh --target ~/code/my-api \
  --project-name "My API" --project-id my-api --project-type web-api --prd-file docs/PRD.md

# Existing bootstrapped projects: preview, then merge-refresh hooks across ~/code.
python3 ~/code/arango-shared-memory/scripts/rollout_cursor_hooks.py
python3 ~/code/arango-shared-memory/scripts/rollout_cursor_hooks.py --apply
```
**Do NOT run `install.py` / `setup_*` / `phase*` against the shared cluster** — those stand up a *new*
backend, not join an existing one. Full walkthrough: **[ONBOARDING.md](ONBOARDING.md)**.

## Admin — stand up a NEW backend
Only when creating a fresh shared memory (a new cluster, or a private local one for solo/offline use).

```bash
cd ~/code/arango-solutions-mcp-server   # no virtualenv active
poetry config virtualenvs.in-project true --local && poetry install

# Local Docker instance — NOTE the --experimental-vector-index flag (required for hybrid/graph):
docker run -d --name shared-memory-arangodb --restart unless-stopped \
  -p 8539:8529 -e ARANGO_ROOT_PASSWORD=openSesame \
  -v shared-memory-arango-data:/var/lib/arangodb3 \
  arangodb/arangodb:latest arangod --experimental-vector-index
# (For a hosted cluster instead: skip Docker; just target its host in the env below.)

# Register the MCP with admin creds + OpenAI key (setup.md STEP 3), reload, then create schema+view:
poetry run python ~/code/arango-shared-memory/scripts/install.py
```
- **Hybrid + graph:** with `OPENAI_API_KEY` set and ≥1 saved pattern, run `phase1b_setup.py` (embeddings +
  vector index) then `phase2_setup.py` (graph edges), or `install.py --with-embeddings`.
- **Existing database?** `scripts/migrate.py` auto-detects what an older `memory` database is
  missing (collections, graph definitions, `memory_type` backfill, edge weights, schema
  validation) and applies only that, non-destructively. `install.py` runs it automatically;
  `--dry-run` previews. Safe to re-run any time.
- **Periodic maintenance:** `scripts/maintain.py` runs every upkeep pass in order (embedding
  backfill, graph rebuild, edge weights, lifecycle, health check). Schedule it once with
  `scripts/install_maintenance_schedule.sh` (weekly launchd job on macOS; prints the cron line
  elsewhere).
- **Provision teammates:** `scripts/add_teammate.py <username>` creates a least-privilege user (rw on
  `memory` only) and prints creds to hand out; `--revoke` offboards. See setup.md "Shared deployment."
- **Going local → shared** is a config change, not a code change (env `ARANGO_HOSTS` + real creds/TLS +
  the vector flag enabled server-side).

`verify.py` (either path) checks connectivity, collections, indexes, a round-trip, and prints the
**adoption + read-path scorecard** (patterns, projects, drift, interactive searches, automatic
recalls, zero-read projects, hit rate). Exit 0 = healthy.

## Repository layout
```
setup.md                       Full design + onboarding (canonical)
docs/PRD.md                    The PRD for this system (REQ-numbered; /prd-sync-auditable)
docs/implementation-plan.md    Current change round: defects, gaps, migration
docs/scorecard.md              System scorecard: grades, live metrics, ranked next actions
scripts/
  install.py                   One-shot: schema + migrate + view + verify (+ optional embeddings/graph)
  setup_schema.py              Collections + indexes + JSON schema validation (idempotent)
  migrate.py                   Auto-detecting, non-destructive migration for existing databases
  maintain.py                  All periodic passes in order (backfill/graph/weights/lifecycle/verify)
  install_maintenance_schedule.sh  Schedule maintain.py (launchd on macOS; cron line elsewhere)
  phase1_setup.py              patterns_search view + graded-scoring fields
  phase1b_setup.py             Embeddings + cosine vector index
  phase2_setup.py              Graph: similarity + provenance edges
  phase2b_extract.py           LLM-extracted edges (gpt-4o; periodic)
  phase3_lifecycle.py          Supersede / TTL / staleness (periodic)
  eval_retrieval.py            Golden-set retrieval eval: recall@k / MRR per mode (bm25,
                               hybrid, hybrid+graph); history in eval_runs. Run after any
                               ranking change — the AQL mirrors the server's
                               (tests/test_eval_aql_sync.py guards the two from drifting).
  verify.py                    Health check + adoption/read-path scorecard
  add_teammate.py              Per-developer least-privilege users (also drives attribution)
  install_visualizer.py        Graph Visualizer theme/queries/canvas actions for memory_graph
  bootstrap_project.sh         Scaffold a project from templates/
  rollout_cursor_hooks.py      Merge-refresh Cursor + shared Claude hooks across local projects
eval/golden_queries.json       The golden query set (grow it as important patterns are saved)
templates/                     Source of truth for CLAUDE.md, Cursor/Claude hooks (session recall,
                               drift queue, capture miner, apply attribution, stop gate), and skills incl.
                               check_evidence.py
tests/                         DB-free test suite: python3 -m unittest discover tests
```

## More
Design, shared-deployment operations, teammate provisioning, and a troubleshooting table:
**[setup.md](setup.md)**. Teammate happy-path: **[ONBOARDING.md](ONBOARDING.md)**.
