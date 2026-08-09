# Checkpoint Cadence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `_db_checkpoint`'s once-per-commit ingestion cadence with a duty-cycle budget whose cost fraction does not grow with graph size, and remove the duplicate Stage B checkpoint.

**Architecture:** A `_CheckpointPolicy` object holds a time budget. A `_db_checkpoint_gated` wrapper consults a module-global policy; when that global is `None` (everything outside ingestion) it is exactly today's `_db_checkpoint`, so non-ingestion paths cannot regress. `_run_ingestion` installs a policy for the duration of a run and clears it in a `finally`. Separately, Stage B's duplicate checkpoint is gated on `not lifecycle_only`, and the end-of-run checkpoint moves onto every terminal path.

**Tech Stack:** Python 3.14, `minigraf` FFI (Rust), pytest + pytest-asyncio, real-backend-only tests.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-07-db-checkpoint-cadence-design.md`. Read it before starting; it carries the measurements every decision here rests on.
- **Branch:** `perf-241-checkpoint-cadence`, already created, spec committed at `74ed3eb`. Work in place — a plain branch, not a worktree.
- **Real backend, always.** Never a `MagicMock` fake of `MiniGrafDb`. See `docs/testing-conventions.md`. Monkeypatching *our own* `_db_checkpoint`/`_db_checkpoint_gated` wrappers is established practice (`tests/test_mcp_server.py:876`, `:990`, `:3004`) and is not the thing that rule forbids.
- **Ablation-proven tests.** Every regression test in this plan names the counterfactual that must fail without the fix. Run that counterfactual and confirm the test fails before considering the task done. A test that passes against both the old and new code is not a regression test.
- **`checkpoint()` is not a durability boundary.** Writes are crash-recoverable via `<graph>.wal`; the handle compacts on clean close. Nothing in this plan may be justified on "so a crash loses at most one commit" grounds — that premise is false and measured false.
- **Do not write closing keywords for any issue** (`Closes #241`, `Fixes #241`, …) in *any* commit message or PR body on this branch. #241 stays open until the at-scale acceptance run in Task 6 lands. A negated form ("does not close #241") still auto-closes — do not write the issue number next to any such word at all.
- **`mcp` stays pinned `<2.0.0`.** Do not touch that pin.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `mcp_server.py` | `_CheckpointPolicy`, `_db_checkpoint_gated`, the module global, call-site changes, policy lifecycle in `_run_ingestion` | 1–4 |
| `tests/test_mcp_server.py` | Policy math, gate behaviour, dedup, terminal-path guarantees | 1–4 |
| `evals/at_scale/probe_checkpoint_cost.py` | Committed probe: checkpoint scaling vs graph size, and the WAL/durability experiment | 5 |
| `evals/at_scale/results/241-checkpoint-cost.json` | Recorded probe output | 5 |
| `evals/at_scale/profile_forward_reconcile_attribution.py` | `--checkpoint-mode` flag so ablation legs stop needing patched worktrees | 5 |
| `evals/at_scale/benchmark.md` | Amend the `20260803T095104Z` entry; add the acceptance entry | 6 |
| `CLAUDE.md` | Document `MINIGRAF_INGEST_CHECKPOINT_DUTY` | 4 |

`SKILL.md` needs **no** change. Its only checkpoint reference (`SKILL.md:834`) describes the interactive `minigraf_transact` path, which stays on the ungated `_db_checkpoint`. Confirm this is still true at Task 4 rather than assuming it.

---

### Task 1: `_CheckpointPolicy` and the gated wrapper, wired but inert

Introduces the mechanism and routes ingestion through it, with the policy global left `None` so behaviour is bit-identical to today. This isolates "did the plumbing change anything?" from "is the budget correct?", which are different review questions.

