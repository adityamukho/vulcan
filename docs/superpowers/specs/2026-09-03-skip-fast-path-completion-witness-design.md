# Skip fast-path for already-ingested positions, and its completion witness

Issue: #326 (split out of #325). Related: #313, #316, #317, #280, #260,
project-minigraf/minigraf#287.

Date: 2026-09-03

## Problem

Re-walking a position that is already in the graph costs approximately what
walking it fresh costs. `_frontier_load`'s discard branch says the replay is
harmless -- "re-processing an already-processed position is idempotent, so the
graph converges and only work is wasted" -- and #325 measured what that waste
is: ~6.6 positions/min on ArangoDB `origin/4.0`, or roughly 18 hours of pure
replay before a single new commit is ingested. A replayed position pays a
`git show` plus tree-sitter parse in `_extract_commit`, `_reverse_apply`'s
whole write batch (including the one-per-call `:contains`/`:depends-on`/
`:parent` loops that work around minigraf#287), one `_db_checkpoint`, and the
per-commit `MiniGrafDb` handle drop that #280 attributes ~47% of write time to.

Three distinct paths re-walk already-ingested positions, and only the first is
#325's:

1. the discarded high interval on tip growth (#325);
2. an interrupted run re-walking the position whose write was torn (#313);
3. any future frontier-lifecycle change that chooses to re-walk rather than fold.

A skip fast-path caps the cost of all three. It also de-risks #325: with the
fast path in place a discard is an inconvenience rather than an outage, so the
fold can be designed on its merits instead of under time pressure.

## The predicate is the whole issue

#325 words the fast path as "skips a position whose `:commit/<hash>` entity
already exists and whose facts are already tagged at the required authority
level". **The first half of that predicate is unsound and must not be used.**

In `_reverse_apply`, `[:commit/<hash> :entity-type :type/commit]` is the FIRST
element of `all_triples`, written before any file result is looked at.
`_frontier_persist_claim` runs LAST, followed by the single
`_db_checkpoint_gated`. A commit's write is a sequence of transacts, not an
atomic unit, so the commit entity's presence is the WEAKEST available witness
of a completed write.

That is exactly the state #313 leaves behind. An interrupted run leaves a torn
entity -- live `:ident`, no `:introduced-by`, no persisted claim -- while the
commit entity for that position is present, because it was written first.
#313's fix is that the resumed run re-walks that position and `_reverse_apply`
treats a live entity holding no `:introduced-by` as newly discovered. A fast
path keyed on commit-entity existence would skip precisely that position and
the orphaned lineage would become permanent, surfacing later as #316's
`entities_without_introduced_by` going red on a graph with no other symptom --
or, on a graph predating that gate, not surfacing at all.

**So the witness must be something written LAST, not first.**

## Approaches considered

**A. The persisted frontier interval, as it exists today.** Correct by
construction and needs no new fact -- `_frontier_persist_claim` is the last
write of a position, so membership in a persisted interval means the position
completed. Rejected in #326's own text because in the #325 scenario the
interval is the thing that was just discarded, so the witness is unavailable in
the one case that motivated the fast path. It does cover #313 (whose claim
genuinely never persisted).

**B. A per-position completion marker.** Sound in every case and independent of
#325, but it adds a new fact per commit to the very write path this issue exists
to make cheaper, and it needs its own answer for what happens to markers when an
interval is discarded.

**C. A structural sufficiency check** -- commit entity plus at least one
lineage-bearing fact at the required authority level. No new facts, but a
heuristic, and heuristics on this path fail silently in the direction of
permanent data loss.

**D (chosen). Archive the interval on discard.** The interval is not
*unavailable* in #325's scenario, it is *thrown away*: `_frontier_load` retracts
the bounds. Record them additively into a separate ident BEFORE retracting.
This has A's soundness (it is the same bounds, copied rather than dropped),
survives the case A was rejected for, costs one fact-set per discard event
rather than one per commit, and introduces no heuristic.

D was chosen in full knowledge that it lands a small piece of frontier
persistence #325 will build on, so the two are sequenced #326 first. It is
forward-compatible: when #325 stops discarding, the archiving branch simply
goes quiet and the fast path reads live intervals instead.

## Fact model

A new entity type, one entity per archived region:

```
:ingestion/completed-region-<lo_hash[:12]>
  :entity-type  :type/completed-region
  :lo-hash      "<full hash>"
  :hi-hash      "<full hash>"
  :tag          :provisional
```

Positions are stored as HASHES, not linearization positions, exactly as
`:ingestion/frontier-low`/`-high` are -- a position number is meaningless
against a linearization that has grown.

The ident is derived from the region's low hash so it is deterministic, but
that determinism is NOT the idempotency argument (see below). Coalescing can
change a region's low bound and therefore its ident; the write path handles
that as an ordinary retract-plus-assert of the changed set.

No `:ident` or `:description` attribute, matching `_frontier_persist_claim`'s
own four-fact shape.

### Against the four fact-model checks

**1. `MINIGRAF_SCHEMA` / audit safety.** `:type/completed-region` is
deliberately NOT registered in `MINIGRAF_SCHEMA`, the same status
`:type/ingest-interval` already holds. `handle_minigraf_audit` iterates exactly
the registered types and retracts any attribute outside a registered type's
allowed set; it never scans for a type it does not know, so an unregistered
companion type is invisible to it. This is the "own companion entity" option,
not the "add attributes to a registered type" option.

**2. Deterministic ident is not idempotency.** minigraf will create a duplicate
live datom when the same (entity, attribute, value) is re-transacted at a new
`valid_from` (#156). `_completed_region_record` therefore reads the current
region set first, computes the coalesced target set, and writes only the
difference -- retracting the regions that disappear, transacting the regions
that appear, and doing nothing at all when the target set equals the current
set. Same query-before-write guard `_watermark_update` and
`_frontier_persist_claim` established.

The idempotency test must call the function twice and assert a raw fact count
stays put, AND must prove non-clobbering of an ADVANCED set (record region X,
then record a strictly larger region Y, then record X again, and assert Y
survives) -- a test that only asserts "same value stays the same" cannot
distinguish a correct implementation from one that ignores its own guard.

**3. Migration / catch-up.** There is none, and none is needed -- but the
reason must be stated, not assumed. A completed region is only knowable from an
interval that exists at discard time; there is no older state to derive one
from, so there is nothing a catch-up function could seed. A graph carrying no
region facts simply never skips, which is exactly today's behaviour.

This does NOT mean existing graphs wait a run to benefit. Archiving happens in
`_frontier_load`, at load time, before the walk starts. The run that discards
is the run that skips. An ArangoDB-shaped graph is fixed on the first run after
upgrade, not the second.

**4. Transact path.** `_completed_region_record` and the load-time prune call
the internal `_transact`/`_retract` helpers directly. Routing them through
`handle_minigraf_transact` would fail every write: the public handler's
`_validate_facts` rejects a string-valued triple naming an unregistered entity
type outright. Unregistered-type safety from `minigraf_audit` does not imply
write-time safety -- they are two different validation paths with different
scopes.

### Set maintenance

Regions are kept small deliberately; an unbounded set would be a slow leak of
facts across runs.

* **Coalesce on write.** Same-tag regions that overlap or touch merge into one.
  In the realistic sequence they always do: run 1 leaves high = `[a, tip1]`;
  run 2 discards and archives `[a, tip1]`, then re-walks down and leaves high =
  `[b, tip2]` with `b <= a`; run 3 discards and archives `[b, tip2]`, which
  contains `[a, tip1]`. A run interrupted mid-skip leaves `[c, tip2]` with
  `c` inside `[a, tip1]`, so the two still overlap and merge.
* **Prune on load.** After the live intervals are built, retract any archived
  region fully covered by a live interval of the same tag. If that interval is
  itself discarded later, the discard re-archives it, so no information is lost
  by pruning.
* **Drop the unmappable.** A region whose `:lo-hash` or `:hi-hash` is not in the
  current linearization (the branch changed under us) is dropped from the
  in-memory set, mirroring `_frontier_load`'s own precedent for a bound it
  cannot map. Dropping only costs a re-walk.

## The predicate

```
_skip_claim(tag, pos) -> bool
```

True if and only if BOTH:

1. `tag == "rev"`, and
2. `pos` lies inside a loaded completed region tagged `:provisional`.

It consults NEITHER the `:commit/<hash>` entity NOR any lineage fact. Those are
the unsound witnesses; the region record is the sound one.

**Why #313 is safe by construction rather than by care.** A torn position's
`_frontier_persist_claim` never ran, so that position was never inside the
interval that got archived, so it is in no region, so it is not skipped. The
predicate does not need to know what a torn write looks like.

**Why `fwd` never skips**, which does double duty:

* A forward claim landing inside a provisional region is the authority upgrade
  that must still happen. A skip there would make a provisional region look
  confirmed -- the authority half of #325's wording, and a real failure mode.
* `_forward_apply` mutates `_ForwardWalkState`'s ten cross-position preload
  dicts in place. A skipped forward position would leave that state
  desynchronized for every later forward position, silently. `_reverse_apply`
  takes no state object and reads what it needs from the graph, so reverse
  skips carry no equivalent hazard.

Restricting to same-tag skipping gets both properties from one clause. Archived
regions are provisional in practice (`_frontier_load` only ever discards
frontier-high; frontier-low is `[C0, W]` and always representable), but the tag
is stored and checked rather than assumed, so a future discard of an
authoritative interval cannot silently license a forward skip.

## Where the skip lands

In `submit_next()` inside `_run_ingestion`, before
`loop.run_in_executor(executor, _extract_commit, ...)`. That placement is what
makes a skipped position cost neither the parse nor the write batch nor the
checkpoint nor the handle drop -- #326's requirement 2.

The region set is loaded once, by `_frontier_load`, which already reads the
persisted interval facts and is already the one function that maps hashes to
positions against this run's linearization. It returns the mapped, pruned region
list alongside the allocator, and `_run_ingestion` threads it into `submit_next`
as a plain in-memory list -- `_skip_claim` performs no query.

`submit_next` becomes a loop: take a claim, and while the claim is skippable,
count it and take the next one; queue the first non-skippable claim and return
True; return False when the gap empties. The loop body is a pure in-memory
interval lookup against the loaded region set, so a long run of skips costs
microseconds per position and does not stall the event loop the way an awaited
parse would.

`_reverse_fill_claim_and_process` is deliberately left alone. The 2d pipeline
calls `_reverse_apply` directly (see its docstring); that older fused helper is
not on the live claim path.

### Frontier persistence for skipped positions

#326's requirement 3 is that a skipped position still advances and persists the
frontier claim -- skipping the work is not skipping the bookkeeping.

In the common case this needs no new write. `_frontier_persist_claim(from_low=
False)` moves `:lo-hash`, which is a RANGE bound, so the next genuinely-walked
reverse position below a run of skips persists a bound that subsumes every
skipped position above it, in a write that already happens. The in-memory
allocator is advanced by `claim_high()` itself, so the skipped positions are
never handed out twice within a run.

One gap has to be closed explicitly: if the walk ends -- gap empty, or shutdown
requested -- while still inside a run of skips, nothing persisted it. The walk
loop therefore flushes once on exit, via a new helper:

```
_frontier_persist_span(db, linearization, lo_pos, hi_pos, from_low, commit_ts_iso, index_con)
```

`_frontier_persist_claim` cannot be reused for the flush. After a discard the
interval's facts are gone, so its `existing is None` branch fires and writes
`lo == hi == moved_hash`, collapsing the interval to a point and losing the top
bound. `_frontier_persist_span` writes both bounds when the interval is absent
and moves only the one bound when it is present.

Under-persisting is a cost, never a correctness failure: an unflushed region is
still covered by its archive record and is re-skipped cheaply on the next run.
The flush exists so the live interval converges, not to protect correctness.

A reverse position whose write FAILS (`_run_ingestion`'s per-commit `except`)
never reaches `_frontier_persist_claim`, so a run of skips above it stays
unpersisted. Same story: re-skipped next run.

## Counters and status

New `_ingest_progress["positions_skipped"]`, added to BOTH initializers (the
module-level `_ingest_progress` dict and the reset in `handle_minigraf_ingest_git`
-- a key present in one and not the other reads as 0 in exactly the runs that
matter). `positions_skipped_this_run` is derived in
`handle_minigraf_ingest_status` alongside the existing `processed_this_run`
(in-memory, no extra DB query).

**The name is deliberately not `skipped`.** `_ingest_progress["status"]` already
takes the value `"skipped"`, meaning the whole run declined because another
process owns the graph, and `commit_census` already reports `skipped_commits`,
meaning commits dropped for extraction failure. A third, unrelated `skipped`
would be misread as one of those two on sight.

`processed` is ALSO incremented for a skipped position, at the new site in
`submit_next`. This preserves its existing meaning -- positions retired by the
walk -- which is what #317's `commit_census.walk_claimed` reads.

That choice interacts with the census in a way worth stating, because it makes
an existing gate do useful new work:

* `walk_vs_graph` (walk_claimed vs `_count_commit_entities`) is gated ALWAYS.
  A skipped position increments `walk_claimed`, and its `:type/commit` entity is
  already in the graph from the earlier run, so the comparison still balances --
  **but only because the witness guarantees the graph already holds that
  commit.** If the skip predicate were ever wrong about a position, the census
  would go red. `walk_vs_graph` therefore becomes an independent gate on the
  predicate's soundness, at no extra cost.
* `repo_vs_walk` is gated only on `final_status == complete`. A complete resume
  run over a discarded region over-counts: `prior_ingested` already counted
  those positions and this run counts them again. **That over-count exists today
  and is unchanged by this work** -- today those positions are re-walked and
  `processed += 1` for each. #326 neither fixes nor worsens it. The at-scale
  gate runs on a fresh ingestion-only graph, so it cannot fire there.

The alternative -- excluding skips from `processed` -- was rejected precisely
because it would silently redefine the number `commit_census` gates on, turning
a clean skip-heavy resume into a reported lost commit.

Per-stream positions in `minigraf_ingest_status` (forward watermark, reverse
`lo`, tip-gap size) are #325's OTHER smaller change and #222 phase 4's
territory. Out of scope here.

## Testing

Real backend throughout, per `docs/testing-conventions.md` -- no `MagicMock`
fake of `MiniGrafDb`.

Two tests carry the acceptance criteria, and the second is the load-bearing one:

1. **The skip happens.** Ingest a real repo to completion so `frontier-high`
   is persisted, then add commits to the repo so the linearization grows past
   the persisted `:hi-hash` -- this is #325's actual trigger, reproduced rather
   than simulated, and it drives `_frontier_load` down the discard branch on its
   own. Re-run and assert zero `_extract_commit` calls and zero writes for the
   archived positions, while the genuinely-new tip commits above them ARE
   extracted and written.
2. **Positive control: a #313-style torn position is NOT skipped.** Produce the
   tear through the REAL write path by killing the lineage batch mid-
   `_reverse_apply` -- the deterministic technique #313's own regression tests
   use, so nothing depends on hitting a timing window. Assert that position IS
   extracted and written, and that `entities_without_introduced_by` stays 0.

Test 2 is what makes test 1 mean anything: a predicate that quietly matched
nothing would pass test 1 by doing no skipping at all.

Supporting tests:

3. `_completed_region_record` idempotency, in both directions (repeat is a
   no-op; a repeat of an older, smaller region does not clobber an advanced one).
4. Coalescing: overlapping and touching regions merge; disjoint ones do not.
5. Load-time prune: a region covered by a live interval is retracted; a region
   whose hashes are absent from the linearization is dropped from the in-memory
   set.
6. A forward claim inside a provisional archived region is NOT skipped.
7. The end-of-walk flush: a run that ends inside a skip run persists the span,
   and specifically does not collapse the interval to a point when the
   interval's facts were absent.

**Every guard here is ablation-proven, not asserted.** For each of tests 1, 2, 6
and 7, revert the specific clause it covers and confirm the test goes red. In
particular test 2 must go red when clause 2 of `_skip_claim` is replaced by the
unsound commit-entity predicate #325 proposed -- that is the counterfactual the
whole design turns on.

## Documentation

* CLAUDE.md gains a section under Graph Storage covering the witness, why the
  commit entity is not one, and why `fwd` never skips. It is the same class of
  standing decision as the `nil` and `:introduced-by` sections already there.
* SKILL.md's `minigraf_ingest_status` section carries a literal example payload
  (`{"ok": true, "status": "running", "processed": 21717,
  "processed_this_run": 2, ...}`) and prose explaining `processed` vs
  `processed_this_run`. Both need the new field, and the prose needs the one
  sentence that makes it useful: a run whose `positions_skipped_this_run` is
  climbing while `processed_this_run` tracks it is replaying an already-ingested
  region, which is the signal #325's incident had no way to show for 98 minutes.
  `tests/test_skill_doc.py` checks SKILL.md's examples against
  `mcp_server._TOOLS`, so the change is verified there rather than by eye.

## Non-goals

* Folding a confirmed high region into the frontier so it is never re-walked at
  all. That is #325 / #222 phase 3.
* Removing the discard branch. #326 makes the discard cheap; #325 decides
  whether it should happen.
* Per-stream status positions. #222 phase 4.
* Repairing existing graphs. Consistent with the standing decision that a graph
  predating #222's closure is rebuilt into a fresh graph path, never migrated.
