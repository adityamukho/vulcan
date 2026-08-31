# #284 item 5 — portable graph-ownership hint: design

Replaces the `.graph.lock` sidecar machinery that minigraf 2.0.0 deletes, and
restores the #108 "decline instead of racing" pre-check on a mechanism that
works on Linux, macOS and Windows.

This is item 5 of #284, promoted to its own spec because the measurement pass
found it is not a comment-cleanup task: a guard silently stops guarding, and
the obvious replacements are all non-portable.

## What actually broke, measured

`probe_minigraf_v2_surface.py`, recorded in
`evals/at_scale/results/284-v2-surface-{1.2.3,2.0.0}.json`:

| | 1.2.3 | 2.0.0 |
|---|---|---|
| `.graph.lock` present while held | True | **False** |
| `_stale_lock_holder_pid` (cross-process) | a real PID | **`None`** |
| `_live_lock_holder_pid` while a holder demonstrably holds | a real PID | **`None`** |
| #108 pre-check is a silent no-op | False | **True** |

Upstream #317 moved locking into the kernel (`std::fs::File::try_lock`, i.e.
`flock` on Unix / `LockFileEx` on Windows) on the `.graph` file itself. The
lock is released by the kernel on process exit however it exits. The PID
sidecar every one of our helpers reads is simply gone.

The single-handle invariant is unaffected and needs no work here: minigraf
2.0.0 keeps an in-process `OPEN_PATHS` registry (`already_open_here`) and
raises `[STG-025]` on a same-process second open, which our `_is_lock_error`
still matches. This spec is only about the *pre-check*, not the invariant.

## The decision: own an advisory hint; stop reading minigraf's lock

There is no mechanism that is simultaneously non-contending, PID-returning,
and portable across our three platforms:

- **`/proc/locks`** returns the holder PID at 16.4 ms and correctly identifies
  the holder (verified). Linux only.
- **Non-blocking `flock`** answers in 0.03 ms. POSIX only, returns no PID,
  and — decisively — **cannot distinguish our own handle from another
  process's**, because flock locks attach to the open file description.
  `mcp_server` routinely holds a lease, so this reports "held" against
  ourselves and ingestion would decline to start against itself.
- **Try-open-and-release** contends by construction, at 376 ms per attempt,
  which is precisely what #108 exists to avoid.

So we stop trying to read minigraf's lock at all. We publish our own advisory
hint, and **correctness continues to rest entirely on minigraf's kernel
lock**. The hint is a scheduling courtesy: acting on a wrong hint costs one
race or one unnecessary decline, never a correctness failure. That is the same
"best-effort / racy by nature" contract `_live_lock_holder_pid` already
documented — this design makes it true by construction rather than by
depending on another project's private file format.

### Staleness is decided by a heartbeat, not by PID liveness

The hint carries a PID, but **the PID is a diagnostic label and is never
consulted for liveness**. Freshness is decided by the hint file's mtime.

This is what makes the design portable, and it removes a latent hazard.
`_pid_is_alive` uses `os.kill(pid, 0)`. On POSIX that is a liveness probe; on
Windows, CPython maps non-`CTRL_*` signals to `TerminateProcess(pid, sig)`, so
the "check" would terminate the process it asks about. **This must be
confirmed on a real Windows host** — it was not reproducible on the Linux
development machine — but the design removes the question entirely, because
`_pid_is_alive` becomes unused and is deleted.

Heartbeating also disposes of PID reuse without special handling: a fresh hint
means *something is actively refreshing it right now*, which is the liveness
claim we want. A reused PID under a stale hint is expired by definition.

## The mechanism

**File:** `<graph_path>.owner`, JSON. Deliberately NOT named `.lock` — a
leftover pre-2.0.0 `.graph.lock` may still be on disk (2.0.0 ignores and never
deletes it), and nothing should confuse ours with minigraf's.

```json
{
  "pid": 12345,
  "host": "somehost",
  "purpose": "ingestion",
  "started": "2026-08-31T12:00:00.000Z",
  "graph": "/abs/path/memory.graph"
}
```

**Constants** (`mcp_server.py`, beside the lease constants):

- `_OWNER_HINT_HEARTBEAT = 5.0` — seconds between refreshes.
- `_OWNER_HINT_TTL = 30.0` — a hint older than this is stale.
  Override: `MINIGRAF_OWNER_HINT_TTL`.