**Files:**
- Modify: `mcp_server.py` — add after `_db_checkpoint` (currently `:3291-3294`); swap 5 call sites
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_db_checkpoint(db)`, existing.
- Produces:
  - `_CheckpointPolicy(duty: float, clock: Callable[[], float] = time.monotonic)` with `.maybe(db) -> bool`, `.force(db) -> None`, and counters `.checkpoints: int`, `.suppressed: int`
  - `_db_checkpoint_gated(db) -> bool`
  - module global `_ingest_checkpoint_policy: Optional[_CheckpointPolicy]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class _FakeClock:
    """Deterministic monotonic clock. Tests advance it explicitly, including
    from inside the patched _db_checkpoint, so a checkpoint's measured
    duration is exactly what the test says it is."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class TestCheckpointPolicy:
    """#241: the budget that replaces the once-per-commit cadence.

    Patches mcp_server._db_checkpoint (our own wrapper, not MiniGrafDb) so a
    checkpoint's cost is exact and the scheduling arithmetic is testable
    without a multi-hundred-MB graph. real_db supplies a genuine handle so
    the call still goes somewhere real.
    """

    def _policy(self, monkeypatch, clock, duty, cost):
        import mcp_server
        monkeypatch.setattr(
            mcp_server, "_db_checkpoint", lambda db: clock.advance(cost)
        )
        return mcp_server._CheckpointPolicy(duty, clock=clock)

    def test_first_call_checkpoints_and_seeds_the_duration(self, real_db, monkeypatch):
        clock = _FakeClock()
        policy = self._policy(monkeypatch, clock, duty=0.05, cost=0.1)
        assert policy.maybe(real_db) is True
        assert policy.checkpoints == 1
        assert policy.suppressed == 0

    def test_suppresses_until_the_budget_has_elapsed(self, real_db, monkeypatch):
        clock = _FakeClock()
        policy = self._policy(monkeypatch, clock, duty=0.05, cost=0.1)
        policy.maybe(real_db)                       # seeds d = 0.1s
        # duty 0.05 -> wait = 0.1 * (1/0.05 - 1) = 1.9s
        clock.advance(1.8)
        assert policy.maybe(real_db) is False
        assert policy.suppressed == 1
        clock.advance(0.2)                          # now 2.0s > 1.9s
        assert policy.maybe(real_db) is True
        assert policy.checkpoints == 2

    def test_wait_scales_with_checkpoint_cost(self, real_db, monkeypatch):
        """The graph-size-invariance property: a 10x more expensive
        checkpoint must buy a 10x longer suppression window, so the FRACTION
        of wall clock spent checkpointing stays fixed as the graph grows."""
        clock = _FakeClock()
        policy = self._policy(monkeypatch, clock, duty=0.05, cost=1.0)
        policy.maybe(real_db)                       # seeds d = 1.0s
        clock.advance(18.9)                         # < 1.0 * 19
        assert policy.maybe(real_db) is False
        clock.advance(0.2)                          # > 19s
        assert policy.maybe(real_db) is True

    def test_duty_bounds_the_checkpoint_fraction(self, real_db, monkeypatch):
        """Drive a simulated run and assert the realised duty honours the
        budget. This is the property the whole design exists to provide."""
        clock = _FakeClock()
        cost, duty = 0.1, 0.05
        policy = self._policy(monkeypatch, clock, duty=duty, cost=cost)
        for _ in range(2000):
            policy.maybe(real_db)
            clock.advance(0.05)                     # simulated per-commit work
        spent = policy.checkpoints * cost
        assert spent / clock.t <= duty * 1.05, (
            f"realised duty {spent / clock.t:.4f} exceeds budget {duty}"
        )
        assert policy.suppressed > 0, "budget never suppressed anything"

    def test_rejects_an_out_of_range_duty(self):
        import mcp_server
        for bad in (0.0, -0.1, 1.5):
            with pytest.raises(ValueError):
                mcp_server._CheckpointPolicy(bad)


class TestDbCheckpointGated:
    def test_checkpoints_every_call_with_no_policy_installed(self, real_db, monkeypatch):
        """Everything outside ingestion runs with _ingest_checkpoint_policy
        None, and must be indistinguishable from calling _db_checkpoint
        directly (#241)."""
        import mcp_server
        assert mcp_server._ingest_checkpoint_policy is None
        calls = []
        monkeypatch.setattr(mcp_server, "_db_checkpoint", lambda db: calls.append(db))
        for _ in range(5):
            assert mcp_server._db_checkpoint_gated(real_db) is True
        assert len(calls) == 5
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "CheckpointPolicy or DbCheckpointGated" -v`
Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_CheckpointPolicy'`

- [ ] **Step 3: Implement the policy and the wrapper**

In `mcp_server.py`, immediately after `_db_checkpoint` (`:3291-3294`). Check `Callable` is in the `typing` import line at the top of the file and add it if not.

```python
_ingest_checkpoint_policy: Optional["_CheckpointPolicy"] = None


class _CheckpointPolicy:
    """Decides when an ingestion write batch is compacted to disk.

    db.checkpoint() is O(graph size) WAL compaction that is FLAT in dirty
    bytes -- measured at ~4.9 ms/MB, a checkpoint after one fact costing the
    same as one after 5,000 (#241). Running it once per commit therefore
    costs N_commits x avg_graph_size, super-linear in history length, and was
    ~51% of at-scale ingestion wall clock.

    It is NOT a durability boundary. minigraf appends every transact to
    <graph>.wal, and writes survive a hard kill with no checkpoint at all;
    the handle also compacts on clean close. Deferring a checkpoint trades
    REOPEN LATENCY (~45 ms per MB of outstanding WAL, paid once by the next
    process to open the graph), never data integrity.

    The gate holds checkpointing to `duty` of wall clock: after a checkpoint
    costing d seconds the next is suppressed until d * (1/duty - 1) seconds
    have passed, since d / (d + W) <= duty exactly when W >= d * (1/duty - 1).
    Because the wait scales with d, the FRACTION stays fixed as the graph
    grows -- that is what removes the super-linear term rather than dividing
    it by a constant. Same self-scaling shape as #242's ingestion poller,
    which held 8.56% duty on CI against 8.65% locally on 23%-slower hardware.

    Thread confinement: every ingestion checkpoint site runs on
    _run_ingestion's single-worker write_executor, so this object's mutable
    state is confined to one thread and needs no lock of its own beyond the
    _db_native_lock that _db_checkpoint already takes. Do not call it from
    another thread without adding one.
    """

    def __init__(self, duty: float, clock: "Callable[[], float]" = time.monotonic) -> None:
        if not 0.0 < duty <= 1.0:
            raise ValueError(f"checkpoint duty must be in (0, 1], got {duty!r}")
        self._duty = duty
        self._clock = clock
        self._last_duration: Optional[float] = None
        self._last_finished_at = 0.0
        self.checkpoints = 0
        self.suppressed = 0

    def _budget_elapsed(self) -> bool:
        if self._last_duration is None:
            return True  # nothing measured yet; checkpoint once to seed d
        wait = self._last_duration * (1.0 / self._duty - 1.0)
        return self._clock() - self._last_finished_at >= wait

    def maybe(self, db: Any) -> bool:
        """Checkpoint if the budget allows. Returns whether it did."""
        if not self._budget_elapsed():
            self.suppressed += 1
            return False
        self.force(db)
        return True

    def force(self, db: Any) -> None:
        """Checkpoint regardless of budget, and re-measure d."""
        started = self._clock()
        _db_checkpoint(db)
        finished = self._clock()
        self._last_duration = finished - started
        self._last_finished_at = finished
        self.checkpoints += 1


def _db_checkpoint_gated(db: Any) -> bool:
    """Checkpoint unless the active ingestion policy says the budget is spent.

    With no ingestion in flight the policy is None and this is exactly
    _db_checkpoint(db), so the interactive write path is unchanged (#241).
    Returns whether a checkpoint actually ran.
    """
    policy = _ingest_checkpoint_policy
    if policy is None:
        _db_checkpoint(db)
        return True
    return policy.maybe(db)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k "CheckpointPolicy or DbCheckpointGated" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Swap the five ingestion call sites to the gated wrapper**

Leave the three non-ingestion sites alone: `_checkpoint_after_write` (`:3308`), `minigraf_audit` (`:3835`), `_transact_extracted_facts` (`:6429`).

Change these five from `_db_checkpoint(db)` to `_db_checkpoint_gated(db)`:

| location | context to match |
|---|---|
| `_reverse_apply` tail (`:8464`) | follows `_frontier_persist_claim(db, linearization, pos, from_low=False, ...)` |
| `_correction_sweep_apply` (`:9508`) | inside `if update_watermark:`, follows `_correction_sweep_through_update(...)` |
| `_forward_apply` tail (`:9114`) | immediately before `_commit_index_writer_safe(index_con)` |
| sweep loop (`:10138`) | `await loop.run_in_executor(write_executor, _db_checkpoint, db)` after `_correction_sweep_through_update` |
| lineage fold (`:10169`) | `await loop.run_in_executor(write_executor, _db_checkpoint, db)` inside `if should_fold:` |

The two executor submissions pass the function by reference — change the argument to `_db_checkpoint_gated`, e.g.
`await loop.run_in_executor(write_executor, _db_checkpoint_gated, db)`.

**Do not** change the final checkpoint at `:10184` (inside `if completed_all:`). Task 3 handles it.

- [ ] **Step 6: Add the no-behaviour-change regression test**

```python
class TestGatedWrapperIsInertWithoutAPolicy:
    @pytest.mark.asyncio
    async def test_ingestion_checkpoint_count_is_unchanged(self, git_repo, monkeypatch):
        """Task 1 is pure plumbing: routing ingestion through
        _db_checkpoint_gated with no policy installed must not change how
        many checkpoints a run performs (#241)."""
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
            "error_at": None, "phase": None,
        }
        calls = []
        real = mcp_server._db_checkpoint
        monkeypatch.setattr(
            mcp_server, "_db_checkpoint",
            lambda db: (calls.append(1), real(db))[1],
        )
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete"
        assert mcp_server._ingest_checkpoint_policy is None
        assert len(calls) > 0, "a completed ingestion must checkpoint at least once"
