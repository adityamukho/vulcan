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

    def append(self, chunk: bytes) -> None:
        self._chunks.append(chunk)

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


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
    """
    capture = _Capture()
    saved_fd = os.dup(2)
    read_fd, write_fd = os.pipe()
    os.dup2(write_fd, 2)
    os.close(write_fd)

    def pump() -> None:
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            os.write(saved_fd, chunk)
            capture.append(chunk)

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()
    try:
        yield capture
    finally:
        sys.stderr.flush()
        # Restoring fd 2 closes the pipe's write end, which is what gives the
        # pump thread its EOF. Order matters: join before closing anything
        # the pump thread still touches -- saved_fd (its passthrough target)
        # and read_fd both stay open until the thread has actually stopped.
        os.dup2(saved_fd, 2)
        pump_thread.join(timeout=10)
        os.close(saved_fd)
        os.close(read_fd)
