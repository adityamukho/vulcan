# At-Scale Code-Graph Benchmark

See issue #120 and `docs/superpowers/specs/2026-07-19-at-scale-benchmark-design.md`.
Observational only -- no pass/fail thresholds.

The "cross-layer" ground-truth category (entries 5-6) is a genuine single-query
graph-level join: it binds the seeded decision's own `:db/valid-from` as an output
variable via minigraf's `:db/valid-from`/`:db/valid-to` pseudo-attributes, then
filters structural facts by comparing each one's own `:db/valid-from` against it
in the same query (`[(< ?fvf ?dvf)]` / `[(> ?fvf ?dvf)]`). If the seed decision
fact were silently missing, `?dvf` would never bind and `count-distinct` over the
resulting empty join returns `0`, not an error -- entry 5's own expected answer is
already `0`, so it can't distinguish a working join from a silently broken one on
its own; entry 6 (expects `12`, degrades to `0` if the join breaks) is the one
that actually proves the join fired. Run both together. This capability exists
in minigraf and is already used internally by `mcp_server.py` (`_preload_known_deps`,
`_rebuild_index_from_graph`), but is not yet documented in `SKILL.md` — see #165.
An earlier version of this note incorrectly claimed no such mechanism existed and
shipped entries 5-6 as a weaker two-query valid-time-bracket workaround instead;
that was wrong and has been corrected here.

