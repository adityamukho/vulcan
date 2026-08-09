# Checkpoint cadence: a duty-cycle budget instead of once per commit

Design spec for **#241** (`_db_checkpoint`'s once-per-commit cadence is ~50% of
at-scale ingestion and grows with graph size). Part of **#222**'s post-#236
performance tier, sequenced alongside **#239**.

`_db_checkpoint` is the largest single call site in at-scale ingestion. This
spec establishes what it actually costs, corrects the issue's stated rationale
for the cadence, and replaces the per-commit cadence with a time budget whose
cost fraction does not grow with graph size.

## Summary of findings

Four measurements drive the design. All were taken on 2026-08-07 against
master `68ddb9d`; the raw artifacts are named in "Provenance" below.

1. **`checkpoint()` is O(graph size) and independent of how much was written
   since the last one.** It is WAL compaction, not an incremental flush.
2. **It is not a durability boundary.** Writes are crash-recoverable the moment
   they are transacted, via `<graph>.wal`. The issue's premise for the cadence
   is false.
3. **~51% of ingestion wall clock is inside it — but only ~47% of that is on
   the critical path**, and the split is not uniform: Stage B's checkpoints are
   ~92% critical-path, Stage A's are almost entirely hidden behind the parse
   pool.
4. **Batching divides total checkpoint cost by N**, essentially exactly, because
   cost is dirty-independent. This is the *opposite* of the retract cost model that
   #236 recorded, and the reason batching is worth doing here when it was not
   there.

### 1. Cost is linear in graph size, flat in dirty bytes

Growing one graph and timing the checkpoint after a single small transact,
versus after a 5,000-fact batch:

| facts | graph MB | ckpt after **1** fact | ckpt after **5,000** facts |
|---:|---:|---:|---:|
| 5,000 | 1.57 | 7.21 ms | 19.23 ms |
| 20,000 | 6.29 | 33.47 ms | 41.87 ms |
| 50,000 | 15.73 | **79.88 ms** | **87.04 ms** |

(Numbers as recorded in the committed probe artifact,
`evals/at_scale/results/241-checkpoint-cost.json`.)

~5.1 ms per MB of graph file (fit from the 1-fact column: (79.88 - 7.21) /
(15.73 - 1.57) MB). The two right-hand columns converging is the finding, but
that convergence is **asymptotic in graph size, not tight at every plateau** —
at 5,000 facts the batch column runs 2.67x the single-fact one (the batch's
own write cost is not yet swamped by checkpoint cost), narrowing to 1.25x at
20,000 and 1.09x at 50,000 as per-checkpoint cost comes to dominate the two
writes' own cost. It is only once that domination is complete that "flushing
one fact costs the same as flushing five thousand" holds tightly; the
underlying claim — checkpoint cost is a function of graph size, not dirty
bytes — is unaffected, since even the outlying 5,000-fact ratio is far below
what a dirty-bytes-proportional cost would produce (a 5,000x multiplier, not
2.67x).

Extrapolated to the at-scale graph at 126 MB this predicts ~643 ms, which
reproduces the 0.69 s per-call average recorded in #241 from an independent
harness. Total checkpoint cost across a run is therefore
`N_commits x avg_graph_size` — quadratic in history length. **That is the
super-linear term #241 went looking for.**

### 2. Checkpointing is not what makes a write durable

A child process transacted a checkpointed fact, an uncheckpointed fact, and an
uncheckpointed `:ingestion/watermark`, then died via `os._exit(9)` — no atexit,
no flush, no destructors. Reopening the graph recovered **all three**.

Repeating the experiment at 30,000 facts shows the mechanism:

| | `g.graph` | `g.graph.wal` | reopen after hard kill | recovered |
|---|---:|---:|---:|---|
| checkpoint each batch | 9.89 MB | — | 2.9 ms | 30,000 |
| **never checkpoint** | 0.00 MB | 2.57 MB | 114.9 ms | 30,000 |

Every transact appends to a write-ahead log; `checkpoint()` materializes that
log into the main file and truncates it. First-query time was unchanged (105 vs
98 ms), so a long WAL does not degrade in-run reads.

