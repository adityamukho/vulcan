"""Stderr capture and scanning for the at-scale ingestion benchmark (#256).

_run_ingestion isolates per-commit failures rather than propagating them (its
documented "fail only the one commit" contract), and _ingest_progress
["processed"] increments on the skip paths too. So neither `processed` nor
`final_status` can tell you a commit was dropped -- the stderr line is the
only signal. Same for the correction sweep's residue total.
"""

from __future__ import annotations

import contextlib
import os
import re
import selectors
import sys
import threading
from typing import Any

# Both skip sites in _run_ingestion's per-commit loop (mcp_server.py:11106
# and 11152). One regex covers both; the optional "unreadable " is the
# extraction-phase variant.
_SKIPPED_COMMIT_RE = re.compile(
    r"^\[_run_ingestion\] skipping (?:unreadable )?commit (\S+)",
    re.MULTILINE,
)

# _correction_sweep_log_summary (mcp_server.py:10590). UNCAPPED, unlike the
# per-entity logs (_CORRECTION_SWEEP_LOG_CAP = 10), which is what makes it
# usable as an accounting total.
_SWEEP_SUMMARY_RE = re.compile(
    r"^\[_correction_sweep\] (\d+) entities left provisional/unreconciled this run",
    re.MULTILINE,
)

# Four patterns: the three #251 signatures, then one tee-health signal.
#
# The #251 three are REGEXES, NOT LITERALS: the page error carries live
# numbers ("Page 130 out of bounds (total pages: 113)" is the form #251
# actually reproduced), so a literal would match nothing and report all-clear.
#
# The fourth entry is NOT a #251 signature -- it is tee_stderr()'s own
# pump-failure marker (#256 review round 3, New Important): without it, a
# dead pump's synthetic "[tee_stderr] pump failed: ..." line -- the ONLY
# signal left in the captured text once the pump has died -- matched none of
# the other three patterns, so this scanner (the sole text-only consumer)
# read a marker-only capture as indistinguishable from a clean run. It says
# "the capture apparatus broke", not "the graph broke".
#
# UNANCHORED, deliberately (#256 review round 4, Important 1). It was `^\[`
# through round 3, which the emitter itself defeated: the marker is appended
# after an arbitrary 64 KiB os.read() slice that frequently does not end at a
# line boundary, so a pump dying MID-RUN (the likelier EMFILE timing) glued
# the marker to a partial line and this scanner reported zero error signals.
# The emitter now prepends its own newline; the missing anchor is the second,
# independent half of that fix, for any text that reaches a consumer already
# concatenated.
#
# Dropping the anchor is not free, and an earlier version of this comment
# overstated the case by claiming it "bought no protection against spurious
# matches". It did protect one real path: both _run_ingestion skip lines embed
# the commit subject as `{subject!r}` (mcp_server.py:11107 and 11154), so a
# commit whose subject contained this literal would now match mid-line where
# `^\[` would have rejected it. The trade-off still favours the unanchored
# pattern -- that requires an adversarial commit subject, while a pump dying
# mid-run is the ordinary EMFILE timing -- but it is a trade-off, not a
# free win.
_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("page_out_of_bounds", re.compile(r"Page \d+ out of bounds")),
    # Provenance: tests/test_mcp_server.py:22876, which records this string as
    # OBSERVED against minigraf 1.2.1. Unlike the other two it is NOT confirmed
    # against current minigraf source (file.rs:265, btree_v6.rs:514 back those);
    # a grep of the whole tree for serde in an error/Display context finds
    # nothing. Kept anyway -- an extra pattern that never fires cannot cause a
    # false clean, and a 1.2.1-era string may still surface from an older graph.
    ("serde_deserialization_error", re.compile(r"Serde Deserialization Error")),
    (
        "stream_all_entries_expected_leaf_page",
        re.compile(r"stream_all_entries: expected leaf page"),
    ),
    ("tee_stderr_pump_failed", re.compile(r"\[tee_stderr\] pump failed:")),
    # #270. The four above are per-commit or apparatus signatures; none of
    # them fires when the RUN ITSELF dies. _run_ingestion catches its own
    # exception and returns normally, so a failure before Stage A skips no
    # commit, corrupts no page and breaks no pump -- every other signal here
    # reads clean for a run that ingested nothing. That is the fail-open this
    # module exists to close, and it stayed open until _run_ingestion started
    # printing this line.
    ("ingestion_failed", re.compile(r"\[_run_ingestion\] ingestion failed:")),
)


