# Coalescing provisional intervals at load time (#329)

Status: design approved 2026-09-06. Successor to
`2026-09-04-frontier-interval-set-design.md` (#325) and
`2026-09-03-skip-fast-path-completion-witness-design.md` (#326).

## Problem

`frontier_registry._coalesce` runs only from `_extend`, and `_extend` runs only
from a claim. `FrontierAllocator.__init__` stores the intervals it is handed
verbatim. So if `_frontier_load` produces two provisional intervals that are
contiguous or overlapping **and the gap between the interval set and the rest of
the linearization is already empty**, no claim ever happens, nothing merges, and
both entities stay on disk for the life of the graph.

Verified against the shipped allocator:

```
contiguous loaded:  gap_empty=True  intervals=[Interval(0,10,'provisional',is_base=True,
                                                        ident=':ingestion/frontier-high'),
                                               Interval(11,25,'provisional',is_base=False,
                                                        ident=':ingestion/interval-provisional-abc')]
overlapping loaded: gap_empty=True  unclaimed=[]
```

`_intervals_read_extra` is then permanently non-empty, so
`_correction_sweep_select_position` and `_should_fold_lineage_watermark` both
return early on every subsequent run. Stage B never runs again,
`:ingestion/lineage-confirmed-through` never advances, and provisional
`:introduced-by` stays provisional for the life of the graph — on runs that
report `status: complete`.

### Why nothing catches it

The usual detectors are all blind to this state, for the same reasons recorded
throughout the #222 arc:

* `fact_audit`'s `divergence` reads 0 — graph and index agree; nothing is
  missing from either, the lineage is simply never upgraded.
* Both `:introduced-by` checks (`introduced_by_duplicates`,
  `entities_without_introduced_by`) only examine entities that EXIST and are
  well-formed; a provisional-forever `:introduced-by` is exactly one value on
  exactly one entity.
* `stderr_capture` has nothing to read — no commit is skipped, no write fails.
* `commit_census` compares commit counts, which are correct.

The failure profile — no self-heal, no detector, silent, permanent, on a run
reporting success — is the profile the #222 arc exists to refuse, which is why
this is fixed rather than accepted as a residual.

### Reachable path today

Gated behind `_skip_claim`, which #325 made vestigial in production, so this is
latent rather than live:

1. `frontier-high` retained `[0,6]`, an extra `T` retained `[11,25]`, positions
   7–10 covered by a loadable archived `:type/completed-region`.
2. Claims 10 → 8 skip and accumulate `skipped_span[T]`.
3. The claim at 7 merges `T` into `frontier-high` in memory **and is itself
   skipped**, so `_frontier_persist_claim` never runs and `T`'s facts are never
   retracted.
4. The fold moves the span onto `frontier-high`; with no floor set anywhere, the
   end-of-walk flush writes `_frontier_persist_span(frontier-high, 7, 10)` →
   `[0,10]`, contiguous with a still-live `T` at `[11,25]`.
5. Gap empty forever after.

## Scope

Three decisions were taken before design, and they bound this work:

1. **Coalesce only.** The load post-condition also covers "the base is the
   lowest provisional interval", but no repair code is written for a violation
   of it. Coalescing alone does not enforce that invariant — two *disjoint*
   intervals with a real gap between them never merge, and
   `_intervals_read_extra`'s query carries no positional predicate, so a
   below-base extra would load. Nothing produces that state today and it
   degrades conservatively (one more interval a caller re-walks or folds
   defensively, never one silently dropped), so it is made LOUD rather than
   repaired.
2. **Raise on adjacency, warn on base order.** See "Post-condition" below.
3. **Seed graph facts directly in the regression test.** See "Testing" below.

## Design

### 1. `frontier_registry.coalesce_intervals`

Extract the body of `FrontierAllocator._coalesce` into a module-level function:

```python
def coalesce_intervals(
    intervals: List[Interval], tag: str
) -> Tuple[List[Interval], List[Interval]]:
    """Returns (merged_full_list, absorbed)."""
```

`FrontierAllocator._coalesce` becomes a thin wrapper that calls it and assigns
`self._intervals`. The survivor rule is therefore **shared, not mirrored**: the
base wins if either participant is base, otherwise the LOWER one wins, and the
keeper's `anchor_pos` and `ident` travel with it. This is the whole reason for
approach A over "reimplement the merge inside `_frontier_load`" — the load-time
merge cannot drift from what a claim-time merge does, because it is the same
function.

The alternative of coalescing inside `FrontierAllocator.__init__` was rejected:
construction would silently mutate its input, many tests and call sites
construct allocators directly, and the absorbed set still has to escape for
persistence.

### 2. `_frontier_load`

Immediately after `_frontier_promote_base_if_missing` — the base must already
exist on disk and in the list, or the survivor rule has no base to keep and the
merge would re-point the fixed ident at a different entity:

```python
merged, absorbed = frontier_registry.coalesce_intervals(
    intervals, frontier_registry.TAG_PROVISIONAL
)
if absorbed:
    _frontier_persist_merge(db, linearization, merged, absorbed, run_ts_iso,
                            index_con=index_con)
    intervals[:] = merged
_frontier_check_load_invariants(intervals)
return frontier_registry.FrontierAllocator(len(linearization), intervals)
```

Only `TAG_PROVISIONAL` is coalesced. `_frontier_load` appends at most one
authoritative interval (`frontier-low`, read by its fixed ident), so there is
never a same-tag pair to merge on that side — and the authoritative/provisional
boundary must survive the two sides becoming adjacent, which `_coalesce`'s
same-tag filter already guarantees.

### 3. `_frontier_persist_merge`

Idents are resolved with `_interval_persist_ident` for both the survivors and
the absorbed intervals, so this agrees exactly with what the reverse walk's
write dispatch (`_reverse_claim_persist_target`) would resolve for the same
`Interval` — including the #325 Finding 3 case where a loaded extra's
`anchor_pos` fell back to `hi_pos` and re-deriving `_interval_ident` would mint
a different ident than the one on disk.

Order, and it is the same order and the same rationale as
`_frontier_persist_claim`'s absorb-then-extend:

1. **Discard every absorbed entity first.** Bounds are read from disk with
   `_frontier_read_bounds`; `None` means there is nothing on disk to retract
   (a phantom), which is skipped rather than treated as an error, matching
   `_frontier_persist_claim`. `pos_count` is read with
   `_frontier_read_pos_count` and passed through so the denominator is
   retracted with the rest — left behind it would attach to whatever interval
   the next write creates at that ident.
2. **Then widen each surviving provisional interval whose on-disk bounds differ
   from its merged span**, retracting only the moved bound(s) and reasserting
   them, with `_frontier_pos_count_delta` folded into the same write (different
   attributes, so minigraf#287 does not reach them).

A crash between the two steps leaves the absorbed span described by NOBODY: it
reads unclaimed and is re-walked by the next `_frontier_load`, losing nothing.
Widening first would instead risk the DUPLICATE outcome — the survivor already
claiming the merged span while the absorbed entity's now-redundant facts are
still live, invisible right up until the discard never runs, leaking a phantom
into `_intervals_read_extra` forever.

**Defensive guard:** if any provisional interval in the merge carries
`ident is None`, the coalesce is skipped entirely (no in-memory merge, no
write). `_load_one_interval` and `_frontier_promote_base_if_missing` always set
an ident on a loaded interval, so this is unreachable; the fail-safe direction
is to leave the graph exactly as found and re-walk, never to retract an entity
this function cannot name.

### 4. The survivor's `:pos-count`

The merged interval's `:pos-count` is the merged span, `hi_pos - lo_pos + 1`.

This has to be argued, not assumed, because it looks like the trap #326 already
paid for once — "a count computed where it is read discriminates nothing". It
is not that trap, and the difference is which run does the comparing:

* Both components were **validated against THIS run's linearization moments
  earlier**: `_load_one_interval` retains an interval only when its STORED
  claim-time count still equals its current span. Their adjacency was likewise
  established in this same linearization.
* The merged count is therefore a fresh assertion about run N — "as of this
  linearization, positions `[lo..hi]` are all complete" — which is exactly what
  the two validated components jointly assert.
* It is compared in run N+1, against a linearization that may differ. That is a
  real comparison across a real gap in time, so it discriminates. The archive
  case #326 got wrong was different: there, the archiving and the loading ran in
  the SAME run against the SAME linearization, so the count always agreed.

**Accepted cost, stated rather than discovered later:** merging is coarser than
keeping the components apart. If a later commit lands inside what used to be the
upper component, the count check now discards the whole union instead of that
component alone, and the whole union is re-walked. Bigger re-walk, never a loss.

**The `:pos-count` residual is unchanged.** It remains a CHECKSUM, not a proof
of set identity — equal count does not imply the same member set. Do not upgrade
that language to "sound"; the undemonstrated residual recorded in the #326
design applies verbatim to every interval this merge produces.

### 5. Post-condition: `_frontier_check_load_invariants`

Two checks over the returned provisional set, with deliberately different
consequences:

* **Adjacent or overlapping provisional intervals → raise.** After the coalesce
  above this is unreachable unless `coalesce_intervals` or the persist path is
  broken, so it should never fire. A raise from `_frontier_load` is caught by
  `_run_ingestion`'s run-level `except`: `status` goes to `error`, the traceback
  reaches fd 2 (so `stderr_capture`'s `error_signals` and
  `run_ingestion_benchmark._exit_code` fail the at-scale gate), and no walk has
  started, so nothing is half-written.
* **Base is not the lowest provisional interval → print a loud stderr line and
  continue.** Raising here would abort EVERY future run on such a graph forever,
  with no repair path (scope decision 1 declined to write one) — turning
  conservative degradation into a permanent denial of service, which is worse
  than the state being guarded against.

Cross-tag overlap is deliberately NOT checked. It is not reachable (`claim_low`
and `claim_high` are served from `_unclaimed`, the complement of the interval
set, so no claim can land inside an interval of any tag) and adding a third
raise widens the blast radius of a guard whose whole point is to be quiet.

## What this deliberately does not do

* No repair for a below-base extra (scope decision 1).
* No positional predicate added to `_intervals_read_extra`. Its permissiveness
  is documented as deliberate — an entity satisfying its three clauses is
  returned regardless of where its bounds sit, so callers degrade conservatively
  rather than silently dropping one. Narrowing it would change that trade.
* No change to `_skip_claim` or the archived-region machinery. #329 is a load
  contract defect; the skip path is only the currently-known route into it.
* No new at-scale gate. The load post-condition runs on every run, including
  every at-scale run, and reports through channels the nightly already reads.

## Testing

**`tests/test_mcp_server.py`** — seed a real graph directly with two provisional
interval entities and call `_frontier_load`. Two cases: contiguous
(`[0,10]` + `[11,25]`) and overlapping (`[0,10]` + `[5,25]`). Assert both sides:

* the returned allocator holds ONE provisional interval spanning the union, with
  `is_base` preserved on it;
* on disk, the absorbed entity's facts are gone (`_intervals_read_extra` empty),
  and the survivor's `:lo-hash`/`:hi-hash`/`:pos-count` describe the union;
* `_intervals_read_extra(db)` returns empty, which is the direct condition
  `_correction_sweep_select_position` and `_should_fold_lineage_watermark` both
  decline on. Assert that condition rather than the sweep's overall verdict —
  the sweep declines for seven different reasons and a test asserting only
  "it selected something" would pass or fail for reasons unrelated to #329.

Seeding rather than driving the issue's steps 1–5 is a deliberate choice: the
end-to-end route runs through `_skip_claim`, which #325 made vestigial (it needs
a loadable archived `:type/completed-region`, and after #325 that exists only on
the divergent-ref-regained path). A seeded test exercises the LOAD CONTRACT,
which is where the fix lives and where every future producer of this state
arrives — rather than pinning the fix to one narrow route through a mechanism
that may itself be removed.

**Ablation requirement.** Each of these tests MUST be run against the current
code and observed to FAIL before the fix lands, and the observed failure must be
the one the test names — not an unrelated error. A test that passes on master is
not a regression test for #329.

**`tests/test_frontier_registry.py`** — a unit test that
`coalesce_intervals` merges a loaded-but-never-claimed pair and reports the
absorbed interval, and that the survivor rule picks the base.

**Post-condition test** — `_frontier_check_load_invariants` raises when handed an
adjacent pair, and warns (does not raise) when handed a base that is not lowest.

## Migration and format version

**No `GRAPH_FORMAT_VERSION` bump, no migration step.** This changes no fact
shape: it retracts facts that already exist and widens bounds that already
exist, using the same attributes `_frontier_persist_claim` writes. A graph
carrying the #329 state is repaired the first time it is loaded by this code —
which is the one case in this arc that DOES self-heal, because the defective
state is by definition present at load time and the fix runs at load time.

Graphs predating #222's closure are still rebuilt rather than migrated, per the
standing decision; nothing here changes that.

## Documentation

A CLAUDE.md section in the #325/#326 sequence recording: the load-time
invariant and where it is enforced, the `:pos-count` argument in section 4
(specifically why it is not the "computed where it is read" trap), the accepted
coarsening cost, and the two different post-condition consequences. SKILL.md is
expected to be unaffected — no tool surface changes — but that is to be verified
against `mcp_server._TOOLS`, not assumed.