Minigraf also compacts on clean handle close: the `noop` ablation leg below
suppressed *every* explicit checkpoint and still left a complete 44.7 MB
`bench.graph` with no `.wal` beside it.

The repository already documented this without drawing the conclusion.
`SKILL.md:834` tells callers that when `minigraf_transact` returns a
checkpoint-failure warning, "the fact is durably written — do not retry."
That is the same property, stated for the interactive path: a write survives a
*failed* checkpoint, so it does not depend on a successful one. `SKILL.md`
needs no change here — the design leaves that path on the ungated
`_db_checkpoint`.

**Consequence for the design.** #241's scope item 3 frames the trade as
"batching every N commits trades recovery granularity for throughput." There is
no such trade. Recovery granularity is a property of the WAL and is unaffected.
What batching actually trades is **reopen latency** — roughly 45 ms per MB of
outstanding WAL, paid once by the next process to open the graph.

### 3. How much is on the critical path

`_db_checkpoint` runs on the single-threaded `write_executor` while parsing
proceeds on a process pool, so its seconds are not automatically wall clock.
Rather than infer the overlap from timers — a method this project has
repeatedly found unreliable — it was measured by **ablation**: the same
330-commit slice through the real `_run_ingestion`, varying only
`_db_checkpoint`.

| leg | wall s | vs `normal2` | ckpt n | ckpt s | Stage A | Stage B |
|---|---:|---:|---:|---:|---:|---:|
| `normal` | 193.7 | +8.0 | 662 | 99.2 | — | — |
| **`normal2`** (baseline) | **185.7** | — | 662 | 95.9 | 73.9 | 111.8 |
| `dedup` | 166.6 | **−19.1 (−10.3%)** | 497 | 75.2 | 76.3 | 90.3 |
| `every25` | 146.6 | −39.1 (−21.1%) | 662 | 4.6 | — | — |
| `noop` | 140.8 | −44.9 (−24.2%) | 662 | 0.0 | — | — |

All legs `status=complete`, 330/330 commits.

- **Attribution confirmed.** 95.9 s of 185.7 s = 51.6%, reproducing #241's 51%
  on a different slice size.
- **Critical-path share is about half that.** Suppressing checkpoints entirely
  recovers 44.9 s of 95.9 s — **~47% of checkpoint time, 24.2% of wall clock.**
  #241's "51%" is the right number for the wrong quantity; the recoverable
  ceiling is ~24%.
- **`every25` captures 87% of that ceiling** while still checkpointing 26
  times, and drives checkpoint time from 95.9 s to 4.6 s — a 20.8x reduction
  against a 25x cadence reduction. Batching divides by N, as predicted.

### 4. The two stages behave differently

`dedup` removes exactly the 165 duplicate Stage B checkpoints (662 − 165 = 497,
as fired). The phase marks show where the saving lands:

- Stage B: 111.8 s → 90.3 s (**−21.5 s**)
- Stage A: 73.9 s → 76.3 s (unchanged, within the ±4% run-to-run spread)

It removed 20.7 s of checkpoint time and recovered 19.1 s of wall clock —
**92% pass-through**, against the ~47% run-wide average.

The cause is structural. Stage B's sweep loop is serial: select, extract,
`_correction_sweep_apply`, `_forward_apply`, watermark update, checkpoint —
with no prefetch of the next commit's parse. Nothing overlaps its checkpoints.
Stage A runs the parse pool concurrently, so its checkpoints hide behind it.

**Stage A's checkpoints are close to free. Stage B's are close to fully
billed.**

## The duplicate

Stage B checkpoints twice per swept commit:

- `_forward_apply` (`mcp_server.py:9114`) checkpoints unconditionally at its
  tail, including on the `lifecycle_only=True` pass.
- `_run_ingestion`'s sweep loop (`mcp_server.py:10138`) checkpoints again
  immediately after, following `_correction_sweep_through_update`.

The 902 calls #241 recorded for a 450-commit slice decompose exactly —
225 forward + 225 reverse + 225x2 Stage B + 2 = 902 — as do the 662 observed
here for 330 commits. The first of the pair is a pure duplicate: it fires
*before* the watermark advances, so a crash between the two re-processes the
commit either way.