```

Record the observed count in the assertion message when you run it, so Tasks 2 and 4 have a concrete baseline to compare against.

- [ ] **Step 7: Run the full ingestion test suite**

Run: `.venv/bin/pytest tests/test_mcp_server.py -x -q`
Expected: PASS, no new failures. Note the pre-existing pass/fail count so later tasks can tell regressions from pre-existing state.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add a checkpoint duty-cycle policy and route ingestion through it

Introduces _CheckpointPolicy and the _db_checkpoint_gated wrapper, and moves
ingestion's five checkpoint sites onto the wrapper. The policy global stays
None, so this commit changes no behaviour -- with no policy installed the
wrapper is exactly _db_checkpoint, which the added test pins by counting
checkpoints across a real ingestion.

Splitting the plumbing from the budget keeps 'did routing change anything'
separate from 'is the budget right' at review. Refs #241."
```

---

### Task 2: Remove Stage B's duplicate checkpoint

`_forward_apply` checkpoints at its tail even on the `lifecycle_only=True` pass, and `_run_ingestion`'s sweep loop checkpoints again immediately afterwards. Measured worth: **−19.1 s of 185.7 s (−10.3%)** on a 330-commit slice, at 92% pass-through because Stage B's loop is serial and has nothing to overlap with.

**Files:**
- Modify: `mcp_server.py:9114`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_db_checkpoint_gated` from Task 1.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

```python
class TestStageBDoesNotDoubleCheckpoint:
    """#241: the sweep loop checkpoints after _correction_sweep_through_update,
    so _forward_apply's own tail checkpoint on the lifecycle_only pass is a
    pure duplicate -- it fires BEFORE the watermark advances, so a crash
    between the two re-processes the commit either way."""

    @pytest.mark.asyncio
    async def test_lifecycle_only_pass_issues_no_checkpoint(self, git_repo, monkeypatch):
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
            "error_at": None, "phase": None,
        }
        seen = []
        real = mcp_server._db_checkpoint_gated

        def spy(db):
            frame = sys._getframe(1)
            seen.append((frame.f_code.co_name, frame.f_locals.get("lifecycle_only")))
            return real(db)

        monkeypatch.setattr(mcp_server, "_db_checkpoint_gated", spy)
        await mcp_server._run_ingestion(str(git_repo), "HEAD")

        assert ("_forward_apply", True) not in seen, (
            "_forward_apply must not checkpoint on the lifecycle_only pass -- "
            f"the sweep loop already does. Saw: {seen}"
        )
        assert ("_forward_apply", False) in seen, (
            "the Stage A forward pass must still checkpoint; a gate that "
            f"suppressed both would pass the assertion above vacuously. Saw: {seen}"
        )
