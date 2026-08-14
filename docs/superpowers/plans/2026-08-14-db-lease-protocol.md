# DB Lease Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `mcp_server.py`'s `_db = None` "release the graph file lock" idiom with a refcounted lease manager whose release genuinely releases, and stop `_run_ingestion` leaking its `write_executor` on two error paths.

**Architecture:** A single `_DbLeaseManager` owns the process's one `MiniGrafDb` handle, its bound path, an integer lease count, and a `weakref` to the last released handle. Callers hold `with db_lease() as db:` / `async with db_lease_async() as db:`; nested acquisitions reuse the live handle, and the handle is dropped only when the count reaches zero. A leak detector fires at the next `0 → 1` acquire if the previously released handle is still alive. Because a handle can never outlive its leases, the mtime-based staleness subsystem becomes unreachable and is deleted.

**Tech Stack:** Python 3.11+, `minigraf>=1.2.3` (Rust-backed `MiniGrafDb` via pyo3), `asyncio`, `concurrent.futures`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-08-14-db-lease-protocol-design.md`

## Global Constraints

- **ALWAYS run Python via `.venv/bin/python`.** System `python` carries minigraf 1.1.1 against a `>=1.2.3` floor; it fakes 122 test failures and makes queries ~7x slower. Every `pytest` invocation below is `.venv/bin/python -m pytest`.
- **`pyproject.toml` sets `asyncio_mode = "auto"`.** An `async def test_*` needs no `@pytest.mark.asyncio` marker; 133 existing tests carry one anyway, harmlessly. New async tests in this plan omit it.
- **Tests use the real minigraf backend only** — no mocks or fakes for the DB. See `docs/testing-conventions.md`.
- **Performance/hygiene guards count operations or assert state transitions, never wall clock.** See `docs/testing-conventions.md`, "Performance guards count operations, never wall clock".
- **Every guard must be ablation-proven**: show the test failing against the pre-fix behaviour, not merely passing against the fixed one.
- **Single-handle invariant:** at most one live `MiniGrafDb` handle per process. A second open raises `Database is already open in this process`.
- **Closing-keyword discipline:** this branch closes **#255 and #250 only**. Both issue bodies reference #251, #253 and project-minigraf/minigraf#304. Use `Refs #NNN`, never `Closes/Fixes/Resolves`, in every commit below; the PR body carries the closing keywords once, at the end. Re-scan after **every** commit added later, and verify with `gh pr view --json closingIssuesReferences`.
- **Baseline suite:** 1334 passed, 1 xfailed. The 1 xfail is `TestPreloadKnownEntitiesDescriptionValueIsDateBounded` (#257's permanent guard) and must stay xfailed.
- **Branch:** `fix-255-250-db-lease-protocol`, already created off `master` (`7ee0cbf`). The design commit `adcbccd` is already on it.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `mcp_server.py` | Everything. The lease manager lives here, beside the lock helpers it uses. | Modify |
| `tests/test_mcp_server.py` | All unit/integration tests. | Modify |
| `hooks/prepare_hook.py` | Separate-process UserPromptSubmit hook that calls into `mcp_server`. | Modify |
| `evals/at_scale/run_ingestion_benchmark.py` | The harness that produced #255's evidence. | Modify |
| `evals/at_scale/probe_description_preload_exposure.py` | Reads `mcp_server._db` directly. | Modify |
| `evals/at_scale/probe_dep_preload_exposure.py` | Reads `mcp_server._db` directly. | Modify |
| `evals/at_scale/profile_forward_reconcile_attribution.py` | Sets `m._db = None`. | Modify |
| `tests/test_at_scale_dep_preload_probe.py` | Reads `mcp_server._db` directly. | Modify |
| `docs/testing-conventions.md` | Test conventions. | Modify (Task 9) |

**A structural alternative was considered and rejected:** extracting the manager and the lock-file helpers into a new `db_lease.py` module. `mcp_server.py` is over 11,000 lines and a split is defensible, but the manager needs `SESSION_RULES`, `_user_rules` and `_db_execute`, so the split needs a registration callback to avoid a circular import, and it would move `_is_lock_error` out from under `TestLockErrorRecognisesSameProcessOpen`'s feet. Net line count here is roughly neutral anyway — the manager adds ~150 lines and deletes ~170 (four open/retry functions plus the mtime subsystem). Keeping it in place also keeps the diff reviewable, which is why the no-re-indent option was chosen for Task 1.

---

### Task 1: Close the `write_executor` leak (#250)

Independent of everything else. Lands first so it is not buried under the lease diff.

**Files:**
- Modify: `mcp_server.py:10681` (creation), `mcp_server.py:11093` (inner shutdown), `mcp_server.py:11095-11107` (the false comment), `mcp_server.py:11108` (outer finally)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`, at the end of the file:

```python
class TestWriteExecutorIsShutDownOnEarlyFailure:
    """#250: write_executor is created above the try whose finally shuts it
    down, with two awaited calls in the gap.

    THE GUARD ASSERTS SHUTDOWN STATE, NOT THREAD LIVENESS, and the distinction
    is the finding that produced it. #250's body says a live thread "leaks for
    the lifetime of the process"; measured on this interpreter, it does not.
    CPython's ThreadPoolExecutor registers a weakref callback that wakes its
    worker once the executor becomes unreachable, so when _run_ingestion's
    frame dies the thread exits on its own.

    That cleanup is emergent, not designed. It disappears the moment anything
    retains the traceback -- which pins the frame holding the executor -- and
    that is the exact fifth shape PR #254 found here and had to fix with
    `e.__traceback__ = None`. It also never JOINS the worker, which
    shutdown(wait=True) does.

    So the defect axis is "was the executor shut down on this path", and that
    is what this asserts, per docs/testing-conventions.md: pin the defect axis,
    never a downstream symptom that something else happens to clean up.
    """

    @pytest.mark.parametrize("failing", ["_open_index_writer_safe", "_frontier_load"])
    def test_the_executor_is_shut_down_when_a_pre_try_call_raises(
        self, tmp_path, git_repo, monkeypatch, failing
    ):
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        mcp_server._db = None
        mcp_server._graph_path = ""

        created = []

        class _Recording(concurrent.futures.ThreadPoolExecutor):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                created.append(self)

        # mcp_server resolves concurrent.futures.ThreadPoolExecutor at call
        # time, so patching the attribute catches every executor the run
        # builds -- including the preload one, which uses `with` and must
        # therefore also come back shut down.
        monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _Recording)

        def boom(*a, **kw):
            raise RuntimeError(f"induced failure in {failing}")

        monkeypatch.setattr(mcp_server, failing, boom)

        asyncio.run(mcp_server._run_ingestion(str(git_repo), "master"))

        # _run_ingestion swallows the failure into _ingest_progress rather than
        # raising, so assert we actually exercised the intended path.
        assert mcp_server._ingest_progress["status"] == "error"
        assert "induced failure" in (mcp_server._ingest_progress["error"] or "")
        assert created, (
            "no ThreadPoolExecutor was constructed -- the run aborted before "
            "reaching write_executor, so this test proves nothing"
        )

        unclosed = [ex for ex in created if not ex._shutdown]
        assert not unclosed, (
            f"{len(unclosed)} of {len(created)} executor(s) were never shut "
            f"down after {failing} raised"
        )
        mcp_server._db = None
```

- [ ] **Step 2: Run it and confirm it FAILS (the ablation)**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestWriteExecutorIsShutDownOnEarlyFailure -v`

Expected: **both parametrisations FAIL** with `1 of N executor(s) were never shut down`. Record the exact output in the commit message.

If either PASSES here, stop and report — the test is not exercising the gap. The likely cause is that `git_repo` produced a repo whose ingestion aborts before line 10681, which the `assert created` line is there to distinguish from a genuine pass.

Do **not** substitute a thread-liveness assertion. `ThreadPoolExecutor._shutdown` is `False` while live and `True` after `shutdown()`, verified on this interpreter; a thread-liveness check passes before the fix and proves nothing.

- [ ] **Step 3: Hoist the name**

In `mcp_server.py`, immediately above the outer `try:` at line 10589 (after `_reset_introduced_by_ambiguity_log_budget()`), insert:

```python
    # Bound BEFORE the try so the outermost finally can shut it down no matter
    # where a failure lands, including the two awaited calls
    # (_open_index_writer_safe, _frontier_load) that sit above the inner try
    # (#250). Without this the finally would need an unbound-name guard.
    write_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
```

Change line 10681 from `write_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)` to keep the same assignment (the name is now pre-bound; no other edit needed there).

- [ ] **Step 4: Move the shutdown to the outermost finally**

Delete line 11093 (`            write_executor.shutdown(wait=True)`) from the inner `finally`.

Add to the function's outermost `finally` (the one beginning at line 11108), as its **last** statement:

```python
        # The single shutdown for this executor. It lives here, not in the
        # inner finally, because two awaited calls (_open_index_writer_safe,
        # _frontier_load) sit above the inner try and can raise in the gap --
        # #250. One cleanup writer for one resource.
        if write_executor is not None:
            write_executor.shutdown(wait=True)
```

- [ ] **Step 5: Replace the false comment**

Replace lines 11096-11102 (the comment inside `except Exception as e:` beginning "write_executor is already shut down by the inner finally") with:

```python
        # write_executor is shut down by the outermost finally below, which
        # covers every path into this handler -- including the two awaited
        # calls above the inner try (_open_index_writer_safe, _frontier_load).
        # The comment that used to sit here asserted the inner finally already
        # covered them; it did not, and that is #250.
```

- [ ] **Step 6: Run the test and the surrounding suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestWriteExecutorIsShutDownOnEarlyFailure -v`
Expected: both parametrisations PASS.

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -x -q -k "ingestion or Ingest"`
Expected: no new failures.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Shut down write_executor on the two pre-try failure paths

