# Reverse-Bulk-Fill Corrections (2b1) — Design Spec

**Issue:** #222 (Phase 2, corrective sub-phase 2b1)
**Date:** 2026-07-25
**Branches from:** `design-222-phase2b-reverse-bulk-fill-walk` (`55dd993`, PR #228)
**Merge order:** #228 → **2b1** → 2c

## Background

Sub-phase 2b (PR #228, open) built Stream 2's reverse-bulk-fill walk. A
spec-plus-implementation review
(`docs/superpowers/specs/2026-07-24-reverse-bulk-fill-walk-review.md`, every
finding reproduced against a real `MiniGrafDb` and a real git repo) found three
High and four lower-severity defects in it, plus two phase-1 defects that 2b is
the first caller to exercise. 2b's code is already committed, so none of those
are changes to PR #228 — they are this sub-phase.

2b1 branches from 2b's tip rather than amending PR #228, so #228 keeps its
review history. This is safe because **2b has no caller wired into
`_run_ingestion`** — 2d does that — so merging #228 with these defects present
changes no production behavior. 2c stacks on 2b separately and is being
implemented concurrently; 2b1 touches only functions 2c does not add, so the two
branches conflict only incidentally (both edit `mcp_server.py`, in different
regions).

## Scope

**Group 1 — phase-1 root cause.** The frontier/allocator defects behind 2b's
infinite loop. Not 2b's bugs, but untestable without a caller that grows a
linearization, which is exactly 2b's walk — so they land here, as the first
commits, reviewable as their own half of the PR.

**Group 2 — 2b's own defects.** The seven findings in bucket (a) of the review,
plus one additional write path the review missed (see "Monotonicity" below).

**Group 3 — tests.** The review's twelve test gaps, anchored on four
high-value ones.

Explicitly deferred:

- **The interval lifecycle on incremental re-ingest** — what *should* happen
  when new commits land above a previously-filled region. See "Why re-ingest is
  made safe, not efficient" below. Assigned to 2d.
- **Duplicate `:introduced-by` from an uncoordinated forward walk** (review
  bucket (c)) — 2b has no caller and cannot prevent it; 2d's to fix. 2b1 adds a
  one-line note to 2b's spec so 2d inherits the constraint.
- 2b's own original deferrals — `:depends-on`, deletions, renames — unchanged.

## Design

### Group 1: phase-1 root cause

#### 1.1 `_extend` must pick the interval adjacent in the direction of growth

`FrontierAllocator._extend` looks up `_interval_covering(neighbor_pos)`, and
`_interval_covering` returns the **first match in insertion order**
(`frontier_registry.py:71-75`). With two provisional intervals in play, it can
merge into the wrong one and make no progress at all. From the review's
allocator-only repro (3 positions claimed in run 1, 2 commits added, high
interval reloaded as `[1, 2]` against a 5-position linearization):

```
claim 0: pos=4 gap_hi=3 intervals=[Interval(1,2,'provisional'), Interval(4,4,'provisional')]
claim 1: pos=3 gap_hi=2 intervals=[Interval(1,2,...), Interval(3,4,...)]
claim 2: pos=2 gap_hi=1 intervals=[Interval(1,2,...), Interval(2,4,...)]
claim 3: pos=1 gap_hi=1 intervals=[Interval(1,2,...), Interval(2,4,...)]
claim 4: pos=1 ...   (unchanged forever)
sequence: [4, 3, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
```

`_extend(1, ...)` finds `Interval(1,2)` via `_interval_covering(2)` and rewrites
it to `Interval(1,2)` — a no-op — pinning `gap_hi` at 1 so `is_gap_empty()` is
never true.

Fix: `_extend` selects the interval that is adjacent *in the direction of
growth* (for `from_low=False`, the one whose `lo_pos == pos + 1`; for
`from_low=True`, the one whose `hi_pos == pos - 1`), rather than any interval
covering the neighbour position. Where no such interval exists, append a new
one, as today. This makes `_extend` monotone by construction: every call either
extends an interval by exactly one position or creates a one-position interval,
and `gap_lo`/`gap_hi` therefore strictly converge.

#### 1.2 `_frontier_load` normalises an unrepresentable persisted interval

`_frontier_persist_claim` writes only the moved bound — `:lo-hash` for
`from_low=False` (`mcp_server.py:5007-5014`) — so `:hi-hash` is written once, at
interval creation, and never again. On a grown linearization the first
`claim_high()` of the new run returns the new last position and persists it as
`:lo-hash`, leaving the pair **inverted**:

```
after claiming pos 4: persisted frontier-high = ('pos4', 'pos2')
after claiming pos 3: persisted frontier-high = ('pos3', 'pos2')
```

`_frontier_load` will rebuild `Interval(lo_pos=4, hi_pos=2)` from that, which
`_interval_covering` can never match, so the whole high region reads as
unclaimed after a crash there.

Fix, in `_frontier_load`, applied to the high interval before it is
reconstructed:

1. If `lo_pos > hi_pos` (inverted), **discard** the interval.
2. If `hi_hash` is not the linearization's last position (the grown case),
   **discard** the interval.

Both cases mean "the persisted pair cannot faithfully describe this run's
claimed region", and dropping it yields one contiguous gap again —
`[frontier-low.hi + 1, N-1]` — which is the only shape the rest of phase 1 and
both streams are written against.

Discarding is sound rather than merely convenient because **re-processing an
already-processed position is idempotent**: the review verified this on a real
repo (`:introduced-by` and the `:modified-in` set byte-identical before and
after a replay, live count unchanged), and the mechanism is that a replayed
provisional move sees `superseded_ident == commit_ident` and skips the
retroactive edge. The graph converges to the same state; only work is wasted.

**Discarding must also retract the persisted facts, and persistence must mirror
the allocator.** Dropping the interval only from the in-memory allocator is not
enough — and getting this wrong re-creates the very state being fixed. If
`_frontier_load` discards `('pos4', 'pos2')` but leaves it in the graph, the
next `_frontier_persist_claim(from_low=False)` call sees `existing is not None`
and rewrites only `:lo-hash` again, reproducing an inverted pair on the first
claim of the new run. Two changes, together:

1. When `_frontier_load` discards an unrepresentable interval, it retracts that
   interval's persisted facts, so the graph and the allocator agree on "this
   side holds nothing".
2. `_frontier_persist_claim` writes the bounds of **the allocator's interval
   containing `pos`**, rather than inferring which single bound moved from the
   persisted pair. The caller already holds the allocator, so it passes the
   interval (or its `lo_pos`/`hi_pos`) in. This removes the whole class of
   drift between what the allocator believes and what is persisted, of which the
   never-moving `:hi-hash` is one instance: a freshly created one-position
   interval persists as `(pos, pos)`, and each subsequent claim persists the
   interval as it actually stands.

Consequence for 2c, which reads frontier-high's persisted bounds directly rather
than through the allocator: after a discard it sees no high interval at all
(`_frontier_read_bounds` returns `None`) instead of an inverted pair. Its
position-selection guard already returns `None` for both
(`2026-07-25-stream1-correction-sweep-design.md`, "Position tracker"), so the
sweep no-ops safely either way — but "absent" is the honest state, and it is the
one 2c's `low_bounds is None or high_bounds is None` branch was written for.

#### Why re-ingest is made safe, not efficient

Discarding means an incremental re-ingest re-walks the entire previously-filled
region — the opposite of what Stream 2 exists for. That is a deliberate,
bounded choice, and the reason is a representation limit this sub-phase should
not unilaterally change:

The persisted schema holds **one `lo`/`hi` pair per side**
(`mcp_server.py:4917-4925`). After growth the real state is three spans — the
authoritative low region, the already-filled high region, and the newly-arrived
commits above it — with the unclaimed gap now sitting **above** the high
interval rather than between the two. One pair per side cannot express that.

The correct resolution is that a fully-confirmed high region should be folded
into the authoritative low region on load, so the new commits form the new gap
with no re-walking at all. That requires knowing whether 2c's sweep has
confirmed the region — i.e. reading `:ingestion/correction-sweep-through`, a
watermark that does not exist yet (2c is being implemented concurrently) — and
it is a run-lifecycle decision spanning all three streams. It is therefore 2d's,
and this spec states it as an explicit open item rather than guessing. What 2b1
guarantees is that until 2d gets there, a re-ingest is **correct and
terminating**, just slower than it should be.

#### 1.3 Progress guard in `_reverse_bulk_fill_walk`

`while True: ... if result is None: break` (`mcp_server.py:7428-7437`) assumes
`claim_high()` either makes progress or returns `None`. With 1.1 and 1.2 fixed
that assumption holds, but the loop is unbounded by construction and drives an
`_extract_commit` (git subprocess plus tree-sitter parse), a DB write batch and
a `_db_checkpoint` fsync per iteration — an unbounded fsync loop inside what 2d
intends to run as a background task. The walk therefore tracks the last claimed
position and breaks, logging to stderr, if `claim_high()` does not return a
strictly smaller position. The guard stays even though the allocator is fixed:
it converts any future allocator regression from a hang into a loud stop.

### Group 2: 2b's own defects

#### 2.1 Split `:contains` out of the batched transact

`_reverse_fill_claim_and_process` writes every structural triple for a commit in
one call (`mcp_server.py:7377`). Minigraf's EAVT pending index omits value bytes
from the key, so facts sharing `(entity, attribute, valid_from)` in one
`transact` collapse to the last. Re-verified during this design:

```
batched  -> {"results":[[":function/c"]]}                      # 3 in, 1 survives
split    -> {"results":[[":function/x"],[":function/y"],[":function/z"]]}
distinctE-> 2 results                                          # different E: no collision
```

The third line matters for scoping the fix: only facts sharing the *same
entity* collide, so within `all_triples` the only affected attribute is
`:contains` (module→N children, class→N fields). The batched
`[ident :modified-in commit_ident]` writes are for distinct entities and are
safe as they are.

Fix: build `:contains` triples into a separate list and `_transact` each
individually, exactly as `_run_ingestion` does (`mcp_server.py:7957-7970`, which
carries a comment explaining the same constraint). The same rule applies to any
future repeated-`(E, A)` attribute, and to the `:parent` edges added in 2.6.

The consequence of not fixing this is permanent: no phase rewrites `:contains`,
and 2c's sweep touches only `:introduced-by`/`:modified-in`/lineage markers. The
review measured five of six module-containment edges and one of two
class-containment edges lost on a single ordinary file.

#### 2.2 Monotonicity: one invariant, three write paths

The invariant: **no entity may carry a `:modified-in` at a position earlier than
or equal to its `:introduced-by`'s position.** Forward walk cannot produce such
a fact (`_build_code_triples` emits `:modified-in` only on its already-known
branch, `mcp_server.py:6511-6521`); 2b produces both variants today, and nothing
in the graph, the audit, or the test suite detects it.

**Positions, not timestamps.** The comparison must use topological positions.
Committer dates are not monotonic in topological order — which is why
`build_linearization` uses `--topo-order` in the first place
(`frontier_registry.py:28-32`) — and this repository's own history contains a
real inversion (`df6b8be`, ~6 days earlier than its topological predecessor).
Comparing `:date` values would silently mis-order those commits.

`_entity_introduced_by_set_provisional` therefore takes two new parameters,
`pos: Optional[int]` and `pos_by_commit_ident: Optional[Dict[str, int]]`, and
refuses any move whose `pos` is not strictly less than the current guess's
position. The guard lives in the helper rather than the caller because the
helper's docstring already claims the contract ("reverse walk has now reached an
*earlier* commit", `mcp_server.py:5243-5252`) while nothing enforced it — which
is how the defect arose. Both parameters default to `None`, in which case the
guard is skipped and behavior is exactly as today, so 2a's existing callers and
tests are unaffected; 2b passes them always.

`_reverse_fill_claim_and_process` builds `pos_by_commit_ident` alongside the
`ts_by_commit_ident` map it already builds (`mcp_server.py:7396`), and applies
the invariant at all three write paths:

1. **The provisional move** — delegated to the helper's new guard. A rejected
   move leaves the guess where it is; the walk logs and continues.
2. **The retroactive `:modified-in`** — currently written unconditionally on
   every move (`mcp_server.py:7397-7407`). Skipped unless `superseded_ident` is
   strictly later than `commit_ident`.
3. **`already_authoritative_touched`** (`mcp_server.py:7379-7387`) — **not in
   the review's list**, and the remaining path that can violate the invariant: an
   entity whose `:introduced-by` is authoritative at some commit can still be
   handed a `:modified-in` at an earlier claimed commit, with no check at all.
   Gated on the same comparison. This is also the symptom half of the
   2c-interleaving scenario (`2026-07-25-stream1-correction-sweep-review.md`,
   round 4), so the two reviews' findings converge here.

Note what path 3 does **not** fix: 2b must never clobber an authoritative
`:introduced-by` (`mcp_server.py:5254-5256`, "never clobber a fact Stream 1 has
already confirmed"), so when this case fires, the entity's *introduction* is
still wrong — only the contradictory edge is suppressed. Preventing the wrong
lineage is 2c's gap-closed precondition, which stays load-bearing. The two fixes
are complementary, not alternatives.

#### 2.3 Clear the superseded candidate-diff record at move time

2b persists a candidate-diff record for every `(claimed commit, entity)` pair,
on both the first-sighting path and every provisional move
(`mcp_server.py:7391-7394`, `7399-7402`). Only the final, lowest one is a
correct guess. Measured: one function touched in each of 8 commits leaves 8
records for a single live `:introduced-by`. `_candidate_diff_clear`'s own
docstring states these must not "accumulate unbounded across a full ingest"
(`mcp_server.py:5210-5213`).

Fix: call `_candidate_diff_clear(db, superseded_hash, ident)` in the
`provisional_moves` loop, where `superseded_ident` is already in hand.
`superseded_hash` comes from stripping the `":commit/"` prefix (8 characters —
`_candidate_diff_ident` keys on `commit_hash[:12]`, which is exactly what the
ident was minted from). Storage then tracks O(live guesses), not O(entity
touches).

This does not remove 2c's opportunistic cleanup, which still covers records
orphaned by earlier runs.

#### 2.4 Re-date structural facts on each provisional move

2b writes an entity's `:entity-type`/`:ident`/`:description`/`:file`/`:path`
once, at the timestamp of the commit where the walk first *sighted* it
(`mcp_server.py:7353-7361`), and never re-dates them as the guess moves earlier.
The result is a valid-time window where lineage is live for an entity with no
type, name, or file:

```
REVERSE  valid-at 2026-01-15: [[':introduced-by', ':commit/cc340bfa5ae3']]
FORWARD  valid-at 2026-01-15: [':description','login'] [':entity-type',':type/function']
                              [':file','auth.py'] [':ident',...] [':introduced-by',...]
```

Current-time queries agree between the streams; only valid-time queries differ —
but `:valid-at`/`:as-of` history is the product's advertised feature, and every
such query joins `:entity-type`, so "which functions existed in January" returns
nothing across the whole reverse-filled region while "when was `login`
introduced" answers January.

Fix: in the `provisional_moves` loop, retract the entity's live structural
triples and re-assert them at the new (earlier) `commit_ts_iso`. The candidate
triples are already in `precomputed`, and `_retract` targets live rows
regardless of their original `valid_from`, so this is mechanical. Three
constraints:

- `:introduced-by` is excluded from the re-dated set — it is owned by
  `_entity_introduced_by_set_provisional`, which already retracts and re-asserts
  it at the right timestamp.
- The re-asserted set includes `:contains` edges, so it goes through 2.1's
  one-transact-per-edge path, not the batch.
- Cost is write amplification: an entity touched N times in the claimed range
  pays N retract+assert cycles of ~5 facts instead of one. Accepted in exchange
  for exact forward/reverse `:valid-at` parity, which 3.2 asserts directly.

#### 2.5 `commit_metadata`: state the contract, assert it, fix the spec

The shipped signatures take a 4th positional `commit_metadata` that 2b's own
design spec's "Driving functions" section does not contain at all, and
`_reverse_bulk_fill_walk` has no `run_ts_iso` despite the spec declaring one
(design:258-286 vs `mcp_server.py:7251-7259`, `7414-7422`).

The code indexes the parameter positionally against `linearization`
(`commit_metadata[pos]`, `mcp_server.py:7314`) while persisting
`linearization[pos]` (`:7409`). `build_linearization` and
`_git_commits(repo, watermark_hash=None)` are aligned, which is what every 2b
test supplies — but `_run_ingestion` builds a **watermark-relative** list
(`mcp_server.py:7486`, `4116`). Handed that on a resumed ingest, 2b either
raises `IndexError` or silently attributes entities to the wrong commit while
persisting the right one.

Fix: state the contract in the docstring and the 2b spec (full-history,
positionally aligned with `linearization`, i.e.
`_git_commits(repo, watermark_hash=None)`); assert
`len(commit_metadata) == len(linearization)` at entry to both functions, raising
a named error rather than mis-attributing; and correct the spec's stale
signatures. 2c's spec already documents the mirror-image contract with a
`return None` guard — the wording should match, with the difference in
failure mode (2b raises, since it is given a position by an allocator that has
already claimed it; 2c returns `None`, since it selects its own position and can
decline) stated in both.

#### 2.6 Emit `:parent`

Forward walk writes `[commit :parent parent_commit]` per commit, one transact
each (`mcp_server.py:7982-7999`, split for the same EAVT reason as `:contains`,
since a merge commit has two parents). 2b writes the other seven commit facts —
verified byte-identical to forward's — but no `:parent`, and the bootstrap
`ancestor` rule is defined purely over `:parent` (`mcp_server.py:46-49`), so
`ancestor` queries return nothing across the reverse-filled region. The gap is
not in 2b's deferred list either.

Fix: emit them, reusing the existing `_git_parent_hashes(repo_path,
commit_hash)` helper, one `_transact` per parent.

Reverse-walk-specific note to state in the docstring: the walk reaches a
commit's parents *later* than the commit itself, so a `:parent` edge points at a
commit entity that does not exist yet and materialises when the walk descends to
it (or when the forward stream covers it, for parents below frontier-high's
floor). The edge is temporarily dangling and converges — the same shape as the
lineage facts around it — and `ancestor` returns partial results until the
region is complete.

#### 2.7 Retroactive-`:modified-in`: correct the ownership note, stop back-dating

Two small things in the same paragraph (`mcp_server.py:7295-7301`,
design:244-254).

**Ownership.** 2b's docstring says 2c will correct the un-narrowed retroactive
edge using persisted candidate-diff hashes. The review recorded this as
ownerless because 2c's spec had dropped the case. That is now out of date: 2c's
case-3 reconcile retracts the over-asserted edge when its own `unchanged_idents`
disagrees (`2026-07-25-stream1-correction-sweep-design.md`, "Per-commit
algorithm"). So the limitation **is** owned by 2c — the docstring needs
rewording, not reassignment: 2c corrects the fact after the fact, rather than
2b preventing the write. 2b cannot self-narrow here, because narrowing needs the
superseded commit's own diff, which 2b does not have while processing a
different commit.

**Back-dating.** `superseded_ts = ts_by_commit_ident.get(superseded_ident,
commit_ts_iso)` (`mcp_server.py:7404`) falls back to the *current* commit's
timestamp when the superseded ident is absent from `commit_metadata` — earlier
than the modification it describes, i.e. a fact asserted valid before it was
true, silently. Fix: skip the edge and log to stderr instead of back-dating. With
2.5's alignment assertion this should be unreachable; it is a fail-safe, and a
silent wrong answer is the worst available behavior for one.

### Group 3: tests

Following `docs/testing-conventions.md` (real `MiniGrafDb`, no mocks), and the
existing real-git-repo fixture pattern. The review lists twelve gaps; these four
carry the most weight, and the rest follow mechanically.

1. **`:contains` completeness, forward vs reverse.** Ingest one multi-entity
   file (a class with 2 fields plus 3 functions) through
   `_reverse_bulk_fill_walk` and through `_run_ingestion`, and assert the
   `:contains` *sets* are equal for both the module and the class. Fails today
   with 1 edge against 6. This is the single highest-value test in the
   sub-phase.
2. **Valid-time parity, forward vs reverse.** Same repo through both streams;
   for every commit date in the range, assert the `:valid-at` fact set for a
   tracked entity is identical. Fails today at every date before the entity's
   first sighting. Pins 2.4.
3. **A genuine resume.** Every existing 2b test builds its allocator from a
   graph with no persisted frontier
   (`tests/test_mcp_server.py:13927`, `14082`, `14107`). Add: run a walk, add
   commits, rebuild the linearization, run again — asserting the second walk
   terminates, the persisted interval is never inverted, and no entity ends up
   with a `:modified-in` earlier than its `:introduced-by`. This one test covers
   the hang, the inverted interval, and the guess-moving-later corruption
   together.
4. **The cross-cutting invariant.** A helper asserting no entity carries a
   `:modified-in` at a position `<=` its `:introduced-by`'s, called at the end of
   every walk-level test. Cheap, and it would have caught 2.2 on its own.

Also, per the review: fix
`test_frontier_high_interval_advances_by_one`
(`tests/test_mcp_server.py:14045-14058`), which asserts only `hi_hash` — the one
bound `_frontier_persist_claim` never moves for `from_low=False` — and never
claims twice despite its name, so it would pass if the claim were never
persisted at all; and give
`test_walks_until_gap_closes_and_returns_count` (`:14062-14091`) a file touched
by more than one commit, so the provisional-move path it is meant to exercise
actually runs. Add coverage for the `"D"`/`"R"` skip, the structural
written-once invariant, the retroactive edge's `valid_from`, the unchanged-body
suppression path, the candidate-diff record count after a multi-commit walk, and
the `commit_metadata` misalignment assertion.

## Schema/audit safety

No new entity types or attributes. `:parent` is already registered optional on
`:type/commit` (`mcp_server.py:5399`). `:contains`, `:entity-type`, `:ident`,
`:description`, `:file`, `:path`, `:introduced-by` and `:modified-in` are
unchanged in both shape and registration; 2b1 changes only *when* and *in how
many transacts* they are written. The structural re-dating in 2.4 retracts and
re-asserts existing facts through the same `_transact`/`_retract` choke points
that already maintain the persisted fact index, so index consistency is
preserved by construction — every call threads `index_con`.

## Verified during design

- **EAVT collapse and its scope** — repro above; only same-entity facts collide,
  so `:contains` (and `:parent` on merge commits) is the whole exposure.
- **Committer dates are not topologically monotonic** — one real inversion in
  this repository's own 548-commit history, which is what forces position-based
  comparison in 2.2.
- **Baseline before any change** — full suite green in the 2b1 worktree: 915
  passed.
