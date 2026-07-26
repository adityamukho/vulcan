# Stream 1 Correction Sweep - Spec Review

**Reviewed spec:** `docs/superpowers/specs/2026-07-25-stream1-correction-sweep-design.md`
**Review date:** 2026-07-25
**Review pass:** round 9 — spec after the three-way step split (commit `d1c93b3`)

> **Status: all round-9 findings applied to the design by the reviewer**
> (uncommitted, working tree). See "Round 9 Fixes Applied" at the end for the
> judgment calls made while fixing, which are worth a confirming read since they
> were not the spec author's own choices.

Round 8's Medium is properly fixed, and fixed the harder way: rather than
softening the prescription, the step is genuinely split into
`_correction_sweep_select_position` (DB reads, parse-free),
`_extract_commit` (reused unmodified, DB-free, process-pool-eligible), and
`_correction_sweep_apply` (DB writes, the three-case logic) — with the old
function demoted to an explicitly test-only wrapper, a concrete 2d loop sketch,
and a composition test that pins the wrapper against the three pieces. That is
the right factoring and it now matches forward walk's.

Both Lows are addressed too. What remains are consequences of the split itself:
the observability state the previous round introduced doesn't have a home in the
new signatures, and the prescriptive 2d snippet has two problems a verbatim copy
would inherit.

## Findings

### Medium: the log budget and the summary line are cross-call state with no owner, and the summary hangs off a function 2d is told not to call

design:890-894 specifies the cap as "a simple counter compared against a
constant, **not a per-call budget** — 2d's loop calls this function many times",
followed by "a single summary line once `_correction_sweep_walk` finishes if
`skipped_events > 0`". Neither has anywhere to live in the new design:

- **The budget.** `_correction_sweep_apply(db, commit_hash, commit_ts_iso,
  file_results, index_con=None) -> int` (design:736-751) returns a count and
  holds no state; each call is independent. A counter that spans calls has to be
  a module-level global or an explicit parameter, and the spec picks neither. The
  global variant has a specific failure mode worth pre-empting: this server is
  long-lived and can run several ingests (`handle_minigraf_ingest_git` starts a
  new run per invocation), so an unreset module counter exhausts its 10-line
  budget on the first ingest and silently logs nothing for every ingest after —
  the same "looks clean, isn't" failure the counter exists to prevent.
- **The summary.** It is attributed to `_correction_sweep_walk`, but design:795-797
  and design:855-858 both tell 2d not to call that function — its whole role is
  now "tests and callers that don't care about executor placement". So in the
  production path nobody emits the summary line, and the test that checks for it
  (design:1115-1121) only exercises the wrapper. The one observability output
  meant for operators is specified exclusively on the path operators won't use.

Required spec change: give the budget an explicit owner threaded through the
signature — a small mutable stats object (or an `int` in / `int` out pair) passed
to `_correction_sweep_apply` — and state that the summary line is the *driving
loop's* responsibility, emitted by `_correction_sweep_walk` and by 2d's own loop
alike, with the same wording. Also say the budget resets per run, however it is
stored.

References: design:736-751, design:786-800, design:855-858, design:878-897,
design:1115-1121

### Low: the 2d loop sketch drops `index_con`, which would silently desync the fact index

design:840-853's snippet calls:

```python
skipped_events = await loop.run_in_executor(
    write_executor, _correction_sweep_apply, db, commit_hash, commit_ts_iso, file_results,
)
```

`index_con` is missing. Every other call site in this design threads it
deliberately (`_lineage_confirm(..., index_con=index_con)`,
`_candidate_diff_clear(..., index_con=index_con)`, the `_retract`/`_transact` pair
at design:465-470), because `_transact`/`_retract` write the persisted fact index
as well as the graph. Followed verbatim, 2d's sweep would leave the index
diverged from the graph in exactly the direction that is hardest to notice:
`:modified-in` rows this sweep retracts stay live in the index, and the entities
it reconciles keep stale index rows, while the graph itself is correct.

The likely reason it was dropped is that `loop.run_in_executor(executor, func,
*args)` takes no keyword arguments. The fix is to pass it positionally (it is
already the 5th parameter) or wrap in `functools.partial`; either way the snippet
should show it, since a snippet this concrete will be copied.

References: design:840-853, design:736-751

### Low: the prescribed loop is strictly serial, giving up the extraction pipelining forward walk already has

In the sketch, commit `N+1`'s `_correction_sweep_select_position` cannot run
until commit `N`'s `_correction_sweep_apply` has written the watermark it reads
— so extraction and writing strictly alternate, with the process pool idle during
every write and the write executor idle during every parse. `_run_ingestion`
avoids exactly this with a bounded sliding window that extracts upcoming commits
ahead of time (`mcp_server.py:7443-7445`), and this sweep is, by the spec's own
terminal-pass framing, the single longest-running phase of the ingest — the place
where losing that overlap costs the most.

The dependency is only apparent. Once the gap-closed precondition holds, the
range `[pos, ceiling_pos]` is fixed for the rest of the run (the spec establishes
this at design:656-661), so successive positions are just successive integers: 2d
can call `_correction_sweep_select_position` once, then extract ahead over the
known range, applying in order to keep the watermark's crash-resume meaning
intact.

Required spec change: state which it is — either "2d may derive successive
positions locally after the first selection and pipeline extraction, applying
strictly in order", or "this sweep must not be pipelined", with the reason. As
written a reader would reasonably infer the serial shape is required.

References: design:833-870, design:656-661; `mcp_server.py:7440-7466`

### Nit

