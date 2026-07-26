# Reverse-Bulk-Fill Walk (Stream 2) — Spec + Implementation Review

**Reviewed spec:** `docs/superpowers/specs/2026-07-24-reverse-bulk-fill-walk-design.md`
**Reviewed implementation:** `_reverse_fill_claim_and_process` / `_reverse_bulk_fill_walk`
(`mcp_server.py:7251-7437`), `_entity_introduced_by_query` /
`_entity_introduced_by_set_provisional` (`mcp_server.py:5228-5263`), and
`_precompute_file_triples`' new `body_hashes` output (`mcp_server.py:6423-6438`)
**Tests reviewed:** `tests/test_mcp_server.py:6320-6385`, `13894-14115`
**Review date:** 2026-07-25

**2b is already committed** (commits `9c04595`..`55dd993`, PR #228 open). Nothing
below is a change request against 2b's current tree — every "Required change" is
scoped to a new **sub-phase 2b1**.

Every backend claim below was executed against a real `MiniGrafDb` and a real
throwaway git repo, not reasoned about. Repro output is inlined. The existing 13
2b tests all pass (`pytest -k "ReverseFillClaim or ReverseBulkFill or
EntityIntroducedBy"` → 13 passed), so none of this is a regression the suite
catches — see "Test gaps".

---

## (a) Real 2b defects

### High: the batched `_transact` silently drops all but one `:contains` edge per parent

`_reverse_fill_claim_and_process` writes every structural triple for the whole
commit in one batched call (`mcp_server.py:7377`):

```python
_transact(db, "[" + " ".join(all_triples) + "]", commit_ts_iso, index_con=index_con)
```

Minigraf's EAVT pending index omits value bytes from the key, so multiple facts
sharing `(entity, attribute, valid_from)` in a single `transact` collapse to the
**last** one. The forward walk knows this and splits `:contains` out into
one-transact-per-triple, with an explicit comment
(`mcp_server.py:7957-7970`, and `_ingest_close`'s docstring at
`mcp_server.py:4805-4807`). 2b does not.

Minimal confirmation of the backend behaviour:

```
_transact([[:module/m :contains :function/a] [:module/m :contains :function/b]
           [:module/m :contains :function/c]], "2026-01-01T00:00:00Z")
query [:find ?f :where [:module/m :contains ?f]]  ->  [[':function/c']]
```

End-to-end, one commit adding `m.py` with `class Acct` (2 fields) + 3 functions,
same repo ingested twice — once by `_reverse_bulk_fill_walk`, once by
`_run_ingestion`:

```
REVERSE  module :contains -> [':field/m-py-acct-rate']
REVERSE  class  :contains -> [':field/m-py-acct-rate']
FORWARD  module :contains -> [':class/m-py-acct', ':field/m-py-acct-bal',
                              ':field/m-py-acct-rate', ':function/m-py-a',
                              ':function/m-py-b', ':function/m-py-c']
FORWARD  class  :contains -> [':field/m-py-acct-bal', ':field/m-py-acct-rate']
```

All six entities exist with correct `:entity-type`/`:ident`/`:file`; **five of
six module-containment edges and one of two class-containment edges are gone.**
This is not a convergence problem that a later phase repairs: no phase rewrites
`:contains`, and 2c's sweep only touches `:introduced-by`/`:modified-in`/
lineage markers. The loss is permanent for the entire reverse-filled region, and
it hits essentially every real file (any file with >1 entity). It also silently
breaks the module→entity traversal `_preload_known_entities`'
`file_entities` reconstruction and every "what's in this module" query depends on.

**Required change (2b1):** split `:contains` (and any other repeated-`(E, A)`
attribute — `:depends-on` if it ever comes into scope) out of `all_triples` and
transact each individually, exactly as `_run_ingestion` does at
`mcp_server.py:7962-7970`. Add a test that a single claimed commit introducing
N>1 entities in one file yields N `:contains` edges, and a direct
forward-vs-reverse `:contains`-set equality test.
The spec must also state the EAVT batching constraint in its
"Per-commit algorithm" section — it currently says only "matching forward's
candidate-triple shape" (design:170-178), which is what caused the omission.

References: `mcp_server.py:7377`, `mcp_server.py:7957-7970`,
`mcp_server.py:4805-4807`, design:170-199

---

### High: `_entity_introduced_by_set_provisional` has no monotonicity guard, so a guess can move *later*

This confirms candidate finding #1's **effect** but refutes its **diagnosis**.
The contradiction ("entity modified before it was introduced") is reachable
**purely within 2b**, with no 2c involvement, and the root cause is not that
`_lineage_is_provisional` is the only gate — it is that neither
`_entity_introduced_by_set_provisional` (`mcp_server.py:5254-5263`) nor 2b's
provisional-move branch (`mcp_server.py:7397-7407`) ever checks that
`commit_ident` is *earlier* than the current guess. The helper's whole contract
is "move the guess **down**" (design:136-152, docstring `mcp_server.py:5243-5252`
say "reverse walk has now reached an *earlier* commit") but nothing enforces it,
and 2b's step-3 unconditionally writes `[E :modified-in <old guess>]` at the old
guess's own timestamp on every move — including a move in the wrong direction.

Reproduced end-to-end (3-commit repo, `auth.py::login` touched in all of them;
2b claims positions 2 and 1 in run 1; two commits are added; run 2 re-claims
4,3,2,1 — see the next finding for why re-claiming happens):

```
[after run1]      introduced-by=pos1 modified-in=['pos2']
RUN 2:
  claimed pos 4   introduced-by=pos4 modified-in=['pos1', 'pos2']     <-- contradiction
  claimed pos 3   introduced-by=pos3 modified-in=['pos1', 'pos2', 'pos4']
  claimed pos 2   introduced-by=pos2 modified-in=['pos1','pos2','pos3','pos4']
  claimed pos 1   introduced-by=pos1 modified-in=['pos1','pos2','pos3','pos4']
```

Two distinct wrong states, both persistent:

1. Transiently (and durably, if the run stops or crashes there) `:introduced-by`
   = pos4 while `:modified-in` includes pos1 and pos2 — an entity modified three
   commits *before* it was introduced. Forward walk cannot produce this.
2. Terminally, `:modified-in` contains **pos1, which is also `:introduced-by`** —
   the one edge forward walk provably never emits (`_build_code_triples`'s
   new-entity branch, `mcp_server.py:6511-6521`), and the exact invariant the 2c
   review asked for.

Because the guess ends up back at pos1, `_lineage_is_provisional` still reads
True and 2c will happily confirm a fact carrying a self-referential
`:modified-in`.

Note the consequence for candidate #1's original 2c scenario: a 2b1
monotonicity guard fixes the 2b half of it too. If `set_provisional` refuses to
move a guess to a *later* commit, and 2b's step-3 only writes the retroactive
`:modified-in` when `superseded` is genuinely later than `C`, then the
already-confirmed-entity case degrades to a benign no-op instead of a
contradiction. That makes the fix cheaper than the 2c review's "gate the sweep
on gap-closed" option, and the two should be designed together.

**Required change (2b1):** make `_entity_introduced_by_set_provisional` reject a
move to a commit that is not strictly earlier than the current guess (it needs a
position or timestamp argument to decide — pass `pos`, or `commit_ts_iso` plus
the current guess's `:date`), and skip the retroactive `:modified-in` when
`superseded_ident` is not strictly later than `commit_ident`. Add the invariant
test the 2c review already proposed: no entity has a `:modified-in` at a commit
whose position is `<=` its `:introduced-by`'s position.

References: `mcp_server.py:5236-5263`, `mcp_server.py:7364-7373`,
`mcp_server.py:7397-7407`, `mcp_server.py:6511-6521`, design:136-163,
design:201-242

---

### High: `_reverse_bulk_fill_walk` never terminates on any incremental re-ingest

`_reverse_bulk_fill_walk`'s loop is `while True: ... if result is None: break`
(`mcp_server.py:7428-7437`). It assumes `claim_high()` makes progress or returns
`None`. On a grown linearization it does neither.

Root cause is in phase 1 (see bucket (b)), but 2b is the first and only caller
that turns it into a hang, and 2b's own persistence call is what creates the
state. Allocator-only repro (3 commits claimed down to position 1 in run 1, then
2 commits added, `_frontier_load` rebuilds the high interval as `[1, 2]` against
the new 5-position linearization):

```
claim 0: pos=4 gap_hi=3 intervals=[Interval(1,2,'provisional'), Interval(4,4,'provisional')]
claim 1: pos=3 gap_hi=2 intervals=[Interval(1,2,...), Interval(3,4,...)]
claim 2: pos=2 gap_hi=1 intervals=[Interval(1,2,...), Interval(2,4,...)]
claim 3: pos=1 gap_hi=1 intervals=[Interval(1,2,...), Interval(2,4,...)]
claim 4: pos=1 ...   (unchanged forever)
sequence: [4, 3, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
```

`_extend(1, ...)` finds `Interval(1,2)` first via `_interval_covering(2)` and
rewrites it to `Interval(1,2)` — a no-op — so `gap_hi` is pinned at 1 and
`is_gap_empty()` is never true. `_reverse_bulk_fill_walk` therefore spins
forever, re-running `_extract_commit` (a `git diff-tree` + tree-sitter parse),
re-querying the DB, re-transacting, and calling `_db_checkpoint(db)` on every
iteration — an unbounded fsync loop, inside what 2d intends to run as a
background ingestion task.

It is also permanent, not a one-run accident. Because
`_frontier_persist_claim(from_low=False)` only rewrites `:lo-hash`
(`mcp_server.py:5012-5014`), positions 3 and 4 were processed but never
persisted, and the third run reloads the same `[1, 2]`:

```
RUN 3 reloaded intervals: [Interval(lo_pos=1, hi_pos=2, tag='provisional')]
RUN 3 claim_high sequence: [4, 3, 2, 1, 1, 1, 1, 1]
```

Position 0 — `login`'s true introduction — is **never** claimed by `claim_high()`
in any run, so the guess can never converge there via Stream 2 alone. (In 2d
`claim_low` would eventually cover it, so this part is not a correctness loss on
its own; the hang is.)

Mid-walk the persisted interval is also **inverted**, which is what the 2c
review flagged at second hand:

```
after claiming pos 4: persisted frontier-high = ('pos4', 'pos2')
after claiming pos 3: persisted frontier-high = ('pos3', 'pos2')
```

`_frontier_load` will happily rebuild `Interval(lo_pos=4, hi_pos=2)` from that,
and `_interval_covering` can never match it, so a crash at that point makes the
whole high region read as unclaimed.

**Required change (2b1):** (i) add a progress guard to
`_reverse_bulk_fill_walk` — track the last claimed position and break (loudly)
if `claim_high()` does not strictly decrease; (ii) fix the underlying phase-1
bugs, see bucket (b). The guard is worth having even after the allocator is
fixed, because 2b's loop is unbounded by construction.

References: `mcp_server.py:7428-7437`, `mcp_server.py:5012-5014`,
`mcp_server.py:4974-4978`, `frontier_registry.py:60-101`, design:81-103,
design:273-286

---

### Medium: structural facts keep the first-sighting `valid_from`, so `:valid-at` sees a phantom entity

This confirms candidate finding #4, and it is a genuine divergence from forward
walk, not an acceptable simplification.

2b writes an entity's `:entity-type`/`:ident`/`:description`/`:file`/`:path`
once, at the timestamp of the commit where the reverse walk first *sighted* it
(`mcp_server.py:7353-7361`, `commit_ts_iso` of the newest touch). When the guess
later moves earlier, `:introduced-by` and the retroactive `:modified-in` are
written at the *earlier* commits' timestamps — correctly — but the structural
facts are never re-dated. Result: a valid-time window in which `:introduced-by`
and `:modified-in` are live for an entity that has no type, name, or file.

Same 3-commit repo, commit dates Jan/Feb/Mar 2026, `_reverse_bulk_fill_walk`
over all three, then the identical repo through `_run_ingestion`:

```
REVERSE  valid-at 2026-01-15: [[':introduced-by', ':commit/cc340bfa5ae3']]
REVERSE  valid-at 2026-02-15: [[':introduced-by', ...], [':modified-in', ':commit/30cd355790c4']]
REVERSE  valid-at 2026-03-15: <all 7 facts>

FORWARD  valid-at 2026-01-15: [':description','login'] [':entity-type',':type/function']
                              [':file','auth.py'] [':ident',...] [':introduced-by',...]
FORWARD  valid-at 2026-02-15: <the above + ':modified-in' h1>
FORWARD  valid-at 2026-03-15: <all 7 facts>
```

Current-time queries are identical between the two streams (verified: same 7
facts) — only valid-time queries differ. But every history question the product
advertises (`:valid-at`, and the fact index's bi-temporal windows) joins
`:entity-type`, so "which functions existed in January" returns nothing for the
whole reverse-filled region while "when was `login` introduced" says January.
No later phase repairs this: 2c converts provisionality, it does not re-date
facts.

**Required change (2b1):** on a provisional move, retract and re-assert the
entity's structural triples at the new (earlier) `commit_ts_iso` — the candidate
triples are already in `precomputed`, so this is mechanical. Or, if the cost is
unacceptable, the spec must say so explicitly and the limitation must be owned
somewhere (see the ownerless-limitation finding). Either way the spec's implicit
"converges to the same graph forward walk would have produced" claim
(design:223-224, design:238-242) needs a valid-time carve-out, and there should
be a forward-vs-reverse `:valid-at` parity test.

References: `mcp_server.py:7353-7361`, `mcp_server.py:7389-7407`,
design:170-178, design:223-242

---

### Medium: `commit_metadata`'s positional-alignment contract is undocumented, and the spec's signature doesn't have the parameter at all

Confirms candidate finding #3, with an extra problem: **the spec is stale.**
design:258-286 declares

```python
def _reverse_fill_claim_and_process(db, repo_path, linearization, allocator,
                                    ignore_patterns=(), index_con=None) -> Optional[str]
def _reverse_bulk_fill_walk(db, repo_path, linearization, allocator, run_ts_iso,
                            ignore_patterns=(), index_con=None) -> int
```

The shipped signatures take a 4th positional `commit_metadata` and
`_reverse_bulk_fill_walk` has no `run_ts_iso` (`mcp_server.py:7251-7259`,
`7414-7422`). `commit_metadata` appears only in prose (design:232-233) with no
statement of where it comes from or what it must be aligned to.

That matters because the code indexes it positionally against `linearization`:

```python
commit_hash, commit_ts_iso, author, subject = commit_metadata[pos]   # :7314
...
_frontier_persist_claim(db, linearization, pos, from_low=False, ...)  # :7409
```

`build_linearization` and `_git_commits(repo, watermark_hash=None)` both use
`git log --topo-order --reverse`, so they are aligned — that is what every 2b
test passes (`tests/test_mcp_server.py:13926`, `14081`). But `_run_ingestion`
builds `commits = _git_commits(repo_path, watermark, branch)`
(`mcp_server.py:7486`), a **watermark-relative** list (`mcp_server.py:4116`).
Hand that to 2b on a resumed ingest and `commit_metadata[pos]` either raises
`IndexError` (for `pos` near HEAD) or silently returns a *different* commit —
2b would then write `:introduced-by`/`:modified-in`/candidate-diffs attributing
entities to the wrong commit, while `_frontier_persist_claim` persists the
right one from `linearization[pos]`. Silent, systematic misattribution.

**Required change (2b1):** state the contract in the spec and the docstring
(`commit_metadata` is full-history, positionally aligned with `linearization`,
i.e. `_git_commits(repo, watermark_hash=None)`), and either assert
`len(commit_metadata) == len(linearization)` at entry or derive the metadata
inside the function from `linearization[pos]`. Update the spec's "Driving
functions" signatures to what actually shipped. 2c's spec has the mirror-image
version of this problem; fix them consistently.

References: `mcp_server.py:7251-7259`, `7314`, `7409`, `7414-7422`, `7486`,
`4110-4132`, design:258-286, `tests/test_mcp_server.py:13926`

---

### Medium: candidate-diff records are now unbounded scratch, with no owner to clear them

2b persists a candidate-diff record for **every** `(claimed commit, entity)`
pair, on both the first-sighting path and every provisional move
(`mcp_server.py:7391-7394`, `7399-7402`). Only the final, lowest one is a
correct guess; every superseded record is stale by construction, and the spec
explicitly hands reconciliation to 2c (design:62-63, design:213-220).

2c's spec has since **removed** the wrong-guess case from its scope entirely
(deferred to 2d — see `2026-07-25-stream1-correction-sweep-review.md:190-199`),
so nothing clears them. Measured on an 8-commit repo where one function is
touched in every commit:

```
processed: 8
live :introduced-by count: [[1]]
candidate-diff records for that one entity: 8
```

`_candidate_diff_clear`'s own docstring states the purpose is "so these scratch
facts don't accumulate unbounded across a full ingest"
(`mcp_server.py:5210-5213`). As shipped they accumulate at O(entity touches),
not O(entities) — on a real repo that is one unregistered-type entity with four
facts per entity per commit in the reverse-filled region.

**Required change (2b1):** clear the superseded record at move time
(`_candidate_diff_clear(db, superseded_hash, ident)` in the `provisional_moves`
loop) — 2b has `superseded_ident` in hand and it is the cheapest possible place
to do it — or explicitly assign the cleanup to 2d in both specs. Add a test
pinning the record count after a multi-commit walk.

References: `mcp_server.py:7389-7407`, `5207-5225`, design:62-63, design:201-220

---

### Low: the documented retroactive-`:modified-in` limitation now has no owner

Confirms candidate finding #6. `_reverse_fill_claim_and_process`'s docstring
(`mcp_server.py:7295-7301`) and design:244-254 both say the retroactive
`:modified-in` for a superseded commit skips #221's unchanged-body narrowing and
that "2c already has each commit's persisted candidate-diff body hash available
to correct this precisely during its own sweep". 2c's spec no longer contains
that case. The limitation is now asserted by 2b, described as 2c's to fix, and
owned by nobody.

Same paragraph, second problem: the timestamp lookup fails open in the wrong
direction.

```python
superseded_ts = ts_by_commit_ident.get(superseded_ident, commit_ts_iso)   # :7404
```

If `superseded_ident` is not in `commit_metadata` (a guess written by an earlier
run with a different `commit_metadata`, an out-of-range guess, or a
foreign/test-seeded ident), the retroactive edge is written at `C`'s timestamp —
which is *earlier* than the modification it describes, i.e. a fact asserted valid
before it was true — silently.

**Required change (2b1):** point the limitation at whichever phase actually owns
it (2d, per the deferral chain) in both the docstring and design:244-254; and
make the missing-timestamp case explicit — skip the edge and log, rather than
back-dating it.

References: `mcp_server.py:7295-7301`, `7396-7407`, design:244-254,
`2026-07-25-stream1-correction-sweep-review.md:190-199`

---

### Low: `:parent` edges are not written, and the gap is not in the spec's deferred list

Forward walk writes `[commit :parent parent_commit]` for every commit, one
transact each (`mcp_server.py:7982-7999`). 2b writes the same seven commit-entity
facts as forward (verified byte-identical, `mcp_server.py:7321-7329` vs
`7634-7642`, both at `commit_ts_iso` from the same `_git_commits` formatter, so
they are genuinely idempotent across streams) but no `:parent`. Confirmed on a
reverse-filled commit:

```
commit facts: [:author, :date, :description, :entity-type, :hash, :ident, :subject]
```

The bootstrap `ancestor` rule is defined purely over `:parent`
(`mcp_server.py:46-49`), so `ancestor` queries return nothing across the entire
reverse-filled region. The spec's "Explicitly deferred" list (design:52-64) names
`:depends-on`, renames, and deletions — not `:parent`.

**Required change (2b1):** either emit `:parent` (one transact per parent hash,
same EAVT reason as `:contains`) or add it to the deferred list with the
`ancestor`-rule consequence spelled out.

References: `mcp_server.py:7321-7329`, `7982-7999`, `46-49`, design:52-64

---

## (b) Pre-existing phase-1/2a defects 2b inherits

These are not 2b's bugs, but 2b is the first caller that exercises them, so 2b1
(or a phase-1 patch it depends on) has to deal with them. Both are the real
mechanism behind candidate finding #2, which is **confirmed**.

1. **`_frontier_persist_claim` only moves one bound for `from_low=False`**
   (`mcp_server.py:5007-5014`). `:hi-hash` is written once, on interval
   creation, and never again. On a grown linearization the persisted high
   interval becomes inverted (`('pos4','pos2')` above) and then permanently
   understates the claimed region — positions genuinely processed are lost from
   persistence and re-processed on every subsequent run. This is also the
   corruption the 2c review flagged as "flag it for 2b/2d to fix"
   (`...correction-sweep-review.md:103-111`); it is confirmed, and it is worse
   than that note implies because it never self-heals.

2. **`FrontierAllocator._extend` / `_interval_covering` can produce overlapping
   intervals and a stuck `gap_hi`** (`frontier_registry.py:71-101`).
   `_interval_covering` returns the first match in insertion order, so when two
   provisional intervals exist `_extend` can merge into the wrong one and make no
   progress. Only reachable with two provisional intervals, which only arises
   from (1). Fix: have `_frontier_load` normalise/merge/discard an inverted or
   overlapping persisted interval, and have `_extend` pick the interval adjacent
   in the direction of growth rather than the first covering one.

3. **`_code_ident` slug collisions** are acknowledged as best-effort
   (`mcp_server.py:4072-4074`). 2b's `body_hash_by_ident` / `unchanged_by_ident`
   are accumulated across the whole per-file loop keyed only by ident — see the
   refutation below for why this adds nothing on top of the pre-existing issue.

---

## (c) Correctly deferred to 2d

- **Duplicate `:introduced-by` from the forward walk** (candidate finding #5) is
  real and is strictly 2d's. Verified mechanism: `_run_ingestion` seeds
  `entity_valid_from` once from `_preload_known_entities`
  (`mcp_server.py:6593-6635`), 2b's DB writes never enter that dict, and
  `_build_code_triples`' gate is dict membership (`mcp_server.py:6512`), so a
  forward walk reaching a 2b-introduced entity mid-run emits a second
  `:introduced-by` at a different `valid_from` — a second live datom, not a
  no-op. 2b has no caller and cannot prevent it. Worth noting: `:introduced-by`
  written by 2b **is** visible to `_preload_known_entities` on a *later* run
  (2b writes the commit's `:date`, which that query joins on,
  `mcp_server.py:6620-6621`), so the hazard is same-run only. 2b's spec does not
  acknowledge the constraint at all — 2b1 should add a one-line note so 2d's
  spec inherits it.
- **The 2c-confirm-then-2b-descend interleaving** (candidate #1's original
  framing). Note however that the 2b1 monotonicity guard above fixes 2b's half
  of it cheaply, so this should not be designed as a 2c/2d-only problem.
- `:depends-on`, renames, deletions, and the born-and-reborn-in-gap case
  (design:52-64) — all correctly out of scope and consistently absent from the
  implementation.

---

## Refuted candidate findings

- **#1, as diagnosed.** The claim that `_lineage_is_provisional` being 2b's only
  gate means "2b will never correct `:introduced-by` again once 2c confirms" is
  true but is not the load-bearing defect, and the conclusion "not reachable
  purely within 2b" is **wrong**. 2b alone produces both `:modified-in` earlier
  than `:introduced-by` and `:modified-in` equal to `:introduced-by` (repro
  above), because the real gap is the missing monotonicity check in
  `_entity_introduced_by_set_provisional`. Nothing in the graph or in any query
  detects either state — there is no constraint, no audit rule
  (`handle_minigraf_audit` only checks attribute/type validity, and both
  attributes are registered optional on all five types,
  `mcp_server.py:5307-5352`), and no test.
- **Bi-temporal history explosion from repeated `:introduced-by`
  retract+reassert.** Refuted. After 8 downward moves of one entity's guess:
  `:any-valid-time` `:introduced-by` count `[[1]]`, one distinct value, one
  lineage-marker fact. `_retract` removes the live assertion rather than
  leaving a queryable historical datom (`mcp_server.py:3568-3588`), so there is
  no N² join risk here.
- **Cross-file ident collision in `unchanged_by_ident` / `body_hash_by_ident`.**
  Refuted as a 2b-specific issue. `_code_ident` interpolates `file_path` into
  every function/class/variable/field ident (`mcp_server.py:4065-4079`), so two
  entities in different files cannot share an ident except via the pre-existing
  best-effort slug collision the docstring already documents — and in that case
  forward walk conflates the two entities completely, so 2b's accumulate-then-
  key-by-ident is not an amplification. No change needed.
- **`already_authoritative_touched` double-writing a `:modified-in` 2b already
  wrote in the same commit.** Refuted. The classification at
  `mcp_server.py:7368-7373` puts each ident in exactly one of
  `provisional_moves` / `already_authoritative_touched`; the retroactive write
  is guarded by `superseded_ident != commit_ident` (`:7403`); and across commits
  a duplicate would need the same `(E, :modified-in, C)` at the same
  `valid_from`, which minigraf treats as a no-op. Verified: re-processing the
  same position leaves the `:modified-in` set and `:introduced-by` count
  unchanged.
- **Resume-safety of a partially processed commit.** Refuted as a defect.
  Re-invoking `_reverse_fill_claim_and_process` for the *same* position is
  idempotent — verified on a real repo: `:introduced-by` and the `:modified-in`
  set are byte-identical before and after a replay, live count stays 1. The
  mechanism is that a replayed provisional move sees
  `superseded_ident == commit_ident` and skips the retroactive edge. (The spec's
  claim of a single "atomic write boundary", design:92-103, is nonetheless
  aspirational — the implementation issues 3+N separate `_transact`/`_retract`
  calls and relies entirely on `_db_checkpoint` being the durability boundary.
  Worth a wording fix, not a code change: replay idempotency is what actually
  makes it safe, and that is worth saying instead.)
- **2b's commit-entity facts diverging from forward walk's.** Refuted — the
  seven triples at `mcp_server.py:7321-7329` are string-identical to
  `mcp_server.py:7634-7642`, and both use `commit_ts_iso` from the same
  `_git_commits` `strftime` (`mcp_server.py:4130`), so cross-stream re-writes
  are true no-ops. (`:parent` is the one omission — see above.)
- **`unchanged_by_ident` gating for already-authoritative entities.** Works.
  Verified: an authoritative entity whose body is byte-identical across the
  claimed commit gets no `:modified-in` (`extra() modified-in -> []`), and
  modules are correctly never gated (they carry no `unchanged_idents` entry, so
  `.get(ident, False)` yields False — matching forward's deliberate
  non-gating at `mcp_server.py:6505-6509`).
- **Retroactive `:modified-in` `valid_from`.** Correct. Verified via `:valid-at`:
  the superseded commit's edge is live at that commit's own date and not before
  (`valid-at 2026-02-15` shows the h1 edge, `2026-01-15` does not).
- **Schema/audit safety.** Confirmed fine, as the spec claims (design:68-79).
  `:introduced-by`/`:modified-in` are registered optional on module/function/
  class/variable/field (`mcp_server.py:5307-5352`); `:type/lineage-marker` and
  `:type/candidate-diff` stay unregistered and 2b reaches them only through 2a's
  internal-`_transact` helpers, never a public handler.

---

## Test gaps

Behaviours the spec claims that no existing test would catch if broken:

1. **`:contains` completeness.** No test asserts more than one containment edge,
   so the High finding above is invisible. A forward-vs-reverse fact-set
   equality test on a multi-entity file is the single highest-value test to add.
2. **Any resume at all.** Every test builds the allocator from a graph with no
   persisted frontier (`_frontier_load` on a fresh `real_db`,
   `tests/test_mcp_server.py:13927`, `14082`, `14107`). Nothing exercises a
   second run, a grown linearization, or a persisted frontier-high — i.e. the
   hang, the inverted interval, and the guess-moving-later corruption are all
   untested.
3. **`_reverse_bulk_fill_walk` termination** under anything other than a
   single-run empty-frontier start.
4. **`test_frontier_high_interval_advances_by_one`**
   (`tests/test_mcp_server.py:14045-14058`) checks only
   `hi_hash == linearization[-1]` — it never looks at `lo_hash`, which is the
   only bound `_frontier_persist_claim` actually moves for `from_low=False`, and
   never claims twice despite its name. It would pass if the claim were never
   persisted at all.
5. **`test_walks_until_gap_closes_and_returns_count`**
   (`tests/test_mcp_server.py:14062-14091`) uses three commits each adding a
   *different* file, so no entity is ever touched twice — the provisional-move
   path, the retroactive `:modified-in`, and the candidate-diff churn are never
   exercised at walk level, and the `fn_ident_a == :commit/lin[0]` assertion is
   true trivially (a.py exists only from h0).
6. **Valid-time parity with forward walk.** Nothing queries `:valid-at` or
   `:as-of` against 2b's output, so the phantom-entity finding is invisible.
7. **`valid_from` of the retroactive `:modified-in`.**
   `test_walking_backward_moves_introduced_by_to_the_oldest_commit`
   (`:13984-13988`) asserts only the *set* of `:modified-in` values; the spec's
   explicit rule that the edge carries the superseded commit's own timestamp
   (design:229-237) is unasserted.
8. **The unchanged-body gate on `:modified-in`.**
   `test_already_authoritative_entity_only_gets_modified_in` uses a function
   whose body *changed*, and `test_two_entities_in_one_commit...` seeds `extra()`
   (whose body is identical h1→h2) but asserts nothing about its `:modified-in`.
   The suppression path is untested even though it works.
9. **`"D"`/`"R"` skip** (`mcp_server.py:7337-7338`) — no test.
10. **Structural "written once" invariant** (design:174-178) — no raw count
    check on `:entity-type`/`:description` after a multi-commit walk, only on
    `:introduced-by`.
11. **`commit_metadata` misalignment** — no test, and no assertion in the code,
    so a 2d wiring mistake fails silently.
12. **The cross-cutting invariant** "no entity has a `:modified-in` at a position
    `<=` its `:introduced-by`" — would have caught the High monotonicity finding
    and is cheap to assert at the end of any walk test.
