# PRD Sync Skill

## Invocation
`/prd-sync` — audit implementation files against the PRD and write drift gaps to ArangoDB.

## Purpose
Find gaps between what the PRD says the system must do and what is actually implemented —
in BOTH directions: requirements the code doesn't satisfy (drift alerts), and PRD text the
code has legitimately outgrown (PRD patches, applied only with user approval).
Every requirement must have a `file:line` evidence reference or be classified as MISSING/PARTIAL.

---

## Protocol

### Phase 0 — Locate the PRD + staleness check
Read `PRD_FILE` from `AGENTS.md` (the consolidated canonical agent doc), falling back to `CLAUDE.md` if `AGENTS.md` is absent or has no identity block. If not found, search for files matching `*PRD*`, `*requirements*`, `*spec*` in `docs/`. If still not found, ask the user.

Compute the PRD content hash and compare it to the stored one:
```bash
shasum -a 256 <PRD_FILE> | cut -d' ' -f1
```
```
Use tool: execute-aql-query   database_name: "memory"
query: RETURN DOCUMENT("project_registry", @pid).prd_sha256
bind_vars: { "pid": "<PROJECT_ID>" }
```
If they differ (or no hash is stored), note in the report: **"PRD changed since last sync"** —
requirement numbering may have shifted, so re-extract everything rather than assuming prior REQ ids.

**Branch guard — decide the SYNC MODE before anything else writes.** The shared drift
baseline (`prd_sha256`, `drift_alerts`, `prd_patches`) is per-project, **not per-branch**.
Running the write phases against a PRD that differs from the default branch moves the
team's baseline to unmerged content and feeds phantom gaps into every teammate's session
digest. The SOP ("spec changes are their own PR, merged first") is enforced *here*, at the
write boundary, so it does not depend on anyone remembering it:

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "no-git")
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "no-git")
# default branch: origin/HEAD if set, else main, else master
DEFAULT=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|origin/||')
[ -z "$DEFAULT" ] && { git show-ref -q refs/remotes/origin/main && DEFAULT=main || DEFAULT=master; }
git fetch origin --quiet 2>/dev/null || true          # best effort; do not block on network
git diff --quiet "origin/${DEFAULT}" -- "<PRD_FILE>" 2>/dev/null; PRD_DIFFERS=$?
```

Decide the mode (record `BRANCH`, `COMMIT`, and the mode in the report header):
- **No `origin` remote at all** → `SHARED`. A purely local repo has nothing to diverge from.
- **PRD identical to `origin/${DEFAULT}`** (`PRD_DIFFERS` = 0) → `SHARED`, on any branch:
  the audit runs against the agreed contract, which is exactly what a feature branch wants.
- **PRD differs from `origin/${DEFAULT}`** (`PRD_DIFFERS` = 1) → `LOCAL-ONLY`.
- **Comparison impossible** (`PRD_DIFFERS` > 1: no such ref, fetch failed and no local
  copy of the default ref) → `LOCAL-ONLY`, and say why. When the hazard cannot be ruled
  out, shared state stays untouched — fail closed on writes, never on the audit itself.

**`LOCAL-ONLY` mode:** Phases 1–3 (extraction, audit, evidence gate, drift report) run in
full. **Skip Phases 4, 4b, 4c and the registry update entirely**, and **do not accept PRD
patches in Phase 6a** (accepting one edits the PRD outside a spec PR). Phase 6c may still
clear the queue — the audit happened. Emit prominently:
`[PRD-SYNC] LOCAL-ONLY: PRD differs from origin/${DEFAULT} (branch: ${BRANCH}) — shared
baseline untouched. Merge the spec PR, then re-run /prd-sync to publish.`

### Phase 1 — Extract requirements (+ consume prior observations)
First pull this project's unprocessed observations — discoveries from earlier audits that were
recorded but not yet acted on. Use them as hints (ambiguities already found, edge cases already
hit) instead of re-deriving them:
```
Use tool: execute-aql-query   database_name: "memory"
query: FOR o IN sync_observations
         FILTER o.project_id == @pid AND o.state == "unprocessed"
         SORT o.created_at RETURN o