_run_ingestion created write_executor above the try whose finally shut it
down, with two awaited calls in the gap (_open_index_writer_safe and
_frontier_load). Either raising left it never shut down, and the comment
beside the outer handler asserted the opposite.

#250's body overstates the symptom, and the guard is aimed accordingly.
Measured on CPython 3.14.6: the worker thread does NOT leak for the process
lifetime -- ThreadPoolExecutor's weakref callback wakes it once the frame
holding the executor dies. That cleanup is emergent, not designed: it vanishes
the moment anything retains the traceback, which is the fifth shape PR #254
already had to fix here with e.__traceback__ = None, and it never joins the
worker the way shutdown(wait=True) does. So the test asserts shutdown STATE,
which fails before this change, rather than thread liveness, which does not.

The name is now bound above the outer try and a single guarded shutdown lives
in the outermost finally; the inner one is deleted, leaving one cleanup writer
for one resource. Pre-binding the name is what dissolves the unbound-name
problem rather than guarding around it.

Ablation: both parametrisations of the new test fail against the previous code
with 'write_executor leaked 1 thread(s)'.

Refs #250"
```

---

### Task 2: The lease manager core

Pure addition. Nothing is wired to it yet, so the suite stays green throughout.

**Files:**
- Modify: `mcp_server.py` (new imports; new class + module functions inserted after `_open_db_at_with_extended_retry`, i.e. after line 3300)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: existing `_is_lock_error(exc)`, `_stale_lock_holder_pid(exc)`, `_clear_stale_lock(path, pid)`, `SESSION_RULES`, `_user_rules`, `_db_execute(db, datalog)`, `_get_graph_path()`, `_LOCK_RETRY_MAX`, `_LOCK_RETRY_BASE`, `_INGEST_LOCK_RETRY_BASE`, `_INGEST_LOCK_RETRY_CAP`, `_INGEST_LOCK_RETRY_BUDGET`.
- **Does NOT consume `_try_open_with_self_heal` or `_open_db_at`.** See `_open_for_lease` below — this was a defect in an earlier draft of this plan and it is the one thing most likely to be "helpfully" reverted.
- Produces, relied on by Tasks 3-9:
  - `_lease_manager: _DbLeaseManager` — module-level singleton
  - `_DbLeaseManager.lease_count -> int` (property)
  - `_DbLeaseManager.path -> str` (property)
  - `_DbLeaseManager.bind_path(path: str) -> None`
  - `_DbLeaseManager.try_acquire(path: str) -> Optional[MiniGrafDb]` — `None` means "another process holds the file lock, back off"
  - `_DbLeaseManager.release() -> None`
  - `db_lease(extended: bool = False) -> ContextManager[MiniGrafDb]`
  - `db_lease_async() -> AsyncContextManager[MiniGrafDb]`
  - `_reset_db_state() -> None`
  - `_graph_path_current() -> str`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class TestDbLeaseManager:
    """#255: `_db = None` releases only when it drops the LAST reference, so
    it is not a release at all. A lease refcounts, and the handle drops exactly
    when the count reaches zero -- which the sidecar .lock file makes directly
    observable (minigraf's FileLock::drop removes it).
    """

    def test_nested_leases_open_exactly_one_handle(self, tmp_path, monkeypatch):
        """The reuse that makes nesting free: call_tool holds the outer lease,
        handle_minigraf_query nests inside it and must not open a second time."""
        import mcp_server
        from minigraf import MiniGrafDb

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()

        opens = []
        real_open = MiniGrafDb.open
        monkeypatch.setattr(
            MiniGrafDb, "open",
            staticmethod(lambda p: (opens.append(p), real_open(p))[1]),
        )

        with mcp_server.db_lease() as outer:
            with mcp_server.db_lease() as inner:
                assert inner is outer, "the nested lease must reuse the live handle"
                assert mcp_server._lease_manager.lease_count == 2
            assert mcp_server._lease_manager.lease_count == 1
        assert mcp_server._lease_manager.lease_count == 0
        assert len(opens) == 1, f"expected exactly one open, got {len(opens)}: {opens}"

    def test_release_actually_releases_the_file_lock(self, tmp_path, monkeypatch):
        """The inversion of the old
        test_release_idiom_does_not_drop_a_handle_others_still_hold: under the
        lease protocol the lock file MUST be gone once the last lease exits."""
        import mcp_server

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()

        with mcp_server.db_lease() as db:
            mcp_server._db_execute(db, "(query [:find ?e :where [?e :ident ?v]])")
            assert os.path.exists(graph + ".lock"), "lock must be held while leased"

        assert not os.path.exists(graph + ".lock"), (
            "the lock file survived the last lease -- the handle was not dropped, "
            "which is exactly the #255 failure the lease protocol exists to remove"
        )

    def test_a_second_path_while_leased_is_refused(self, tmp_path, monkeypatch):
        """_open_db_at(path, force=False) returned _db without ever comparing
        paths. Not reachable from today's callers; nothing stopped it becoming
        reachable."""
        import mcp_server

        graph_a = str(tmp_path / "a.graph")
        graph_b = str(tmp_path / "b.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph_a)
        mcp_server._reset_db_state()

        with mcp_server.db_lease():
            with pytest.raises(RuntimeError, match="lease requested for"):
                mcp_server._lease_manager.try_acquire(graph_b)

    def test_release_without_a_lease_is_an_error(self, tmp_path, monkeypatch):
        """An unbalanced release would silently drop a live handle."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        mcp_server._reset_db_state()
        with pytest.raises(RuntimeError, match="no outstanding lease"):
            mcp_server._lease_manager.release()

    async def test_async_lease_nests_under_a_sync_lease(self, tmp_path, monkeypatch):
        """call_tool acquires asynchronously and the sync handler nests inside
        it -- that nesting is what keeps the blocking backoff off the event
        loop (#99)."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        mcp_server._reset_db_state()

        async with mcp_server.db_lease_async() as outer:
            with mcp_server.db_lease() as inner:
                assert inner is outer
                assert mcp_server._lease_manager.lease_count == 2
        assert mcp_server._lease_manager.lease_count == 0
```

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestDbLeaseManager -v`
Expected: all FAIL with `AttributeError: module 'mcp_server' has no attribute '_reset_db_state'`.

- [ ] **Step 3: Add the two new imports**

In `mcp_server.py`, add to the stdlib import block (lines 8-28), in alphabetical position:

```python
import gc
import weakref
```

- [ ] **Step 4: Implement the manager**

Insert into `mcp_server.py` immediately after `_open_db_at_with_extended_retry` ends (after line 3300, before `async def _ensure_db_async`):

```python
def _open_for_lease(path: str) -> MiniGrafDb:
    """Open a handle FOR THE LEASE MANAGER, self-healing a stale lock.

    Deliberately does not go through _open_db_at / _try_open_with_self_heal,
    and the difference is the entire point. _open_db_at's contract is to
    PUBLISH the handle into the module global `_db` -- which is precisely the
    stray reference the lease protocol exists to remove. Routed through it,
    release() could never drop the last reference: the lease count would reach
    zero while `_db` still held the handle, so the lock file would survive
    every lease and the next open would be a second handle.

    The self-heal logic below mirrors _try_open_with_self_heal minus that
    publication. The duplication is deliberate and TEMPORARY: the task that
    deletes `_db` also deletes _open_db_at, _try_open_with_self_heal and the
    two retry wrappers, leaving this as the only opener.
    """
    try:
        return MiniGrafDb.open(path)
    except Exception as e:
        if not _is_lock_error(e):
            raise
        holder_pid = _stale_lock_holder_pid(e)
        if holder_pid is not None and _clear_stale_lock(path, holder_pid):
            return MiniGrafDb.open(path)
        raise