def scan_ingestion_stderr(text: str) -> dict[str, Any]:
    """Scan captured ingestion stderr for dropped commits, #251 signatures,
    and the correction sweep's residue total.

    correction_sweep_skipped defaults to 0 when no summary line is present.
    That is not "unmeasured" -- _correction_sweep_log_summary prints only
    `if skipped_events:`, so an absent line genuinely means zero. The key is
    therefore ALWAYS present; a consumer that finds it missing is looking at
    a metrics file written before this instrumentation existed and must fail
    rather than assume.

    Two loops call _correction_sweep_log_summary, so a run can emit more than
    one summary. Every value is kept in correction_sweep_summaries; the
    headline correction_sweep_skipped is their max -- the smallest defensible
    N, which keeps `M <= N` as strict as the evidence allows.
    """
    error_signals: list[dict[str, str]] = []
    for line in text.splitlines():
        for name, pattern in _ERROR_PATTERNS:
            if pattern.search(line):
                error_signals.append({"pattern": name, "line": line.strip()})

    summaries = [int(n) for n in _SWEEP_SUMMARY_RE.findall(text)]

    return {
        "skipped_commits": _SKIPPED_COMMIT_RE.findall(text),
        "error_signals": error_signals,
        "correction_sweep_summaries": summaries,
        "correction_sweep_skipped": max(summaries) if summaries else 0,
    }


class TeeStderrFailure(RuntimeError):
    """Raised on `with` exit if the pump thread failed or timed out, or if
    teardown's own restore() of fd 2 failed.

    Without this, a pump that dies at iteration 0 leaves capture.text() ==
    "", which scan_ingestion_stderr() reads as a byte-identical clean run --
    exactly the "verification fails open" shape #256 exists to catch (#256
    review round 2). A synthetic marker line is also appended to the
    captured text itself (see pump()'s except clause below), so a
    text-only consumer sees it too; this exception is the belt to that
    suspenders, for any consumer that only checks the return value.
    """


