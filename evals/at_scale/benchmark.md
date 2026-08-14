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
>
> **Cross-day full-history wall-clock is not reliable to better than tens of
> percent on this hardware (#241).** `20260807T125753Z` (626 commits,
> 3009.61s) and `20260809T042507Z` (629 commits, 1888.43s) both ingest the
> same `master` history, on the same machine, running the same pre-#241
> `mcp_server.py` — nothing in the code changed between them — yet disagree by
> ~59% per commit (4.808 s/commit vs 3.002 s/commit) purely from being taken
> two days apart. The `20260808T102652Z` / `20260809T042507Z` entries below
> show what a same-day, same-machine pair looks like instead (-22.9%, not
> the -51.9% the cross-day pair implied). Treat any entry-to-entry wall-clock
> comparison that spans different days on this box as **directional only**;
> a same-day A/B is required before trusting the magnitude of a percentage.

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

### Correction (2026-08-08, #241): `_db_checkpoint` was never named, and the attribution above cannot be reconciled

**Added while landing #241's checkpoint duty-cycle budget, after a four-leg
ablation (see `docs/superpowers/specs/2026-08-07-db-checkpoint-cadence-design.md`,
"Resolved" section) put `_db_checkpoint` at ~51% of wall clock on the same
kind of at-scale run this entry describes.** In the style of the revision
sections in `docs/superpowers/specs/2026-07-31-reverse-walk-write-amplification-design.md`:
the numbers above are **not rewritten**, because they cannot be independently
re-derived — this note records why, and what should not be trusted from them
going forward.

- **The attribution cannot be reconciled from surviving artifacts.** The
  33.6% / 28.3% breakdown above came from an ad-hoc harness that lived in
  `.superpowers/sdd/2026-08-03-fact-index-delete-by-rowid/`, which no longer
  exists — confirmed by directory listing, not assumed. No committed
  profiler from that era ran cProfile at all: `evals/at_scale/results/ingestion-20260803T095104Z.json`,
  the one surviving artifact from that run, carries only wall-clock,
  throughput, RSS, graph/index size, and latency percentiles — no per-call
  breakdown of any kind. There is no path from what survives back to how
  33.6% or 28.3% were computed.
- **The `_db_execute` figure predates #242** and therefore carries the old
  poller's query overhead: `_poll_during_ingestion`'s pre-fix form issued a
  blocking, cost-growing graph query on the event loop every 0.5s regardless
  of that query's own cost, serializing against `_db_native_lock` for a
  share of the run this file's own 2026-08-07 note already flags as
  unquantified. Whatever fraction of that 33.6% / 523.02s was this
  contention rather than genuine query cost is not separable after the fact.
- **`_db_checkpoint` was never named in the breakdown at all**, despite the
  #241 ablation putting it at ~51.6% of wall clock on a comparably-shaped
  run (330-commit slice, `normal2` baseline: 95.9s of 185.7s). The 28.3%
  bucket labeled "process-pool `_extract_commit`, orchestration, thread
  hops" was therefore far too small to be a residual once checkpointing is
  accounted for — the true unattributed-or-mislabeled share was closer to
  half the run, not under a third.