```

`sys` is already imported in `tests/test_mcp_server.py`; confirm before relying on it.

**If `git_repo`'s two commits do not reach Stage B**, the first assertion passes vacuously. Verify by printing `seen` on a run against unmodified code and confirming `("_forward_apply", True)` is present. If it is not, build a larger fixture (8–10 commits with adds, modifies, and a delete) in the same style as `git_repo` and use that instead — do not weaken the test.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k lifecycle_only_pass_issues_no_checkpoint -v`
Expected: FAIL — `("_forward_apply", True)` is present in `seen`.

This failure **is** the ablation: it demonstrates the duplicate exists before the fix. Record the observed `seen` list in the task report.

- [ ] **Step 3: Gate the checkpoint**

At `mcp_server.py:9114`, change:

```python
        _lineage_confirmed_through_update(db, commit_hash, commit_ts_iso, index_con=index_con)
    _db_checkpoint_gated(db)
    _commit_index_writer_safe(index_con)
```

to:

```python
        _lineage_confirmed_through_update(db, commit_hash, commit_ts_iso, index_con=index_con)
    # Stage B's lifecycle pass is followed immediately by
    # _correction_sweep_through_update and a checkpoint in _run_ingestion's
    # sweep loop, so checkpointing here would be a pure duplicate -- and it
    # fires BEFORE the watermark advances, so a crash between the two
    # re-processes the commit either way. Measured at 25% of all ingestion
    # checkpoints and 10.3% of wall clock (#241).
    if not lifecycle_only:
        _db_checkpoint_gated(db)
    _commit_index_writer_safe(index_con)
```

`_commit_index_writer_safe` stays unconditional: the fact index is a derived cache (`fact_index.py:141`) with a self-healing `rebuild_index()`, and it is the only index commit in the sweep path.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k lifecycle_only_pass_issues_no_checkpoint -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/test_mcp_server.py -x -q`
Expected: PASS, matching Task 1's baseline count.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Stop double-checkpointing each swept commit in Stage B

_forward_apply checkpointed at its tail even on the lifecycle_only pass,
immediately before the sweep loop's own checkpoint. It fires before the
watermark advances, so a crash between the two re-processes the commit
either way -- the first of the pair buys nothing.

Measured at 165 of 662 checkpoints on a 330-commit slice and 19.1s of 185.7s
wall clock, at 92% pass-through: Stage B's loop is serial with no
next-commit prefetch, so unlike Stage A its checkpoints overlap with nothing.

Refs #241."
```

---

### Task 3: Guarantee a final checkpoint on every terminal path

`mcp_server.py:10184`'s unconditional checkpoint sits inside `if completed_all:`. An interrupted or errored run gets none. Harmless today (≤1 commit of WAL outstanding); not harmless once Task 4 lets the WAL grow.

