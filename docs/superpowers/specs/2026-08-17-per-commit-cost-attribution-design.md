# Per-commit ingestion cost attribution

Design spec for the measurement that decides **#260**. Not a fix itself.

#260 reports that ingestion's per-commit cost grows with history length —
1.39 → 4.29 s/commit across ~520 commits, roughly 3.1x — and establishes that
it is **not** the per-ident point queries #239 names, which
`bench_introduced_by_query_cost.py` measured flat across 50x of graph size and
50x of ident population.

#260's own body names the thing to do first: **kill the confound.** It asks
whether entities-touched-per-commit grows across this repository's history, and
says that if it does, the growth must be normalised by it before being treated
as a code problem at all.

This spec is that normalisation, done at per-commit granularity instead of
against #260's eight wall-clock windows.

## The confound is real, and it is larger than the signal

Measured from git alone, before any ingestion — `def`/`class` count in the
added/modified `.py` files each commit touches, over all 767 commits on master
(38 of which are merges, contributing no files):

| commit index | files touched / commit | `def`+`class` in touched `.py` / commit |
|---|---:|---:|
| 0–95 | 2.3 | 8.3 |
| 95–190 | 1.7 | 37.2 |
| 190–285 | 3.9 | 113.9 |
| 285–380 | 1.6 | 300.9 |
| 380–475 | 2.0 | 617.2 |
| 475–570 | 2.1 | 710.6 |
| 570–665 | 1.7 | 491.3 |
| 665–767 | 1.8 | 288.0 |

**Files-per-commit is flat. Entities in those files are not** — a ~94x rise to
the peak, against #260's 3.1x cost rise.

The mechanism is that extraction is **whole-file**.
`_precompute_file_triples` (`mcp_server.py:7451`) builds candidate triples for
*every* module, function, class, global and field in a touched file, and
re-resolves *every* import in it against that commit's whole `known_files` via
`_resolve_module_import`. A one-line edit to a 12k-line `mcp_server.py` costs
what the whole file costs. So per-commit work tracks **repository size**, which
grows for reasons that have nothing to do with the graph.

### A second confound #260 does not name

#260's table is indexed by `_ingest_progress["processed"]`, sampled by a poller
every 180 s. But the walk is #222 phase 2d's **converging two-stream claimer**
(`_RoundRobinClaimer`, `mcp_server.py:10764`): `fwd` positions ascend from 0,
`rev` positions descend from N, claims alternate by a fixed ratio, and
`processed` increments for both. So every window in #260's table pools a
low-index commit with a high-index commit, and the mix shifts toward the middle
of history as the streams converge.

The ratio is `MINIGRAF_INGEST_STREAM_RATIO`, defaulting to **1:1**
(`_DEFAULT_STREAM_RATIO`, `mcp_server.py:10730`) — so the split is even by
default, and the mix model below is not assuming it.

Modelling that against the table above — at run fraction φ the forward stream
sits near index φ·N/2 and the reverse near N−1−φ·N/2, taking a ±25-commit
windowed mean at each:

| run % | fwd idx | fwd ent | rev idx | rev ent | window mean |
|---:|---:|---:|---:|---:|---:|
| 5% | 19 | 9.0 | 747 | 310.4 | 159.7 |
| 25% | 95 | 6.1 | 671 | 119.2 | 62.6 |
| 50% | 191 | 104.9 | 575 | 703.7 | 404.3 |
| 75% | 287 | 200.9 | 479 | 527.3 | 364.1 |
| 95% | 363 | 379.9 | 403 | 689.6 | 534.7 |

**3.32x across the run, against #260's observed 3.09x** — and non-monotonic in
the same way #260 reports its own segments to be noisy.

That is close enough that the confound may account for the entire signal. It is
**not** a verdict: the proxy is a `def`/`class` regex over `.py` only (no
globals, no fields, no imports, no other language), and it models the stream
positions rather than reading them from a real run. It is a hypothesis with a
mechanism and a matching magnitude, which is what makes it worth measuring
properly rather than arguing about.

## Why the existing tooling cannot answer this

**Nothing in the repository records per-commit cost.** `run_ingestion_benchmark.py`
times the whole run (`time.perf_counter()` at :172/:234) and the poller samples
`processed` at intervals; that is exactly the resolution that produced #260's
table and destroyed the information needed to normalise it.

`profile_forward_reconcile_attribution.py` is the right tool for *what* the wall
clock goes to — it drives Stage A and Stage B and merges cProfile across
threads. But it aggregates over a slice. It cannot say whether a call site got
more expensive or merely got called more often, which is the whole question.

## What is measured

### The trace hook — `mcp_server.py`, gated on `MINIGRAF_INGEST_TRACE_PATH`