- **#239's 33.6% priority rests on this entry**, and this correction does
  **not** resolve that. `_db_execute` may still be the right thing to fix
  next, or it may not be, once the same run is re-measured with
  `_db_checkpoint` correctly named and post-#242 query costs isolated from
  poller contention — but that re-measurement is explicitly **out of scope
  for this branch** (see the design spec's "Follow-ups"). Flagged here so
  whoever next picks up #239 does not treat 33.6% as settled.

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

## Ingestion Run — 20260807T125753Z

- Repo: `.` @ `HEAD`

| Metric | Value |
|---|---|
| Commits ingested | 626 |
| Final status | complete |
| Wall-clock | 3009.61s |
| Throughput | 12.5 commits/min |
| Peak RSS | 611488 KB |
| Graph size | 212938752 bytes |
| Fact-index size | 87404544 bytes |
| Status-query latency (min/p50/p99/max) | 0.1ms / 0.4ms / 10.7ms / 32.2ms |
| Graph-query latency (min/p50/p99/max) | 0.7ms / 29.7ms / 2006.9ms / 2270.6ms |
| Poll duty cycle (#242) | 8.65% over 864 polls |

## Ingestion Run — 20260808T102652Z

- Repo: `.` @ `master`

| Metric | Value |
|---|---|
| Commits ingested | 629 |
| Final status | complete |
| Wall-clock | 1455.75s |
| Throughput | 25.9 commits/min |
| Peak RSS | 752092 KB |
| Graph size | 210333696 bytes |
| Fact-index size | 86487040 bytes |
| Status-query latency (min/p50/p99/max) | 0.1ms / 0.3ms / 4.7ms / 30.7ms |
| Graph-query latency (min/p50/p99/max) | 0.7ms / 15.0ms / 1043.9ms / 1379.4ms |
| Poll duty cycle (#242) | 6.31% over 1076 polls |
| Checkpoint duty cycle (#241) | 4.58% over 98 checkpoints (66.52s total, 845 suppressed) |

This is the at-scale acceptance run for **#241** (the checkpoint duty-cycle
budget; design/plan under `docs/superpowers/specs/2026-08-07-db-checkpoint-cadence-design.md`,
branch `perf-241-checkpoint-cadence` at `0eef8b6`, which carries the full
Task 1-6 stack: the gated wrapper, the Stage B dedup, the final checkpoint on
every terminal path, and this task's realised-duty statistics).

**Load sampling for this specific run is incomplete, and that gap is
disclosed rather than papered over.** The run that produced these numbers
was started under this task, but the interactive shell that launched it was
torn down before it finished; a completed run was recovered from a detached
relaunch (`ingestion-20260808T102652Z.json`, completing at 15:56:52 IST /
10:26:52 UTC after the recorded 1455.75s wall clock, i.e. starting ~15:32:26
IST). The last `uptime` this session captured before that start, at 15:31:06
IST — about 80s earlier, before the detached relaunch — read `load average:
3.43, 2.47, 1.50` (1/5/15 min), with no single non-self process above ~3% CPU
at that moment. **No sample close to the actual 15:56:52 IST completion was
taken** — the run continued outside this session's active polling, and nothing
recorded between then and this report being written (next day) is
representative enough to report as an "after" figure. This is a real gap
against the "sample load before and after" instruction, not satisfied by
substituting a stale reading; recorded here as missing rather than invented.
This machine is a shared development workstation, not a dedicated bench rig,
and was not necessarily idle throughout — the pre-run sample above already
shows load above 3, one contributor being an unrelated full test-suite run
in this same session shortly before.

**Ingested ref, for the record.** `_default_git_branch` pins ingestion to the
repo's stable `main`/`master` branch regardless of what is checked out (#130),
and no `--branch` was passed, so this run walked literal `master` at 629
commits — three ahead of the comparison run below, since three more commits
landed on master between the two measurements. The *code* that ran the
ingestion is this branch's checkout (`mcp_server.py` at `0eef8b6`, with the
full checkpoint-cadence stack), not master's own pre-#241 `mcp_server.py` --
"Repo: `.` @ `master`" describes the commit history that was ingested, not
the code that ingested it.

**Comparison baseline: `ingestion-20260807T125753Z.json` (626 commits,
3009.61s, complete), same machine, same repo, three commits earlier on
master, already recorded above as this file's immediately preceding entry.**
That run predates every commit on this branch (`perf-241-checkpoint-cadence`
branches from master `68ddb9d`, which already contains the commit recording
that entry) and its `branch` field is literally `HEAD`, which at the time it
was taken coincided with master's tip — it is the honest "before" state:
same hardware, same repo, post-#242 poller fix on both sides, adjacent commit
counts. The CI run cited in the design/plan docs (31182651935: 629 commits,
2323.0s, poll duty 8.56%) is **different hardware** and kept here only for
continuity; it is directional, not the number this entry's speedup is
computed against.

- **Per-commit wall clock, same-day controlled A/B (629 vs 629 commits,
  resolved 2026-08-09 — see the reconciliation below): 3.002 s/commit ->
  2.314 s/commit, a -22.9% reduction.** (1888.43/629 = 3.0023 vs
  1455.75/629 = 2.3144; (1888.43-1455.75)/1888.43 = 22.91%.) This entry
  originally led with a cross-day comparison against `20260807T125753Z`
  (4.808 s/commit -> 2.314 s/commit, -51.9%, of which only ~39-42% could be
  attributed at the time). That figure is **kept below, not deleted** — it
  is now understood to have been inflated by a stale cross-day baseline, not
  by an unexplained mechanism. Raw wall clock: same-day, 1888.43s -> 1455.75s
  (-432.68s); cross-day (as originally reported), 3009.61s -> 1455.75s
  (-1553.86s).
- **Realised checkpoint duty: 4.58%, under the 5% budget**, over 98
  checkpoints totaling 66.52s of the run's 1452.27s policy-tracked window
  (845 further calls suppressed). This is the number #241's acceptance
  criterion asked for and the number the design's budget arithmetic
  predicts: close to, and safely under, the configured `duty=0.05`.
  **Caveat on completeness:** this total excludes the mandatory unconditional
  final checkpoint(s) `mcp_server.py:10340` and `:10358` as they stood at this
  run's commit (`0eef8b6`) — Task 3's outer unconditional checkpoint plus a
  since-removed duplicate on the `completed_all` path, which this fix wave's
  own review found structurally identical to the Stage B duplicate Task 2
  removed and deleted (current HEAD keeps only the outer one). Both call
  `_db_checkpoint` directly rather than going through the policy specifically
  so they can never be suppressed — by the design's own framing this is
  "policy.checkpoints", not "every checkpoint in the run". At this run's
  ~210MB final graph size the ~5.1ms/MB scaling law bounds each omitted call
  at roughly ~1.1s, negligible against the measured 66.52s and the 1455.75s
  wall clock, but it is a real, if small, gap in what this row accounts for.

**Resolved (2026-08-09): a same-day, same-machine A/B closes the gap — the
cross-day baseline was inflated, not the checkpoint-cadence win overstated.**
`ingestion-20260809T042507Z.json` (entry below) reruns the identical
629-commit `master` history on this same machine, this same day, in a
detached worktree checked out to master `68ddb9d`'s own pre-#241
`mcp_server.py` (verified: no `_CheckpointPolicy`, no `checkpoint_summary` in
its output) — the "same-day controlled A/B" the paragraphs below say was
missing. It completed in **1888.43s** against this branch's **1455.75s** on
the same 629 commits: **3.002 s/commit -> 2.314 s/commit, a controlled
-22.9%** (1888.43/629 = 3.0023, 1455.75/629 = 2.3144,
(1888.43-1455.75)/1888.43 = 22.91%). That lands within ~1.6 points of both
the design spec's controlled 330-commit slice (185.7s -> 140.2s, -24.5%) and
its ablation-predicted `noop` ceiling (185.7s -> 140.8s, -24.2%) — three
independent measurements agreeing to within noise. **The mechanism delivers
what the ablation predicted.** The -51.9% headline was a real, honestly
measured cross-day number, but the cross-day baseline it used
(`20260807T125753Z`, 3009.61s, 626 commits) was itself ~59% slower per
commit than master is on this same hardware today (4.808 vs 3.002 s/commit)
for reasons unrelated to #241 — see the reconciliation attempt immediately
below, kept for the record with its resolution threaded through it rather
than rewritten.

**The paragraphs immediately below are the original reconciliation attempt,
written before the same-day A/B existed — retained rather than deleted or
silently rewritten, per this branch's own standard for measurement
corrections (see the 20260808T102652Z-adjacent correction to the
20260803T095104Z entry above).** The design spec's
"How much is on the critical path" section (the `normal2`/`noop` legs, pre-
dedup, pre-duty code, 330-commit slice) put the *recoverable ceiling* for
suppressing checkpoints entirely at ~24.2% of wall clock — suppressing
recovered 44.9s of a 185.7s baseline. This run's per-commit time fell 51.9%
-- more than double that ceiling. Worked with actual arithmetic, not
asserted:

- The design spec's own call-count decomposition (`902 calls = 225 forward +
  225 reverse + 225x2 Stage B(duplicated) + 2` for a 450-commit slice, and
  the identical ratio for the 330-commit slice's 662) gives, for N commits
  under the **pre-dedup** cadence (forward + reverse + 2x Stage B + 2):
  `K_old = 2N + 2`. For this run's N=629, `K_old = 1260`. The **post-dedup**
  form (`1.5N + 2 = 945.5`) predicts 946 against this run's own measured 98 +
  845 = **943** gated attempts — a 0.3% match, which is good evidence the
  scaling assumption is sound enough to extrapolate from.
- The design spec's scaling probe gives **~5.1 ms per MB** of graph size
  (corrected from an earlier ~4.9 ms/MB fit that used the wrong batch-column
  numbers — see the design spec's "Cost is linear in graph size" section),
  flat in dirty bytes. Approximating checkpoints as evenly spaced across a
  graph growing ~linearly from 0 to this run's final 210.33 MB, the average
  outstanding size is ~105.17 MB, so each checkpoint costs an estimated
  ~536 ms at this run's scale. `K_old x 536ms = 1260 x 0.5364s ~= 675.9s` of
  estimated old-cadence checkpoint cost, against the here real, measured
  66.52s — an estimated **~609.4s** reduction.
- Against the measured **1553.86s** total wall-clock reduction, that is
  **~39.2%** — confirming the *direction* of the graph-size-scaling
  hypothesis (checkpoint cost is O(graph size), so its cost fraction is
  necessarily larger on a 210MB run than on whatever smaller graph the
  330-commit ablation slice reached, and the realised win should exceed that
  slice's ceiling) but **not closing the gap to 51.9%**. Using the design
  spec's own cross-check anchor instead of the nominal rate (it recorded a
  126MB graph costing 690ms/checkpoint against the model's ~643ms prediction,
  a ~7% undershoot, i.e. the nominal rate is a soft floor) raises the
  estimate to ~725.9s old-cadence cost, ~659.4s reduction, **~42.4%** of the
  total saving. Neither reaches half.
- **~58-61% of the observed 1553.86s reduction (roughly 894-944s) is
  therefore left unexplained by the checkpoint-cost mechanism under this
  model, and is reported as unexplained rather than attributed.** Candidate,
  non-exclusive, unmeasured contributors: the ~5.1ms/MB rate was fit at
  <=15.73MB and linearly extrapolated ~13x to this run's scale, and the
  126MB/690ms anchor already shows real cost undershooting the linear model
  in the same direction, so per-checkpoint cost may be more super-linear at
  full scale than either estimate captures; the two compared runs were taken
  a day apart on a shared, non-dedicated development machine whose load this
  session's own samples show swinging between 0.27 and 3.89, and neither
  run's filesystem-cache state was captured; and no controlled same-day,
  same-machine, old-code-vs-new-code back-to-back A/B was run to isolate the
  checkpoint change from ordinary run-to-run variance or from other code
  differences between master `68ddb9d` and this branch's HEAD (e.g. Task 3's
  unconditional final checkpoint itself running on every path now, where
  before it may not have on an interrupted run). **The -51.9%/commit figure
  is the real, honestly measured observation. The ~39-42%
  checkpoint-attributable estimate above is a partial, order-of-magnitude
  sanity check on direction, not a full accounting of the remaining ~58-61%,
  which should not be attributed to this change without a same-day
  controlled A/B that was not performed here.**

  **Superseded: that same-day controlled A/B has now been performed** (see
  the "Resolved (2026-08-09)" note above and the `20260809T042507Z` entry
  below). It measures a controlled -22.9%, not -51.9%, which is within ~1.6
  points of both this section's own ~39-42% attribution estimates and the
  design spec's ablation ceiling below. There is no large residual left to
  attribute: the ~58-61%/894-944s "unexplained" figure above was a property
  of comparing against an inflated cross-day baseline, not evidence of a
  second, unidentified mechanism. It is retained above as the historical
  reconciliation attempt, not as a current estimate of what remains
  unexplained.

  **A same-day controlled A/B also already existed at smaller scale, in the
  design spec, not this run.** Its "How much is on the critical path" ablation
  held everything but `_db_checkpoint` fixed: same 330-commit slice at
  `82aa7e6c`, same machine, same harness, master `68ddb9d`'s
  checkpoint-every-commit baseline (`normal2`, 185.7s) against a leg with
  checkpointing suppressed entirely (`noop`, 140.8s) — a controlled
  **-24.2%**. That lands almost exactly on this branch's own duty-gated
  `every25` leg's predicted ceiling (-21.1%). The `20260809T042507Z` /
  `20260808T102652Z` pair above extends this same kind of comparison from a
  330-commit slice to the full 629-commit run this entry is about, and gets
  the same answer within noise: -22.9% against this slice's -24.2%, not the
  cross-day pair's -51.9%.

**The flaky `Page N out of bounds` write failure (Task 5's follow-up)
recurred.** One commit was skipped: `[_run_ingestion] skipping commit
a1c4a5f777643c9f78238d1c86347c318db14f92 (...): write failed: msg='Page 280
out of bounds (total pages: 280)'`, caught and isolated by the per-commit
write try/except exactly as designed (`mcp_server.py:10198-10218` as of this
branch's HEAD — shifted from the `:10168-10186` the design spec and Task 5
cite, since this task's own earlier commit on this branch added lines above
it; same block, same behaviour) — the run proceeded and still reported
`status=complete`, 629/629 commits attempted.
Stage B's correction sweep separately logged **8 entities left
provisional/unreconciled** at the end of the run (3 with ambiguous
`:introduced-by` values, 5 left provisional against `:commit/26565abdf1f7`).
As in Task 5's occurrence, no query was run against the resulting graph to
check whether those 8 entities were ever correctly reconciled, so this is
*no data loss observed in run status; final-state reconciliation not
independently verified*, not "nothing was corrupted" — and, also as in Task
5, causation between the write failure and checkpoint deferral remains
unestablished, not ruled out. This is the second observed occurrence on this
branch; still not reproduced on demand, still tracked as a follow-up, not
addressed by this task.

**Do not read the graph-query p99 change (2006.9ms -> 1043.9ms) as a clean
2x latency win.** Both runs are post-#242, so this comparison is more
defensible than one crossing the 2026-08-07 poller-fix boundary — but the
two runs recorded different poll counts (864 vs 1076), so they sampled the
query-cost curve at different densities. And the checkpoint-cadence change
this task measures is **not** latency-neutral here: `_db_execute` and
`_db_checkpoint` both serialize on the same `_db_native_lock`
(`mcp_server.py:3288` and `:3294`), so a poll's graph query can block behind
an in-flight checkpoint. Removing ~1,160 checkpoints' worth of lock-holding
(the pre-dedup `K_old ~= 1260` estimate above, at ~536ms each on this run's
scale) necessarily reduces how often a poll's query queues behind one, which
supplies a named mechanism for part of both the p99 drop and part of the
unattributed wall-clock residual above. Treat the *magnitude* as directional
at best — the differing poll density and sample size mean this is not a
controlled measurement of the effect — but the *direction* is expected, not
incidental.

**A further confound sits inside the polling instrument itself.** The two
runs' poll duty-cycle rows above give total poll-query time as duty x
wall-clock: 8.65% x 3009.61s = 260.3s over 864 polls (mean 301ms/poll) for
the comparison baseline, versus 6.31% x 1455.75s = 91.9s over 1076 polls
(mean 85ms/poll) for this run — a **168.4s** difference. That is 10.8% of
the observed 1553.86s wall-clock saving, sitting in the measurement
instrument rather than in ingestion itself: fewer, cheaper polls this run
means less time the ingestion loop spent yielding to a poller, independent
of anything #241 changed. It is plausibly part of the same lock-contention
mechanism above (cheaper checkpoints -> less time a poll's query waits on
`_db_native_lock` -> a cheaper mean poll -> a smaller duty-cycle sleep budget
consumed), which would make it a second-order consequence of #241 rather
than a wholly separate confound, but that chain is not independently verified
here and is reported as a candidate, not a settled attribution.

**Reframed after the same-day A/B (2026-08-09):** this 168.4s figure was
computed against the cross-day comparison baseline (`20260807T125753Z`) and
is best read as one measured, named contributor to *why* that cross-day
comparison overstated the win (-51.9% vs the same-day, controlled -22.9%),
not as an unresolved gap inside an otherwise-unexplained result. It is not
recomputed against the same-day pair here — the same-day control run's own
poll duty (8.43% over 693 polls) sits close to the cross-day baseline's
8.65%, well above this branch's 6.31%, so some version of the same
lock-contention effect plausibly still contributes a smaller amount to the
same-day -22.9%, but quantifying that was not part of this correction and is
left for whoever next revisits poll/checkpoint lock interaction.

## Ingestion Run — 20260809T042507Z

- Repo: `.` @ `master` (`68ddb9d`, full history, detached worktree)

| Metric | Value |
|---|---|
| Commits ingested | 629 |
| Final status | complete |
| Wall-clock | 1888.43s |
| Throughput | 20.0 commits/min |
| Peak RSS | 605140 KB |
| Graph size | 211824640 bytes |
| Fact-index size | 86999040 bytes |
| Status-query latency (min/p50/p99/max) | 0.1ms / 0.3ms / 10.5ms / 29.6ms |
| Graph-query latency (min/p50/p99/max) | 0.8ms / 21.3ms / 1122.8ms / 1249.7ms |
| Poll duty cycle (#242) | 8.43% over 693 polls |

**This is a control run of master `68ddb9d`, not a release measurement.**
It carries no checkpoint-cadence row because it ran none of #241's code: it
was launched in a detached worktree checked out to master's own commit
`68ddb9d`, prior to any commit on `perf-241-checkpoint-cadence` — verified
directly rather than assumed, by inspecting that worktree's `mcp_server.py`
for `_CheckpointPolicy` (absent) and by inspecting this run's own result
JSON for a `checkpoint_summary` field (also absent, unlike the
`20260808T102652Z` entry above). Its sole purpose is to supply the same-day,
same-machine "before" half of a controlled A/B against that entry: same
machine, same day (2026-08-09), same input (`master`'s history through the
same tip, 629 commits both sides — three more than `20260807T125753Z`
walked, since `20260808T102652Z` and this run were both taken after those
three additional commits landed). Result file:
`evals/at_scale/results/ingestion-20260809T042507Z.json`.

See the "Resolved (2026-08-09)" note under the `20260808T102652Z` entry
above for the resulting comparison: **1888.43s here vs 1455.75s there on the
same 629 commits, a controlled -22.9% per-commit reduction (3.002 s/commit
-> 2.314 s/commit)**, agreeing within ~1.6 points of both the design spec's
controlled 330-commit slice (-24.5%) and its ablation-predicted `noop`
ceiling (-24.2%). This resolves the "no same-day A/B" gap that entry's own
reconciliation flagged, and supersedes that entry's cross-day-derived
-51.9%/"~58-61% unexplained" framing as the number to trust for the size of
#241's effect — without deleting either figure, per this branch's own
standard for measurement corrections.

## Point-Query Cost Bench — 239-introduced-by-query-cost

- Repo: `.` @ `bench-239-introduced-by-query-cost`
- Script: `evals/at_scale/bench_introduced_by_query_cost.py`
- Raw: `evals/at_scale/results/239-introduced-by-query-cost.json`
- Question: is per-ident point-query cost constant in graph size? #239's fix
  direction depends on the answer.

| Metric | Value |
|---|---|
| Verdict | FLAT |
| Threshold (fixed in the spec before any data) | 2.0x |
| Worst growth | 1.11x on `filler:is_live_miss` |
| Control reproduced | yes (ceiling 5.0 ms/call misconfiguration tripwire: OK; growth 1.14x < 2.0x flatness check: OK) |
| `_entity_ident_is_live` HIT, 100k → 5M | 0.0544 → 0.0579 ms/call |
| `_entity_ident_is_live` MISS, 100k → 5M | 0.0499 → 0.0556 ms/call |
| `_entity_introduced_by_query` HIT, 100k → 5M | 0.0547 → 0.0602 ms/call |
| `_entity_introduced_by_query` MISS, 100k → 5M | 0.0527 → 0.0584 ms/call |
| Ident-population axis, worst growth | 1.11x |
| Batch crossover N (vs 1265 candidates/commit) | 126–140 (5M → 100k filler) |
| E4 real-scale point (~68 MB graph, 250k filler) | `_entity_introduced_by_query` HIT 0.0597 ms/call |

**What this means for #239:**

1. **Direction is settled.** All eight measured series are flat (worst growth
   1.11x against a 2.0x threshold, fixed before any data existed) across both
   a 50x filler range and a 50x ident-population range. The cost is CALL
   COUNT, not per-call growth: a per-commit batch or write-through cache is
   the right fix. The crossover sits at 126–140 point queries per batch call
   against 1265 candidates/commit, so a per-commit batch pays for itself
   roughly 9-10x over. A companion index (the #152/#236 pattern) is NOT
   indicated here — that pattern applies when per-call cost grows, and it
   does not.

2. **The size of the prize is now in doubt.** #239's own D2 table records
   `_entity_introduced_by_query` at 0.46 ms/call, and the whole query line
   item at 523s / 33.6% of a 1,558s run. This bench measures the same call at
   ~0.06 ms/call — about 7.7x lower. At that rate the same ~1.33M queries
   cost roughly 80s, not 523s: the realistic ceiling on fixing #239 is
   single-digit percent of a run, not a third of one. #239's headline figure
   should be re-derived from this bench before anyone invests in building the
   batch/cache fix.

3. **The superlinear growth seen elsewhere is NOT these queries.** The #245
   acceptance run's per-commit cost climbed 1.39 → 4.29 s/commit across ~520
   commits, but `_entity_ident_is_live` and `_entity_introduced_by_query` are
   flat here across a 50x graph-size range and a 50x ident-population range.
   Whatever drives ingestion cost up with history, it is something other than
   these two queries, and fixing #239 will not touch it.

## Description Preload Exposure Probe — 257-description-preload-exposure

- Repo: `.` @ `master` (`1dceec1`, full history, 656 commits) — the head is
  taken from the linearization, i.e. the commit the swept graph actually ends
  at. An earlier version of this entry named `8310f7d`, a commit on the probe
  branch that is not on `master` at all: `sweep()` was recording `git rev-parse
  HEAD` instead. Fixed in the probe; the artifact was regenerated.
- Script: `evals/at_scale/probe_description_preload_exposure.py`
- Raw: `evals/at_scale/results/257-description-preload-exposure.json`
- Question: does `_preload_known_entities`' date-bounded `:description`
  seeding (`entity_descriptions[ident]` = whichever version was live at
  `:valid-at T_hi(W)`) ever return a value a position-bounded query would
  not? #257's stated mechanism.
- Prediction, fixed in the design spec before any data existed: **census
  zero, mismatches zero.**

| Metric | Value |
|---|---|
| Exit code | 0 (valid measurement) |
| `:description` facts (deduped) | 2737 |
| Structurally affected watermarks (W) | 16 of 656 |
| W actually mismatching | 0 |
| **Stage 1 — census (idents with >1 distinct `:description` value)** | **3 total** |
| &nbsp;&nbsp;module | 0 of 55 |
| &nbsp;&nbsp;function | **3 of 2030** |
| &nbsp;&nbsp;class | 0 of 284 |
| &nbsp;&nbsp;variable | 0 of 227 |
| &nbsp;&nbsp;field | 0 of 62 |
| &nbsp;&nbsp;external-dependency | 0 of 76 |
| Stage 2 — value mismatches, position-weighted | 0 |
| Stage 2 — value mismatches, distinct idents | 0 |
| Stage 2 — value comparisons actually made | 12685 |
| **Stage 2 — of those, on a census offender** | **0** |
| Ambiguous idents (not the finding) | 15 |
| Preloaded but not live (not the finding) | 0 |
| Live but not preloaded (not the finding) | 677 |
| Timestamp collisions | 0 |
| Unmappable `:description` valid-from / valid-to | 0 / 0 |
| Preload descriptions empty everywhere | False |
| Gitlink events | 0 |

**Verdict: the census half of the prediction is FALSIFIED; the mismatch half
is UNTESTED where it mattered — not confirmed.** The mismatch count is 0, but
zero of those comparisons were on an ident that could have produced a mismatch
(see item 2), so the 0 is not a null result. This is Outcome 2 of the design
spec's three-way read, not Outcome 1 — reported as a real finding, not folded
into a close-oriented "prediction holds" framing.

1. **Three `function` idents carry two distinct `:description` values each,
   contradicting the spec's premise that description is a deterministic
   function of ident for this type:**

   | Ident | Values |
   |---|---|
   | `:function/tests-test-mcp-server-py-commit` | `_commit`, `commit` |
   | `:function/tests-test-mcp-server-py-snapshot` | `_snapshot`, `snapshot` |
   | `:function/evals-at-scale-profile-forward-reconcile-attribution-py-main` | `_main`, `main` |

   Root cause, confirmed by reading `_canonical_ident` (`mcp_server.py:4090-4099`):
   the slug step replaces every non-`[a-z0-9-]` character (including `_` and
   the `::` name separator) with `-`, then collapses consecutive hyphens. A
   name with a leading underscore (`_commit`) and its bare form (`commit`)
   both slug to the identical `...-commit` suffix once the run of hyphens
   from `::_` collapses — the ident scheme is underscore-blind at a name
   boundary. `tests/test_mcp_server.py` defines many distinct local helper
   functions named `commit`/`_commit` and `snapshot`/`_snapshot` nested
   inside different test functions (confirmed by grep: 10+ occurrences of
   `def _commit`/`def commit` alone), and `_code_ident` does not qualify by
   enclosing scope — so unrelated nested functions across the file collide
   onto one ident in addition to the underscore collapse. Either mechanism
   alone would be enough to break the "description is a deterministic
   function of ident" premise for `function`; here both are present.

2. **Stage 2's zero is zero out of ZERO informative comparisons, and settles
   nothing about the mechanism.** The shipped `_preload_known_entities` was
   driven against the position-correct oracle at all 16 affected watermarks and
   12685 value comparisons were made, but **not one of them was on a census
   offender** (`offender_value_comparisons: 0`). A mismatch is only recordable
   for an ident carrying at least two distinct values across time — i.e. a
   census offender — so `value_mismatches` is always a strict subset of item
   1's three idents. Those three were excluded at every position where the
   question could have been put:

   | Positions | Offenders in the preload | Disposition |
   |---|---|---|
   | 118–128 (11 W) | 0 of 3 | not preloaded at all — none of the three functions existed yet |
   | 645–649 (5 W) | 3 of 3 | **both values simultaneously live → classified `ambiguous`, comparison skipped** |

   So the probe *declined to compare* at 645–649; it did not find agreement
   there. Stage 2 therefore **neither confirms nor refutes** #257's mechanism
   at those positions. What Stage 2 does establish is narrower and still worth
   having: across the 12685 comparisons it did make — the whole preloaded
   population at every affected watermark — the shipped query's output never
   diverged from the position-correct oracle for any ident where a single
   correct value existed. That is a real check on the query, not evidence about
   the three idents that could actually exhibit the defect.

   The exclusion is not incidental, either. The ambiguity rule removes exactly
   the case where the mechanism is most likely to fire: two competing versions
   contemporaneous at a position is both what makes the oracle unable to name a
   single right answer and what gives a date bound a wrong version to pick. The
   probe now prints this as a NOTE and records `compared_idents`,
   `offenders_present` and `offenders_compared` per position, so the limitation
   is checkable from the artifact rather than by re-derivation.

3. **The submodule `external-dependency` arm is UNMEASURABLE on this
   history, not zero.** `gitlink events: 0` — this repository has no
   `.gitmodules` changes, so the one entity type whose `:description` is
   read from `.gitmodules` (`name or path`, independently variable from the
   ident-bearing path) rather than derived from the ident never gets a
   chance to exercise the mechanism. The `external-dependency` row above
   (`0 of 76`) reflects an absence of opportunity, not an absence of risk.
4. **Whether #257's per-attribute interval-inversion fix is justified is
   left open, not answered, by this run.** The census result shows the
   ident-determinism premise is false for at least three real idents in this
   repository (for reasons unrelated to #257's stated `entity_descriptions`
   mechanism — a slug-collision bug in `_code_ident`/`_canonical_ident`, not
   a `:valid-at` date-vs-position bug), while the mechanism #257 actually
   describes was never put to the test on those three idents at all (item 2).
   This run neither bounds the risk nor demonstrates it. It is one repository,
   and on that repository the only candidates were unmeasurable — which is
   weaker than "one data point against"; it is not a data point about the
   mechanism at all. The two arms it does close off are narrower: the shipped
   query agreed with the oracle on 12685 unambiguous comparisons, and no
   entity type other than `function` has an ident carrying two values.

**Resolution (2026-08-14): #257 CLOSED as an accepted residual, not fixed, and
item 4 above is what closed it — it was never answered.** The interval-inversion
fix is not built. Two corrections to how the arms were described, both verified
against the shipped code:

- **The submodule arm is remove → re-add at the same path, not a rename.**
  `_gitlink_changes` classifies from the gitlink tree entry's modes and never
  reads `.gitmodules`; `"bump"` writes `:pinned-commit` + `:modified-in` only.
  A name-only `.gitmodules` edit produces no event and rewrites no
  `:description`. Item 3's "independently variable from the ident-bearing path"
  is right about the value and wrong about what triggers a second write.
- **The three `function` collisions in item 4 were fixed by #263 (rule R3), but
  the ARM is not closed.** These descriptions are pre-slug raw values, so
  ident → description is injective only if the slug is, and R3's zero is
  measured over 674 commits rather than proven: `a/b.py` and `a-b.py` both reach
  `:module/a-b-py`. #267 is the missing census over history written under R3.

Both arms additionally need a **close-then-reintroduce**, since every
`entity_descriptions` write site is guarded by "ident not currently live". The
standing record is `_preload_known_entities`' docstring and the strict xfail in
`TestPreloadKnownEntitiesDescriptionValueIsDateBounded`, which fails the suite if
anyone fixes the preload — so this measurement is not the only thing keeping the
mechanism visible.

## Ident Collision Census — 263-ident-collision-census

- Repo: `.` @ `master` (`14166d1`, full history, 674 commits)
- Script: `evals/at_scale/probe_ident_collision_census.py`
- Raw: `evals/at_scale/results/263-ident-collision-census.json`
- Run: `.venv/bin/python evals/at_scale/probe_ident_collision_census.py
  --repo-path . --json-out evals/at_scale/results/263-ident-collision-census.json`
- Question: how many `_code_ident` values on real history are reachable from
  more than one distinct `(entity_type, file_path, name)` input? #257's census
  observed three such collisions from the graph side; #263 asks for the true
  count and for what a fix would cost.

**On the head commit.** `head_commit` is `14166d1`, mainline's tip — *not* the
tip of the `audit-263-ident-collision-census` branch this probe was written on.
That is deliberate, not a stale run: `collect_inputs` resolves an unspecified
branch through `mcp_server._default_git_branch`, which returns `master` here, so
the measurement counts the idents mainline history produces and excludes the
probe being added. `branch` and `head_commit` are both recorded in the artifact
and both printed by the CLI so a run is never ambiguous about what it read.

**Why source-derived and not graph-derived.** A graph-side census structurally
cannot answer this. When two entities collide, the loser takes
`_build_code_triples`' already-known branch, which appends `:modified-in` and
nothing else — no `:entity-type`, no `:ident`, no `:description`, no `:file`.
The graph holds no record of the losing input's `(file_path, name)` pair at all.
The three collisions #257 saw were visible only because those entities were
closed and reopened, which re-runs the introduction branch. Any graph-side count
is a bound; only the inputs give an exact one.

| Metric | Value |
|---|---|
| Exit code | 0 (valid measurement) |
| Commits walked | 674 |
| Extraction failures | 0 |
| Distinct inputs collected | 2789 |
| Distinct idents | 2780 |
| Ignore patterns applied | `3rdParty/`, `third_party/`, `vendor/`, `node_modules/`, `dist/`, `build/`, `*.min.js`, `*.map` |
| **Offenders (idents reachable from >1 distinct input)** | **9 of 2780 (0.32%)** |
| &nbsp;&nbsp;module | **1** of 133 |
| &nbsp;&nbsp;function | **7** of 2058 |
| &nbsp;&nbsp;class | **1** of 290 |
| &nbsp;&nbsp;variable | 0 of 232 |
| &nbsp;&nbsp;field | 0 of 67 |

**The `module` row pools three producers.** `:module/` is one namespace shared by
in-tree files (`_code_ident("module", path)`), gitlink/submodule paths, and the
unresolved-import fallback `_canonical_ident("module", specifier)`, so the 133
module idents are not 133 files. Split by producer over the same head: **57 from
in-tree files, 76 from unresolved import specifiers, 0 from gitlinks** (this
repository has no submodules — see finding 4). No module ident is reached from
more than one producer, which is the same fact `cross-producer = 0` reports, so
the two counts partition the 133 exactly. Sizing migration cost from `module 133`
would over-count real file modules by more than 2×.

**By shape** (an offender may carry more than one label; `other` is emitted when
nothing *but* `cross-producer` applied — not when no label at all applied,
because `cross-producer` names which call sites the inputs came from and never
what made their values collide, so an offender carrying only that label is still
unexplained and belongs where an unpredicted family surfaces):

| Shape | Count |
|---|---|
| leading-underscore | 7 |
| case-only | 0 |
| separator-vs-path | 0 |
| cross-producer | 0 |
| other | 2 |

**The nine offenders in full:**

| Ident | Colliding inputs | Shape |
|---|---|---|
| `:module/mcp-server` | import `mcp.server`, import `mcp_server` | **other** |
| `:function/tests-test-mcp-server-py-commit` | `tests/test_mcp_server.py` `_commit` / `commit` | leading-underscore |
| `:function/tests-test-mcp-server-py-snapshot` | `tests/test_mcp_server.py` `_snapshot` / `snapshot` | leading-underscore |
| `:function/tests-test-mcp-server-py-boom` | `tests/test_mcp_server.py` `_boom` / `boom` | leading-underscore |
| `:function/tests-test-mcp-server-py-parse` | `tests/test_mcp_server.py` `_parse` / `parse` | leading-underscore |
| `:function/tests-test-mcp-server-py-results` | `tests/test_mcp_server.py` `_results` / `results` | leading-underscore |
| `:function/evals-at-scale-profile-forward-reconcile-attribution-py-main` | `evals/at_scale/profile_forward_reconcile_attribution.py` `_main` / `main` | leading-underscore |
| `:class/tests-test-mcp-server-py-fakedb` | `tests/test_mcp_server.py` `FakeDb` / `_FakeDb` | leading-underscore |
| `:function/tests-test-mcp-server-py-init` | `tests/test_mcp_server.py` `__init__` / `_init` | **other** |

**Candidate rules** — residual is offenders remaining under the rule; renames is
baseline idents that move, i.e. the cost the change imposes on every existing
graph:

| Rule | Residual | Renames | % of 2780 | Description |
|---|---|---|---|---|
| R1 | **0** | 2710 | 97.5% | keep underscores: charset becomes `[^a-z0-9_-]` |
| R2 | 1 | 2649 | 95.3% | no hyphen collapse: drop `re.sub(r'-+', '-', slug)` |
| R3 | **0** | 2721 | 97.9% | R1 + R2 |
| R4 | **0** | 2780 | 100.0% | hash suffix: current slug + `-` + `sha256(raw)[:8]` |
| R5 | 9 | 2647 | 95.2% | CONTROL, expected to still collide |

Rename cost by entity type, for the rules that reach zero residual:

| Rule | module | function | class | variable | field |
|---|---|---|---|---|---|
| R1 | 74/133 | 2049/2058 | 289/290 | 231/232 | 67/67 |
| R3 | 74/133 | 2058/2058 | 290/290 | 232/232 | 67/67 |
| R4 | 133/133 | 2058/2058 | 290/290 | 232/232 | 67/67 |

R2's split is recorded too, because P4 is ruled on it: module 2/133, function
2058/2058, class 290/290, variable 232/232, field 67/67. R5's: module 0/133,
function 2058/2058, class 290/290, variable 232/232, field 67/67.

**Predictions** (fixed before any data existed; a failure is a finding about the
design, not noise). All four held:

| ID | Outcome | Evidence, verbatim |
|---|---|---|
| P1 — "The true collision count exceeds the 3 #257's census found, because that census can only see collisions whose loser was closed and reopened." | **held** | `offenders_total=9` |
| P2 — "leading-underscore STRICTLY outnumbers every other named shape among all offenders." | **held** | `leading-underscore=7, runner-up separator-vs-path=0` |
| P3 — "R5, the control, reports a nonzero residual at least as large as the leading-underscore offender count." | **held** | `R5 residual=9, leading-underscore offenders=7` |
| P4 — "R2's rename cost falls on named entities, not on module idents: a module input has no `::` separator to produce an adjacent hyphen run, so R2 renames a strictly smaller FRACTION of module idents than of function idents." | **held** | `R2 renames module 2/133 = 0.015, function 2058/2058 = 1.000` |

Self-check (holds by construction; a failure would indict the scorer, not the
design, which is why it is reported apart from the predictions):

| ID | Outcome | Evidence, verbatim |
|---|---|---|
| S1 — R4 renames every ident, so its rename count must equal the ident total | **held** | `R4 renames=2780, idents_total=2780` |

**The control behaved.** R5 came back with residual 9 — every offender survives
it, because `_slug_current` strips leading hyphens, so `_commit` and `commit`
both reduce to `commit` under independent slugging. The CLI's `NOTE: R5` warning
did not fire. Had R5 reported clean while `leading-underscore` was nonzero, every
other row in the candidate table would have been suspect.

**Findings:**

1. **The count is 9, three times what the graph could see.** #257's graph-side
   census found 3; the input-side count is 9. All three of #257's are present
   (`:function/tests-test-mcp-server-py-commit`,
   `:function/tests-test-mcp-server-py-snapshot`,
   `:function/evals-at-scale-profile-forward-reconcile-attribution-py-main`),
   confirming the two measurements are of the same phenomenon and that the
   graph-side number is the bound the design spec said it was. The expected
   reason the graph missed the other six is the mechanism above — their loser
   was never closed and reopened, so the introduction branch never re-ran — but
   this run did not measure that: it never opened a graph, so "six" is a
   subtraction, not an observation. Nor is 9-vs-3 a controlled comparison: #257
   measured at 656 commits and this run at 674, so the two counts are at
   different heads and some of the gap could simply be history added since.

2. **`other` was not empty, and what landed there is a real finding.**
   `:function/tests-test-mcp-server-py-init` collides `__init__` with `_init`.
   The shape classifier's `leading-underscore` test compares names after
   `lstrip("_")` on the last dot-segment, so `__init__` → `init__` and `_init` →
   `init` are unequal and the pair falls through to `other`. It is still an
   underscore collision in substance — the trailing dunder underscores also slug
   to hyphens and get stripped — but it is a *dunder-versus-private* variant the
   predictions did not name. The classifier is not wrong; its
   `leading-underscore` label is narrower than the underscore family actually
   present on this history.

3. **One collision is not an underscore problem at all, and it crosses the
   internal/external boundary.** `:module/mcp-server` is reached from two
   unresolved import specifiers: `mcp.server` (the external `mcp` package's
   submodule) and `mcp_server` (this repository's own top-level module). `.` and
   `_` both slug to `-`, so an external dependency and an in-tree module land on
   one ident. This is the single offender R2 does not fix — dropping the hyphen
   collapse changes nothing when there is no hyphen run to preserve — and it is
   why R2's residual is 1 rather than 0. R1 fixes it (`mcp_server` keeps its
   underscore); so do R3 and R4. It is the **second** member of `other`, and the
   shape table now says so: both of its inputs are unresolved import specifiers
   with `name=None`, so there is no path/name boundary anywhere in the pair and
   `separator-vs-path` — which asks whether one input's *name* is a trailing
   whole path segment of the other's `file_path` — cannot apply to it.

4. **No cross-producer, no case-only and no separator-vs-path collisions on this
   history.** All three counts are 0. `cross-producer` being 0 does not mean the
   three producers sharing the `:module/` namespace is safe — it means this
   repository has no gitlinks at all (consistent with #257's `gitlink events:
   0`), so the code/gitlink arm of that risk was never exercised. Absence of
   opportunity, not absence of risk. `separator-vs-path` at 0 is a *weaker*
   result than it looks, for a structural reason: an ident carries its
   entity_type, so every member of an offender group shares one, and the pairs
   that most obviously satisfy the definition (a `module` at `src/auth/login.py`
   against a `function` `login` in `src/auth.py`) span two types and therefore
   can never be one group's members. The family is reachable within a single
   type — `src/utils_py/handlers::utils` and `src/utils.py::handlers.utils` both
   slug to `:function/src-utils-py-handlers-utils`, and the test suite pins that
   pair — but it takes a path shape this repository does not contain. Read the 0
   as "the `::` mitigation `_code_ident`'s docstring describes held on *this*
   history", not as "the mitigation is airtight".

5. **Every zero-residual rule is a near-total rename.** The cheapest one, R1,
   still moves 2710 of 2780 idents (97.5%); R3 moves 2721 (97.9%) and R4 moves
   all 2780. There is no candidate here that fixes the 9 while leaving the other
   2771 idents where they are. The per-type split shows why, and the two rules
   spare different things:

   - **R3** spares only module idents (74/133 renamed, 0 spared anywhere else).
     It drops the collapse, and every named ident's raw value carries a `::`
     whose adjacent hyphen run the collapse used to eat, so every function,
     class, variable and field ident moves. A module's raw value is a bare path
     with no `::`, so 59 of them are untouched.
   - **R1** spares those same 59 module idents *and* 11 named ones (function
     2049/2058, class 289/290, variable 231/232, field 67/67 → 9 + 1 + 1 + 0
     spared). R1 changes only the charset, and `::` slugs to `-` under both the
     current rule and R1, so a named ident whose path and name contain no
     underscore at all is untouched — `install.py::main` is the shape.

   The two together are 70 idents, exactly the 2780 − 2710 R1 does not rename.

**Conclusion: a fix needs a migration for existing graphs, not only a forward
change** — the cheapest rule that eliminates all 9 collisions (R1) still renames
97.5% of existing idents, so a forward-only change would orphan the history of
nearly every code entity in every graph already written.
