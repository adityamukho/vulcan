# evals/at_scale/run_query_benchmark.py
"""Query correctness + latency benchmark against hand-verified ground truth
(#120, Part B). Reuses Part A's ingestion harness to populate a graph, then
runs each ground-truth entry's Datalog query and its git-command baseline,
timing both and comparing the Datalog result to the recorded expected value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess as _subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.at_scale.run_ingestion_benchmark import (  # noqa: E402
    _exit_code as _ingestion_exit_code,
    run_ingestion_benchmark,
)

# poll_offsets is the ONE key dropped from the ingestion metrics, on two
# grounds that both have to hold: it is unbounded (one float per poll --
# thousands on a full-history run, and this dict is printed to stdout), and it
# is the only key run_ingestion_benchmark._exit_code does not read.
#
# Everything else is kept WHOLE and deliberately. Hand-picking the health keys
# (skipped_commits / error_signals / stderr_capture_complete) would drift the
# moment _exit_code grows a clause -- and _exit_code is precisely what this
# module delegates to, so a stale subset would silently reinstate the fail-open
# #275 exists to close.
_DROPPED_INGESTION_KEYS = ("poll_offsets",)


def _ingestion_health(metrics: dict[str, Any]) -> dict[str, Any]:
    """The ingestion metrics this benchmark carries forward (#275)."""
    return {k: v for k, v in metrics.items() if k not in _DROPPED_INGESTION_KEYS}


async def run_query_benchmark(
    repo_path: str,
    graph_path: Path,
    ground_truth_path: Path,
) -> dict[str, Any]:
    """Run the query-correctness benchmark, returning both the per-entry
    results and the health of the ingestion that built the graph they were
    measured over (#275).

    Returns {"entries": [...], "ingestion": {...}}. The ingestion half used to
    be discarded; query latencies measured over a graph that silently lost
    commits are not comparable to ones that were not, and nothing else in this
    module can see that loss.
    """
    import mcp_server

    ground_truth = json.loads(ground_truth_path.read_text())
    pinned_ref = ground_truth.get("pinned_commit") or "HEAD"

    metrics = await run_ingestion_benchmark(
        repo_path, pinned_ref, graph_path, poll_interval=0.05
    )

    results: list[dict[str, Any]] = []
    for entry in ground_truth["entries"]:
        if "seed" in entry:
            db = mcp_server.get_db()
            # NOTE: the minigraf grammar is `(transact {opts} facts)` -- opts
            # dict FIRST, facts second (confirmed against mcp_server._transact
            # and every real call site in mcp_server.py/tests/test_mcp_server.py).
            # The reverse order parses without error but silently drops
            # :valid-from (verified empirically: a query with :valid-at before
            # "now" no longer finds the fact), so getting this order right is
            # load-bearing for the "seed" entries' bi-temporal semantics.
            db.execute(
                f'(transact {{:valid-from "{entry["seed_valid_from"]}"}} {entry["seed"]})'
            )

        if entry.get("skip_scoring"):
            # Multi-query manual-diff entries (see the entry's "note") are
            # documented, not scored -- no single query/expected pair exists.
            results.append({
                "id": entry["id"],
                "category": entry["category"],
                "passed": None,
                "actual": None,
                "expected": None,
                "minigraf_latency_seconds": 0.0,
                "baseline_latency_seconds": 0.0,
            })
            continue

        t0 = time.perf_counter()
        query_result = mcp_server.handle_minigraf_query(entry["datalog"])
        minigraf_latency = time.perf_counter() - t0
        actual = query_result.get("results")

        t0 = time.perf_counter()
        _subprocess.run(
            entry["baseline_cmd"], shell=True, cwd=repo_path,
            capture_output=True, text=True,
        )
        baseline_latency = time.perf_counter() - t0

        results.append({
            "id": entry["id"],
            "category": entry["category"],
            "passed": actual == entry["expected"],
            "actual": actual,
            "expected": entry["expected"],
            "minigraf_latency_seconds": minigraf_latency,
            "baseline_latency_seconds": baseline_latency,
        })

    return {"entries": results, "ingestion": _ingestion_health(metrics)}


def _exit_code(report: dict[str, Any]) -> int:
    """Return 1 if any scored entry failed, or if the ingestion phase that
    built the graph was unclean; else 0.

    The second clause is #275. This benchmark ingests its own graph and used to
    drop the resulting metrics on the floor, so it could report success over a
    graph that dropped commits, logged a #251 signature, or ran with a broken
    stderr capture -- the same fail-open shape #256 spent its review cycle
    removing from the ingestion benchmark, surviving in the entry point that
    branch did not cover.

    run_ingestion_benchmark._exit_code is IMPORTED, never reimplemented: its
    clauses are the definition of "unclean", and a copy here would go stale the
    first time one is added.

    Unscored entries (passed is None, e.g. manual-diff-only delta entries)
    never affect the exit code -- only an explicit False does. An absent
    "ingestion" key evaluates as clean, matching that function's own
    .get()-everywhere posture toward inputs that predate an instrument.
    """
    if any(r.get("passed") is False for r in report["entries"]):
        return 1
    ingestion = report.get("ingestion")
    if ingestion is not None and _ingestion_exit_code(ingestion):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the at-scale query benchmark (#120).")
    parser.add_argument("--repo-path", default=".")
    parser.add_argument(
        "--ground-truth",
        default=str(REPO_ROOT / "evals" / "at_scale" / "query_ground_truth.json"),
    )
    args = parser.parse_args()

    import tempfile
    with tempfile.TemporaryDirectory(prefix="minigraf-at-scale-query-") as tmpdir:
        graph_path = Path(tmpdir) / "bench.graph"
        report = asyncio.run(
            run_query_benchmark(args.repo_path, graph_path, Path(args.ground_truth))
        )

    from evals.at_scale.report import append_query_report, runtime_versions

    versions = runtime_versions()
    report["minigraf_version"] = versions["minigraf"]
    report["python_version"] = versions["python"]
    report_path = REPO_ROOT / "evals" / "at_scale" / "benchmark.md"
    append_query_report(report, report_path)
    print(json.dumps(report, indent=2))
    print(f"\nAppended to {report_path}")
    return _exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
