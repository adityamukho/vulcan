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
  starting at its current `:lo-hash` and proceeding toward its own
  persisted `:hi-hash` — using a dedicated watermark for its own resumable
  progress (**not** `:ingestion/lineage-confirmed-through`, and **not**
  `allocator.claim_low()`; see "Why not `claim_low()`" and "Why a
  dedicated watermark" below), converting the provisional facts 2b wrote
  into authoritative ones as it reaches them and correcting 2b's own
  documented `:modified-in` over-assertion where its own parse disagrees.
  **This sweep only ever runs once Stream 1 and Stream 2 have both
  permanently finished for the run** (see "Why confirming requires the gap
  to already be closed") — it is a third, sequential, post-convergence
  pass, not a concurrent task alongside the other two.
- **2d** — the actual concurrency wiring inside `_run_ingestion`. Its task
  structure is not this spec's to design, but 2c's own precondition already
  constrains it: it cannot be simply "two asyncio tasks sharing the
  allocator, with fairness so neither starves" plus this sweep bolted on as
  a third — this sweep cannot start until both of the other two are
  finished, so 2d needs a third, sequential phase after the first two
  converge, not a third concurrent one.

Like 2a and 2b, this sub-phase adds functions with **no caller wired into
`_run_ingestion` yet** — 2d ties everything together into the real commit
loop. This branch stacks on top of 2b's (`design-222-phase2b-reverse-bulk-fill-walk`,
PR #228, not yet merged) since 2c calls 2b's `_entity_introduced_by_query`
and depends on 2b's exact candidate-diff/provisional-marker write pattern.

**Consequence for phase 2's own value proposition:** recent history becomes
*visible* as soon as Stream 2 writes it, but every entity Stream 2 touched
remains provisional — and therefore untrusted by 2a's composed trust
predicate — until this sweep has walked the *entire* converged range after
both streams finish. On a large repository that is close to the full
ingest duration, not a small tail. This isn't a defect in this sweep's own
logic; it's a real scheduling consequence 2d has to plan around, stated
here so it isn't discovered only once 2d is being designed.

**Cost:** this sweep calls `_extract_commit` (a `git diff-tree`, a `git
show` per changed file, and a tree-sitter parse of both sides of each) for
*every* commit in frontier-high's converged range — duplicating work 2b
already did over the same commits. For a large converged range that is
close to a second full extraction pass over the repository, stacked on top
of the terminal-pass consequence above. This is a deliberate simplicity
choice, not an oversight: the sweep's classification only strictly needs
`unchanged_idents` from the parse (which entities `C` touches, and which
idents to act on, could instead be enumerated from the graph itself — any
`ident` satisfying `[ident :introduced-by C]` or `[ident :modified-in C]`
covers everything case 1 and case 3 act on) but `unchanged_idents` is what
makes the reconcile step in case 3 possible at all, so the parse cannot be
eliminated outright. A future optimization could enumerate from the graph
and call `_extract_commit` only when at least one enumerated ident has a
live `:modified-in` the reconcile step might need to revisit — not adopted
here, in favor of reusing the same discovery path 2b already established.

## Scope (2c only)

In scope:

- A per-commit correction step that advances a dedicated position tracker
  upward through frontier-high's own claimed interval — starting at its
  current `:lo-hash` on first use, proceeding toward its own persisted
  `:hi-hash` (not necessarily `HEAD`/`len(linearization) - 1` — see the
  incremental-re-ingest note under "Position tracker") — and, for every
  entity touched by that commit's "A"/"M" files, reconciles its lineage per
  the three-case algorithm below.
- A thin driving loop that repeats the above until the tracker reaches
  frontier-high's own persisted `:hi-hash`. Frontier-low is never touched
  by this sweep, and neither is `:ingestion/lineage-confirmed-through` (see "Why a
  dedicated watermark" below) — 2c introduces its own new watermark,
  `:ingestion/correction-sweep-through`.
- Opportunistically clearing stale intermediate candidate-diff records left
  behind along a supersession chain (2a's spec flagged this explicitly as
  "2c's job"). Candidate-diff records are a pure cleanup obligation for
  this sweep — nothing in its own classification logic reads one; contrast
  with an earlier draft, which read persisted hashes to skip re-parsing (a
  goal that never actually applied once this sweep needed a full
  `_extract_commit` call for entity discovery regardless).
- A guard against re-asserting `:modified-in` at an entity's own
  introduction commit on a resumed/re-run sweep (see "Per-commit algorithm").
- Correcting 2b's documented `:modified-in` over-assertion: when 2b
  supersedes a provisional guess, it retroactively asserts `:modified-in`
  at the superseded commit *unconditionally*, without re-checking #221's
  unchanged-body narrowing against that commit's own diff (2b's own spec,
  "Known, documented limitation"). This sweep has the real parse in hand
  for every commit it visits and reconciles the fact to match — see case 3
  below.

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

### Why confirming requires the gap to already be closed

A third earlier draft of this design argued that Stream 2's persisted guess
for any entity `C` touches is always `<= C`'s position, so by induction a
provisional guess can never point somewhere other than the commit currently
being visited. That argument silently assumed Stream 2's guess is *final* —
true only once Stream 2 can never claim another position. During a real
concurrent run it is not yet final, and the gap between "not yet final" and
"final" is exactly where this sweep can corrupt data if it runs too early:

1. Sweep visits `C = frontier-high.lo`, confirms entity `E` (case 1):
   `:introduced-by = C`, provisional marker dropped.
2. Stream 2 later claims some `C' < C` (a position still below `C` that it
   had not yet reached) and finds `E` there too. `_reverse_fill_claim_and_process`'s
   *sole* gate for whether to move a guess is `_lineage_is_provisional`
   (mcp_server.py:7369-7373) — since this sweep already confirmed `E`, it no
   longer reads as provisional, so 2b takes its "already authoritative"
   branch: writes `[E :modified-in C']` and leaves `:introduced-by` at `C`.
3. `E` now claims it was introduced at `C` while carrying a `:modified-in`
   edge at the *earlier* commit `C'` — a contradiction ordinary forward walk
   can never produce — and it reads as fully authoritative, so 2a's trust
   predicate will trust it as soon as `lineage-confirmed-through` reaches
   it. Confirming turned a still-correctable provisional guess into
   uncorrectable (and wrong) ground truth: the deferred reconciliation this
   spec relies on (see "Explicitly deferred" above) is described in terms
   of a *pre-existing provisional guess* to correct, and there is no longer
   one to find.

This is not an exotic interleaving — it is the expected one once 2d runs
Stream 1 and Stream 2 concurrently, one descending into the gap while this
sweep ascends through already-claimed territory. **The precondition that
makes a confirm sound is that no unclaimed position remains below the
position being confirmed — i.e. the gap is closed:
`frontier-low.:hi-hash`'s position `+ 1 == frontier-high.:lo-hash`'s
position** (equivalently, `allocator.is_gap_empty()`, if an allocator were
available — it is not, so this sweep checks the equivalent condition
against the two persisted interval bounds directly). Once the gap is
closed, `claim_high()` can never return another position for the rest of
this run (`is_gap_empty()` stays `True` — the gap only shrinks), so Stream
2 is permanently done: its guess for every entity within frontier-high's
*entire* range is now genuinely final, and the induction argument above
holds validly. `_correction_sweep_claim_and_process` therefore checks this
precondition on every call (both bounds are cheap to re-read) and returns
`None` — nothing safe to correct yet — until it holds.

Given the precondition holds, the induction argument is exactly as before:
for a commit `C` Stream 2 claimed, Stream 2's final persisted guess for any
entity `C`'s files touch is necessarily `<= C`'s position (it always moves
a candidate's guess *down* to the earliest occurrence seen, never up), so
walking `[frontier-high.lo, N-1]` oldest-to-newest, the sweep reaches each
entity's true within-range-earliest occurrence before any later commit that
also touches it. **Even so, the per-commit algorithm below keeps an
explicit no-op branch for a provisional guess pointing elsewhere, rather
than treating that state as unreachable** — the precondition is what a
*correct caller* guarantees, not something the per-ident classification can
independently verify, and a naive two-branch implementation that assumes
the state can't happen does something actively harmful if it ever does
(confirms an entity at a commit that was never validated as its true
introduction). See "Explicitly deferred" above for where the genuine
reconciliation (an entity whose true earliest occurrence lies below
frontier-high's range entirely, in frontier-low's own territory) belongs —
that is a different scenario from the one above and remains this sweep's
own no-op case, not something it can resolve itself.

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
directly.

**Caveat inherited from 2b, not introduced here:** "already structurally
complete" assumes 2b's own structural writes are complete and correctly
timestamped, which a separate review of 2b's own spec found is not
uniformly true today (2b's `:contains` edges collapse into one batched
`_transact` rather than the several forward walk deliberately issues, and
its structural facts keep their first-sighting `valid_from` — later than
whatever `:introduced-by` a subsequent guess-move settles on, so a query
`:as-of` the true introduction can return lineage with no structure yet).
Those are 2b's fixes to make (tracked as "2b1"), not this sweep's — but
this sweep's case 1 promotes an entity to authoritative based on
`:introduced-by` alone, so until 2b1 lands, confirming here can promote an
entity whose structural facts are still incomplete or mistimed. Similarly,
2b's retroactive `:modified-in` writes can carry a back-dated `valid_from`
looked up via `ts_by_commit_ident.get(..., commit_ts_iso)`
(mcp_server.py:7396-7407) that this sweep's case 3 reconciliation — a
current-time read, not a temporal one — cannot detect or correct.

```python
file_results, _gitlink_changes, _gitmodules_map, _renamed_pairs = _extract_commit(
    repo_path, commit_hash, ignore_patterns
)
for status, file_path, extracted, precomputed, _old_path in file_results:
    if status not in ("A", "M"):
        continue  # "D"/"R" deferred -- see Scope, matches 2b's own cut
    candidate_idents = (
        [precomputed["module_ident"]]
        + [ident for ident, _name, _t in precomputed["function_entries"]]
        + [ident for ident, _name, _t in precomputed["class_entries"]]
        + [ident for ident, _name, _t in precomputed["global_entries"]]
        + [ident for ident, _name, _t in precomputed["field_entries"]]
    )
    unchanged_idents = precomputed.get("unchanged_idents", set())
```

**Classification reads *all* live `:introduced-by` values for `ident`, not
just one.** `_entity_introduced_by_query` returns `results[0][0]` — an
arbitrary row when more than one exists (mcp_server.py:5228-5233). Two live
`:introduced-by` facts for one entity is a real, reachable state once 2d
exists: `_run_ingestion` seeds `entity_valid_from` once at startup, 2b's
writes never enter that in-memory dict, so if ordinary forward walk later
reaches an entity 2b already introduced, `_build_code_triples` treats it as
new and asserts a *second* `:introduced-by` at a different commit and a
different `valid_from` — both survive. Relying on `results[0][0]` would
make case 1's confirm and case 3's self-introduction guard silently
order-dependent on which row minigraf happens to return first, which is
not a documented guarantee anywhere. So this sweep queries the full set:

```python
raw = _db_execute(db, f"(query [:find ?c :where [{ident} :introduced-by ?c]])")
introduced_by_values = {row[0] for row in json.loads(raw).get("results", [])}
```

and every guard below tests `introduced_by_values == {commit_ident}` —
*exactly one distinct* value, equal to `commit_ident` — never membership
(`commit_ident in introduced_by_values`), never the first-row shortcut, and
never exact-list equality. The set (not list) comparison matters on its
own: two live rows with the *same* value (a duplicate that survives
despite the valid_from-equality idempotency property — e.g. two writes at
genuinely different `valid_from` that happen to carry an identical value)
would fail a list-length check but is exactly the single-value case for
this sweep's purposes; comparing distinct values treats it correctly
instead of stranding a correctly-guessed entity as provisional forever.
Zero distinct values or two both fail the equality and fall through to
whichever branch's `else` is the safe one, below.

Each candidate ident then falls into one of three cases. Every write below
uses `C`'s own `commit_ts_iso` (the same timestamp 2b itself used when it
first wrote at `C`) — this equality is load-bearing, not incidental: it is
what makes any write this sweep re-issues at a position 2b already touched
land as an idempotent no-op under minigraf's real write semantics (an
identical `(entity, attribute, value, valid_from)` tuple is a no-op; only a
*differing* `valid_from` produces a second live datom) rather than a
duplicate.

This sweep classifies every candidate `ident` by two independent facts:
whether it's provisional, and how many *distinct* `:introduced-by` values it
has and what they are. The three cases below are defined so that exactly
one always applies — no ident falls through all three, and none can match
two at once:

1. **Provisional, guess matches this commit** (`_lineage_is_provisional(db,
   ident)` and `introduced_by_values == {commit_ident}`) — per "Why
   confirming requires the gap to already be closed" above, this is the
   *only* state a provisional entity can be in in this sweep's range once
   the sweep reaches it: Stream 2's guess is guaranteed to already equal
   its true within-range-earliest occurrence by the time this sweep,
   walking the same order, gets there. Call `_lineage_confirm(db, ident,
   index_con=index_con)` (drops the marker; the `:introduced-by` fact
   itself is untouched, since its value was already correct) and
   `_candidate_diff_clear(db, commit_hash, ident, index_con=index_con)`.
   No `:modified-in`.

2. **Provisional, anything else** (`_lineage_is_provisional(db, ident)` and
   `introduced_by_values != {commit_ident}` — zero distinct values,
   `commit_ident` plus at least one other, or a single different value) —
   the "guess points elsewhere" state per "Why confirming requires the gap
   to already be closed" above, and now also the duplicate-fact state
   above: a correct caller (gap-closed precondition, and no stray second
   `:introduced-by` from an uncoordinated forward walk) means this
   shouldn't arise for a commit inside this sweep's range, but neither
   condition is something this per-ident classification can verify itself.
   **Leave the entity completely untouched** — no `_lineage_confirm`, no
   `:introduced-by` change, no candidate-diff touch — so this sweep fails
   safe (does nothing) rather than confirming an unvalidated or ambiguous
   guess. The entity remains provisional and available for whatever the
   deferred reconciliation (or, for the duplicate-fact case, 2d's own fix)
   turns out to be.

3. **Already authoritative** (`not _lineage_is_provisional(db, ident)`;
   confirmed at an earlier position in this same sweep, or — after this
   sweep has fully caught up once — simply revisited on a later run),
   split on `introduced_by_values` into two **non-overlapping**
   sub-branches (an earlier draft stated these as "self-intro" vs. "anything
   else, skip", which left no room for the reconcile step to ever run —
   they must instead partition on the *count* of distinct values, with the
   value-comparison only deciding *which* of the single-value outcomes
   applies):

   - **Exactly one distinct value**: if it equals `commit_ident`, `C` *is*
     this entity's own introduction commit — skip entirely (no
     `:modified-in` write or retraction, no candidate-diff touch). This
     matters for resume-safety: if the process dies after case 1's
     `_lineage_confirm` but before `_correction_sweep_through_update`
     persists, a re-run revisits `C` and would otherwise find the
     now-authoritative entity at its own introduction commit and wrongly
     touch a self-`:modified-in` — a fact class ordinary forward walk
     never produces (`_build_code_triples` only emits `:modified-in` on
     its already-known branch, never at introduction). If the one distinct
     value is anything *other* than `commit_ident`, `C` is an ordinary
     later touch of an unambiguously-introduced entity — **reconcile
     rather than merely assert**, below.
   - **Zero or two-or-more distinct values**: the same duplicate-fact risk
     as case 2 — skip, left alone rather than guessed at. This sweep only
     actively reconciles `:modified-in` when it has an unambiguous single
     introduction commit to compare `C` against.

   The reconcile step is where this sweep corrects 2b's documented
   over-assertion (see Scope), not just re-confirms what's already there —
   which requires actually checking whether `commit_ident` is among
   `ident`'s live `:modified-in` values, not a query with an unbound `:find`
   variable (a bare `[:find ?c :where [ident :modified-in commit_ident]]`
   never binds `?c` to anything and always returns empty, regardless of
   whether the fact exists):

   ```python
   raw = _db_execute(db, f"(query [:find ?c :where [{ident} :modified-in ?c]])")
   modified_in_values = {row[0] for row in json.loads(raw).get("results", [])}
   already_has_modified_in = commit_ident in modified_in_values
   if ident in unchanged_idents:
       if already_has_modified_in:
           _retract(db, f"[[{ident} :modified-in {commit_ident}]]", index_con=index_con)
   else:
       if not already_has_modified_in:
           _transact(db, f"[[{ident} :modified-in {commit_ident}]]", commit_ts_iso, index_con=index_con)
   ```

   For the common case — `C` is an ordinary later touch 2b itself already
   classified as "already authoritative touched" (mcp_server.py:7379-7387,
   the *same* `unchanged_idents` gate, computed from the identical
   `_extract_commit` call) — this is provably a no-op: both streams agree,
   so the fact's presence already matches what this sweep would decide,
   and the retract/assert branch never fires. Where it actually matters is
   2b's *retroactive* supersession write (mcp_server.py:7396-7407), which
   asserts `[ident :modified-in superseded_ident]` unconditionally, with no
   `#221` check at all — if this sweep's own `unchanged_idents` (computed
   from its own, potentially more complete, pass over `superseded_ident`'s
   diff) disagrees, it retracts the over-asserted edge here. This is the
   only place in the whole design where 2b's known limitation actually gets
   fixed; everywhere else this sweep's `:modified-in` handling is
   redundant by construction. Either way, opportunistically call
   `_candidate_diff_clear(db, commit_hash, ident, index_con=index_con)` if
   a record happens to exist for this exact
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
`:description` *attribute* — but its own distinct string, "correction sweep
progress watermark," not a copy of lineage-confirmed-through's; see
"Schema/audit safety" below) but tracking a different thing — this sweep's own
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

