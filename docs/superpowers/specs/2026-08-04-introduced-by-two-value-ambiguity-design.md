# Two-value `:introduced-by` ambiguity — design

Issue: #235. A #222 phase 2d defect, independent of #233 and reproduced
identically on `master` at `bbe7fee`. Sequenced ahead of #231/#238/#239 because
it is a live data-corruption bug: the corruption is permanent once created and
nothing in the system repairs it.

## Problem

An entity can end up with **two live `:introduced-by` facts**. The pair never
converges — it regenerates on every subsequent write — and
`_correction_sweep_apply` only fails safe on it, never repairs it.

### Why a second fact is minted

`_forward_apply`'s non-`lifecycle_only` branch (`mcp_server.py:8354`) prefilters
reconciliation candidates through `state.provisional_idents`:

```python
candidate_in_set = [
    ident for ident in _forward_candidate_idents(precomputed)
    if ident in state.provisional_idents          # run-start snapshot
]
reconcilable = [
    ident for ident in candidate_in_set
    if _lineage_is_provisional(db, ident)
]
```

`state.provisional_idents` comes from `_preload_provisional_idents`
(`mcp_server.py:7317`), taken once at run start. On a fresh ingest it is empty.
Stream 2 writes its provisional guesses *during that same run*, so they are
never in it.

The prefilter therefore drops every same-run guess before the DB-authoritative
`_lineage_is_provisional` check can see it. `reconcilable` comes out empty,
`_forward_reconcile_provisional` never fires, and `_build_code_triples` — whose
own gate is `state.entity_valid_from` membership, which the reverse stream also
does not populate — mints a second `:introduced-by` alongside the guess.

The comment block at `8306`–`8326` already describes this failure mode, and even
notes for the `lifecycle_only` branch that the snapshot "is empty on a fresh
ingest, and the guess was made during this same run". Its reasoning about
*stale-positive* entries (an ident confirmed authoritative mid-run that the set
still lists) is correct and stays correct. Its conclusion — keep the set as a
cheap prefilter — is the defect: the set is also stale-*negative*, and a
prefilter's false negatives are unrecoverable because the authority never runs.

### Why it never converges

`_entity_introduced_by_query` returns `results[0][0]` — the first of possibly
several values, in unspecified order, silently. Once an entity has two, every
later reader sees only one, retracts that one, and asserts a fresh one, so the
pair regenerates rather than collapsing:

```
[reverse@pos11] :function/mod-py-born-2 existing=[':commit/1cbcc20d42b5'] new=:commit/5a47b50d8cba
```

