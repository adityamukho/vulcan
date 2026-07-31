# Reverse-Walk Write Amplification — Design Spec

**Issue:** #233 (blocking #222 phases 3–5)
**Date:** 2026-07-31

## Background

#222's phase 2 built a converging multi-stream ingestion: a forward-truth
stream that owns lineage correctness, and a reverse-bulk-fill stream (Stream
2) that provisionally back-fills recent history from `HEAD` downward so recent
commits become queryable early. Phase 2d (PR #230, merged) wired both into
`_run_ingestion` as Stage A, with a terminal correction sweep as Stage B.

Every test in phase 2 uses a fixture of 6–10 commits. At that size the walk's
per-commit cost is invisible. Against a real repository it is not: the at-scale
ingestion benchmark run against master at `bbe7fee` was killed incomplete after
62 minutes having reached 100 of ~552 commits, where the recorded 2026-07-19
forward-only baseline completed 498 commits in 78.87 seconds.

### Measurement

The issue established transaction amplification by inspecting the two benchmark
graphs (12 tx/commit baseline vs 3,152 tx/commit at 2d — 263x). This spec is
designed against a sharper measurement: `_transact` and `_retract` wrapped with
per-call-site counters and timers, driving `_reverse_fill_claim_and_process`
over the 12 commits below `HEAD` of this repository.

**12 commits, 532.1 s, 83,638 writes → 6,970 tx/commit.** (Higher than the
issue's 3,152 average over the first 100 commits: these tip commits touch both
`mcp_server.py` and `tests/test_mcp_server.py`, so ~1,265 candidate entities
per commit rather than the run-wide average.)

| call site | op | count | sec |
|---|---|---:|---:|
| `_re_date_structural_facts` | retract | 17,250 | **225.9** |
| `_candidate_diff_clear` | retract | 8,605 | **172.7** |
| `_entity_introduced_by_set_provisional` | retract | 8,618 | 55.1 |
| `_re_date_structural_facts` | transact | 17,250 | 15.4 |
| `_reverse_apply` (retroactive `:modified-in`) | transact | 10,161 | 10.9 |
| `_candidate_diff_persist` | transact | 10,105 | 9.6 |
| `_entity_introduced_by_set_provisional` | transact | 10,122 | 8.3 |
| `_lineage_mark_provisional` | transact | 1,504 | 1.1 |
| `_frontier_persist_claim` | both | 23 | 0.1 |

Reads, for completeness: `_entity_introduced_by_query` 30,061 calls / 4.4 s,
`_lineage_is_provisional` 27,358 / 2.8 s, `_candidate_diff_read` 18,723 / 2.2 s.

Three findings this spec is built on, none of which were available in the
issue:

1. **Retracts cost ~13 ms each; transacts cost ~1 ms.** 34,473 retracts account
   for 454 s of the 502 s spent inside write calls. Transaction *count* is the
   proxy the issue measured; **retract count is the actual cost driver**, and
   the two rank the fixes differently.
2. **`_candidate_diff_*` is 34% of wall time writing records nothing reads.**
   `_candidate_diff_read` has exactly two callers: `_candidate_diff_persist`
   and `_candidate_diff_clear` themselves. 2a specced these records so that 2c
   could confirm or reject a candidate "via hash comparison, without
   re-invoking git-show + tree-sitter parsing" — but 2c as built re-parses on
   the process pool and reads `precomputed["unchanged_idents"]`. The persisted
   body hash is never consulted by any production code path.
3. **The retroactive `:modified-in` loop issues one transact per entity**
   (10,161 over 12 commits). Facts differing in entity do not collide in
   minigraf's EAVT pending index — `_reverse_apply` already batches
   `authoritative_modified_triples` on exactly that reasoning — so these are
   batchable and currently are not.

Read against the profile, the issue's own diagnosis holds and is the largest
single lever (48% of wall time), but it is not sufficient on its own: with only
re-dating deferred, Stage A would still issue ~3,600 tx/commit against the
forward walk's 12, because every remaining per-entity write is still
per-entity-per-commit.

### The asymmetry being corrected

The forward walk sustains 12 tx/commit over the same ~1,265 candidate entities
because it decides "have I seen this ident before" from an in-memory
`entity_valid_from` dict and emits one batched transact per commit. The reverse
walk substitutes a DB query for that dict (correctly — it has no accumulated
forward state, and the gap allocator is resumed from persisted state, not
memory), but it also inherited a *write* shape of one-or-more calls per entity
per commit. The query substitution is sound and cheap (7.2 s of 532 s). The
write shape is the defect.

**Goal: Stage A's per-commit transaction count becomes O(1) in the number of
entities touched, matching the forward walk's cadence.** Per-entity structural
work does not disappear; it moves to a path that runs once per entity for the
whole region instead of once per entity per commit.

## Design

### 1. Re-dating moves from `_reverse_apply` to the correction sweep

`_re_date_structural_facts` is removed from `_reverse_apply`'s
`provisional_moves` loop entirely. `_correction_sweep_apply` calls it in case 1
(provisional, guess equals the commit being visited → confirm), immediately
before `_lineage_confirm`.

The sweep already carries the necessary data and discards it: `precomputed`
holds `module_candidate_triples` and `(ident, name, triples)` for each of
`function_entries`/`class_entries`/`global_entries`/`field_entries` — the same
source `_reverse_apply` builds `candidate_triples_by_ident` from — and the
sweep currently reads only the idents out of it. The sweep builds the identical
`candidate_triples_by_ident` map per file and passes
`[t for t in triples if ":introduced-by" not in t]` to
`_re_date_structural_facts`, matching both existing callers.

**Why this is sound.** The sweep's gap-closed precondition
(`frontier-low.:hi-hash` position `+ 1 == frontier-high.:lo-hash` position,
checked on every `_correction_sweep_claim_and_process` call) means
`claim_high()` can never return another position for the rest of the run, so
Stream 2's guess for every entity in frontier-high's range is final. Combined
with the sweep's ascending walk, the commit at which an entity reaches case 1
*is* its introduction. Re-dating there is once per entity for the whole region.
This is the same induction the 2c spec relies on to justify confirming at all —
deferring re-dating rides on an argument the design already makes, rather than
introducing a new one.

`_forward_reconcile_provisional` keeps its `_re_date_structural_facts` call
unchanged: that path already fires once per entity, at the forward walk's
arrival at the true introduction.

**Known, unchanged exposure.** `_re_date_structural_facts` retracts the triples
the *caller* computed, not the triples that are actually live. If the sweep's
parse at the introduction commit produced a structurally different triple from
the reverse walk's parse at the sighting commit, the retract would miss and the
transact would add, leaving a duplicate. In practice the re-dated attributes
(`:entity-type`, `:ident`, `:description`, `:file`/`:path`, `:contains`) are
functions of the ident and the containing module, which are stable for a fixed
ident. This exposure is pre-existing and identical in
`_forward_reconcile_provisional` today; this spec does not widen it and does
not fix it.

### 2. The candidate-diff path is deleted

Removed: `_candidate_diff_ident`, `_candidate_diff_persist`,
`_candidate_diff_read`, `_candidate_diff_clear`, the `:type/candidate-diff`
entity type, and all six call sites (`_reverse_apply` ×3,
`_forward_reconcile_provisional` ×1, `_correction_sweep_apply` ×2).

These were 2a infrastructure for an optimization 2c did not take. They are
recoverable from git history if a later phase wants them; carrying them costs
34% of Stage A's wall time and 22% of its writes to maintain records with no
reader.

**Migration.** Graphs written by 2d hold live `:candidate/…` records that
nothing will clear once `_candidate_diff_clear`'s call sites are gone, and
these records *are* indexed — `fact_index.py` filters nothing by prefix
(`_MEMORY_PREFIXES` affects scoring only, not inclusion), so they would remain
retrievable scratch noise indefinitely.

`_frontier_load` — already the one-time-migration site for the
watermark→interval and lineage-marker migrations, and already called on
`write_executor` with a live `db` and `index_con` for that reason — gains a
one-time purge: query every entity with
`[?e :entity-type :type/candidate-diff]`, retract each record's four facts in
one batched `_retract` call, and no-op when the query is empty. Records for
distinct `:candidate/…` entities do not share `(entity, attribute, valid_from)`
so the batch is collision-free.

The purge is unconditional rather than watermark-gated: it is a single query on
every load, cheap when there is nothing to purge, and gating it would require a
new watermark whose only job is to record that a one-time deletion happened.

### 3. Remaining per-entity writes are batched per commit

Ordered by retract count, since retracts dominate wall time.

**`_entity_introduced_by_set_provisional` becomes batch-first.** Its only two
production callers are the `new_candidates` and `provisional_moves` loops in
`_reverse_apply`; everything else that calls it is a unit test. So the batch
becomes the implementation —
`_entity_introduced_by_set_provisional_batch(db, idents, commit_ident,
commit_ts_iso, pos, pos_by_commit_ident, index_con)` — and the existing
per-ident function is kept as a one-element delegating wrapper. That preserves
`TestLineageProvisionalMarker`'s per-ident tests verbatim and makes it
structurally impossible for the batched and unbatched gates to drift apart.

The batch:

1. Classifies every ident first, applying the existing gates verbatim and in
   the same order — authoritative-skip (`current is not None and not
   _lineage_is_provisional`), the `current == commit_ident` no-op-but-still-mark
   case, and the monotonicity refusal with its stderr line (`a guess may only
   move earlier`).
2. Emits one `_retract` carrying every `[E :introduced-by old_E]` for the
   idents that genuinely move.
3. Emits one `_transact` carrying every `[E :introduced-by commit_ident]`.
4. Emits one `_transact` carrying the lineage-marker facts for every ident not
   already marked (three facts each, on distinct `:lineage/…` companion
   entities).
5. Returns the set of idents that actually moved, so the caller knows which
   supersede-side work applies.

All four batches are collision-free: within each, the facts differ in entity.
The monotonicity refusal is per-ident and must stay per-ident — batching must
not turn one refused move into a whole batch being dropped, or into a refused
ident being silently included.

**Retroactive `:modified-in` is grouped by superseded timestamp.** Each edge is
transacted at the *superseded* commit's own timestamp, not this commit's, so a
single batch is not possible. Grouping by `superseded_ts` and emitting one
transact per distinct value is: entities in the same file were almost always
last sighted at the same commit, so this collapses to a handful per commit
rather than one per entity. The `superseded_pos <= pos` refusal, the
`superseded_ident != commit_ident` guard, and the missing-timestamp skip with
its stderr line all stay per-ident, evaluated during grouping.

**`_lineage_confirm` in the sweep is batched per commit.** One `_retract`
carrying the marker facts for every entity confirming at that commit. The
per-ident `_lineage_confirm` is retained for its other callers.

**`:contains` re-dating stays one triple per `_retract`/`_transact` call.** The
issue comment proposed batching these "when the facts differ in entity"; they
do not differ in entity. `[module :contains fn]` has the *module* as its
entity, so batching a module's children into one call collapses them to the
last — precisely the minigraf#287 EAVT collision that cost five of six
containment edges in 2b1. Section 1 already reduces these from
O(entity-touches) to O(entities), which is what the forward walk pays for the
same edges.

### 4. The invariant that moves

This is the only observable behaviour change, and it is a real one.

Today, because re-dating is eager, "an entity live at its own introduction
carries its structure there too" holds after Stage A alone — this is what
`TestReverseFillValidTimeParity` asserts against `_reverse_bulk_fill_walk` with
no sweep. After this change it holds **after Stage B**. During Stage A a
provisional entity's structural facts are dated at its sighting commit, later
than its provisional `:introduced-by`, so `:as-of` the provisional introduction
reports the entity as nonexistent.

That is what provisional means, and the region is already excluded from 2a's
trust predicate — `lineage-confirmed-through` does not cover frontier-high, and
nothing may trust what Stream 2 wrote until the sweep completes (2c/2d
constraint 2). The `:parent` edges `_reverse_apply` writes already carry the
same "temporarily dangling, and convergent" shape, for the same reason.

What genuinely widens: an **interrupted** run now leaves a larger inconsistent
window than before, because the eager re-date was doing partial repair as it
went. The next run's sweep closes it, and the region was untrustworthy either
way — but this is a property a reader of `_reverse_apply` would otherwise have
to derive, so it is stated in `_reverse_apply`'s docstring, in
`_re_date_structural_facts`'s docstring (whose current text names
`_reverse_apply` as a caller), and in `_correction_sweep_apply`'s.