**Files:**
- Modify: `mcp_server.py` — the `finally` that closes `index_con` and shuts the extraction pool (currently `:10195-10197`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_CheckpointPolicy.force`, `_db_checkpoint`, `_ensure_db_async`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

```python
class TestFinalCheckpointOnEveryTerminalPath:
    """#241: with a batched cadence an interrupted run can leave a whole
    run's WAL uncompacted, and the next process to open the graph pays the
    replay. The final checkpoint must therefore not be conditional on
    completed_all. There is an implicit backstop -- _db = None drops the
    handle and minigraf compacts on close -- but a durability-adjacent
    property must not rest on refcount timing.
    """

    def _fresh_progress(self):
        return {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
            "error_at": None, "phase": None,
        }

    def _count_checkpoints(self, monkeypatch):
        import mcp_server
        calls = []
        real = mcp_server._db_checkpoint
        monkeypatch.setattr(
            mcp_server, "_db_checkpoint",
            lambda db: (calls.append(1), real(db))[1],
        )
        return calls

    @pytest.mark.asyncio
    async def test_completed_run_checkpoints_at_the_end(self, git_repo, monkeypatch):
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = self._fresh_progress()
        calls = self._count_checkpoints(monkeypatch)
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete"
        assert len(calls) > 0

    @pytest.mark.asyncio
    async def test_shutdown_mid_run_still_checkpoints(self, git_repo, monkeypatch):
        """Mirrors TestRunIngestionShutdown's patched-sleep technique."""
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = self._fresh_progress()
        calls = self._count_checkpoints(monkeypatch)

        original_sleep = asyncio.sleep

        async def patched_sleep(t):
            mcp_server._shutdown_requested.set()
            await original_sleep(t)

        try:
            with patch("mcp_server.asyncio.sleep", patched_sleep):
                await mcp_server._run_ingestion(str(git_repo), "HEAD")
        finally:
            mcp_server._shutdown_requested.clear()

        assert mcp_server._ingest_progress["status"] == "stopped"
        assert len(calls) > 0, "an interrupted run must still compact its WAL"

    @pytest.mark.asyncio
    async def test_stage_b_failure_still_checkpoints(self, git_repo, monkeypatch):
        """A Stage B exception sets completed_all False and skips the
        if-completed_all block entirely."""
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = self._fresh_progress()
        calls = self._count_checkpoints(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("injected Stage B failure")

        monkeypatch.setattr(mcp_server, "_correction_sweep_apply", boom)
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert len(calls) > 0, "a failed run must still compact its WAL"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k FinalCheckpointOnEveryTerminalPath -v`
Expected: the `completed` case PASSES; the shutdown and Stage-B-failure cases may already pass because per-commit checkpoints happen throughout.

**This is the one place the plan's test cannot be ablation-proven as written**, because today's per-commit cadence makes `len(calls) > 0` true on every path. Strengthen it: instead of `len(calls) > 0`, assert that a checkpoint occurred **after the last write**. Capture `len(calls)` immediately before triggering shutdown/failure and assert it grew afterwards. If that proves impractical to observe from outside, install a `_CheckpointPolicy` with `duty` small enough to suppress everything mid-run (e.g. `1e-6`), so only a genuinely unconditional final checkpoint can make the count non-zero. Use that form — it is the honest ablation, and it also exercises Task 4's machinery.

- [ ] **Step 3: Move the final checkpoint onto the terminal path**

In the `finally` that currently reads:

```python
            finally:
                await loop.run_in_executor(write_executor, _close_index_writer_safe, index_con)
                await loop.run_in_executor(write_executor, executor.shutdown)
```

make it:

```python
            finally:
                # Compact the WAL on EVERY terminal path, not just
                # completed_all. Under the duty-cycle cadence an interrupted
                # run can leave a whole run's writes outstanding in
                # <graph>.wal, and the next process to open the graph pays
                # the replay (~45 ms/MB). Nothing is lost if this fails --
                # the WAL is already durable -- so a failure here must never
                # mask the real error that brought us into this finally
                # (same rule as _checkpoint_after_write, #176).
                try:
                    final_db = await _ensure_db_async()
                    await loop.run_in_executor(write_executor, _db_checkpoint, final_db)
                except Exception as e:
                    print(f"[_run_ingestion] final checkpoint failed: {e}", file=sys.stderr)
                finally:
                    _db = None
                await loop.run_in_executor(write_executor, _close_index_writer_safe, index_con)
                await loop.run_in_executor(write_executor, executor.shutdown)
```

Call `_db_checkpoint` directly, **not** `_db_checkpoint_gated` — this one must never be suppressed.

`_db` is assigned here, so confirm `_run_ingestion` already declares `global _db` (it assigns `_db = None` in several places, so it must). If the local name `final_db` shadows anything in scope, rename it.

The existing checkpoint at `:10184` inside `if completed_all:` becomes redundant. Leave it — it is harmless, and removing it widens the diff for no gain. Note the redundancy in a comment.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k FinalCheckpointOnEveryTerminalPath -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/test_mcp_server.py -x -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Compact the WAL on every terminal ingestion path

The final checkpoint sat inside 'if completed_all', so an interrupted or
errored run performed none. That is harmless under a per-commit cadence --
at most one commit of WAL is outstanding -- but under the duty-cycle cadence
an interrupted run can leave a whole run's writes in the WAL for the next
opener to replay.

There was an implicit backstop: _db = None drops the handle and minigraf
compacts on close. Relying on refcount timing for a durability-adjacent
property is the kind of implicit invariant this codebase has been bitten by,
so it is now explicit. A failure here cannot mask the error that brought us
into the finally.

Refs #241."
```

---

### Task 4: Activate the policy for the duration of a run

**Files:**
- Modify: `mcp_server.py` — `_run_ingestion` setup and outer `finally`
- Modify: `CLAUDE.md`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_CheckpointPolicy`, `_ingest_checkpoint_policy` from Task 1.
- Produces: env var `MINIGRAF_INGEST_CHECKPOINT_DUTY` (default `0.05`).

- [ ] **Step 1: Write the failing tests**

```python
class TestCheckpointPolicyLifecycle:
    @pytest.mark.asyncio
    async def test_policy_is_installed_during_and_cleared_after_a_run(
        self, git_repo, monkeypatch,
    ):
        """A stale budget must never gate a later interactive transact."""
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
            "error_at": None, "phase": None,
        }
        observed = []
        real = mcp_server._db_checkpoint_gated
        monkeypatch.setattr(
            mcp_server, "_db_checkpoint_gated",
            lambda db: (observed.append(mcp_server._ingest_checkpoint_policy), real(db))[1],
        )
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert observed, "no gated checkpoint ran"
        assert all(p is not None for p in observed), "policy missing mid-run"
        assert mcp_server._ingest_checkpoint_policy is None, "policy leaked past the run"

    @pytest.mark.asyncio
    async def test_policy_is_cleared_even_when_the_run_fails(self, git_repo, monkeypatch):
        import mcp_server
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None,
            "error_at": None, "phase": None,
        }

        def boom(*a, **k):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(mcp_server, "_forward_apply", boom)
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_checkpoint_policy is None

    def test_duty_is_read_from_the_environment(self, monkeypatch):
        import mcp_server
        monkeypatch.setenv("MINIGRAF_INGEST_CHECKPOINT_DUTY", "0.25")
        assert mcp_server._checkpoint_duty_from_env() == 0.25

    def test_invalid_duty_in_the_environment_falls_back_to_the_default(self, monkeypatch):
        """A typo in an env var must not crash or silently disable
        checkpointing."""
        import mcp_server
        for bad in ("nonsense", "0", "-1", "5"):
            monkeypatch.setenv("MINIGRAF_INGEST_CHECKPOINT_DUTY", bad)
            assert mcp_server._checkpoint_duty_from_env() == mcp_server._DEFAULT_CHECKPOINT_DUTY
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k CheckpointPolicyLifecycle -v`
Expected: FAIL — `_checkpoint_duty_from_env` undefined; policy is `None` mid-run.

- [ ] **Step 3: Add the env reader**

Next to `_CheckpointPolicy` in `mcp_server.py`:

```python
_DEFAULT_CHECKPOINT_DUTY = 0.05


