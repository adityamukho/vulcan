# Temporal Reasoning — AI Coding Agent Memory

Temporal Reasoning provides persistent bi-temporal graph memory for AI coding agents.

## Quick Start

```bash
# Install dependencies and sync skill (--harness is required: claude-code, opencode, or codex)
python install.py --harness claude-code

# Use in code
from minigraf import query, transact

transact("[[:decision/cache :decision/description \"use Redis\"]]", reason="Caching strategy")
result = query("[:find ?d :where [?e :decision/description ?d]]")
```

## Key Files

- `mcp_server.py` - Persistent MCP server (primary interface)
- `minigraf.py` - Python wrapper for direct use outside MCP
- `SKILL.md` - Skill definition with all query syntax
- `install.py` - Setup script (runs weekly updates)
- `docs/testing-conventions.md` - Real-backend-only test conventions for `tests/test_mcp_server.py`
- `hooks/claude-code.json` - Claude Code MCP + auto-memory hook config
- `evals/at_scale/` - at-scale ingestion + query-correctness benchmark tier (real repo history, observational, see its own `benchmark.md`). It also holds one-off read-only probes (e.g. `probe_dep_preload_exposure.py`, #245) and their recorded measurements under `results/`, which are analysis artifacts rather than part of the recurring benchmark run.

## Graph Storage

Default: `memory.graph` in the current working directory.

Override: `MINIGRAF_GRAPH_PATH=/custom/path python ...`

Memory retrieval index: `<graph_path>.fts.sqlite3` alongside the graph file.

Override: `MINIGRAF_INDEX_PATH=/custom/path`

Ingestion checkpoint budget: `MINIGRAF_INGEST_CHECKPOINT_DUTY` (default `0.05`).

`db.checkpoint()` is full WAL-to-graph compaction, so it costs O(graph size)
regardless of how much was written since the last one. Ingestion holds it to
this fraction of wall clock instead of running it once per commit. Writes are
durable via `<graph_path>.wal` without it; a larger WAL only slows the next
process that opens the graph. See #241.

The fact index is bi-temporal: it includes historical (retracted/superseded) facts
alongside current ones, labeled with their validity window.

**Graph format version — there is no migration, by design.** `GRAPH_FORMAT_VERSION`
(mcp_server.py) is stamped as `:ingestion/format-version` and ingestion refuses to
run against any other version, including an absent stamp (which means the graph
predates it). Bump it whenever a change makes stored facts unreadable by current
code — today that means the ident rule in `_canonical_ident` (#263), since
ingestion recomputes idents from `(type, path, name)` on every run rather than
reading them back, so an old-rule graph read by new-rule code silently FORKS every
entity instead of erroring.

Do not propose a migration for this. The standing decision is that any graph built
before #222 closes gets **rebuilt into a fresh graph path**, never migrated or
re-ingested in place: several #222-arc fixes (#235, #251/#253, #238/#245, phase 2d)
write facts that do not self-heal on a later ingest, so those graphs are condemned
independently of any one bug. Re-running ingestion over an existing file repairs
nothing. See `docs/superpowers/specs/2026-08-14-ident-rule-r3-and-format-version-design.md`.

**Single-handle invariant.** At most one live `MiniGrafDb` handle may exist per
process. Two handles on one file each cache their own `page_count` and corrupt
each other — the flaky `Page N out of bounds (total pages: M)` (#251, #253,
project-minigraf/minigraf#304). minigraf enforces this as of 1.2.2 (hence the
`minigraf>=1.2.3` floor): a second open now raises `Database is already open in
this process` instead of silently succeeding. That makes the bug visible, not
absent — `mcp_server.py` still does not enforce it: `_db =
None` is its "release the lock" idiom, but it only releases when it drops the
LAST reference, and a local `db` on a stack (e.g. `_run_ingestion`'s per-commit
handle, held across awaits) keeps the handle alive while a concurrent
`call_tool`'s `finally` clears the global. Before touching DB lifecycle, read the
invariant comment above `_db_native_lock`. Enforcing it in Python by reusing the
live handle was tried and rejected — it changes handle lifetime and segfaults.

## Claude Code Plugin Publishing

The plugin is published via a stub architecture — `install.py` handles all registration automatically.

**Why a stub:** Claude Code's internal copier (`mc$()`) copies the plugin source tree to a versioned cache. REPO_DIR contains `.venv/` (hundreds of MB), causing the copy to fail silently. `install.py` builds a minimal stub at `~/.claude/plugins/stubs/temporal-reasoning-local/` containing only `.claude-plugin/` and `skills/`, which `mc$()` can copy successfully.

**Five files that must be correct** (all written by `install.py`):

1. `~/.claude/plugins/stubs/…/.claude-plugin/marketplace.json` — must have `owner` field; plugin `source: "./"`
2. `~/.claude/plugins/stubs/…/.claude-plugin/plugin.json` — plugin identity and version
3. `~/.claude/settings.json` — `enabledPlugins` + `extraKnownMarketplaces` → stub dir
4. `~/.claude/plugins/installed_plugins.json` — `installPath` → versioned cache dir
5. `~/.claude/plugins/known_marketplaces.json` — **authoritative store**; `source.path` and `installLocation` → stub dir (settings.json changes don't propagate here automatically)

**Version bumps:** canonical version lives in `.claude-plugin/plugin.json`; `install.py` reads it via `PLUGIN_VERSION`. Stale versioned cache dirs are deleted on each run.

**Diagnosing failures:** `claude plugin list` shows per-plugin status and errors. "Plugin X not found in marketplace Y" means marketplace.json failed validation — check the `owner` field and run `claude plugin validate <stub-dir>`.

**Official directory structure** (from docs): the recommended layout separates marketplace root from plugin subdir, with `source: "./plugins/my-plugin"` in marketplace.json. Our stub uses `source: "./"` (root = plugin), which is non-standard but works. A proper separation would let `mc$()` copy without the stub workaround.

**Offline resilience:** set `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1` to prevent Claude Code from wiping the marketplace cache when a git pull fails.

**Files outside plugin dir:** plugins are copied to cache, so `../relative-paths` break. Use symlinks if the plugin needs to reference files outside its directory.

## Query Examples

```python
# Basic query
query("[:find ?x :where [?e :attr ?x]]")

# With temporal
query("[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Count
query("[:find (count ?e) :where [?e :decision/description ?d]]")
```
