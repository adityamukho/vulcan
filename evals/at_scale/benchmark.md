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
