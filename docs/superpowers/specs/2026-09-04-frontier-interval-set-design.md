# #222 Phase 3 / #325 — Frontier Interval Set

Persist the provisional side of the ingestion frontier as a SET of disjoint
intervals, so a branch tip that grows under a running or finished ingestion no
longer discards the reverse frontier.

- Issue: #325 ("resume the reverse stream from `:ingestion/frontier-high`
  across tip growth instead of discarding it and re-walking ~8k commits")
- Parent: #222 phase 3 (Stream 3 tip fill) plus the "fold a 2c-confirmed high
  region into `frontier-low` instead of discarding it" item carried out of 2d
- Predecessor: #326 (`docs/superpowers/specs/2026-09-03-skip-fast-path-completion-witness-design.md`)

## What #326 already fixed, and what is left

#325's headline measurement is an ~18 h replay: a `git fetch` landed 17 commits
during an ArangoDB `origin/4.0` ingestion, the next run's linearization was 17
positions longer, `_frontier_load` discarded the high interval, and the reverse
stream restarted from HEAD with ~7,365 already-ingested positions to re-walk at
~6.6/min while holding 90% of the scheduling slots.

**That cost is already gone.** #326's `_skip_claim` fires in `submit_next`
BEFORE the parse is queued, so after a discard those positions retire as a pure
in-memory interval scan — microseconds each, no `git show`, no tree-sitter
parse, no write batch, no checkpoint, no handle drop. The reverse stream then
genuinely walks only the new tip commits and the real gap below the archived
region, and `_frontier_persist_claim` plus the end-of-walk
`_frontier_persist_span` flush re-form one contiguous interval. Mechanically
that is already #325's "merge on contact", reached by re-claiming-and-skipping
rather than by never discarding.

Two caveats on that, both real:

* A `frontier-high` written before #326 carries no `:pos-count`, so the FIRST
  tip-growth event after upgrading archives nothing and pays the full re-walk.
  One-time, and self-correcting afterwards.
* The skip rests on a `:pos-count` CHECKSUM, not a proof of set identity.

So this phase is not about the replay cost. It is about the four things the
discard branch still gets wrong, and about removing the discard as the way tip
growth is handled:

1. The persisted model cannot express the state that actually exists after tip
   growth (authoritative low, filled high, new unclaimed above). One `lo`/`hi`
   pair per side is not enough, which is the whole reason the discard exists.
2. `:ingestion/correction-sweep-through` has no defined meaning across a
   discard: a finished run parks it at the old `:hi-hash`, and after a discard
   `_correction_sweep_select_position` restarts from a NEW `frontier-high.lo`,
   so the rebuilt region is authoritative by assumption rather than by sweep.
3. A persisted bound hash that is no longer in the linearization leaks: the
   outer guard in `_frontier_load` fails, no interval loads, AND the facts stay
   in the graph, so the next `_frontier_persist_claim` reads a non-`None`
   `existing` and extends bounds the allocator does not believe in.
4. `rev_claim_floor_pos` is a run-global scalar, which stops being correct the
   moment the reverse stream serves more than one gap in a run.

## Non-goals

* **Rewritten history (force-push / rebase) is phase 5's.** Today the discard
  branch is what nominally "absorbs" it; this phase removes the discard from
  the tip-growth path, so item 3 above is closed explicitly (retract the stale
  facts) and nothing more is claimed. A divergent ref falls back to a full
  re-walk, stated rather than implied.
* **Per-stream ingest status is phase 4's.** A fragmented provisional side makes
  the current single reverse position more misleading than it already is; that
  goes on phase 4's inherit list, it is not built here.
