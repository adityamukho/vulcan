"""Stderr capture and scanning for the at-scale ingestion benchmark (#256).

_run_ingestion isolates per-commit failures rather than propagating them (its
documented "fail only the one commit" contract), and _ingest_progress
["processed"] increments on the skip paths too. So neither `processed` nor
`final_status` can tell you a commit was dropped -- the stderr line is the
only signal. Same for the correction sweep's residue total.
"""

from __future__ import annotations

import re
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