class _DbLeaseManager:
    """Owns this process's single MiniGrafDb handle and its lifetime (#255).

    `_db = None` was this module's "release the graph file lock" idiom. It is
    not one: it releases only when it drops the LAST reference, so any local
    `db` still on a stack keeps the handle -- and its lock -- alive while the
    global says otherwise. That is the #251/#253 mechanism, and since minigraf
    1.2.2 it surfaces as "Database is already open in this process" rather than
    as silent page-table corruption.

    Here the count is authoritative. The handle is opened at 0 -> 1 and dropped
    at 1 -> 0; every acquisition in between reuses it. `_db_native_lock` is a
    separate concern and is NOT folded in: it serializes CALLS INTO a handle,
    while this class governs the handle's LIFETIME.

    THIS IS NOT THE WEAKREF GUARD REJECTED IN #253. That one reused a handle
    whenever a weakref to it was still live, including handles whose backing
    file had been deleted -- it resurrected dead graphs and segfaulted the
    suite 3/3 runs. Reuse here requires count > 0: a caller is inside a `with`
    block right now, so the file cannot have been torn down under them. A live
    weakref at count == 0 is the opposite of a reuse candidate -- it is the
    leak signal (see _detect_leaked_handle).

    One open attempt runs under self._lock, and the caller backs off OUTSIDE
    it. Holding the lock across the attempt is what makes a same-process double
    open impossible: a second thread blocks, then finds count > 0 and joins.
    Sleeping under it would serialize every waiter behind one backoff budget.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handle: Optional[MiniGrafDb] = None
        self._path: str = ""
        self._count: int = 0
        self._prev_ref: Optional["weakref.ref"] = None
        # Set False only by a test that deliberately reconstructs a leak (see
        # the #255 interleaving ablation). Defaults to strict; flipping the
        # default would quietly turn the always-on detector into a test-only one.
        self.strict_leak_detection: bool = True

    @property
    def lease_count(self) -> int:
        with self._lock:
            return self._count

    @property
    def path(self) -> str:
        with self._lock:
            return self._path

    def bind_path(self, path: str) -> None:
        """Point the manager at a graph without opening it (open_db's job)."""
        with self._lock:
            if self._count > 0 and self._path and path != self._path:
                raise RuntimeError(
                    f"cannot bind {path!r}: {self._count} lease(s) outstanding "
                    f"on {self._path!r}"
                )
            self._path = path

    def try_acquire(self, path: str) -> Optional[MiniGrafDb]:
        """One acquisition attempt.

        Returns the leased handle, or None if the graph file lock is currently
        held by another PROCESS -- the caller backs off and retries. Raises for
        any non-lock error and for a path conflict.
        """
        with self._lock:
            if self._count > 0:
                if path != self._path:
                    raise RuntimeError(
                        f"lease requested for {path!r} while {self._count} "
                        f"lease(s) are outstanding on {self._path!r}"
                    )
                self._count += 1
                return self._handle

            self._detect_leaked_handle(path)
            try:
                handle = _open_for_lease(path)
            except Exception as e:
                if _is_lock_error(e):
                    return None
                raise

            # Rules are registered under the lock, before the count goes
            # positive: a thread joining at count > 0 must never observe a
            # handle whose session rules are half-registered.
            for rule in SESSION_RULES:
                _db_execute(handle, rule)
            for rule in _user_rules:
                _db_execute(handle, rule)

            self._handle = handle
            self._path = path
            self._count = 1
            self._prev_ref = None
            return handle

    def release(self) -> None:
        with self._lock:
            if self._count <= 0:
                raise RuntimeError(
                    "release() called with no outstanding lease -- an unbalanced "
                    "release would drop a handle another caller is still using"
                )
            self._count -= 1
            if self._count == 0:
                handle, self._handle = self._handle, None
                if handle is not None:
                    # The detector's input. If this weakref is still live at the
                    # next 0 -> 1 acquire, someone escaped their lease.
                    self._prev_ref = weakref.ref(handle)

    def reset(self) -> None:
        """Force the manager back to its initial state.

        For tests and for _run_ingestion's error path. Runs the leak detector
        first, so a test that leaks a handle and then resets is blamed at its
        own teardown rather than at its successor's first acquire.
        """
        with self._lock:
            self._detect_leaked_handle(self._path or "<reset>")
            self._handle = None
            self._count = 0
            self._prev_ref = None
            # The path must clear too, or a graph path set by one test leaks
            # into the next one that never binds its own -- nothing else
            # resets this manager between tests.
            self._path = ""


_lease_manager = _DbLeaseManager()


@contextlib.contextmanager
def db_lease(extended: bool = False):
    """Hold a lease on the graph handle for the duration of the block.

    Blocking backoff -- call this OFF the event loop, or from inside an
    already-held async lease (where the count is already positive and no open
    happens). extended=True selects the long time-budgeted backoff that
    _load_ingestion_preload_state needs to survive an orphan-process cleanup
    window (#106) instead of giving up in ~1.55s.
    """
    path = _lease_manager.path or _get_graph_path()
    handle = None
    if extended:
        deadline = time.monotonic() + _INGEST_LOCK_RETRY_BUDGET
        delay = _INGEST_LOCK_RETRY_BASE
        while True:
            handle = _lease_manager.try_acquire(path)
            if handle is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"could not acquire a lease on {path!r} within "
                    f"{_INGEST_LOCK_RETRY_BUDGET}s: the file lock is held by "
                    f"another process"
                )
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _INGEST_LOCK_RETRY_CAP)
    else:
        delay = _LOCK_RETRY_BASE
        for attempt in range(_LOCK_RETRY_MAX):
            handle = _lease_manager.try_acquire(path)
            if handle is not None:
                break
            if attempt < _LOCK_RETRY_MAX - 1:
                time.sleep(delay)
                delay *= 2
        if handle is None:
            raise RuntimeError(
                f"could not acquire a lease on {path!r} after "
                f"{_LOCK_RETRY_MAX} attempts: the file lock is held by another "
                f"process"
            )
    try:
        yield handle
    finally:
        handle = None  # drop this frame's reference before the count goes to 0
        _lease_manager.release()


@contextlib.asynccontextmanager
async def db_lease_async():
    """Hold a lease, backing off with asyncio.sleep instead of time.sleep.

    Await this from any event-loop coroutine (call_tool, _run_ingestion). A
    blocking sleep here would freeze the single-threaded loop for the whole
    retry budget, and worse, would prevent the very coroutine holding the lock
    from ever releasing it during the wait (#99).
    """
    path = _lease_manager.path or _get_graph_path()
    handle = None
    delay = _LOCK_RETRY_BASE
    for attempt in range(_LOCK_RETRY_MAX):
        handle = _lease_manager.try_acquire(path)
        if handle is not None:
            break
        if attempt < _LOCK_RETRY_MAX - 1:
            await asyncio.sleep(delay)
            delay *= 2
    if handle is None:
        raise RuntimeError(
            f"could not acquire a lease on {path!r} after {_LOCK_RETRY_MAX} "
            f"attempts: the file lock is held by another process"
        )
    try:
        yield handle
    finally:
        handle = None
        _lease_manager.release()


def _graph_path_current() -> str:
    """The bound graph path, falling back to the environment."""
    return _lease_manager.path or _get_graph_path()


def _reset_db_state() -> None:
    """Force the module's DB state back to its initial condition.

    Replaces the `mcp_server._db = None` idiom at every test and eval call
    site. Strictly stronger: it clears the lease COUNT too, so a test that
    leaks a lease cannot poison its successor -- with the bare global there was
    no way to reset the count at all.
    """
    _lease_manager.reset()
```

Note: `_detect_leaked_handle` is referenced above and is implemented in Task 3. Add this temporary stub inside the class now so Task 2's tests run, and replace its body in Task 3:

```python
    def _detect_leaked_handle(self, path: str) -> None:
        return  # implemented in Task 3
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestDbLeaseManager -v`
Expected: all 5 PASS.

- [ ] **Step 6: Confirm nothing regressed**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: 1334 passed, 1 xfailed. The manager is not wired to anything yet, so the count must be unchanged apart from the 5 new tests — i.e. **1339 passed, 1 xfailed**.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add the DB lease manager, not yet wired in

_DbLeaseManager owns the handle, the bound path and an integer lease count.
The handle opens at 0 -> 1 and drops at 1 -> 0; every acquisition between
reuses it, which is what makes call_tool's outer lease and a handler's inner
one cost a single open.

One open attempt runs under the manager lock with backoff outside it. Holding
the lock across the attempt is what makes a same-process double open
impossible: a second thread blocks, then finds count > 0 and joins.

Reuse requires count > 0, which is the difference from the weakref guard
rejected in #253 -- that one reused handles nobody was holding, including
handles whose backing file was gone.

Nothing calls this yet; the existing open path is untouched.

Refs #255"
```

---

### Task 3: The leak detector

**Files:**
- Modify: `mcp_server.py` (replace the `_detect_leaked_handle` stub; add `_describe_referrers` beside it)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_lease_manager`, `db_lease`, `_reset_db_state` from Task 2.
- Produces: `_DbLeaseManager._detect_leaked_handle(path) -> None`, `_describe_referrers(obj, limit=6) -> str`, and the `_lease_manager.strict_leak_detection` opt-out relied on by Task 9's ablation.

- [ ] **Step 1: Write the failing tests**

```python
class TestDbLeaseLeakDetector:
    """#255: the count reaching zero means the manager dropped ITS reference.
    The handle only actually dies if nobody kept a stray one -- and a stray
    reference is the entire bug. The detector turns a later, confusing
    "Database is already open in this process" into a diagnostic that names the
    variable still holding it.

    It cannot run at __exit__: the caller's `as db` name is still bound at that
    moment, so every release would look like a leak. It runs at the next
    0 -> 1 acquire and from _reset_db_state().
    """

    def test_a_handle_that_escaped_its_lease_is_reported(self, tmp_path, monkeypatch):
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        mcp_server._reset_db_state()

        with mcp_server.db_lease() as db:
            escaped = db          # exactly the #255 mistake
        assert mcp_server._lease_manager.lease_count == 0

        with pytest.raises(RuntimeError, match="DB lease leak") as exc_info:
            with mcp_server.db_lease():
                pass
        assert "escaped" in str(exc_info.value), (
            "the diagnostic must name the variable still holding the handle -- "
            "that naming is the whole value over minigraf's own lock error; "
            f"got: {exc_info.value}"
        )
        del escaped
        mcp_server._reset_db_state()

    def test_a_clean_release_is_not_reported(self, tmp_path, monkeypatch):
        """Positive control's counterpart: the detector must not fire on the
        normal path, or it would be noise and get switched off."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        mcp_server._reset_db_state()

        for _ in range(3):
            with mcp_server.db_lease() as db:
                mcp_server._db_execute(db, "(query [:find ?e :where [?e :ident ?v]])")
        assert mcp_server._lease_manager.lease_count == 0

    def test_reset_blames_the_leaking_test_not_its_successor(self, tmp_path, monkeypatch):
        """The detector fires one step late at acquire time. Running it from
        _reset_db_state() is what puts the blame on the test that leaked."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        mcp_server._reset_db_state()

        with mcp_server.db_lease() as db:
            escaped = db
        with pytest.raises(RuntimeError, match="DB lease leak"):
            mcp_server._reset_db_state()
        del escaped
        mcp_server._reset_db_state()

    def test_strict_can_be_switched_off_for_a_deliberate_ablation(
        self, tmp_path, monkeypatch, capsys
    ):
        """Task 9's ablation reconstructs a leak on purpose. It needs an
        opt-out -- an attribute, not an env var, so it cannot leak into an
        unrelated process -- and the opt-out must still print."""
        import mcp_server

        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(tmp_path / "t.graph"))
        mcp_server._reset_db_state()
        monkeypatch.setattr(mcp_server._lease_manager, "strict_leak_detection", False)

        with mcp_server.db_lease() as db:
            escaped = db
        with mcp_server.db_lease():
            pass                                   # must NOT raise
        assert "DB lease leak" in capsys.readouterr().err
        del escaped
        monkeypatch.setattr(mcp_server._lease_manager, "strict_leak_detection", True)
        mcp_server._reset_db_state()
