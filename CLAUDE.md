# Temporal Reasoning — AI Coding Agent Memory

Temporal Reasoning provides persistent bi-temporal graph memory for AI coding agents.

## Quick Start

```bash
# Install dependencies and sync skill (--harness is required: claude-code, opencode, or codex)
python install.py --harness claude-code
```

Everything else goes through the MCP tools — there is no wrapper module:

```
minigraf_transact(facts='[[:decision/cache :description "use Redis"]]',
                  reason="Caching strategy")
minigraf_query(datalog='[:find ?d :where [?e :description ?d]]')
```

`from minigraf import query, transact` has never worked. The installed
`minigraf` package exports `MiniGrafDb`, `MiniGrafError` and `minigraf_ffi`;
there is no `minigraf.py` in this repo and `git log --all --diff-filter=A`
shows there never was. To read a graph outside the server, open a
`MiniGrafDb` directly — subject to the single-handle invariant below.

## Key Files

- `mcp_server.py` - Persistent MCP server (the only runtime interface to the graph)
- `fact_index.py` - SQLite FTS5 fact index; retrieval, and the graph's only independent witness (#302)
- `frontier_registry.py` - Per-position claim registry for the two ingestion streams
- `SKILL.md` - Skill definition with all query syntax. Every example is an MCP
  tool call with named arguments, checked against `mcp_server._TOOLS` by
  `tests/test_skill_doc.py` (#322) — nothing executes those examples, so the
  dead `from minigraf import query, transact` form survived in them for months
- `skill.json` + `tools/*.json` - Portable manifest, generated from `mcp_server._TOOLS` and guarded by `tests/test_tool_schemas.py`
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

**The index is also the graph's only independent witness (#302).** It is written
from the same triples in the same transaction boundary but by a different
storage engine, so `evals/at_scale/fact_audit.py` can ask what the graph has
stopped producing. That gap is real: garbling one fact page cost ~11% of a
measured graph with **zero bytes on stderr and zero `error_signals`**, so
`stderr_capture.py` — the tier's other detector — reported it clean. The
at-scale gate now fails on any divergence, with NO tolerance, because a clean
audit diverges by exactly zero — verified on the 822-commit at-scale graph, at
a cost of 0.8s. Two normalizations buy that exactness and both are
load-bearing: index entities are mapped forward into UUID space the way
minigraf derives them (`uuid5(NAMESPACE_OID, ":the/ident")`), and graph values
are rendered back into the datalog text the index stored (`_index_text`:
minigraf returns `1`, the index holds `'1'`).

**The audit also found that boolean-valued facts were never indexed, and #303
fixed it.** `_FACTS_TRIPLE_PATTERN` accepted a quoted string, keyword, number
or `#uuid`/`#inst` literal as a value but not a bare `true`/`false`, so
`[:function/f :static true]` reached the graph and never the index — 83 facts
on the at-scale graph, all `:static`, invisible to memory retrieval as well as
to the audit. The pattern now has a `true|false` alternative, and the audit's
`unindexed_boolean_facts` key is **deleted, not zeroed**: a key that always
reports 0 reads like a covered case while covering nothing, and while the
exclusion stood a `:static` fact the graph had genuinely LOST was
indistinguishable from one the index could never hold.

Two things the fix depends on. The index stores the **EDN spelling**, lowercase
`true`/`false` — the datalog text it was transacted from, the same rule every
other value type follows — and `fact_audit._index_text` renders minigraf's
Python `True` back into it. That test is on the Python **type**, never the
text, so a fact whose value is genuinely the string `"True"` keeps its
capitalization on both sides and stays auditable. No graph format bump: stored
facts stay readable, the index simply gains rows going forward. Existing graphs
are not repaired — index rows are written at transact time — so a graph that
needs `:static` searchable gets rebuilt, per the standing decision below.

**`nil` is not a storable value, and #306 is the decision that settled it.**
The one remaining bare EDN literal outside the pattern was `nil`, and #303 left
it there deliberately rather than closing it speculatively. It was NOT then
fixed the way `true`/`false` was. Indexing it would store a row whose value is
the text `'nil'` — "no value" occupying a slot in a lexical retrieval index and
answering a search for "nil" — and it would have to teach
`fact_audit._index_text` a `None` case in the same commit, because minigraf
returns `None` and `str(None)` is `'None'`, not `'nil'`. A pattern-only fix
trades one divergence for another.

So `handle_minigraf_transact` REFUSES a nil-valued triple, before the graph is
touched (`_has_nil_valued_triple`, mcp_server.py). `_FACTS_TRIPLE_PATTERN` and
`_index_text` are unchanged. The at-scale audit gate stays honest as a result: a
nil fact found in a graph is now a genuine write-path defect, not the index
being blamed for what it cannot hold. Rejection is all-or-nothing for the
block — a partial write the caller cannot detect from `ok:True` is worse than
a refusal.

Two things the guard depends on. It blanks quoted strings before scanning, and
that is load-bearing, not tidiness: a note written ABOUT this defect carries the
offending triple as prose, so a raw scan would refuse a legitimate write.
`_FACTS_TRIPLE_PATTERN` shares that blind spot but pays only a spurious index
row for it; here the cost is a rejected write. And the trailing `\]` is what
makes the unanchored `nil` safe, exactly as for `true`/`false` —
`[:d/x :note nilpotent]` does not match. Both are ablation-proven, not merely
asserted.

The guard is on the public handler only; the internal `_transact` (ingestion)
is unguarded, and deliberately so — ingestion emits no `nil`, and if it ever
did, a red gate is then the correct signal rather than a misattribution.
Existing graphs are not repaired either way.

**The same scan also answers a graph-only question: #287's two-value
`:introduced-by`.** `fact_audit`'s full `[:find ?e ?a ?v]` scan is already in
memory, so `evals/at_scale/introduced_by_audit.py` reads #235's corruption off
it for one pass over a dict — no second query, no second scan, no second lease.
It is reported under its own key, `introduced_by_duplicates`, and deliberately
NOT folded into `divergence`, because it is not a two-witness finding: both
values are faithfully in the index too, so the two witnesses agree perfectly
about a graph that is wrong. **A run can read `divergence | 0` and still be
condemned**, which is exactly why the report carries a separate row. The check
is also narrowed to `:introduced-by` specifically — `:contains`, `:modified-in`
and `:depends-on` are legitimately multi-valued, so a detector keyed on "this
entity has two values for some attribute" would report every module in every
healthy graph. It reports entities, not surplus facts, and the sample names
each one by its own `:ident` fact, already in the same scan.

**The at-scale gate fails on any nonzero count, with NO tolerance, and that
was measured before it was wired.** The 831-commit at-scale graph carries 3150
`:introduced-by` facts across 3150 entities, **every one of them holding
exactly one** — so a clean graph is zero, not "small", and the count needed no
threshold. The positive control matters as much as the zero: a check that
scanned no `:introduced-by` facts at all would also report 0, so the
distribution was read before the gate was believed. Cost is unchanged at 0.84s
for the whole audit, because this rides the scan rather than paying for one.
An absent `introduced_by_duplicates` key (a metrics file from a harness that
had the fact audit but not this check) stays clean — it cannot be
retro-audited, so it is not retro-failed.

**The OPPOSITE defect needed its own detector, and #316 is it.** An entity
holding ZERO `:introduced-by` is what an interrupted run leaves behind (#313),
and every gate the harness had read it clean — `introduced_by_duplicates` skips
anything with fewer than two values; `divergence` compares two witnesses that
are missing exactly the same fact, because neither was ever written;
`stderr_capture` had nothing to read; and `probe_provisional_residue`'s
`M <= N` reads clean MORE comfortably when the defect is present, because a
torn entity raises N without ever raising M. So
`entities_without_introduced_by` (introduced_by_audit.py) rides the same scan
and reports under its own key, gated as clause 7 with no tolerance. Measured
before wiring: **0 orphans of 3277 live code entities** on the 845-commit
at-scale graph, whole audit 0.93s against 0.84s before.

Two things it depends on. `CODE_ENTITY_TYPES` is the five types
`_build_code_triples` writes an `:introduced-by` for —
module/function/class/variable/field — and **`:type/external-dependency` is
excluded deliberately**: unresolved-import stubs are opened with exactly
`:entity-type`/`:ident`/`:description` and the lineage machinery can never give
them one later (`_reverse_apply`'s `candidate_idents` comes from
`_build_code_triples`, which never yields a stub), so all **72 of 72** on the
at-scale graph legitimately hold none — including the type would have made the
gate permanently red on its first run. And liveness is `:ident` PLUS the
`:entity-type` FACT, never the ident prefix, because those same stubs carry a
`:module/...` ident while being `:type/external-dependency`.

The result carries its own denominator, `code_entities_scanned`, and that is
not decoration: a check that matched no code entities would also report 0, so
shipping the denominator makes every future run re-prove the positive control
instead of it being a fact about the day the gate was wired. The report row
renders `0 of 3277`, never a bare `0`; a run whose denominator is itself 0
renders as "proved nothing about it" and does NOT fail the gate — a graph
holding no code is not a defect. An absent key stays clean, same precedent.

The one false positive it cannot rule out: `module`/`function`/`class`/
`variable`/`field` are registered in `MINIGRAF_SCHEMA` with `:introduced-by`
optional, so `handle_minigraf_transact` accepts `[:module/foo :description
"x"]` and produces a legitimately lineage-free `:type/module`. The at-scale
gate runs on a fresh ingestion-only graph so it cannot fire there; on a mixed
graph the row is informational. The graph carries no discriminator between an
ingested and a memory-written code entity.

**What no fact-level check can see is a whole COMMIT going missing, and #317
is the census that does.** Graph and index are consistent about the absence,
so `divergence` reads 0 again; the two `:introduced-by` checks are
well-formedness checks on entities that EXIST, and a commit that produced no
entities produces nothing for either; `stderr_capture` has nothing to read;
and `_exit_code`'s `graph_facts == 0` clause only fires when the graph reads
back COMPLETELY empty — one commit in 847 is not zero facts. So
`evals/at_scale/commit_census.py` compares THREE numbers, not one delta, and
is wired beside the audit rather than inside it (it needs a repo handle
`fact_audit` deliberately does not take): `git rev-list --count <branch>`,
`_ingest_progress["processed"]`, and `_count_commit_entities`. **`walk_vs_graph`
catches a commit walked and then lost; `repo_vs_walk` catches one NEVER
WALKED** — the case no in-process counter can see, because the counter and the
walk share the bug. Gated as clause 8.

The clean difference **is** zero, which was the open question: **847 = 847 =
847** on the 847-commit at-scale graph (`results/317-commit-census.json`), so
the gate is zero-tolerance. Predicted from the code, then confirmed — shipping
it as zero without measuring would have repeated the `:type/external-dependency`
trap. The five hazards the issue required measuring all resolved clean: merges
count on both sides (`build_linearization` and `rev-list` are the same set, and
both apply functions write the `:type/commit` triple FIRST, before any file is
looked at, so path-ignore changes nothing either); an extraction-skipped commit
raises the walk count without raising the graph's, and is already failed by the
`skipped_commits` clause. **`repo_commits` ships as the census's own
denominator** — three counts that are all zero also agree, so an empty repo
reports `proved_nothing` and is NOT failed. **An incomplete run is not failed
for walking fewer**: `repo_vs_walk` is gated only when `final_status` is
`complete`, while `walk_vs_graph` is gated always.

Two things it depends on. The ref is **the resolved branch, never `HEAD`** —
`_run_ingestion`'s own `repo_total` was hardcoded to `HEAD` while ingestion
takes a `branch` argument, live in the very run that measured this (the harness
resolves `master` while the checkout sits on a feature branch); fixed at source.
And **`_count_commit_entities` now uses `count-distinct`, not `count`**:
`(count ?e)` counts matching ROWS, so one duplicated commit entity would CANCEL
one genuinely lost commit and the census would read clean on a graph that lost
history. That duplicate is NOT reachable from ingestion (the commit triples are
transacted at `commit_ts_iso`, so a #313 re-walk rewrites the identical triple
at the identical valid-from and it collapses); it is reachable from the public
handler, since `commit` is a registered `MINIGRAF_SCHEMA` type and
`handle_minigraf_transact` writes `:entity-type` at wall-clock valid-from.
`_STATUS_QUERY` keeps `count` deliberately — it is a latency instrument whose
query is frozen for cross-run comparability, and its number is never read as a
count. An absent `commit_census` key stays clean, same precedent as the rest.

**Re-running ingestion repairs none of it, and that is mechanical, not policy.**
A finished run parks `:ingestion/correction-sweep-through` at frontier-high's
own `:hi-hash`, so the next run's `_correction_sweep_select_position` computes
`pos = through + 1 > ceiling_pos` and returns None on its FIRST call —
`_correction_sweep_apply`, which owns the repair, then runs zero times (measured
during #235: watermark at position 13 of 13, one select call, zero apply calls).
Later commits raise the ceiling but never lower the resume point, so a position
already passed is not revisited either. The sweep heals only what it reaches
while a run is still climbing. `_entity_introduced_by_query`'s docstring and its
stderr warning both used to claim the repair unconditionally, which is false for
every reader holding a finished graph — the one state in which anyone consults
it — and #287 corrected both. An affected graph gets **rebuilt into a fresh
graph path**, like every other condemned graph here.

**A commit's write is a SEQUENCE of transacts, not an atomic unit, and #313 is
what that costs on resume.** `_reverse_apply` writes the commit entity and the
structural facts first, the provisional `:introduced-by` several calls later,
and `_frontier_persist_claim` last of all. A process killed inside that window
leaves an entity TORN: live `:ident`, no `:introduced-by`, no lineage marker —
and because the claim never persisted, the resumed run re-walks that very
position and meets its own wreckage.

Reading live-but-unintroduced as *authoritative* was the defect. The
already-authoritative branch only ever considers `:modified-in`, so the entity
kept no lineage at all; `_correction_sweep_apply` could not repair it either,
because its case 3 reads zero `:introduced-by` values as ambiguous and fail-safe
skips. The run then reported `status: complete` with **zero bytes on stderr**.
Neither at-scale detector saw it: `fact_audit`'s two witnesses agree perfectly
(the index is missing exactly what the graph is missing, so `divergence` reads
0), `stderr_capture` has nothing to read, and `introduced_by_audit` looks for
entities with TWO values where this one has none. `_reverse_apply` now treats a
live entity holding no `:introduced-by` as newly discovered — it is re-applying
the commit whose interrupted write created it, so the guess it writes is the one
an uninterrupted run would have.

**Measure this class over many interrupts, never one.** Rate was 3 of 14
interrupted resumes on an 80-commit linear repo (SIGKILL at ~43 of 80), 0 of 5
uninterrupted — the first five interrupted trials of the original report passed
and briefly read as "resume is clean". The two regression tests are
deterministic instead: they produce the tear through the REAL write path, by
killing the lineage batch mid-`_reverse_apply`, so nothing depends on hitting a
timing window.

A harness that SIGKILLs ingestion must kill the **process group**, not the PID.
`_run_ingestion`'s spawn-context `ProcessPoolExecutor` leaves ~9 orphaned
`spawn_main` interpreters per kill (~66 MB RSS each), reparented to init and
blocked forever on a queue whose write end they hold open themselves. They do
not exit on their own; 34 trials of that exhausts 15 GB.

**R3's zero ident collisions is MEASURED, never proven — and #267 is what keeps
measuring it.** `_canonical_ident`'s rule (keep `_`, drop the hyphen-run
collapse) was chosen over R4's hash suffix in full knowledge that a contrived
path/name pair can still collide: `a/b.py` and `a-b.py` both reach
`:module/a-b-py`. Three guards, answering DIFFERENT questions — never treat one
as covering the others:

  * `TestIdentCollisionRegression263` (in the suite) — the 9 measured pairs still
    separate. A FIXED corpus: catches a regression in the rule, discovers nothing.
  * `evals/at_scale/probe_ident_collision_new_history.py` (#267) — censuses FULL
    history against the LIVE `_code_ident`. The only one that can discover a new
    collision. Runs in the at-scale nightly with `--fail-on-collision`; ~97s over
    835 commits, measured clean (3692 inputs, 3692 idents, 0 offenders).
  * `evals/at_scale/probe_ident_collision_census.py` — **FROZEN at the pre-#263
    rule** and NOT a guard on the shipped rule. It reproduces the audit that chose
    R3, whose predictions were pre-registered against the old baseline. Never
    re-point it at production, and never merge the two: each file's tests assert
    its own baseline in both directions precisely so they cannot drift together.

There is deliberately **no `--since` bound** on the new-history census. A collision
is a property of a PAIR, and the pair to fear is a new entity against an OLD one, so
a bounded collection sees only new-vs-new and reports clean while missing the case it
exists for. A red census step in the nightly is **not** a harness failure — it means
history produced two entities sharing one ident, and #263's rule choice is reopened.

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

**What ships in the wheel is a hand-maintained list, and it has been wrong.**
`[tool.setuptools] py-modules` (pyproject.toml) names the top-level modules
setuptools packages; the repo is a flat layout with no package directory, so
nothing is discovered automatically. `frontier_registry.py` landed 2026-07-24
and was imported from `mcp_server.py` without being added — the whole suite
stayed green (the repo root is on `sys.path` in every checkout and every CI
run) and a built wheel died at `import mcp_server` with `ModuleNotFoundError`.
It surfaced only when #82 ran `uvx temporal-reasoning` for real.
`tests/test_packaging.py` now walks the shipped modules' imports transitively
and fails if the list does not cover them.

**PyPI metadata is stamped at RELEASE time, so a dependency cap that is not
released does not exist.** 0.6.0 (2026-07-22) is on PyPI with uncapped
`mcp>=1.27.0` and `minigraf>=1.2.1`; the `mcp<2.0.0` cap landed 2026-08-03 in
`4f630c9`. So every `uvx temporal-reasoning` between those dates resolved mcp
2.x and crashed at `@server.list_tools()` — the exact failure the cap exists to
prevent — while `pyproject.toml` on master read as correct. `install.py` writes
a `.mcp.json` pointing at the PyPI package, so this broke the full install as
much as the MCP-only one. After changing a dependency bound, cut a release or
the bound protects only developers.

**`serverInfo.version` is a two-source ladder, and `0.0.0` counts as absent
(#312).** `Server(name)` with no `version` makes the SDK report *its own*
package version, so the published 0.7.0 told every client it was `1.29.1` — a
number that moved with a user's `mcp` resolution and never with a release
here. `_package_version()` (mcp_server.py) reads
`importlib.metadata.version("temporal-reasoning")` first, then
`.claude-plugin/plugin.json`, then gives up with `unknown`. Both sources are
load-bearing and neither covers the other: plugin.json is NOT in the wheel
(`py-modules` ships four `.py` files and nothing else), so an installed
package has only metadata; and a dev install writes a real
`temporal_reasoning-0.0.0.dist-info` carrying the placeholder that only
`release.yml` stamps, so handling `PackageNotFoundError` alone would report
`0.0.0` on every developer machine and in CI. That is why the placeholder is
treated as absent rather than trusted — and why a fix here is verified
against a built, stamped wheel, not just the checkout.

**project-minigraf/minigraf#287 is still OPEN and is worked around here, not
fixed.** Batching facts that share `(entity, attribute, valid_from)` into one
transact silently keeps only the last, because the EAVT pending index omits
value bytes. It is VERSION-INVARIANT — measured identically on 1.2.3 and 2.0.0
— so the upgrade neither helped nor hurt. `:contains`, `:depends-on` and
`:parent` are therefore transacted ONE PER CALL at four sites; never "simplify"
those loops into a single batch. All four are now regression-guarded.

**Which triples go down that one-per-call path is decided by SUBSTRING match on
the whole rendered triple, and its safety rests on an unwritten invariant:
code-entity string-valued attributes never contain `:`.** Eight sites classify
with `":contains" in t` / `":introduced-by" not in t` and friends
(`_forward_apply`, `_reverse_apply`, `_re_date_structural_facts`,
`_ingest_close`, the correction sweep), matching anywhere in the string —
including inside a quoted VALUE. #232 filed that as silent corruption and was
closed 2026-09-01 as an **accepted residual**, on a measurement rather than an
argument: on the 831-commit at-scale graph, **0 of 7,235** code-entity
`:description`/`:file`/`:path` values contain even one colon, because those
attributes hold identifiers and paths and every classification literal starts
with `:`.

The 24 values that DO carry the literals are commit `:description`/`:subject`
(this repo's own subjects, e.g. "Retract `:introduced-by` at every entity close
site"), and they reach only the two `:contains` splits — where a misroute sends
the triple into the one-per-call loop instead of the batch, i.e. toward the
SAFE path. Nothing is dropped.

**So before adding any free-text attribute to a code entity — a docstring, a
comment, a commit-message-derived summary — reopen #232 and switch those sites
to an anchored attribute-slot match (`^\[\S+\s+:contains\s`) FIRST.** The
corrupting direction becomes reachable the moment such an attribute exists: a
structural triple wrongly filtered out of the re-dated set leaves an entity with
lineage but no type, name or file, silently. The same applies to ingesting a
repository with `:` in a tracked file path.

`mcp_server.py` enforces the single-handle invariant through `_DbLeaseManager`
(#255), which replaced the old `_db = None` "release the lock" idiom — that global is **deleted**, so
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

**Re-walking an already-ingested position is now skipped, and the witness is
the thing that decides whether that is safe (#326).** The obvious predicate is
unsound: in `_reverse_apply` `[:commit/<hash> :entity-type :type/commit]` is the
FIRST element of `all_triples`, written before any file result is looked at,
while `_frontier_persist_claim` runs LAST. So the commit entity's presence is
the WEAKEST available witness of a completed write — and it is present on
exactly the torn positions #313 needs re-walked. A fast path keyed on it would
make that orphaned lineage permanent.

The witness is membership in a `:type/completed-region` fact set, archived by
`_frontier_load` from the high interval it is about to discard. Because
`_frontier_persist_claim` is the last write of a position, every position inside
a persisted interval provably completed, and the archive inherits that. A torn
position's claim never persisted, so it was never inside the interval that got
archived: it is in no region and is never skipped, by construction rather than
by care. Only a REPRESENTABLE interval is archived — the inverted case reaches
the same branch and describes no completed region at all.

`fwd` never skips, and one clause buys two properties. A forward claim inside a
provisional region is the authority upgrade that must still happen; and
`_forward_apply` mutates `_ForwardWalkState`'s cross-position preload dicts
in place, so a skipped forward position would desynchronize that state for every
later forward position, silently. `_reverse_apply` takes no state object and
reads what it needs from the graph, which is why reverse carries no equivalent
hazard.

The region type is deliberately absent from `MINIGRAF_SCHEMA`, like
`:type/ingest-interval` — `handle_minigraf_audit` iterates exactly the registered
types and would otherwise retract its attributes — and every write goes through
the internal `_transact`/`_retract`, since the public handler rejects an
unregistered type outright. Regions carry a string-valued `:ident` because
enumerating by `:entity-type` binds the entity in UUID space.

**A skipped position still costs `processed`, and that is not sloppiness.**
#317's `commit_census` reads `_ingest_progress["processed"]` as `walk_claimed`,
so excluding skips would silently redefine the number that gate compares against
`git rev-list` and turn a clean skip-heavy resume into a reported lost commit.
The counter is `positions_skipped`, never `skipped`: `status` already takes the
value `"skipped"` (run declined, another process owns the graph) and
`commit_census` already reports `skipped_commits` (extraction failures).

**`walk_vs_graph` is NOT a backstop on the skip predicate, and an earlier draft
of this section said it was.** `_ingest_progress["processed"]` is SEEDED with
`prior_ingested = _count_commit_entities(db)` and then incremented for every
position retired this run, including positions already counted in that seed. So
`walk_vs_graph` is nonzero on ANY resume that touches already-ingested
territory, skip or no skip — measured 10 with the fast path against 9 without,
on the same scenario. It cannot discriminate a wrong skip from an ordinary
resume.

State plainly what follows: **no existing gate catches a wrong skip.**
`fact_audit`'s `divergence` reads 0 because a skipped commit reaches neither
the graph nor the index, so the two witnesses agree about its absence; both
`:introduced-by` checks only examine entities that EXIST; `stderr_capture` has
nothing to read; and the at-scale `commit_census` runs on a fresh
ingestion-only graph where no archived region exists at all. The predicate's
soundness therefore rests entirely on the witness above and on its positive
control (the #313 torn-position test), plus the two stored denominators
described next — not on anything downstream noticing afterwards.

**A region is stored as two HASHES but consumed as a closed POSITION RANGE, and
that needs a denominator to be sound.** Every position between the bounds is
treated as proven-complete, which holds only if the linearization grew by
APPENDING above the region. `git log --topo-order --reverse` guarantees no such
thing: it places a new commit immediately after its branch point whenever the
old tip's line stalls behind it — "branch off an old commit, merge the mainline
in, fast-forward the mainline" is enough. That commit lands INSIDE the archived
bounds and is skipped, permanently and silently.

So the frontier interval carries `:pos-count`, the span it was CLAIMED under,
written at claim time in the same transact as the bound that moved, and
`_frontier_load` archives only an interval whose stored count still matches its
current span. The count MUST come from the interval, not be computed where the
region is archived: a count computed at archive time is computed from the very
span it would then be compared against, so it always agrees and discriminates
nothing. Archived regions carry the same denominator and
`_completed_regions_load` re-checks it, which covers an insertion that lands in
a LATER run. An interval or region carrying NO count is not trusted — "no
denominator" and "a denominator that still checks out" must not be the same
branch when the failure mode is silent permanent loss. Cost of a mismatch is one
full re-walk, i.e. exactly master's behaviour. This is #316's
`code_entities_scanned` idiom: every run re-proves its own positive control.

**The end-of-walk flush's hi bound is the highest SKIPPED position, never the
highest reverse position claimed.** `_frontier_persist_span` moves `:hi-hash`
UP, which `_frontier_persist_claim` never does for the high interval. A reverse
position whose write FAILS takes the per-commit `except`, which does
`processed += 1`, persists no claim, and leaves `completed_all` True — so a
flush bounded by the highest claim would raise the persisted top bound over it.
Once `:hi-hash` reaches the tip the interval is representable, so the next
`_frontier_load` RETAINS it instead of discarding it and nothing ever re-walks
those positions. The skipped span is the only thing the flush is entitled to
assert.

There is no `GRAPH_FORMAT_VERSION` bump — this only adds facts going forward.
Existing graphs are not repaired and need no migration: a region is only
knowable from an interval that exists at discard time, so there is nothing to
seed. They simply never skip until their first discard, and because archiving
happens in `_frontier_load` at LOAD time, the run that discards is the run that
skips.

`handle_minigraf_ingest_status`'s report carries `positions_skipped_this_run`
alongside a bare `positions_skipped` that is always the same number today — the
counter resets at the start of every run rather than being derived from a
process-lifetime total the way `processed_this_run` is derived from
`prior_ingested`, so there is no separate lifetime figure to look for under the
unqualified name. The per-run figure is the one worth watching: #325's incident
read as healthy for 98 minutes because `processed` climbs on a replayed
position exactly as it does on a new one, and nothing distinguished the two. A
run whose `positions_skipped_this_run` climbs alongside `processed_this_run`
while the graph's own commit count stays flat is re-walking territory it
already holds, not making progress.

## Claude Code Plugin Publishing

The plugin is published via a stub architecture — `install.py` handles all registration automatically.

**Why a stub:** Claude Code's internal copier (`mc$()`) copies the plugin source tree to a versioned cache. REPO_DIR contains `.venv/` (hundreds of MB), causing the copy to fail silently. `install.py` builds a minimal stub at `~/.claude/plugins/stubs/temporal-reasoning-local/` containing only `.claude-plugin/` and `skills/`, which `mc$()` can copy successfully.

**Five files that must be correct** (all written by `install.py`):

1. `~/.claude/plugins/stubs/…/.claude-plugin/marketplace.json` — must have `owner` field; plugin `source: "./"`
2. `~/.claude/plugins/stubs/…/.claude-plugin/plugin.json` — plugin identity and version
3. `~/.claude/settings.json` — `enabledPlugins` + `extraKnownMarketplaces` → stub dir
4. `~/.claude/plugins/installed_plugins.json` — `installPath` → versioned cache dir
5. `~/.claude/plugins/known_marketplaces.json` — **authoritative store**; `source.path` and `installLocation` → stub dir (settings.json changes don't propagate here automatically)

**Version bumps:** canonical version lives in `.claude-plugin/plugin.json`; `install.py` reads it via `PLUGIN_VERSION`. Stale versioned cache dirs are deleted on each run. That read has **no fallback** and exits on failure, deliberately: the version names the cache dir Claude Code is told to copy the stub into, and the same run deletes every cache dir that is not it — so a guessed version deletes the working install and registers a path nothing will populate, while the script prints a tick and exits 0. It used to fall back to a hardcoded `0.3.0`.

**Diagnosing failures:** `claude plugin list` shows per-plugin status and errors. "Plugin X not found in marketplace Y" means marketplace.json failed validation — check the `owner` field and run `claude plugin validate <stub-dir>`.

**Official directory structure** (from docs): the recommended layout separates marketplace root from plugin subdir, with `source: "./plugins/my-plugin"` in marketplace.json. Our stub uses `source: "./"` (root = plugin), which is non-standard but works. A proper separation would let `mc$()` copy without the stub workaround.

**Offline resilience:** set `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1` to prevent Claude Code from wiping the marketplace cache when a git pull fails.

**Files outside plugin dir:** plugins are copied to cache, so `../relative-paths` break. Use symlinks if the plugin needs to reference files outside its directory.

## Query Examples

```python
# Basic query
minigraf_query(datalog="[:find ?x :where [?e :attr ?x]]")

# With temporal
minigraf_query(datalog="[:find ?x :as-of 5 :where [?e :attr ?x]]")

# Count
minigraf_query(datalog="[:find (count ?e) :where [?e :description ?d]]")
```
