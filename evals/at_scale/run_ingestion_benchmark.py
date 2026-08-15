# evals/at_scale/run_ingestion_benchmark.py
"""In-process ingestion-performance benchmark harness (#120, Part A).

Drives mcp_server.py's real handlers directly -- no subprocess, no MCP
stdio transport, no LLM -- following this project's real-backend testing
convention (docs/testing-conventions.md) applied to a standalone script
instead of a pytest fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.at_scale.metrics import latency_stats, throughput_per_minute  # noqa: E402

_STATUS_QUERY = "[:find (count ?e) :where [?e :entity-type :type/commit]]"


async def _poll_during_ingestion(
    ingest_task: "asyncio.Task[None]",
    poll_interval: float,
    duty_factor: float = 10.0,
) -> tuple[list[float], list[float], list[float]]:
    """Poll ingest_status and a graph query while ingest_task runs.

    Returns (status_latencies, query_latencies, poll_offsets); latencies in
    seconds, offsets in seconds since polling began.

    #242: both halves of this are load-bearing.

    The handlers run on a dedicated single-worker executor rather than inline,
    because handle_minigraf_query is synchronous -- calling it on the event
    loop stalls the _run_ingestion coroutine, the process-pool result
    collection, and every write_executor completion. Two full-history runs
    were killed at 3h54m and 36m on code measured at +3% before this was
    understood.

    Off-loop alone is NOT sufficient. handle_minigraf_query acquires
    _db_native_lock (mcp_server._db_execute), and _STATUS_QUERY counts every
    :type/commit entity, so its cost grows for the whole run -- a ~1s scan
    every 0.5s would still serialize against every ingestion write from
    another thread. The adaptive interval is what bounds the instrument's
    share of that lock: at duty_factor=10 the poller holds it for at most
    ~9% of the run however large the scan grows.

    _STATUS_QUERY is deliberately unchanged, so the QUERY being timed is the
    same one every entry already in benchmark.md timed. That is NOT the same
    claim as "the recorded latency series stays comparable", which an earlier
    version of this docstring made and which is false. The adaptive interval
    sleeps in proportion to the poll's own cost, so it undersamples exactly
    the late, expensive polls -- and _STATUS_QUERY's cost grows monotonically
    through a run, because it counts every :type/commit entity. Pre-fix
    entries polled every 0.5s regardless and sampled that expensive tail at
    full density. So query_latency p50/p99 recorded here are biased LOW
    against every pre-fix entry, in the opposite direction from the pre-fix
    wall-clock inflation. benchmark.md's 2026-08-07 note says both halves.

    A dedicated executor, rather than the loop default, keeps the poll off any
    thread the ingestion may want.

    Known limitation: a cancelled run still waits on an in-flight poll thread
    at executor shutdown. The previous implementation blocked the event loop
    outright, so this is strictly better, but it is not zero.
    """
    import mcp_server

    loop = asyncio.get_running_loop()
    status_latencies: list[float] = []
    query_latencies: list[float] = []
    poll_offsets: list[float] = []
    started = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="bench-poll"
    ) as poll_executor:
        while not ingest_task.done():
            poll_offsets.append(time.perf_counter() - started)

            t0 = time.perf_counter()
            await loop.run_in_executor(
                poll_executor, mcp_server.handle_minigraf_ingest_status
            )
            status_duration = time.perf_counter() - t0
            status_latencies.append(status_duration)

            t0 = time.perf_counter()
            await loop.run_in_executor(
                poll_executor, mcp_server.handle_minigraf_query, _STATUS_QUERY
            )
            query_duration = time.perf_counter() - t0
            query_latencies.append(query_duration)

            await asyncio.sleep(
                max(poll_interval, duty_factor * (status_duration + query_duration))
            )

    return status_latencies, query_latencies, poll_offsets


async def run_ingestion_benchmark(
    repo_path: str,
    branch: Optional[str],
    graph_path: Path,
    poll_interval: float = 0.5,
    duty_factor: float = 10.0,
    compare_ignore: bool = False,
) -> dict[str, Any]:
    """Run a full git ingestion against repo_path into an isolated graph at
    graph_path, measuring wall-clock, throughput, peak RSS, final graph/index
    size, and MCP responsiveness (status/query latency) while ingestion runs.

    graph_path must not already exist -- each call is a fresh, isolated run.
    """
    import fact_index
    import mcp_server

    mcp_server._reset_db_state()
    mcp_server.open_db(str(graph_path))
    mcp_server._ingest_progress = {
        "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    }

    resolved_branch = branch or mcp_server._default_git_branch(repo_path)

    start = time.perf_counter()
    ingest_task = asyncio.create_task(mcp_server._run_ingestion(repo_path, resolved_branch))
    try:
        status_latencies, query_latencies, poll_offsets = await _poll_during_ingestion(
            ingest_task, poll_interval, duty_factor
        )
        await ingest_task
    except BaseException:
        if not ingest_task.done():
            ingest_task.cancel()
            try:
                await ingest_task
            except (asyncio.CancelledError, Exception):
                pass
        raise
    wall_clock = time.perf_counter() - start

    poll_seconds = sum(status_latencies) + sum(query_latencies)
    poll_duty_fraction = (poll_seconds / wall_clock) if wall_clock > 0 else 0.0

    commits_ingested = mcp_server._ingest_progress["processed"]
    final_status = mcp_server._ingest_progress["status"]
    # Published by _run_ingestion's two policy-clearing finally blocks just
    # before _ingest_checkpoint_policy is discarded (#241 Task 6) -- the
    # policy itself does not survive the run, so this dict is the only
    # surviving source for realised checkpoint duty.
    checkpoint_summary = mcp_server._ingest_progress.get("checkpoint_summary")
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    index_path = fact_index.index_path_for(str(graph_path))
    graph_size_bytes = os.path.getsize(graph_path) if graph_path.exists() else 0
    index_size_bytes = os.path.getsize(index_path) if os.path.exists(index_path) else 0

    result = {
        "repo_path": repo_path,
        "branch": resolved_branch,
        "commits_ingested": commits_ingested,
        "wall_clock_seconds": wall_clock,
        "throughput_per_minute": throughput_per_minute(commits_ingested, wall_clock),
        "peak_rss_kb": peak_rss_kb,
        "graph_size_bytes": graph_size_bytes,
        "index_size_bytes": index_size_bytes,
        "status_latency": latency_stats(status_latencies),
        "query_latency": latency_stats(query_latencies),
        "final_status": final_status,
        "poll_count": len(poll_offsets),
        "poll_duty_fraction": poll_duty_fraction,
        "poll_offsets": poll_offsets,
        "checkpoint_summary": checkpoint_summary,
    }

    if compare_ignore:
        no_ignore_graph_path = graph_path.parent / f"{graph_path.stem}-no-ignore{graph_path.suffix}"
        original_patterns = mcp_server._DEFAULT_IGNORE_PATTERNS
        mcp_server._DEFAULT_IGNORE_PATTERNS = ()
        try:
            mcp_server._reset_db_state()
            mcp_server.open_db(str(no_ignore_graph_path))
            mcp_server._ingest_progress = {
                "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
                "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
            }
            await mcp_server._run_ingestion(repo_path, resolved_branch)
        finally:
            mcp_server._DEFAULT_IGNORE_PATTERNS = original_patterns

        without_ignore_size = (
            os.path.getsize(no_ignore_graph_path) if no_ignore_graph_path.exists() else 0
        )
        result["ignore_comparison"] = {
            "with_ignore_graph_size_bytes": graph_size_bytes,
            "without_ignore_graph_size_bytes": without_ignore_size,
            "delta_bytes": without_ignore_size - graph_size_bytes,
        }

    return result


def _exit_code(metrics: dict[str, Any]) -> int:
    """Return 1 if ingestion ended in an error state, else 0."""
    return 1 if metrics.get("final_status") == "error" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the at-scale ingestion benchmark (#120).")
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--poll-duty-factor", type=float, default=10.0,
        help="Sleep max(poll_interval, N * last_poll_duration) between polls, "
             "bounding the instrument's share of _db_native_lock (#242).",
    )
    parser.add_argument("--compare-ignore", action="store_true")
    args = parser.parse_args()

    import tempfile
    with tempfile.TemporaryDirectory(prefix="minigraf-at-scale-") as tmpdir:
        graph_path = Path(tmpdir) / "bench.graph"
        metrics = asyncio.run(
            run_ingestion_benchmark(
                args.repo_path,
                args.branch,
                graph_path,
                poll_interval=args.poll_interval,
                duty_factor=args.poll_duty_factor,
                compare_ignore=args.compare_ignore,
            )
        )

    from evals.at_scale.report import append_ingestion_report, write_json_result

    results_dir = REPO_ROOT / "evals" / "at_scale" / "results"
    report_path = REPO_ROOT / "evals" / "at_scale" / "benchmark.md"
    json_path = write_json_result(metrics, results_dir)
    append_ingestion_report(metrics, report_path)
    print(json.dumps(metrics, indent=2))
    print(f"\nWrote {json_path}")
    print(f"Appended to {report_path}")
    return _exit_code(metrics)


if __name__ == "__main__":
    sys.exit(main())
