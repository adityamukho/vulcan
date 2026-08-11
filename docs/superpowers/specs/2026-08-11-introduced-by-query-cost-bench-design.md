# `:introduced-by` / liveness point-query cost bench

Design spec for the measurement that decides **#239**'s fix. Not a fix itself.

#239 proposes replacing per-ident point queries with a set-at-a-time form. That
is the right move **if and only if** the per-call cost is constant in graph
size. If it grows, batching only reduces how often we pay a growing cost and
the run stays superlinear. Nobody has measured which it is for the two queries
that actually dominate.

This bench answers that one question, in minutes rather than the many hours a
full ingestion needs.

## Why this is not "just run the profiler"

**#239's own numbers are no longer safe to build on.** Three reasons, each
independent:

1. **The harness that produced them no longer exists.** #239's attribution came
   from parent-process monkeypatching in a directory that has since been
   deleted. Its figures cannot be reconciled from artifacts, only re-measured.
   The same caveat already applies to the 33.6% headline.
2. **The code underneath changed.** PR #246 replaced `_reverse_apply`'s
   known-entity gate: `_entity_introduced_by_query(db, ident) is not None`
   became `_entity_ident_is_live(db, ident)` (`mcp_server.py:5648`, used at
   ~8896). So #239's D2 row "`_entity_introduced_by_query` (from
   `_reverse_apply`) — 464,377 calls / 215.44 s / 13.8%" no longer describes
   that call site. The volume is the same; the query is not.
3. **The absolute cost is wildly off #239's model.** #245's acceptance probe
   was killed at **247 of 657 commits after 9.8 hours** — **142.5 s/commit**
   against #239's measured **2.74 s/commit** (568 commits / 1,558.6 s). That is
   **~52x**. Part is environmental (the machine ran test suites concurrently)
   and the repo grew 568 → 657 commits, but neither accounts for 52x.

If per-call cost grows with graph size, (3) is explained and #239's flat
per-call model is the thing that is wrong. That is a testable proposition and
this bench tests it.

## The hypothesis, and why it has priors

**This project has found the same mechanism twice.** #152 and #236 both
discovered a per-item operation degrading to a full scan because minigraf's
storage had no B-tree on the queried column, and both were fixed by a companion
table (`facts_dedup`), not by batching. #236 measured that batching bought
nothing there: at 80k rows, 200 triples cost 11.17 / 11.41 / 11.53 ms per
triple as 200 calls / 20 calls / 1 call.

If `_entity_introduced_by_query` or `_entity_ident_is_live` scans, this is that
finding a third time, and the fix is an index rather than a batch.

**The counter-prior is equally real.** `bench_lineage_query_cost.py` tested
exactly this hypothesis for `_lineage_is_provisional` and **refuted** it: a MISS
is 10-12% *cheaper* than a HIT, and neither scales across a 50x graph-size
range (0.046-0.053 ms/call at 100k / 1M / 5M facts). So the scan hypothesis is
not a foregone conclusion for these two either — which is the whole reason to
measure instead of assuming.

## What it measures

`evals/at_scale/bench_introduced_by_query_cost.py`, a sibling of
`bench_lineage_query_cost.py`, reusing its `fresh_db` / `populate_filler`
helpers, its timing style, and its `CANDIDATES_PER_COMMIT = 1265` working
figure.

### E1 — per-call cost vs graph size, HIT and MISS

Three query shapes at 100k / 1M / 5M filler facts, each measured for a HIT
(entity exists) and a MISS (it does not):

| function | `mcp_server.py` (verified on `master` = `463a922`) | why it is here |
| --- | --- | --- |
| `_entity_introduced_by_query` | 5718, delegating to `_entity_introduced_by_values_query` at 5674 | #239's 13.8%, and the frame `py-spy` caught on the killed run |
| `_entity_ident_is_live` | 5648 | #246's replacement gate; same per-ident volume, never measured |
| `_lineage_is_provisional` | 5480 | **control** |

Time the **public wrappers**, not the raw Datalog: `_entity_introduced_by_query`
carries ambiguity detection and a rate-capped warning on top of
`_entity_introduced_by_values_query`, and the fix will replace the wrapper, so
the wrapper is what the cost model needs.

**The control is load-bearing, not padding.** Its answer is already known from
`bench_lineage_query_cost.py`. If this bench does not reproduce flat
0.046-0.053 ms/call for it, the methodology is broken and the other two numbers
must be discarded rather than believed.

MISS is measured separately because the #235 result was counter-intuitive — a
MISS came out *cheaper*, which is what refuted the scan hypothesis there. A
MISS that grows with graph size while a HIT stays flat is the companion-index
signature.

### E2 — two independent size axes

