# #256 — Persistent-graph at-scale verification of #251 non-recurrence

Date: 2026-08-16
Issue: #256. Refs #251, #253, #254, #255, project-minigraf/minigraf#304, #314.

## Purpose

#251 (two live `MiniGrafDb` handles on one file corrupting each other's cached
`page_count`) is fixed: upstream in minigraf 1.2.2, and at the four local
overlap sites in #254. What has never been done is confirming it at scale, on
real history, against a graph that still exists when the run ends.

Every historical occurrence was unobservable for the same reason: the at-scale
harness ingests into a `tempfile.TemporaryDirectory`, so the graph was deleted
before anyone could query it. This spec closes that gap.

## What the re-read changed

The issue text proposes three checks. Reading the current code rather than
trusting the issue, two of them do not hold as written.

### Item 2 is vacuous as specified

> "Assert no commit was skipped by the per-commit handler (`processed` equals
> the expected count)."

`_ingest_progress["processed"]` is incremented on all three paths through the
per-commit loop:

- `mcp_server.py:11158` — after a successful write, **and** after a write
  failure swallowed by the `except Exception` at 11140, which prints to stderr
  and falls through.
- `mcp_server.py:11112` — the extraction-failure skip path, which increments
  and `continue`s.

So `processed == 705` holds whether or not commits were skipped, and
`final_status: complete` is equally blind — per-commit failures are
deliberately isolated per `_run_ingestion`'s documented "fail only the one
commit" contract, not propagated.

The only real signal that a commit was dropped is the stderr line
`[_run_ingestion] skipping commit <hash> (<subject>): write failed: ...` or
`skipping unreadable commit ...`.

### Item 3 contains a contradiction

> "Query the surviving graph for entities left provisional and confirm Stage B
> reconciled them."

If Stage B reconciled them they would not be provisional. A non-empty
provisional residue is by design: `_correction_sweep_apply` returns
`skipped_events`, its documented fail-safe for candidates with "an
ambiguous/wrong guess" that "stayed provisional or unreconciled despite this
call visiting their commit" (`mcp_server.py:10382`). Asserting zero provisional
entities would fail on a healthy graph.

What makes item 3 checkable is that `_correction_sweep_log_summary`
(`mcp_server.py:10590`) emits exactly one **uncapped** stderr line at end of
sweep:

```
[_correction_sweep] N entities left provisional/unreconciled this run
```

The per-entity logs are capped at 10 (`_CORRECTION_SWEEP_LOG_CAP`); this
summary is not. So N is recoverable from stderr with no production-code change.

### Consequence

The design replaces three assertions with **one real assertion, one real
failure gate, and a set of recorded observations** — and makes the
instrumentation itself the thing under test.

## Architecture

Two components, joined by a file on disk rather than a function call.

### Component 1 — `evals/at_scale/run_ingestion_benchmark.py` (modified, generic)

Neither addition is #256-specific.