bind_vars: { "pid": "<PROJECT_ID>" }
```
Mark each observation you actually use as acknowledged. Use `upsert-document`, **not** a
raw AQL `UPDATE` — mutating AQL is refused by the policy layer (see the mutation-gate note
in Phase 4):

```
Use tool: upsert-document   collection_name: "sync_observations"
search_fields: { "_key": "<observation _key>" }
document_data: { "_key": "<observation _key>", "project_id": "<PROJECT_ID>",
                 "state": "acknowledged" }
update_data:   { "state": "acknowledged", "acknowledged_at": "<ISO timestamp>",
                 "resolution": "<what closed it, with file:line where relevant>" }
```

Skip silently if the collection doesn't exist (backend not yet migrated).

Then parse the PRD and extract every distinct, testable requirement. A requirement is any statement that describes what the system MUST, SHOULD, or SHALL do.

Number them: `REQ-001`, `REQ-002`, etc.

Output a table:
```
REQ-001 | The system must authenticate users via JWT | PENDING
REQ-002 | The API must return 400 for missing fields | PENDING
...
```

### Phase 2 — Audit implementation

For each requirement, search the implementation (src/, lib/, app/, api/ — wherever code lives):

```bash
grep -rn "<key term from requirement>" src/ lib/ app/ api/ 2>/dev/null | head -20
```

Classify each requirement:
- **IMPLEMENTED** — found in implementation code with `file:line` evidence
- **TEST-ONLY** — found only in test files (`*.test.*`, `*.spec.*`, `*_test.*`)
- **PARTIAL** — some parts implemented, others missing
- **MISSING** — no evidence found anywhere
- **SKIP** — infrastructure/deployment requirement, not verifiable in code
- **OUTDATED-PRD** — the code deliberately and legitimately diverges: the requirement is
  obsolete, imprecise, or the implementation is a documented improvement. This is drift in the
  PRD, not the code — it produces a PRD *patch* (Phase 4b), never a drift alert. Use sparingly
  and only with evidence; "we didn't get to it" is MISSING, not OUTDATED-PRD.

**Never mark IMPLEMENTED without a file:line reference.**

### Phase 2.5 — Verify evidence mechanically (the confabulation gate)

Before writing the report, verify every `file:line` citation with the checker script — a claim
that cannot be mechanically confirmed must not be persisted as IMPLEMENTED:

```bash
python3 .claude/skills/prd-sync/check_evidence.py <<'EOF'
{"claims": [
  {"req_id": "REQ-001", "classification": "IMPLEMENTED",
   "evidence": ["src/auth/jwt.ts:42"], "term": "jwt"},
  {"req_id": "REQ-007", "classification": "PARTIAL",
   "evidence": ["src/api/users.ts:89"]}
]}
EOF
```

It verifies each cited file exists, each cited line is in range, and (when `term` is given) the
term appears near the cited line. **Any IMPLEMENTED claim with a failed verdict is downgraded to
PARTIAL** with gap `evidence unverifiable: <reason>`. Exit code 1 means at least one claim failed
— fix the classifications before Phase 3. This gate is not overridable.

### Phase 3 — Drift report

Emit a structured report:

```
[PRD-SYNC] Drift Report — <project> — <date>

SUMMARY: X implemented | Y partial | Z missing | W test-only | V skip

IMPLEMENTED (X):
  REQ-001 src/auth/jwt.ts:42 — JWT validation middleware
  ...

PARTIAL (Y):
  REQ-007 src/api/users.ts:89 — POST /users exists but missing input validation
  Gap: field validation not present

MISSING (Z):
  REQ-012 — Rate limiting on all endpoints
  REQ-015 — Audit log for admin actions

