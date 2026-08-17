"""JSON and Markdown report writers for the at-scale benchmark tier (#120)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

_REPORT_HEADER = "# At-Scale Code-Graph Benchmark\n\nSee issue #120 and `docs/superpowers/specs/2026-07-19-at-scale-benchmark-design.md`.\nObservational only -- no pass/fail thresholds.\n"


def _utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _relative_to_report(path: Any, report_path: Path) -> str:
    """Render an artifact path relative to the report's own directory.

    benchmark.md lives at evals/at_scale/benchmark.md and the artifacts it
    names live at evals/at_scale/results/, so the useful rendering is
    `results/ingestion-<ts>.json`. Falls back to the absolute path when the
    artifact is not under the report's directory -- an artifact copied off a
    run host, or a tmp_path under test -- because a `../../../tmp/...` chain
    is worse than an absolute path for a human reader.

    Both sides are resolved first so a symlinked temp root (macOS's
    /var -> /private/var) does not defeat the relative case.
    """
    resolved = Path(path).resolve()
    base = Path(report_path).resolve().parent
    try:
        return str(resolved.relative_to(base))
    except ValueError:
        return str(resolved)


def _metrics_json_bullet(json_path: Any, report_path: Path) -> str:
    """The "- Metrics JSON:" provenance bullet (#276).

    ALWAYS emitted, for the reason _poll_duty_row is: an omitted line is
    invisible, so a reader could not tell a harness that did not record the
    path from one that was never asked to.
    """
    if json_path is None:
        return "- Metrics JSON: not recorded (this harness did not write one)"
    return f"- Metrics JSON: `{_relative_to_report(json_path, report_path)}`"


def write_json_result(metrics: dict[str, Any], results_dir: Path, prefix: str = "ingestion") -> Path:
    """Write metrics as machine-readable JSON to results_dir/<prefix>-<ts>.json."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{prefix}-{_utc_timestamp()}.json"
    path.write_text(json.dumps(metrics, indent=2) + "\n")
    return path


def _poll_duty_row(metrics: dict[str, Any]) -> str:
    """The "Poll duty cycle" row, rendered whether or not the metrics carry it.

    The row is ALWAYS emitted: benchmark.md's own note tells the reader to
    treat a missing duty cycle as "unmeasured, assume inflated", which only
    works if the absence is visible. Reading metrics['poll_duty_fraction']
    unconditionally instead raised KeyError on any result JSON produced before
    2026-08-07 (#242), making every pre-fix artifact un-re-renderable -- the
    opposite of the note's intent.
    """
    fraction = metrics.get("poll_duty_fraction")
    if fraction is None:
        return (
            "| Poll duty cycle (#242) | not measured "
            "(pre-2026-08-07 harness; assume inflated) |"
        )
    return (
        f"| Poll duty cycle (#242) | {fraction*100:.2f}% "
        f"over {metrics.get('poll_count', 'unknown')} polls |"
    )


def _checkpoint_duty_row(metrics: dict[str, Any]) -> str:
    """The "Checkpoint duty cycle" row (#241), rendered the same defensive
    way _poll_duty_row is: metrics from a pre-#241 run (or a run that failed
    before _run_ingestion ever installed a policy) simply lack
    'checkpoint_summary', and re-rendering that JSON must not raise.
    """
    summary = metrics.get("checkpoint_summary")
    if summary is None:
        return (
            "| Checkpoint duty cycle (#241) | not measured "
            "(pre-2026-08-08 harness; assume once-per-commit cadence) |"
        )
    return (
        f"| Checkpoint duty cycle (#241) | {summary['realised_duty']*100:.2f}% "
        f"over {summary['checkpoints']} checkpoints "
        f"({summary['total_seconds']:.2f}s total, "
        f"{summary['suppressed']} suppressed) |"
    )


def _stderr_tee_row(metrics: dict[str, Any]) -> str:
    """The "Stderr tee" row (#256).

    Renders the MEASUREMENT BASIS, not a result. As of 2026-08-16 the run is
    wrapped in an fd-level tee, so wall-clock, throughput and the latency
    series in this table were all measured with a pump thread copying every
    fd-2 write. Every earlier entry in benchmark.md was measured without one,
    and nothing else in this table marks which is which -- so a reader
    comparing a new row against an old one has no way to see that the
    instrument changed.

    Presence of 'stderr_capture_complete' is the discriminator: only the
    post-#256 harness emits it, on both the clean and the failed path.
    """
    if "stderr_capture_complete" not in metrics:
        return (
            "| Stderr tee (#256) | none (pre-2026-08-16 harness; "
            "wall-clock and latencies measured with no tee active) |"
        )
    return (
        "| Stderr tee (#256) | active (wall-clock and latencies measured "
        "with an fd-level tee in place) |"
    )


def _stderr_capture_row(metrics: dict[str, Any]) -> str:
    """The "Stderr capture" row (#256), rendered the same defensive way
    _poll_duty_row is.

    This row governs how the two rows below it may be read. On an incomplete
    capture the dropped-commit and error-signal counts are LOWER BOUNDS: the
    tee stopped collecting at some unknown point, so "0 dropped" means
    "none seen", not "none happened". Without this row, benchmark.md renders a
    truncated run as visually identical to a clean one -- which is the exact
    ambiguity #256 exists to remove, and the one the #255 acceptance claim
    rested on.
    """
    complete = metrics.get("stderr_capture_complete")
    if complete is None:
        return (
            "| Stderr capture (#256) | not measured "
            "(pre-2026-08-16 harness; dropped commits were never captured) |"
        )
    if complete:
        return "| Stderr capture (#256) | complete |"
    detail = metrics.get("tee_failure", "reason not recorded")
    return (
        f"| Stderr capture (#256) | **INCOMPLETE** -- the rows below are LOWER "
        f"BOUNDS, not counts: `{detail}` |"
    )


def _skipped_commits_row(metrics: dict[str, Any]) -> str:
    """The "Commits dropped" row (#256).

    Not derivable from any other row in this table. _run_ingestion isolates a
    per-commit failure rather than propagating it and increments `processed`
    anyway, so "Final status | complete" and "Commits ingested | N" are both
    blind to a dropped commit.
    """
    skipped = metrics.get("skipped_commits")
    if skipped is None:
        return (
            "| Commits dropped (#256) | not measured "
            "(pre-2026-08-16 harness; assume unknown, NOT zero) |"
        )
    if not skipped:
        return "| Commits dropped (#256) | 0 |"
    shown = ", ".join(f"`{sha}`" for sha in skipped[:5])
    more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
    return f"| Commits dropped (#256) | **{len(skipped)}**: {shown}{more} |"


def _error_signals_row(metrics: dict[str, Any]) -> str:
    """The "Error signatures" row (#256): #251 corruption signatures plus the
    tee's own pump-failure marker, as counted by scan_ingestion_stderr."""
    signals = metrics.get("error_signals")
    if signals is None:
        return (
            "| Error signatures (#251/#256) | not measured "
            "(pre-2026-08-16 harness; assume unknown, NOT zero) |"
        )
    if not signals:
        return "| Error signatures (#251/#256) | 0 |"
    names = sorted({s.get("pattern", "unknown") for s in signals})
    return (
        f"| Error signatures (#251/#256) | **{len(signals)}**: "
        f"{', '.join(names)} |"
    )


def _residue_verdict_row(result: dict[str, Any]) -> str:
    """The "Verdict" row (#256/#276): the M <= N reading, in words.

    The numbers alone do not carry the verdict -- `M <= N` is not the
    comparison a reader would guess (equality would fail on a healthy graph,
    and so would M == 0, since a non-empty residue is the correction sweep's
    documented fail-safe). Spelling the reading out is the point of putting
    this in a human record at all.
    """
    ok = result.get("ok")
    if ok is None:
        return (
            "| Verdict (#256) | not measured "
            "(result JSON carries no `ok` key) |"
        )
    if ok:
        return (
            "| Verdict (#256) | OK -- M <= N: provisional residue is within "
            "the correction sweep's own accounting |"
        )
    return (
        "| Verdict (#256) | **FAILED** -- M > N: provisional state the sweep "
        "never accounted for (the #251 signature) |"
    )


def _residue_count_row(label: str, result: dict[str, Any], key: str) -> str:
    """One of the residue section's plain integer rows, rendered the same
    defensive way _poll_duty_row is: an absent key says so rather than
    rendering 0, which here would read as a clean measurement of an empty
    graph.
    """
    value = result.get(key)
    if value is None:
        return f"| {label} | not measured (absent from the result JSON) |"
    return f"| {label} | {value} |"


def _residue_breakdown_row(result: dict[str, Any]) -> str:
    """The per-entity-type breakdown of M.

    Three distinct states, and collapsing any two of them loses information:
    an ABSENT key is unmeasured; an EMPTY dict is a measured zero (the
    healthy case, and the common one); a populated dict names where the
    residue sits.
    """
    breakdown = result.get("breakdown_by_entity_type")
    if breakdown is None:
        return (
            "| Provisional by entity type | not measured "
            "(absent from the result JSON) |"
        )
    if not breakdown:
        return "| Provisional by entity type | none |"
    rendered = ", ".join(f"{name}: {count}" for name, count in sorted(breakdown.items()))
    return f"| Provisional by entity type | {rendered} |"


def _residue_path_row(label: str, value: Any, report_path: Path) -> str:
    """An artifact-path row. Absence renders "not recorded", never an empty
    cell, so a reader can tell a path that was not captured from one that was
    captured as blank.
    """
    if value is None:
        return f"| {label} | not recorded |"
    return f"| {label} | `{_relative_to_report(value, report_path)}` |"


def append_ingestion_report(
    metrics: dict[str, Any],
    report_path: Path,
    json_path: Path | None = None,
) -> None:
    """Append a dated ingestion-run section to report_path, creating it with
    the shared header first if it doesn't exist yet.

    json_path is this run's results JSON, recorded so a reader can find the
    machine-readable artifact -- and through it the paired residue verdict
    (#276). It is a parameter rather than a metrics key because
    write_json_result only learns the path by writing the file, so folding it
    into the dict would need a second write.
    """
    if not report_path.exists():
        report_path.write_text(_REPORT_HEADER)

    lines = [
        "",
        f"## Ingestion Run — {_utc_timestamp()}",
        "",
        f"- Repo: `{metrics['repo_path']}` @ `{metrics['branch']}`",
        _metrics_json_bullet(json_path, report_path),
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Commits ingested | {metrics['commits_ingested']} |",
        f"| Final status | {metrics['final_status']} |",
        f"| Wall-clock | {metrics['wall_clock_seconds']:.2f}s |",
        f"| Throughput | {metrics['throughput_per_minute']:.1f} commits/min |",
        f"| Peak RSS | {metrics['peak_rss_kb']} KB |",
        f"| Graph size | {metrics['graph_size_bytes']} bytes |",
        f"| Fact-index size | {metrics['index_size_bytes']} bytes |",
        f"| Status-query latency (min/p50/p99/max) | "
        f"{metrics['status_latency']['min']*1000:.1f}ms / "
        f"{metrics['status_latency']['p50']*1000:.1f}ms / "
        f"{metrics['status_latency']['p99']*1000:.1f}ms / "
        f"{metrics['status_latency']['max']*1000:.1f}ms |",
        f"| Graph-query latency (min/p50/p99/max) | "
        f"{metrics['query_latency']['min']*1000:.1f}ms / "
        f"{metrics['query_latency']['p50']*1000:.1f}ms / "
        f"{metrics['query_latency']['p99']*1000:.1f}ms / "
        f"{metrics['query_latency']['max']*1000:.1f}ms |",
        _poll_duty_row(metrics),
        _checkpoint_duty_row(metrics),
        _stderr_tee_row(metrics),
        _stderr_capture_row(metrics),
        _skipped_commits_row(metrics),
        _error_signals_row(metrics),
    ]
    if "ignore_comparison" in metrics:
        comp = metrics["ignore_comparison"]
        lines += [
            f"| Graph size with path-ignore | {comp['with_ignore_graph_size_bytes']} bytes |",
            f"| Graph size without path-ignore | {comp['without_ignore_graph_size_bytes']} bytes |",
            f"| Path-ignore bloat reduction | {comp['delta_bytes']} bytes |",
        ]
    lines.append("")

    with report_path.open("a") as f:
        f.write("\n".join(lines))


def append_residue_report(
    result: dict[str, Any],
    report_path: Path,
    json_out_path: Path | None = None,
) -> None:
    """Append a dated provisional-residue section to report_path (#276).

    Called by probe_provisional_residue.main(), which runs as a SEPARATE
    PROCESS by design -- it opens the graph with no other handle live, the
    hazard class #251/#253 came from -- so append_ingestion_report cannot
    render these numbers itself: they do not exist yet when it runs. This
    appender keeps that separation intact by consuming a plain dict, exactly
    as append_ingestion_report consumes a metrics dict; report.py learns
    nothing about the probe.

    json_out_path is the probe's own verdict JSON. It is a parameter rather
    than a `result` key because `result` is written to disk BEFORE the report
    is appended, so folding the path in would either need a second write or
    leave the on-disk artifact disagreeing with the rendered section.
    """
    if not report_path.exists():
        report_path.write_text(_REPORT_HEADER)

    lines = [
        "",
        f"## Provisional Residue — {_utc_timestamp()}",
        "",
        "| Metric | Value |",
        "|---|---|",
        _residue_verdict_row(result),
        _residue_count_row("Provisional entities (M)", result, "provisional_entities"),
        _residue_count_row("Sweep skipped (N)", result, "sweep_skipped"),
        _residue_count_row("Commits in graph", result, "commits_in_graph"),
        _residue_breakdown_row(result),
        _residue_path_row("Graph", result.get("graph_path"), report_path),
        _residue_path_row("Metrics JSON", result.get("metrics_json"), report_path),
        _residue_path_row("Residue JSON", json_out_path, report_path),
        "",
    ]

    with report_path.open("a") as f:
        f.write("\n".join(lines))


def _query_ingestion_block(report: dict[str, Any]) -> list[str]:
    """The ingestion-health block of a query-benchmark section (#275).

    The query benchmark ingests its OWN graph, and used to discard the
    resulting metrics -- so its section could show a clean sweep of query
    latencies measured over a graph that had silently dropped commits.

    _stderr_capture_row / _skipped_commits_row / _error_signals_row are reused
    verbatim rather than re-rendered from the same keys. That is the point: an
    Ingestion Run section and a Query Correctness Run section must not be able
    to disagree about how a dirty run reads.
    """
    metrics = report.get("ingestion")
    if metrics is None:
        return [
            "",
            "Ingestion phase (#275): **not measured** -- this report carries no "
            "ingestion metrics, so nothing is known about the graph these "
            "latencies were measured over.",
        ]
    return [
        "",
        "Ingestion phase (#275) -- the graph these latencies were measured over:",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Commits ingested | {metrics.get('commits_ingested', 'not measured')} |",
        f"| Final status | {metrics.get('final_status', 'not measured')} |",
        _stderr_capture_row(metrics),
        _skipped_commits_row(metrics),
        _error_signals_row(metrics),
    ]


def append_query_report(report: dict[str, Any], report_path: Path) -> None:
    """Append a dated query-correctness section to report_path, creating it
    with the shared header first if it doesn't exist yet.

    Takes the {"entries": [...], "ingestion": {...}} report run_query_benchmark
    returns. No back-compatibility with the bare list it used to take: query
    results have never been persisted as JSON artifacts, so unlike the
    ingestion metrics files there is no historical input to re-render -- which
    is why this function is defensive about the ingestion keys and not about
    its own argument.
    """
    if not report_path.exists():
        report_path.write_text(_REPORT_HEADER)

    lines = [
        "",
        f"## Query Correctness Run — {_utc_timestamp()}",
        "",
        "| ID | Category | Result | minigraf latency | baseline latency |",
        "|---|---|---|---|---|",
    ]
    for r in report["entries"]:
        if r["passed"] is None:
            status = "SKIPPED (manual diff)"
        elif r["passed"]:
            status = "PASS"
        else:
            status = f"FAIL (expected `{r['expected']}`, got `{r['actual']}`)"
        lines.append(
            f"| {r['id']} | {r['category']} | {status} | "
            f"{r['minigraf_latency_seconds']*1000:.1f}ms | "
            f"{r['baseline_latency_seconds']*1000:.1f}ms |"
        )
    lines += _query_ingestion_block(report)
    lines.append("")

    with report_path.open("a") as f:
        f.write("\n".join(lines))