`_correction_sweep_claim_and_process` takes an additional optional
`hash_to_pos: Optional[Dict[str, int]] = None` parameter — if omitted, it
builds one from `linearization` itself (so the function stays independently
callable, e.g. from tests, exactly as before); `_correction_sweep_walk`
builds it **once** and passes it to every call instead. `linearization` is
fixed for the duration of a run, so rebuilding an `N`-entry map on every one
of a full sweep's `N` calls (as an earlier draft's snippet did, with no
caller-side reuse) is pure `O(N²)` waste — `_frontier_load` itself only
ever builds this map once per run (mcp_server.py:4967), and this sweep
should match that.

Position selection: first the gap-closed precondition ("Why confirming
requires the gap to already be closed" above), then the ceiling, then the
resume point — each handling its own absent/stale case:

```python
low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
if low_bounds is None or high_bounds is None:
    return None  # migration hasn't run yet, or Stream 2 hasn't claimed anything
if hash_to_pos is None:
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
if low_bounds[1] not in hash_to_pos or high_bounds[0] not in hash_to_pos:
    return None  # a boundary hash is stale (rewritten history); nothing safe to do
if hash_to_pos[low_bounds[1]] + 1 != hash_to_pos[high_bounds[0]]:
    return None  # gap still open -- Stream 2 may still descend past a position
                 # this sweep would otherwise confirm; see the precondition above
if high_bounds[1] not in hash_to_pos:
    return None  # frontier-high's :hi-hash is stale -- see the incremental-
                 # re-ingest note below; nothing safe to do until 2b/2d address it
ceiling_pos = hash_to_pos[high_bounds[1]]

through_hash = _correction_sweep_through_query(db)
if through_hash is not None and through_hash in hash_to_pos:
    pos = hash_to_pos[through_hash] + 1
else:
    # Unset (first-ever call), or a stale hash from rewritten/rebased
    # history -- (re)start from frontier-high's current lo-hash, mirroring
    # _frontier_load's own precedent of dropping a bound that no longer
    # resolves (mcp_server.py:4970-4978) rather than erroring. See the
    # cross-run caveat below for why this rule is only complete within a
    # single run.
    pos = hash_to_pos[high_bounds[0]]  # already validated above
if pos > ceiling_pos:
    return None  # reached frontier-high's own :hi-hash; nothing left to correct
if len(commit_metadata) != len(linearization) or commit_metadata[pos][0] != linearization[pos]:
    return None  # commit_metadata violates its stated contract -- nothing safe to
                 # do, rather than an IndexError/wrong-commit read; see the
                 # contract note below
commit_hash, commit_ts_iso, _author, _subject = commit_metadata[pos]
```

The contract check above is a real `if`/`return None`, not a bare `assert`:
the documented violation this guards against (2d accidentally passing
`_run_ingestion`'s own watermark-relative `commit_metadata`, shorter than
`linearization`) would make `commit_metadata[pos]` raise `IndexError`
*before* an `assert` placed after the indexing could ever run, or, for a
`pos` that happens to still be in range, silently read the wrong commit's
metadata — and `assert` disappears entirely under `python -O`, which would
let that silent misread through undetected and quietly break the
valid_from-equality property every idempotency argument in this design
depends on. Checking the lengths and the hash *before* indexing, and
returning `None` like every other unmet precondition in this function,
keeps the same "nothing safe to do" discipline instead of crashing.

**Cross-run caveat:** `pos = hash_to_pos[through_hash] + 1` assumes no
position *below* the watermark ever becomes newly claimed after the sweep
has passed it. That holds within a single run — the gap-closed precondition
guarantees Stream 2 is permanently finished before this sweep starts, so
frontier-high's `:lo-hash` is frozen for the rest of the run. It does not
hold *across* runs once the incremental-re-ingest growth hazard noted below
is fixed: a later run could then legitimately claim positions below a
previous run's floor, and this resume rule would skip past them without
ever confirming their provisional facts or clearing their candidate-diff
records. Today this is masked — the inverted-interval state produced by
that same hazard makes `pos > ceiling_pos` and the sweep no-ops instead —
so this is latent incompleteness, not live corruption. Whoever fixes the
growth handling should also make this sweep restart from `:lo-hash`
whenever its position is below `correction-sweep-through`'s.

**`ignore_patterns` must match what 2b used for the same region.** Every
"2b already wrote a fact for every ident this sweep will find" invariant in
"Per-commit algorithm" assumes both streams' `_extract_commit(repo_path,
commit_hash, ignore_patterns)` calls see the same file set.
`ignore_patterns` comes from `_load_ignore_patterns(repo_path)`, read from
the working tree once per run (mcp_server.py:7487) — not persisted, and not
tied to the commit being processed. Within one run this is automatically
consistent (both streams' calls in that run use the same value); *across*
runs it is not, if the ignore file changes between the run where 2b claimed
a region and a later run where this sweep walks it. Newly-ignored files'
entities are then silently never visited by this sweep (their provisional
facts and candidate-diff records live forever, uncleared, though nothing is
corrupted); newly-unignored files yield idents with zero `:introduced-by`
values, landing harmlessly in case 2's or case 3's skip. Neither corrupts
data, but both are exactly the kind of silent incompleteness this sweep
otherwise exists to eliminate — the caller (2d) must pass the same
`ignore_patterns` the reverse stream used for the region being swept.

`ceiling_pos` reads frontier-high's *persisted* `:hi-hash`
(`high_bounds[1]`) rather than `len(linearization) - 1`. Those are **not**
interchangeable across separate runs: within a single run, `claim_high()`'s
`_extend` never touches `existing.hi_pos`, so the in-memory interval's high
end genuinely is fixed at the linearization's last position. But on an
incremental re-ingest, `linearization` is rebuilt fresh and longer while
`:hi-hash` remains whatever was persisted last time — `_frontier_load`
reconstructs the high interval as `[pos(lo_hash), pos(hi_hash)]` against
the *new* linearization (mcp_server.py:4974-4978), so
`pos(hi_hash) = N_old - 1`, strictly less than `N_new - 1`. Using
`len(linearization) - 1` as the ceiling would walk the sweep straight into
`[N_old, N_new - 1]` — brand new commits no stream has claimed yet — and
every invariant this design relies on would fail there exactly as in the
gap case above. Stopping at `high_bounds[1]`'s own position avoids this
regardless of how much the linearization has grown.

**Known dependency, not itself a 2c defect:** `_frontier_persist_claim`
only updates the moved bound (`:lo-hash` for `claim_high`), never
`:hi-hash` (mcp_server.py:5012-5014). On an incremental re-ingest, Stream
2's first `claim_high()` of the new run returns the new `gap_hi`
(`N_new - 1`, since the reloaded interval no longer reaches the true last
position) and persists it as the new `:lo-hash`, leaving the persisted
interval as `[N_new - 1, N_old - 1]` — inverted. This sweep's own ceiling
check above would then read `high_bounds[1] = N_old - 1` as the ceiling
while `through_hash`/`high_bounds[0]` sit at `N_new - 1`, well above it —
`pos > ceiling_pos` immediately, so the sweep silently does nothing rather
than corrupting anything, but it also makes no progress until 2b or 2d
fixes the underlying interval-growth handling. Flagged here since 2c is the
first consumer to read frontier-high's persisted `:lo-hash` as a position
to walk *from*, which is what exposes it.

`commit_metadata` is assumed **full-history and positionally aligned with
`linearization`** — the same contract 2b's own tests construct
(`tests/test_mcp_server.py:13926`, via `_git_commits(repo, None, branch)`)
and `_reverse_fill_claim_and_process` itself relies on
(`commit_metadata[pos]`, mcp_server.py:7314). This is *not* what
`_run_ingestion` currently builds for its own use (a watermark-relative
list via `_git_commits(repo_path, watermark, branch)`,
mcp_server.py:7486) — 2d, when it wires this sweep in, must pass the
full-history list, not `_run_ingestion`'s own watermark-relative one.
Indexing positionally (`commit_metadata[pos]`) rather than searching by
hash both matches 2b's convention and avoids a bare `next(...)` with no
default raising `StopIteration` if the contract is ever violated. Because
both streams derive `commit_ts_iso` from `_git_commits`'s single
`strftime("%Y-%m-%dT%H:%M:%SZ")` formatting (mcp_server.py:4130), the two
streams cannot disagree on the timestamp string for the same commit — this
is what makes the valid_from-equality argument in "Per-commit algorithm"
above hold.

Once `pos` is known to be `<= ceiling_pos` at the time this call started,
the walk needs no further bounds-checking against frontier-high for the
rest of *this* call — `[frontier-high.lo, ceiling_pos]` is fixed for the
duration of one call, regardless of how far Stream 2 additionally
progresses concurrently (its `:lo-hash` can only move further down, never
past `ceiling_pos`, and `:hi-hash` never moves within a run).

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
    hash_to_pos: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[str, int]]:
    """Advance the correction sweep by exactly one commit, upward through
    frontier-high's own claimed territory, and reconcile that commit's
    entities per the three-case algorithm above. Returns (commit_hash,
    skipped_count) -- skipped_count is how many candidate idents at this
    commit landed in case 2 or case 3's ambiguous-value skip, i.e. stayed
    provisional or unreconciled despite the sweep visiting their commit --
    or None if there is nothing safe to correct yet: the gap is still open
    (Stream 2 could still descend past a position this call would confirm
    -- see "Why confirming requires the gap to already be closed"),
    frontier-high hasn't claimed anything yet, a required boundary hash is
    stale, commit_metadata doesn't match linearization, or the sweep has
    already reached frontier-high's own :hi-hash. Every skip also logs the
    ident to stderr (`[_correction_sweep] left {ident} provisional/
    unreconciled at {commit_hash}: ...`), matching `_run_ingestion`'s own
    stderr-on-skip idiom for unreadable commits -- a run in which every
    entity failed safe must not look identical to a fully successful one.
    No `allocator` parameter -- this reads frontier-low and frontier-high's
    persisted bounds directly via _frontier_read_bounds and tracks its own
    progress via _correction_sweep_through_query/_update, not the
    allocator's claim_low(); see "Why not claim_low()" above. Never calls
    _frontier_persist_claim or _lineage_confirmed_through_update -- neither
    frontier-low nor lineage-confirmed-through is touched by this sweep.
    `hash_to_pos`, if omitted, is built fresh from `linearization` --
    callers doing a full sweep should build it once and pass it in instead
    (see _correction_sweep_walk) to avoid rebuilding an N-entry map on every
    one of a full sweep's N calls."""

def _correction_sweep_walk(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> Tuple[int, int]:
    """Build hash_to_pos once and repeatedly call
    _correction_sweep_claim_and_process (passing it down) until that
    returns None. Returns (commits_processed, entities_left_unreconciled) --
    both 0 both when the gap-closed precondition isn't met yet (the common
    case early in a run; callers should not read (0, 0) as "nothing to do,
    ever") and when the sweep has already fully caught up to frontier-
    high's :hi-hash. A nonzero entities_left_unreconciled with a nonzero
    commits_processed is the signal 2d should surface prominently (e.g.
    through handle_minigraf_ingest_status, mirroring how
    handle_minigraf_audit returns violation lists rather than only a
    boolean) -- this sweep's entire product is "these lineage facts are now
    trustworthy", and silently leaving some provisional is a materially
    different outcome from a clean run even though both return successfully.
    No caller in this sub-phase -- 2d wires this into the real concurrent
    ingestion loop, run only after both the reverse stream and ordinary
    claim_low()-driven introduction have permanently finished for the run
    (see "Why confirming requires the gap to already be closed")."""
```