> **2026-08-07 — poller overhead in entries before this date (#242).** Every
> ingestion entry recorded before 2026-08-07 was measured by a harness whose
> in-flight poller ran a blocking, cost-growing graph query on the event loop
> every 0.5s, starving the ingestion it measured. Entry-to-entry comparisons
> remain valid — both sides carry the same instrument — but the absolute
> wall-clock figures, including the 78.87s forward-only baseline and the
> 1,600.55s post-#236 figure, overstate real ingestion cost by an unquantified
> margin. Entries from 2026-08-07 onward carry a "Poll duty cycle" row; treat
> its absence as "unmeasured, assume inflated".
>
> **The latency percentiles are biased the OTHER way, and by a different
> mechanism.** `_STATUS_QUERY` is unchanged across the fix, so the query being
> timed is the same one — but comparability of the query is not comparability of
> the percentiles. The post-fix poller sleeps `duty_factor × (status + query)`,
> so it undersamples exactly the late, expensive polls: `_STATUS_QUERY` counts
> every `:type/commit` entity, and its cost grows monotonically through a run.
> Pre-fix entries polled every 0.5s regardless and so sampled the expensive tail
> at full density. `query_latency` p50/p99 from 2026-08-07 onward are therefore
> biased **low** relative to every earlier entry. Wall-clock comparisons
> overstate the old runs; percentile comparisons understate the new ones. Do not
> read a p99 drop across 2026-08-07 as a speedup.

## Ingestion Run — 20260719T074053Z

- Repo: `.` @ `HEAD`

| Metric | Value |
|---|---|
| Commits ingested | 498 |
| Final status | complete |
| Wall-clock | 78.87s |
| Throughput | 378.9 commits/min |
| Peak RSS | 248528 KB |
| Graph size | 45801472 bytes |
| Fact-index size | 60080128 bytes |
| Status-query latency (min/p50/p99/max) | 0.0ms / 0.0ms / 0.0ms / 0.1ms |
| Graph-query latency (min/p50/p99/max) | 0.0ms / 35.0ms / 277.5ms / 305.0ms |

## Query Correctness Run — 20260719T081810Z

| ID | Category | Result | minigraf latency | baseline latency |
|---|---|---|---|---|
| 1 | point-in-time | PASS | 7.4ms | 2.8ms |
| 2 | delta | SKIPPED (manual diff) | 0.0ms | 0.0ms |
| 3 | regression-tracing | PASS | 2.1ms | 7.5ms |
| 4 | dependency-impact | PASS | 14.4ms | 5.4ms |
| 5 | cross-layer | PASS | 9.6ms | 6.5ms |
| 6 | cross-layer | PASS | 9.6ms | 3.0ms |

## Query Correctness Run — 20260719T082707Z

| ID | Category | Result | minigraf latency | baseline latency |
|---|---|---|---|---|
| 1 | point-in-time | PASS | 6.6ms | 3.1ms |
| 2 | delta | SKIPPED (manual diff) | 0.0ms | 0.0ms |
| 3 | regression-tracing | PASS | 2.0ms | 7.0ms |
| 4 | dependency-impact | PASS | 14.1ms | 5.6ms |
| 5 | cross-layer | PASS | 9.4ms | 6.8ms |
| 6 | cross-layer | PASS | 9.4ms | 3.1ms |

## Query Correctness Run — 20260719T183705Z

| ID | Category | Result | minigraf latency | baseline latency |
|---|---|---|---|---|
| 1 | point-in-time | PASS | 6.8ms | 3.1ms |
| 2 | delta | SKIPPED (manual diff) | 0.0ms | 0.0ms |
| 3 | regression-tracing | PASS | 1.8ms | 6.9ms |
| 4 | dependency-impact | PASS | 14.1ms | 6.1ms |
| 5 | cross-layer | PASS | 571.3ms | 6.0ms |
| 6 | cross-layer | PASS | 551.7ms | 2.9ms |

## Query Correctness Run — 20260719T184747Z

| ID | Category | Result | minigraf latency | baseline latency |
|---|---|---|---|---|
| 1 | point-in-time | PASS | 6.8ms | 3.1ms |
| 2 | delta | SKIPPED (manual diff) | 0.0ms | 0.0ms |
| 3 | regression-tracing | PASS | 1.8ms | 8.0ms |
| 4 | dependency-impact | PASS | 14.5ms | 5.3ms |
| 5 | cross-layer | PASS | 568.4ms | 6.2ms |
| 6 | cross-layer | PASS | 549.6ms | 2.9ms |

## Ingestion Run — 20260802T082540Z

- Repo: `.` @ `master`

| Metric | Value |
|---|---|
| Commits ingested | 553 |
| Final status | complete |
| Wall-clock | 5133.19s |
| Throughput | 6.5 commits/min |
| Peak RSS | 541132 KB |
| Graph size | 174772224 bytes |
| Fact-index size | 79183872 bytes |
| Status-query latency (min/p50/p99/max) | 0.0ms / 0.0ms / 0.0ms / 0.8ms |
| Graph-query latency (min/p50/p99/max) | 0.1ms / 12.2ms / 697.2ms / 956.8ms |

This is the post-#233 acceptance-gate run (Task 5, issue #233; see
`docs/superpowers/specs/2026-07-31-reverse-walk-write-amplification-design.md`).
Compared to the pre-fix phase-2d run against master at `bbe7fee`, which was
**killed incomplete after 62 minutes having reached ~100 of ~552 commits**
(measured at 3,152 tx/commit average, 263x the 12 tx/commit forward-only
baseline): this run **completes**, which the pre-fix run did not. That is
the real, headline improvement.

Against the 2026-07-19 forward-only baseline (498 commits, 78.87s, 378.9
commits/min, 45,801,472 B) — not an apples-to-apples comparison, since this
run does strictly more work (Stage A writes provisional lineage the baseline
never wrote, and Stage B re-parses) and ingests 553 commits, not 498:

- Wall-clock is 5,133.19s against 78.87s — **65x**. The design spec's stated
  bar was "completes in a time of the same order as the baseline rather than
  a different one." **That bar is not met.**
- Graph size is 174,772,224 B against 45,801,472 B — **3.8x**, which does
  fit the spec's "within a small multiple" bar.

Neither number is dominated by per-entity-per-commit scaling of the kind
#233 fixed — Task 5's `TestReverseApplyWriteBudget` isolates that axis
directly and shows flat, O(1) per-commit write cost as entity count varies
(see the commit message and the task-5 report for the counts). The
remaining 65x gap against the baseline is a real, unresolved cost — most
plausibly Stage B's re-parse and the two-stream interleaving overhead
inherent to concurrent forward+reverse ingestion, rather than anything this
task's regression test is positioned to catch. Left as a decision for a
human, not addressed further in this task.