TEST-ONLY (W):
  REQ-009 tests/auth.test.ts:33 — "should reject expired tokens" (test exists, impl missing)

OUTDATED-PRD (U):
  REQ-018 src/queue/redis.ts:12 — PRD mandates RabbitMQ; implementation moved to Redis Streams
  Proposed patch: <one-line summary; full patch persisted in Phase 4b>
```

### Phase 4 — Write to ArangoDB (skip if MCP unavailable; **SHARED mode only** — in LOCAL-ONLY skip 4/4b/4c and the registry update)

Every write below carries `branch` and `commit` from Phase 0. This is the provenance that
makes branch-era writes auditable later (and lets a future pass verify that a closing
commit actually reached the default branch before treating the close as final).

For each MISSING or PARTIAL requirement, write a drift alert with **`save-drift-alert`**
(NOT a raw `upsert-document` into `drift_alerts`). This tool upserts the alert AND
links it to its project node via an `alert_from_project` edge, so drift alerts and
their projects never become orphan nodes in the memory graph:

```
Use tool: save-drift-alert
project_id: "<PROJECT_ID>"
req_id: "<REQ-NNN>"
requirement: "<requirement text>"
classification: "MISSING" | "PARTIAL"
status: "open"
evidence: "<file:line or empty>"
gap_description: "<what is missing>"
detected_at: "<ISO timestamp>"
branch: "<BRANCH from Phase 0>"
commit: "<COMMIT from Phase 0>"
```

It is idempotent on `<PROJECT_ID>_<REQ_ID>`: identity (`project_id`/`req_id`) is
preserved and only the fields you pass are merged on re-detection, so re-running
`/prd-sync` keeps each alert's identity and its provenance edge.

For each IMPLEMENTED requirement where a previous alert was open, close it with the
same tool (pass `status: "closed"` and the closing evidence):

```
Use tool: save-drift-alert
project_id: "<PROJECT_ID>"
req_id: "<REQ-NNN>"
status: "closed"
closed_at: "<ISO timestamp>"
closed_evidence: "<file:line>"
branch: "<BRANCH from Phase 0>"
commit: "<COMMIT from Phase 0>"
```

> **Mutation gate — this will bite on a sync that finds real gaps.** The MCP policy
> layer classifies every call. Tools declared `confirmation: none` (`upsert-document`,
> `update-document`) run freely. Tools declared `conditional` — **`save-drift-alert`**
> and `execute-aql-query` — demand a short-lived `confirmation_token` the moment the
> call is classified as a mutation. That token is minted out-of-band by a human
> (`scripts/mint_confirmation.py`, signed with `MCP_CONFIRMATION_SECRET`); an agent
> cannot mint its own. If that secret is not configured on the server, the gate fails
> **closed** with `confirmation_not_configured`. When that happens, fall back to
> `upsert-document` into `drift_alerts` and say so in the report — the alert will lack
> its `alert_from_project` provenance edge until `phase2_setup.py` next runs. Never
> write drift results with a raw AQL `INSERT`/`UPDATE`; the same gate refuses them.

> If your MCP server predates `save-drift-alert`, reload it; as a last resort you
> can still `upsert-document` into `drift_alerts`, but that leaves the alert an
> orphan until `phase2_setup.py` next runs.

### Phase 4b — Persist PRD patches (reverse drift)

For each OUTDATED-PRD finding, write a **proposed** patch (never applied here — Phase 6):

```
Use tool: upsert-document
collection_name: "prd_patches"
search_fields: { "_key": "<PROJECT_ID>_<REQ_ID>_<YYYYMMDD>" }
document_data: {
  "_key": "<PROJECT_ID>_<REQ_ID>_<YYYYMMDD>",
  "project_id": "<PROJECT_ID>",
  "req_id": "<REQ-NNN>",
  "delta_type": "missing-semantics" | "wrong-signature" | "typo" | "obsolete" | "clarification" | "new-requirement",
  "observed": "<what the code actually does, with file:line>",
  "proposed_patch": "<the exact replacement/additional PRD text>",
  "justification": "<why the PRD, not the code, should change>",
  "review_state": "proposed",
  "created_at": "<ISO timestamp>",
  "branch": "<BRANCH from Phase 0>",
  "commit": "<COMMIT from Phase 0>"
}
update_data: { "observed": "<...>", "proposed_patch": "<...>", "justification": "<...>",
               "branch": "<BRANCH from Phase 0>", "commit": "<COMMIT from Phase 0>" }
