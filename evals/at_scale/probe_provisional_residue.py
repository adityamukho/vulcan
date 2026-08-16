"""#256: cross-check a surviving graph's provisional residue against the
correction sweep's own accounting.

Read-only, and a SEPARATE PROCESS by design. It opens the persisted graph
with no other handle live anywhere in the process -- querying in-process
while the benchmark's handle lifecycle unwinds is the exact hazard class
that produced #251/#253, and this probe exists to confirm that bug is gone.

The comparison is `M <= N`, not `M == N`:

  M = live :type/lineage-marker entities carrying :status :provisional
  N = the correction sweep's own "left provisional/unreconciled" total

N counts entities left provisional OR unreconciled. _correction_sweep_apply's
case 2 leaves an already-authoritative entity with an ambiguous
:introduced-by unreconciled without marking it provisional, so it raises N
without raising M. M is therefore a subset of N, and equality would fail on
a healthy graph -- as would asserting M == 0, since a non-empty residue is
the documented fail-safe, not a defect.

`M > N` means an entity sits provisional in the graph that the sweep never
accounted for: state left inconsistent by something other than the designed
fail-safe. That is the signature this probe detects.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def read_sweep_total(metrics: dict[str, Any]) -> int:
    """N, from the benchmark's metrics JSON.

    A missing key is fatal, not zero. Zero is a legitimate MEASURED value --
    _correction_sweep_log_summary prints only `if skipped_events:`, so an
    absent stderr line genuinely means no residue. A missing JSON key means
    something else entirely: metrics written before this instrumentation
    existed. Defaulting it to 0 would silently turn `M <= N` into `M == 0`
    and fail against a perfectly healthy graph.
    """
    if "correction_sweep_skipped" not in metrics:
        raise SystemExit(
            "metrics JSON has no 'correction_sweep_skipped' key -- it predates "
            "the #256 instrumentation. Re-run the benchmark; this probe will "
            "not guess N."
        )
    try:
        return int(metrics["correction_sweep_skipped"])
    except (TypeError, ValueError) as e:
        value = metrics["correction_sweep_skipped"]
        raise SystemExit(
            f"'correction_sweep_skipped' has invalid value {value!r} "
            f"(type: {type(value).__name__}) -- expected an integer. "
            f"Check the metrics JSON for corruption."
        )


def require_complete_run(metrics: dict[str, Any]) -> None:
    """Refuse a graph whose run did not finish. Residue on an aborted run is
    not evidence of anything."""
    status = metrics.get("final_status")
    if status != "complete":
        raise SystemExit(
            f"run finished with final_status={status!r}, not 'complete' -- "
            f"provisional residue on an unfinished run means nothing."
        )


def breakdown_by_entity_type(idents: Sequence[str]) -> dict[str, int]:
    """Count provisional entities per entity-type namespace.

    The type is taken from the ident's own namespace (":function/foo" ->
    "function"), which is how idents are constructed, rather than a second
    query per entity.
    """
    return dict(Counter(ident.lstrip(":").split("/", 1)[0] for ident in idents))


def residue_verdict(m: int, n: int) -> dict[str, Any]:
    """Apply `M <= N` and keep both raw numbers.

    Both survive in the output on purpose: `M <= N` weakens as N grows -- if
    the sweep legitimately skips thousands, M could hide real corruption
    underneath -- so a future run must be able to compare N itself across
    runs. A jump in N is its own signal.
    """
    return {
        "provisional_entities": m,
        "sweep_skipped": n,
        "ok": m <= n,
        "interpretation": (
            "M <= N: provisional residue is within the correction sweep's own "
            "accounting."
            if m <= n
            else "M > N: provisional state the sweep never accounted for -- "
                 "the #251 signature."
        ),
    }