**Answered by the 20260803T095104Z entry below (#236):** the gap was neither
Stage B's re-parse nor the interleaving overhead — it was the fact index's
delete path. 65x → 20.3x.

## Ingestion Run — 20260803T095104Z

- Repo: `/home/aditya/Work/AMC/Minigraf/temporal_reasoning` @ `master`

| Metric | Value |
|---|---|
| Commits ingested | 568 |
| Final status | complete |
| Wall-clock | 1600.55s |
| Throughput | 21.3 commits/min |
| Peak RSS | 552432 KB |
| Graph size | 188432384 bytes |
| Fact-index size | 83906560 bytes |
| Status-query latency (min/p50/p99/max) | 0.0ms / 0.0ms / 0.0ms / 0.2ms |
| Graph-query latency (min/p50/p99/max) | 0.1ms / 17.5ms / 1000.3ms / 1154.5ms |

This is the acceptance-gate run for issue #236 (fact-index delete by rowid;
Task 4, see `.superpowers/sdd/2026-08-03-fact-index-delete-by-rowid/`). It is
the direct answer to the open question the 20260802T082540Z entry above left
for a human, and it closes it.

- **Wall clock is 1,600.55s against the 78.87s forward-only baseline — 20.3x,
  down from 65x.** Normalising for commit count (568 vs 498) it is 17.8x per
  commit. Against the 5,133.19s pre-fix run on master it is 3.21x faster in
  raw wall clock and 3.29x per commit (2.818 s/commit vs 9.282 s/commit),
  while doing ~2.7% more work (master absorbed #233/PR #237 between the two
  runs, growing 553 → 568 commits). The spec projected 1,500–1,700s and
  "roughly 20x"; the measurement landed essentially at the band's midpoint,
  with no adjustment to either the result or the expectation. Two further
  instrumented runs of the same build corroborate it (1,622.50s and
  1,558.61s — ±2% around ~1,594s, all three `status=complete`).
- **The 65x gap was not Stage B's re-parse or two-stream interleaving.** The
  prior entry named those as "most plausible" and it was wrong. The cost was
  `fact_index.delete_facts` scanning the FTS5 table on every retracted
  triple: **all `_retract` fell from 4,137.8s / 73.5% of wall clock to 32.7s
  / 2.1%** (126x fewer aggregate seconds while making *more* calls — 88,917
  vs 79,414), and the residual `fact_index.delete_facts` cost is 8.79s across
  234,928 triples, 0.037 ms/triple.
- **`_candidate_diff_purge_legacy`'s O(N²) is confirmed gone.** A same-harness
  A/B (shipped rowid delete vs an in-process reimplementation of the pre-#236
  equality DELETE) at 500 / 2,000 / 8,000 records: legacy 0.995 → 3.372 →
  14.549 ms/record (14.6x growth across 16x N — super-linear), rowid 0.211 →
  0.241 → 0.297 ms/record (1.4x — flat). The legacy leg reproduces master's
  recorded 0.50 / 7.24 / 126.71s at 0.50 / 6.74 / 116.39s, so the harness is
  recreating the original conditions rather than a different workload.
- **The new dominant cost is graph reads, not writes.** `_db_execute`
  `(query ...)` is **1,328,250 calls / 523.02s / 33.6% of the run**,
  concentrated in `_correction_sweep_apply`'s inline `:introduced-by` /
  `:modified-in` lookups (258,074 calls, 255.48s, 16.4%) and
  `_entity_introduced_by_query` from `_reverse_apply` (464,377 calls,
  215.44s, 13.8%) — the same structural mistake #236 fixed in a different
  table, a per-item lookup issued in a loop where a set-at-a-time form
  exists. **Now tracked as #239.** 28.3% of wall clock remains unattributed
  (process-pool `_extract_commit`, orchestration, thread hops), which #233
  measured at ~312s across both stages — ~20% here, still below 33.6%.

