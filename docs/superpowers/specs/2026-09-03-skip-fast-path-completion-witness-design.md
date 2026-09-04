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

**A. The persisted frontier interval, as it exists today.** Needs no new fact.
The argument made here was "`_frontier_persist_claim` is the last write of a
position, so membership in a persisted interval means the position completed" --
**which turned out to be false as written; see "The witness statement, corrected"
below.** Rejected in #326's own text because in the #325 scenario the
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
:ingestion/completed-region-<tag>-<lo_hash[:12]>
  :entity-type  :type/completed-region
  :ident        ":ingestion/completed-region-<tag>-<lo_hash[:12]>"
  :lo-hash      "<full hash>"
  :hi-hash      "<full hash>"
  :tag          :provisional
  :pos-count    <positions the bounds spanned when the region was proven>
```

The TAG is part of the ident, not just of the fact set (see "The denominators",
item 3), and `:pos-count` is the stored denominator of item 1.

Positions are stored as HASHES, not linearization positions, exactly as
`:ingestion/frontier-low`/`-high` are -- a position number is meaningless
against a linearization that has grown.

The ident is derived from the region's tag and low hash so it is deterministic, but
that determinism is NOT the idempotency argument (see below). Coalescing can
change a region's low bound and therefore its ident; the write path handles
that as an ordinary retract-plus-assert of the changed set.

**The `:ident` fact is load-bearing, not decoration.** Unlike frontier-low and
frontier-high, which are fixed idents a query can name literally, the region set
has to be ENUMERATED. `[?e :entity-type :type/completed-region]` binds `?e` to
the entity, and minigraf answers that in UUID space -- `_count_commit_entities`
gets away with the same pattern only because it counts and never reads `?e`
back. Carrying a string-valued `:ident` lets the enumeration query bind and
return `?ident` instead, so no UUID-to-ident resolution is needed to find a
region or to retract one. `:ingestion/format-version` already carries an
`:ident` fact for its own reasons, so this is an established shape here.

No `:description` attribute.

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

### The witness statement, corrected

The sentence this whole design rested on -- "`_frontier_persist_claim` is the
LAST write of a position, so membership in a persisted interval proves that
position completed" -- is **FALSE as written**, and was reproduced end to end
during the final review.

`:lo-hash` is a closed RANGE bound. Membership is therefore implied by a
NEIGHBOUR's claim, never by the position's own. A reverse write that RAISES
takes `_run_ingestion`'s per-commit `except`, which logs, does `processed += 1`,
and **continues the descent**; the next lower position that succeeds moves
`:lo-hash` beneath the failed one and sweeps it into the interval. Pure tip
growth preserves the interval's `:pos-count` exactly, so it is archived, and
`_skip_claim` then skips that position forever. #313 is safe only because a
SIGKILL STOPS the process -- the `except` path does not.

The interval was always this imprecise, master included. Master was
self-healing by accident: the discard forced a full re-walk. **Archiving the
interval is what converts the imprecision into permanent silent loss**, which
every at-scale detector reads clean (`fact_audit`'s two witnesses agree about an
absence, both `:introduced-by` checks only examine entities that EXIST,
`stderr_capture` sees only the one skip line, and `commit_census` runs on a
fresh graph).

The fix makes the INTERVAL precise rather than weakening the predicate. The
reverse stream descends monotonically, so the highest position a run failed to
complete is a floor `:lo-hash` may not cross for the rest of that run:
`_reverse_apply` gains `persist_claim`, and the end-of-walk
`_frontier_persist_span` flush clamps its lo bound to the same floor. Both
incompleteness paths raise the floor -- a write that raised and an extraction
that raised -- because they are the same defect; the forward stream is
untouched, since it claims frontier-low and its failure semantics are out of
scope here.

There is a third way a reverse position retires incomplete that does NOT raise
the floor: the shutdown `break` in `_run_ingestion`'s pipeline loop, which
abandons whatever is still queued in `pending` unclaimed. It needs no floor,
because nothing lower ever claims after a shutdown, and `completed_all=False`
already gates the end-of-walk flush off for that run -- so a reader auditing
this invariant should not expect a third `_note_incomplete_rev` call site to
match it.

**It withholds BOOKKEEPING, never WORK.** Positions below the floor are still
claimed, still parsed and still written in full. They simply do not assert
completion, so the next run re-walks them. Cost is one run's re-walk below a
transient failure -- what master effectively did anyway. A DETERMINISTIC failure
at a fixed position blocks reverse-frontier progress below it for as long as it
keeps failing; that is the accepted price of a precise interval, and it is loud
rather than silent. It also starves Stage B (the correction sweep) for that
entire reverse region, not just below the floor:
`_correction_sweep_select_position` needs the PERSISTED gap closed before it
selects anything, and a floored run never moves frontier-high's `:lo-hash`
down to meet frontier-low, so the sweep runs zero times -- no lineage
confirmation and none of `_forward_apply(..., lifecycle_only=True)`'s D/R
closes, renames, dependency churn or gitlink changes for the span. It
self-heals on the next clean run; `_should_fold_lineage_watermark` stays
correct throughout, since it requires the sweep to have reached the high
bound.

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

The region set is loaded once per run, by a new `_completed_regions_load`
called from `_run_ingestion` immediately after `_frontier_load`, on the same
write_executor and under the same lease. It maps hashes to positions against
this run's linearization, prunes, and returns a plain list of
`frontier_registry.Interval`. `_run_ingestion` threads that list into
`submit_next` -- `_skip_claim` performs no query.

**`_frontier_load`'s own signature and return type do NOT change.** It gains
only the archiving call inside its discard branch. Widening its return to a
tuple would break roughly a dozen existing call sites and tests that use its
result directly as an allocator, for no gain: the archiving must live there
(that is where the doomed bounds are), but the loading need not.

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
unpersisted. Same story: re-skipped next run. It ALSO raises the run's claim
floor, so nothing below it persists a claim either and the flush's lo bound is
clamped to sit above it -- see "The witness statement, corrected".

## Counters and status

New `_ingest_progress["positions_skipped"]`, added to BOTH initializers (the
module-level `_ingest_progress` dict and the reset in `handle_minigraf_ingest_git`
-- a key present in one and not the other reads as 0 in exactly the runs that
matter). `positions_skipped_this_run` is derived in
`handle_minigraf_ingest_status` alongside the existing `processed_this_run`
(in-memory, no extra DB query).

**The name is deliberately not `skipped`.** `_ingest_progress["status"]` already
takes the value `"skipped"`, meaning the whole run declined because another
process owns the graph, and `stderr_capture` already reports `skipped_commits`
(gated by `run_ingestion_benchmark._exit_code`), meaning commits dropped for
extraction OR write failure -- `_SKIPPED_COMMIT_RE` matches both log lines. A
third, unrelated `skipped` would be misread as one of those two on sight.

`processed` is ALSO incremented for a skipped position, at the new site in
`submit_next`. This preserves its existing meaning -- positions retired by the
walk -- which is what #317's `commit_census.walk_claimed` reads.

The alternative -- excluding skips from `processed` -- was rejected precisely
because it would silently redefine the number `commit_census` gates on, turning
a clean skip-heavy resume into a reported lost commit.

**An earlier revision of this section claimed `walk_vs_graph` becomes an
independent gate on the predicate's soundness. That is FALSE and is struck.**
`_ingest_progress["processed"]` is seeded with
`prior_ingested = _count_commit_entities(db)` and then incremented for every
position retired this run, INCLUDING positions already counted in that seed. So
`walk_vs_graph` is nonzero on any resume that touches already-ingested
territory, skip or no skip -- measured 10 with the fast path against 9 without,
on the same scenario. It cannot discriminate a wrong skip from an ordinary
resume, and it was the only stated backstop, which made it the worst possible
place for a wrong safety-net claim.

`repo_vs_walk` is gated only on `final_status == complete`. A complete resume
run over a discarded region over-counts, for the same reason. **That over-count
exists today and is unchanged by this work** -- today those positions are
re-walked and `processed += 1` for each. #326 neither fixes nor worsens it. The
at-scale gate runs on a fresh ingestion-only graph, so it cannot fire there.

State the consequence plainly rather than substituting a different overclaim:
**no existing gate catches a wrong skip.** `fact_audit`'s `divergence` reads 0,
because a skipped commit reaches neither the graph nor the index and the two
witnesses agree about its absence; `introduced_by_duplicates` and
`entities_without_introduced_by` only examine entities that EXIST;
`stderr_capture` has nothing to read; and `commit_census` runs on a fresh
ingestion-only graph where no archived region exists. The predicate's soundness
rests on the witness, on its positive control (the #313 torn-position test), and
on the two stored denominators below -- not on anything downstream noticing
afterwards.

## The denominators

Two guards, added after the first whole-branch review, both of the
"ship the positive control with the number" kind #316 established.

**1. A region is stored as HASHES but consumed as a closed POSITION RANGE.**
(The denominator below is a CHECKSUM, not a proof of set identity -- see the
residual at the end of this section.)
Every position between the bounds is treated as proven-complete, which is sound
only if the linearization grew by APPENDING above the region.
`git log --topo-order --reverse` guarantees no such thing: it places a new
commit immediately after its branch point whenever the old tip's line stalls
behind it, and "branch off an old commit, merge the mainline in, fast-forward
the mainline" produces exactly that. Such a commit lands INSIDE the archived
bounds and is skipped -- lost permanently and silently, since it reaches
neither graph nor index.

So `:ingestion/frontier-high` (and `-low`) carries `:pos-count`, the span it was
CLAIMED under, written at claim time in the same transact as the bound that
moved -- different attributes, so minigraf#287 does not reach the batch, and the
per-commit persist stays at two DB calls. `_frontier_load` archives a discarded
interval only when its stored count still equals its current span. The count
MUST originate at claim time: one computed where the region is archived is
computed from the very span it would then be compared against, so it always
agrees and discriminates nothing. Archived regions carry the same denominator
and `_completed_regions_load` re-checks it against the current linearization,
which covers an insertion arriving in a LATER run. An interval or region
carrying no count is not trusted -- "no denominator" and "a denominator that
still checks out" must not be the same branch here. A mismatch costs one full
re-walk, which is exactly master's behaviour.

**1b. RESIDUAL: an equal count is not the same member set.** The `:pos-count`
guard catches a span that changed length, which is the reachable case. It cannot
catch a rearrangement that preserves the length: an old commit inside the range
that is neither ancestor nor descendant of `lo` could be reordered below `lo` by
a later `git log --topo-order` while a new commit lands inside, leaving the count
unchanged and the region trusted. A real repository realizing this was **NOT
constructed** -- git's tie-breaking constrains which of the many valid
topological orders it actually emits, and the attempt did not produce one. So
this is an undemonstrated residual, not a measured loss, and it is recorded
because the earlier wording ("the denominator makes the mapping SOUND")
overstates what a checksum can do. Closing it takes a per-position completion
marker (approach B above), at one fact per commit on the write path this issue
exists to make cheaper -- which is exactly the trade B was rejected on, now with
the residual it leaves stated on both sides.

**2. The end-of-walk flush's hi bound is the highest SKIPPED position, never
the highest reverse position CLAIMED.** `_frontier_persist_span` moves
`:hi-hash` UP, which `_frontier_persist_claim` never does for the high interval.
A reverse position whose write fails takes the per-commit `except`, which does
`processed += 1`, persists no claim, and leaves `completed_all` True; a flush
bounded by the highest claim raises the persisted top bound over it. Once
`:hi-hash` reaches the tip the interval is representable, so the next
`_frontier_load` RETAINS it instead of discarding it and nothing re-walks those
positions ever again. The skipped span is the only thing the flush is entitled
to assert.

**3. The region's ident carries its TAG.** Two regions sharing a low hash but
differing in tag would otherwise collide onto one entity, and
`_completed_regions_read`'s join returns their cross product -- including a
provisional region larger than anything proven, which `_skip_claim` would
honour. Not reachable while only `:provisional` is archived, but the tag is
stored and checked precisely so a future authoritative discard cannot silently
license a forward skip, and the collision does the opposite.

**4. Ordering.** `_completed_region_record`'s merge loop compares regions
archived under DIFFERENT linearizations using CURRENT positions, so a stale
region left in the candidate set manufactures coverage over positions in
neither original region. The denominator guard is therefore applied BEFORE the
coalescing, never after.

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
