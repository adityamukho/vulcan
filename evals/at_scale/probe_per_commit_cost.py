"""#260: drive a traced full-history ingestion and fit its per-commit cost.

The question: does per-commit cost grow with history length once per-commit
WORK SIZE is controlled for? #260 observed 3.09x growth across ~520 commits and
ruled out #239's point queries. The confound it names -- entities touched per
commit -- rises ~85x over this repository's history, because extraction is
whole-file, and #222's 1:1 converging streams turn that into a ~3.32x rise in
mean work per PROCESSED commit. That is the same magnitude as the observed cost
growth, which is why this needs a per-commit fit rather than another argument.

Two modes, because a 30-minute run must not have to be repeated to re-analyse:

    # measure (fresh graph, full history)
    .venv/bin/python evals/at_scale/probe_per_commit_cost.py --run \\
        --graph-path /tmp/260/g.graph --trace /tmp/260/trace.jsonl

    # re-analyse an existing trace
    .venv/bin/python evals/at_scale/probe_per_commit_cost.py \\
        --trace /tmp/260/trace.jsonl --metrics evals/at_scale/results/ingestion-*.json

USE .venv/bin/python. Bare python on the development machine has carried
minigraf 1.1.1 against this project's >=1.2.3 floor, where these queries run
~7x slower; that produced a retracted diagnosis on #239 and cost a day. The
artifact records the interpreter and the minigraf version so a reader can
check rather than trust.

The verdict logic lives in trace_fit.py and its constants are PRE-REGISTERED.
This module does I/O and provenance only -- it must not reinterpret a verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.at_scale import trace_fit  # noqa: E402

#: Pinned so the artifact describes a known interleave. The fit's
#: identifiability depends on fwd and rev commits sharing each window (see
#: trace_fit's docstring), so the ratio is part of the experiment, not an
#: incidental setting.
STREAM_RATIO = "1:1"


def read_trace(path: Any) -> list[dict]:
    """Parse a JSONL trace. A truncated FINAL line costs one record.

    An at-scale run takes ~30 minutes and the interesting ones sometimes die
    mid-write, so a half-written last record must not cost the whole trace. A
    malformed line anywhere else IS fatal -- that is corruption, not truncation,
    and silently dropping interior records would bias the fit invisibly.

    An empty trace raises. Zero records is not a verdict: "the run wrote
    nothing" and "cost was flat" are different findings and must not render
    the same.
    """
    lines = [l for l in Path(path).read_text().splitlines() if l.strip()]
    records: list[dict] = []
    for i, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                print(
                    f"[probe] dropping truncated final trace line "
                    f"({len(line)} bytes) -- run likely killed mid-write",
                    file=sys.stderr,
                )
                continue
            raise SystemExit(
                f"trace {path} has a malformed line at {i + 1} of {len(lines)}, "
                f"which is corruption rather than truncation. Refusing to fit a "
                f"trace with an interior hole."
            )
    if not records:
        raise SystemExit(
            f"trace {path} has no records. That is not a flat result -- it means "
            f"nothing was traced. Check MINIGRAF_INGEST_TRACE_PATH reached "
            f"_run_ingestion."
        )
    return records


def _minigraf_version() -> str:
    try:
        from importlib.metadata import version
        return version("minigraf")
    except Exception as e:
        return f"unknown ({e})"


def build_result(records: list[dict], metrics: dict, env: dict) -> dict:
    """The artifact: trace_fit's analysis plus enough provenance to audit it."""
    result = dict(trace_fit.analyse(records))
    result["commits_ingested"] = metrics.get("commits_ingested")
    result["wall_clock_seconds"] = metrics.get("wall_clock_seconds")
    result["final_status"] = metrics.get("final_status")
    result["graph_path"] = metrics.get("graph_path")
    result["trace_path"] = metrics.get("trace_path")
    result["provenance"] = {
        "executable": sys.executable,
        "minigraf_version": _minigraf_version(),
        "stream_ratio": env.get("MINIGRAF_INGEST_STREAM_RATIO", STREAM_RATIO),
        "checkpoint_duty": env.get("MINIGRAF_INGEST_CHECKPOINT_DUTY", "default"),
    }
    # Exploratory only -- W is frozen and these may not be substituted for it.
    result["exploratory"] = {
        "mean_await_s": (
            sum(float(r.get("await_s", 0.0)) for r in records) / len(records)
        ),
        "tag_counts": {
            tag: sum(1 for r in records if r.get("tag") == tag)
            for tag in ("fwd", "rev")
        },
    }
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="Drive a fresh traced full-history ingestion first.")
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--graph-path", default=None,
                        help="Required with --run. Must not already exist.")
    parser.add_argument("--trace", required=True, help="Trace JSONL path.")
    parser.add_argument("--metrics", default=None,
                        help="Benchmark metrics JSON. Required without --run.")
    parser.add_argument("--out", default=None,
                        help="Artifact path (default: "
                             "results/260-per-commit-cost-attribution.json).")
    args = parser.parse_args(argv)

    os.environ.setdefault("MINIGRAF_INGEST_STREAM_RATIO", STREAM_RATIO)

    if args.run:
        if not args.graph_path:
            raise SystemExit("--run needs --graph-path (a fresh path)")
        import evals.at_scale.run_ingestion_benchmark as bench
        # resolve_graph_path enforces the fresh-path rule -- it refuses the
        # graph, its .wal AND its fact index, because minigraf replays a
        # leftover .wal and re-ingesting into an existing graph repairs
        # nothing (see CLAUDE.md and #235).
        with bench.resolve_graph_path(args.graph_path) as graph_path:
            metrics = asyncio.run(bench.run_ingestion_benchmark(
                args.repo_path, args.branch, graph_path,
                trace_path=Path(args.trace),
            ))
    else:
        if not args.metrics:
            raise SystemExit("without --run, --metrics is required")
        metrics = json.loads(Path(args.metrics).read_text())

    records = read_trace(args.trace)
    result = build_result(records, metrics, dict(os.environ))

    out = Path(args.out) if args.out else (
        REPO_ROOT / "evals" / "at_scale" / "results"
        / "260-per-commit-cost-attribution.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")

    print(json.dumps(result, indent=2))
    print(f"\nWrote {out}")
    print(f"VERDICT: {result['verdict']} -- {result['verdict_reason']}")

    # A VOID run is a failure: the measurement did not work, and exiting 0
    # would let CI or a reader record it as a clean flat result.
    return 0 if result["verdict"] != "VOID" else 1


if __name__ == "__main__":
    sys.exit(main())