Note `mcp_server.py:8464` is **not** this site; it is `_reverse_apply`'s tail,
one legitimate checkpoint per reverse commit. #241's summary and the project
memory both name 8464, and both are wrong.

## Design

### A gated wrapper, with non-ingestion paths untouched by construction

```python
_ingest_checkpoint_policy: Optional["_CheckpointPolicy"] = None

def _db_checkpoint_gated(db: Any) -> bool:
    """Checkpoint unless the active ingestion policy says the budget is spent.

    checkpoint() is O(graph size) WAL compaction, NOT a durability boundary:
    minigraf's WAL makes every transact crash-recoverable on its own, and the
    handle compacts on clean close. This gate trades reopen latency, never
    data integrity (#241).
    """
    policy = _ingest_checkpoint_policy
    if policy is None:
        _db_checkpoint(db)
        return True
    return policy.maybe(db)
```

Ingestion call sites move to `_db_checkpoint_gated`. The three non-ingestion
sites — `_checkpoint_after_write` (3308), `minigraf_audit` (3835),
`_transact_extracted_facts` (6429) — keep calling `_db_checkpoint` directly and
are unchanged. With no ingestion in flight the policy is `None` and the wrapper
is behaviourally identical to today, so the interactive write path cannot
regress.

### The budget

`_CheckpointPolicy` holds the duty fraction, the last checkpoint's duration
`d`, and when it finished. `maybe()` checkpoints when

```
elapsed_since_last >= d * (1 / duty - 1)
```

which is the condition for checkpointing to consume no more than `duty` of wall
clock: a period of `d + W` containing `d` seconds of checkpoint satisfies
`d / (d + W) <= duty` exactly when `W >= d * (1/duty - 1)`. At `duty = 0.05` a
145 ms checkpoint suppresses the next for 2.75 s. The first call has no `d`
yet, so it checkpoints unconditionally and seeds one.

**This is what makes the cost fraction invariant to graph size.** As the graph
grows and `d` rises, the gate widens in proportion. Total checkpoint cost
becomes `duty x wall_clock` rather than `N_commits x avg_graph_size`, which
removes the super-linear term rather than dividing it by a constant.

The shape has direct precedent in this codebase: #242's ingestion poller sleeps
`max(poll_interval, duty_factor * last_poll_duration)` and was measured holding
8.56% duty on CI against 8.65% locally on 23%-slower hardware — the self-scaling
makes the bound hardware-independent. The same argument makes this bound
graph-size-independent.

Configured by `MINIGRAF_INGEST_CHECKPOINT_DUTY`, default `0.05`.

### Thread confinement

Every ingestion checkpoint site runs on `write_executor`, a single-worker
`ThreadPoolExecutor`. The policy's mutable state is therefore confined to one
thread and needs no lock of its own beyond the `_db_native_lock` that
`_db_checkpoint` already takes. This is an invariant worth stating in the
docstring rather than leaving implicit, because it is what licenses the absence
of a lock.

### Call-site changes

| site | change |
|---|---|
| `_forward_apply:9114` | wrap in `if not lifecycle_only:` **and** gate — removes the duplicate |
| `_reverse_apply:8464` | gate |
| sweep loop `:10138` | gate |
| lineage fold `:10169` | gate |
| `_correction_sweep_apply:9508` | gate (unreached in Stage B today; kept consistent) |
| final `:10184` | **stays unconditional**, and moves onto every terminal path |

### Crash and shutdown semantics

Recoverability does not change: the WAL already guarantees it, verified against
`os._exit(9)`. Only compaction timing changes. Two things become load-bearing
as a result.

**The final checkpoint must run on every terminal path.** Today
`mcp_server.py:10184` sits inside `if completed_all:`, so an interrupted or
errored run gets no final checkpoint at all. That is harmless under the current
cadence — at most one commit of WAL is outstanding — but under a budgeted
cadence an interrupted run could leave a whole run's WAL uncompacted, and the
next process to open the graph pays the replay. There is an implicit backstop
(`_db = None` drops the handle, and minigraf compacts on close), but relying on
refcount timing for a durability-adjacent property is precisely the kind of
implicit invariant this codebase has been bitten by before. Move the
unconditional checkpoint into the `finally`.