### 5. Expected result

Per commit, Stage A after this change: one transact for the batched structural
facts, one per `:contains` for genuinely-new entities only, one per parent
edge, one batched transact for authoritative `:modified-in`, one retract plus
two transacts for the batched provisional guess and markers, a handful of
transacts for grouped retroactive `:modified-in`, plus the frontier claim and
checkpoint — the forward walk's ~12 tx/commit cadence.

The sweep absorbs ~2 writes per entity **for the whole region**, plus one
`:contains` pair per entity, which is the same order the forward walk pays for
the same facts.

## Testing

Real-backend only, per `docs/testing-conventions.md`.

**The regression test #233 needed.** A budget test: commit a file with N
functions, commit it again with one function's body changed, run the reverse
walk over both, and assert the total `_transact` + `_retract` call count for
the second commit does not scale with N. Parameterized on N=5 and N=50; the
assertion is that the count for N=50 is within a small constant of the count
for N=5, not an absolute number. This is the shape of test that phase 2's
6-to-10-commit fixtures could never have caught, because they varied commit
count and never varied entity count.

**Moved.** `TestReverseFillValidTimeParity::test_structural_facts_are_re_dated_when_the_guess_moves_earlier`
currently runs `_reverse_bulk_fill_walk` alone and asserts `:entity-type` and
`:file` are live at the introduction timestamp. It is extended to run the
correction sweep before asserting — same assertions, later point in the
pipeline. A companion test asserts the negative during Stage A (structural
facts are *not* yet re-dated), so the moved invariant is pinned in both
directions rather than merely relaxed.