When the env var is set, `_run_ingestion`'s per-commit loop appends one JSON
object per commit to that path. When it is unset, the hook is a no-op and
nothing changes. This follows the existing env-gated-ingestion-knob precedent
(`MINIGRAF_INGEST_CHECKPOINT_DUTY`, `_checkpoint_duty_from_env`) and the
existing in-run instrumentation precedent (`_ingest_progress`,
`_CheckpointPolicy.summary()`).

Per record:

| field | source |
|---|---|
| `pos` | the claimed position — the commit's index in `linearization` |
| `tag` | `"fwd"` or `"rev"`, from the claim |
| `hash` | `commit_metadata[pos][0]` |
| `t_since_start` | monotonic seconds since the trace opened |
| `await_s` | wall clock stalled on the extraction future (`await fut`) |
| `apply_s` | wall clock of the `_forward_apply` / `_reverse_apply` executor call |
| `ckpt_d_count`, `ckpt_d_seconds` | **deltas** of `_ingest_checkpoint_policy.checkpoints` / `.total_seconds` across this commit |
| `files_by_status` | counts of `A`/`M`/`D`/`R` in `extracted_files` |
| `n_functions`, `n_classes`, `n_globals`, `n_fields` | `len()` of each `precomputed` entry list, summed over files |
| `n_imports_total`, `n_imports_resolved` | from `precomputed["resolved_imports"]` |
| `n_unchanged_idents` | `len(precomputed["unchanged_idents"])`, summed |

Every work counter is a `len()` over data the parent thread already holds.
`extracted_files` is `_extract_commit`'s `file_results` — one
`(status, file_path, extracted, precomputed, old_path)` per changed file
(`mcp_server.py:8963-8975`) — so nothing is re-parsed and no worker-process
patching is needed.

`ckpt_d_*` is a delta of the policy's existing cumulative counters, so the
policy gains no state. It is sampled around the same span as `apply_s`, because
every ingestion checkpoint site runs on the same single-worker `write_executor`
the apply call runs on.

**The hook adds no `await`, takes no lock, and does not touch the lease.** That
constraint is not cosmetic: the invariant comment above `_db_native_lock` and
the #251/#253 history make the per-commit loop the most dangerous place in the
file to add anything that changes handle lifetime. Timing and `len()` change
neither.

**Not measured, deliberately — per-commit extraction *duration*.** Extraction
runs in a spawn `ProcessPoolExecutor`, so a parent-side hook cannot see it, and
patching in the parent does not propagate to spawn workers. `await_s` is the
stall the serial loop actually pays, which is the quantity that appears in wall
clock; the rest overlaps other commits by design.

**Not measured, deliberately — Stage B.** The correction sweep is a separate
sequential pass after the loop (`mcp_server.py:11173`), and it does not
increment `_ingest_progress["processed"]`. The trace therefore covers **Stage A
only** — which is exactly what #260's polled table covered, so the two are
comparable. Total run wall clock still comes from `run_ingestion_benchmark.py`.

This matters more than it looks, because Stage B is *also* work-driven and
therefore *also* confounded: `_parse_stream_ratio`'s docstring records that a
reverse-claimed commit is **parsed twice** — once by the reverse walk, once by
the sweep's own `_extract_commit` — so total parse cost is
`N · (1 + reverse_fraction)`. If the Stage A verdict comes back CONFOUNDED, the
same confound is a live hypothesis for Stage B and should not be assumed
independent of it.

### The analysis — `evals/at_scale/probe_per_commit_cost.py`

Drives a full-history ingestion with the trace enabled against a **fresh graph
path**, then fits the trace.

The fresh path is mandatory, not hygiene: a completed graph parks its
correction-sweep watermark at frontier-high, so re-ingesting an existing file
repairs and re-measures nothing (see #235's reach). Following `--graph-path`'s
precedent in `run_ingestion_benchmark.py`, the probe refuses to start if the
graph, its `.wal`, **or** its fact index already exists — minigraf replays a
leftover `.wal`, so clearing only the graph silently doubles history.

The probe pins `MINIGRAF_INGEST_STREAM_RATIO` to the 1:1 default and records the
effective ratio, `MINIGRAF_INGEST_CHECKPOINT_DUTY`, the `minigraf` version and
the interpreter path in the artifact. The last one is not boilerplate: running
this project's benchmarks under bare `python` (minigraf 1.1.1 against a `>=1.2.3`
floor, where these queries are ~7x slower) has already produced one retracted
diagnosis on #239 and cost a day. **Use `.venv/bin/python`,** and let the
artifact prove which interpreter ran.

**The model.** Records are split into three **equal-count** groups in the order
they were emitted, which is processed order — first third, middle third, last
third. Not equal spans of wall clock, and not equal spans of `pos`. Within each
group, fit by OLS:

```
apply_s = a + b · W
```

Two parameters, because the two hypotheses have different shapes and a
zero-intercept "cost per unit work" ratio cannot tell them apart:

- **`a` grows, `b` flat** — per-commit *fixed* cost grows with graph size. The
  prime suspect is checkpointing: `_CheckpointPolicy`'s own docstring records
  `db.checkpoint()` as O(graph size), flat in dirty bytes, ~5.1 ms/MB.
