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
- **2c (this spec)** — Stream 1's correction sweep: walks forward through
  positions Stream 2 has *already* claimed provisional (tracked via
  `:ingestion/lineage-confirmed-through` and frontier-high's current
  boundary — **not** `allocator.claim_low()`, see "Why not `claim_low()`"
  below), converting the provisional facts 2b wrote into authoritative ones
  as it reaches them, using 2a's persisted candidate diffs for cheap replay
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

- A per-commit correction step that advances a position tracker — the
  commit immediately after `:ingestion/lineage-confirmed-through`, bounded
  above (inclusive) by frontier-high's *current* `:lo-hash` — and, for
  every entity touched by that commit's "A"/"M" files, reconciles its
  lineage per the two-case algorithm below.
- A thin driving loop that repeats the above until the tracker reaches
  frontier-high's boundary, advancing `:ingestion/lineage-confirmed-through`
  per commit. Frontier-low is deliberately never touched by this sweep (see
  "Why not `claim_low()`" below).
- Fixing 2b's documented known limitation: a retroactive `:modified-in` fact
  written when a provisional guess is superseded does not re-check #221's
  unchanged-body narrowing against the superseded commit's own diff. 2c has
  both commits' persisted candidate-diff body hashes available and can
  correct this without re-parsing.
- Opportunistically clearing stale intermediate candidate-diff records left
  behind along a supersession chain (2a's spec flagged this explicitly as
  "2c's job").

Explicitly deferred (matches 2b's own scope cut):

- `:depends-on` edges, deletions ("D"), and renames ("R"). The existing
  `_run_ingestion` loop already has full support for these; this sweep
  reuses the same `_extract_commit`/`_build_code_triples` entity-discovery
  path 2b used and keeps the same `if status not in ("A", "M"): continue`
  guard.
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

That means the entire premise of cases 2 and 3 below — Stream 1 "reaching"
a commit Stream 2 already wrote a provisional guess for — cannot happen via
`claim_low()`. The positions this sweep needs to revisit are exactly the
ones **already claimed** by Stream 2 (tagged provisional, `:introduced-by`
facts already written), which lie in the frontier-high interval, not the
gap. So the sweep needs its own, entirely separate position tracker, keyed
off two already-existing watermarks instead of the allocator:

- **Lower bound (exclusive)**: `:ingestion/lineage-confirmed-through`'s
  current hash — everything up to and including this position is already
  fully confirmed, by either the original single-stream walk or an earlier
  pass of this sweep.
- **Upper bound (inclusive)**: frontier-high's *current* `:lo-hash` (via
  `_frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)`) — the lowest position
  Stream 2 has claimed so far. The sweep never walks past this, since
  anything below it either hasn't been claimed by Stream 2 yet (nothing to
  correct) or belongs to frontier-low's own territory (never provisional in
  the first place).

This does mean 2c is *not* a drop-in replacement for `claim_low()` as
Stream 1's single per-commit call site — 2d will need to run both: ordinary
`claim_low()`-driven introduction for genuinely virgin territory below the
gap, and this sweep for already-claimed territory above it. That's an
honest reflection of the allocator's actual semantics rather than a
convenience this spec can assume away.

### Per-commit algorithm

For a commit `C` in the sweep's range (positions processed strictly
oldest→newest via the position tracker above), reusing the same per-file
A/M walk `_extract_commit`/`_build_code_triples` that 2b uses for entity
discovery/parsing (direction-agnostic):