def _checkpoint_duty_from_env() -> float:
    """Read MINIGRAF_INGEST_CHECKPOINT_DUTY, falling back to the default on
    anything unparseable or out of range. A typo must not crash ingestion or
    silently switch checkpointing off (#241)."""
    raw = os.environ.get("MINIGRAF_INGEST_CHECKPOINT_DUTY")
    if raw is None:
        return _DEFAULT_CHECKPOINT_DUTY
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[_run_ingestion] ignoring unparseable "
            f"MINIGRAF_INGEST_CHECKPOINT_DUTY={raw!r}; "
            f"using {_DEFAULT_CHECKPOINT_DUTY}",
            file=sys.stderr,
        )
        return _DEFAULT_CHECKPOINT_DUTY
    if not 0.0 < value <= 1.0:
        print(
            f"[_run_ingestion] MINIGRAF_INGEST_CHECKPOINT_DUTY={value} out of "
            f"(0, 1]; using {_DEFAULT_CHECKPOINT_DUTY}",
            file=sys.stderr,
        )
        return _DEFAULT_CHECKPOINT_DUTY
    return value
```

- [ ] **Step 4: Install and clear the policy**

In `_run_ingestion`, immediately after `write_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)`:

```python
        # Hold checkpointing to a fixed fraction of wall clock rather than
        # once per commit. See _CheckpointPolicy for why the per-commit
        # cadence was super-linear and why deferring is safe (#241).
        global _ingest_checkpoint_policy
        _ingest_checkpoint_policy = _CheckpointPolicy(_checkpoint_duty_from_env())
```

Add `_ingest_checkpoint_policy` to `_run_ingestion`'s existing `global` declaration if one already covers `_db`, rather than introducing a second `global` statement mid-function.

In the outer `finally` that currently reads `write_executor.shutdown(wait=True)`:

```python
        finally:
            # Never let a finished run's budget gate a later interactive
            # transact (#241).
            _ingest_checkpoint_policy = None
            write_executor.shutdown(wait=True)
```

Confirm this `finally` is reached on the failure path too — the module's outer `except Exception` sits below it, and its comment already states the executor is shut down by this point on every exit.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k CheckpointPolicyLifecycle -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest tests/test_mcp_server.py -x -q`
Expected: PASS

Task 1's `test_ingestion_checkpoint_count_is_unchanged` asserts the policy is `None` *after* a run, which stays true — but re-read it and confirm its checkpoint-count assertion is still meaningful now that a policy is active mid-run. If it now pins the wrong thing, retitle it and narrow it to the post-run `None` assertion rather than deleting it.

- [ ] **Step 7: Document the env var**

In `CLAUDE.md`, after the `MINIGRAF_INDEX_PATH` block:

```markdown
Ingestion checkpoint budget: `MINIGRAF_INGEST_CHECKPOINT_DUTY` (default `0.05`).

`db.checkpoint()` is full WAL-to-graph compaction, so it costs O(graph size)
regardless of how much was written since the last one. Ingestion holds it to
this fraction of wall clock instead of running it once per commit. Writes are
durable via `<graph>.graph.wal` without it; a larger WAL only slows the next
process that opens the graph. See #241.
```

Confirm `SKILL.md` still needs no change — its one checkpoint reference (`:834`) is about the interactive `minigraf_transact` path, which stays on the ungated `_db_checkpoint`.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py CLAUDE.md
git commit -m "Hold ingestion checkpointing to a duty-cycle budget

