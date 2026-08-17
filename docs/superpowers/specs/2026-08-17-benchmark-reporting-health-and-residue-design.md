# Benchmark reporting: ingestion health in the query benchmark, residue verdict in `benchmark.md`

Design for issues #275 and #276, both filed from PR #273's review (#256) and
deliberately left out of that branch. They are one work item: the same defect
class — an at-scale instrument that reports success without reporting what it
was built to check — in two entry points that #273 did not cover.

Date: 2026-08-17

## Problem

### #275 — the query benchmark discards its own ingestion evidence

`run_query_benchmark.py:36` calls `run_ingestion_benchmark(...)` and drops the
return value. Since #256 that return carries the only evidence that the query
benchmark's own ingestion phase was clean:

- `skipped_commits` — commits the per-commit handler dropped. Not derivable
  from anything else. `_ingest_progress["processed"]` increments on the skip
  paths too, and `final_status` stays `"complete"`, because `_run_ingestion`
  isolates per-commit failures by design.
- `error_signals` — the #251 corruption signatures.
- `stderr_capture_complete` — `False` when the capture itself truncated, which
  makes the two lists lower bounds rather than counts.

`run_ingestion_benchmark._exit_code` enforces all of these and
`run_ingestion_benchmark.main()` honours it. `run_query_benchmark._exit_code`
(`run_query_benchmark.py:92`) looks only at query results, so its ingestion
phase can drop commits, or run with a broken capture, and the run still reports
success. Query latencies measured over a graph that silently lost commits are
not comparable to ones that were not.

### #276 — `benchmark.md` records capture health but not the residue verdict

`append_ingestion_report` renders the #256 capture-health rows (tee active,
capture complete, commits dropped, error signatures) but not **M**, **N** or
`commits_in_graph` — the `M <= N` verdict that #256 exists to produce. That
lives only in `evals/at_scale/results/256-provisional-residue.json`, and
nothing in `benchmark.md` names which JSON that is. So the durable human record
says a run was clean in the capture sense while saying nothing about whether
its provisional residue was accounted for.

This is not a one-liner because `probe_provisional_residue.py` is a **separate
process by design** — it opens the graph with no other handle live, which is
the hazard class #251/#253 came from — so `append_ingestion_report` has no
access to the probe's output at the time it runs.

## Decisions

Both taken by the user on 2026-08-17.

- **#276 shape:** the issue's option 1 (a second appender called by the probe)
  **plus** the small half of option 3 (the ingestion section names its own
  results JSON). Option 2 — the probe rewriting the benchmark's own section —
  is rejected: it makes the probe edit a file the benchmark owns, and the two
  can disagree if either is re-run. Option 3 alone is rejected because it never
  puts the verdict in the human record, which is the issue's headline
  complaint.
- **#275 severity:** fail the run. A dropped commit, a #251 signature, an
  incomplete capture or an errored ingestion makes `run_query_benchmark` exit
  non-zero, *and* the health fields land in its reported output either way.
  Accepted cost: the nightly workflow's query-benchmark step can now fail on an
  ingestion defect rather than only on a query mismatch.

## Design — #275

### Return shape

`run_query_benchmark` changes from `list[dict[str, Any]]` to
`dict[str, Any]`:

```python
{
    "entries":   [ ...unchanged per-entry result dicts... ],
    "ingestion": { ...the ingestion metrics dict, minus poll_offsets... },
}
```

`"ingestion"` holds the **whole** metrics dict, not a curated
`skipped_commits`/`error_signals`/`stderr_capture_complete` triple. A curated
subset drifts the moment `run_ingestion_benchmark._exit_code` grows a clause,
and that function is precisely what this delegates to; copying its input keys
by hand reintroduces the fail-open one release later.

`poll_offsets` is the single exclusion, for two stated reasons: it is unbounded
(thousands of floats on a full-history run, and this dict is printed to stdout),
and it is the only key `_exit_code` does not read. The exclusion carries that
reasoning as a comment so a future clause that *does* read it is a visible
conflict rather than a silent one.

### Exit code

```python
def _exit_code(report: dict[str, Any]) -> int:
    """1 if any scored entry failed, or if the ingestion phase was unclean."""
```

Returns 1 if any entry has `passed is False` (unchanged semantics — `None`
never counts), **or** if
`run_ingestion_benchmark._exit_code(report["ingestion"])` returns 1. That
function is imported, never reimplemented.

A missing `"ingestion"` key evaluates as clean, matching
`run_ingestion_benchmark._exit_code`'s own `.get()`-everywhere posture toward
pre-#256 inputs. Both causes return exit code 1; they are not split into
distinct codes, because the printed output and the `benchmark.md` section both
name which fired.

### Reporting

`append_query_report(report, report_path)` takes the new dict. It renders the
existing per-entry table unchanged, then an ingestion-health block built by
**reusing `_stderr_capture_row`, `_skipped_commits_row` and
`_error_signals_row` verbatim** from `report.py`, plus a final-status row. The
reuse is the point: the query section and the ingestion section must not be
able to disagree about how a dirty run reads.

An absent `"ingestion"` key renders the block as *not measured*, following the
file's existing `_poll_duty_row` convention.

No back-compatibility with the old list shape is provided. Query results have
never been persisted as JSON artifacts, so no historical input exists to
re-render — unlike the ingestion metrics files, whose age is exactly why
`report.py` is defensive elsewhere.

### Deliberately not handled

#275 notes two failures that became newly reachable at that call site once it
began running under `tee_stderr()`. Neither needs code here, and adding a
handler for either would be dead code:

- **A tee *setup* failure** re-raises as `OSError`. It already propagates out of
  `run_query_benchmark` and `main()`; `asyncio.run` re-raises, the process
  prints a traceback and exits non-zero.
- **`TeeStderrFailure`** is caught inside `run_ingestion_benchmark` (which is
  why a 25-minute run's metrics survive it) and surfaces as the `tee_failure`
  key — which the new exit-code clause above now catches.

The residual — an ingestion crash destroys the query run's `benchmark.md`
record entirely — is the same shape #256 addressed for the ingestion benchmark,
and is out of scope here.

## Design — #276

### `append_residue_report`

New in `report.py`:

```python
def append_residue_report(
    result: dict[str, Any],
    report_path: Path,
    json_out_path: Path | None = None,
) -> None:
```

`json_out_path` is the probe's own verdict-JSON path. It is a parameter rather
than a `result` key for the same reason the crumb is: `result` is written to
disk *before* the report is appended, so folding the path into it would either
require a second write or leave the on-disk artifact disagreeing with the
rendered section.

Called from `probe_provisional_residue.main()` after it writes its verdict
JSON. It writes its own dated `## Provisional Residue — <UTC ts>` section:

| Row | Source key |
|---|---|
| Verdict (#256) | `ok` — `OK — M <= N` / `**FAILED** — M > N` with the failure reading |
| Provisional entities (M) | `provisional_entities` |
| Sweep skipped (N) | `sweep_skipped` |
| Commits in graph | `commits_in_graph` |
| Provisional by entity type | `breakdown_by_entity_type` — `none` when empty, *not measured* when absent |
| Graph | `graph_path` |
| Metrics JSON | `metrics_json` |
| Residue JSON | the probe's `--json-out` path, passed by `main()` |

`report.py` gains no knowledge of the probe: it consumes a plain dict, exactly
as `append_ingestion_report` consumes a metrics dict. The probe's separate
process is untouched, which is what keeps it safe.

**Every row follows the `_poll_duty_row` convention — an absent key renders
*not measured*, never `0`.** This matters more here than anywhere else in the
file: the nightly workflow does **not** run this probe, so most `benchmark.md`
entries will carry no residue section at all, and a re-rendered older artifact
must not be able to manufacture a clean verdict from missing keys.

The probe appends unconditionally, matching both other entry points.
`benchmark.md` is append-only by design, and `--json-out` already warns that a
re-run overwrites the committed artifact.

It gains a `--report-path` argument defaulting to
`evals/at_scale/benchmark.md`, mirroring `--json-out`'s reason for existing:
`tests/test_at_scale_provisional_residue_probe.py`'s `TestMain` drives `main()`
end to end, and without an overridable path every one of those tests would
append to the committed `benchmark.md`.

### The pairing crumb

`append_ingestion_report` gains an optional `json_path` parameter, passed by
`run_ingestion_benchmark.main()` from the `write_json_result` return value it
already holds.

It renders as a **bullet beside the existing `- Repo:` line, not as a table
row**:

```
- Repo: `.` @ `master`
- Metrics JSON: `results/ingestion-20260817T041942Z.json`
```

Provenance is not a metric, and keeping it out of the table leaves the existing
row assertions in `tests/test_at_scale_report.py` untouched. The path is
rendered relative to the report's own directory (`evals/at_scale/`), falling
back to the absolute path when it is not relative to it. `json_path=None`
renders `not recorded`, so absence stays visible rather than becoming a missing
line.

With both halves in place the pairing is discoverable in both directions: the
ingestion section names its metrics JSON, and the residue section names the
metrics JSON it read.

## Testing

`tests/test_at_scale_report.py`:

- `append_residue_report`: clean verdict; the `M > N` failure verdict; the
  entity-type breakdown rendered and omitted-when-empty; and one case per
  defensively-absent key, asserting *not measured* and asserting the rendered
  text does **not** contain a bare `0` for that row.
- The crumb: present with a path relative to `evals/at_scale/`, present with an
  unrelated absolute path, and `None` rendering `not recorded`.
- The existing `append_query_report` tests updated to the new dict shape, plus
  the ingestion-health block rendered and absent.

`tests/test_at_scale_query_benchmark.py`:

- **One test per `_exit_code` clause**, each independently flipping the exit
  code from 0 to 1 with everything else clean: non-empty `skipped_commits`;
  non-empty `error_signals`; `stderr_capture_complete: False`;
  `final_status: "error"`. This proves the delegation rather than assuming it —
  a single "dirty metrics fail" test would pass even if only one clause were
  wired up.
- Query-side semantics preserved: all-pass is 0, any `passed is False` is 1,
  `passed is None` never counts, empty entries is 0.
- A missing `"ingestion"` key evaluates as clean.
- The three existing integration tests updated to read `report["entries"]`, and
  one extended to assert `report["ingestion"]` carries the health keys and does
  not carry `poll_offsets`.

## Documentation

`evals/at_scale/benchmark.md`'s explanatory notes describe the row set it
carries; they are updated to name the new residue section and the metrics-JSON
bullet, and to state that a run with no residue section means the probe was not
run — not that residue was zero.

## Out of scope

- Running the residue probe from the nightly workflow. It needs `--graph-path`
  and a persisted graph, and adding a third long step to a job that already
  budgets to GitHub's 360-minute ceiling is a separate decision.
- Persisting a query-benchmark results JSON to `results/`. The query benchmark
  has never written one; #275 asks for the health fields to reach its reported
  output, which stdout and `benchmark.md` both satisfy.
- Any change to `run_ingestion_benchmark._exit_code`'s clauses, to
  `stderr_capture.py`, or to the probe's own guards.