The margin is deliberate: TTL must comfortably exceed the heartbeat so a
merely-busy holder is never declared dead. The cost of the margin is that
after a hard crash the graph looks owned for up to `_OWNER_HINT_TTL`, during
which ingestion declines. That is the correct trade — declining is recoverable
on the next attempt; racing a long ingestion is what #108 filed against.

**Writer:** `_graph_owner_hint_held(path, purpose)`, an async context manager.
Writes the hint on entry, starts an asyncio task refreshing mtime every
`_OWNER_HINT_HEARTBEAT`, cancels the task and deletes the file on exit.

A dedicated task, not a refresh folded into the per-commit progress update:
a single slow commit must not be able to let the hint expire while ingestion
is demonstrably alive.

**Reader:** `_graph_owner_hint(path) -> Optional[dict]`, returning the parsed
hint only when it is fresh AND not ours. Returns `None` when the file is
absent, unparseable, older than the TTL, or names *this* process — where
"this process" means `pid == os.getpid()` **and** `host == socket.gethostname()`,
since a PID from another machine on a shared filesystem means nothing.

**Everything is best-effort.** Every read and write is wrapped; any failure
(read-only directory, unparseable JSON, a filesystem without usable mtime)
degrades to "no hint" and ingestion proceeds. A hint that cannot be written
must never be able to block real work.

### Deliberate semantic narrowing

The old pre-check detected *any* holder, because it read minigraf's lock,
which every open takes. The hint is published only around **long-held**
ownership — ingestion — not around every lease.

This is a behaviour change and it is intended. Declining to start ingestion
because another process ran a 50 ms query would be wrong; the lease's own
retry already absorbs short overlaps, and #284's measurements show it now
tolerates ~2.5 s of contention. The pre-check exists to avoid *losing a long
race*, and that is exactly what the narrowed hint covers.

## Code that is deleted

All of it is sidecar-era. None of it is load-bearing on either version — see
"Landing" below for the measurement that establishes that for `_clear_stale_lock`.

| symbol | site | disposition |
|---|---|---|
| `_live_lock_holder_pid` | `mcp_server.py:3111` | replaced by `_graph_owner_hint` |
| `_read_lock_holder_raw` | `:3072` | delete |
| `_clear_stale_lock` | `:3090` | delete |
| `_stale_lock_holder_pid` | `:3052` | delete |
| `_pid_is_alive` | `:3058` | delete (last user goes with the above) |

Call sites:

- **`_open_for_lease` (`:3208`)** — the stale-lock self-heal (`extract PID →
  clear stale lock → retry`) is deleted outright, not replaced. Under kernel locking there is no stale lock to clear: the kernel
  releases it on process exit however that exit happens. The `except` then
  narrows to re-raising after `_is_lock_error`, which still matches correctly.
- **`handle_git_ingest_status` (`:11710`, `:11716`)** — the `"error"` branch's
  `_stale_lock_holder_pid` lookup is deleted; it can no longer name a holder
  and `result["stale"]` is simply not set on that path. The `"skipped"`
  branch keeps working, with staleness re-derived from **hint freshness**
  rather than `_pid_is_alive(owner_pid)`. `owner_pid` remains populated from
  the hint, so the diagnostic keeps its shape.
- **The two pre-check sites (`:11663`, `:12105`)** — swap
  `_live_lock_holder_pid` for `_graph_owner_hint`, keeping `owner_pid` and the
  `status: "skipped"` contract intact so `handle_git_ingest_status` and its
  tests see no shape change.

The `[ingestion] skipped: already owned by live pid N` stderr line keeps its
wording; the PID now comes from the hint.

## Landing: one stage, and why the obvious two-stage split is wrong

An earlier draft of this spec split the work in two, holding the sidecar
deletions back until the `<2.0.0` cap was lifted, on the reasoning that
`_clear_stale_lock` is 1.2.3's crash recovery and deleting it behind the pin
would leave a crashed graph unopenable. **That reasoning was wrong, and the
probe says so.** It is recorded here because the error text actively invites
the mistake.

minigraf 1.2.3 tells you to clean up after a crash yourself — *"If no other
process is using this database, delete the lock file manually."* Measured
(`stale_recovery` section, both runs, positive control passing):

