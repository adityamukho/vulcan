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
import select
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

# The three #251 signatures. REGEXES, NOT LITERALS: the page error carries
# live numbers ("Page 130 out of bounds (total pages: 113)" is the form #251
# actually reproduced), so a literal would match nothing and report all-clear.
_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("page_out_of_bounds", re.compile(r"Page \d+ out of bounds")),
    ("serde_deserialization_error", re.compile(r"Serde Deserialization Error")),
    (
        "stream_all_entries_expected_leaf_page",
        re.compile(r"stream_all_entries: expected leaf page"),
    ),
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
        tee could not run to a clean completion. Never raised -- read via
        .errors after the `with` block exits."""
        self._errors.append(exc)

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")

    @property
    def errors(self) -> list[BaseException]:
        return list(self._errors)


def _redirect_fd2_to_devnull() -> None:
    """Emergency valve: point fd 2 at something that can never block. Used
    when the pump has died and fd 2 still points at a pipe nobody is
    draining -- without this, the next write past the kernel pipe buffer
    (64 KiB on Linux) blocks forever (#256 review, Critical 2)."""
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull_fd, 2)
    finally:
        os.close(devnull_fd)


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

    Shutdown does NOT rely on EOF. mcp_server._run_ingestion's
    ProcessPoolExecutor uses the spawn context, whose multiprocessing
    resource_tracker inherits fd 2 -- a duplicate of the tee pipe's write
    end -- and holds it open for the parent process's ENTIRE lifetime, well
    past this context manager's exit. Restoring fd 2 on our side therefore
    never closes the last write end, so the pump would never see a real EOF
    (measured: pump still blocked in os.read() 10s after the pool was shut
    down). A dedicated control pipe signals shutdown instead: the pump
    selects on both the tee pipe and the control pipe, drains whatever is
    still buffered in the tee pipe, and only then exits once the control
    pipe fires -- deterministic regardless of who else is holding the tee
    pipe's write end open.
    """
    capture = _Capture()
    opened: list[int] = []
    saved_fd: int | None = None
    pump_thread: threading.Thread | None = None
    try:
        saved_fd = os.dup(2)
        opened.append(saved_fd)
        read_fd, write_fd = os.pipe()
        opened.extend((read_fd, write_fd))
        ctrl_read_fd, ctrl_write_fd = os.pipe()
        opened.extend((ctrl_read_fd, ctrl_write_fd))

        os.dup2(write_fd, 2)
        os.close(write_fd)
        opened.remove(write_fd)

        def pump() -> None:
            try:
                while True:
                    try:
                        readable, _, _ = select.select([read_fd, ctrl_read_fd], [], [])
                    except InterruptedError:
                        continue
                    if read_fd in readable:
                        chunk = os.read(read_fd, 65536)
                        if chunk:
                            os.write(saved_fd, chunk)
                            capture.append(chunk)
                            continue
                        # A real EOF (every writer closed) shouldn't happen in
                        # the spawn-context/resource_tracker environment this
                        # is designed for, but honor it if it does.
                        break
                    if ctrl_read_fd in readable:
                        break
            except BaseException as exc:  # must never die without unblocking fd 2
                capture.record_error(exc)
                with contextlib.suppress(OSError):
                    _redirect_fd2_to_devnull()

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

    # Setup completed without raising, so both are real by construction.
    assert saved_fd is not None
    assert pump_thread is not None

    try:
        yield capture
    finally:
        with contextlib.suppress(Exception):
            sys.stderr.flush()
        # Restore fd 2 FIRST so no new write can enter the tee pipe, then
        # signal the pump over the control pipe -- not by closing anything,
        # since resource_tracker's own duplicate of the write end means
        # closing ours is not sufficient to produce EOF. See the docstring.
        os.dup2(saved_fd, 2)
        with contextlib.suppress(OSError):
            os.write(ctrl_write_fd, b"x")
        pump_thread.join(timeout=10)
        if pump_thread.is_alive():
            # The pump is still touching saved_fd/read_fd/ctrl_read_fd.
            # Closing them now would recycle those fd numbers under a live
            # thread -- a leaked fd is strictly better than a corrupted one.
            capture.record_error(
                TimeoutError("tee_stderr: pump thread did not exit within 10s")
            )
        else:
            os.close(saved_fd)
            os.close(read_fd)
        os.close(ctrl_write_fd)
        if not pump_thread.is_alive():
            os.close(ctrl_read_fd)