```

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestDbLeaseLeakDetector -v`
Expected: the three raising tests FAIL (`DID NOT RAISE RuntimeError`) because the stub returns immediately; `test_a_clean_release_is_not_reported` PASSES already (that is fine — it is the counterpart control, not the positive one).

- [ ] **Step 3: Implement the detector**

Replace the `_detect_leaked_handle` stub from Task 2 with:

```python
    def _detect_leaked_handle(self, path: str) -> None:
        """Fire if the previously released handle is still alive.

        Called at 0 -> 1 and from reset(). A live weakref here means a caller
        kept its `db` past the end of its `with` block: the manager dropped its
        reference, the count says "released", and the file lock is still held.
        Left alone, that surfaces later and elsewhere as minigraf's "Database
        is already open in this process". Naming the holder is the whole point
        -- gc.get_referrers is what found the four holder sites in PR #254,
        after two attempts to reason it out from the source were both wrong.

        Must be called with self._lock held.
        """
        ref = self._prev_ref
        if ref is None:
            return
        stale = ref()
        self._prev_ref = None
        if stale is None:
            return  # released cleanly, which is the normal path
        holders = _describe_referrers(stale)
        del stale  # do not let this frame be one of the holders we report
        msg = (
            f"DB lease leak: the handle from the previous lease on {path!r} was "
            f"still alive at the next acquire. A caller kept a reference past "
            f"the end of its `with db_lease()` block, so the graph file lock "
            f"was never released. Still held by: {holders}"
        )
        if self.strict_leak_detection and os.environ.get("PYTEST_CURRENT_TEST"):
            raise RuntimeError(msg)
        print(f"[db_lease] {msg}", file=sys.stderr)
```

And add, immediately above `class _DbLeaseManager`:

```python
def _describe_referrers(obj: Any, limit: int = 6) -> str:
    """Name the places still referencing obj, for the lease-leak diagnostic.

    Reports the BINDING NAMES where it can (a frame's locals or a module's
    globals are plain dicts, so the name holding obj is recoverable), because
    "still held by: escaped" is actionable and "still held by: dict" is not.
    Best-effort by nature -- a reference held only from a C-level structure
    has no name to report.
    """
    found: List[str] = []
    for referrer in gc.get_referrers(obj):
        if isinstance(referrer, dict):
            names = [k for k, v in referrer.items() if v is obj]
            if names:
                found.append(", ".join(sorted(names)))
        elif hasattr(referrer, "f_code"):
            code = referrer.f_code
            found.append(f"{os.path.basename(code.co_filename)}:{code.co_name}")
        else:
            found.append(type(referrer).__name__)
        if len(found) >= limit:
            break
    return "; ".join(found) if found else "<no named holder found>"
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestDbLeaseLeakDetector -v`
Expected: all 4 PASS.

If `test_a_handle_that_escaped_its_lease_is_reported` fails on the `"escaped" in str(...)` assertion, the referrer is being reported as a bare frame rather than by name. Do not weaken the assertion — it is the positive control, and a detector that reports `<no named holder found>` for the commonest case is the "verification fails open" failure mode. Fix `_describe_referrers` instead.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: 1343 passed, 1 xfailed.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Detect handles that escape their lease, and name the holder

A lease count of zero means the manager dropped ITS reference; the handle only
dies if nobody kept a stray one, and a stray reference is the whole of #255.
The detector fires at the next 0 -> 1 acquire, and from _reset_db_state() so
that a leaking test is blamed at its own teardown rather than at its
successor's first acquire.

It cannot run at __exit__: the caller's `as db` name is still bound there, so
every release would look like a leak. That one-step-late blame is stated in
the docstring rather than papered over.

gc.get_referrers reports the binding NAME where one exists -- 'still held by:
escaped' is actionable where 'dict' is not. That is the technique that found
the four holder sites in PR #254 after reasoning from the source failed twice.

Raises under pytest, prints to stderr in production: a diagnostic must never
abort a live ingestion. strict_leak_detection exists for deliberate ablations
and defaults to on.

Refs #255"
```

---

### Task 4: Migrate every `_db` touch in tests and evals

Done **before** `_db` is removed, so each intermediate commit stays green. `_reset_db_state()` currently clears only the manager; the legacy `_db` global is still live and still cleared by the production code, so both mechanisms coexist for this task and the next three.

**Files:**
- Modify: `tests/test_mcp_server.py`, `tests/test_at_scale_dep_preload_probe.py`, `evals/at_scale/run_ingestion_benchmark.py`, `evals/at_scale/probe_description_preload_exposure.py`, `evals/at_scale/probe_dep_preload_exposure.py`, `evals/at_scale/profile_forward_reconcile_attribution.py`
- Modify: `mcp_server.py` (`_reset_db_state` also clears the legacy global, temporarily)

**Interfaces:**
- Consumes: `_reset_db_state()`, `_lease_manager` from Task 2.
- Produces: a test suite with zero direct `_db` writes, which Task 7 depends on before deleting the global.

- [ ] **Step 1: Make `_reset_db_state` clear the legacy global too**

In `mcp_server.py`, change `_reset_db_state` to:

```python
def _reset_db_state() -> None:
    """Force the module's DB state back to its initial condition. (docstring
    from Task 2 retained.)"""
    global _db
    # TRANSITIONAL (removed in the commit that deletes `_db`): the legacy
    # global and the manager coexist while call sites are converted, so a
    # reset must clear both or a stale global would outlive the lease it
    # shadowed.
    _db = None
    _lease_manager.reset()
```

- [ ] **Step 2: Mechanically migrate the assignment sites**

```bash
sed -i 's/^\(\s*\)mcp_server\._db = None$/\1mcp_server._reset_db_state()/' \
    tests/test_mcp_server.py tests/test_at_scale_dep_preload_probe.py \
    evals/at_scale/run_ingestion_benchmark.py \
    evals/at_scale/probe_description_preload_exposure.py \
    evals/at_scale/probe_dep_preload_exposure.py
sed -i 's/^\(\s*\)m\._db = None$/\1m._reset_db_state()/' \
    evals/at_scale/profile_forward_reconcile_attribution.py