**New.** The sweep's case 1 re-dates structural facts. `_frontier_load` purges
pre-existing candidate-diff records, and no-ops on a graph containing none.
`_entity_introduced_by_set_provisional_batch` honours the monotonicity refusal
per-ident: a batch containing one ident whose guess would move *later* leaves
that ident alone and still applies the rest.

**Deleted.** `TestCandidateDiff` and `TestReverseFillCandidateDiffLifecycle`.

**Unchanged and must stay green.** Every class under `TestReverseFill*`,
`TestReverseApplySplit`, `TestReverseBulkFillWalk*`, `TestCorrectionSweep*`,
and `TestStageBCorrectionSweep`. The `:contains` edge tests
(`TestReverseFillContainsEdges`) are the guard against the batching in section
3 being over-applied.

## Acceptance gate

`.venv/bin/python evals/at_scale/run_ingestion_benchmark.py --repo-path . --branch master`
must complete, and is compared against the recorded 2026-07-19 forward-only
baseline: 498 commits, 78.87 s, 378.9 commits/min, 45,801,472 B graph.

Stage A now does strictly more work per commit than the forward-only baseline
did (it writes provisional lineage the baseline never wrote, and Stage B
re-parses), so parity with 78.87 s is not the bar. The bar is that total
ingestion completes in a time of the same order as the baseline rather than a
different one, with graph size within a small multiple of 45,801,472 B — and
specifically that neither number scales with entity *touches*.

The instrumented harness used to produce the profile above is retained under
`evals/at_scale/` so the per-call-site attribution can be re-run rather than
re-derived.

## Explicitly not in scope

- **Caching `_entity_introduced_by_query` in-run** (issue suggestion 3). The
  profile shows 30,061 calls costing 4.4 s of 532 s. It is 0.8% of the problem
  and would introduce an in-memory cache that must stay coherent with a DB the
  sweep also writes. Revisit only if it becomes visible after this fix lands.
- **Why `_retract` costs 13x a `_transact`.** That is a minigraf-side question
  and may be worth its own issue; this spec routes around it by not issuing
  34,473 retracts.
- **Phase 3 (tip-liveness), 4 (status/observability), 5 (hardening).**
  Unblocked by this work, not part of it.
