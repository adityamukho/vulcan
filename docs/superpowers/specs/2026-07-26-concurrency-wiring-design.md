# Concurrency Wiring — Design Spec

**Issue:** #222 (Phase 2, sub-phase 2d of 4 — the last one)
**Date:** 2026-07-26

## Background

#222's overall design is a converging multi-stream ingestion: a forward-truth
stream (the existing `_run_ingestion` engine) and a reverse-bulk-fill stream
that provisionally back-fills recent history from `HEAD` downward, so recent
history becomes visible almost immediately while the forward stream still owns
lineage correctness.

- **Phase 1** (merged, PR #226) built `frontier_registry.py`
  (`Interval`/`FrontierAllocator`/`build_linearization`) plus
  `_frontier_load`/`_frontier_persist_claim`/`_frontier_seed_from_watermark` —
  the shared-gap allocator both streams claim work from.
- **Phase 2a** (merged, PR #227) built the provisional/authoritative fact
  model: `_lineage_mark_provisional`/`_lineage_confirm`/`_lineage_is_provisional`
  (a companion `:type/lineage-marker` entity per tracked entity),
  `_lineage_confirmed_through_*`, and `_candidate_diff_persist`/`_read`/`_clear`.
- **Phase 2b + 2b1** (merged, PR #228) built Stream 2's reverse-bulk-fill walk
  (`_reverse_fill_claim_and_process`/`_reverse_bulk_fill_walk`) plus the
  allocator adjacency/coalescing corrections and the EAVT-collision splits.
- **Phase 2c** (merged, PR #229) built the correction sweep
  (`_correction_sweep_select_position`/`_correction_sweep_apply` and two
  synchronous convenience wrappers), which converts 2b's provisional facts to
  authoritative ones once the gap has closed.

**Every one of those sub-phases is inert against production ingestion.**
Nothing built so far is called from `_run_ingestion`. 2d is the sub-phase that
wires all of it in, and is therefore the first point at which #222 delivers any
user-visible behaviour at all.

### Constraints inherited from the 2b/2c reviews

These were established by review, not preference, and 2d cannot design them
away:

1. **The correction sweep is terminal and post-convergence.**
   `FrontierAllocator.claim_low()`/`claim_high()` partition one shared gap: a
   position claimed by either side is never returned to the other, so no
   sequence of `claim_low()` calls can reach territory `claim_high()` already
   claimed. The sweep is a third, strictly sequential pass with its own
   gap-closed precondition (`frontier-low.hi + 1 == frontier-high.lo`), not a
   third concurrent task.
2. **Nothing Stream 2 writes is trusted until that sweep completes.** On a
   large repo that is close to the full ingest duration, not a small tail.
3. **The sweep's three pieces must stay independently schedulable**, and
   neither `_correction_sweep_claim_and_process` nor `_correction_sweep_walk`
   may be called from async code — both fuse the CPU-bound parse and the
   DB-bound writes into one function body.
4. **`commit_metadata` must be full-history**
   (`_git_commits(repo, watermark_hash=None, branch)`), not `_run_ingestion`'s
   current watermark-relative list. Both 2b and 2c index it positionally
   against `linearization`, and 2b raises `ValueError` on a length mismatch.

## Design decisions

### Single interleaved loop, not two asyncio tasks

The issue's own text says "two asyncio tasks in one process", but that framing
predates knowing how `_run_ingestion` is actually built. Every DB write in this
server already serialises on `write_executor`, a deliberately
`max_workers=1` `ThreadPoolExecutor`, and every parse already fans out to a
`ProcessPoolExecutor`. Two asyncio tasks would therefore contend for the same
single write thread and gain no throughput whatsoever, while adding:

- a shared `_db` global whose lifecycle is `_db = None` between commits — with
  two tasks, one clears it mid-commit for the other;
- nondeterministic interleaving, so the fairness property under test is
  probabilistic;
- an explicit fairness mechanism (semaphore/turnstile) that has to be designed,
  tuned and tested.

Instead, Stage A is **one coroutine over one tagged prefetch pipeline**.
Fairness *is* the submit order — starvation is not expressible, and the
interleave is deterministic and directly assertable in a test.

This is a change to the issue's stated mechanism, not to its goal. The
user-visible property #222 asks for — recent history usable long before the
ingest finishes — is delivered identically.

### 1:1 default ratio, env-overridable

A commit claimed by Stream 1 is parsed **once** and is authoritative
immediately. A commit claimed by Stream 2 is parsed **twice**: once by the
reverse walk, then again by the correction sweep, which calls `_extract_commit`
rather than reading 2a's candidate-diff hashes (2c's final design deliberately
dropped the hash-comparison shortcut; the records are now a cleanup obligation
only). Total parse cost is therefore `N × (1 + reverse_fraction)`.

At 1:1 that is ~1.5N parses with a ~0.5N terminal sweep. `HEAD` itself is
visible after the first round regardless of ratio — what the ratio controls is
how *deep* into recent history the reverse stream descends per unit of total
progress.

`MINIGRAF_INGEST_STREAM_RATIO="F:R"` overrides it (e.g. `"1:3"` to prioritise
recent-first harder on a large repo, `"3:1"` to minimise total work). A
malformed or non-positive value logs one line to stderr and falls back to
`1:1` rather than raising — this is a background ingestion coroutine, and a bad
env var must not be the reason a repo never ingests.

### Extract the per-commit forward body

`_run_ingestion` is ~630 lines, ~380 of them a single inline per-commit write
section inside `while pending:`. Interleaving requires that body to be callable
per-commit, and the tagged pipeline requires the drain loop to dispatch on tag.
The body is therefore extracted to `_forward_apply`.

That body mutates ten preload dictionaries (`entity_valid_from`,
`entity_descriptions`, `file_entities`, `file_deps`, `dep_valid_from`,
`pinned_commit_state`, `field_class_ident`, `field_static_ident`,
`submodule_paths`, `unresolved_dep_idents`). Passing ten mutable dicts as ten
parameters is how this function becomes unreviewable, so they are bundled into
a `_ForwardWalkState` dataclass constructed once from
`_load_ingestion_preload_state`'s return.

This is a large mechanical diff over already-reviewed code, and the extraction
itself is the single largest implementation risk in the phase. It is worth it:
it makes the forward step unit-testable for the first time (today it is
reachable only through a whole ingestion run), and it cuts the file's worst
function to a readable driver.

## Architecture

`_run_ingestion` becomes a driver over three sequential stages:

```
preload state (_load_ingestion_preload_state, + provisional_idents)
release DB lock
linearization  = frontier_registry.build_linearization(repo_path, branch)
commit_metadata = _git_commits(repo_path, None, branch)        # FULL history
allocator      = _frontier_load(db, linearization, run_ts)     # migrates watermark
        │
Stage A ├── interleaved walk: tagged pipeline, until the allocator gap is empty
        │
Stage B ├── correction sweep: 2c's three pieces, until select returns None
        │
Stage C └── _ingest_tags + _last_run_write   (only if completed_all)
```

### Stage A — the tagged pipeline

The existing pipeline already pre-submits `pipeline_depth = max_workers * 2`
extractions to the process pool from a plain iterator. 2d replaces the iterator
with the allocator:

```python
def submit_next() -> bool:
    tag, pos = _next_claim(allocator, round_state)   # "fwd" -> claim_low, "rev" -> claim_high
    if pos is None:
        return False                                  # gap empty: BOTH sides are done
    fut = loop.run_in_executor(
        executor, _extract_commit, repo_path, linearization[pos], ignore_patterns
    )
    pending.append((tag, pos, fut))
    return True
```

`_next_claim` walks the `F:R` ratio as a round: `F` forward claims, then `R`
reverse claims, repeat. There is no "the other side might still have work"
fallback, and deliberately so: `claim_low()` and `claim_high()` both return
`None` on exactly the same condition, `is_gap_empty()`, so they can only ever
return `None` together. A single `None` from whichever side's turn it is
therefore means the gap is empty and Stage A is finished submitting. A
fallthrough to the other side would be dead code that reads as if the two
frontiers could exhaust independently — they cannot; they share one gap.

The drain loop is FIFO, so **relative order within each tag is preserved**:
forward stays strictly ascending, reverse strictly descending. Both streams
require exactly that — the forward walk's `_ForwardWalkState` is an ascending
state machine, and 2b's monotonicity guard and progress guard both assume a
descending claim sequence.

Each drained entry dispatches on its tag to `_forward_apply` or
`_reverse_apply`, both of which run on `write_executor` and are DB-bound and
parse-free.

### Stage B — the correction sweep, driven asynchronously

2c's own docstrings forbid calling `_correction_sweep_claim_and_process` or
`_correction_sweep_walk` from async code. Stage B therefore drives the three
pieces directly, each on its correct executor:

```python
hash_to_pos = {h: i for i, h in enumerate(linearization)}
skipped = 0
while not _shutdown_requested.is_set():
    selected = await loop.run_in_executor(
        write_executor, _correction_sweep_select_position,
        db, linearization, commit_metadata, hash_to_pos,
    )
    if selected is None:
        break
    commit_hash, commit_ts_iso = selected
    file_results, *_ = await loop.run_in_executor(
        executor, _extract_commit, repo_path, commit_hash, ignore_patterns,
    )
    skipped += await loop.run_in_executor(
        write_executor, _correction_sweep_apply,
        db, commit_hash, commit_ts_iso, file_results, index_con, skipped,
    )
_correction_sweep_log_summary(skipped)
```

`hash_to_pos` is built once and threaded through, and `skipped_so_far` is
threaded through every `_correction_sweep_apply` call — the two obligations
2c's spec places on 2d's own loop. `_correction_sweep_log_summary` is called
exactly once at the end so an operator grepping for that line does not have to
know which loop drove the sweep.

Pipelining `_extract_commit` ahead of `_correction_sweep_apply` is permitted by
2c provided the `_correction_sweep_apply` calls stay in ascending position
order. The initial implementation does **not** pipeline: `select_position`
reads `:ingestion/correction-sweep-through`, which only advances inside
`_correction_sweep_apply`, so a prefetching version has to predict positions
rather than select them. That is a real optimisation, and it is deliberately
out of scope here.

## The correctness core: forward-introduction reconciliation

This is the part that turns 2d from wiring into lineage surgery, and it is not
optional — without it, essentially every entity alive at `HEAD` but introduced
low in history ends up with **two** `:introduced-by` values, which
`_correction_sweep_apply` then reads as an ambiguous count, hits its case-2
fail-safe on, and leaves provisional forever.

The root cause is an asymmetry between the two streams:

- `_reverse_fill_claim_and_process` determines what it already knows by
  querying the DB per candidate ident (`_entity_introduced_by_query`), so it
  sees the forward stream's writes immediately and correctly routes them into
  its `already_authoritative_touched` branch, which never clobbers.
- The forward walk determines what it already knows from `entity_valid_from`,
  an **in-memory dict preloaded once at run start** that never sees Stream 2's
  writes.

That produces two distinct failure modes:

- **Same run.** The entity is absent from `entity_valid_from`, so
  `_build_code_triples` emits an authoritative `:introduced-by` on top of
  Stream 2's provisional one. Two values; detectable.
- **Resumed run.** Stream 2's structural facts from the previous run *are* in
  the preload, so `entity_valid_from` contains the entity and
  `_build_code_triples` suppresses the introduction entirely. The entity keeps
  a provisional `:introduced-by` pointing at a wrong, far-too-late commit,
  permanently. This one is silent — there is no duplicate to notice.

### The fix

`_load_ingestion_preload_state` additionally loads `provisional_idents`: the
set of tracked idents that currently have a `:type/lineage-marker` companion
entity.

```
[:find ?e :where [?m :entity-type :type/lineage-marker] [?m :entity ?e]]
```

No `:status` clause is needed. The marker exists **only** while the entity is
provisional — `_lineage_confirm` retracts the whole companion entity rather
than flipping its `:status` — which is the same existence test
`_lineage_is_provisional` performs per-ident. This is the set form of that
query, so the preload cannot disagree with the per-ident check the
reconciliation later relies on.

The forward walk's introduction gate widens from
*"ident not in `entity_valid_from`"* to *"ident not in `entity_valid_from`
**or** ident in `provisional_idents`"*.

The gate itself lives in `_build_code_triples`, whose `entity_valid_from`
membership test means "is this the introduction" only for a forward walk. 2d
does **not** widen that function's signature. Instead `_forward_apply` pops
each provisional ident out of `entity_valid_from` before calling
`_build_code_triples` for that file, which is also the semantically correct
statement: a provisionally-introduced entity has not been authoritatively
introduced, and the `valid_from` Stream 2 recorded for it is a wrong guess
that must not be used as an `orig_ts` for anything.

Then, for each such ident, `_forward_reconcile_provisional` runs **before**
the normal forward emission:

1. Retract the provisional `[ident :introduced-by <guess_commit>]`.
2. Retract and re-transact the entity's structural triples at this commit's
   (true, earlier) timestamp — otherwise there is a valid-time window where
   `:introduced-by` is live for an entity with no type, name or file, so
   `:as-of` the true introduction reports it as nonexistent. This is exactly
   the re-dating `_reverse_fill_claim_and_process` already performs on a
   provisional move; `:contains` triples go one per `_retract`/`_transact`
   call, on both sides, for the EAVT-collision reason 2b1 established.
3. `_lineage_confirm(db, ident)` — flip the marker to authoritative.
4. `_candidate_diff_clear(db, <guess_commit_hash>, ident)` — the record is
   stale the moment the guess moves off it, and this is the cheapest place to
   drop it, mirroring 2b1's own supersede path.
5. Transact `[ident :modified-in <guess_commit>]` at the **guess commit's own**
   timestamp, not this commit's. The guess commit is now known to be a genuine
   modification rather than the introduction, and back-dating the edge to this
   commit would assert a fact valid before it was true.

Normal forward emission then writes the authoritative `:introduced-by` at the
true introduction commit, and `entity_valid_from`/`entity_descriptions` are
populated as they would be for any newly-introduced entity.

Step 5 inherits 2b's documented over-assertion: it cannot check #221's
unchanged-body narrowing against the guess commit's own diff, because only that
commit's own parse carries the data. That is already owned and needs no new
machinery — the guess commit lies inside frontier-high's territory, so Stage B
visits it, finds exactly one `:introduced-by` (case 3, already authoritative),
and retracts the `:modified-in` edge if its own parse says the body was
unchanged there.

This also closes the "wrong provisional guess" reconciliation 2c proved could
not be handled within its own walk and left explicitly unowned: the correction
belongs to whichever walk processes the entity's true earlier occurrence, which
is precisely the forward walk, here.

## Watermarks and persistence

- **`:ingestion/watermark`** — advanced by the forward stream only, once per
  applied commit, exactly as today. Its meaning is preserved: the forward
  stream claims contiguously upward from `C0`, so the watermark remains the
  contiguous-from-`C0` frontier. The reverse stream must never touch it.
- **`_frontier_persist_claim`** — called once per applied commit,
  `from_low=True` for forward and `from_low=False` for reverse. New for the
  forward side; the reverse side already does this inside
  `_reverse_fill_claim_and_process` and keeps doing it in `_reverse_apply`.
- **`:ingestion/lineage-confirmed-through`** — advanced by the forward stream
  per applied commit (the virgin positions it claims are authoritative on
  first write). It is folded forward to `frontier-high`'s `:hi-hash` when the
  sweep genuinely completes.

  **Stage B's exit condition alone must not trigger that fold.**
  `_correction_sweep_select_position` returns `None` for six different
  reasons, only one of which is "reached the ceiling": it also returns `None`
  when `frontier-high` is absent, when either boundary hash is stale, when the
  gap is still open, and when `commit_metadata` violates its contract. Folding
  the watermark on any `None` would claim lineage is confirmed through `HEAD`
  in exactly the situations where the sweep did no work at all.

  Stage B therefore performs the fold only on an explicit positive check after
  its loop ends: re-read `_correction_sweep_through_query(db)` and
  `_frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)`, and fold only if the
  former equals the latter's `:hi-hash`. Otherwise the watermark is left where
  the forward stream put it, which correctly reports the region as
  unconfirmed.
- **`:ingestion/correction-sweep-through`** — 2c owns it; Stage B advances it
  via `_correction_sweep_apply` and 2d does not touch it directly.

The prefetch pipeline pre-claims positions in memory ahead of persisting them,
so a crash re-walks at most `pipeline_depth` positions on the next run. Every
write path involved is idempotent, so this costs work and nothing else.

## Termination, degenerate cases, shutdown

**Stage A ends** when `submit_next()` returns `False` (both `claim_low()` and
`claim_high()` returned `None`) *and* `pending` has drained.

**Stage B ends** when `_correction_sweep_select_position` returns `None`.

The degenerate cases the issue enumerates fall out of this without special
casing:

| Case | Behaviour |
|---|---|
| Gap already empty on resume | `submit_next()` returns `False` on its first call; Stage A does nothing |
| Empty or single-commit repo | `build_linearization` returns 0 or 1 entries; the allocator handles it, Stage B sees `high_bounds is None` and no-ops |
| `lo == hi`, one position left | Whichever side's turn it is claims it; the other observes an empty gap. Exactly-once by construction |
| Forward claimed everything | `frontier-high` is never created, `_correction_sweep_select_position` returns `None` immediately (2c's `high_bounds is None` branch) |
| Reverse claimed everything | `frontier-low` is never persisted, and 2c's `low_bounds is None` branch reads that as `low_hi_pos = -1` — exactly the case 2c's spec added for 2d |
| Visibility complete, lineage incomplete | Stage A no-ops on resume, Stage B resumes from `:ingestion/correction-sweep-through` |

**Shutdown.** `_shutdown_requested` is checked between drains in Stage A and
between steps in Stage B. On a shutdown request: `completed_all = False`, Stage
C is skipped, status becomes `"stopped"`. The persisted frontier and the sweep
watermark are what the next run resumes from; no in-memory state is required to
survive.

Note that a run interrupted during Stage A leaves the frontier-high region
provisional and the sweep un-run, which is correct and self-describing —
`:ingestion/lineage-confirmed-through` has not advanced past the forward
stream's own position, so a consumer can tell.

## Progress reporting

`_ingest_progress` gains a `phase` key: `"converging"` during Stage A,
`"sweeping"` during Stage B. `processed` counts each **applied** position once
across both streams; Stage B's commits are re-visits of already-counted
positions and are not added, or the count would exceed `total`.

That is the whole of 2d's obligation here. The real multi-stream status view —
per-stream state, visibility coverage versus lineage-authority coverage as two
distinct completion signals, degenerate states represented explicitly — is
phase 4's, and this spec deliberately does not pre-empt its shape. 2d owes only
that the existing single-scalar view does not actively lie.

## Error handling

Unchanged in character from today's per-commit isolation:

- An extraction failure for one commit is logged to stderr and skips that one
  commit, in whichever stream owns it. The other stream is unaffected.
- A write failure inside `_forward_apply` or `_reverse_apply` is logged and
  skips that one commit; the position stays claimed (re-processing is
  idempotent, and un-claiming would require the allocator to support
  releasing a position, which it deliberately does not).
- `BrokenProcessPool` still propagates to the outer handler — it poisons every
  pending future in the window, both tags, and is not isolable.
- A failure inside Stage B aborts Stage B only. Stage A's work is already
  persisted, `completed_all` becomes `False`, and the next run resumes the
  sweep from its watermark.

## Testing

Real-backend only, per `docs/testing-conventions.md` — real `MiniGrafDb`, real
git fixture repos, no mocks.

**Ratio and interleave**
- `MINIGRAF_INGEST_STREAM_RATIO` parses `"1:1"`, `"1:3"`, `"3:1"`; malformed
  (`"x"`, `"0:1"`, `"1:0"`, `"-1:2"`, unset) falls back to `1:1` and logs once.
- Under each ratio, the claim sequence over a fixture repo matches the expected
  interleave exactly — this is the fairness property, and the single-loop
  design is what makes it deterministically assertable.
- Every position in the linearization is claimed exactly once, and the union of
  the two claimed intervals is the whole range with no overlap.

**Reconciliation (the core)**
- *Same-run variant*: a fixture repo with an entity introduced early and still
  present at `HEAD`, ingested in one run with a ratio that guarantees Stream 2
  reaches it first. Assert exactly one `:introduced-by`, that it names the true
  introduction commit, that the lineage marker is confirmed, that structural
  facts are live `:as-of` the true introduction, and that the guess commit
  carries a `:modified-in` edge.
- *Resumed-run variant*: same repo, but interrupt after Stage A has done some
  reverse work, then start a second run. Assert the same postconditions —
  this is the variant that fails silently without `provisional_idents`, so it
  needs its own test and cannot be folded into the one above.
- A test asserting `provisional_idents` is actually consulted: with it forced
  empty, the resumed-run case leaves a provisional marker behind.

**Staging and ordering**
- Stage B does not start until Stage A has fully drained: assert
  `:ingestion/correction-sweep-through` is unset while any pipeline entry is
  outstanding.
- Stage B's `_correction_sweep_apply` calls occur in ascending position order.
- Neither `_correction_sweep_claim_and_process` nor `_correction_sweep_walk`
  nor `_reverse_bulk_fill_walk` is called from `_run_ingestion`.

**Watermarks**
- `:ingestion/watermark` advances only on forward-applied commits and equals
  `frontier-low`'s `:hi-hash` at every checkpoint.
- `:ingestion/lineage-confirmed-through` equals `frontier-high`'s `:hi-hash`
  after a completed run, and does *not* after a run stopped during Stage A.
- The fold guard: force each of `_correction_sweep_select_position`'s
  non-ceiling `None` paths (absent `frontier-high`, stale boundary hash, gap
  still open) and assert the watermark is **not** folded to `HEAD` in any of
  them. Without the explicit positive check this passes vacuously on the happy
  path and lies on every one of these.

**Degenerate and lifecycle**
- Empty repo, single-commit repo, already-fully-ingested repo (Stage A no-ops,
  Stage B no-ops), and a resume where visibility is complete but the sweep is
  not.
- Shutdown mid-Stage-A: status `"stopped"`, Stage C skipped, and a follow-up
  run completes correctly from the persisted frontier.

**End-to-end**
- A full ingest of a real multi-commit fixture repo leaves **zero** provisional
  lineage markers and no entity with more than one `:introduced-by` value.
- The resulting graph is equivalent to what a pure forward-only ingest of the
  same repo produces, for `:introduced-by`. `:modified-in` is compared as a
  superset-with-explanation rather than equality only if a genuine divergence
  is found and traced to 2b's documented over-assertion; the expectation is
  equality, since Stage B is what repairs it.

## Out of scope

- **Stream 3 (tip-liveness)** — phase 3. `Ht` is read once at run start; a
  branch advancing mid-ingest is not chased.
- **The multi-stream status view** — phase 4.
- **Folding a sweep-confirmed frontier-high region into frontier-low** so an
  incremental re-ingest touches only genuinely new commits — deferred
  (confirmed with the user). Today's behaviour stands: `_frontier_load`
  discards and retracts an unrepresentable high interval and that region is
  re-walked. Correct and terminating, just slower than it needs to be.
- ~~**`"D"`/`"R"` file handling in the reverse and sweep paths** — 2b's existing
  scope cut, unchanged.~~ **AMENDED 2026-07-27 — this was wrong, and is now in
  scope.** Treating it as an unchanged scope cut held only while every stream
  was inert. Stage A makes them live, and the Task 8 review measured the
  result: at the 1:1 default, roughly the top half of history loses its
  deletions, renames and submodule changes — closed entities stay open,
  `:renamed-to` is never emitted, removed fields keep satisfying
  `[?e :static true]`, closed `:depends-on` edges stay live — with no pass to
  repair them. For a graph whose primary use is "what does this codebase look
  like now", shipping that as the default is not an acceptable scope cut.

  **The correction sweep now applies them** (`_forward_apply(...,
  lifecycle_only=True)`, called per swept commit after
  `_correction_sweep_apply`). The sweep is the right home: it already walks the
  entire reverse region ascending, re-parsing every commit — the direction
  lifecycle attribution requires, and exactly why D/R was intractable for the
  reverse walk — and at convergence `state` already holds the live-entity
  picture at the meeting point.

  These facts are applied **fresh, not re-applied**: the reverse stream never
  wrote D/R or gitlink facts for those commits, so there is nothing to
  deduplicate against. The A/M emission stays owned by `_correction_sweep_apply`
  precisely because re-running it *would* duplicate — minigraf creates a
  genuinely live duplicate when the same `(entity, attribute, value)` is
  re-transacted under a different valid-from (#156). In `lifecycle_only` mode
  `_build_code_triples` is still called for A/M files, but purely for its dict
  side effects, with its triples discarded: an entity introduced *inside* the
  reverse region is absent from Stage A's `entity_valid_from`, and without that
  bookkeeping a later deletion of it closes against the delete commit's own
  timestamp, yielding a wrong valid interval.
- **Pipelining Stage B's extraction** ahead of its apply — permitted by 2c but
  requires predicting positions rather than selecting them.
- **Force-push / rebase detection, DAG diamonds, octopus merges, multiple
  roots** — phase 5.