- The composition test (design:1122-1130) asserts the three-piece path produces
  graph state "identical to what `_correction_sweep_claim_and_process` produces
  for the same starting state" — which needs two independent graphs built from
  the same fixture, since the first run mutates the state the second would start
  from. Worth saying, because the cheaper-looking alternative (run the wrapper on
  the already-swept graph and assert nothing changed) tests idempotency instead
  of composition, which is a different property.

## Verified This Pass

- `loop.run_in_executor(executor, func, *args)` accepts no keyword arguments,
  which is what makes the `index_con` omission above a real trap rather than a
  typo.
- `_extract_commit`'s return value is safe to route through the process pool as
  the sketch does — `precomputed` carries only plain strings, sets, dicts and
  tuples (`unchanged_idents`, `body_hashes`, the `*_entries` lists), and its
  docstring already commits to never returning tree-sitter nodes across that
  boundary (`mcp_server.py:6968-6989`).
- `_correction_sweep_apply`'s parameter list is sufficient for the body
  design:333-506 specifies: `commit_ident` derives from `commit_hash`,
  `unchanged_idents` comes from each `precomputed`, and the watermark update needs
  only `commit_hash`/`commit_ts_iso`. No hidden input beyond the logging state in
  the first finding.

## Round 9 Fixes Applied

Applied directly to the design rather than handed back. Four judgment calls were
made that the spec author didn't get to make, flagged here for a confirming read:

1. **Budget ownership: `skipped_so_far`, a caller-threaded running total.**
   `_correction_sweep_apply` gains `skipped_so_far: int = 0` and logs only while
   `skipped_so_far + skipped_events < _CORRECTION_SWEEP_LOG_CAP`. Chosen over a
   mutable stats object or a module counter because it keeps the function pure
   (directly unit-testable by passing a chosen `skipped_so_far`) and resets per
   run for free — each loop starts its total at `0`. The wrapper and
   `_correction_sweep_walk` thread it through; `_CORRECTION_SWEEP_LOG_CAP = 10`
   is now a named constant beside `_CORRECTION_SWEEP_THROUGH_IDENT`.
2. **Summary line: extracted to `_correction_sweep_log_summary(skipped_events)`.**
   A named function rather than an inline print, precisely because two loops must
   emit the identical message — `_correction_sweep_walk` and 2d's own — and the
   previous draft's version existed only on the path 2d is told not to take.
   Specified as called *inline*, not through an executor: it only writes stderr,
   matching `_run_ingestion`'s own inline skip prints.
3. **`index_con` in the 2d sketch: passed positionally, with the reason inline.**
   `run_in_executor` takes `*args` only, so the comment in the snippet now says
   that explicitly and spells out the failure mode (graph correct, fact index
   silently diverged) so a future edit doesn't "clean up" the positional args.
4. **Pipelining: explicitly permitted, with an ordering constraint.** 2d may call
   `_correction_sweep_select_position` once, derive the remaining positions
   locally (the range is fixed once the precondition holds), and keep a sliding
   window of extractions in flight — but `_correction_sweep_apply` calls must
   still happen in ascending position order, since each advances the watermark
   and out-of-order application would let a crash leave the watermark above
   commits never applied. The synchronous wrappers stay strictly serial.

Also added three test bullets: the log cap resets per run (the direct regression
test for the module-global implementation, which would pass the existing capped-
logging test and still go silent on every later ingest), `_correction_sweep_log_summary`
is caller-driven, and the composition test now specifies two independent graphs
from one fixture with the reason (a second pass over a swept graph tests
idempotency, a different property).

Consistency checked after editing: code fences balanced, no stale references to
the old per-call budget or walk-owned summary.

## Cross-phase note (2b1)

Unchanged from round 8, plus one addition the split makes concrete:
design:872-876 now recommends 2b1 split `_reverse_fill_claim_and_process` the
same way (`mcp_server.py:7317-7411` has the identical parse-and-write
conflation). Agreed, and doing both at once keeps 2d's two driver loops
symmetrical — which matters more now that this spec ships a concrete loop shape
2d will copy for the other stream.

## Resolved From Prior Passes

- **Execution context prescribed an impossible wiring** (round 8, Medium):
  resolved by actually splitting the step (design:280-295, design:512-555,
  design:707-806, design:808-876), including the data-dependency reason for
  keeping `select_position` separate, an explicit "2d must not call this"
  on both wrappers, and a composition test. The residual findings above are about
  the snippet and the observability state, not the factoring.
- **Unbounded per-ident stderr** (round 8, Low): resolved in substance — capped
  at 10 with an exact returned count and a test that would catch capping the
  count along with the log (design:878-897, design:1115-1121). Ownership of the
  counter is the open item.
- **`entities_left_unreconciled` semantics** (round 8, Low): resolved thoroughly
  — renamed `skipped_events`, both gaps documented (per-`(ident, commit)`, and
  non-persistence across runs), the graph-query alternative named, and a test
  asserting `2` rather than `1` for one entity skipped at two commits
  (design:899-923, design:1109-1114).
- **Nits** (round 8): both resolved — `result[0]` in the resume test
  (design:1057-1058), and the deliberate return-shape divergence from
  `_reverse_fill_claim_and_process` called out for 2d (design:803-806).
- Rounds 1-7 remain resolved; round 8's copy of this section carries that
  history. The two standing facts: `_entity_introduced_by_set_provisional`
  returns early for authoritative entities (`mcp_server.py:5254-5256`), so a 2b1
  monotonicity guard cannot substitute for the gap-closed precondition; and
  identical `(entity, attribute, value, valid_from)` is a no-op while a differing
  `valid_from` duplicates.