**The policy must be cleared in that same `finally`,** so a later interactive
`transact` is never gated by a stale budget from a finished run.

### What is deliberately not addressed

Per-checkpoint cost growth is upstream. `checkpoint()` compacts the entire
graph with no incremental path, and nothing in this repository can change that;
this spec bounds how often the cost is paid, not the cost itself. Filed
upstream separately — see "Follow-ups".

## Testing

Per this project's rule that a regression test must be ablation-proven, each
test below names the counterfactual that must fail without the fix.

| test | ablation that must fail |
|---|---|
| `_CheckpointPolicy` suppression math, injected clock: first-call seeding, duty boundary, widening as `d` grows | mutate the inequality; assertions must break |
| `policy is None` checkpoints on every call | make the wrapper unconditionally consult a default policy |
| Stage B's `lifecycle_only` pass issues no checkpoint | revert the `not lifecycle_only` gate |
| every terminal path — complete, `_shutdown_requested`, Stage B exception — ends with an unconditional checkpoint | leave the final checkpoint inside `if completed_all:` |

Wall-clock figures are **not** tests. They belong in a `benchmark.md` entry, and
the acceptance evidence is a completed at-scale run carrying a checkpoint-duty
row alongside the existing poll-duty row.

## One question the plan must settle by measurement

The `every25` and `noop` legs gated **both** stages, and both showed other DB
work getting slower. Against the `normal` leg, which has both figures recorded:
`_db_execute` 59.3 → 65.2 s and `_retract` 5.3 → 10.6 s in `noop`.
`every25` shows most of the same penalty despite keeping the WAL short,
so WAL length does not obviously explain it and no mechanism is established.

Given finding 4 — Stage A's checkpoints are nearly free *and* they keep the WAL
compacted — gating Stage B alone may capture most of the win without that
penalty. This spec specifies the uniform gate because it is simpler and
defensible; the implementation plan carries a `stageB-only` ablation leg to
settle it against data.

Stating this explicitly because two successive confident mechanisms were
demolished by measurement during #235 within hours. The penalty is recorded
here as an observation, not explained.

## Resolved: keep the uniform gate — no measurable win from narrowing to Stage B

