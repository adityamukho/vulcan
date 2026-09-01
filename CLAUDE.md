# Temporal Reasoning — AI Coding Agent Memory

Temporal Reasoning provides persistent bi-temporal graph memory for AI coding agents.

## Quick Start

```bash
# Install dependencies and sync skill (--harness is required: claude-code, opencode, or codex)
python install.py --harness claude-code

# Use in code
from minigraf import query, transact

transact("[[:decision/cache :description \"use Redis\"]]", reason="Caching strategy")
result = query("[:find ?d :where [?e :description ?d]]")
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

Per-commit cost trace: `MINIGRAF_INGEST_TRACE_PATH` (unset by default).

When set, ingestion appends one JSON object per applied commit — timings, work
counters, and per-commit checkpoint deltas — for #260's cost attribution. Unset
means no trace and no file. Commits skipped for extraction failure emit no
record, and neither do commits whose write fails, so a trace is not a commit
census; `stderr_capture.py` counts those.
Read with `evals/at_scale/probe_per_commit_cost.py`.

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
project-minigraf/minigraf#304). minigraf has enforced this since 1.2.2: a second
open raises `Database is already open in this process` instead of silently
succeeding. That makes the bug visible, not absent.

**The floor is `minigraf>=2.0.0,<3.0.0`** as of #284 item 6. The upper bound is
deliberate: with no cap, CI silently resolved 2.0.0 the day it shipped and ran
red for days before anyone connected the two (#286). A major bump must be a
decision, not a resolver outcome. `install.py` mirrors this spec and is
version-aware — pyproject.toml is canonical, and the two have drifted before.

**project-minigraf/minigraf#287 is still OPEN and is worked around here, not
fixed.** Batching facts that share `(entity, attribute, valid_from)` into one
transact silently keeps only the last, because the EAVT pending index omits
value bytes. It is VERSION-INVARIANT — measured identically on 1.2.3 and 2.0.0
— so the upgrade neither helped nor hurt. `:contains`, `:depends-on` and
`:parent` are therefore transacted ONE PER CALL at four sites; never "simplify"
those loops into a single batch. All four are now regression-guarded.

`mcp_server.py` enforces it through `_DbLeaseManager` (#255), which replaced the
old `_db = None` "release the lock" idiom — that global is **deleted**, so
ignore any comment still describing it. The refcount is authoritative: the
handle opens at 0 -> 1 and drops at 1 -> 0, and every acquisition in between
reuses it. Before touching DB lifecycle, read the invariant comment above
`_db_native_lock`. Enforcing the invariant by reusing a handle held only by a
live weakref was tried and rejected — it resurrects dead graphs and segfaults
(#253).

**A lease is cheap in-process and exclusive out-of-process, and the difference
decides design questions.** At count > 0 `try_acquire` joins and returns the
same handle, so a concurrent `call_tool` never blocks. But `try_acquire` returns
None while another PROCESS holds minigraf's lock on the graph file, and BOTH
auto-memory hooks (`hooks/claude-code.json`) are `command` hooks in separate
processes — `finalize_hook.py` takes a lease to write each turn's facts. The
retry budget is `_LOCK_RETRY_MAX` x `_LOCK_RETRY_BASE` doubling = **0.75 s
total**, and both hooks swallow failures (`except Exception: pass`). So
lengthening how long ingestion holds a lease does not block queries — it
**silently discards auto-memory writes**. Do not "just hold one lease for the
whole run".

**Locking is in the kernel; there is no PID sidecar to read.** As of minigraf
2.0.0 the lock is `File::try_lock` on the `.graph` file itself (`flock` on Unix,
`LockFileEx` on Windows), released by the kernel on process exit however it
exits. The old `.graph.lock` PID sidecar is gone, so nothing on disk names the
holder, and `mcp_server`'s stale-lock self-heal is gone with it — measured, it
recovered nothing on either version (see `evals/at_scale/benchmark.md`).

Ingestion's #108 "decline instead of racing" pre-check therefore reads **our
own** advisory hint, `<graph_path>.owner`, written by `_graph_owner_hint_held`
and read by `_graph_owner_hint` — never minigraf's lock. Freshness is a
heartbeat-refreshed mtime (`_OWNER_HINT_TTL`, default 30s, override
`MINIGRAF_OWNER_HINT_TTL`), never PID liveness: no portable mechanism can both
name a holder and avoid contending, and `os.kill(pid, 0)` terminates the target
on Windows. Correctness still rests entirely on minigraf's kernel lock — a
wrong hint costs one race or one needless decline, never correctness. The hint
is published only around LONG-held ownership (ingestion), not every lease.

**Dropping the handle is not free: it runs a full O(graph size) checkpoint**
inside minigraf's `Drop for Inner`, outside `_CheckpointPolicy`'s duty gate and
invisible to the trace's `ckpt_d_seconds`. Ingestion currently drops it ~1.02
times per commit, measured at 48% of write time and growing 3.47x within a
220-commit run (#280). See `evals/at_scale/benchmark.md`, "Per-Commit Cost
Attribution".

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
query("[:find (count ?e) :where [?e :description ?d]]")
```
