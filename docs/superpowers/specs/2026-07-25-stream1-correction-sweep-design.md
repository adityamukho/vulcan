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
- **2c (this spec)** — Stream 1's correction sweep: claims positions via
  `allocator.claim_low()`, converting the provisional facts 2b wrote into
  authoritative ones as it reaches them, using 2a's persisted candidate
  diffs for cheap replay instead of re-parsing.
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

- A per-commit correction step that claims one position from the gap's low
  end (`FrontierAllocator.claim_low()`) and, for every entity touched by
  that commit's "A"/"M" files, reconciles its lineage per the four-case
  algorithm below.
- A thin driving loop that repeats the above until the gap closes and
  persists each claim via `_frontier_persist_claim`, keeping
  `:ingestion/lineage-confirmed-through` in lockstep with frontier-low's
  advancement.
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

### Why `claim_low()`, not a hand-rolled position tracker

Phase 1's `FrontierAllocator` already models exactly the invariant this
sweep needs: `claim_low()` returns the next unclaimed position from the low
end, and returns `None` once the gap is closed (i.e. frontier-low's
advancing boundary has met frontier-high's current boundary). Using it
directly means:

- The sweep never walks past ground Stream 2 has actually covered — its
  termination *is* "reached the point where Stream 2 already provisionally
  covered ground," expressed through the allocator instead of a manually
  computed bound.
- It reuses already-tested machinery (`_frontier_persist_claim`,
  `FrontierAllocator.claim_low`) instead of introducing a second, parallel
  notion of "how far has Stream 1 gotten."
- It sets 2c up to become Stream 1's one true per-commit step in 2d: the
  four-case algorithm below is a strict generalization of ordinary forward
  walk (case 1 degenerates to exactly today's `_build_code_triples`
  introduction path when nothing is provisional), so 2d does not need two
  separate code paths for "virgin territory" vs. "correcting Stream 2's
  work" — a single call site suffices once concurrency is wired.

### Per-commit algorithm

For a claimed commit `C` (positions processed strictly oldest→newest via
repeated `claim_low()` calls), reusing the same per-file A/M walk
`_extract_commit`/`_build_code_triples` that 2b uses for entity
discovery/parsing (direction-agnostic):

Before any per-file work, `all_triples` is seeded with the claimed commit's
own `:type/commit` entity — this sweep processes commits `_reverse_fill_claim_and_process`
never touched (it claims from the *low* end of the gap; 2b only ever claims
from the high end), so unlike case 3's `guessed_ident` handling, which
targets a commit 2b already wrote, the commit metadata for `C` itself does
not exist yet and must be written here, identically to 2b's own shape:

```python
all_triples: List[str] = [
    f"[{commit_ident} :entity-type :type/commit]",
    f'[{commit_ident} :ident "{commit_ident}"]',
    f'[{commit_ident} :description "{_edn_escape(subject[:120])}"]',
    f'[{commit_ident} :hash "{commit_hash}"]',
    f'[{commit_ident} :author "{_edn_escape(author)}"]',
    f'[{commit_ident} :subject "{_edn_escape(subject[:200])}"]',
    f'[{commit_ident} :date "{commit_ts_iso}"]',
]
```

`:parent` edges are explicitly deferred, matching 2b's own precedent — 2b's
`_reverse_fill_claim_and_process` writes the same seven commit attributes
above and also does not write `:parent` (mcp_server.py:7321-7329). Both
streams currently leave every commit they claim without a `:parent` edge;
only ordinary forward walk (`_run_ingestion`, mcp_server.py:7982) writes it
today. This is a known gap shared by 2b and 2c, not something 2c introduces
new — 2d must decide how (or whether) to backfill `:parent` for commits
either stream claims once concurrency is wired, since that's the point at
which the frontier's authoritative region stops corresponding 1:1 with
"ordinary forward walk processed it."

Per-file candidate discovery then proceeds:

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
# Snapshot BEFORE calling _build_code_triples -- it mutates known_before in
# place (its entity_valid_from parameter), inserting every newly introduced
# ident as a side effect of building that ident's own structural triples.
# Classifying case 1 against the post-call dict would therefore find every
# fresh introduction already "known" and skip its authoritative
# :introduced-by write entirely. Same reason 2b takes a snapshot
# (mcp_server.py:7351) before its own equivalent call.
known_before_snapshot = set(known_before.keys())
triples = _build_code_triples(
    file_path, extracted, commit_ts_iso, known_before, {}, {}, commit_ident,
    precomputed, {}, {},
)
# This sweep owns the write timing of both attributes, same reason 2b does.
all_triples.extend(t for t in triples if ":introduced-by" not in t and ":modified-in" not in t)
```

Each candidate ident then falls into exactly one of four cases, classified
against `known_before_snapshot` (never the post-call `known_before`):

1. **No fact yet** (`ident not in known_before_snapshot`) — neither stream
   has touched this entity before. This *is* the true chronological
   introduction, since the sweep walks oldest→newest and nothing earlier
   claimed it. Assert `[ident :introduced-by commit_ident]` directly via
   `_transact` — no provisional marker, no candidate-diff record (nothing
   will ever need to re-check it, since this fact was never a guess). No
   `:modified-in`. This is exactly what `_build_code_triples`'s own gate
   would have done unfiltered — 2c only needs to special-case the other
   three.

2. **Known, provisional, guess matches this commit**
   (`_lineage_is_provisional(db, ident)` and
   `_entity_introduced_by_query(db, ident) == commit_ident`) — the expected
   case once Stream 2 has fully walked the range down to `C`: by
   construction, Stream 2's final guess for any entity within a range it
   has completely traversed already equals that entity's true earliest
   occurrence in that range. Call `_lineage_confirm(db, ident)` (drops the
   marker; the `:introduced-by` fact itself is untouched, since its value
   was already correct) and `_candidate_diff_clear(db, commit_hash, ident)`.
   No `:modified-in`.

3. **Known, provisional, guess points elsewhere**
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

4. **Known, authoritative** (confirmed earlier in this same sweep, or
   introduced by the original single-stream walk before #222 existed) — the
   ordinary "already known" case: assert `[ident :modified-in commit_ident]`
   unless `precomputed["unchanged_idents"]` says the body is provably
   unchanged this commit (#221, unmodified). Additionally, opportunistically
   call `_candidate_diff_clear(db, commit_hash, ident)` if a record happens
   to exist for this exact `(commit, ident)` pair — this is what cleans up
   the *intermediate* stale records 2b's own spec left behind along a
   supersession chain (guess moves `L3 → L2 → L1 → C`; case 2 clears `C`'s
   own record when the sweep reaches `C`; each of `L1`/`L2`/`L3` falls into
   this case 4 when the sweep later reaches them, and each still has its
   own now-orphaned candidate-diff record from when it was a transient
   guess). No special-casing was needed for this — it falls out of the
   ordinary path once the sweep passes back over those commits.

### Driving functions

```python
def _correction_sweep_claim_and_process(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    allocator: "frontier_registry.FrontierAllocator",
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> Optional[str]:
    """Claim one position from the gap's low end and reconcile that
    commit's entities per the four-case algorithm above. Returns the
    claimed commit's hash, or None if the gap was already empty (caller's
    signal to stop). Writes are followed by exactly one _db_checkpoint(db)
    call, after _frontier_persist_claim and _lineage_confirmed_through_update
    both record the claim -- mirrors _reverse_fill_claim_and_process's
    one-checkpoint-per-commit cadence."""

def _correction_sweep_walk(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    allocator: "frontier_registry.FrontierAllocator",
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> int:
    """Repeatedly call _correction_sweep_claim_and_process until the gap
    closes. Returns the count of commits processed. No caller in this
    sub-phase -- 2d wires this into the real concurrent ingestion loop
    alongside the reverse stream."""
```

### Frontier + watermark advancement

Each processed commit calls both `_frontier_persist_claim(db, linearization,
pos, from_low=True, commit_ts_iso=commit_ts_iso, index_con=index_con)` *and*
`_lineage_confirmed_through_update(db, commit_hash, commit_ts_iso,
index_con=index_con)` — restoring the lockstep the two watermarks had before
Stream 2 existed (`_lineage_confirmed_through_migrate` originally seeded
`lineage-confirmed-through` from frontier-low's boundary precisely because
they were always advanced together by the single old forward walk). Every
commit this sweep claims is, by construction, now fully lineage-confirmed,
so advancing both together is correct and keeps
`_lineage_confirmed_through_query`'s trust predicate (2a spec, "Migration
seeding") meaningful without special-casing. One `_db_checkpoint(db)` call
per commit, after both watermark updates, same cadence as 2b.

### Schema/audit safety, idempotency

No new entity types or attributes — 2c is purely a new consumer of 2a's
already-audited-safe primitives (`_lineage_confirm`/`_candidate_diff_clear`
already established their own safety in 2a's spec) plus the existing
`:introduced-by`/`:modified-in` attributes forward walk always used. Every
write in the four-case algorithm is either a query-before-write primitive
already built in 2a/2b, or a direct `_transact`/`_retract` pair gated on a
query result (case 1 and case 3's direct authoritative assert) — never a
blind unconditional transact.

## Testing

Following `docs/testing-conventions.md` (real `MiniGrafDb`, no mocks), using
the same `tmp_path / "repo"` real-git-repo fixture pattern already used
throughout `tests/test_mcp_server.py`'s existing ingestion tests, and (where
a candidate needs a pre-existing provisional state) `_reverse_fill_claim_and_process`
itself to set that state up realistically rather than hand-constructing
facts:

- **Fresh introduction, no prior provisional state** (case 1): a two-commit
  repo with no reverse-walk activity; sweep from the start; assert the
  entity's `:introduced-by` is asserted directly, no `:type/lineage-marker`
  companion entity was ever created, and no candidate-diff record exists.
  Also query the claimed commit itself and assert its `:type/commit` entity
  was written (`:entity-type`, `:hash`, `:subject`, `:date` at minimum) —
  this is new 2c-owned behavior (2c claims commits from the low end that 2b
  never touched, so unlike case 3's `guessed_ident` handling, nothing else
  has written this commit's metadata first) and a regression that dropped
  it would still pass every other case-1 assertion while leaving
  `:introduced-by` pointing at a dangling `:commit/...` ident. Additionally
  assert no `:parent` fact exists for the claimed commit, documenting the
  scope cut rather than leaving it untested.
- **Confirms a correct provisional guess** (case 2): run
  `_reverse_bulk_fill_walk` first so an entity ends up with a provisional
  `:introduced-by` pointing at its true earliest commit; then run the
  correction sweep; assert `_lineage_is_provisional` is now `False`, the
  `:introduced-by` value is unchanged, and the candidate-diff record for
  that `(commit, entity)` is gone (`_candidate_diff_read` returns `None`).
- **Reconciles a wrong provisional guess** (case 3): hand-construct a
  provisional `:introduced-by` pointing at the *wrong* (later) commit for
  an entity that's also genuinely present at an earlier commit the sweep
  will reach, simulating the rename/rebirth edge case; run the sweep;
  assert `:introduced-by` now points at the true earlier commit, is
  authoritative, and the superseded commit received a retroactive
  `:modified-in` with its own timestamp.
- **Skips the retroactive `:modified-in` when the body is unchanged**
  (case 3's #221 fix): same setup as above, but with matching body hashes
  persisted at both the superseded and the true-introduction commits;
  assert no `:modified-in` fact was written for the superseded commit —
  this is the test that would fail against 2b's own documented limitation
  if 2c naively reused its unconditional-assert behavior.
- **Fails open when a body hash is missing** (case 3's fail-open guard):
  same reconciliation setup as above, but for a *module* ident (which
  `precomputed["body_hashes"]` never populates) so both the persisted
  candidate-diff hash and the current body hash are `None`; assert the
  retroactive `:modified-in` is still written — this is the test that would
  catch a naive `==` comparison treating the `None == None` case as
  "unchanged" and wrongly suppressing a real edit.
- **Ordinary modification of an already-authoritative entity** (case 4): an
  entity already authoritative (pre-seeded, non-provisional); sweep over a
  commit that touches it; assert `:modified-in` is added and no lineage
  marker exists.
- **Opportunistic stale candidate-diff cleanup** (case 4): pre-seed a stale
  candidate-diff record at a commit for an entity that's already
  authoritative by the time the sweep reaches that commit (simulating an
  orphaned intermediate guess along a supersession chain); assert the sweep
  clears it even though that commit falls into the ordinary case-4 path.
- **Gap-empty no-op**: an allocator whose gap is already empty; assert
  `_correction_sweep_claim_and_process` returns `None` and writes nothing.
- **`_frontier_persist_claim` + `_lineage_confirmed_through_update`
  lockstep**: after `_correction_sweep_walk` processes N commits, assert
  both frontier-low's persisted interval and
  `_lineage_confirmed_through_query` reflect the same final commit.
- **Full integration**: run `_reverse_bulk_fill_walk` over a range from
  `HEAD`, then `_correction_sweep_walk` over the same range from the start;
  assert every touched entity ends up authoritative
  (`_lineage_is_provisional` is `False` for all of them) and no
  `:type/candidate-diff` entities remain live.
