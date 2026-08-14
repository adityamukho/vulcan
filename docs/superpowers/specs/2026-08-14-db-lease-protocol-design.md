# DB lease protocol (#255) and the `write_executor` leak (#250)

Date: 2026-08-14
Issues: #255, #250
Branch: `fix-255-250-db-lease-protocol`

## Problem

`_db = None` is `mcp_server.py`'s "release the graph file lock" idiom. It is not
one. It releases only when it drops the **last** reference, so any local `db`
still on a stack keeps the handle — and its lock — alive while the global says
otherwise.

The failure is live, not theoretical. It broke PR #262's CI (`test (3.11)`,
`Database is already open in this process`). Mechanism, verified in source:
`run_ingestion_benchmark.py` runs `_run_ingestion` as a task concurrently with a
poller whose `handle_minigraf_query` → `get_db()` opens a handle whenever the
global is None. `get_db()`'s own docstring names the precondition its safety
rests on — "safe here only because event-loop call sites always await
`_ensure_db_async()` first" — and a thread-executor poller is not one.

Before minigraf 1.2.2 this was silent graph corruption (#251, #253,
project-minigraf/minigraf#304): two `FileBackend`s, each caching its own
`header.page_count`. Upstream now refuses the second open, which converts
corruption into a lock error. Better, not fixed: it still costs a CI re-run
every time it lands, and PR #254 found it aborting ingestions and silently
skipping WAL compaction on interrupted runs.

PR #254 removed four instances of the idiom. **The pattern survives**, and every
future `db = await _ensure_db_async()` can reintroduce it.

`_run_ingestion` separately leaks a `ThreadPoolExecutor` (#250): it is created
above the `try` whose `finally` shuts it down, with two awaited calls in the
gap. The comment beside the outer handler asserts the opposite of the truth.

## Decisions taken

Four, all made by the user during design:

1. **Full lease protocol**, not a targeted guard on the open-if-absent branch.
2. **Tests migrate to a reset helper** — `_db` leaves the module rather than
   being retained as the manager's storage slot.
3. **The leak detector is always on** — stderr in production, raise under
   pytest.
4. **#250 hoists the name** rather than re-indenting `_run_ingestion` under a
   `with` block.

Plus one taken after section 2 of the design: **delete the mtime subsystem**
(`_refresh_if_stale`, `_update_mtime`, `_db_mtime`) rather than keep it as a
documented no-op.

## Facts verified before designing

Run under `.venv/bin/python` (see `project_venv_interpreter_trap` — system
python carries minigraf 1.1.1 against a `>=1.2.3` floor and fakes results):

- `MiniGrafDb` **is** weakref-able. The leak detector needs no lock-file
  heuristics.
- A second `MiniGrafDb.open()` while the first handle is still bound **raises**
  `Database is already open in this process`. This is what makes
  `_refresh_if_stale`'s `force=True` reopen a live bug, not a theoretical one.
- The sidecar `.lock` file disappears the instant the last reference drops.
  That is the observable proof a release genuinely released, and it is what
  test 2 below asserts.

Two source facts that shape the design:

- `_open_db_at(path, force=False)` returns `_db` **without comparing paths**
  (mcp_server.py:3063). Not reachable from today's three callers, all of which
  pass `_graph_path`. Nothing prevents it becoming reachable.
- `hooks/prepare_hook.py` is a separate process that calls `mcp_server.get_db()`
  directly. "Release the lock between turns" is a real requirement, not
  vestigial, and `get_db()` is effectively public API.

## Architecture

One object owns the handle's lifetime. `_db`, `_graph_path`, `_open_db_at`,
`_try_open_with_self_heal`, `_open_db_at_with_retry`,
`_open_db_at_with_extended_retry` and `_ensure_db_async` collapse into
`_DbLeaseManager`, holding:

- a `threading.Lock` guarding slot, path and count
- the handle slot
- the bound graph path
- an integer lease count
- a `weakref` to the last released handle (the detector's input)

### Acquire

| transition | behaviour |
|---|---|
| `0 → 1` | run the leak detector, then open with the requested retry policy, register `SESSION_RULES` + `_user_rules`, store |
| `n → n+1`, same path | return the live handle; no open |
| any count, different path | `RuntimeError` |

The `n → n+1` reuse is what makes nesting free: `call_tool` holds the outer
lease and `handle_minigraf_query` nests inside it.

### Release

| transition | behaviour |
|---|---|
| `n → n-1` | decrement only |
| `1 → 0` | take a weakref to the handle, drop the slot |

### Entry points

Three, one per existing retry policy:

- `db_lease()` — sync, standard blocking backoff. Off-loop only, exactly as
  `_open_db_at_with_retry` is today.
- `db_lease(extended=True)` — the long time-budgeted backoff
  `_load_ingestion_preload_state` needs (#106).
- `db_lease_async()` — asyncio backoff, for event-loop callers (#99).

`_db_native_lock` is untouched and stays separate. It serializes *calls into* a
handle; this manager governs *lifetime*. Conflating them is what made the old
comment at mcp_server.py:71-73 necessary.

### What a lease hands out: `_LeasedDb`

**Added 2026-08-14 during implementation, after Task 2 measured the failure.**

`with db_lease() as db:` does **not** unbind `db` at block exit — that is plain
Python, not a quirk. So the caller's surviving binding keeps the real handle
alive past its own lease, the count reaches zero with the handle still
referenced, and the next acquire opens a SECOND handle on the same file.
Measured: it raises `Database is already open in this process` on the **second
iteration of any loop**, which is exactly the shape of `_run_ingestion`'s
per-commit loop.

So a lease does not yield the `MiniGrafDb` itself. It yields `_LeasedDb`, a
`__slots__` wrapper that forwards attribute access via `__getattr__` and drops
its reference to the real handle at `__exit__`. Verified: the lock file is gone
after every block across a three-iteration loop, despite the caller's binding
surviving, and a use-after-release raises
`graph handle used after its lease ended` instead of silently succeeding
against a handle nobody is leasing.

This is what makes "a release genuinely releases" a property of the protocol
rather than of caller discipline. Without it, every lease block would have to
end by dropping its own binding — the same invisible ordering rule that
produced #251/#253, merely relocated.

Cost: one `__getattr__` hop per call, negligible beside the native call it
wraps. There are no `isinstance(..., MiniGrafDb)` checks in `mcp_server.py` to
break — the six occurrences are type annotations, which do not enforce at
runtime. The manager still holds and weakrefs the **real** handle, so the leak
detector below is unaffected; the proxy narrows what it can catch to a caller
that deliberately reaches through to the underlying object.

### The leak detector

The check **cannot** run at `__exit__`: the caller's `as db` name is still bound
at that moment, so every release would look like a leak. It runs instead at the
next `0 → 1` acquire and inside `_reset_db_state()`. If the previous handle's
weakref is still live, someone escaped their lease. The diagnostic walks
`gc.get_referrers()` for the holding frames — the technique that actually found
the four sites in PR #254, after two attempts to reason it out from the source
were both wrong.

Output: stderr in production (never abort a live ingestion over a diagnostic),
raise when `PYTEST_CURRENT_TEST` is set.

**Known limitation, stated rather than papered over:** at acquire time, blame
lands one step late — the *next* test, not the leaker. Calling the same check
from `_reset_db_state()` mostly closes that, since tests reset at teardown, but
a test that leaks and never resets will be blamed on its successor.

### Why this is not the rejected weakref guard

The guard rejected in #253 reused a handle whenever a weakref to it was still
live, including handles whose backing file had been deleted. That resurrects
dead graphs and segfaulted the suite reproducibly, 3/3 runs.

Here reuse requires `count > 0`: someone is inside a `with` block right now, so
the file cannot have been torn down under them. A live weakref at `count == 0`
is the opposite of a reuse candidate — it is the error signal.

## Call-site conversion

| Site | Becomes |
|---|---|
| `get_db()` and its 9 in-module callers | deleted; each caller wraps its DB work in `with db_lease() as db:` |
| `_ensure_db_async()` | deleted; folded into `db_lease_async()` |
| `call_tool`'s 9 `await _ensure_db_async()` + `finally: _db = None` | one `AsyncExitStack` entering `db_lease_async()` only for the tools that open the DB today |
| `_run_ingestion`'s 7 release sites | 6 `async with db_lease_async() as db:` scopes + the preload's own extended lease |
| `_startup_index_backfill`'s `finally: _db = None` | `_rebuild_index_from_graph` leases internally |
| `open_db(path)` | binds the graph path, returns nothing, leaves no handle open. 4 of its 68 callers (2 in `tests/`, 2 in `evals/at_scale/`) use the return value and take a lease instead |
| `hooks/prepare_hook.py` | drops its `get_db()` call; `handle_memory_prepare_turn` leases internally |

`_get_graph_path()` (the `MINIGRAF_GRAPH_PATH` reader) is unchanged. The
manager exposes the bound path read-only for the several sites that need it
without a handle — `fact_index.index_path_for(_graph_path or
_get_graph_path())` being the common one.

The set of DB-opening tools is not a judgement call: it is exactly the branches
that await `_ensure_db_async()` today — `minigraf_query`, `minigraf_transact`,
`minigraf_retract`, `minigraf_rule`, `memory_prepare_turn`, `minigraf_audit`,
and `minigraf_ingest_status` **only when** `_ingest_progress["status"] !=
"running"`. `minigraf_report_issue`, `memory_finalize_turn` and
`minigraf_ingest_git` are excluded from `call_tool`'s pre-acquire, and that
conditional on `minigraf_ingest_status` must be carried across verbatim.

**Corrected 2026-08-14, during planning:** "excluded" above means excluded from
`call_tool`'s pre-acquire, **not** that the tool never opens the graph.
`handle_memory_finalize_turn` awaits `_ensure_db_async()` itself
(mcp_server.py:7111), conditional on `MINIGRAF_EXTRACTION_STRATEGY` being one
of `heuristic`/`llm`/`agent`. It is async, so it takes its own
`async with db_lease_async():` over that same condition. An earlier draft of
this section read as though the tool touched no DB at all, which would have
dropped a real acquire.

Two properties this must preserve, both easy to lose:

1. **The DB-opening tool set does not change.** `minigraf_report_issue` and
   `memory_finalize_turn` do not await `_ensure_db_async()` today and must not
   acquire a lease. A single outer lease around the whole dispatch would have
   changed that silently, which is why the `AsyncExitStack` is conditional.
2. **Blocking backoff never runs on the event loop.** `call_tool` acquires
   asynchronously; the sync handler's `with db_lease()` then nests at
   `count 1→2` and never opens. This is #99's guarantee and it survives
   unchanged.

The `db = None` / `_db = None` ordering comments in `_run_ingestion`
(mcp_server.py:10858-10873, 10991-10995, 11014-11027, 11048-11056) describe an
ordering discipline that no longer exists once leases unwind themselves. They
are deleted, not updated.

## Deleting the mtime subsystem

`_refresh_if_stale`, `_update_mtime` and `_db_mtime` are removed: 5 + 3 call
sites and one global.

`_db_mtime` has exactly one reader, `_refresh_if_stale` (mcp_server.py:3110).
Under leases neither state it handles can occur:

- `count == 0` ⟹ the slot is None ⟹ there is no handle, so the next acquire
  opens fresh and reads current bytes.
- `count > 0` ⟹ we hold the file lock ⟹ no other process can open the graph,
  so the only writer is us and a stale page table is impossible.

The invariant both rest on is `count == 0 ⟹ slot is None`, which holds by
construction. It survives even a leaked handle: if a stray reference keeps the
handle alive, the lock is still held, so no other process can modify the file
either.

`_refresh_if_stale` additionally carries the verified bug above — `_open_db_at`
with `force=True` evaluates `MiniGrafDb.open(path)` while the old handle is
still bound to `_db`, and that raises on minigraf ≥ 1.2.2. It is reachable from
four handler entry points whenever the graph mtime changed under a live handle.

Rejected alternative: keep it as a documented no-op. Dead code with an
explanatory comment is precisely how the false claims at mcp_server.py:73-74
and in `_open_db_at`'s docstring accumulated and hid #251/#253 for months.

## #250 — the `write_executor` leak

`write_executor = None` is initialised above the outer `try`. A single guarded
shutdown moves to the function's outermost `finally`:

```python
finally:
    if write_executor is not None:
        write_executor.shutdown(wait=True)
```

The inner `finally`'s shutdown is deleted, leaving one cleanup writer for one
resource. Initialising the name above the outer `try` dissolves the
"unbound name" problem the issue flags rather than guarding around it.

The comment at mcp_server.py:11096-11102 is rewritten. It currently asserts
`write_executor` is already shut down by the inner finally, which is false for
exactly the two failures the issue names.

## Testing

Real backend only, per `docs/testing-conventions.md`.

1. **Nesting opens one handle.** Nested leases call `MiniGrafDb.open` exactly
   once (spy on the call count).
2. **Release actually releases.** After the outermost lease exits,
   `<graph>.lock` is gone. This deliberately **inverts**
   `TestSingleHandlePerProcess::test_release_idiom_does_not_drop_a_handle_others_still_hold`,
   which #255 flags as pinning the broken semantics and needing a deliberate
   update. The old test is replaced, not deleted silently.
3. **The CI failure, made deterministic.** A thread calling
   `handle_minigraf_query` against a coroutine holding a per-commit lease.
   Ablation: restoring the `_db = None` idiom must fail with `Database is
   already open in this process`. Constructing the mechanism directly is what
   solved #251, where a 20x20 statistical A/B had zero power.

   Note this ablation deliberately creates the condition the leak detector
   raises on, so the detector needs an explicit opt-out (a manager attribute
   the test sets, not an env var) for this test and any future ablation of the
   same shape. The opt-out must default to strict, or decision 3 quietly
   becomes decision "test-only".
4. **Path mismatch.** Acquiring a lease for a different path while one is
   outstanding raises.
5. **Leak-detector positive control.** Stash a handle outside its `with`,
   acquire again; assert it raises *and* that the message names the holding
   frame. Without a positive control the detector can fail open and report
   all-clear forever (see `feedback_verification_can_fail_open`).
6. **#250 ablation.** Make `_open_index_writer_safe` raise; assert no executor
   thread survives, and that reverting the fix leaves one.

Every guard above pins an operation count or an observable state transition, not
wall clock — the rule `docs/testing-conventions.md` gained in PR #265.

### Test migration

`mcp_server._db = None` appears 138 times in `tests/test_mcp_server.py`, plus 7
`_db is None` reads and several in `evals/at_scale/`. All become
`mcp_server._reset_db_state()`, which clears the slot **and** the lease count —
strictly better than today, where a test that leaks a lease has no way to reset
the count and would poison its successor.

A guard test greps `mcp_server.py`, `tests/` and `evals/` for any surviving
`_db =` outside the manager. This is required, not belt-and-braces: assigning to
a module attribute that no longer exists is silently accepted by Python, so
without the grep an unmigrated site fails open.

## Acceptance

- Full suite green against the 1334 passed / 1 xfailed baseline. The 1 xfail is
  #257's permanent guard and must stay xfailed.
- One `run_ingestion_benchmark.py` run with the poller live. That harness
  produced #255's evidence, so it is the only honest confirmation the
  interleaving is closed at scale.

## Sequencing and process

One branch, `fix-255-250-db-lease-protocol`. #250 lands as the first commit —
it is independent, ~5 lines, and does not want to be buried under the lease
diff.

The PR must close **#255 and #250 only**. Both issue bodies reference #251,
#253 and project-minigraf/minigraf#304, so every commit message and the PR body
need a closing-keyword scan before push, re-run after any commit added later.
Verify with `gh pr view --json closingIssuesReferences`, not by grepping line by
line — the keyword/`#N` pair can span a blank line.