* **Closing the `:pos-count` checksum residual** (a per-position completion
  marker, approach B in #326's spec) is not attempted. See "Residual" below.
* **No `GRAPH_FORMAT_VERSION` bump and no migration.** This only adds facts
  going forward.

## The model

Ingestion state becomes:

* one AUTHORITATIVE interval anchored at position 0 — `:ingestion/frontier-low`,
  unchanged, grows upward;
* a SET of disjoint PROVISIONAL intervals. `:ingestion/frontier-high` keeps its
  fixed ident and means **the lowest provisional interval**. Any others are
  strictly above it with a hole between, and persist as their own entities.

That invariant — `frontier-high` is the lowest provisional interval — is what
keeps the sweep-side readers nearly untouched. Both of them care about the
boundary between authoritative and provisional territory, and that boundary is
`frontier-high.lo` whether or not the provisional side is fragmented.

### Allocator

`FrontierAllocator` generalizes from "two anchored intervals and one shared gap"
to "sorted disjoint intervals; the gap is their complement":

* `claim_low()` -> lowest unclaimed position. Still strictly ascending across
  runs, so `_forward_apply`'s state-machine precondition holds.
* `claim_high()` -> highest unclaimed position. With a hole above
  `frontier-high` that is the new tip, so the reverse stream serves the topmost
  gap first and falls through to the bulk gap when it closes. This is #325's own
  "serve whichever unclaimed high region is topmost" alternative, obtained from
  the definition of the gap rather than from a precedence rule bolted on top.
* `is_gap_empty()` -> the complement is empty, i.e. EVERY hole is closed.

`_coalesce` already merges same-tag intervals on contact and is unchanged, and
`_adjacent_interval`'s existing docstring already describes exactly this
two-provisional-interval state (it was written for the reload-after-growth case
and explains why "first interval COVERING the neighbour" is the wrong pick).
The allocator was written in anticipation of this; only persistence collapsed it.

One addition: `claim_low()`/`claim_high()` return the position AND the resulting
interval AND any interval the coalesce swallowed. That lets the persist path
mirror an in-memory merge without re-reading the interval set once per commit.

## Persistence

A provisional interval above `frontier-high` persists as its own entity:

```
:ingestion/interval-provisional-<hi_hash[:12]>
  :entity-type :type/ingest-interval
  :ident       "…"            ; string-valued, for enumeration
  :tag         :provisional
  :lo-hash     "…"
  :hi-hash     "…"
  :pos-count   N
```

**The ident is fixed at creation from the interval's initial `hi-hash`, and
never changes.** A provisional interval grows DOWNWARD, so `hi` is its stable
anchor while `lo` moves on every claim; keying on `lo` would recreate the entity
per commit. Current bounds are never used to re-derive the ident afterwards —
that is #326's ident-keyed-not-bounds-keyed rule, whose violation collided two
regions onto one entity, made `:pos-count` nondeterministic through a
last-write-wins join, and made a retract destroy the surviving witness. Creating
an interval at an ident that already carries facts retracts them first.

**`frontier-high` and `frontier-low` are still read by fixed ident; only the
extras are enumerated** (binding `?ident` via the string-valued `:ident`, since
`[?e :entity-type :type/ingest-interval]` binds the entity in UUID space). So
there is nothing to migrate: an existing graph has zero extra intervals and
behaves exactly as today until its first tip growth.

**`:pos-count` is maintained at claim time**, per interval, by the existing
`_frontier_pos_count_delta` — batched into the same transact as the bound that
moved, so the per-commit persist stays at its current two DB calls. It MUST
originate at claim time, not where an interval is later read or archived: a
count computed from the very span it is then compared against always agrees and
discriminates nothing.

**The retention test changes, and the count check becomes mandatory rather than
defensive.** Today `_frontier_load` retains the high interval only under
`hi_hi_pos == len(linearization) - 1`, and that retain path performs NO count
check. That is safe today by accident: a genuinely new commit implies a new tip,
so a commit landing strictly inside the bounds forces `hi != last` and pushes
the case onto the discard path where #326's count check lives. Retaining
`hi < last` removes the accident. So an interval is retained iff:

* both bounds resolve in this linearization, and
* `lo_pos <= hi_pos`, and
* the stored `:pos-count` equals `hi_pos - lo_pos + 1`.

An interval carrying no count is not retained — "no denominator" and "a
denominator that still checks out" must not be the same branch when the failure
mode is silent permanent loss.

**The discard branch narrows, it does not disappear.** What survives of it is
the genuinely unrepresentable cases: inverted bounds, and a failed count. Those
still archive to `:type/completed-region` and still discard, so `_skip_claim`
remains the backstop for precisely the states in which an interval can no longer
be trusted as an interval.

**The divergent-ref leak is closed here** (item 3). An interval whose bound hash
is absent from the linearization gets an explicit branch: archive first when the
interval is otherwise well-formed, then retract. It must not be left in the
graph for the next `_frontier_persist_claim` to extend.

`:type/ingest-interval` stays deliberately ABSENT from `MINIGRAF_SCHEMA`, like
`:type/completed-region`: `handle_minigraf_audit` iterates exactly the registered
types and would otherwise retract its attributes. Every write goes through the
internal `_transact`/`_retract`; the public handler rejects an unregistered type
outright.

## The walk

### `rev_claim_floor_pos` becomes per-interval

This is the one place the new model breaks something that currently works, and
it must be fixed in the same change.

The floor is #326 Finding A's fix: the highest reverse position this run failed
to complete, past which `:lo-hash` may not descend, because `:lo-hash` is a
CLOSED RANGE bound and a write that raises takes `_run_ingestion`'s per-commit
`except`, which logs, does `processed += 1`, and CONTINUES THE DESCENT — so the
next lower position that succeeds would otherwise sweep the failed one into the
interval. With one contiguous reverse descent, a run-global scalar is exactly
right.

With multiple gaps it is not. The reverse stream descends gap A, closes it, then
jumps to gap B entirely BELOW A. A failure anywhere in A leaves a floor above
every position in B, so `pos > floor` is false for the whole bulk gap: every
bulk-gap claim would do the work and withhold the bookkeeping, and the next run
would re-walk all of it. Silent, and it would read as a mysterious loss of
resume progress.

The floor becomes one per provisional interval — the highest incomplete position
within the interval this claim extends. Same guarantee, stated over the unit it
was always really about. Both incompleteness paths still raise it (a write that
raised, an extraction that raised); the shutdown `break` still needs no floor,
because nothing lower ever claims after a shutdown and `completed_all = False`
already gates the flush off.

### The end-of-walk flush

Targets the interval it belongs to rather than the fixed high ident, and clamps
against THAT interval's floor. Otherwise unchanged, including the two rules that
are easy to get wrong and were paid for once:

* its `hi` bound is the highest SKIPPED position, never the highest claimed —
  a reverse position whose write failed persists no claim but leaves
  `completed_all` True, and a flush bounded by the highest claim would raise the
  persisted top bound over it;
* it stays gated on `completed_all`, because the shutdown `break` leaves
  `pending` entries claimed-and-queued but never applied, and #326's own shape
  makes that the likely case rather than an exotic one.

### Stage B

`_correction_sweep_select_position` gets ONE added clause and no new watermark
semantics: on top of the existing gap-closed test (`low.hi + 1 == high.lo`), it
requires that **no provisional interval exists above `frontier-high`**. While a
hole remains the sweep declines — correct, because Stream 2 can still descend
past a position the sweep would otherwise confirm. Once everything has coalesced
there is exactly one provisional interval and the old test is precisely right.

That clause is also what makes `:ingestion/correction-sweep-through` work across
tip growth without redefining it (item 2). With no discard, `frontier-high.lo`
no longer moves; a finished run's watermark stays parked at the old tip; once
the tip gap merges in, the ceiling becomes the new tip; and `through + 1 ..
ceiling` is exactly the set of new commits. The failure mode this replaces is
NOT "the sweep re-runs" — it is that after a discard both the ceiling and the
region were rebuilt, so the merged region was authoritative by assumption and
never actually swept.

`_should_fold_lineage_watermark` gets the same clause, so the lineage fold
cannot fire while the provisional side is fragmented.

**Stage B starvation is reduced, not eliminated.** A per-interval floor means a
deterministic failure in the tip gap no longer starves the sweep on the bulk
region's behalf. But an open hole still declines the sweep for that run, so a
deterministic failure at a fixed position still costs a run's worth of lineage
confirmation and of `_forward_apply(..., lifecycle_only=True)`'s D/R closes,
renames, dependency churn and gitlink changes for that span. That is inherent to
a precise interval and is the accepted price, unchanged from #326. It is LOUD
rather than silent: `stderr_capture`'s `_SKIPPED_COMMIT_RE` matches both the
extraction-failure and write-failure log lines, gated by
`run_ingestion_benchmark._exit_code`.

### Unchanged on purpose

* `_skip_claim` and the completed-region machinery stay, as the backstop for
  intervals that fail the resolve-or-count tests.
* `fwd` NEVER skips. One clause buys two properties: a forward claim inside a
  provisional region is the authority upgrade that must still happen, and
  `_forward_apply` mutates `_ForwardWalkState`'s cross-position preload dicts in
  place, so a skipped forward position desynchronizes that state silently.
* A skipped position still increments `processed`, because #317's
  `commit_census` reads it as `walk_claimed`; excluding skips would silently
  redefine the number that gate compares against `git rev-list`.
* The counter stays `positions_skipped` — `status` already takes the value
  `"skipped"` and `stderr_capture` already reports `skipped_commits`.

## Risk, and why the tests below are the whole guard

**No existing at-scale gate catches a wrongly-retained interval.** A position
inside a retained interval is never claimed, so:

* `fact_audit`'s `divergence` reads 0 — the commit reaches neither the graph nor
  the index, so the two witnesses agree perfectly about its absence;
* both `:introduced-by` checks only examine entities that EXIST;
* `stderr_capture` has nothing to read;
* `commit_census` runs on a FRESH ingestion-only graph, where no retention can
  occur at all.

Retention also becomes the common path rather than a rare one, so the checksum
residual gets exercised on every tip growth instead of only through a discard.
This is the same class of failure #326 shipped three Criticals against:
permanent silent commit loss that every existing detector reads clean.

## Residual (stated, not papered over)

`:pos-count` is a CHECKSUM, not a proof of set identity. Equal count does not
imply the same member set: an old commit inside the range that is neither
ancestor nor descendant of `lo` could be reordered below `lo` by a later
`git log --topo-order` while a new commit lands inside, leaving the count
unchanged and the interval trusted. #326 attempted to construct a real
repository realizing this and could not — git's tie-breaking constrains which of
the valid topological orders it actually emits — so it is an UNDEMONSTRATED
residual, not a measured loss. Do not upgrade this language to "sound" without
constructing one. Closing it takes a per-position completion marker at one fact
per commit on the write path this arc exists to make cheaper.

## Testing

Real-backend only, per `docs/testing-conventions.md`. Every regression test is
ablation-proven: the counterfactual must be the real old code, and each test
must be watched to FAIL before it is believed.

1. **Tip growth does not re-walk.** Ingest N commits, add M above, re-ingest.
   Assert the reverse stream claims exactly M positions and `frontier-high` was
   retained. Counterfactual on master: N+M claims and a discard.
2. **Second growth before merge.** Interrupt inside the tip gap, grow the tip
   again, re-ingest — the case that defeats a single extra fixed ident. Assert
   two provisional intervals persist and reload, and that the sweep declines
   while a hole remains.
3. **Per-interval floor.** Force a write failure in the tip gap while a bulk gap
   is open below. Assert bulk-gap claims still persist their bookkeeping.
   Ablation with a run-global floor: they do not, and the next run re-walks the
   bulk gap.
4. **Torn write still re-walked.** #313's positive control under the new model;
   `TestSkipFastPathDoesNotSkipTornWrites` must still pin BOTH halves — that a
   torn position is not skipped, and that the rejected commit-entity predicate
   WOULD have skipped it.
5. **Divergent-ref leak.** A persisted bound absent from the linearization is
   retracted rather than surviving to be extended. Ablation: current code leaves
   the facts and the next claim extends them.
6. **Insertion inside a retained interval.** Construct the "branch off an old
   commit, merge the mainline in, fast-forward the mainline" repository; assert
   the count check refuses the interval and the region is fully re-walked. If a
   repository realizing the EQUAL-COUNT variant still cannot be constructed, it
   stays the residual above.

### The one new gate

`commit_census` today only ever sees a fresh ingestion-only graph, so it cannot
observe the failure this change makes possible. The at-scale tier gains a
**resume scenario**: ingest to a point, advance the branch, re-ingest, then run
the same three-way `git rev-list --count <branch>` / `walk_claimed` /
`_count_commit_entities` comparison on the RESUMED graph.

* Zero tolerance, with `repo_commits` shipped as its own denominator so a run
  that proved nothing renders as "proved nothing" rather than as a pass.
* Its clean value is MEASURED before it is wired, never predicted — the
  `:type/external-dependency` trap (a gate that would have been permanently red
  on its first run) and #317's own three-way measurement are the precedents.
* It reads `repo_vs_walk` and the graph commit count, NOT `walk_vs_graph`:
  `_ingest_progress["processed"]` is seeded with
  `prior_ingested = _count_commit_entities(db)` and then incremented for every
  position retired this run, so `walk_vs_graph` is nonzero on ANY resume and
  discriminates nothing there.
* An absent key stays clean, same precedent as every other census key.

### Cost acceptance

The per-commit persist stays at its current query/write count: the allocator
reports coalesces in memory, so nothing re-reads the interval set once per
commit. Verified against the existing `MINIGRAF_INGEST_TRACE_PATH` per-commit
trace on the at-scale tier, measured rather than asserted.