The remaining 20.3x is therefore still above the design spec's "same order as
the baseline" bar, but it is no longer an unexplained gap: it has a named,
measured, filed successor (#239).

## No Ingestion Run — fix-235-two-value-introduced-by

**No acceptance-gate run was completed on this branch, and this entry is not
one.** Two attempts at the full-history acceptance benchmark on
`fix-235-two-value-introduced-by` were killed before completion — one at
3h54m (~1/3 complete), one at 36m. Neither produced a result worth recording
in the metrics-table format above, and no table is given here. The gate
should be re-run once #242 (below) is fixed.

- **The cause is the benchmark harness itself, filed as #242, not #235's
  code.** `_poll_during_ingestion` issues a blocking graph query on the
  event loop every 0.5 s, and `_STATUS_QUERY` counts every `:type/commit`
  entity — so the poll's own cost grows across the run. Late in a run it
  consumes most of the event loop and ingestion approaches a standstill.
  The second killed attempt shows the collapse directly: graph growth fell
  1.35 -> 0.405 -> 0.101 MB/min across three consecutive 15-minute windows,
  with the main process pinned at 99% CPU while all 8 parse workers sat
  idle at 0.5-1.1%. This implicates the two entries above this one, too: the
  78.87s and 1600.55s figures carry the same polling overhead, so
  entry-to-entry *comparisons* remain apples-to-apples (same harness bug,
  present throughout), but the *absolute* numbers in this file overstate
  real ingestion cost. **Hypothesis, not measured:** a feedback loop between
  rising poll-query cost and lock contention against the ingestion writer
  may explain why the two killed attempts on this branch diverged so
  sharply in wall clock (3h54m vs 36m) rather than failing at a consistent
  point.
- **What replaces it here: a two-revision A/B**, not a single-revision gate
  run, using `evals/at_scale/profile_forward_reconcile_attribution.py`
  (committed `f830285`), which drives the same `_run_ingestion` across
  Stage A and Stage B directly and does not poll. BEFORE = `20b9b38` (the
  prefilter still in place), AFTER = `7b08db6`. Both legs ingest the same
  root-anchored refs. On the 450-commit slice, the largest both revisions
  ran to completion: **720.57s BEFORE vs 741.85s AFTER, a ratio of 1.03**
  (1.01 at 150 commits, 1.03 at 300 commits). On the full 586-commit
  history, both capped at 1500s, AFTER is *ahead*: Stage A done at 619.9s
  vs BEFORE's 675.8s, and more Stage B work completed in the remaining
  budget. The single largest attributed delta is `_correction_sweep_apply`,
  +34.5s (131.8 -> 166.3s) over the same 225 calls, driven by `_retract`
  rising 53,567 -> 79,175 calls (+48%) — the sweep doing repair work the bug
  used to suppress — partly offset by `_reverse_apply` running 12.9s
  faster, netting +21.3s overall.
- **The hypothesis that motivated concern about #235 in the first place is
  disproved.** `_lineage_is_provisional` was expected to dominate the cost,
  since #235 makes it run per candidate ident instead of behind an
  in-memory prefilter. Measured directly
  (`evals/at_scale/bench_lineage_query_cost.py`, commit `7b08db6`): HIT
  costs 0.0514 / 0.0600 / 0.0597 ms and MISS costs 0.0459 / 0.0542 / 0.0528
  ms at 100k / 1M / 5M facts — a miss is *cheaper* than a hit, and neither
  scales with graph size. In the A/B above it accounts for 29.0s of a
  741.85s run. The follow-up hypothesis, `_forward_reconcile_provisional`,
  fires 281 times for 0.73s total — confirming the behavioural claim (0
  calls on BEFORE: the prefilter suppressed it entirely) while refuting the
  cost claim.
- **A separate finding, unrelated to #235, filed as #241:** `_db_checkpoint`
  is the largest single call site on *both* revisions in the A/B —
  371.0s vs 367.1s over the same 902 calls on the 450-commit slice, roughly
  0.69s per checkpoint, once per commit against a graph that grows the
  whole run. Likely missed by earlier per-call attributions because the
  per-thread cProfile hook used for them was killing the `write_executor`
  threads it was attached to.