**Total facts and ident population are different variables and must be varied
separately.** `_entity_ident_is_live` queries `:ident`; its cost may track the
number of *entities* rather than the size of the graph. Filler facts inflate
the graph without adding idents, so varying only filler could report "flat" and
hide a real cliff at high entity counts. `bench_lineage_query_cost.py` already
splits these axes for marker population (its E2b) for the same reason.

Axis A: filler facts at 100k / 1M / 5M, ident population held fixed.
Axis B: ident population at 1k / 10k / 50k, filler held fixed.

### E3 — the set-at-a-time counterparts

Cost of the batch forms the fix would use, so the crossover is measured rather
than assumed:

- **whole-relation preload** — one query binding `?e` as an output variable,
  the `_preload_provisional_idents` / `_preload_known_deps` pattern;
- **scoped batch** — the same restricted to N specific idents. This *is*
  #239's candidate fix 1 (a per-commit batch over that commit's candidate
  idents), and this bench is the only place its cost gets established before
  anyone builds it.

Crossover reported at `CANDIDATES_PER_COMMIT = 1265`, at each graph size.

### E4 — one point at the real scale

A single measurement at roughly the scale the killed run reached (~35 MB graph,
the fact population ~247 commits of this repo produces), so the report can say
whether the 52x per-commit gap is explained by graph size or remains
unaccounted for. This is a data point, not a full ingestion.

## The verdict it emits

One number decides #239's fix, and **the threshold is stated here, before the
data exists**, so it cannot be chosen to fit the result:

> **FLAT** = per-call latency grows by **less than 2x** across the 50x filler
> range AND less than 2x across the 50x ident-population range, for both HIT
> and MISS.

- **FLAT** → cost is call *count*. #239's premise holds; the fix is a
  per-commit scoped batch or a write-through cache, and E3's crossover picks
  between them.
- **NOT FLAT** → cost is per-call and grows. Batching reduces only how often we
  pay it; the fix is a companion index, the #152/#236 pattern a third time.

The 2x bound is deliberately loose: the control's own spread
(0.046-0.053 ms across 50x) is ~1.15x, so 2x sits far outside measured noise
while leaving no room to argue a genuine cliff into "flat".

## Methodology — two traps, both already paid for once

1. **Checkpoint before every timed query.** `db.checkpoint()` after fixture
   writes and before timing. `bench_lineage_query_cost.py`'s own methodology
   note records that skipping this makes an uncheckpointed graph answer the
   point query and the join query *differently* once enough facts accumulate —
   which would silently invert this bench's verdict.
2. **One live `MiniGrafDb` handle per process.** Two handles on one file each
   cache their own `page_count` and corrupt each other (`CLAUDE.md`'s
   single-handle invariant, #251/#253, project-minigraf/minigraf#304). minigraf
   1.2.2+ raises on the second open rather than corrupting, so a violation here
   fails loudly — but the fixture must still open and release deliberately.

Timing: repetitions per measurement with the median reported, matching the
existing bench. Fixtures built once per graph size and reused across the query
shapes measured at that size, so fixture construction is not on the clock.

## Output

Printed table plus JSON to `evals/at_scale/results/239-introduced-by-query-cost.json`,
matching the existing benches' convention. The JSON carries every raw
measurement, the control's reproduction check, and the FLAT / NOT FLAT verdict
with the growth ratios that produced it.

If the control fails to reproduce, the report says **INVALID MEASUREMENT** and
the verdict is withheld — the same discipline
`probe_dep_preload_exposure.py` applies to its unmappable-fact counters.

## Scope

**In:** the bench, its results file, and a short entry in
`evals/at_scale/benchmark.md` recording the verdict.

**Out:** any change to `mcp_server.py`. This spec fixes nothing. #239's actual
fix gets its own spec once this produces a number.

**Out:** re-running #239's full attribution harness. That needs a complete
ingestion, which is the thing currently blocked; this bench is deliberately the
cheap discriminator that does not.

## Testing

The bench is measurement tooling, not production code, and the existing benches
carry no unit tests. It gets the same treatment, with one exception: **the
control (E1's `_lineage_is_provisional` row) is the self-test.** A run whose
control does not reproduce the known flat result is reported as invalid.

Real minigraf backend throughout — no mocks, per `docs/testing-conventions.md`.

## Documentation

No `SKILL.md` or `CLAUDE.md` change: no query syntax, attribute, or tool
surface moves. `evals/at_scale/benchmark.md` gains the verdict entry.

## Issue hygiene

`Refs #239`, `Refs #245`, `Refs #222`. **No closing keyword** — this bench does
not fix #239, and #222 must stay open. GitHub scans commit messages and PR
bodies, and a *negated* keyword has auto-closed an issue on this project.