```
Re-detection merges into the same key; a patch already `accepted`/`rejected`/`superseded` is
never flipped back to `proposed` — create a new dated key if the situation genuinely changed.

### Phase 4c — Persist observations (learning survives rejection)

Findings that become neither an alert nor a patch still carry information: PRD ambiguities,
edge cases discovered while grepping, deprecation signals, and any patch the user rejects in
Phase 6. Append each to `sync_observations` so the next audit starts from them (Phase 1)
instead of rediscovering:

```
Use tool: upsert-document
collection_name: "sync_observations"
search_fields: { "_key": "<PROJECT_ID>_<YYYYMMDD_HHMMSS>_<n>" }
document_data: {
  "_key": "<PROJECT_ID>_<YYYYMMDD_HHMMSS>_<n>",
  "project_id": "<PROJECT_ID>",
  "req_id": "<REQ-NNN or null>",
  "observation_type": "spec_gap" | "assumption_violation" | "precision_needed" | "edge_case" | "cross_layer_invariant" | "design_alternative" | "deprecation_signal",
  "summary": "<one line>",
  "detail": "<enough context to act on next audit>",
  "severity": "low" | "medium" | "high",
  "state": "unprocessed",
  "source": "prd-sync",
  "created_at": "<ISO timestamp>",
  "branch": "<BRANCH from Phase 0>",
  "commit": "<COMMIT from Phase 0>"
}
```

Update the project registry (now including the PRD hash from Phase 0, which also powers the
session-start staleness check):

```
Use tool: upsert-document
collection_name: "project_registry"
search_fields: { "_key": "<PROJECT_ID>" }
document_data: {
  "_key": "<PROJECT_ID>",
  "project_id": "<PROJECT_ID>",
  "project_name": "<PROJECT_NAME from AGENTS.md>",
  "prd_path": "<PRD_FILE>",
  "last_sync": "<ISO timestamp>",
  "open_gaps": <count of MISSING + PARTIAL>,
  "prd_sha256": "<hash from Phase 0>",
  "prd_checked_at": "<ISO timestamp>"
}
update_data: {
  "last_sync": "<ISO timestamp>",
  "open_gaps": <count of MISSING + PARTIAL>,
  "prd_sha256": "<hash from Phase 0>",
  "prd_checked_at": "<ISO timestamp>",
  "last_sync_branch": "<BRANCH from Phase 0>",
  "last_sync_commit": "<COMMIT from Phase 0>"
}
```

If MCP is unavailable: emit `[PRD-SYNC] ArangoDB unavailable — drift report is local only.` and continue.

### Phase 5 — Drift queue: do NOT clear yet (moved to Phase 6c)

Accepting a patch in Phase 6a **edits the PRD**, and the PostToolUse hook
(`.claude/hooks/drift_queue.py`) enqueues a `prd_*` marker for every such edit. Clearing
here therefore re-arms the Stop gate the moment the sync finishes, demanding a fresh
`/prd-sync` that has nothing new to audit — structurally guaranteed whenever a sync accepts
at least one patch. Moved to Phase 6c on 2026-08-12 after exactly this loop was observed in
`domyn-gdelt`: 8 accepted-patch edits, queue cleared in Phase 5, 8 markers re-queued, gate
fired again with zero code change.

### Phase 6 — Review PRD patches + propose fixes

**6a — PRD patch review (user decision required).** Present each `proposed` patch: the
requirement, the observed divergence, the proposed PRD text, the justification. For each:
- **Accept** → edit the PRD file applying the patch, then update the patch document:
  `review_state: "accepted"`, `applied_at: <ISO>`. Re-run the Phase 0 hash and store it.
- **Reject** → `review_state: "rejected"`, and record the rejection as a `sync_observations`
  entry (Phase 4c) so the learning survives — the next audit sees why it was rejected.
- **Defer** → leave `proposed`.

**PRD patches are NEVER auto-applied.** If the session is unattended (no user response), the
default is: leave every patch `proposed`, write the observation, continue — never stall the
audit waiting, and never apply.

**6b — Fix proposals (optional).** For each MISSING requirement, propose a concrete
implementation: which file, what function/class/middleware, any dependency changes.
Do not implement without user confirmation.

**6c — Clear the drift queue + stamp the audit point (must be the last step).** Only after
every accepted patch has been written to disk:

```bash
python3 .claude/hooks/reconcile_drift_queue.py --mark-synced
```

One command clears the queue markers and then stamps the audit point, so the order cannot
be got wrong. It prints what it cleared and what it stamped; a non-zero exit means it
failed, and the gate will keep firing. Do not fall back to `rm` — see below.

The stamp records **the working tree this audit read** — a git *tree* of every tracked,
modified and untracked file (`.gitignore` honoured), built in a scratch index so the real
index is never touched. The Stop-time reconciler compares the working tree against it by
*content*: committing audited work stays quiet, editing a file after the audit is caught,
and reverting an edit to its audited content is correctly not drift. `.last-sync` is a
**dotfile deliberately**, so `ls` never counts it and the clear step skips it.

> **Why a tree, not `git rev-parse HEAD`** (the stamp before 2026-09-30). The audit reads
> the working tree, but a commit stamp recorded HEAD. Any audited work still uncommitted
> therefore always differed from the stamp: the gate re-queued it the moment the sync
> finished, and re-running the sync re-stamped the same HEAD — an endless loop on zero
> change, hitting every repo that audits before committing (the normal order). Legacy
> commit stamps are still read, and resolve to that commit's tree.
>
> **Why not `rm -f .prd-drift-queue/*`.** A glob whose base the shell cannot resolve
> statically — for instance after a `cd` — is exactly what agent command-safety checks
> refuse, which left this step unrunnable in some environments. `--mark-synced` deletes
> only regular, non-dot files directly inside the queue directory.

If the sync is abandoned before this point the queue survives **by design** — the audit did
not finish, so the gate should still fire. Do not clear it early to silence the gate.

> **Why the reconciler exists.** The PostToolUse hook fires on `Write|Edit` and reads
> `tool_input.file_path`. A Bash call carries none, so a `cat > f <<EOF` heredoc, `sed -i`,
> a generated script, or an edit made outside the session queues **nothing** — and this gate
> only counts markers. Agents are actively steered toward the shell by tool-preference
> settings, so that is the common path, not an edge case. The reconciler asks git what
> actually changed since the audit instead of trying to parse shell.

---

## Key invariants
- Never claim IMPLEMENTED without `file:line` evidence. A test is not an implementation.
- Evidence must pass `check_evidence.py` (Phase 2.5) before it is persisted — no exceptions,
  no override flag.
- Never skip Phase 2 — even if you believe the code is aligned, grep it.
- OUTDATED-PRD produces a proposed patch, never a silent absorption of the divergence and
  never an unapproved PRD edit.
- If the PRD itself is ambiguous, note the ambiguity in the drift report AND record it as a
  `sync_observations` entry, but do not block the audit.
- Unattended sessions take the recorded defaults (leave proposed, record, continue) — they
  never stall and never auto-apply.