### Execution context

Neither function above may run on the event-loop thread. `_run_ingestion`'s
whole architecture exists to keep exactly this off it: `_extract_commit`
(a `git diff-tree`, a `git show` per changed file, and a tree-sitter parse
of both sides of each) runs on the `ProcessPoolExecutor` because
tree-sitter's GIL-holding C parse was measured to starve the event loop
(#116), and every `db.execute()`/`checkpoint()` goes through the
single-worker `write_executor` so an fsync never blocks concurrent
`call_tool()` requests (mcp_server.py:7440-7466). This sweep's per-commit
step does both a full `_extract_commit` call and its own
`_db_execute`/`_transact`/`_retract`/`_db_checkpoint` calls synchronously
in one function body, exactly the shape that architecture exists to keep
off the event loop — and, per the terminal-pass consequence above, this
sweep is the single longest-running phase in the whole ingest, so running
it inline would be the worst place in the design to get this wrong: no MCP
request would be served for the duration of a full pass.

This spec does not wire the actual `await`/executor calls — that remains
2d's job, same as the rest of the concurrency wiring — but it does fix the
function *shape* 2d has to schedule, since that's what determines whether
2d even *can* schedule it sanely: `_correction_sweep_claim_and_process`
processes exactly one commit and returns, which is the right granularity
for cooperative scheduling (2d can `await` between calls, interleaving
other event-loop work). What 2d must not do is call
`_correction_sweep_walk` directly from a coroutine with no `await` inside
its loop (blocks for the entire pass) or route it through
`run_in_executor(write_executor, _correction_sweep_walk, ...)` as a single
call (serializes every one of this sweep's parses behind the same thread
`_run_ingestion` reserves for writes, which is exactly the contention
`write_executor`'s single-worker design exists to avoid). The natural
shape, mirroring `_run_ingestion`'s own loop, is 2d awaiting
`run_in_executor` calls to *each* piece (extraction on the process pool,
DB operations on `write_executor`) once per commit, with
`_correction_sweep_claim_and_process` itself staying a plain synchronous
function ignorant of asyncio, the same role `_extract_commit` and the
per-commit DB helpers already play for forward walk.

### Schema/audit safety, idempotency

`:ingestion/correction-sweep-through` is a new *entity* of the
already-registered/audited `:type/ingestion` type — the same type
`:ingestion/lineage-confirmed-through` uses — so no new schema surface.
Unlike `lineage-confirmed-through`, it gets its own distinct `:description`
string ("correction sweep progress watermark," not a copy of lineage-
confirmed-through's) — two `:type/ingestion` entities with byte-identical
descriptions would both pass audit but be indistinguishable from each
other in the fact index and in `minigraf_audit` output. Case 1's writes are
the same query-before-write primitives 2a/2b already established as safe
(`_lineage_confirm`, `_candidate_diff_clear`). Case 2 writes nothing. Case
3's `:modified-in` reconciliation is itself query-before-write (it reads
`already_has_modified_in` before deciding to retract or assert), so its
retract is never a blind retraction of a possibly-absent fact. The assert
branch, when it does fire, is still an unconditional `_transact` — it is
safe not because of the query-before-write guard (that guard decides
*whether* to write, not what makes a repeat write safe) but because it
always uses `C`'s own `commit_ts_iso`, matching what forward walk (or 2b,
if `C` happens to already carry other facts from it) would have used at
the same position, so a re-issued write lands as an idempotent no-op
rather than a duplicate (see "Per-commit algorithm" above). This is worth
stating plainly rather than leaving implicit, since it is the one write in
this design that depends on timestamp discipline rather than a guard alone
for its safety.

## Testing

Following `docs/testing-conventions.md` (real `MiniGrafDb`, no mocks), using
the same `tmp_path / "repo"` real-git-repo fixture pattern already used
throughout `tests/test_mcp_server.py`'s existing ingestion tests. Since this
sweep now requires the gap to be closed before it does anything (see "Why
confirming requires the gap to already be closed"), most scenarios below
close it using real functions rather than hand-constructing frontier facts:
a real `frontier_registry.FrontierAllocator`, `claim_low()` +
`_frontier_persist_claim(..., from_low=True, ...)` in a loop for the low
side (standing in for 2d's future ordinary forward walk, which this
sub-phase doesn't build), and `_reverse_fill_claim_and_process` for the
high side, until `allocator.is_gap_empty()`.

- **Sweep is a no-op while the gap remains open** (the precondition itself):
  claim some positions via `_reverse_fill_claim_and_process` but leave a
  non-empty gap below them (don't also claim from the low side); call
  `_correction_sweep_claim_and_process`; assert it returns `None` and
  writes nothing, even though frontier-high has real provisional facts
  available to confirm — this is the direct regression test for the race
  in "Why confirming requires the gap to already be closed": a version of
  this sweep without the precondition check would confirm here and be
  wrong the moment Stream 2 claims a still-lower position touching the
  same entity.
- **Confirms a correct provisional guess** (case 1): close the gap (as
  above) so an entity ends up with a provisional `:introduced-by` pointing
  at its true earliest commit within Stream 2's claimed range; then run
  the correction sweep; assert `_lineage_is_provisional` is now `False`,
  the `:introduced-by` value is unchanged, and the candidate-diff record
  for that `(commit, entity)` is gone (`_candidate_diff_read` returns
  `None`).
- **Does not duplicate the claimed commit's metadata**: same setup as
  above; before running the sweep, capture the claimed commit entity's
  total live fact count and its `:hash`/`:author`/`:subject`/`:date`
  values as written by `_reverse_fill_claim_and_process`; run the sweep;
  assert the fact count is unchanged and none of those attributes was
  re-asserted at a *different* `valid_from` — this is the regression test
  that can actually distinguish a correct implementation (skips the write,
  or re-issues it idempotently at the same `commit_ts_iso`) from one that
  writes it at a different timestamp and produces a second live datom.
- **Leaves an entity untouched when its guess points elsewhere** (case 2,
  the explicit no-op): with the gap closed, hand-construct a provisional
  `:introduced-by` pointing at a commit *other* than the one the sweep is
  about to visit for that same entity (simulating a precondition
  violation, or the deferred rename/rebirth scenario, directly rather than
  via a real interleaving); run the sweep over that commit; assert the
  entity is still provisional, `:introduced-by` is unchanged, and no
  candidate-diff record was touched — the test that pins "fails safe"
  rather than "confirms an unvalidated guess."
- **Fails safe on a duplicate `:introduced-by`, regardless of row order**
  (case 2, the multi-value guard): with the gap closed, hand-construct
  *two* live `:introduced-by` facts for the same entity — one equal to
  `commit_ident` for the commit the sweep is about to visit, one at a
  different commit — simulating the uncoordinated-forward-walk state this
  design defers to 2d; run the sweep over that commit; assert it is a
  no-op regardless of which fact minigraf's query happens to return first
  (assert this for both physical insertion orders, since row order is not
  a documented guarantee) — the test that would fail against a naive
  `results[0][0]`-based guard, which could confirm on an unvalidated
  duplicate depending on row order.
- **Skips `:modified-in` at an entity's own introduction commit on a
  resumed sweep** (case 3's self-introduction guard): with the gap closed,
  run the correction sweep once so an entity is confirmed authoritative at
  its true introduction commit; call `_correction_sweep_claim_and_process`
  again as if resuming after a crash (i.e. without having advanced
  `_correction_sweep_through_update` past that commit — construct this
  directly rather than via the real crash path, mirroring how other resume
  tests in this codebase re-invoke a step function against pre-set state);
  assert no `:modified-in` fact was created for that entity at its own
  introduction commit — this is the test that would fail against a naive
  implementation of case 3 with no self-introduction guard.
- **Ordinary modification of an already-authoritative entity is a no-op
  when 2b already agrees** (case 3, the common path): with the gap closed,
  an entity already authoritative at some earlier commit (pre-seeded via a
  real earlier sweep pass, non-provisional) genuinely modified at a later
  commit `C` that 2b itself already classified as "already authoritative
  touched" (i.e. `_reverse_fill_claim_and_process` already wrote
  `[entity :modified-in C]` there); sweep over `C`; assert
  `:modified-in` is present exactly once (raw count, not just presence) —
  proving the reconciliation is idempotent for the common case, not merely
  "asserts and happens not to duplicate."
- **Retracts 2b's over-asserted retroactive `:modified-in`** (case 3, the
  actual fix): with the gap closed, construct the supersession scenario
  2b's own known limitation describes — an entity whose provisional guess
  gets superseded, so 2b retroactively (and unconditionally) writes
  `[entity :modified-in superseded_commit]` — but arrange for the entity's
  body to be provably unchanged at `superseded_commit` per this sweep's own
  `unchanged_idents` (a case 2b's own retroactive write doesn't check);
  once the sweep, walking forward, reaches `superseded_commit` in case 3's
  general path, assert the over-asserted `:modified-in` fact has been
  retracted — this is the regression test for the "case 3's write can only
  ever be redundant" finding: without the retract branch, this fact stays
  live forever.
- **Opportunistic stale candidate-diff cleanup** (case 3): pre-seed a stale
  candidate-diff record at a commit for an entity that's already
  authoritative by the time the sweep reaches that commit (simulating an
  orphaned intermediate guess along a supersession chain); assert the sweep
  clears it even though that commit falls into the ordinary case-3 path.
- **No-op when frontier-high hasn't claimed anything yet**: a graph with
  only frontier-low/migration state, no `_reverse_fill_claim_and_process`
  ever run; assert `_correction_sweep_claim_and_process` returns `None`
  and writes nothing.
- **Resumes from `correction-sweep-through`, not from frontier-high's
  lo-hash again**: close the gap over a range spanning several commits;
  call `_correction_sweep_claim_and_process` once (processes frontier-
  high's lo-hash); call it again; assert it processes the position
  immediately after the first call's result, not frontier-high's lo-hash
  again — proving the dedicated watermark, not frontier-high's bound,
  drives resumption. (Unlike an earlier draft, this cannot be tested via a
  *further* `claim_high()` after the gap closes — `is_gap_empty()` makes
  `claim_high()` return `None` forever once closed, which is itself the
  point of the precondition.)
- **No-op when the sweep has already reached frontier-high's own
  `:hi-hash`**: close the gap; run `_correction_sweep_walk` to exhaustion;
  assert a further call to `_correction_sweep_claim_and_process` still
  returns `None` rather than erroring or re-processing the last commit.
- **Respects frontier-high's persisted `:hi-hash` as the ceiling, not
  `len(linearization)`**: close the gap and run the sweep to exhaustion
  against an initial `linearization`; then extend the underlying repo with
  new commits and rebuild a longer `linearization` (simulating an
  incremental re-ingest) *without* claiming the new positions via either
  stream; call the correction sweep again against the longer
  `linearization`; assert it still returns `None` and does not walk into
  the newly-added, unclaimed commits — the regression test for the bug an
  earlier draft's `len(linearization) - 1` ceiling would have produced.
- **Falls back to frontier-high's current lo-hash when the stored
  `correction-sweep-through` hash is stale**: seed
  `:ingestion/correction-sweep-through` with a hash not present in a fresh
  `linearization` (simulating rewritten/rebased history), with the gap
  otherwise closed; assert the sweep restarts from frontier-high's current
  lo-hash rather than erroring.
- **Frontier-low and `lineage-confirmed-through` are never touched**:
  close the gap; capture frontier-low's persisted interval and
  `_lineage_confirmed_through_query`'s value before running
  `_correction_sweep_walk`; run it; assert both are byte-for-byte unchanged
  afterward, while `_correction_sweep_through_query` has advanced — the
  direct regression test for the bugs both "Why not `claim_low()`" and "Why
  a dedicated watermark" describe.
- **Full integration**: build a repo with a non-trivial number of commits
  and a fixture constructed so no entity ever lands in case 2 (no
  hand-injected ambiguous/duplicate `:introduced-by` state — the fixture
  this design's other tests use for that scenario is deliberately excluded
  here, since case 2 leaving its candidate-diff record untouched is
  by-design behavior, not a bug this test should be asserting against);
  close the gap at some meeting point (claim some positions via
  `claim_low()`/`_frontier_persist_claim` from the low side, the rest via
  `_reverse_fill_claim_and_process` from the high side, until
  `allocator.is_gap_empty()`); then run `_correction_sweep_walk` to
  exhaustion; assert its returned `entities_left_unreconciled` count is `0`,
  every entity touched within frontier-high's claimed range ends up
  authoritative (`_lineage_is_provisional` is `False` for all of them), no
  `:type/candidate-diff` entities remain live, and
  `_correction_sweep_through_query` now equals frontier-high's `:hi-hash` —
  proving the sweep correctly walks all the way through frontier-high's
  claimed territory to its actual persisted ceiling, not the single
  position or the unbounded `len(linearization)` an earlier draft's
  (wrong) bounds would have produced.
- **`entities_left_unreconciled` counts case-2 skips**: reuse the
  duplicate-`:introduced-by` fixture from the case-2 multi-value test
  above; run `_correction_sweep_claim_and_process` over that commit; assert
  the returned count reflects the skipped entity and that a stderr message
  naming its ident was emitted — the direct test for the observability fix
  (a run that skipped everything must not look identical to a clean run).