_run_ingestion now installs a _CheckpointPolicy for the duration of a run and
clears it in the outer finally, so checkpointing costs a fixed fraction of
wall clock instead of once per commit. Because the suppression window scales
with the last checkpoint's measured duration, that fraction stays fixed as
the graph grows -- which removes the super-linear term rather than dividing
it by a constant.

Tunable via MINIGRAF_INGEST_CHECKPOINT_DUTY, default 0.05, with an
unparseable or out-of-range value falling back to the default rather than
crashing or silently disabling checkpointing.

Refs #241."
```

---

### Task 5: Commit the probes, and settle uniform vs Stage-B-only by measurement

The spec's one deliberately open question. Stage A's checkpoints are nearly free (they hide behind the parse pool) *and* they keep the WAL short, so gating them may be all downside. The `every25`/`noop` legs both showed other DB work slowing (`_db_execute` 59.3 → 65.2 s, `_retract` 5.3 → 10.6 s) with no established mechanism.

**Files:**
- Create: `evals/at_scale/probe_checkpoint_cost.py`
- Create: `evals/at_scale/results/241-checkpoint-cost.json`
- Modify: `evals/at_scale/profile_forward_reconcile_attribution.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (the probe is standalone and read-only).
- Produces: a recorded decision that Task 6's benchmark entry cites.

- [ ] **Step 1: Commit the probe**

