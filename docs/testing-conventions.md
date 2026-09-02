# Testing Conventions

## Real backend, always

Every test in `tests/test_mcp_server.py` uses a real `minigraf` backend —
never a `MagicMock`-based fake of `MiniGrafDb`. There are two patterns for
getting a real handle, depending on what the test needs.

### Pattern 1: `real_db` (in-memory, the default)

Most tests use the `real_db` fixture, which opens a genuine
`MiniGrafDb.open_in_memory()` instance (redirected via a
`monkeypatch.setattr(MiniGrafDb, "open", ...)` so `mcp_server.open_db()`'s
real code path — session-rule registration, mtime tracking — still runs):

```python
@pytest.fixture
def real_db(monkeypatch, tmp_path):
    """Open a real (non-mocked) in-memory MiniGrafDb for the duration of the test.
    Full Datalog parsing, schema validation, and bi-temporal semantics — just
    backed by open_in_memory() instead of a disk file, so tests stay fast."""
    from minigraf import MiniGrafDb
    real_open_in_memory = MiniGrafDb.open_in_memory
    monkeypatch.setattr(MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory()))
    import mcp_server
    mcp_server.open_db(str(tmp_path / "t.graph"))
    yield mcp_server.get_db()
```

This exists because a `MagicMock`-based fake of `MiniGrafDb` never parses or
validates the Datalog string passed to `execute()` — it just records call
arguments and returns a canned response. That blind spot hid a real bug for
months: `mcp_server.py`'s valid-time-bounded `(transact ...)` calls
constructed the command with facts and options in the wrong order, silently
making minigraf ignore `:valid-from`/`:valid-to` bounds. No mocked test could
catch this, because none of them ever asked minigraf to actually parse the
string.

### Pattern 2: real file-backed DB (multi-commit / persistence tests)