**2c never writes `C`'s own `:type/commit` entity.** Every commit within
the sweep's range `[lineage-confirmed-through+1, frontier-high's current
lo-hash]` was, by the range's own definition, already claimed by 2b's
`claim_high()` and fully processed by `_reverse_fill_claim_and_process` —
which writes its claimed commit's seven-attribute metadata block
unconditionally, before its per-file loop even runs (mcp_server.py:7321-7329),
regardless of whether that commit touches any code entity. So `C`'s commit
entity is guaranteed to already exist with the correct metadata (including
the same `:parent`-edge gap 2b itself has — not something 2c introduces or
needs to fix). Re-`_transact`-ing the same commit attributes here would not
just be redundant, it would create a duplicate live datom under minigraf's
write semantics (the same hazard `_watermark_update` and every 2a/2b
primitive already guards against with query-before-write) — so this sweep
must not attempt it.

For the same reason, **every candidate entity ident this sweep encounters
is guaranteed to already have an `:introduced-by` fact.** Stream 2 parses
the identical diff for `C` via the same `_extract_commit` call when it
claims `C`, so any entity `C`'s "A"/"M" files touch was necessarily also
discovered by Stream 2's own processing of `C` and got at least a
provisional `:introduced-by` (2b's own "no fact yet" branch). There is no
entity-discovery case symmetric to 2b's "no fact yet" branch for this
sweep to handle:

```python
candidate_idents = (
    [precomputed["module_ident"]]
    + [ident for ident, _name, _t in precomputed["function_entries"]]
    + [ident for ident, _name, _t in precomputed["class_entries"]]
    + [ident for ident, _name, _t in precomputed["global_entries"]]
    + [ident for ident, _name, _t in precomputed["field_entries"]]
)
known_before = {
    ident: "known" for ident in candidate_idents
    if _entity_introduced_by_query(db, ident) is not None
}
triples = _build_code_triples(
    file_path, extracted, commit_ts_iso, known_before, {}, {}, commit_ident,
    precomputed, {}, {},
)
# This sweep owns the write timing of both attributes, same reason 2b does.
all_triples.extend(t for t in triples if ":introduced-by" not in t and ":modified-in" not in t)
```

`known_before` is still built and passed through exactly as shown — it is
what makes `_build_code_triples` skip re-emitting structural triples
(`:entity-type`/`:description`/`:file`/`:contains`) for entities 2b already
wrote structurally, which is real and necessary. It is only the *case
classification* below that no longer needs a "not in known_before" branch,
because that branch is unreachable given the invariant above — so, unlike
2b, this sweep does **not** need a `known_before_snapshot` taken before the
`_build_code_triples` call: nothing here depends on `known_before`'s
pre-call membership, only on `_lineage_is_provisional`/
`_entity_introduced_by_query`, queried independently per ident below.

Each candidate ident then falls into exactly one of two cases:

1. **Provisional, guess matches this commit**
   (`_lineage_is_provisional(db, ident)` and
   `_entity_introduced_by_query(db, ident) == commit_ident`) — the expected
   case once Stream 2 has fully walked the range down to `C`: by
   construction, Stream 2's final guess for any entity within a range it
   has completely traversed already equals that entity's true earliest
   occurrence in that range. Call `_lineage_confirm(db, ident)` (drops the
   marker; the `:introduced-by` fact itself is untouched, since its value
   was already correct) and `_candidate_diff_clear(db, commit_hash, ident)`.
   No `:modified-in`.

2. **Provisional, guess points elsewhere**
   (`_lineage_is_provisional(db, ident)` and
   `_entity_introduced_by_query(db, ident) != commit_ident`) — the
   reconciliation case 2a's spec reserved for 2c (rename- or
   rebirth-spanning-the-gap; out of scope for both 2b and 2c to *detect*
   structurally, but 2c corrects the resulting lineage once it reaches the
   entity's real introduction here). Let `guessed_ident` be the current
   (wrong) value:
   - Retract `[ident :introduced-by guessed_ident]`, assert
     `[ident :introduced-by commit_ident]` directly as authoritative (no
     provisional marker — 2c is ground truth), then `_lineage_confirm(db,
     ident)` to drop the marker 2b left behind.
   - Give `guessed_ident` a retroactive `:modified-in` fact using **its
     own** commit timestamp (looked up from `commit_metadata` via the same
     `ts_by_commit_ident = {f":commit/{h[:12]}": ts for h, ts, _a, _s in
     commit_metadata}` mapping 2b's step 3 builds) — **unless both**
     `_candidate_diff_read(db, guessed_hash, ident)` and
     `precomputed["body_hashes"].get(ident)` are non-`None` **and** equal,
     meaning the body was actually unchanged between the two commits. This
     is the fix for 2b's documented limitation, using the persisted hash
     instead of re-parsing `guessed_ident`'s own diff. The comparison must
     fail open (assert `:modified-in` conservatively) whenever either side
     is `None` — `precomputed["body_hashes"]` deliberately excludes modules
     (mcp_server.py:6414, function/class/variable/field only) and can be
     empty on a parse/hash failure, so a bare `==` comparison would treat
     module idents (and any hash-failure case) as "unchanged" via a
     `None == None` false positive and wrongly suppress a real edit.
     `guessed_hash` is recovered directly from `guessed_ident` by stripping
     its `":commit/"` prefix (`guessed_ident[9:]`) — sufficient because
     `_candidate_diff_ident` only ever keys on `commit_hash[:12]`, which is
     exactly what `commit_ident` already truncated to when 2b minted it, so
     no separate hash→ident reverse lookup is needed.
   - `_candidate_diff_clear` both `(commit_hash, ident)` and
     `(guessed_hash, ident)`.

3. **Already authoritative** (confirmed earlier in this same sweep, or
   introduced by the original single-stream walk before #222 existed) — the
   ordinary "already known" case: assert `[ident :modified-in commit_ident]`
   unless `precomputed["unchanged_idents"]` says the body is provably
   unchanged this commit (#221, unmodified). Additionally, opportunistically
   call `_candidate_diff_clear(db, commit_hash, ident)` if a record happens
   to exist for this exact `(commit, ident)` pair — this is what cleans up
   the *intermediate* stale records 2b's own spec left behind along a
   supersession chain (guess moves `L3 → L2 → L1 → C`; case 1 clears `C`'s
   own record when the sweep reaches `C`; each of `L1`/`L2`/`L3` falls into
   this case when the sweep later reaches them, and each still has its own
   now-orphaned candidate-diff record from when it was a transient guess).
   No special-casing was needed for this — it falls out of the ordinary
   path once the sweep passes back over those commits.

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
    """Advance the correction sweep by exactly one commit past
    :ingestion/lineage-confirmed-through and reconcile that commit's
    entities per the two-case algorithm above. Returns the processed
    commit's hash, or None if the tracker has already reached frontier-
    high's current lo-hash (nothing left to correct against what Stream 2
    has claimed so far), or if frontier-high hasn't claimed anything yet.
    No `allocator` parameter -- this reads frontier-high's persisted bounds
    directly via _frontier_read_bounds, not the allocator's claim_low(); see
    "Why not claim_low()" above. Writes are followed by exactly one
    _db_checkpoint(db) call, after _lineage_confirmed_through_update records
    the advance -- mirrors _reverse_fill_claim_and_process's
    one-checkpoint-per-commit cadence. Never calls _frontier_persist_claim:
    frontier-low is not touched by this sweep."""

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

### Frontier + watermark advancement

Each processed commit calls `_lineage_confirmed_through_update(db,
commit_hash, commit_ts_iso, index_con=index_con)` — **and nothing else**.
Unlike an earlier draft of this spec, `_frontier_persist_claim(...,
from_low=True, ...)` is never called: `C` already lies inside frontier-
high's persisted interval (that's the sweep's own range invariant), and
extending frontier-low's `:hi-hash` onto a position frontier-high's
`:lo-hash` also covers would make the two persisted intervals overlap —
`_frontier_load`'s reconstruction has no defined behavior for that (`
FrontierAllocator._interval_covering` returns whichever interval it checks
first), and `gap_lo`/`gap_hi` would no longer describe a coherent gap. This
is exactly the confusion "Why not `claim_low()`" above already ruled out at
the allocator-call level; not calling `_frontier_persist_claim` here is the
same rule applied to the lower-level persistence helper directly, since
nothing stops it from being called with an arbitrary position otherwise.
`:ingestion/lineage-confirmed-through` is the sole watermark this sweep
advances, matching 2a's spec: the composed trust predicate it defines
(`_lineage_is_provisional(entity)` is `False` **and** the entity's
`:introduced-by` position is `<=` `lineage-confirmed-through`'s position)
never references frontier-low at all. One `_db_checkpoint(db)` call per
commit, after the watermark update, same cadence as 2b.

### Schema/audit safety, idempotency

No new entity types or attributes — 2c is purely a new consumer of 2a's
already-audited-safe primitives (`_lineage_confirm`/`_candidate_diff_clear`
already established their own safety in 2a's spec) plus the existing
`:introduced-by`/`:modified-in` attributes forward walk always used. Every
write in the two-case algorithm is either a query-before-write primitive
already built in 2a/2b, or a direct `_transact`/`_retract` pair gated on a
query result (case 2's direct authoritative assert) — never a blind
unconditional transact.

## Testing

Following `docs/testing-conventions.md` (real `MiniGrafDb`, no mocks), using
the same `tmp_path / "repo"` real-git-repo fixture pattern already used
throughout `tests/test_mcp_server.py`'s existing ingestion tests, and (where
a candidate needs a pre-existing provisional state) `_reverse_fill_claim_and_process`
itself to set that state up realistically rather than hand-constructing
facts:

- **Confirms a correct provisional guess** (case 1): run
  `_reverse_bulk_fill_walk` first so an entity ends up with a provisional
  `:introduced-by` pointing at its true earliest commit; then run the
  correction sweep; assert `_lineage_is_provisional` is now `False`, the
  `:introduced-by` value is unchanged, and the candidate-diff record for
  that `(commit, entity)` is gone (`_candidate_diff_read` returns `None`).
- **Does not re-write the claimed commit's metadata**: same setup as above;
  before running the sweep, capture the claimed commit entity's live facts
  (`:hash`/`:author`/`:subject`/`:date`) as written by
  `_reverse_bulk_fill_walk`; run the sweep; assert those facts are
  byte-for-byte unchanged and, via a raw fact-count query, that no
  duplicate `:hash`/`:subject`/etc. datom was created — this is the
  regression test for the bug an earlier draft of this spec would have
  introduced by unconditionally re-`_transact`-ing commit metadata 2b
  already wrote.
- **Reconciles a wrong provisional guess** (case 2): hand-construct a
  provisional `:introduced-by` pointing at the *wrong* (later) commit for
  an entity that's also genuinely present at an earlier commit the sweep
  will reach, simulating the rename/rebirth edge case; run the sweep;
  assert `:introduced-by` now points at the true earlier commit, is
  authoritative, and the superseded commit received a retroactive
  `:modified-in` with its own timestamp.
- **Skips the retroactive `:modified-in` when the body is unchanged**
  (case 2's #221 fix): same setup as above, but with matching body hashes
  persisted at both the superseded and the true-introduction commits;
  assert no `:modified-in` fact was written for the superseded commit —
  this is the test that would fail against 2b's own documented limitation
  if 2c naively reused its unconditional-assert behavior.
- **Fails open when a body hash is missing** (case 2's fail-open guard):
  same reconciliation setup as above, but for a *module* ident (which
  `precomputed["body_hashes"]` never populates) so both the persisted
  candidate-diff hash and the current body hash are `None`; assert the
  retroactive `:modified-in` is still written — this is the test that would
  catch a naive `==` comparison treating the `None == None` case as
  "unchanged" and wrongly suppressing a real edit.
- **Ordinary modification of an already-authoritative entity** (case 3): an
  entity already authoritative (pre-seeded, non-provisional); sweep over a
  commit that touches it; assert `:modified-in` is added and no lineage
  marker exists.
- **Opportunistic stale candidate-diff cleanup** (case 3): pre-seed a stale
  candidate-diff record at a commit for an entity that's already
  authoritative by the time the sweep reaches that commit (simulating an
  orphaned intermediate guess along a supersession chain); assert the sweep
  clears it even though that commit falls into the ordinary case-3 path.
- **No-op when frontier-high hasn't claimed anything yet**: a graph with
  only frontier-low/migration state, no `_reverse_bulk_fill_walk` ever run;
  assert `_correction_sweep_claim_and_process` returns `None` and writes
  nothing.
- **No-op when the sweep has already caught up**: run
  `_correction_sweep_walk` to exhaustion over a range Stream 2 has claimed;
  assert a further call to `_correction_sweep_claim_and_process` still
  returns `None` (lineage-confirmed-through has reached frontier-high's
  lo-hash) rather than erroring or re-processing the last commit.
- **Frontier-low is never touched**: capture frontier-low's persisted
  interval before running `_correction_sweep_walk`; run it; assert
  frontier-low's persisted `:lo-hash`/`:hi-hash` are byte-for-byte
  unchanged afterward, while `_lineage_confirmed_through_query` has
  advanced — the direct regression test for the bug this spec's `claim_low()`
  draft would have introduced (see "Why not `claim_low()`").
- **Full integration**: call `_reverse_fill_claim_and_process` a fixed
  number of times directly (not `_reverse_bulk_fill_walk` to exhaustion —
  leaving a non-trivial gap below Stream 2's claimed territory, position
  `P`, is what makes this a meaningful test of the sweep's own bounds, not
  just its logic) so Stream 2 claims down to `P` but no further; then run
  `_correction_sweep_walk`; assert every entity touched at or above `P`
  ends up authoritative (`_lineage_is_provisional` is `False` for all of
  them), no `:type/candidate-diff` entities remain live, and
  `_lineage_confirmed_through_query` now equals `P`'s hash — proving the
  sweep's own upper bound (frontier-high's lo-hash) was respected, not
  walked past.