**`--graph-path PATH`.** When given, `main()` uses it instead of
`tempfile.TemporaryDirectory`, and refuses to run if the path already exists.
That enforces the precondition `run_ingestion_benchmark`'s own docstring
already states ("graph_path must not already exist — each call is a fresh,
isolated run") and matches CLAUDE.md's standing rule that graphs are rebuilt,
never re-ingested in place. When omitted, behaviour is identical to today.

**A file-descriptor-level stderr tee** active for the duration of the run
(see "Capture mechanism" below). Output still reaches real stderr live; a copy
accumulates for scanning.

After the run it scans the captured text and adds three keys to the metrics
dict:

| Key | Source | Meaning |
|---|---|---|
| `skipped_commits` | lines matching `[_run_ingestion] skipping commit` / `skipping unreadable commit` | commits the per-commit handler dropped |
| `error_signals` | lines matching any of the four error patterns — the three #251 signatures, plus `tee_stderr`'s own pump-failure marker | see below |
| `correction_sweep_skipped` | the `[_correction_sweep] N entities left provisional/unreconciled this run` line | N, consumed by the probe |

The first three are the #251 signatures, and they are **regexes, not
literals** — the page error carries live numbers (`Page 130 out of bounds
(total pages: 113)` is the form #251 actually reproduced), so a literal `Page N
out of bounds` would match nothing:

- `Page \d+ out of bounds`
- `Serde Deserialization Error`
- `stream_all_entries: expected leaf page`

Matching a literal where a regex is needed is the fail-open case the scanner
positive controls exist to catch.

The fourth pattern, `\[tee_stderr\] pump failed:`, is **not** a #251 signature
— it is a health signal for the capture apparatus itself, saying "the tee
broke", not "the graph broke". If the pump thread dies, the marker it appends
to the captured text is the only evidence left; without a pattern for it a
marker-only capture scans byte-identically to a clean run. It is deliberately
**unanchored**: the pump appends the marker directly after an arbitrary 64 KiB
`os.read()` slice that need not end at a line boundary, so an anchored pattern
was defeated by the emitter itself whenever the pump died mid-run rather than
at iteration 0. The emitter also prepends its own newline; the two halves are
independent, and each is separately ablation-proven by
`test_a_mid_run_pump_death_is_still_scannable`.

`_exit_code` treats this signal like the others — a run whose stderr capture
failed is not a run that verified anything.

`_exit_code` extends to return non-zero when `skipped_commits` or
`error_signals` is non-empty. `correction_sweep_skipped` is an observation, not
a failure condition — a legitimate non-zero value is normal.

### Component 2 — `evals/at_scale/probe_provisional_residue.py` (new, #256-specific)

A separate process run after the benchmark. Takes `--graph-path` and
`--metrics-json`. Opens the graph read-only, counts live
`:type/lineage-marker` entities carrying `:status :provisional` (M) plus a
per-`:entity-type` breakdown, reads N from the metrics JSON, applies `M <= N`,
and writes `results/256-provisional-residue.json`. Exits non-zero on `M > N`.

**Why `<=` and not `==`.** N counts entities left "provisional *or*
unreconciled". The case-2 branch that leaves an already-authoritative entity
with an ambiguous `:introduced-by` count is unreconciled but never marks the
entity provisional, so it contributes to N without contributing to M. M is
therefore a subset of N, and equality would fail on a healthy graph for the
same reason asserting `M == 0` would.

`M > N` means an entity is sitting provisional in the graph that the sweep
never accounted for — state left inconsistent by something other than the
designed fail-safe. That is the #251 signature, and it is the one condition
this probe exists to detect.

This follows the probe convention CLAUDE.md documents — read-only probes whose
recorded measurements live under `results/` as analysis artifacts rather than
part of the recurring benchmark run. `probe_dep_preload_exposure.py` and
`probe_ident_collision_census.py` are the existing precedent.

### Why two processes, not one function

The probe being a separate process is load-bearing. It opens the persisted
graph with no other handle live anywhere in the process. Querying in-process
while the benchmark's own handle lifecycle unwinds is the exact hazard class
that produced #251/#253, and #256 exists to confirm that bug is gone — its
check must not run through a path that could re-create it.

### What is deliberately absent

No change to `mcp_server.py`. Both N and the skip lines are read from stderr
the production code already emits by design. Nothing on this branch touches
`_lease_manager`, so #272's instance-level-monkeypatch trap
(`tests/test_mcp_server.py:288`, and the workaround at 23331) has no contact
with this work. #272 remains open and deserves its own branch.

## Data flow

```
1. run_ingestion_benchmark.py --repo-path . --graph-path <persistent>
      -> ingests master's commits (~25 min) with the poller running throughout
      -> writes results/ingestion-<ts>.json, now carrying skipped_commits,
         error_signals, correction_sweep_skipped
      -> appends the run table to benchmark.md
      -> exit 1 if any commit was skipped or any #251 string appeared

2. probe_provisional_residue.py --graph-path <same> --metrics-json <that file>
      -> opens the graph (separate process, sole handle)
      -> M = live :type/lineage-marker entities with :status :provisional
      -> N = correction_sweep_skipped, read from the metrics JSON
      -> writes results/256-provisional-residue.json
      -> exit 1 if M > N
```

### Where the graph lives

Outside the repository. The last recorded run produced a 211 MB graph plus an
89 MB fact index, and `results/` is committed. The graph goes under the
session scratchpad directory.

**The graph is an intermediate, not an artifact.** What gets committed is the
two JSON files and the `benchmark.md` entry. The graph is what the probe
consumes and is then disposable.

### Re-running

Because `--graph-path` refuses an existing path, a second run needs a fresh
path or an explicit manual delete. That friction is intentional: it makes
"ingested twice into one graph" impossible rather than merely discouraged,
consistent with the rebuild-never-migrate rule.

## Capture mechanism

The tee operates at the **file-descriptor level** — `os.dup2` onto a pipe with
a pump thread writing to both real stderr and a buffer — not by swapping
`sys.stderr`.

The difference is not cosmetic. A `sys.stderr` swap catches everything the
parent Python process prints, which covers both the `[_run_ingestion]
skipping...` lines and the `[_correction_sweep]` summary; items 2 and 3 would
work either way. But `_extract_commit` runs in a `ProcessPoolExecutor` whose
workers inherit fd 2, not the parent's `sys.stderr` object, and
`Serde Deserialization Error` / `stream_all_entries: expected leaf page` are
minigraf's own Rust strings. If either arrives as a native panic rather than a
caught Python exception message, it goes straight to fd 2 and a `sys.stderr`
swap never sees it.

That is precisely the failure class #256 is hunting. A clean scan from a blind
instrument is the fail-open pattern: a broken check reports all-clear having
examined nothing.

This justification is a counterfactual claim, so it carries an ablation
requirement — see Testing.

## Failure modes handled explicitly

- **Probe run against the wrong graph or a stale JSON.** The probe records the
  graph path and the metrics filename it read into its own output, so the
  pairing is auditable after the fact.
- **Metrics JSON missing `correction_sweep_skipped`.** The probe fails loudly
  rather than defaulting N to 0, which would silently turn `M <= N` into
  `M == 0` and produce a false failure.
- **Ingestion ended in `error`.** `_exit_code` already returns 1; the probe
  refuses to run against a graph whose run did not reach `complete`, since
  residue on an aborted run means nothing.
- **N is large.** Recorded raw, not only compared. `M <= N` weakens as N grows
   — if the sweep legitimately skips thousands, M could hide real corruption
  underneath. Recording both raw numbers lets a future run compare N itself
  across runs, where a jump in N is its own signal. This is a stated limitation,
  not a solved problem.

## Testing

Tests follow the existing `tests/test_at_scale_<component>.py` convention.

### Extended: `tests/test_at_scale_ingestion_benchmark.py`

- `--graph-path` refuses an existing path.
- Omitting `--graph-path` preserves today's temp-dir behaviour exactly. This
  matters: the recurring benchmark must not change.
- **Scanner positive controls, one per pattern.** Feed the scanner a synthetic
  log containing each of the five strings — both `[_run_ingestion] skipping`
  variants and all three #251 strings — and assert each is detected. Then a
  clean log asserting empty results. Without positive controls this is the
  fail-open shape: a scanner with a broken pattern reports "no errors found"
  having matched nothing, indistinguishable from a healthy run. The negative
  control alone cannot separate those two cases.
- **Fixture gotcha to design around:** `tmp_path` bakes the test's own name
  into any path it produces. A test named e.g. `test_page_out_of_bounds` that
  builds a synthetic log through a `tmp_path` string can make the scanner match
  on the path rather than the planted line, passing for the wrong reason.
  Fixtures must plant patterns in log text carrying no `tmp_path`-derived
  substring.

### The tee requires an ablation

The justification for fd-level capture over a `sys.stderr` swap is the claim
"it catches native/child-process output that `sys.stderr` would miss." That is
a claim about a counterfactual and needs the experiment, not an assumption:

- A test that writes to fd 2 **from a child process** and asserts the fd tee
  captures it **while a `sys.stderr` swap does not**.
- If that ablation does not reproduce the miss, the fd-level complexity is not
  buying what this spec argues it buys, and the implementation should fall back
  to the simpler `sys.stderr` swap.
- Separately, assert pass-through: output still reaches real stderr. A tee that
  silently swallowed a 25-minute run's live output would be worse than no tee.

### New: `tests/test_at_scale_provisional_residue_probe.py`

Following the fixture shape `tests/test_at_scale_dep_preload_probe.py` uses:

- A small real graph with a **known** number of planted
  `:type/lineage-marker` / `:status :provisional` entities; assert M matches
  exactly and that the per-type breakdown sums to M.
- `M <= N` passes; `M > N` fails — and the failing case must be **observed
  failing**, not assumed. A threshold comparison never watched go red is not a
  guard.
- Missing `correction_sweep_skipped` fails loudly rather than defaulting N to 0.
- A graph whose run did not reach `final_status: complete` is refused.

### Deliberately not tested

The at-scale run itself. It takes ~25 minutes against real history, and its
output is an observation, not an assertion: the actual values of M and N are
measurements of this repository's history, not invariants.

What is tested is the **instrumentation** — that the scanner sees what it
claims to see, the tee captures what it claims to capture, and the comparison
fires when it should. The run then produces numbers those instruments can be
trusted to have measured honestly.

## Acceptance

#256 is satisfied when:

1. A persistent-graph at-scale run over master's full history completes with
   `final_status: complete`, empty `skipped_commits`, and empty
   `error_signals`.
2. The probe reports `M <= N` against that run's surviving graph, with M, N and
   the per-type breakdown recorded.
3. Both JSON results and the `benchmark.md` entry are committed; the graph
   itself is not.

## Relationship to the #255 acceptance run

The run recorded in `results/ingestion-20260816T022619Z.json` (commit 067a75d)
already covers much of items 1–2 informally: 705 commits, `final_status:
complete`, concurrent poller throughout, no "already open in this process".
But its metrics captured no error strings and no skip accounting, and its graph
was in a temp directory. Its "no errors" claim rests on a human having watched
stdout. This spec exists to replace that with a captured, machine-checked
result against a graph that survives the run.