Write `evals/at_scale/probe_checkpoint_cost.py` combining the two experiments from the spec, following the header-docstring style of `bench_index_delete_cost.py` and the results convention of `probe_dep_preload_exposure.py` (#245):

1. **Scaling.** Grow one graph in 5,000-fact plateaus. At each, checkpoint after a 5,000-fact batch and again after a single fact; report ms and graph MB. Establishes cost is linear in size and flat in dirty bytes.
2. **Durability.** A child process transacts a checkpointed fact, an uncheckpointed fact, and an uncheckpointed `:ingestion/watermark`, then dies via `os._exit(9)`. The parent reopens and asserts all three recovered, reporting `g.graph` vs `g.graph.wal` sizes and reopen time.

Note in the docstring that raw `db.execute` needs the command form — `(query [:find ...])`, not a bare `[:find ...]`.

The probe must be self-isolating (its own tempdir; never touch `memory.graph`) and must write its results to `results/241-checkpoint-cost.json`.

- [ ] **Step 2: Run it and commit the recorded result**

Run: `.venv/bin/python evals/at_scale/probe_checkpoint_cost.py`
Expected: linear ms-vs-MB, the two checkpoint columns within ~10% of each other, and all three facts recovered after the hard kill.

```bash
git add evals/at_scale/probe_checkpoint_cost.py evals/at_scale/results/241-checkpoint-cost.json
git commit -m "Record the checkpoint cost and WAL durability probes for #241"
```

- [ ] **Step 3: Add `--checkpoint-mode` to the attribution harness**

The five ablation legs behind the spec were run from git worktrees patched by hand. Fold that into the harness so the legs are reproducible from the committed tree. The harness already wraps `_db_checkpoint` (`WRAPPED`, `:129-147`); add a flag with modes `normal` (default), `noop`, `every-N`, and `stage-b-only`.

`stage-b-only` needs to know the phase: read `m._ingest_progress["phase"]`, which the harness's `_watch_phase` already samples, and suppress only while it is `"sweeping"`.

Keep `normal` byte-identical in behaviour to today so existing entries stay comparable.

- [ ] **Step 4: Run the decision ablation**

Four legs against the same slice, `--no-profile`, on an otherwise idle machine. Run nothing else concurrently — probe contamination cost the original `normal` leg ~4%.

```bash
REF=82aa7e6c0353b52da5932f7ecbabdc7e42f6e418
for mode in normal normal-repeat stage-b-only duty; do ...; done
```

- `normal` and `normal-repeat` — establish today's spread before believing any delta (the observed spread was ±4%).
- `stage-b-only` — the hypothesis.
- `duty` — the shipped `MINIGRAF_INGEST_CHECKPOINT_DUTY=0.05` behaviour, run against the branch HEAD rather than via the flag.

Report wall clock, `_db_checkpoint` count and seconds, `_db_execute` seconds, `_retract` seconds, and the Stage A/Stage B split from the phase marks, for every leg.

- [ ] **Step 5: Decide, and record the decision**

If `stage-b-only` beats `duty` by more than the measured baseline spread, narrow the gate to Stage B and adjust Task 4's wiring. Otherwise keep the uniform gate.

Either way the spec must not be left claiming an open question it no longer has. Append a short "Resolved" section to `docs/superpowers/specs/2026-08-07-db-checkpoint-cadence-design.md` giving the numbers and the decision — following the precedent of the two revision sections in `2026-07-31-reverse-walk-write-amplification-design.md`, which correct their own spec's original claim.

**Do not** report a conclusion without the counterfactual leg. Three separate confident mechanisms on this codebase have been demolished by measurement — see the spec's closing note.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/profile_forward_reconcile_attribution.py \
        docs/superpowers/specs/2026-08-07-db-checkpoint-cadence-design.md
git commit -m "Settle the uniform-vs-Stage-B-only gate with a four-leg ablation

Adds --checkpoint-mode to the attribution harness so the legs are
reproducible from the committed tree instead of hand-patched worktrees, and
records the decision in the spec against a measured baseline spread.

Refs #241."
```

---

### Task 6: At-scale acceptance run and the `benchmark.md` reckoning

**Files:**
- Modify: `evals/at_scale/benchmark.md`

**Interfaces:**
- Consumes: everything above.
- Produces: the evidence #241 closes on.

- [ ] **Step 1: Run the at-scale benchmark on the branch**

Run: `.venv/bin/python evals/at_scale/run_ingestion_benchmark.py` (~40 min on current master; expect meaningfully less).

`run_ingestion_benchmark.py` is trustworthy again as of #242 — the poller runs on a dedicated executor and holds ~8.6% duty. Entries dated before 2026-08-07 carry the old poller's overhead, so compare against the post-#242 run (CI 31182651935: 629 commits, 2323.0 s) and **not** the 1,600.55 s figure.

Latency percentiles are **not** comparable across the #242 boundary — never read a p99 change across it as a speedup.

- [ ] **Step 2: Add a checkpoint-duty row**

The run must report realised checkpoint duty (`policy.checkpoints`, total checkpoint seconds, and the fraction of wall clock) alongside the existing poll-duty row. Add it to the benchmark's metric collection if it is not already surfaced. This is #241's acceptance criterion — an entry without it does not close the issue.

- [ ] **Step 3: Write the new entry**

Follow the existing entry format. State the comparison baseline explicitly, and record the realised duty against the 0.05 budget.

- [ ] **Step 4: Amend the `20260803T095104Z` entry**

It attributes 33.6% to `_db_execute (query)`, leaves 28.3% unattributed to "process-pool `_extract_commit`, orchestration, thread hops", and never names `_db_checkpoint` — which the ablation puts at ~51% of wall clock.

Do **not** silently rewrite the numbers. Append a correction note, in the style the spec's own revision sections use:

- The attribution came from an ad-hoc harness in `.superpowers/sdd/2026-08-03-fact-index-delete-by-rowid/`, which no longer exists; no committed profiler of that era ran cProfile at all. It cannot be reconciled from surviving artifacts.
- Its `_db_execute` figure predates #242 and carries the old poller's query overhead.
- `_db_checkpoint` was never named and is ~51% of wall clock, so the 28.3% "unattributed" bucket was far too small.
- **#239's 33.6% priority rests on this entry.** Flag that explicitly — re-examining #239's sequencing is a follow-up, not part of this branch.

- [ ] **Step 5: Commit and open the PR**

```bash
git add evals/at_scale/benchmark.md
git commit -m "Record the at-scale acceptance run for the checkpoint cadence

Also corrects the 20260803T095104Z entry, which never named _db_checkpoint
and left 28.3% unattributed. That entry's attribution came from a harness
that no longer exists and cannot be reconciled from surviving artifacts; its
_db_execute figure also predates #242 and carries the old poller's overhead.

Refs #241."
```

Then open the PR. **Re-scan every commit message on the branch for closing keywords before pushing, and again after any commit added later** — scanning once before the push has missed late commits on this project before. The PR body must not contain a closing keyword next to `#241` either, in any form including a negated one.

Branch protection requires an approving review on top of green CI. Ask before using `--admin` to bypass.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `_db_checkpoint_gated` + policy global, non-ingestion paths untouched | 1 |
| The duty budget and its arithmetic | 1, 4 |
| Thread confinement documented | 1 |
| Call-site table (5 gated sites) | 1 |
| `not lifecycle_only` dedup gate | 2 |
| Final checkpoint on every terminal path | 3 |
| Policy cleared in `finally` | 4 |
| `MINIGRAF_INGEST_CHECKPOINT_DUTY` | 4 |
| Test table, all four rows, ablation-proven | 1, 2, 3, 4 |
| Uniform vs Stage-B-only open question | 5 |
| Probes committed with recorded results | 5 |
| `benchmark.md` amendment | 6 |
| Acceptance = at-scale run with a checkpoint-duty row | 6 |
| Upstream minigraf issue | **not covered — see below** |
| Correct #241's `8464` line reference | **already done** (issue comment, 2026-08-07) |

Two spec follow-ups are deliberately **out of scope for this branch**: filing the upstream minigraf issue (`checkpoint()` has no incremental path) and re-examining #239's priority. Both are independent of this code and should be filed separately rather than widening the diff.

**Placeholder scan:** every code step carries real code; every test step carries real assertions. The one soft spot is Task 3 Step 2, where the naive assertion is not ablation-provable — the step says so explicitly and specifies the stronger form to use instead, rather than leaving it to judgement.

**Type consistency:** `_CheckpointPolicy(duty, clock=…)`, `.maybe(db) -> bool`, `.force(db) -> None`, `.checkpoints`, `.suppressed`, `_db_checkpoint_gated(db) -> bool`, `_ingest_checkpoint_policy`, `_checkpoint_duty_from_env() -> float`, `_DEFAULT_CHECKPOINT_DUTY` — used identically in Tasks 1, 3, 4 and in every test.

**Known risk carried into execution:** Task 2's test passes vacuously if the two-commit `git_repo` fixture never reaches Stage B. The step requires verifying `("_forward_apply", True)` actually appears against unmodified code, and building a larger fixture if it does not. Do not skip that check — a vacuous test here would pin nothing while looking like coverage.
