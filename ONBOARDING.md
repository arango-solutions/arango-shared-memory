# Onboarding — team shared memory + drift detection for Claude Code / Cursor

Welcome! Our team runs **one shared ArangoDB** so patterns, drift, and project state are visible to
everyone. This gets you connected in ~10 minutes. In every project you opt in you get:

- **`/pattern-search`** — before solving a problem, check solutions teammates already verified.
- **`/pattern-save`** — after solving something reusable, save it for the whole team (memory
  types: pattern, feedback, user, project, reference — feedback keeps corrections/confirmed
  approaches alive across sessions).
- **`/prd-sync`** — audit your code against its PRD; open gaps are tracked automatically, and
  when the code has legitimately outgrown the PRD you get a reviewable PRD patch instead of
  silent divergence.
- **Automatic session digest** — every session starts with your project's open gaps, PRD
  staleness, feedback memories, and top relevant patterns injected for you.
- **Automatic capture candidates** — at session end, a hook mines the transcript for likely
  lessons (a command that failed then succeeded; corrections you gave) and queues them; the next
  session's digest nags until you triage with `/pattern-save` (save the real ones, discard noise).
- **A drift stop gate** — if you edited implementation files (or the PRD) and didn't run
  `/prd-sync`, session end is blocked once with instructions (bypass: `.no-drift-gate` file).

Everything you save, search, or apply is **attributed to your username** — that's why you get your
own account instead of a shared one. Contribution and reuse are visible per person (and yes, that
means your saved patterns get credited to you when teammates reuse them).

Retrieval is hybrid (semantic + keyword) with a graph of related patterns — you don't need to know
that to use it. `setup.md` is the deep reference + troubleshooting; this is the happy path.

> **You connect to the team's shared cluster — you do NOT run your own database, and you do NOT run any
> `install.py`/schema setup.** The shared memory already exists; you just point your MCP client at it.

---

## Prerequisites (install once)
- **Python 3.11+ and Poetry** (`pipx install poetry` if needed).
- **Shared-cluster credentials** — get these **from your team lead / secrets manager** (never from a
  repo or chat). You'll paste them into your *local* MCP config in step 3; they are never committed.
- **Your own OpenAI API key** — for semantic search (keyword-only still works without one).
- **Claude Code and/or Cursor.**
- (Docker is *not* required — that was only for the old local setup.)

## 1. Clone both repos under `~/code/`
```bash
mkdir -p ~/code && cd ~/code
git clone https://github.com/arango-solutions/arango-solutions-mcp.git arango-solutions-mcp-server
git clone https://github.com/arango-solutions/arango-shared-memory.git
```

## 2. Install the MCP server (runs locally, talks to the shared cluster)
```bash
# Run with no virtualenv active, or Poetry installs into that one instead of creating .venv.
cd ~/code/arango-solutions-mcp-server
poetry config virtualenvs.in-project true --local   # put the venv at ./.venv, which step 3 launches
poetry install
```

## 3. Register the MCP server pointing at the shared cluster
Add this under a top-level `"mcpServers"` key in **both** `~/.claude.json` and `~/.cursor/mcp.json`.
Fill in `<you>`, the **credentials you were given out-of-band**, and **your own** OpenAI key:
```json
{
  "arangodb-memory-mcp": {
    "command": "bash",
    "args": ["-c", "cd /Users/<you>/code/arango-solutions-mcp-server && exec .venv/bin/python main.py"],
    "cwd": "/Users/<you>/code/arango-solutions-mcp-server",
    "env": {
      "ARANGO_HOSTS": "https://prod.demo.pilot.arango.ai",
      "ARANGO_ROOT_USERNAME": "<your shared-cluster username>",
      "ARANGO_ROOT_PASSWORD": "<your shared-cluster password — DO NOT COMMIT>",
      "ARANGO_DEFAULT_DB_NAME": "memory",
      "ARANGO_VERIFY_SSL": "true",
      "OPENAI_API_KEY": "sk-...your own key...",
      "EMBEDDING_MODEL": "text-embedding-3-small"
    }
  }
}
```
These files live in your home directory and are **not** in any repo — keep the credentials there only.
Then **reload Cursor / restart Claude Code** so the tools load.

> **A wrong launch line is easy to miss** — the memory tools simply aren't there, because the
> skills fail open (the client's MCP logs show the real error). The line matches the server's
> `main` branch: Poetry, `package-mode = false`, `main.py` at the root. The `pyproject.toml`
> there declares an `arangodb-mcp` script, but Poetry never creates it in that mode, so launch
> `main.py` with the venv's Python. If `.venv/` is missing, see the Troubleshooting table in
> `setup.md`.
>
> If your tools vanish after a `git pull` of the server, check the launch line first.

## 4. Verify you're connected to the shared memory
```bash
cd ~/code/arango-solutions-mcp-server
poetry run python ~/code/arango-shared-memory/scripts/verify.py
```
You should see the shared host, "ALL CHECKS PASSED", and a non-zero pattern count + several registered
projects (that's the shared state — you're in). **Do not run `install.py` or the `setup_*`/`phase*`
scripts against the shared cluster** — they're for standing up a *new* backend, not joining an existing one.

## 5. Turn a project into a dark-factory project
From anything under `~/code/`:
```bash
~/code/arango-shared-memory/scripts/bootstrap_project.sh --target ~/code/my-project \
  --project-name "My Project" --project-id my-project \
  --project-type web-api --prd-file docs/PRD.md
```
Installs `CLAUDE.md`, the hooks (session digest, drift queue, stop gate), the skills, and the
evidence checker (kept in `templates/` — never hand-copy). Use a **unique `--project-id`** (it
namespaces your patterns/drift in the shared store). Add a `PRD.md`, run `/prd-sync` once for a
baseline. Repeat per project. Re-run with `--force` after template updates to pick up new
hooks/skills — anything it actually changes is first backed up as `<file>.pre-update.<timestamp>`,
so local customizations (like your `settings.json` permission entries) are recoverable: re-apply
them from the backup after the re-run.

## 6. Use it
- Starting a non-trivial problem? **`/pattern-search "<what you're stuck on>"`** first.
- Applied one of the results? The skill records it (`pattern-applied`) — that's what makes good
  patterns rank higher and gives the author credit. Don't skip it.
- Solved something reusable? **`/pattern-save`**.
- Digest says capture candidates are queued? Triage them with **`/pattern-save`** (2 minutes).
- Touched implementation files? **`/prd-sync`** at session end (the hook reminds you).

---

## Notes
- **It's shared** — patterns you save are visible to the whole team immediately, and you see theirs.
  Save reusable, non-secret techniques; never put credentials or client-specific data in a pattern.
- **Credentials:** shared-cluster creds and your OpenAI key live *only* in your local MCP config. Never
  commit them; never paste them into a repo, PR, or pattern. `.env` files are gitignored.
- **Stuck?** `setup.md` has a Troubleshooting table (no view, no OpenAI key, MCP server won't start,
  hooks not firing, TLS/auth).
- **Graph Visualizer: look, don't touch.** A known display bug can render canvas nodes as
  empty stubs (Properties shows only `_id`/`_key`) even though the documents are intact —
  and the panel still offers **Save**, which risks overwriting a real document with the
  stub. Never edit documents through the visualizer; use the Collections UI or AQL. Details:
  `docs/visualizer/BUG-REPORT-node-hydration.md`.