```

Verify the count moved as expected:

```bash
grep -c "_reset_db_state()" tests/test_mcp_server.py   # expect 138
grep -rn "mcp_server\._db\b\|m\._db\b" tests/ evals/at_scale/*.py
```

The second command must now show only the **reads** handled in Steps 3 and 4.

- [ ] **Step 3: Convert the five `_db is None` assertions in `tests/test_mcp_server.py`**

These assert "the lock was released", which `_db is None` never actually proved. Replace each with the assertion that does prove it.

At line 2422:

```python
        assert mcp_server._lease_manager.lease_count == 0, (
            "every lease must be released after call_tool so prepare_hook can "
            "open the DB between turns"
        )
```

At line 11415, change the snapshot list to record the lease count instead:

```python
            db_none_snapshots.append(mcp_server._lease_manager.lease_count == 0)
```

At lines 12880 and 12901:

```python
        assert mcp_server._lease_manager.lease_count == 0
```

Line 22178 belongs to `test_release_idiom_does_not_drop_a_handle_others_still_hold`, which Task 7 replaces wholesale. Leave it untouched here.

- [ ] **Step 4: Convert the four handle reads in evals**

`evals/at_scale/probe_description_preload_exposure.py:559` — replace the
`mcp_server._db if mcp_server._db is not None else mcp_server.open_db(...)`
expression and its use at 580-581 with a lease around that whole region:

```python
        with mcp_server.db_lease() as db:
            raw = mcp_server._db_execute(
                db,
                # ... existing query argument, unchanged ...
            )
```

Apply the identical shape at:
- `evals/at_scale/probe_dep_preload_exposure.py:965`
- `tests/test_at_scale_dep_preload_probe.py:450`

Delete the now-dead `mcp_server.open_db(str(graph_path))` fallback in each — `db_lease()` opens on demand, which is what those expressions were hand-rolling.

- [ ] **Step 5: Add the grep guard test**

```python
class TestNoDirectDbGlobalAssignment:
    """#255: assigning to a module attribute that no longer exists is silently
    accepted by Python. Without this grep an unmigrated `mcp_server._db = None`
    would keep "working" -- doing nothing -- and the lease count it was meant
    to clear would leak into the next test.

    Scoped to tests/ and evals/ for now; mcp_server.py joins the scan in the
    commit that deletes the global.
    """

    def test_no_test_or_eval_assigns_the_db_global_directly(self):
        import subprocess
        repo = Path(__file__).resolve().parent.parent
        hits = subprocess.run(
            ["git", "grep", "-n", r"\._db\s*=", "--",
             "tests/", "evals/at_scale/"],
            cwd=repo, capture_output=True, text=True,
        ).stdout.strip()
        assert not hits, (
            "these sites still assign the DB global directly; they must call "
            f"mcp_server._reset_db_state() instead:\n{hits}"
        )

    def test_the_grep_itself_matches_something(self, tmp_path):
        """Positive control. A pattern that matches nothing reports 'all clear'
        forever; validate it against a known-positive before trusting a clean
        result."""
        import subprocess
        (tmp_path / "canary.py").write_text("mcp_server._db = None\n")
        hits = subprocess.run(
            ["grep", "-n", r"\._db\s*=", str(tmp_path / "canary.py")],
            capture_output=True, text=True,
        ).stdout.strip()
        assert hits, "the guard's pattern matches nothing -- it would fail open"
        assert "canary.py" in hits, hits
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: 1345 passed, 1 xfailed. Any failure here is a migration error, not a design problem — the production code is still on the old path.

Run the at-scale probe tests too, since they were edited:
`.venv/bin/python -m pytest tests/test_at_scale_dep_preload_probe.py tests/test_at_scale_description_preload_probe.py -q`

- [ ] **Step 7: Commit**

```bash
git add tests/ evals/ mcp_server.py
git commit -m "Migrate every test and eval off direct _db assignment

138 sites in tests/test_mcp_server.py plus a handful in evals/at_scale set
mcp_server._db = None as a reset. They now call _reset_db_state(), which
clears the lease COUNT as well as the slot -- strictly stronger, since with
the bare global a test that leaked a lease had no way to reset the count and
would poison its successor.

Five 'assert _db is None' sites become 'lease_count == 0'. _db is None never
proved the lock was released; that was the premise #255 exists to correct.

A grep guard pins tests/ and evals/, with a positive control: assigning to a
module attribute that no longer exists is silently accepted by Python, so
without the control this guard could match nothing and report all clear.

_reset_db_state clears the legacy global too, transitionally, so this commit
and the next three stay green while call sites convert.

Refs #255"
```

---

### Task 5: Convert the synchronous handlers and reshape `open_db`

**Files:**
- Modify: `mcp_server.py:3566`, `:3867`, `:3886`, `:3909`, `:3943`, `:6533`, `:6668`, `:6756`, `:11198` (the nine `get_db()` callers); `:3078` (`open_db`); `:3335-3357` (`get_db`, deleted)
- Modify: `hooks/prepare_hook.py:30-35`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `db_lease` from Task 2.
- Produces: `open_db(graph_path: Optional[str] = None) -> None` (was `-> MiniGrafDb`). `get_db` no longer exists.

- [ ] **Step 1: Write the failing test**

```python
class TestHandlersLeaseRatherThanHold:
    """#255: a handler that returns while still holding the handle is the leak.
    Every handler must have released by the time it returns, whether it
    succeeded or raised."""

    @pytest.mark.parametrize("call", [
        lambda m: m.handle_minigraf_query("[:find ?e :where [?e :ident ?v]]"),
        lambda m: m.handle_minigraf_query("[:find ?e :where"),          # malformed
        lambda m: m.handle_minigraf_transact('[[:decision/x :decision/description "d"]]', "r"),
        lambda m: m.handle_minigraf_audit(),
    ])
    def test_handler_releases_its_lease_before_returning(self, tmp_path, monkeypatch, call):
        import mcp_server

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)

        call(mcp_server)

        assert mcp_server._lease_manager.lease_count == 0
        assert not os.path.exists(graph + ".lock"), (
            "the handler returned still holding the graph file lock, so the "
            "prepare_hook subprocess cannot open the DB between turns"
        )

    def test_open_db_leaves_no_handle_open(self, tmp_path, monkeypatch):
        """open_db binds the path; it must not leave an unleased handle, which
        is the #255 bug in its purest form."""
        import mcp_server

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()

        assert mcp_server.open_db(graph) is None
        assert mcp_server._lease_manager.lease_count == 0
        assert mcp_server._lease_manager.path == graph
        assert not os.path.exists(graph + ".lock")

    def test_get_db_is_gone(self):
        """It returned an unleased handle by construction. Leaving it as a
        deprecated wrapper would leave the bug reachable."""
        import mcp_server
        assert not hasattr(mcp_server, "get_db")
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestHandlersLeaseRatherThanHold -v`
Expected: FAIL — `open_db` returns a handle, `get_db` still exists, and the lock file survives each handler call.

- [ ] **Step 3: Reshape `open_db`**

Replace `mcp_server.py:3078-3080` with:

```python
def open_db(graph_path: Optional[str] = None) -> None:
    """Point this module at a graph. Does NOT leave a handle open.

    Returns None by design: handing back a handle nobody holds a lease on is
    exactly the #255 bug -- the caller has no way to say when it is finished,
    so the release never happens. Callers that need a handle take a lease.
    """
    _lease_manager.bind_path(graph_path or _get_graph_path())
```

- [ ] **Step 4: Convert the nine `get_db()` callers**

Each follows one shape — take a lease around the region that uses `db`. Apply to `handle_minigraf_query` (3566), `handle_minigraf_transact` (3867), `handle_minigraf_retract` (3886), `handle_minigraf_rule` (3909), `handle_minigraf_audit` (3943), the `:ident` reader at 6533, `_transact_extracted_facts` (6756), and `handle_minigraf_ingest_status` (11198). For example, `handle_minigraf_query` becomes:

```python
def handle_minigraf_query(datalog: str) -> Dict[str, Any]:
    """Query the graph. Returns {ok, results} or {ok, error}."""
    with db_lease() as db:
        try:
            raw = _db_execute(db, f"(query {datalog})")
            return _parse_query_result(raw)
        # ... existing except clauses unchanged ...
```

The one non-uniform site is line 6668, where the handle is an argument rather than a local:

```python
            with db_lease() as db:
                if _count_commit_entities(db) > 0:
                    nav_nudge = _NAV_NUDGE
```

**Do not** widen any lease beyond the region that uses `db`. A lease held across code that does not need it re-creates the "hold the lock longer than necessary" problem that `_db = None` was introduced to solve.

- [ ] **Step 5: Delete `get_db`**

Delete `mcp_server.py:3335-3357` entirely. Its "reads the global exactly once" guarantee (#122) is now structural: the manager returns the handle from under its own lock, so there is no global for a background thread to race.

- [ ] **Step 6: Update the hook**

In `hooks/prepare_hook.py`, replace lines 30-35 with:

```python
            import mcp_server
            # No explicit open: handle_memory_prepare_turn takes its own lease,
            # which carries the same retry/backoff and stale-lock self-heal
            # get_db() used to provide, and releases when it returns so the
            # next turn's hook process can acquire the file lock (#255).
            context = mcp_server.handle_memory_prepare_turn(prompt)
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestHandlersLeaseRatherThanHold -v`
Expected: all 6 PASS.

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: 1351 passed, 1 xfailed.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py hooks/prepare_hook.py tests/test_mcp_server.py
git commit -m "Convert the synchronous handlers to leases; delete get_db

The nine get_db() callers now hold a lease only across the region that uses
the handle. get_db itself is deleted rather than deprecated: it returned an
unleased handle by construction, so leaving a wrapper would leave the bug
reachable. Its #122 exactly-once read is now structural -- the manager returns
the handle from under its own lock, so there is no global to race.

open_db returns None and only binds the path. Handing back a handle nobody
leases is #255 in its purest form: the caller has no way to say when it is
done, so the release never happens.

prepare_hook.py drops its explicit open. handle_memory_prepare_turn's own
lease carries the same retry, backoff and stale-lock self-heal, and releases
on return so the next turn's hook process can acquire the file lock.

New tests assert the lock FILE is gone after each handler returns, including
on the malformed-query path -- 'the global is None' never proved that.

Refs #255"
```

---

### Task 6: Convert `call_tool` and `handle_memory_finalize_turn`

**Files:**
- Modify: `mcp_server.py:11472-11538` (`call_tool`), `:7109-7111` (`handle_memory_finalize_turn`), `:3303-3332` (`_ensure_db_async`, deleted)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `db_lease_async` from Task 2.
- Produces: `_ensure_db_async` no longer exists. `_DB_LEASE_TOOLS: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

```python
class TestCallToolAcquiresOnlyForDbTools:
    """#255: call_tool's pre-acquire exists so the synchronous handler's own
    lease nests at count 1->2 and never runs blocking backoff on the event
    loop (#99). Converting nine `await _ensure_db_async()` calls into one
    wrapper is where the DB-opening tool SET silently changes -- pin it."""

    def test_report_issue_never_opens_the_graph(self, tmp_path, monkeypatch):
        import mcp_server
        from minigraf import MiniGrafDb

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)

        opens = []
        real_open = MiniGrafDb.open
        monkeypatch.setattr(
            MiniGrafDb, "open",
            staticmethod(lambda p: (opens.append(p), real_open(p))[1]),
        )
        asyncio.run(mcp_server.call_tool(
            "minigraf_report_issue",
            {"issue_type": "bug", "description": "d"},
        ))
        assert opens == [], (
            "minigraf_report_issue does not await _ensure_db_async today and "
            "must not acquire a lease either"
        )

    def test_ingest_status_acquires_only_when_not_running(self, tmp_path, monkeypatch):
        """The conditional that is easiest to lose in the conversion."""
        import mcp_server

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)

        seen = []
        real_try = mcp_server._lease_manager.try_acquire
        monkeypatch.setattr(
            mcp_server._lease_manager, "try_acquire",
            lambda p: (seen.append(p), real_try(p))[1],
        )

        mcp_server._ingest_progress["status"] = "running"
        asyncio.run(mcp_server.call_tool("minigraf_ingest_status", {}))
        assert seen == [], "a running ingest must not have its handle contended"

        mcp_server._ingest_progress["status"] = "idle"
        asyncio.run(mcp_server.call_tool("minigraf_ingest_status", {}))
        assert seen, "an idle ingest status must acquire, as it does today"

    def test_call_tool_releases_even_when_the_handler_raises(self, tmp_path, monkeypatch):
        import mcp_server

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)

        with pytest.raises(ValueError, match="Unknown tool"):
            asyncio.run(mcp_server.call_tool("no_such_tool", {}))
        assert mcp_server._lease_manager.lease_count == 0
        assert not os.path.exists(graph + ".lock")
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestCallToolAcquiresOnlyForDbTools -v`
Expected: `test_ingest_status_acquires_only_when_not_running` FAILS (`try_acquire` is not yet reached — `_ensure_db_async` is still in use). The other two may pass against the old code; they are regression pins for the conversion, and both must still pass afterwards.

- [ ] **Step 3: Declare the tool set**

Insert above `call_tool` in `mcp_server.py`:

```python
# Exactly the tools that await _ensure_db_async() today. call_tool pre-acquires
# for these and only these: the acquisition must be ASYNC so the synchronous
# handler's own lease nests at count 1->2 and never runs blocking backoff on
# the event-loop thread (#99). minigraf_report_issue and minigraf_ingest_git
# are absent because they touch no graph here; memory_finalize_turn is absent
# because it takes its own lease internally, conditional on the extraction
# strategy (see handle_memory_finalize_turn).
_DB_LEASE_TOOLS = frozenset({
    "minigraf_query", "minigraf_transact", "minigraf_retract", "minigraf_rule",
    "memory_prepare_turn", "minigraf_audit",
})
```

- [ ] **Step 4: Rewrite `call_tool`'s scaffolding**

Replace the `global _db` line, the nine `await _ensure_db_async()` calls, and the trailing `finally: _db = None` (lines 11474, 11477, 11482, 11487, 11492, 11506, 11515, 11530, 11535-11538) with a single conditional acquire wrapping the dispatch. The nine dispatch branches keep their bodies verbatim, minus their `await _ensure_db_async()` line:

```python
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    needs_db = name in _DB_LEASE_TOOLS or (
        name == "minigraf_ingest_status" and _ingest_progress["status"] != "running"
    )
    async with contextlib.AsyncExitStack() as stack:
        if needs_db:
            # Acquired here, asynchronously, purely so the synchronous handler
            # below nests at count 1->2 and never runs blocking backoff on the
            # event loop (#99). The lease ends when this block does, which is
            # what lets the prepare_hook subprocess open the DB between turns
            # -- the job `finally: _db = None` used to do, badly.
            await stack.enter_async_context(db_lease_async())

        if name == "minigraf_query":
            result = handle_minigraf_query(arguments["datalog"])
            return [TextContent(type="text", text=json.dumps(result))]
        # ... the remaining branches unchanged ...
        raise ValueError(f"Unknown tool: {name}")
```

- [ ] **Step 5: Convert `handle_memory_finalize_turn`**

At `mcp_server.py:7109-7111`, the `await _ensure_db_async()` guarded by the strategy check becomes a lease over the body that uses it. Replace:

```python
    strategy = os.environ.get("MINIGRAF_EXTRACTION_STRATEGY", "heuristic")
    if strategy in ("heuristic", "llm", "agent"):
        await _ensure_db_async()
```

with:

```python
    strategy = os.environ.get("MINIGRAF_EXTRACTION_STRATEGY", "heuristic")
    async with contextlib.AsyncExitStack() as stack:
        if strategy in ("heuristic", "llm", "agent"):
            await stack.enter_async_context(db_lease_async())
```

and indent the remainder of the function body into that block. This handler is the one call_tool does **not** pre-acquire for, so its lease is the only one — it must cover every `_transact_extracted_facts` call in all three strategy branches, including the LLM path's fallback to heuristic.

- [ ] **Step 6: Delete `_ensure_db_async`**

Delete `mcp_server.py:3303-3332`. Confirm no callers remain:

```bash
grep -rn "_ensure_db_async" mcp_server.py tests/ evals/ hooks/
```

Expected: no hits outside comments. Update any comment that still names it — `_load_ingestion_preload_state`'s docstring (around line 8422) mentions both `get_db()` and `_ensure_db_async()` and is rewritten in Task 7.

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestCallToolAcquiresOnlyForDbTools -v`
Expected: all 3 PASS.

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: 1354 passed, 1 xfailed.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Convert call_tool and memory_finalize_turn to leases

call_tool's nine 'await _ensure_db_async()' calls and its 'finally: _db =
None' collapse into one conditional AsyncExitStack. The acquisition stays
ASYNC deliberately: it is what lets the synchronous handler's own lease nest
at count 1->2 instead of running blocking backoff on the event-loop thread
(#99).

_DB_LEASE_TOOLS pins the set of tools that open the graph, because collapsing
nine awaits into one wrapper is exactly where that set changes silently. The
conditional on minigraf_ingest_status -- acquire only when a run is not
already going -- is carried across verbatim and now has its own test.

handle_memory_finalize_turn keeps its own lease rather than joining
_DB_LEASE_TOOLS: it acquires conditionally on MINIGRAF_EXTRACTION_STRATEGY,
and the lease must span the LLM path's fallback to heuristic as well.

_ensure_db_async is deleted.

Refs #255"
```

---

### Task 7: Convert `_run_ingestion`, the preload, and the startup backfill; delete `_db`

The largest task, and the one that closes the actual CI failure.

**Files:**
- Modify: `mcp_server.py:58` (`_db`, deleted), `:6583-6594` (`_startup_index_backfill`), `:8448` (`_load_ingestion_preload_state`), `:10579`, `:10618-10622`, `:10712-10723`, `:10826-10873`, `:10901-10995`, `:11001-11027`, `:11043-11056`, `:11107` (`_run_ingestion`), `:76-117` (the invariant comment)
- Test: `tests/test_mcp_server.py:22162-22190` (the pinning test, replaced)

**Interfaces:**
- Consumes: `db_lease`, `db_lease_async`, `_reset_db_state`, `_graph_path_current` from Task 2.
- Produces: `mcp_server._db` no longer exists; `mcp_server._graph_path` no longer exists (`_lease_manager.path` replaces it).

- [ ] **Step 1: Replace the pinning test with its inversion**

`TestSingleHandlePerProcess::test_release_idiom_does_not_drop_a_handle_others_still_hold` (lines 22162-22190) pins the **broken** semantics; #255 flags it as needing a deliberate update. Replace that method with:

```python
    def test_a_released_lease_genuinely_drops_the_handle(self, tmp_path, monkeypatch):
        """Replaces test_release_idiom_does_not_drop_a_handle_others_still_hold,
        which pinned the broken semantics: `_db = None` released only when it
        dropped the LAST reference, so `_db is None` was never evidence the
        graph file lock had been released.

        Under the lease protocol the count is authoritative, so the assertion
        inverts -- the lock file MUST be gone. Kept in this class because the
        #253 interleaving it enabled is what this whole protocol removes.
        """
        import mcp_server

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()

        with mcp_server.db_lease() as db:
            raw = mcp_server._db_execute(db, "(query [:find ?e :where [?e :ident ?v]])")
            assert "out of bounds" not in raw
            assert os.path.exists(graph + ".lock")

        assert not os.path.exists(graph + ".lock"), (
            "a released lease left the lock file behind, so the handle is still "
            "alive and the next open is a second handle -- the #253 interleaving"
        )
        assert mcp_server._lease_manager.lease_count == 0
```

- [ ] **Step 2: Convert `_load_ingestion_preload_state`**

At `mcp_server.py:8448`, replace:

```python
    db = _db if _db is not None else _open_db_at_with_extended_retry(_graph_path or _get_graph_path())
```

with a lease spanning the whole function body (it runs on its own worker thread, so the blocking backoff is correct here):

```python
    with db_lease(extended=True) as db:
```

and indent the remainder of the function into that block. Rewrite the docstring paragraph that names `_open_db_at_with_extended_retry`, `get_db()` and `_ensure_db_async()` (around lines 8421-8428) to:

```
    Takes an extended lease (db_lease(extended=True)): this runs off the event
    loop, so blocking backoff is correct here, and it can afford to wait out a
    typical orphan-process cleanup window rather than giving up in ~1.55s and
    entering a permanent "error" state (#106). The lease -- not a manual
    `_db = None` -- is what releases the graph file lock when this returns.
```

- [ ] **Step 3: Convert `_run_ingestion`'s seven release sites**

| Current | Becomes |
|---|---|
| `10579` `global _db, ...` | drop `_db` from the global declaration |
| `10618-10622` the post-preload `_db = None` and its four-line comment | delete both; the preload's own lease released on return |
| `10712-10723` `db = await _ensure_db_async()` … `db = None; _db = None` | `async with db_lease_async() as db:` around the stamp and `_frontier_load` calls |
| `10827-10873` per-commit acquire + `finally: db = None; _db = None` | `async with db_lease_async() as db:` wrapping the existing inner `try/except`; delete the ordering comment at 10858-10873 |
| `10901-10995` Stage B acquire + `finally` | `async with db_lease_async() as db:`; delete the ordering comment at 10991-10995 |
| `11001-11027` tags/last-run acquire + `finally` | `async with db_lease_async() as db:`; delete the comment at 11014-11027 |
| `11043-11056` final checkpoint + `finally` | `async with db_lease_async() as final_db:` inside the existing `try/except`; delete the comment at 11048-11056 |
| `11107` `_db = None` in the outer `except` | `_reset_db_state()` |

The four deleted comment blocks describe a `db = None`-then-`_db = None` ordering discipline that no longer exists. Delete them rather than updating them — the spec's rationale is that dead explanatory comments are how the false claims at `mcp_server.py:73-74` accumulated.

Add one comment at the per-commit site to record what replaced them:

```python
                    # A lease, not a manual acquire/release pair. The old code
                    # cleared the local before the global because a concurrent
                    # thread calling get_db() inside that window would open a
                    # SECOND handle (#251/#253). There is no window now: the
                    # count is authoritative and the handle drops exactly when
                    # it reaches zero.
                    async with db_lease_async() as db:
```

- [ ] **Step 4: Convert `_startup_index_backfill`**

Replace the `global _db` at 6583 and the `finally: _db = None` at 6593-6594 by giving `_rebuild_index_from_graph` its own lease. Delete the `finally` block entirely, and rewrite the docstring paragraph at 6575-6581 to:

```
    The lease taken by _rebuild_index_from_graph releases the graph's file lock
    when the rebuild returns, so the prepare_hook subprocess can still acquire
    it between turns. Without that, a rebuild triggered here would leave the
    persistent server process holding the lock indefinitely -- reproducing this
    issue's own failure mode by lock contention instead of a slow rescan.
```

- [ ] **Step 5: Delete the globals and rewrite the invariant comment**

Delete `mcp_server.py:58` (`_db`) and line 123 (`_graph_path`). Replace every remaining `_graph_path` read with `_graph_path_current()`; find them with:

```bash
grep -n "_graph_path\b" mcp_server.py
```

Remove the transitional `global _db` / `_db = None` lines from `_reset_db_state` (added in Task 4 Step 1), leaving only `_lease_manager.reset()`.

Replace the module comment at lines 76-117 ("THE SINGLE-HANDLE INVARIANT") with:

```python
# THE SINGLE-HANDLE INVARIANT (#255, #253, #251, project-minigraf/minigraf#304)
#
# At most one live MiniGrafDb handle per process. _DbLeaseManager enforces it:
# the handle is opened at lease count 0 -> 1, reused by every acquisition in
# between, and dropped at 1 -> 0. There is no window in which the module says
# "no handle" while a caller still holds one, which is what `_db = None` could
# never promise -- it released only when it dropped the LAST reference.
#
# Two handles on one file each cache their own FileHeader.page_count, allocate
# from it, and bounds-check read_page against it, which is where the flaky
# "Page N out of bounds (total pages: M)" came from. Since minigraf 1.2.2 a
# second same-process open raises instead of corrupting, and this project
# requires >= 1.2.3 -- so that path is closed at the source as well as here.
#
# _db_native_lock (above) is a DIFFERENT concern and is deliberately separate:
# it serializes calls INTO one handle; the manager governs the handle's
# LIFETIME. Conflating them is what made the old comment here necessary.
#
# The remaining hole is a caller that keeps its handle past the end of its
# `with db_lease()` block. That cannot be prevented in Python, so it is
# DETECTED: see _DbLeaseManager._detect_leaked_handle.
```

- [ ] **Step 6: Extend the grep guard to `mcp_server.py`**

In `TestNoDirectDbGlobalAssignment` (Task 4), add `"mcp_server.py"` to the `git grep` pathspec and update the class docstring to drop the "scoped to tests/ and evals/ for now" sentence.

- [ ] **Step 7: Run the suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: 1354 passed, 1 xfailed.

Run the ingestion-focused subset with output, since this task rewrote it:
`.venv/bin/python -m pytest tests/test_mcp_server.py -q -k "Ingest or ingestion or Sweep or Frontier"`

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Convert ingestion to leases and delete the _db global

_run_ingestion's seven release sites become six async leases plus the
preload's own extended one. The four comment blocks explaining why the local
had to be cleared before the global are deleted, not updated: that ordering
discipline existed because a concurrent get_db() in the window between them
would open a second handle, and there is no window now.

_load_ingestion_preload_state takes db_lease(extended=True) -- it runs off the
event loop, so blocking backoff is correct, and the long budget still lets it
wait out an orphan-process cleanup window instead of entering a permanent
error state (#106).

_db and _graph_path are gone; _lease_manager owns both. The grep guard now
covers mcp_server.py too.

test_release_idiom_does_not_drop_a_handle_others_still_hold pinned the BROKEN
semantics and is replaced by its inversion: the lock file must be gone once
the last lease exits. #255 called for that update explicitly.

Refs #255, #251, #253"
```

---

### Task 8: Delete the mtime staleness subsystem

**Files:**
- Modify: `mcp_server.py:124` (`_db_mtime`), `:3083-3092` (`_update_mtime`), `:3095-3111` (`_refresh_if_stale`), `:3530`, `:4057`, `:6801` (the three `_update_mtime()` calls), `:3866`, `:3885`, `:3942`, `:6755`, `:7092` (the five `_refresh_if_stale()` calls), `:119-122` (the comment above `_graph_path`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_refresh_if_stale`, `_update_mtime`, `_db_mtime` no longer exist.

- [ ] **Step 1: Write the test that pins WHY it is safe to delete**

The deletion rests on an invariant, so pin the invariant rather than the absence of the functions:

```python
class TestStaleHandlesAreStructurallyImpossible:
    """#255: _refresh_if_stale existed because a handle held across turns could
    go stale when another process wrote the graph. Under leases neither state
    it handled can occur:

      * count == 0  -> there is no handle, so the next acquire reads current
                       bytes anyway;
      * count > 0   -> we hold the file lock, so no other process can open the
                       graph, and the only writer is us.

    It also carried a live bug: _open_db_at(force=True) evaluated
    MiniGrafDb.open() while the old handle was still bound, which raises on
    minigraf >= 1.2.2.
    """

    def test_no_handle_survives_a_zero_count(self, tmp_path, monkeypatch):
        """The invariant the whole deletion rests on: count == 0 implies no
        live handle, which the lock file's absence proves."""
        import mcp_server

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()

        for _ in range(3):
            with mcp_server.db_lease() as db:
                mcp_server._db_execute(db, "(query [:find ?e :where [?e :ident ?v]])")
            assert mcp_server._lease_manager.lease_count == 0
            assert not os.path.exists(graph + ".lock")

    def test_a_lease_sees_writes_made_between_leases(self, tmp_path, monkeypatch):
        """What _refresh_if_stale was FOR: a change landing while we held no
        handle must be visible to the next lease. Opening fresh at 0 -> 1 is
        what makes the mtime check unnecessary, so pin the behaviour, not the
        mechanism."""
        import mcp_server

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()

        mcp_server.handle_minigraf_transact(
            '[[:decision/mtime-probe :decision/description "written between leases"]]',
            "stale-handle regression",
        )
        assert mcp_server._lease_manager.lease_count == 0

        result = mcp_server.handle_minigraf_query(
            '[:find ?d :where [?e :decision/description ?d]]'
        )
        assert result["ok"], result
        assert any("written between leases" in str(r) for r in result["results"]), result

    def test_the_stale_refresh_helpers_are_gone(self):
        import mcp_server
        for name in ("_refresh_if_stale", "_update_mtime", "_db_mtime"):
            assert not hasattr(mcp_server, name), (
                f"{name} survived; it has no reachable state under the lease "
                f"protocol and its force-reopen raises on minigraf >= 1.2.2"
            )
```

- [ ] **Step 2: Run and verify the third test fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestStaleHandlesAreStructurallyImpossible -v`
Expected: `test_the_stale_refresh_helpers_are_gone` FAILS; the first two PASS (they describe behaviour that already holds after Task 7).

- [ ] **Step 3: Delete the five `_refresh_if_stale()` call sites**

Remove the single-line call at `mcp_server.py:3866`, `:3885`, `:3942`, `:6755`, `:7092`. No replacement.

- [ ] **Step 4: Delete the three `_update_mtime()` call sites**

Remove the call at `mcp_server.py:3530`, `:4057`, `:6801`. Check each surrounding comment: any that explains "so we don't treat our own write as an external modification" goes with it.

- [ ] **Step 5: Delete the definitions**

Remove `_update_mtime` (3083-3092), `_refresh_if_stale` (3095-3111), the `_db_mtime` global (124) and the comment block above it at 119-122 (which describes the mtime workaround). Also delete `_open_db_at`'s `_db_mtime` bookkeeping (3071-3074) and its now-obsolete `force` parameter documentation if `_open_db_at` still exists at this point; if Task 7 already folded it into the manager, confirm with `grep -n "_open_db_at\b" mcp_server.py` that nothing remains.

- [ ] **Step 6: Verify nothing references them**

```bash
grep -rn "_refresh_if_stale\|_update_mtime\|_db_mtime" mcp_server.py tests/ evals/ hooks/
```

Expected: no hits. Anything in `tests/` is a test of the deleted mechanism and should be removed in this commit, not adapted.

- [ ] **Step 7: Run the suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: 1357 passed, 1 xfailed, minus however many tests of the deleted mechanism were removed in Step 6. Record the exact number in the commit message.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Delete the mtime staleness subsystem, now unreachable

_db_mtime had exactly one reader, _refresh_if_stale. Under the lease protocol
neither state it handled can occur: at count 0 there is no handle, so the next
acquire reads current bytes; at count > 0 we hold the file lock, so no other
process can write and the only writer is us. The invariant it rests on --
count == 0 implies no live handle -- survives even a leaked handle, since a
stray reference keeps the lock held too.

It also carried a live bug: _open_db_at(force=True) evaluated
MiniGrafDb.open() while the old handle was still bound, verified to raise on
minigraf >= 1.2.2, and it was reachable from four handler entry points.

Kept as a documented no-op was the alternative and is rejected: dead code with
an explanatory comment is how the false claims at mcp_server.py:73-74
accumulated and hid #251/#253 for months.

The replacement tests pin the invariant and the behaviour -- that a write
landing between two leases is visible to the second -- rather than the absence
of the functions.

Refs #255"
```

---

### Task 9: The #255 interleaving, made deterministic

The acceptance deliverable. Everything before this makes the protocol correct; this proves the original failure is gone.

**Files:**
- Test: `tests/test_mcp_server.py`
- Modify: `docs/testing-conventions.md`

**Interfaces:**
- Consumes: everything from Tasks 2-8, in particular `_lease_manager.strict_leak_detection` as the ablation's opt-out.
- Produces: nothing.

- [ ] **Step 1: Write the interleaving test**

```python
class TestConcurrentPollerDoesNotForceASecondOpen:
    """#255, and the reason it stopped being 'important but not urgent': this
    broke PR #262's CI as `test (3.11)`, 'Database is already open in this
    process'.

    Mechanism: run_ingestion_benchmark runs _run_ingestion concurrently with a
    poller whose handle_minigraf_query -> get_db() opened a handle whenever the
    global was None. get_db()'s own docstring named the precondition its safety
    rested on -- 'event-loop call sites always await _ensure_db_async() first'
    -- and a thread-executor poller is not one.

    Built as a direct reconstruction of the mechanism rather than a repeated
    sampling run: making the bug deterministic is what solved #251, where a
    20x20 statistical A/B had zero power.
    """

    def _drive(self, mcp_server, graph, rounds=40):
        """A poller thread querying while the main thread holds a lease and
        releases it between rounds -- the ingestion/poller interleaving."""
        errors = []
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                try:
                    mcp_server.handle_minigraf_query(
                        "[:find ?e :where [?e :ident ?v]]"
                    )
                except Exception as e:      # noqa: BLE001 - recording, not handling
                    errors.append(e)

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        try:
            for _ in range(rounds):
                with mcp_server.db_lease() as db:
                    mcp_server._db_execute(
                        db, "(query [:find ?e :where [?e :ident ?v]])"
                    )
                time.sleep(0.001)
        finally:
            stop.set()
            poller.join(timeout=5)
        return errors

    def test_a_polling_thread_never_forces_a_second_handle(self, tmp_path, monkeypatch):
        import mcp_server

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)

        errors = self._drive(mcp_server, graph)

        assert not errors, (
            f"{len(errors)} error(s) from the polling thread; first: {errors[0]}"
        )
        assert mcp_server._lease_manager.lease_count == 0

    def test_the_old_release_idiom_still_reproduces_the_failure(self, tmp_path, monkeypatch):
        """THE ABLATION. Without it the test above proves only that the current
        code passes, not that it fixed anything.

        Reconstructs the pre-#255 idiom -- clear the module's handle while
        another caller still holds one -- and requires the failure back.
        """
        import mcp_server
        from minigraf import MiniGrafDb

        graph = str(tmp_path / "t.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._reset_db_state()
        # This ablation deliberately strands a handle, which is precisely what
        # the leak detector exists to catch. Opt out for this test only.
        monkeypatch.setattr(
            mcp_server._lease_manager, "strict_leak_detection", False
        )

        held = MiniGrafDb.open(graph)        # _run_ingestion's local `db`
        try:
            with pytest.raises(Exception) as exc_info:
                MiniGrafDb.open(graph)       # the poller's get_db(), _db being None
            assert "already open in this process" in str(exc_info.value), (
                "the ablation did not reproduce the #255 failure, so the test "
                f"above proves nothing: {exc_info.value}"
            )
        finally:
            del held
            mcp_server._reset_db_state()
```

- [ ] **Step 2: Run both**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestConcurrentPollerDoesNotForceASecondOpen -v`
Expected: both PASS. The first proves the protocol holds under the real interleaving; the second proves the failure is still reproducible when the idiom is restored, so the first is not vacuous.

- [ ] **Step 3: Run it under load**

A concurrency guard that only passes on an idle machine is not a guard. Run it against a loaded box:

```bash
for i in $(seq 8); do (while :; do :; done) & done
.venv/bin/python -m pytest tests/test_mcp_server.py::TestConcurrentPollerDoesNotForceASecondOpen -v
kill %1 %2 %3 %4 %5 %6 %7 %8
```

Expected: still PASS. This is the check #261 taught — its predecessor was load-sensitive and read as a master bug twice.

- [ ] **Step 4: Document the lease rule**

Append to `docs/testing-conventions.md`:

```markdown
## Never hold a graph handle past its lease

`mcp_server` opens at most one `MiniGrafDb` handle per process, owned by
`_DbLeaseManager`. Take one with `with db_lease() as db:` (off the event loop)
or `async with db_lease_async() as db:` (on it), and let the block end.

Do not stash the handle anywhere that outlives the block — not in a module
global, not on `self`, not in a closure, not in a default argument. The manager
drops its own reference when the lease count reaches zero, but the handle only
dies when the LAST reference goes; a stray one keeps the graph file lock held
while the count says released. That is #255, and before minigraf 1.2.2 it was
silent graph corruption (#251, #253).

The leak detector will catch it at the next acquire and name the variable still
holding it, but it blames one step late — the next test, not yours — unless
your test calls `mcp_server._reset_db_state()` at teardown. Call it.

In tests, use `mcp_server._reset_db_state()` rather than assigning any module
global. A grep guard enforces this and will fail the suite.
```

- [ ] **Step 5: Run the full suite twice**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`

Run it a second time. The lease count is process-global, so an ordering-dependent leak shows up as a failure that moves between runs; two clean runs is the minimum evidence.

Expected: same pass count both times, 1 xfailed, and the xfail is `TestPreloadKnownEntitiesDescriptionValueIsDateBounded`. Confirm with:

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -q -rx | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_server.py docs/testing-conventions.md
git commit -m "Pin the #255 interleaving that broke PR #262's CI

A poller thread querying while the main thread cycles leases -- the
run_ingestion_benchmark shape that produced 'Database is already open in this
process' on test (3.11). Reconstructed directly rather than sampled: making
the bug deterministic is what solved #251, where a 20x20 statistical A/B had
zero power.

The ablation is the load-bearing half. It restores the pre-#255 idiom -- a
handle held while the module's slot is cleared -- and requires the failure
back, so the guard above cannot pass vacuously. It opts out of the leak
detector, since stranding a handle is exactly what the ablation is for.

Verified green under 8 busy loops on 8 cores: a concurrency guard that only
passes on an idle machine is not a guard (#261).

docs/testing-conventions.md gains the rule the protocol depends on: never hold
a handle past its lease, and reset in teardown so the detector blames the test
that leaked.

Refs #255"
```

---

### Task 10: At-scale acceptance and the PR

**Files:**
- No source changes expected. If the run surfaces a defect, fix it here and re-run.

**Interfaces:**
- Consumes: the complete protocol.
- Produces: the acceptance evidence for #255.

- [ ] **Step 1: Full suite, clean**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: green, 1 xfailed. Record the exact counts.

- [ ] **Step 2: The at-scale run with the poller live**

This harness is what produced #255's evidence, so it is the only honest confirmation. Expect roughly 30-60 minutes.

```bash
.venv/bin/python evals/at_scale/run_ingestion_benchmark.py --repo-path . \
    > /tmp/lease-acceptance.log 2>&1 &
```

It takes **no** `--graph-path`: `main()` wraps the run in a `TemporaryDirectory` and ingests into `bench.graph` inside it (that is the #256 complaint — every historical occurrence was uninspectable). Do not add one here; that is #256's scope, not this branch's. Its actual flags are `--repo-path`, `--branch`, `--poll-interval`, `--poll-duty-factor`, `--compare-ignore`.

Note this run also appends to `evals/at_scale/benchmark.md` and writes a JSON result under `evals/at_scale/results/`. Decide deliberately whether those belong in the PR — an acceptance run for a lifecycle fix is not a benchmark data point, and committing it muddies that file's history.

Expected: `final_status: "complete"`, commit count matching the repository's history, and **no** `Database is already open in this process` anywhere in the log. Poll progress with `ps -p <PID>`, never `pgrep -f` — it matches the polling shell's own wrapper and never goes false.

- [ ] **Step 3: Check stderr for detector output**

The detector prints rather than raises outside pytest, so a production leak is visible only here:

```bash
grep -c "db_lease" /tmp/lease-acceptance.log
```

Expected: 0. A non-zero count is a real leak the suite did not reach — fix it before opening the PR, and add a test at the site it names.

- [ ] **Step 4: Scan for closing keywords across every commit**

```bash
git log master..HEAD --format='%B' | grep -niE '(close[sd]?|fix(e[sd])?|resolve[sd]?)\s*:?\s*#[0-9]+'
```

Expected: no hits. `Refs #NNN` is the only issue reference in commit bodies. This scan must be re-run after **any** commit added later, including review fixups — scanning once before the push has already missed a commit on this project.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin fix-255-250-db-lease-protocol
```

PR body ends with the closing keywords, once:

```
Closes #255
Closes #250
```

- [ ] **Step 6: Verify what the PR will actually close**

```bash
gh pr view --json closingIssuesReferences
```

Expected: exactly `[255, 250]`. If #251, #253 or #222 appear, fix the body and re-check — a keyword/`#N` pair spans blank lines, so a line-based grep is blind to it.

- [ ] **Step 7: Wait for CI**

Expected: 6/6 green. The pytest matrix now has `fail-fast: false` (PR #265), so a recurrence of the #255 race shows as **one** failing interpreter, not three. Do not read the old three-broken-jobs signature into it.

---

## Self-Review

**Spec coverage.** Every section maps to a task: manager core → Task 2; acquire/release semantics → Task 2; three entry points → Task 2; leak detector and its stated limitation → Task 3; "not the rejected weakref guard" → Task 2 docstring; call-site conversion table → Tasks 5, 6, 7; the `memory_finalize_turn` correction → Task 6 Step 5; DB-opening tool set preserved → Task 6 Step 3; blocking backoff off the loop → Task 6 Step 4; mtime deletion → Task 8; #250 → Task 1; the six named tests → Tasks 1, 2, 3, 7, 9; test migration and grep guard → Task 4; acceptance → Task 10; sequencing and keyword discipline → Global Constraints and Task 10.

**Type consistency.** `_lease_manager`, `lease_count`, `path`, `bind_path`, `try_acquire`, `release`, `reset`, `strict_leak_detection`, `db_lease(extended=False)`, `db_lease_async()`, `_reset_db_state()`, `_graph_path_current()`, `_describe_referrers()`, `_DB_LEASE_TOOLS` are used with the same names and signatures in every task that references them. `open_db` returns `None` from Task 5 onward, and the four callers that used its return value are converted in Task 4 Step 4 — before the signature changes, so no window exists where they read `None`.

**Two ordering hazards worth naming for the implementer.** First, Task 4 must land before Task 7: the tests are migrated off `_db` while the global still exists, so every intermediate commit stays green. Second, Task 2's `_detect_leaked_handle` stub is a deliberate placeholder replaced in Task 3 — it is the one incomplete thing in the plan, and Task 3 Step 3 replaces it in full.

**Expected pass counts are estimates.** Each task's step states the count I expect from the baseline of 1334 plus that task's new tests. If the actual number differs by more than the tests added, investigate rather than updating the number.