class _Capture:
    """Accumulates tee'd bytes. Held by the caller for the duration of the
    `with` block and read afterwards via text()."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._errors: list[BaseException] = []

    def append(self, chunk: bytes) -> None:
        self._chunks.append(chunk)

    def record_error(self, exc: BaseException) -> None:
        """Called by the pump thread (or the teardown timeout path) when the
        tee could not run to a clean completion. Never raised itself --
        tee_stderr()'s teardown turns a non-empty list into a
        TeeStderrFailure once the `with` block exits."""
        self._errors.append(exc)

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")

    @property
    def errors(self) -> list[BaseException]:
        return list(self._errors)


class _RedirectGuard:
    """Coordinates who is allowed to repoint fd 2: the pump's emergency
    valve (on a pump failure, mid-run) and teardown's own restore (once the
    `with` block exits) must never race, and once fd 2 has been handed back
    to the caller as their real stderr, the valve must never touch it again.

    Without this, a pump exception that lands after teardown has already
    restored fd 2 permanently redirects the caller's real stderr to
    /dev/null for the rest of the process -- reproduced in #256 review
    round 2 (fd2 identity unchanged: False; fd2 now: /dev/null). The lock
    makes emergency_redirect() and restore() mutually exclusive; the
    `_restored` flag, checked under that same lock, makes emergency_redirect
    a no-op for any call that loses the race.
    """

    def __init__(self, saved_fd: int, devnull_fd: int) -> None:
        self._lock = threading.Lock()
        self._restored = False
        self._saved_fd = saved_fd
        self._devnull_fd = devnull_fd

    def emergency_redirect(self) -> None:
        """Called by the pump on failure. Prefers the real saved stderr
        (matching what fd 2 would be with no tee at all) and falls back to
        /dev/null only if that itself fails. No-ops once teardown has
        already restored fd 2."""
        with self._lock:
            if self._restored:
                return
            try:
                os.dup2(self._saved_fd, 2)
            except OSError:
                with contextlib.suppress(OSError):
                    os.dup2(self._devnull_fd, 2)

    def restore(self) -> None:
        """Called exactly once, by teardown. Hands fd 2 back to the caller
        and permanently disarms the valve.

        try/finally, not a bare sequence: os.dup2 can raise (EBADF if
        saved_fd was clobbered, EMFILE under the fd pressure this module is
        built for), and if it did, `_restored` stayed False forever -- the
        valve remained armed for the rest of the process even though teardown
        had run. The disarm is unconditional; the failure still propagates to
        teardown, which has its own try/finally (#256 review round 4)."""
        with self._lock:
            try:
                os.dup2(self._saved_fd, 2)
            finally:
                self._restored = True


@contextlib.contextmanager
def tee_stderr():
    """Duplicate everything written to fd 2 into a buffer while still passing
    it through to the real stderr.

    Operates on the FILE DESCRIPTOR, not sys.stderr. _extract_commit runs in
    a ProcessPoolExecutor whose workers inherit fd 2 rather than the parent's
    sys.stderr object, and minigraf's error strings can reach fd 2 natively.
    A sys.stderr swap is blind to both -- see the ablation in
    tests/test_at_scale_stderr_capture.py, which fails loudly if that ceases
    to be true.

    Shutdown does NOT rely on EOF. Anything that inherits fd 2 while the tee
    is armed holds a duplicate of the tee pipe's write end, and can keep it
    open well past this context manager's exit -- in which case restoring
    fd 2 on our side never closes the last write end and the pump would never
    see a real EOF. The measured case was mcp_server._run_ingestion's
    spawn-context ProcessPoolExecutor, whose multiprocessing resource_tracker
    holds it for the parent process's ENTIRE lifetime (pump still blocked in
    os.read() 10s after the pool was shut down). ProcessPoolExecutor's own
    worker children are a second such holder.

    That measured case no longer applies to the at-scale harness itself: as of
    the #256 wiring, run_ingestion_benchmark calls
    multiprocessing.resource_tracker.ensure_running() BEFORE entering this
    context manager, precisely so the tracker inherits the real fd 2 instead.
    A genuine EOF is therefore reachable at teardown for that caller, and the
    pump handles it (the drain-then-break below). The control pipe is still
    required -- pool worker children and any other caller still hold
    duplicates, and correctness here must not depend on who happens to have
    forked -- but this paragraph used to state the tracker case as an
    unconditional fact, which it is not.

    A dedicated control pipe signals shutdown instead: the pump
    selects on both the tee pipe and the control pipe, drains whatever is
    still buffered in the tee pipe, and only then exits once the control
    pipe fires -- deterministic regardless of who else is holding the tee
    pipe's write end open. See
    test_shutdown_does_not_wait_for_eof_with_a_spawn_pool for the
    discriminating regression test.

    Uses selectors.DefaultSelector(), not select.select(): select() has a
    hard FD_SETSIZE (1024) ceiling that a 25-minute run with pool pipes, the
    sqlite index, the WAL, and git subprocesses can exceed, raising
    ValueError on the pump's first iteration with the whole block's stderr
    silently going nowhere (#256 review round 2). The selectors module picks
    epoll/kqueue/poll as available, none of which share that limit.

    If the pump thread fails or does not exit within the join timeout, the
    `with` block raises TeeStderrFailure on exit -- see that class's
    docstring for why a silent failure is unacceptable here.
    """
    capture = _Capture()
    opened: list[int] = []
    saved_fd: int | None = None
    devnull_fd: int | None = None
    pump_thread: threading.Thread | None = None
    guard: _RedirectGuard | None = None
    try:
        saved_fd = os.dup(2)
        opened.append(saved_fd)
        read_fd, write_fd = os.pipe()
        opened.extend((read_fd, write_fd))
        ctrl_read_fd, ctrl_write_fd = os.pipe()
        opened.extend((ctrl_read_fd, ctrl_write_fd))
        # Pre-opened, not opened lazily inside the pump's except clause: the
        # pump can fail with EMFILE (Important 3's own failure mode), which
        # would make a lazy os.open(os.devnull) fail right when it's needed
        # most and silently bring back the Critical-2 deadlock.
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        opened.append(devnull_fd)

        guard = _RedirectGuard(saved_fd, devnull_fd)

        os.dup2(write_fd, 2)
        os.close(write_fd)
        opened.remove(write_fd)

        def pump() -> None:
            # sel = None first, and selector construction/registration INSIDE
            # the try: selectors.DefaultSelector() allocates an epoll fd and
            # can raise OSError(EMFILE) under fd pressure, and register() can
            # raise too. The whole point of switching to selectors was a
            # 25-minute run holding >1024 fds -- the same fd pressure that
            # makes epoll allocation itself fail. If that construction sits
            # outside this try, the exception skips the except clause below
            # entirely: no marker, no record_error, no emergency_redirect,
            # fd 2 left pointing at a pipe nobody drains -- round 1's
            # Critical 2 again (#256 review round 3, New Critical).
            sel = None
            try:
                sel = selectors.DefaultSelector()
                sel.register(read_fd, selectors.EVENT_READ, "tee")
                sel.register(ctrl_read_fd, selectors.EVENT_READ, "ctrl")
                while True:
                    ready = {key.data for key, _ in sel.select()}
                    if "tee" in ready:
                        chunk = os.read(read_fd, 65536)
                        if chunk:
                            os.write(saved_fd, chunk)
                            capture.append(chunk)
                            continue
                        # A real EOF (every writer closed) shouldn't happen
                        # in the spawn-context/resource_tracker environment
                        # this is designed for, but honor it if it does.
                        break
                    if "ctrl" in ready:
                        break
            except BaseException as exc:  # must never die without unblocking fd 2
                # Append a marker into the captured text itself, not just
                # the side-channel .errors list -- scan_ingestion_stderr has
                # a dedicated pattern for exactly this line (#256 review
                # round 3), so a scan of an otherwise-empty capture reads as
                # a pump failure, not a clean run.
                #
                # LEADING newline, not just a trailing one (#256 review round
                # 4, Important 1). The last thing appended before this is an
                # arbitrary 64 KiB os.read() slice, which frequently does not
                # end at a line boundary; without the leading "\n" a pump that
                # dies mid-run glues its marker onto a partial line, and
                # anything reading the capture line-by-line loses the marker
                # as a line of its own. The scanner's pattern is also
                # unanchored now -- two independent halves, on purpose.
                marker = f"\n[tee_stderr] pump failed: {exc!r}\n".encode(
                    "utf-8", errors="replace"
                )
                capture.append(marker)
                capture.record_error(exc)
                # Defensive: emergency_redirect() already contains its own
                # OSError handling, but this is the pump's last chance to
                # not die noisily -- an exception raised from inside this
                # except clause propagates uncaught, it does not re-enter
                # this same handler.
                with contextlib.suppress(Exception):
                    guard.emergency_redirect()
            finally:
                if sel is not None:
                    sel.close()

        pump_thread = threading.Thread(target=pump, daemon=True)
        pump_thread.start()
    except BaseException:
        if saved_fd is not None:
            with contextlib.suppress(OSError):
                os.dup2(saved_fd, 2)
        for fd in opened:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise

    # Setup completed without raising, so all four are real by construction.
    assert saved_fd is not None
    assert devnull_fd is not None
    assert pump_thread is not None
    assert guard is not None

    try:
        yield capture
    finally:
        with contextlib.suppress(Exception):
            sys.stderr.flush()
        # Hand fd 2 back to the caller through the guard FIRST -- this both
        # stops new writes from entering the tee pipe and permanently
        # disarms the pump's emergency valve, so a pump failure during the
        # drain below can no longer clobber the caller's real stderr (#256
        # review round 2, New Critical). Only then signal the pump over the
        # control pipe; not by closing anything, since resource_tracker's
        # own duplicate of the write end means closing ours is not
        # sufficient to produce EOF. See the docstring.
        #
        # try/finally around restore(), NOT contextlib.suppress (#256 review
        # round 4, Minor 3): its os.dup2 can itself raise, and an unguarded
        # call here would skip the control-pipe write and the join below,
        # leaving fd 2 pointing at a pipe nobody drains -- the very hang this
        # module exists to prevent. suppress() would hide a genuinely
        # unrecoverable fd failure; try/finally still lets it propagate, but
        # only after the pump has been signalled, joined, and its fds closed.
        try:
            guard.restore()
        except BaseException as exc:
            # record_error, not just `raise` (#256 Task 4). On a compound
            # failure -- restore()'s dup2 raises while the pump has also died
            # -- the TeeStderrFailure raised at the bottom of this `finally`
            # displaces this OSError into its __context__ only: it appears in
            # neither str(exc) nor capture.errors, so a consumer that logs
            # str(exc) loses the single most consequential thing this module
            # can report, that fd 2 was never handed back. Recording it puts
            # it in both.
            capture.record_error(exc)
            raise
        finally:
            os.close(devnull_fd)  # safe: guard disarmed, valve never touches it again
            with contextlib.suppress(OSError):
                os.write(ctrl_write_fd, b"x")
            pump_thread.join(timeout=10)
            alive = pump_thread.is_alive()  # snapshot once -- review round 2, cheap fix 1
            if alive:
                # The pump is still touching saved_fd/read_fd/ctrl_read_fd, and
                # its selector still owns an epoll fd (pump()'s own `finally`
                # never got to run sel.close()). Closing any of these now would
                # recycle the fd number under a live thread -- a leaked fd is
                # strictly better than a corrupted one.
                capture.record_error(
                    TimeoutError("tee_stderr: pump thread did not exit within 10s")
                )
            else:
                os.close(saved_fd)
                os.close(read_fd)
            os.close(ctrl_write_fd)
            if not alive:
                os.close(ctrl_read_fd)

            if capture.errors:
                raise TeeStderrFailure(
                    "tee_stderr pump did not complete cleanly: "
                    + "; ".join(repr(exc) for exc in capture.errors)
                )
