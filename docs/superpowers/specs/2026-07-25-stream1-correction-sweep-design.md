# Stream 1 Correction Sweep — Design Spec

**Issue:** #222 (Phase 2, sub-phase 2c of 4)
**Date:** 2026-07-25

## Background

#222's overall design is a converging multi-stream ingestion: a forward-truth
stream (the existing `_run_ingestion` engine) running concurrently with a
reverse-bulk-fill stream that provisionally back-fills recent history from
`HEAD` downward, so recent history is visible almost immediately while the
forward stream still owns lineage correctness.

- **Phase 1** (merged, PR #226) built `frontier_registry.py`
  (`Interval`/`FrontierAllocator`/`build_linearization`) plus
  `mcp_server.py`'s `_frontier_load`/`_frontier_persist_claim` — the shared-gap
  allocator both streams claim work from.
- **Phase 2a** (merged, PR #227) built the provisional/authoritative fact
  model primitives: `_lineage_mark_provisional`/`_lineage_confirm`/
  `_lineage_is_provisional` (a companion `:type/lineage-marker` entity per
  tracked entity), `_lineage_confirmed_through_query`/`_update`/`_migrate`
  (a watermark for "is region X's lineage fully confirmed"), and
  `_candidate_diff_persist`/`_read`/`_clear` (per-`(commit, entity)`
  body-hash records so Stream 1 can later confirm/reject a candidate by hash
  comparison instead of re-parsing).
- **Phase 2b** (PR #228, not yet merged) built Stream 2's actual
  reverse-bulk-fill walk (`_reverse_fill_claim_and_process`/
  `_reverse_bulk_fill_walk`), claiming positions via `allocator.claim_high()`
  and writing provisional `:introduced-by` + candidate-diff records for
  every entity it discovers, moving the guess earlier whenever it finds an
  earlier occurrence of the same entity.
- **2c (this spec)** — Stream 1's correction sweep: walks **upward**
  (oldest→newest) through frontier-high's own already-claimed interval —
  starting at its current `:lo-hash` and proceeding toward `HEAD` — using a
  dedicated watermark for its own resumable progress (**not**
  `:ingestion/lineage-confirmed-through`, and **not** `allocator.claim_low()`;
  see "Why not `claim_low()`" and "Why a dedicated watermark" below),
  converting the provisional facts 2b wrote into authoritative ones as it
  reaches them, using 2a's persisted candidate diffs for cheap replay
  instead of re-parsing.
- **2d** — the actual concurrency wiring inside `_run_ingestion`: two
  asyncio tasks sharing the frontier allocator and the existing single
  write_executor, with fairness so neither starves.

Like 2a and 2b, this sub-phase adds functions with **no caller wired into
`_run_ingestion` yet** — 2d ties everything together into the real commit
loop. This branch stacks on top of 2b's (`design-222-phase2b-reverse-bulk-fill-walk`,
PR #228, not yet merged) since 2c calls 2b's `_entity_introduced_by_query`
and depends on 2b's exact candidate-diff/provisional-marker write pattern.

## Scope (2c only)

In scope:

- A per-commit correction step that advances a dedicated position tracker
  upward through frontier-high's own claimed interval — starting at its
  current `:lo-hash` on first use, proceeding toward `HEAD` — and, for every
  entity touched by that commit's "A"/"M" files, reconciles its lineage per
  the two-case algorithm below.
- A thin driving loop that repeats the above until the tracker reaches the
  end of the linearization. Frontier-low is never touched by this sweep,
  and neither is `:ingestion/lineage-confirmed-through` (see "Why a
  dedicated watermark" below) — 2c introduces its own new watermark,
  `:ingestion/correction-sweep-through`.
- Opportunistically clearing stale intermediate candidate-diff records left
  behind along a supersession chain (2a's spec flagged this explicitly as
  "2c's job").
- A guard against re-asserting `:modified-in` at an entity's own
  introduction commit on a resumed/re-run sweep (see "Per-commit algorithm").

Explicitly deferred:

- `:depends-on` edges, deletions ("D"), and renames ("R"). The existing
  `_run_ingestion` loop already has full support for these; this sweep
  reuses the same `_extract_commit` entity-discovery path 2b used and keeps
  the same `if status not in ("A", "M"): continue` guard.
- **The "wrong provisional guess" reconciliation case** — an entity whose
  *true* earliest occurrence lies below frontier-high's own claimed range
  (in the still-unclaimed gap, or in frontier-low's territory) while Stream
  2 guessed some commit *within* its own range as the introduction (a
  rename- or rebirth-spanning-the-gap scenario). An earlier draft of this
  spec included a reconciliation case for this inside the correction sweep
  itself; that is wrong and has been removed — see "Why a dedicated
  watermark, not `lineage-confirmed-through`" below for the proof that a
  provisional guess pointing
  anywhere other than the commit currently being visited cannot occur
  *within* frontier-high's own already-claimed territory, walking
  oldest-to-newest. The genuine reconciliation belongs to whichever walk
  processes the entity's true earlier occurrence — an amendment to ordinary
  forward-walk introduction logic (checking for and correcting a
  pre-existing provisional guess when it discovers what turns out to be an
  even-earlier occurrence), or a dedicated concern of 2d. Left as an
  explicit open item for 2d, not resolved here.
- Wiring into `_run_ingestion` / real concurrency (2d).

## Design

### Why not `claim_low()`

An earlier draft of this spec used `allocator.claim_low()` to drive the
sweep. That is wrong, and worth recording why: `claim_low()` and
`claim_high()` partition a *single shared gap* — each call extends its own
side's interval by exactly the position at that side's edge
(`frontier_registry.py`'s `gap_lo`/`gap_hi`/`_extend`), and a position
absorbed into one side's interval is never again returned by the other
side's claim method. Once Stream 2 has claimed a position via
`claim_high()`, that position is permanently part of the high (provisional)
interval; `claim_low()` will only ever hand Stream 1 positions strictly
below the current gap, which by definition Stream 2 has never touched.
There is no sequence of `claim_low()` calls that returns a position Stream
2 already claimed — the two streams race to shrink the *same* gap from
opposite ends, they do not hand off territory to each other.

The positions this sweep needs to revisit are exactly the ones **already
claimed** by Stream 2 (tagged provisional, `:introduced-by` facts already
written), which lie in the frontier-high interval, not the gap. So the
sweep needs its own position tracker, entirely independent of the
allocator.

This does mean 2c is *not* a drop-in replacement for `claim_low()` as
Stream 1's single per-commit call site — 2d will need to run both: ordinary
`claim_low()`-driven introduction for genuinely virgin territory below the
gap, and this sweep for already-claimed territory above it. That's an
honest reflection of the allocator's actual semantics rather than a
convenience this spec can assume away.

### Why a dedicated watermark, not `lineage-confirmed-through`

A second earlier draft of this spec bounded the sweep as `(lineage-
confirmed-through, frontier-high.lo-hash]` — i.e. lower bound exclusive at
`lineage-confirmed-through`, upper bound inclusive at frontier-high's
current `:lo-hash`. That is also wrong, in two compounding ways:

1. **The range pointed at the wrong territory.** `:ingestion/lineage-
   confirmed-through` is seeded from frontier-**low**'s `:hi-hash`
   (`_lineage_confirmed_through_migrate`, mcp_server.py:5145-5151) and, per
   that draft, was advanced by nothing except this sweep. Frontier-high's
   `:lo-hash` is the *bottom* of Stream 2's claimed interval — the boundary
   closest to the gap, not deep inside claimed territory. Everything
   strictly between those two watermarks is the shared **gap**: positions
   *no* stream has claimed or processed at all. A range bounded this way
   spans the gap and stops at the single position where frontier-high's
   claimed territory begins, rather than walking through that territory.
2. **The bound was backwards even ignoring (1).** The positions where "this
   entity already has a fact from Stream 2" actually holds are
   `[frontier-high.lo, N-1]` (`N-1` = the linearization's last position,
   i.e. `HEAD`) — frontier-high's `:lo-hash` is the correct **floor**, not
   a ceiling approached from below. `claim_high()` only ever moves
   frontier-high's `:lo-hash` *down*, extending the interval; its `:hi-hash`
   is always the fixed top of the linearization (`_extend`'s `from_low=False`
   branch never touches `existing.hi_pos`). So once Stream 2 has claimed
   anything at all, positions `[current lo-hash, N-1]` are *all* already
   claimed and safe to walk — contiguously, with no further bounds-checking
   needed once the walk starts.

Because of (2), the sweep's own progress cannot be tracked by
`lineage-confirmed-through` at all: that watermark specifically means "the
region *contiguous from `C0`* is fully confirmed" (2a's trust predicate,
below), and folding this sweep's progress into it before the gap is
actually closed would assert lineage is confirmed through commits nothing
has ingested yet. So 2c introduces its own watermark,
`:ingestion/correction-sweep-through`, tracking only "how far *this sweep*
has walked through frontier-high's own territory," with no claim about
contiguity from `C0`. Folding the two watermarks together once the gap
closes (so they describe one contiguous confirmed region again) is 2d's
job, once it can also make the *ordinary* `claim_low()`-driven walk advance
`lineage-confirmed-through` for the virgin positions it claims.

**This is also what proves case 2 from an earlier draft (a provisional
guess pointing at a commit other than the one currently being visited)
cannot occur inside this sweep's range.** For a commit `C` Stream 2
claimed, Stream 2 parsed the identical diff (the same invariant the
per-commit algorithm below relies on) and — per its own per-commit
algorithm — always moves a candidate's guess *down* to the earliest
occurrence it has seen. So for any entity `C`'s files touch, Stream 2's
final persisted guess is necessarily `<= C`'s position, never later. Walking
`[frontier-high.lo, N-1]` oldest-to-newest, by induction: the sweep reaches
each entity's true within-range-earliest occurrence *before* any later
commit that also touches it, because that earliest occurrence's position is
itself `<= C` for every later `C` — so by the time the sweep would ever
visit a later `C` for that entity, the entity has already been confirmed
(case 1, below) at its true earliest position and reads as authoritative,
not provisional. A guess pointing at some *other*, unvisited commit while
still provisional is therefore not a state this sweep's own walk can ever
observe for a commit inside its range — see the "Explicitly deferred"
bullet above for where that reconciliation actually belongs.

### Per-commit algorithm

For a commit `C` at the sweep's current position (oldest→newest through
frontier-high's territory), reusing `_extract_commit` for entity discovery
(direction-agnostic, same as 2b) but **not** `_build_code_triples`:

**2c never writes `C`'s own `:type/commit` entity, and never calls
`_build_code_triples`.** Every commit in the sweep's range was already
claimed by 2b's `claim_high()` and fully processed by
`_reverse_fill_claim_and_process`, which writes its claimed commit's
seven-attribute metadata block unconditionally (mcp_server.py:7321-7329)
and, for every entity an "A"/"M" file touches, writes either full
structural triples (first sighting) or nothing but `:introduced-by`
(2b filters `:modified-in` out of `_build_code_triples`'s own output and
owns that timing itself) — so by the time this sweep visits `C`, every
candidate entity ident it will find is *already* structurally complete and
already has an `:introduced-by` fact. Calling `_build_code_triples` here
would therefore be dead code: for a *known* entity it emits nothing but
`:modified-in` (mcp_server.py:6505-6562), which this sweep would then have
to filter back out anyway, exactly as 2b does for `:introduced-by`. So this
sweep skips straight to per-ident classification, using `precomputed`
directly:

```python
candidate_idents = (
    [precomputed["module_ident"]]
    + [ident for ident, _name, _t in precomputed["function_entries"]]
    + [ident for ident, _name, _t in precomputed["class_entries"]]
    + [ident for ident, _name, _t in precomputed["global_entries"]]
    + [ident for ident, _name, _t in precomputed["field_entries"]]
)
unchanged_idents = precomputed.get("unchanged_idents", set())
```

Each candidate ident then falls into one of two cases. Every write below
uses `C`'s own `commit_ts_iso` (the same timestamp 2b itself used when it
first wrote at `C`) — this equality is load-bearing, not incidental: it is
what makes any write this sweep re-issues at a position 2b already touched
land as an idempotent no-op under minigraf's real write semantics (an
identical `(entity, attribute, value, valid_from)` tuple is a no-op; only a
*differing* `valid_from` produces a second live datom) rather than a
duplicate.

1. **Provisional, guess matches this commit**
   (`_lineage_is_provisional(db, ident)` and
   `_entity_introduced_by_query(db, ident) == commit_ident`) — per "Why a
   dedicated watermark" above, this is the *only* state a provisional
   entity can be in in this sweep's range once the sweep reaches it: Stream
   2's guess is guaranteed to already equal its true within-range-earliest
   occurrence by the time this sweep, walking the same order, gets there.
   Call `_lineage_confirm(db, ident, index_con=index_con)` (drops the
   marker; the `:introduced-by` fact itself is untouched, since its value
   was already correct) and `_candidate_diff_clear(db, commit_hash, ident,
   index_con=index_con)`. No `:modified-in`.

2. **Already authoritative** (confirmed at an earlier position in this same
   sweep, or — after this sweep has fully caught up once — simply revisited
   on a later run) — first check whether `C` *is* this entity's own
   introduction commit: if `_entity_introduced_by_query(db, ident) ==
   commit_ident`, skip entirely (no `:modified-in`, no candidate-diff
   touch). This guard matters for resume-safety: if the process dies after
   case 1's `_lineage_confirm` but before `_correction_sweep_through_update`
   persists, a re-run revisits `C` and would otherwise find the
   now-authoritative entity at its own introduction commit and wrongly
   assert a self-`:modified-in` — a fact class ordinary forward walk never
   produces (`_build_code_triples` only emits `:modified-in` on its
   already-known branch, never at introduction). Otherwise: assert
   `[ident :modified-in commit_ident]` at `C`'s own `commit_ts_iso`, unless
   `ident in unchanged_idents` (#221, unmodified this commit). Either way,
   opportunistically call `_candidate_diff_clear(db, commit_hash, ident,
   index_con=index_con)` if a record happens to exist for this exact
   `(commit, ident)` pair — this is what cleans up the *intermediate* stale
   records 2b's own spec left behind along a supersession chain (guess
   moves `L3 → L2 → L1 → C`; case 1 clears `C`'s own record when the sweep
   reaches `C`; each of `L1`/`L2`/`L3` falls into this case when the sweep
   later reaches them, and each still has its own now-orphaned
   candidate-diff record from when it was a transient guess). No
   special-casing was needed for that cleanup — it falls out of the
   ordinary path once the sweep passes back over those commits.

### Position tracker: `:ingestion/correction-sweep-through`

A new watermark, structurally identical to `:ingestion/lineage-confirmed-
through` (`_lineage_confirmed_through_update`'s exact shape: registered
`:type/ingestion`, same retract-only-if-changed pattern, same required
`:description` constant) but tracking a different thing — this sweep's own
progress, independent of contiguity from `C0`:

```python
_CORRECTION_SWEEP_THROUGH_IDENT = ":ingestion/correction-sweep-through"

def _correction_sweep_through_query(db: Any) -> Optional[str]:
    """Return the hash of the last commit this sweep has itself confirmed/
    corrected, or None if it has never successfully processed one yet."""

def _correction_sweep_through_update(
    db: Any, commit_hash: str, commit_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Record the last commit this sweep processed. Mirrors
    _lineage_confirmed_through_update's retract-only-if-changed pattern
    exactly, at a different ident."""
```

Position selection, handling both ends being possibly absent or stale:

```python
high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
if high_bounds is None:
    return None  # Stream 2 hasn't claimed anything yet -- nothing to correct
hash_to_pos = {h: i for i, h in enumerate(linearization)}
through_hash = _correction_sweep_through_query(db)
if through_hash is not None and through_hash in hash_to_pos:
    pos = hash_to_pos[through_hash] + 1
else:
    # Unset (first-ever call), or a stale hash from rewritten/rebased
    # history -- (re)start from frontier-high's current lo-hash, mirroring
    # _frontier_load's own precedent of dropping a bound that no longer
    # resolves (mcp_server.py:4970-4978) rather than erroring.
    lo_hash, _hi_hash = high_bounds
    if lo_hash not in hash_to_pos:
        return None  # frontier-high itself is stale; nothing safe to do
    pos = hash_to_pos[lo_hash]
if pos >= len(linearization):
    return None  # reached HEAD; nothing left to correct
commit_hash = linearization[pos]
commit_ts_iso = next(ts for h, ts, _a, _s in commit_metadata if h == commit_hash)
```

Once `pos` is known to be `>= frontier-high.lo`'s position at the time this
call started, the walk needs no further bounds-checking against
frontier-high for the rest of its run: `[frontier-high.lo, N-1]` only ever
grows (frontier-high's `:hi-hash` is fixed at `N-1`, and `:lo-hash` only
moves further down), so every position from the sweep's own resume point
onward through `N-1` is guaranteed already claimed regardless of how far
Stream 2 has additionally progressed since.

After processing `C`: `_correction_sweep_through_update(db, commit_hash,
commit_ts_iso, index_con=index_con)`, then one `_db_checkpoint(db)` call —
same cadence as 2b. Frontier-low and `:ingestion/lineage-confirmed-through`
are never touched by this sweep (see "Why a dedicated watermark" above for
why folding into the latter is explicitly deferred to 2d).

### Driving functions

```python
def _correction_sweep_claim_and_process(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> Optional[str]:
    """Advance the correction sweep by exactly one commit, upward through
    frontier-high's own claimed territory, and reconcile that commit's
    entities per the two-case algorithm above. Returns the processed
    commit's hash, or None if there is nothing left to correct (frontier-
    high hasn't claimed anything yet, or the sweep has already reached
    HEAD). No `allocator` parameter -- this reads frontier-high's persisted
    bounds directly via _frontier_read_bounds and tracks its own progress
    via _correction_sweep_through_query/_update, not the allocator's
    claim_low(); see "Why not claim_low()" above. Never calls
    _frontier_persist_claim or _lineage_confirmed_through_update -- neither
    frontier-low nor lineage-confirmed-through is touched by this sweep."""

def _correction_sweep_walk(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> int:
    """Repeatedly call _correction_sweep_claim_and_process until it returns
    None. Returns the count of commits processed. No caller in this
    sub-phase -- 2d wires this into the real concurrent ingestion loop
    alongside the reverse stream, and alongside ordinary claim_low()-driven
    introduction for territory below the gap."""
```

### Schema/audit safety, idempotency

One new entity type, `:ingestion/correction-sweep-through`, reusing the
already-registered/audited `:type/ingestion` type exactly as
`:ingestion/lineage-confirmed-through` does — no new schema surface. Case
1's writes are the same query-before-write primitives 2a/2b already
established as safe (`_lineage_confirm`, `_candidate_diff_clear`). Case 2's
`:modified-in` assert, once its guard passes, *is* an unconditional
`_transact` — it is safe not because of a query-before-write guard but
because it always uses `C`'s own `commit_ts_iso`, matching what forward
walk (or 2b, if `C` happens to already carry other facts from it) would
have used at the same position, so a re-issued write lands as an idempotent
no-op rather than a duplicate (see "Per-commit algorithm" above). This is
worth stating plainly rather than leaving implicit, since it is the one
write in this design that depends on timestamp discipline rather than a
guard for its safety.

## Testing

Following `docs/testing-conventions.md` (real `MiniGrafDb`, no mocks), using
the same `tmp_path / "repo"` real-git-repo fixture pattern already used
throughout `tests/test_mcp_server.py`'s existing ingestion tests, and (where
a candidate needs a pre-existing provisional state) `_reverse_fill_claim_and_process`
itself to set that state up realistically rather than hand-constructing
facts:

- **Confirms a correct provisional guess** (case 1): run
  `_reverse_bulk_fill_walk` first so an entity ends up with a provisional
  `:introduced-by` pointing at its true earliest commit within Stream 2's
  claimed range; then run the correction sweep; assert
  `_lineage_is_provisional` is now `False`, the `:introduced-by` value is
  unchanged, and the candidate-diff record for that `(commit, entity)` is
  gone (`_candidate_diff_read` returns `None`).
- **Does not duplicate the claimed commit's metadata**: same setup as
  above; before running the sweep, capture the claimed commit entity's
  total live fact count and its `:hash`/`:author`/`:subject`/`:date`
  values as written by `_reverse_bulk_fill_walk`; run the sweep; assert the
  fact count is unchanged and none of those attributes was re-asserted at a
  *different* `valid_from` — this is the regression test that can actually
  distinguish a correct implementation (skips the write, or re-issues it
  idempotently at the same `commit_ts_iso`) from one that writes it at a
  different timestamp and produces a second live datom.
- **Skips `:modified-in` at an entity's own introduction commit on a
  resumed sweep** (case 2's guard): run the correction sweep once so an
  entity is confirmed authoritative at its true introduction commit; call
  `_correction_sweep_claim_and_process` again as if resuming after a crash
  (i.e. without having advanced `_correction_sweep_through_update` past
  that commit — construct this directly rather than via the real crash
  path, mirroring how other resume tests in this codebase re-invoke a step
  function against pre-set state); assert no `:modified-in` fact was
  created for that entity at its own introduction commit — this is the
  test that would fail against a naive implementation of case 2 with no
  self-introduction guard.
- **Ordinary modification of an already-authoritative entity** (case 2, the
  general path): an entity already authoritative at some earlier commit
  (pre-seeded via a real earlier sweep pass, non-provisional); sweep over a
  later commit that touches it; assert `:modified-in` is added at that
  later commit and no lineage marker exists.
- **Opportunistic stale candidate-diff cleanup** (case 2): pre-seed a stale
  candidate-diff record at a commit for an entity that's already
  authoritative by the time the sweep reaches that commit (simulating an
  orphaned intermediate guess along a supersession chain); assert the sweep
  clears it even though that commit falls into the ordinary case-2 path.
- **No-op when frontier-high hasn't claimed anything yet**: a graph with
  only frontier-low/migration state, no `_reverse_bulk_fill_walk` ever run;
  assert `_correction_sweep_claim_and_process` returns `None` and writes
  nothing.
- **Resumes from `correction-sweep-through` on a second call, not from
  frontier-high's lo-hash again**: run the sweep once (processes frontier-
  high's then-current lo-hash), then claim one more position via
  `_reverse_fill_claim_and_process` so frontier-high's lo-hash moves further
  down; call the correction sweep again; assert it processes the position
  immediately after where it left off (one above its previous stopping
  point), not frontier-high's new (lower) lo-hash — proving the dedicated
  watermark, not frontier-high's current bound, drives resumption.
- **No-op when the sweep has already reached `HEAD`**: run
  `_correction_sweep_walk` to exhaustion over the whole claimed range;
  assert a further call to `_correction_sweep_claim_and_process` still
  returns `None` rather than erroring or re-processing the last commit.
- **Falls back to frontier-high's current lo-hash when the stored
  `correction-sweep-through` hash is stale**: seed
  `:ingestion/correction-sweep-through` with a hash not present in a fresh
  `linearization` (simulating rewritten/rebased history); assert the sweep
  restarts from frontier-high's current lo-hash rather than erroring.
- **Frontier-low and `lineage-confirmed-through` are never touched**:
  capture frontier-low's persisted interval and
  `_lineage_confirmed_through_query`'s value before running
  `_correction_sweep_walk`; run it; assert both are byte-for-byte unchanged
  afterward, while `_correction_sweep_through_query` has advanced — the
  direct regression test for the bugs both "Why not `claim_low()`" and "Why
  a dedicated watermark" describe.
- **Full integration**: call `_reverse_fill_claim_and_process` a fixed
  number of times directly (not `_reverse_bulk_fill_walk` to exhaustion) so
  Stream 2 claims down to some position `P` within a larger history, then
  run `_correction_sweep_walk` to exhaustion; assert every entity touched
  at or above `P` ends up authoritative (`_lineage_is_provisional` is
  `False` for all of them), no `:type/candidate-diff` entities remain live,
  and `_correction_sweep_through_query` now equals `HEAD`'s hash — proving
  the sweep correctly walks all the way through frontier-high's claimed
  territory, not just the single position an earlier draft's (wrong) bound
  would have stopped at.