- **`b` grows** — cost *per unit of work* grows with graph size. This is the
  point-query story, which #239's bench already refuted at the per-call level;
  finding it here would mean it re-enters somewhere else.
- **both flat** — the growth is input-driven and #260 closes as confounded.

**Why thirds of processed order can be fit at all.** The two-stream claimer,
which confounds #260's aggregate table, is what makes the regression work.
Within any window `fwd` commits carry small work and `rev` commits carry large
work **at the same graph size**, so work size and graph size are decorrelated
within-window. A single-stream walk would have them perfectly collinear and no
fit could separate `a` from `b`.

## Pre-registered before the run

Fixed here, in this document, before the trace exists. This project has been
burned by the alternative: `probe_ident_collision_census.py`'s `PREDICTIONS`
block is frozen rather than re-baselined precisely because re-pointing an
experiment after seeing data re-evaluates its predictions against a different
experiment while still printing "held".

**The work metric.**

```
W = idents_considered
  = Σ over files ( 1 + n_functions + n_classes + n_globals + n_fields )
  + n_imports_resolved
```

One entity ident per module plus one per extracted entity, plus one dependency
edge per resolved import — the units `_build_code_triples` iterates. **`W` is
fixed now.** Every other counter in the trace is reported as *exploratory only*
and may not be substituted for `W` after the fact.

**The control gate.** Mean per-checkpoint duration
(`Σ ckpt_d_seconds / Σ ckpt_d_count`) must grow **≥ 2.0x** from the first third
to the last, with **≥ 5 checkpoints in each** to be evaluable.

This is a positive control, and it is load-bearing rather than decorative.
Checkpoint cost is documented as O(graph size), so a method that cannot detect
growth *there* cannot be trusted when it reports flatness anywhere else — it has
failed open. A clean-looking result from a broken measurement is this project's
most repeated failure mode. **If the control gate does not pass, the run is
VOID.** Not "flat", not "inconclusive": void, and the method gets fixed before
any verdict is posted.

Note the gate is on **per-checkpoint duration**, not on total checkpoint time.
The duty policy holds aggregate checkpointing to a fixed *fraction* of wall
clock as the graph grows, so total time is designed not to grow while each
individual checkpoint still does.

**The verdict.** On `a_last/a_first` and `b_last/b_first`:

| result | verdict |
|---|---|
| both < 1.5x | **CONFOUNDED** — the growth is input-driven. #260 closes. |
| either ≥ 2.0x | **REAL residual growth.** #260 stays open and becomes an attribution task, pointed at whichever of `a` or `b` moved. |
| otherwise (1.5–2.0x) | **INCONCLUSIVE.** Report both parameters and the fit quality; the user decides. |

2.0x matches the threshold `bench_introduced_by_query_cost.py` fixed for #239
before any data existed, against a raw uncontrolled signal of 3.1x here: a
residual under 2.0x means the confound carries most of it. The middle band is
explicit rather than collapsed into a binary, because a 1.8x residual is not
honestly describable as flat.

**A sanity check, reported but not a gate.** `await_s` should correlate with `W`
— extraction stall is the one cost that is unambiguously work-driven. If it does
not, `W` is not measuring what this spec claims it measures, and that is worth
seeing before the verdict is read.

## Deliverables

1. The env-gated trace hook in `mcp_server.py`, with tests: the no-op path when
   the var is unset, the record shape when it is set, correct `ckpt_d_*` delta
   arithmetic, and the counter sums against a known `extracted_files`.
2. `evals/at_scale/probe_per_commit_cost.py` — driver, fresh-path refusal, the
   fit, the control gate, and the pre-registered constants above as a frozen
   block in the source.
3. `evals/at_scale/results/260-per-commit-cost-attribution.json` — the fit, the
   verdict, the control-gate outcome, and the raw per-commit trace or a
   pointer to it.
4. A `benchmark.md` entry, via `report.py`, consistent with the nine tracked
   `ingestion-*.json` entries.
5. A verdict comment on #260 stating which of the three outcomes was reached,
   and the control-gate result alongside it.
6. Docs sync: `CLAUDE.md`'s env-var list gains `MINIGRAF_INGEST_TRACE_PATH`;
   `SKILL.md` checked (this is an eval-side knob with no user-facing tool
   surface, so no change is expected — but checked, not assumed).

## Out of scope

- **Any fix.** This measurement decides whether there is something to fix. If
  the verdict is REAL, attribution is the next piece of work and
  `profile_forward_reconcile_attribution.py` is its tool.
- **#239's batch/cache fix.** Independent, already deflated by its own bench,
  and #260's growth is untouched by it either way.
- **Normalising #260's existing eight-window table.** Superseded rather than
  repaired — the per-commit trace answers the same question at a resolution the
  table cannot reach. The table stays in the issue as the observation that
  prompted this.