`real_db`'s `open_in_memory()` hands back a brand-new, isolated store on
every open, so it can't model anything that needs to survive across
separate `MiniGrafDb.open()` calls at the same path — e.g. the git-ingestion
tests that check a watermark written in one `_run_ingestion` call is visible
to a later one, or that a lock is genuinely released between commits. For
those, tests open a real, disk-backed `MiniGrafDb.open()` against a
`tmp_path` graph file directly (no `real_db` fixture), so the same on-disk
graph persists across open/close cycles exactly as it would in production.
See `TestRunIngestionShutdown.test_resumes_from_watermark_after_shutdown`
(two `_run_ingestion` calls against the same on-disk graph, second one
resuming from the first's watermark) and `TestClosedEntityLifecyclePurge`
(ingests, calls `mcp_server._reset_db_state()` to release the lock, then
reopens the same path with `MiniGrafDb.open()` to query post-ingestion state)
for worked examples.

`_reset_db_state()` is the release step, not `mcp_server._db = None`. That
global was this module's release idiom until #255 replaced it with
`_DbLeaseManager`; it no longer exists, in either `mcp_server.py` or the
suite. Several code comments still describe it in the past tense -- read
those as history.

## Always verify results

Never assert on mock call arguments (`"transact" in str(call)`,
`assert_called_once()`). Always re-query `real_db` (or the file-backed DB)
after the code under test runs, and assert on the actual persisted or
returned facts. For bi-temporal code specifically, verify with
`:valid-at`/`:as-of` queries at multiple points in time — before, during,
and after the fact's valid-time window — not just "does it exist right
now," which behaves like `:any-valid-time` regardless of bounds and would
not have caught the argument-order bug either.

## Plain-object doubles for infrastructure concerns

Very rarely, a test needs a plain-object double that isn't `MagicMock` and
doesn't fake Datalog semantics — not because the test is avoiding Datalog
concerns, but because a real `MiniGrafDb` instance is unsuitable for
infrastructure reasons unrelated to correctness. These narrow cases are:
- `SlowFakeDb`, in
  `TestDbNativeCallSerialization.test_db_execute_and_checkpoint_never_overlap_across_threads`.
  A real DB executes too fast to expose lock-contention overlap; a
  deliberately slow plain-object double detects when two threads enter
  concurrently.
- `_FakeDb`, in `TestRunIngestion`'s
  `test_local_db_reference_dropped_before_repo_total_enumeration` and
  `test_git_commits_runs_before_any_db_is_opened`. A real FFI handle's object
  identity and `sys.getrefcount()` profile differ from a plain Python object;
  the first test measures reference-leak patterns and can't use `MagicMock`
  for the same inflation reason, and the second needs a handle that exists
  without a real open having happened.

Named rather than located by line number: both citations here were off by
several thousand lines by 2026-09, pointing into unrelated tests.

## The one narrow exception: external, non-minigraf APIs

Mocking survives only for genuinely external network services (or
third-party parsing libraries) unrelated to minigraf: LLM provider clients
(OpenAI/Anthropic, in `TestCallLlm`, `TestLlmStrategyOpenAI`,
`TestLlmStrategy`, and `TestAgentStrategy`) and GitHub API calls (in
`TestMinigrafReportIssue`, via the `report_issue` module).
`TestGetParser` similarly mocks the optional `tree_sitter`/
`tree_sitter_python` packages, since those are a separate third-party
dependency with no minigraf involvement at all. These stay mocked to avoid
real API cost, network dependency, non-deterministic model output, and an
optional native-extension install requirement in CI — the underlying
`MiniGrafDb` in every one of these tests is still always real (or, for
`TestGetParser`, simply not involved).

## Manufacturing real error conditions instead of faking them

`TestGetDbLockRetry` needs genuine lock contention, which a mock used to fake
via a canned `MiniGrafError`. Locking is in the kernel, so these tests use a
real file-backed `MiniGrafDb.open()` plus `_hold_lock_subprocess()`, which
spawns a subprocess that opens a real `MiniGrafDb` at the same path and holds
it — producing real contention and the real `MiniGrafError` the retry path
sees. Only `mcp_server.time.sleep` is monkeypatched, purely to skip real
backoff delays — that's test-speed plumbing, not faking minigraf's behavior.

Even the non-lock case is manufactured rather than fabricated:
`test_non_lock_errors_are_not_retried` points the graph path at a directory,
so minigraf raises a genuine "Is a directory" on the first `open()`.

This is the pattern to reach for whenever a future test needs a real failure
condition rather than a business-logic result: prefer manufacturing the
condition for real over mocking the exception.

**Two things this section used to say are gone, and the reason matters.** It
named `TestTryOpenWithSelfHealReuse` and `TestOpenDbAtWithExtendedRetry`, and
described tests parsing a holder PID out of `"Database is locked by another
process (lock file: ..., holder PID: ...)"` via
`mcp_server._stale_lock_holder_pid`/`_pid_is_alive`. #284 deleted all four
names along with mcp_server's stale-lock self-heal: minigraf 2.0.0 locks with
`flock`/`LockFileEx` on the `.graph` file itself, so there is no PID sidecar
to unlink, the kernel drops the lock however the process exits, and the error
text is now `"[STG-026] Database is locked by another process (<path>)"` with
no PID in it. Measured before deletion, the self-heal recovered nothing on
either version.

What survives is `test_a_dead_holders_leftover_lock_does_not_wedge_get_db`,
and `_hold_lock_subprocess`'s `exit_immediately=True` mode with it — a real,
verifiably-dead PID is still the honest way to build a stale on-disk artifact
by hand. Read that helper's docstring before using it: on this platform a
subprocess that exits cleanly removes its own lock, so "open() raises citing
an already-dead holder" is not reachable through genuine process death.

## Observing real calls without faking them: `execute_spy()`

Some tests need to assert that `execute()` was or wasn't called (e.g. "must
not query the graph while status is running") without faking any return
value. Use `execute_spy()`, which wraps the real `mcp_server._db_execute` to
record calls while still executing them for real — this is not a mock in
the sense this convention eliminates; it never fakes a return value or
bypasses real parsing.

## Real sqlite3 for the fact index

`tests/test_fact_index.py` and any `mcp_server.py` test touching the
persisted fact index follow the same "real backend, always" rule extended
to `fact_index.py`'s SQLite FTS5 file: tests open a real `sqlite3.Connection`
(a `tmp_path`-backed file, or `:memory:` where cross-process behavior isn't
under test) — never a mocked `sqlite3.Connection`. The one test that
specifically needs a second real OS process (not just a second connection)
is `test_cross_process_reader_sees_writer_commits`, which spawns a real
subprocess via `subprocess.run` to prove the index is actually shared via
the filesystem/OS page cache, not via any in-process Python state — mirrors
the existing DB lock-retry cluster's "spawn a real subprocess to
manufacture a real condition" pattern.

## Performance guards count operations, never wall clock

A performance regression test asserts on a **count that expresses the defect
axis** — write calls, structures built, pairs evaluated — not on elapsed
seconds. Counts are deterministic; elapsed seconds are a property of the
machine and whatever else is running on it, so a wall-clock assertion fails
on a shared or contended CI runner and gets attributed to whatever change
happens to be in flight (#261, where a load-sensitive failure was twice read
as a real defect on a green `master`).

Worked examples:

- `test_per_commit_writes_do_not_scale_with_entity_count` and
  `test_per_commit_writes_stay_in_the_forward_walk_s_range` (#233) pin
  per-commit write calls at a fixed commit count, not run time.
- `test_matcher_per_pair_setup_does_not_scale_with_pool_size` (#261, replacing
  a 12-second bound) counts the O(pool)-sized structures the rename matcher
  builds and asserts the number is flat as the pool triples.

Two things such a test needs:

- **A positive control.** A flat or small count proves nothing unless the
  larger input really did more work, so assert that too (the matcher test
  requires the 3x pool to evaluate >4x the candidate pairs). Otherwise code
  that silently stopped working passes.
- **An ablation.** Reintroduce the defect in the production code, confirm the
  test fails, and confirm it fails on the *assertion that names the defect* —
  then revert. A guard that has never been seen red guarantees nothing.

If a timing assertion genuinely cannot be avoided, its margin must be
justified in a comment against measured numbers, not tuned until it passes.

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

## Patch the class, never the module-level singleton

To replace a *method* for one test, monkeypatch the class:

```python
monkeypatch.setattr(mcp_server._DbLeaseManager, "try_acquire",
                    lambda self, path=None: ...)
```

Not the `mcp_server._lease_manager` singleton. `monkeypatch.setattr(instance,
name, value)` reads the inherited value first and, on undo, **restores** it
rather than deleting it — planting the class's bound method as a permanent
entry in the instance's `__dict__`. Instance lookup then beats class lookup for
the rest of the session, so every later class-level patch of that name is
silently ignored on that object.

The failure this produces is invisible and order-dependent: the victim test
passes 20/20 alone and fails only when it runs after the planting test, with
the patched function simply never called. It cost a full debugging cycle in
PR #269 before it was named (#272).

Instance *data* attributes set in `__init__` — `strict_leak_detection` is the
one here — are not affected: they have no class-level counterpart, so undo
restores them where they already lived. Patching those on the singleton is
fine.

The autouse `reset_mcp_server_db` fixture enforces this: at teardown it fails
any test that left a class-shadowing name in `_lease_manager.__dict__`, and
strips it so one offender cannot poison the rest of the run.