**Added 2026-08-08, after landing `--checkpoint-mode` on
`profile_forward_reconcile_attribution.py` (#241 Task 5) and running the
four-leg ablation the plan promised.** Same 330-commit slice at
`82aa7e6c0353b52da5932f7ecbabdc7e42f6e418`, `--no-profile`, one leg at a time,
against branch HEAD (dedup + the shipped duty policy both already live, unlike
the legs in "How much is on the critical path" above, which predate both).

| leg | wall s | ckpt n | ckpt s | `_db_execute` s | `_retract` s | Stage A s | Stage B s |
|---|---:|---:|---:|---:|---:|---:|---:|
| `normal` (discarded — see caveat) | 150.4 | 52 | 7.3 | 67.4 | 10.5 | 75.2 | 75.2 |
| `normal-repeat` | 139.6 | 50 | 6.5 | 61.9 | 9.4 | 73.6 | 66.0 |
| `normal-clean` | 139.4 | 52 | 6.6 | 61.9 | 9.1 | 73.1 | 66.3 |
| `duty` (no flag, shipped default) | 141.5 | 52 | 6.8 | 62.6 | 8.8 | 74.7 | 66.8 |
| `stage-b-only` | 141.7 | 346 | 43.4 | 63.3 | 9.4 | 74.7 | 67.0 |

Load samples (8-core box), immediately before/after each of the four
**counted** legs (the discarded `normal` run's own load spike is covered in
the caveat immediately below, since it is the point of that caveat):

| leg | load avg before (1/5/15 min) | load avg after | top non-self process after |
|---|---|---|---|
| `normal-repeat` | 0.91, 1.09, 0.86 | 1.42, 1.35, 1.01 | brave ~2% |
| `normal-clean` | 0.27, 0.95, 0.89 | 1.33, 1.22, 1.01 | brave 91.8% (spiked right at the tail end) |
| `duty` | 0.71, 1.15, 1.04 | 1.26, 1.26, 1.10 | brave ~2% |
| `stage-b-only` | 0.93, 1.14, 0.99 | 1.32, 1.30, 1.08 | brave ~2% |

**Caveat on the discarded `normal` leg.** Its first run hit one genuine
mid-run write failure — `[_run_ingestion] skipping commit 295073c4...: write
failed: msg='Page 7737 out of bounds (total pages: 7737)'` — which
`_run_ingestion`'s per-commit try/except (`mcp_server.py:10168-10186`, its own
documented "fail only the one commit" contract) is confirmed by reading that
code to catch, log, and continue to the next commit rather than aborting or
raising. The run went on to report `status=complete`, 330/330 commits
attempted, with Stage B logging 7 entities left provisional. What that
confirms: the failure was isolated to one commit at the time it happened, and
the run did not crash or silently swallow the exception. What it does **not**
confirm: whether those 7 provisional entities were ever actually reconciled
by Stage B to a correct final state, or whether they remained permanently
provisional — no query was run against the resulting graph to check either
way. So the accurate claim is *no data loss observed in run status; final-state
reconciliation was not independently verified*, not "nothing was corrupted."
Separately, it did cost real wall clock — Stage B alone grew from ~66 s
(every clean run) to 75.2 s, accounting for essentially all of the 10.8 s gap
to `normal-repeat` — so this run is not a clean noise sample and is excluded
from the spread below. It did not reproduce on an immediate repeat
(`normal-repeat`) or on any of the other three legs. Load average climbed to
2.54 with a Brave tab at 29% CPU right as this run finished, versus 0.3–1.3
for every other counted leg (table above) — but `normal-clean`'s own load
spiked *harder* (Brave at 91.8%, over 3x the discarded run's 29.3%) with
**zero** resulting anomaly, which argues against a simple "load causes it"
story and should constrain whatever hypothesis a follow-up investigation
starts from. Whether checkpoint deferral widens whatever race window this is
remains unknown. **This is flagged as a follow-up below — it is a possible
correctness concern independent of the uniform-vs-Stage-B-only question this
section resolves**, and is not itself evidence for or against either gate
shape (it did not recur under `stage-b-only` or `duty`, both of which also
defer checkpoints).

**The noise floor.** `normal-repeat`, `normal-clean`, and `duty` are three
independent runs of the *identical* configuration — no flag, or
`--checkpoint-mode normal` (its default), changes nothing about how
`mcp_server.py` behaves, so `duty` is as much a repeat of `normal` as
`normal-repeat` is; `0.05` is already the shipped default duty, so "run
normal" and "run duty" are the same code path on branch HEAD. Put plainly:
this ablation is really **three samples of one baseline configuration plus
one variant leg (`stage-b-only`)**, not four independently-configured legs —
which is a stronger noise-floor estimate than a single normal/normal-repeat
pair would give, not a weaker one. Their wall clocks (139.4, 139.6, 141.5)
span **2.1 s, a 1.5% spread** — tighter than the ±4% this spec's earlier legs
recorded against pre-Task-1/2/4 code, but still the yardstick every delta
below must clear.

**`stage-b-only` clears none of it.** 141.7 s sits 0.2 s above the top of the
baseline cluster (`duty` at 141.5 s) and 1.6 s above its mean (140.2 s) —
both smaller than the cluster's own 2.1 s internal spread. `_db_execute`
(63.3 s) and `_retract` (9.4 s) are likewise inside the baseline band, not
below it: the "other DB work slowing" penalty this section was opened to
investigate does not appear at `duty = 0.05`'s actual operating point. It was
only ever observed on `every25`/`noop`, which suppress far more aggressively
(26 and 0 checkpoints against 330+ commits) than the shipped duty policy does
(50–52) — the two are not the same regime, and this ablation does not
speak to what happens at that far end.

**The clearest signal is in the checkpoint columns, not the wall clock.**
`stage-b-only` ran **346 checkpoints totaling 43.4 s** — 6.6x the count and
6.4x the seconds of `duty`'s 52 checkpoints / 6.8 s, because it lets Stage A
checkpoint on every commit, ungated. That 36.6 s of *additional* checkpoint
work bought back essentially nothing: total wall clock differs by 0.2 s. This
is finding 4 confirmed directly, not inferred: Stage A's checkpoints really
are close to free, hidden behind the parse pool, whether the duty policy
gates them (`duty`) or not (`stage-b-only`) — so narrowing the gate away from
Stage A neither costs nor buys anything measurable.

**Decision: keep the uniform gate.** Per this document's own rule — no
difference is meaningful unless it exceeds the measured spread — `stage-b-only`
is statistically indistinguishable from `duty`. The uniform gate is already
shipped, is simpler than a phase-aware one, and this ablation is the
counterfactual leg the plan required before believing otherwise. No
production code changed as a result (`mcp_server.py`'s gate stays uniform
across both stages, exactly as Task 4 landed it).

## Follow-ups

- **Investigate the one-off `Page N out of bounds (total pages: N)` write
  failure** hit by the discarded `normal` leg above (commit
  `295073c4eacd091c959ae7fc36100fcdd1dc24dc`, #241 Task 5). Did not reproduce
  across four other full-slice runs on the same branch, so it is not
  confirmed to be caused by this spec's checkpoint deferral — but it also
  cannot be ruled out, since a wider gap between checkpoints is exactly the
  kind of change that would widen a pre-existing race window (e.g. around
  `_run_ingestion`'s per-commit `_db = None` / reopen cycle racing a
  same-file checkpoint on another handle). `_run_ingestion`'s per-commit
  isolation (`mcp_server.py:10168-10186`) is confirmed to catch, log, and
  continue rather than crash — but whether the 7 entities left provisional
  by that run were ever correctly reconciled by Stage B was never checked
  against the resulting graph; treat that as unverified, not as "fine",
  until someone queries it. A simple load-causation story is also weaker
  than it first looks: `normal-clean`, one of the three clean baseline runs,
  saw a *larger* CPU spike (Brave at 91.8%, vs. the failing run's 29.3%)
  with no resulting anomaly at all — so elevated load alone is not
  sufficient to trigger this, whatever the real trigger is. This is not an
  emergency — the isolation mechanism did its job at the level it's
  responsible for — but it is new information this task's stress-scale run
  surfaced that the 963-test suite's small fixtures cannot exercise.

- Amend `benchmark.md`'s 20260803T095104Z entry. It attributes 33.6% to
  `_db_execute (query)` and leaves 28.3% unattributed to "process-pool
  `_extract_commit`, orchestration, thread hops", never naming `_db_checkpoint`
  — which the ablation puts at ~51% of wall clock. The entry cannot be
  reconciled from surviving artifacts: it was produced by an ad-hoc harness in
  `.superpowers/sdd/2026-08-03-fact-index-delete-by-rowid/`, which no longer
  exists, and no committed profiler of that era ran cProfile at all. Amend it
  against fresh numbers rather than defending it. Its `_db_execute` figure also
  predates #242, so it carries the old poller's query overhead.
- Re-examine **#239** against this. It is currently tracked as the dominant
  post-#236 cost on the strength of that same entry.
- File upstream against minigraf: `checkpoint()` is a full O(graph) compaction
  with no incremental path.
- Correct the `8464` line reference in #241's body and in project memory.

## Provenance

Measurements taken 2026-08-07 on master `68ddb9d`, 8 cores.

- Ablation legs: `evals/at_scale/profile_forward_reconcile_attribution.py`,
  `--no-profile`, 330-commit slice at `82aa7e6c`, one git worktree per leg
  patched only at `_db_checkpoint`. Baseline reproducibility ±4%
  (`normal` 193.7 s vs `normal2` 185.7 s); `normal2` is the clean baseline, as
  unrelated probes overlapped the `normal` leg.
- Checkpoint scaling and the WAL/durability probes are one-off scripts to be
  committed under `evals/at_scale/` with the implementation, alongside their
  recorded results, following the precedent set by #245's probe.