| | 1.2.3 | 2.0.0 |
|---|---|---|
| sidecar left on disk after `SIGKILL` | True | False |
| **reopen after `SIGKILL` succeeds** | **True** | **True** |
| `_clear_stale_lock` genuinely required | **False** | False |

1.2.3 leaves the file behind but checks the recorded PID's liveness on the next
open and proceeds anyway. Our `_clear_stale_lock` only ever deletes when that
same PID is dead, so it is doing work minigraf already does. It is redundant on
**both** versions, not load-bearing on either.

So this lands as **one change**, behind the existing pin, correct under both
versions: the hint does not depend on minigraf's locking at all, and the
deletions remove a path that recovers nothing on either version.

## Testing

The repo's standing bar applies, and this change is a magnet for tests that
pass for the wrong reason — the failure being restored is a guard that
silently stopped guarding.

**Every test must be ablation-proven.** For each, run it against the *current*
code and confirm it fails, then against the new code and confirm it passes. A
test that passes both ways is not a regression test for this work. Record the
ablation in the PR body, not just the claim.

**Every "not held" assertion needs a positive control.** `_graph_owner_hint`
returning `None` is the failure mode under test; a test whose setup silently
wrote no hint would assert `None` and pass while measuring nothing. Each such
test must first prove the hint really exists (or the holder really holds) by
an independent route.

Required cases:

1. A hint written by another process makes the pre-check decline, with
   `status: "skipped"` and `owner_pid` set.
2. No hint → ingestion proceeds.
3. A hint older than the TTL → proceeds. **Positive control:** the same test
   with a freshly-touched hint at the same path must decline.
4. A hint naming this process → proceeds (self-detection).
5. A hint naming this PID but a *different* host → declines.
6. A hold longer than the TTL stays fresh — proves the heartbeat runs, not
   just that it was started.
7. Holder killed with SIGKILL → the hint goes stale within the TTL and the
   next attempt proceeds. This is the crash path the old `_pid_is_alive`
   covered; it must keep working without it.
8. An unwritable hint location does not block ingestion.
9. A tree-wide assertion that `os.kill` and the deleted helpers are gone, so
   the Windows hazard cannot return unnoticed. Validate the search against a
   positive control before trusting a clean result.
10. A hard-killed holder leaves the graph reopenable **without**
    `_clear_stale_lock`. This is the claim that justifies deleting it; it must
    be asserted in the suite, not left resting on a one-off probe run.

Do not assert on paths containing `tmp_path`: pytest bakes the test's own name
into them, so such an assertion can pass by matching itself.

## Docs sync

- **`CLAUDE.md`** — the "Single-handle invariant" section describes minigraf's
  `.graph.lock` sidecar and a lease that is "exclusive out-of-process" via it.
  The invariant survives; the mechanism description does not. Rewrite for
  kernel locking and note the `.owner` hint as ours, not minigraf's.
- **`hooks/finalize_hook.py:59`** — documents a "stale-lock self-heal" that
  will no longer exist.
- **`SKILL.md`** — check for lock-file references before merging.
- **`evals/at_scale/benchmark.md`** — already carries the measurements
  (`## minigraf v2.0.0 Upgrade Surface — 284-v2-surface`); add a pointer once
  this lands.

## What this explicitly does NOT do

- **It does not take the 2.0.0 upgrade.** The `<2.0.0` cap from #286 stays
  until #284's remaining items land. This change is correct under BOTH
  versions and lands behind the pin, verified on each — see "Landing".
- **It does not touch `_LOCK_RETRY_MAX`/`_LOCK_RETRY_BASE`.** #284's
  measurements show 2.0.0 improves the hook path on both tolerance and
  latency; shrinking the budget would trade real robustness on a path where
  failure is silent.
- **It does not rewrite the four failing lock tests.** Three assert the
  sidecar's existence and die with the mechanism; the fourth
  (`test_retries_open_after_clearing_stale_lock_on_final_attempt`) is about
  the retry loop, not the pre-check. They belong to #284 item 3.
- **It does not address `OpenOptions`.** Still unreachable from Python
  (minigraf#322); #280 tracks the consequence.
- **It does not add a cross-process arbiter.** Upstream #309 named
  temporal_reasoning#108 as the place for one. This restores the existing
  narrow pre-check; a real arbiter remains out of scope.