— the retract landed on the *other* value. Meanwhile `_correction_sweep_apply`
fails safe on any count ≠ 1 (case 2, and case 3's `else`), so nothing repairs it.

### Measured impact

Synthetic repo, 14 commits, driven through the real `_run_ingestion` (both
streams plus the Stage B sweep) against a real on-disk graph:

| | entities with 2 live `:introduced-by` | sweep "left provisional" lines |
|---|---|---|
| `master` @ `bbe7fee` | 6 | 11 |
| `master`, n=20 commits | 9 | 11 (log cap) |

Also visible in a full at-scale ingestion of this repository:

```
[_correction_sweep] :function/install-py-plugin-version left provisional at b44db0c8667c...
  (introduced-by values: [':commit/b44db0c8667c', ':commit/c2fd1eb6a515'])
```

Monkeypatching the preload so `provisional_idents` always reports membership —
leaving `_lineage_is_provisional(db, ...)` as the sole authority — gives 0
two-value entities, 0 second-mint events, 0 sweep skips.

## Design

Three parts: prevent new corruption, repair existing corruption, make residual
ambiguity visible.

### 1. Prevent — drop the prefilter

In `_forward_apply`'s non-`lifecycle_only` branch, `_lineage_is_provisional`
becomes the sole authority, mirroring what the `lifecycle_only`/`"R"` branch at
`8337` already does:

```python
candidate_in_set = _forward_candidate_idents(precomputed)
reconcilable = [i for i in candidate_in_set if _lineage_is_provisional(db, i)]
```

The eviction loop at `8390` stays and keeps its invariant — everything examined
leaves the set — but now drains over the full candidate list rather than the old
snapshot-filtered one, so a resumed run's stale entries still clear.

The `8306`–`8326` comment is rewritten: the stale-positive reasoning is retained
(it is why the DB check must remain, rather than trusting a set that would
otherwise be sufficient), and the "cheap prefilter" conclusion is replaced with
this issue's finding.

**Rejected: maintaining the set live.**
`_entity_introduced_by_set_provisional_batch` already returns the exact set of
idents whose guess it asserted or moved, and both call sites in `_reverse_apply`
(`8003`, `8007`) discard it; streams interleave through one `_RoundRobinClaimer`
with single-threaded writes, so feeding that return into
`state.provisional_idents` would make the set live at zero read cost. Rejected
because it re-creates a cache-versus-DB coherence dependency, which is the exact
bug class being fixed here: any future provisional-write path that forgets to
update the set reintroduces the same silent false negative. Making per-ident
lineage reads cheap is #239's job, for every call site at once.

### 2. Repair — collapse multiplicity in the sweep

`_correction_sweep_apply` gains `pos_by_commit_ident: Optional[Dict[str, int]] =
None`. When it is supplied and an ident holds ≥2 `:introduced-by` values, keep
the **minimum-position** value, retract the rest in one `_retract`, and emit one
stderr line.

Earliest-by-position is the right survivor. The reverse stream's guess is a
*sighting* at some commit at or above the true introduction, and
`_entity_introduced_by_set_provisional_batch`'s existing monotonicity rule
already encodes this — a guess may only ever move earlier. The forward walk's
second mint lands at the true introduction, which is therefore the earlier of
the two.

**Placement: at the top of the per-ident loop**, immediately after
`introduced_by_values` is read and *before* the `_lineage_is_provisional`
branch. Cases 1, 2 and 3 then run unchanged against a single value. Repair
collapses multiplicity only; it never confirms. If the survivor is not
`commit_ident`, the entity stays provisional and is confirmed normally when the
sweep reaches that commit.

**Why positions are required.** A position-free rule — "if `commit_ident` is
among the values, keep it; the ascending sweep reaches the earliest first" —
needs no new parameter but never fires. The second mint happens in the *forward*
region, which Stage B does not sweep; the sweep meets these entities at
unrelated later commits, which is what the at-scale log lines show (the sweep
commit is neither value). `_reverse_apply` already builds this map at
`mcp_server.py:7896` from `commit_metadata`; the sweep does not receive it.

**Gating.** Repair is active only when `pos_by_commit_ident` is supplied, so
every existing caller and test keeps today's fail-safe behaviour until wired.
Callers holding `commit_metadata` — the 2d Stage B driver and
`_correction_sweep_claim_and_process` — pass it. A value absent from the map
sorts last rather than raising, so an unrecognised commit ident can never be
chosen over a known one and can never crash the sweep.

**Limitation, stated deliberately.** An entity whose values are all outside the
map, or which the sweep never visits, is not repaired. Repair is best-effort
healing for graphs already in the field, not a guarantee.

### 3. Observe — make ambiguity loud

Add `_entity_introduced_by_values_query(db, entity_ident) -> List[str]`.
`_entity_introduced_by_query` delegates to it, emits one stderr line when
`len > 1`, and returns `results[0][0]` as today, with the arbitrary ordering
documented rather than implied. The sweep's two inline `:introduced-by` queries
collapse onto the new helper.

It must **not** raise. Both walks call it during the run, before Stage B, so
raising on >1 would hard-fail ingestion on exactly the corrupted graphs the
repair exists to heal. It also cannot pick the earliest itself — it has no
`pos_by_commit_ident` — so position-based selection stays in the sweep.

## Testing

Real-backend only, per `docs/testing-conventions.md`.

`TestForwardApplyReconcilesProvisional.test_forward_apply_supersedes_a_provisional_guess`
(`tests/test_mcp_server.py:16594`) already covers this scenario and passes on
`master`. It hides the bug by calling `_preload_provisional_idents` *after* the
reverse stream has run (`:16619`), so its snapshot contains the guess.
Production preloads at run start, before Stream 2 writes anything. Correcting
that ordering is part of the fix.

RED before any implementation edit:

1. **Integration oracle** (new, Pattern 2, file-backed): the issue's synthetic
   repo — `mod.py` accumulating `born_0..born_N` plus long-lived functions
   edited every commit — through the real `_run_ingestion`. Asserts zero
   entities hold ≥2 live `:introduced-by`, and zero `left provisional` /
   `left unreconciled` lines. Reproduces at 6 entities on `master`.
2. **Prefilter drop**: `_ForwardWalkState` built with `provisional_idents=set()`
   — the honest fresh-ingest value — before the reverse stream runs. Exactly one
   `:introduced-by` survives, naming h0.
3. **Eviction still drains**: an ident in the snapshot but not provisional in
   the DB is still discarded. Guards the loop's changed iteration source; a
   regression here restores the retry-on-every-commit behaviour the `8345`
   comment describes.
4. **Repair**, in `TestCorrectionSweepApply`: two seeded values plus
   `pos_by_commit_ident` → minimum-position survives, the other is retracted,
   one log line. Plus three boundaries — `pos_by_commit_ident=None` leaves
   today's fail-safe skip untouched; a value absent from the map sorts last
   without raising; and collapse-then-case-1, where a survivor equal to
   `commit_ident` is confirmed within the same call.
5. **Loud query**: 2 values → one stderr line via `capsys`, returns a value,
   does not raise; 1 value → silent.
6. **Existing guards stay green**: `TestMultiStreamParityWithForwardOnly`,
   `TestReverseFillValidTimeParity`, `TestStageBCorrectionSweep`.

## Performance

The fix moves a graph point query onto every candidate ident of every "A"/"M"
file in the forward walk — the same per-ident read class #239 measures at 33.6%
of ingestion wall clock. The issue's claim that this is "a cost the `"R"` path
already pays" understates it: the `"R"` path runs only on renames.

`evals/at_scale/run_ingestion_benchmark.py` (~85 min) is run after the fix and
its entry appended to the benchmark log, with the delta against #236's
1,600.55 s recorded in the PR. **Correctness is not gated on the number** — the
measurement exists so #239 starts from a measured baseline rather than a guess,
and so a large regression is attributed here rather than discovered later.

Reclaiming the cost belongs to #239, whose central design problem is
maintaining a cache of a value written during the run by the very walks that
read it. Folding that into this PR would put a correctness fix behind an unsolved
design question.

## Scope

Not in scope: #231 (`_build_close_triples` never retracts `:introduced-by`),
#238 (valid-time preload bound), #239 (read-cost reduction). This fix must land
before phases 3–5 build further on Stage A/Stage B lineage.
