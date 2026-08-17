# Benchmark Reporting: Ingestion Health and Residue Verdict — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the at-scale query benchmark fail on an unclean ingestion phase instead of discarding the evidence, and put the `M <= N` provisional-residue verdict into `benchmark.md` (issues #275 and #276).

**Architecture:** Three entry points feed one shared reporting module. `evals/at_scale/report.py` gains a residue appender and a metrics-JSON provenance bullet; `evals/at_scale/probe_provisional_residue.py` calls the new appender from its own process, preserving the separate-process isolation that makes it safe; `evals/at_scale/run_query_benchmark.py` stops dropping the ingestion metrics dict and delegates its health verdict to `run_ingestion_benchmark._exit_code` rather than recopying that function's clauses.

**Tech Stack:** Python 3.10+, pytest, pytest-asyncio. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-17-benchmark-reporting-health-and-residue-design.md`

## Global Constraints

- **ALWAYS run Python via `.venv/bin/python`.** The system interpreter has minigraf 1.1.1 against a `minigraf>=1.2.3` floor; it fakes ~122 test failures and makes queries ~7x slower. Every `pytest` invocation below means `.venv/bin/python -m pytest`.
- **Python 3.10 floor.** No `Path.relative_to(..., walk_up=True)` (3.12+), no `match` statements needed. All three touched modules already carry `from __future__ import annotations`, so `X | None` annotations are fine.
- **No closing keywords in commit messages.** Never write `closes #N`, `fixes #N`, `resolves #N` (or any negated form) in a commit message on this branch — GitHub auto-closes on merge regardless of intent, and the keyword/`#N` pair is matched across blank lines. Refer to issues as "issue 275" or "#275" without a keyword before it. The closing keywords go in the PR body only, at the end, and are verified with `gh pr view --json closingIssuesReferences` before merge.
- **`report.py`'s defensive-row convention is mandatory** for every new row: an absent key renders `not measured`, never `0`. See `_poll_duty_row` / `_checkpoint_duty_row` / `_stderr_capture_row` for the established shape and docstring style.
- **Branch:** `fix-275-276-benchmark-reporting`, already created, spec already committed at `b276422`.
- **Full suite baseline:** 1464 passed, 1 xfailed on master. The 1 xfail is #257's permanent guard and must stay xfailed.

---

### Task 1: Metrics-JSON provenance bullet in the ingestion section (#276, half A)

Adds the crumb that lets a reader of a `## Ingestion Run` section find the results JSON it was rendered from — which is the file the residue probe pairs against.

**Files:**
- Modify: `evals/at_scale/report.py` (add `_relative_to_report`, `_metrics_json_bullet`; change `append_ingestion_report`'s signature and `lines` list)
- Modify: `evals/at_scale/run_ingestion_benchmark.py:440` (pass the `json_path` `main()` already holds)
- Test: `tests/test_at_scale_report.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `_relative_to_report(path: Any, report_path: Path) -> str` — used again by Task 2.
  - `append_ingestion_report(metrics: dict[str, Any], report_path: Path, json_path: Path | None = None) -> None`.

- [ ] **Step 1: Write the failing tests**

Add `import pytest` to the top of `tests/test_at_scale_report.py` (it currently imports only `json`), and add this class immediately after `TestAppendIngestionReport`:

```python
class TestMetricsJsonBullet:
    """#276: a reader of an Ingestion Run section had no way to find the
    results JSON it was rendered from, and therefore no way to find the
    residue JSON that pairs with it. The bullet sits beside `- Repo:` rather
    than in the metrics table because it is provenance, not a measurement.
    """

    def test_records_the_metrics_json_relative_to_the_report(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        json_path = tmp_path / "results" / "ingestion-20260817T041942Z.json"
        append_ingestion_report(SAMPLE_METRICS, report_path, json_path)
        assert (
            "- Metrics JSON: `results/ingestion-20260817T041942Z.json`"
            in report_path.read_text()
        )

    def test_falls_back_to_the_absolute_path_when_not_under_the_report_dir(self, tmp_path):
        # An artifact copied off a run host, or a tmp_path in a test. An
        # absolute path is more useful to a human than a ../../.. chain.
        report_path = tmp_path / "report" / "benchmark.md"
        report_path.parent.mkdir()
        json_path = tmp_path / "elsewhere" / "ingestion-x.json"
        append_ingestion_report(SAMPLE_METRICS, report_path, json_path)
        assert f"- Metrics JSON: `{json_path.resolve()}`" in report_path.read_text()

    def test_absence_is_visible_rather_than_a_missing_line(self, tmp_path):
        # Same reason _poll_duty_row always emits: an omitted line is
        # invisible, and a reader cannot distinguish "not recorded" from
        # "nobody looked".
        report_path = tmp_path / "benchmark.md"
        append_ingestion_report(SAMPLE_METRICS, report_path)
        assert "- Metrics JSON: not recorded" in report_path.read_text()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py::TestMetricsJsonBullet -v`

Expected: FAIL — the first two with `TypeError: append_ingestion_report() takes 2 positional arguments but 3 were given`, the third with an `AssertionError` on the missing bullet.

- [ ] **Step 3: Implement the helpers**

In `evals/at_scale/report.py`, add after `_utc_timestamp` (before `write_json_result`):

```python
def _relative_to_report(path: Any, report_path: Path) -> str:
    """Render an artifact path relative to the report's own directory.

    benchmark.md lives at evals/at_scale/benchmark.md and the artifacts it
    names live at evals/at_scale/results/, so the useful rendering is
    `results/ingestion-<ts>.json`. Falls back to the absolute path when the
    two share no common root -- an artifact copied off a run host, or a
    tmp_path under test -- because a `../../../tmp/...` chain is worse than
    an absolute path for a human reader.

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
```

- [ ] **Step 4: Wire it into `append_ingestion_report`**

Change the signature and add the bullet:

```python
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
```

and in the `lines` list, insert immediately after the `- Repo:` line:

```python
        f"- Repo: `{metrics['repo_path']}` @ `{metrics['branch']}`",
        _metrics_json_bullet(json_path, report_path),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py -v`

Expected: PASS — the new class plus every pre-existing `TestAppendIngestionReport` test (the bullet is outside the metrics table, so no existing row assertion moves).

- [ ] **Step 6: Pass the path from the ingestion benchmark's `main()`**

In `evals/at_scale/run_ingestion_benchmark.py`, `main()` already binds `json_path` on the line above. Change line 440 from:

```python
    append_ingestion_report(metrics, report_path)
```

to:

```python
    append_ingestion_report(metrics, report_path, json_path)
```

- [ ] **Step 7: Verify the whole at-scale test surface is still green**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py tests/test_at_scale_ingestion_benchmark.py -v`

Expected: PASS, no errors.

- [ ] **Step 8: Commit**

```bash
git add evals/at_scale/report.py evals/at_scale/run_ingestion_benchmark.py tests/test_at_scale_report.py
git commit -m "Record each ingestion section's own results JSON in benchmark.md

Part of issue 276. A reader of an Ingestion Run section had no way to
find the machine-readable artifact it was rendered from, and therefore no
way to find the residue verdict that pairs with it.

The path is a parameter rather than a metrics key because
write_json_result only learns it by writing the file. It renders as a
bullet beside \`- Repo:\` rather than a table row -- provenance is not a
measurement -- and an absent path renders \"not recorded\" so its absence
stays visible.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `append_residue_report` (#276, half B)

The appender itself, in `report.py`. Pure function over a dict; no knowledge of the probe.

**Files:**
- Modify: `evals/at_scale/report.py` (add four row helpers and `append_residue_report`)
- Test: `tests/test_at_scale_report.py`

**Interfaces:**
- Consumes: `_relative_to_report(path, report_path) -> str` and `_REPORT_HEADER` / `_utc_timestamp()` from Task 1 and the existing file.
- Produces: `append_residue_report(result: dict[str, Any], report_path: Path, json_out_path: Path | None = None) -> None`. Task 3 calls it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_at_scale_report.py`, after `TestAppendQueryReport`. Add `append_residue_report` to the `report` import on line 3, and add `from pathlib import Path` at the top (the file currently imports only `json`, plus the `pytest` added in Task 1).

```python
# A verdict as probe_provisional_residue.main() builds it -- keys copied
# from the committed artifact at
# evals/at_scale/results/256-provisional-residue.json.
SAMPLE_RESIDUE = {
    "provisional_entities": 0,
    "sweep_skipped": 3,
    "ok": True,
    "interpretation": "M <= N: provisional residue is within the correction "
                      "sweep's own accounting.",
    "commits_in_graph": 732,
    "graph_path": "/tmp/bench/at-scale.graph",
    "metrics_json": "/repo/evals/at_scale/results/ingestion-20260816T194720Z.json",
    "breakdown_by_entity_type": {},
    "correction_sweep_summaries": [3],
}


class TestAppendResidueReport:
    """#276: benchmark.md carried the #256 capture-health rows but not the
    M <= N verdict those rows exist to qualify, so the durable human record
    said a run was clean in the capture sense while saying nothing about
    whether its provisional residue was accounted for.
    """

    def test_creates_report_with_header_if_missing(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        assert report_path.read_text().startswith("# At-Scale Code-Graph Benchmark")

    def test_a_clean_verdict_renders_the_numbers_and_the_reading(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        text = report_path.read_text()
        assert "## Provisional Residue" in text
        assert "| Verdict (#256) | OK -- M <= N" in text
        assert "| Provisional entities (M) | 0 |" in text
        assert "| Sweep skipped (N) | 3 |" in text
        assert "| Commits in graph | 732 |" in text

    def test_m_above_n_renders_as_a_failure_not_a_number(self, tmp_path):
        # The signature this probe exists to detect. It must not be possible
        # to skim the section and read it as ordinary.
        result = {**SAMPLE_RESIDUE, "provisional_entities": 7,
                  "sweep_skipped": 3, "ok": False}
        report_path = tmp_path / "benchmark.md"
        append_residue_report(result, report_path)
        text = report_path.read_text()
        assert "**FAILED**" in text
        assert "M > N" in text
        assert "#251" in text

    def test_second_call_appends_not_overwrites(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        append_residue_report(SAMPLE_RESIDUE, report_path)
        assert report_path.read_text().count("## Provisional Residue") == 2

    def test_a_populated_breakdown_is_rendered(self, tmp_path):
        result = {**SAMPLE_RESIDUE, "provisional_entities": 3,
                  "breakdown_by_entity_type": {"function": 2, "module": 1}}
        report_path = tmp_path / "benchmark.md"
        append_residue_report(result, report_path)
        assert "| Provisional by entity type | function: 2, module: 1 |" in \
            report_path.read_text()

    def test_an_empty_breakdown_renders_none_not_not_measured(self, tmp_path):
        # An empty dict is a MEASURED zero -- the healthy case. Only an
        # absent key is unmeasured, and conflating the two would make every
        # clean run look uninspected.
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        assert "| Provisional by entity type | none |" in report_path.read_text()

    @pytest.mark.parametrize(
        "key,label",
        [
            ("provisional_entities", "Provisional entities (M)"),
            ("sweep_skipped", "Sweep skipped (N)"),
            ("commits_in_graph", "Commits in graph"),
            ("breakdown_by_entity_type", "Provisional by entity type"),
        ],
    )
    def test_an_absent_key_renders_not_measured_and_never_zero(self, tmp_path, key, label):
        """report.py's standing convention (_poll_duty_row). It matters more
        here than anywhere else in the file: the nightly workflow does not run
        this probe, so most benchmark.md entries carry no residue section at
        all, and a re-rendered older artifact must not be able to manufacture
        a clean verdict out of missing keys.
        """
        result = {k: v for k, v in SAMPLE_RESIDUE.items() if k != key}
        report_path = tmp_path / "benchmark.md"
        append_residue_report(result, report_path)
        row = next(
            line for line in report_path.read_text().splitlines()
            if line.startswith(f"| {label} |")
        )
        assert "not measured" in row
        assert "| 0 |" not in row
        assert "| none |" not in row

    def test_an_absent_verdict_renders_not_measured(self, tmp_path):
        result = {k: v for k, v in SAMPLE_RESIDUE.items() if k != "ok"}
        report_path = tmp_path / "benchmark.md"
        append_residue_report(result, report_path)
        row = next(
            line for line in report_path.read_text().splitlines()
            if line.startswith("| Verdict (#256) |")
        )
        assert "not measured" in row
        assert "OK" not in row

    def test_the_three_artifact_paths_are_recorded(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        out_path = tmp_path / "results" / "256-provisional-residue.json"
        append_residue_report(SAMPLE_RESIDUE, report_path, out_path)
        text = report_path.read_text()
        # Resolved on both sides: _relative_to_report resolves its input, and
        # /tmp is a symlink on some platforms.
        assert f"| Graph | `{Path('/tmp/bench/at-scale.graph').resolve()}` |" in text
        assert "ingestion-20260816T194720Z.json" in text
        assert "| Residue JSON | `results/256-provisional-residue.json` |" in text

    def test_an_absent_residue_json_path_renders_not_recorded(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_residue_report(SAMPLE_RESIDUE, report_path)
        assert "| Residue JSON | not recorded |" in report_path.read_text()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py::TestAppendResidueReport -v`

Expected: FAIL at import with `ImportError: cannot import name 'append_residue_report' from 'evals.at_scale.report'`.

- [ ] **Step 3: Implement the row helpers**

In `evals/at_scale/report.py`, add after `_error_signals_row`:

```python
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
```

- [ ] **Step 4: Implement `append_residue_report`**

Add after `append_query_report`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py -v`

Expected: PASS, all of `TestAppendResidueReport` plus every pre-existing test in the file.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/report.py tests/test_at_scale_report.py
git commit -m "Add append_residue_report, the M <= N verdict's benchmark.md section

Part of issue 276. The verdict lived only in
results/256-provisional-residue.json, so the durable human record said a
run was clean in the capture sense while saying nothing about whether its
provisional residue was accounted for.

A second appender rather than extra rows in the ingestion table: the probe
is a separate process by design (it opens the graph with no other handle
live), so append_ingestion_report has no access to these numbers at the
time it runs. report.py consumes a plain dict and learns nothing about the
probe.

The breakdown row distinguishes three states rather than two -- absent is
unmeasured, an empty dict is a measured zero, populated names where the
residue sits -- and every other row follows the _poll_duty_row convention
that an absent key never renders as 0. That matters more here than
elsewhere in the file: the nightly does not run this probe, so most
entries will carry no residue section at all.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Wire the probe's `main()` to the appender (#276)

**Files:**
- Modify: `evals/at_scale/probe_provisional_residue.py` (`main()` — add `--report-path`, call the appender)
- Test: `tests/test_at_scale_provisional_residue_probe.py`

**Interfaces:**
- Consumes: `append_residue_report(result, report_path, json_out_path)` from Task 2.
- Produces: `probe_provisional_residue.main(argv)` now accepts `--report-path PATH`, defaulting to `evals/at_scale/benchmark.md`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_at_scale_provisional_residue_probe.py`, first change `TestMain._run` so **every** test in the class writes its report to `tmp_path` — without this, running the suite appends to the committed `benchmark.md`:

```python
    @staticmethod
    def _run(graph, metrics, out, extra=()):
        metrics_path = out.parent / "metrics.json"
        metrics_path.write_text(json.dumps(metrics))
        return main(
            [
                "--graph-path", str(graph),
                "--metrics-json", str(metrics_path),
                "--json-out", str(out),
                # Never the committed evals/at_scale/benchmark.md: main()
                # appends unconditionally, so the default would make every
                # test in this class edit a tracked file.
                "--report-path", str(out.parent / "benchmark.md"),
                *extra,
            ]
        )
```

Then add to `TestMain`:

```python
    def test_a_verdict_is_appended_to_the_report(self, tmp_path):
        """#276: the verdict has to reach the durable human record, not only
        the results JSON that nothing in benchmark.md names."""
        graph = tmp_path / "reported.graph"
        _build_graph(graph, commits=3, provisional=[":function/alpha"])
        out = tmp_path / "verdict.json"

        assert self._run(graph, _metrics(), out) == 0

        text = (tmp_path / "benchmark.md").read_text()
        assert "## Provisional Residue" in text
        assert "| Verdict (#256) | OK -- M <= N" in text
        assert "| Provisional entities (M) | 1 |" in text
        assert "| Commits in graph | 3 |" in text
        assert "verdict.json" in text

    def test_a_failing_verdict_is_appended_too(self, tmp_path):
        """The case the record most needs. An M > N run must not be the one
        that quietly leaves no trace."""
        graph = tmp_path / "residue.graph"
        _build_graph(graph, commits=3,
                     provisional=[":function/a", ":function/b", ":function/c"])
        out = tmp_path / "verdict.json"

        assert self._run(graph, _metrics(correction_sweep_skipped=1), out) == 1
        assert "**FAILED**" in (tmp_path / "benchmark.md").read_text()

    def test_a_refused_run_appends_nothing(self, tmp_path):
        """The guards raise before any measurement exists. A section rendered
        from a refusal would be a verdict about a graph the probe declined to
        read."""
        out = tmp_path / "verdict.json"
        with pytest.raises(SystemExit):
            self._run(tmp_path / "typo-in-this-name.graph", _metrics(), out)
        assert not (tmp_path / "benchmark.md").exists()

    def test_the_default_report_path_is_the_committed_benchmark_md(self, tmp_path):
        """--report-path exists for the tests; the default must still be the
        real record, or a genuine run would write its verdict nowhere."""
        from evals.at_scale.probe_provisional_residue import _DEFAULT_REPORT_PATH

        assert _DEFAULT_REPORT_PATH.name == "benchmark.md"
        assert _DEFAULT_REPORT_PATH.parent.name == "at_scale"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_at_scale_provisional_residue_probe.py::TestMain -v`

Expected: FAIL — the four new tests plus every pre-existing `TestMain` test, all from `error: unrecognized arguments: --report-path` (argparse exits 2 via `SystemExit`).

**One exception, and it is worth understanding rather than dismissing:** `test_a_nonexistent_graph_path_never_reports_ok` will still PASS at this step. It catches `SystemExit` and only asserts `exc.code != 0`, which argparse's exit 2 satisfies. That is the test doing its job — it is a property assertion, not a mechanism assertion — but it means it proves nothing about this step. Do not read its green as evidence the argument exists.

- [ ] **Step 3: Add the argument and the call**

In `evals/at_scale/probe_provisional_residue.py`, add beside `_DEFAULT_JSON_OUT`:

```python
_DEFAULT_REPORT_PATH = REPO_ROOT / "evals" / "at_scale" / "benchmark.md"
```

Add the argument in `main()`, after `--json-out`:

```python
    parser.add_argument(
        "--report-path", default=str(_DEFAULT_REPORT_PATH),
        help="benchmark.md to append the verdict section to (#276). "
             "Overridable for the same reason --json-out is: the default is "
             "a tracked file, and the tests drive main() end to end.",
    )
```

Then replace the tail of `main()`, from `out_path = Path(args.json_out)` onward, with:

```python
    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n")

    # After the JSON, and only on a path that reached a verdict: every guard
    # above raises SystemExit before `result` exists, and a section rendered
    # from a refusal would be a verdict about a graph the probe declined to
    # read. A FAILING verdict is appended too -- that is the one the record
    # most needs (#276).
    from evals.at_scale.report import append_residue_report

    report_path = Path(args.report_path)
    append_residue_report(result, report_path, out_path)

    print(json.dumps(result, indent=2))
    print(f"\nWrote {out_path}")
    print(f"Appended to {report_path}")
    return 0 if result["ok"] else 1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_at_scale_provisional_residue_probe.py -v`

Expected: PASS, all tests including the pre-existing `TestMain` ones.

- [ ] **Step 5: Verify the committed benchmark.md was not touched**

Run: `git status --short evals/at_scale/benchmark.md`

Expected: **empty output.** If `benchmark.md` shows as modified, the `_run` helper change in Step 1 did not take effect and the test run appended to the tracked file — `git checkout evals/at_scale/benchmark.md` and fix the helper before continuing.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_provisional_residue.py tests/test_at_scale_provisional_residue_probe.py
git commit -m "Have the residue probe append its verdict to benchmark.md

Part of issue 276. The probe writes its section itself, from its own
process, which is what keeps the separate-process isolation intact.

--report-path exists for the same reason --json-out does: the default is a
tracked file and TestMain drives main() end to end, so without it every
test in that class would append to the committed benchmark.md. The append
sits after the JSON write and after every guard, so a refused run appends
nothing -- but a FAILING verdict is appended, since that is the one the
record most needs.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The query benchmark keeps and judges its ingestion metrics (#275)

The core of #275. After this task the return shape has changed and the exit code is honest; `append_query_report` still takes the entries list, so nothing is left broken.

**Files:**
- Modify: `evals/at_scale/run_query_benchmark.py` (imports, `run_query_benchmark`, new `_ingestion_health`, `_exit_code`, `main()`)
- Test: `tests/test_at_scale_query_benchmark.py`

**Interfaces:**
- Consumes: `run_ingestion_benchmark._exit_code(metrics: dict) -> int` (existing, unchanged).
- Produces:
  - `run_query_benchmark(repo_path: str, graph_path: Path, ground_truth_path: Path) -> dict[str, Any]` with keys `"entries"` (the old list) and `"ingestion"` (the metrics dict minus `poll_offsets`).
  - `_ingestion_health(metrics: dict[str, Any]) -> dict[str, Any]`.
  - `_exit_code(report: dict[str, Any]) -> int`. Task 5 does not use it; the plan's final verification does.

- [ ] **Step 1: Write the failing tests**

Replace `TestExitCode` in `tests/test_at_scale_query_benchmark.py` with:

```python
CLEAN_INGESTION = {
    "final_status": "complete",
    "commits_ingested": 2,
    "skipped_commits": [],
    "error_signals": [],
    "stderr_capture_complete": True,
}


def _report(entries, **ingestion_overrides):
    return {
        "entries": entries,
        "ingestion": {**CLEAN_INGESTION, **ingestion_overrides},
    }


class TestExitCode:
    def test_zero_when_all_scored_entries_pass_and_ingestion_was_clean(self):
        entries = [
            {"id": 1, "passed": True},
            {"id": 2, "passed": None},
            {"id": 3, "passed": True},
        ]
        assert _exit_code(_report(entries)) == 0

    def test_nonzero_when_any_entry_fails(self):
        entries = [{"id": 1, "passed": True}, {"id": 2, "passed": False}]
        assert _exit_code(_report(entries)) == 1

    def test_zero_for_empty_entries(self):
        assert _exit_code(_report([])) == 0

    def test_an_absent_ingestion_key_evaluates_as_clean(self):
        # Matching run_ingestion_benchmark._exit_code's posture toward inputs
        # that predate an instrument: unknown is not failure.
        assert _exit_code({"entries": [{"id": 1, "passed": True}]}) == 0

    @pytest.mark.parametrize(
        "override",
        [
            {"skipped_commits": ["deadbee1"]},
            {"error_signals": [{"pattern": "page_out_of_bounds"}]},
            {"stderr_capture_complete": False},
            {"final_status": "error"},
        ],
        ids=["dropped-commit", "error-signature", "truncated-capture", "errored-run"],
    )
    def test_each_unclean_ingestion_clause_fails_the_run_on_its_own(self, override):
        """#275: the fail-open. Every query entry passes here; the graph those
        latencies were measured over is the thing that is wrong.

        One test PER CLAUSE, not one "dirty metrics" test: a single case would
        pass with only one clause wired up, and the whole point of delegating
        to run_ingestion_benchmark._exit_code is that all of them arrive.
        """
        entries = [{"id": 1, "passed": True}]
        assert _exit_code(_report(entries)) == 0, "positive control"
        assert _exit_code(_report(entries, **override)) == 1
```

Then add to `TestRunQueryBenchmark`:

```python
    @pytest.mark.asyncio
    async def test_the_report_keeps_the_ingestion_metrics(self, git_repo, tmp_path, tiny_ground_truth):
        """#275: these were dropped on the floor. They are the only evidence
        that the graph the query numbers were measured over was built
        cleanly -- `processed` increments on the skip paths too and
        final_status stays "complete", so neither can see a dropped commit.
        """
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        ingestion = report["ingestion"]
        for key in (
            "skipped_commits", "error_signals", "stderr_capture_complete",
            "final_status", "commits_ingested",
        ):
            assert key in ingestion, key

    @pytest.mark.asyncio
    async def test_the_kept_metrics_exclude_only_poll_offsets(self, git_repo, tmp_path, tiny_ground_truth):
        """The one exclusion, and it must stay the only one: a hand-picked
        subset would drift the moment _exit_code grows a clause."""
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        assert "poll_offsets" not in report["ingestion"]
        assert "poll_duty_fraction" in report["ingestion"]
```

Update the three existing `TestRunQueryBenchmark` tests to read `report["entries"]`:

```python
    @pytest.mark.asyncio
    async def test_all_entries_pass_against_matching_fixture(self, git_repo, tmp_path, tiny_ground_truth):
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        assert len(report["entries"]) == 2
        assert all(r["passed"] for r in report["entries"])

    @pytest.mark.asyncio
    async def test_result_includes_latency_fields(self, git_repo, tmp_path, tiny_ground_truth):
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        for r in report["entries"]:
            assert r["minigraf_latency_seconds"] >= 0
            assert r["baseline_latency_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_mismatch_reports_failure(self, git_repo, tmp_path, tiny_ground_truth):
        gt = json.loads(tiny_ground_truth.read_text())
        gt["entries"][0]["expected"] = [[999]]
        tiny_ground_truth.write_text(json.dumps(gt))
        graph_path = tmp_path / "bench.graph"
        report = await run_query_benchmark(str(git_repo), graph_path, tiny_ground_truth)
        entries = report["entries"]
        assert entries[0]["passed"] is False
        assert entries[0]["actual"] != entries[0]["expected"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_at_scale_query_benchmark.py -v`

Expected: FAIL — the `TestExitCode` cases with `AttributeError: 'str' object has no attribute 'get'` (the current `_exit_code` iterates its argument, which over a dict yields its key strings), and the `TestRunQueryBenchmark` cases with `TypeError: list indices must be integers or slices, not str`.

- [ ] **Step 3: Implement the health projection and the return shape**

In `evals/at_scale/run_query_benchmark.py`, change the import at line 23:

```python
from evals.at_scale.run_ingestion_benchmark import (  # noqa: E402
    _exit_code as _ingestion_exit_code,
    run_ingestion_benchmark,
)
```

Add above `run_query_benchmark`:

```python
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
```

Change the signature and the ingestion call:

```python
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
```

and replace the final `return results` with:

```python
    return {"entries": results, "ingestion": _ingestion_health(metrics)}
```

- [ ] **Step 4: Implement the exit code**

Replace `_exit_code` entirely:

```python
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
```

- [ ] **Step 5: Update `main()` to the new shape**

In `main()`, rename the binding and pass the entries list to the still-unchanged appender (Task 5 changes the appender):

```python
        report = asyncio.run(
            run_query_benchmark(args.repo_path, graph_path, Path(args.ground_truth))
        )

    from evals.at_scale.report import append_query_report

    report_path = REPO_ROOT / "evals" / "at_scale" / "benchmark.md"
    append_query_report(report["entries"], report_path)
    print(json.dumps(report, indent=2))
    print(f"\nAppended to {report_path}")
    return _exit_code(report)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_at_scale_query_benchmark.py -v`

Expected: PASS, all tests.

- [ ] **Step 7: Commit**

```bash
git add evals/at_scale/run_query_benchmark.py tests/test_at_scale_query_benchmark.py
git commit -m "Judge the query benchmark's own ingestion phase instead of discarding it

Part of issue 275. run_query_benchmark called run_ingestion_benchmark and
dropped the return value, which since #256 carries the only evidence that
its ingestion phase was clean -- skipped_commits, error_signals and
stderr_capture_complete are not derivable from anything else, because
_ingest_progress[\"processed\"] increments on the skip paths and
final_status stays \"complete\" by design.

The whole metrics dict is carried forward, minus poll_offsets alone: it is
unbounded and it is the only key _exit_code does not read. A hand-picked
health subset would go stale the first time _exit_code grows a clause,
which is the fail-open this closes.

Tested one clause at a time. A single dirty-metrics case would pass with
only one clause wired up.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: The query section reports ingestion health (#275)

**Files:**
- Modify: `evals/at_scale/report.py` (`append_query_report`, new `_query_ingestion_block`)
- Modify: `evals/at_scale/run_query_benchmark.py` (`main()` — pass the whole report)
- Test: `tests/test_at_scale_report.py`

**Interfaces:**
- Consumes: the `{"entries": [...], "ingestion": {...}}` shape from Task 4; the existing `_stderr_capture_row`, `_skipped_commits_row`, `_error_signals_row`.
- Produces: `append_query_report(report: dict[str, Any], report_path: Path) -> None` (signature change from `list`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_at_scale_report.py`, replace the `SAMPLE_QUERY_RESULTS` list with a report dict and rewrite `TestAppendQueryReport`:

```python
SAMPLE_QUERY_REPORT = {
    "entries": [
        {
            "id": 1, "category": "point-in-time", "passed": True,
            "actual": [[8]], "expected": [[8]],
            "minigraf_latency_seconds": 0.003, "baseline_latency_seconds": 0.015,
        },
        {
            "id": 2, "category": "delta", "passed": None,
            "actual": None, "expected": None,
            "minigraf_latency_seconds": 0.0, "baseline_latency_seconds": 0.0,
        },
    ],
    "ingestion": {
        "commits_ingested": 12, "final_status": "complete",
        "stderr_capture_complete": True, "skipped_commits": [], "error_signals": [],
    },
}


class TestAppendQueryReport:
    def test_creates_report_with_header_if_missing(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_REPORT, report_path)
        assert report_path.read_text().startswith("# At-Scale Code-Graph Benchmark")

    def test_reports_pass_fail_and_skipped(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_REPORT, report_path)
        text = report_path.read_text()
        assert "## Query Correctness Run" in text
        assert "PASS" in text
        assert "SKIPPED (manual diff)" in text

    def test_a_clean_ingestion_phase_is_reported(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report(SAMPLE_QUERY_REPORT, report_path)
        text = report_path.read_text()
        assert "Ingestion phase (#275)" in text
        assert "| Commits ingested | 12 |" in text
        assert "| Stderr capture (#256) | complete |" in text
        assert "| Commits dropped (#256) | 0 |" in text
        assert "| Error signatures (#251/#256) | 0 |" in text

    def test_a_dirty_ingestion_phase_is_visible_beside_the_latencies(self, tmp_path):
        """#275: the latencies are the reason this matters. Query numbers
        measured over a graph that silently lost commits are not comparable to
        ones that were not, and the section used to render them identically.
        """
        report = {
            **SAMPLE_QUERY_REPORT,
            "ingestion": {
                **SAMPLE_QUERY_REPORT["ingestion"],
                "skipped_commits": ["deadbee1", "cafe002"],
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_query_report(report, report_path)
        text = report_path.read_text()
        assert "| Commits dropped (#256) | **2**" in text
        assert "deadbee1" in text

    def test_a_truncated_capture_is_flagged_in_the_query_section_too(self, tmp_path):
        # Rendered by the SAME helper the ingestion section uses, so the two
        # cannot disagree about how a dirty run reads.
        report = {
            **SAMPLE_QUERY_REPORT,
            "ingestion": {
                **SAMPLE_QUERY_REPORT["ingestion"],
                "stderr_capture_complete": False,
                "tee_failure": "TeeStderrFailure('pump did not complete cleanly')",
            },
        }
        report_path = tmp_path / "benchmark.md"
        append_query_report(report, report_path)
        text = report_path.read_text()
        assert "INCOMPLETE" in text
        assert "LOWER BOUNDS" in text

    def test_an_absent_ingestion_key_renders_not_measured(self, tmp_path):
        report_path = tmp_path / "benchmark.md"
        append_query_report({"entries": SAMPLE_QUERY_REPORT["entries"]}, report_path)
        text = report_path.read_text()
        assert "Ingestion phase (#275)" in text
        assert "not measured" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py::TestAppendQueryReport -v`

Expected: FAIL with `TypeError: string indices must be integers` — `append_query_report` iterates its argument, which over a dict yields the key strings `"entries"` and `"ingestion"`, and then subscripts one with `["passed"]`.

- [ ] **Step 3: Implement the ingestion block**

In `evals/at_scale/report.py`, add before `append_query_report`:

```python
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
```

- [ ] **Step 4: Change `append_query_report` to the new shape**

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py -v`

Expected: PASS, all tests.

- [ ] **Step 6: Pass the whole report from `main()`**

In `evals/at_scale/run_query_benchmark.py`, `main()`, change:

```python
    append_query_report(report["entries"], report_path)
```

to:

```python
    append_query_report(report, report_path)
```

- [ ] **Step 7: Run both at-scale benchmark test files**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py tests/test_at_scale_query_benchmark.py -v`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add evals/at_scale/report.py evals/at_scale/run_query_benchmark.py tests/test_at_scale_report.py
git commit -m "Render the query benchmark's ingestion health beside its latencies

Part of issue 275. A Query Correctness Run section showed a clean sweep of
latencies whether or not the graph they were measured over had dropped
commits.

The block reuses _stderr_capture_row, _skipped_commits_row and
_error_signals_row verbatim rather than re-rendering the same keys, so an
Ingestion Run section and a Query Correctness Run section cannot disagree
about how a dirty run reads.

append_query_report takes the report dict now. No back-compatibility with
the bare list: query results have never been persisted as JSON artifacts,
so unlike the ingestion metrics files there is no historical input to
re-render.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Documentation and whole-branch verification

**Files:**
- Modify: `evals/at_scale/benchmark.md` (the explanatory notes at the top, before the first `## Ingestion Run` section)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing further depends on this.

- [ ] **Step 1: Add the reader's note to `benchmark.md`**

Insert immediately after the existing `> **Cross-day full-history wall-clock is not reliable...**` blockquote paragraph and before `## Ingestion Run — 20260719T074053Z`:

```markdown
> **2026-08-17 — what a section does and does not tell you (#275, #276).**
> Ingestion Run sections from this date onward carry a `- Metrics JSON:`
> bullet naming the results file they were rendered from; earlier entries
> render `not recorded`, and their JSON has to be matched by timestamp.
>
> **A `## Provisional Residue` section is the `M <= N` verdict (#256), and it
> is written by `probe_provisional_residue.py`, not by the benchmark.** The
> nightly workflow does NOT run that probe — it needs a persisted graph via
> `--graph-path` — so most Ingestion Run sections have no residue section
> beside them. **Read that absence as "the probe was not run", never as "the
> residue was zero".** The comparison is `M <= N`, not `M == N` or `M == 0`: a
> non-empty residue is the correction sweep's documented fail-safe, and N
> counts entities left provisional *or* unreconciled, so M is a strict subset.
>
> **Query Correctness Run sections from this date onward carry an "Ingestion
> phase" block.** Every earlier query entry was measured over a graph whose
> ingestion metrics were discarded, so nothing is known about whether it
> dropped commits — `Final status | complete` and the commit count are both
> blind to that by design. Query latencies measured over a graph that silently
> lost commits are not comparable to ones that were not.
```

- [ ] **Step 2: Check for other docs that need syncing**

Run: `grep -rn "append_query_report\|run_query_benchmark\|append_ingestion_report" SKILL.md CLAUDE.md docs/ --include=*.md | grep -v docs/superpowers/`

Expected: no hits outside `docs/superpowers/` (plans and specs are historical records and are not retro-edited). If `SKILL.md` or `CLAUDE.md` describe these signatures, update them; neither is expected to.

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`

Expected: **1464 + the new tests passed, 1 xfailed, 0 failed.** The 1 xfail is `TestPreloadKnownEntitiesDescriptionValueIsDateBounded` (#257's permanent guard) and must stay xfailed — if it reports as xpassed, something changed the preload and that is a real failure.

- [ ] **Step 4: Confirm the committed benchmark.md carries only the intended edit**

Run: `git diff --stat evals/at_scale/benchmark.md`

Expected: only the reader's note from Step 1 — no appended `## Provisional Residue` or `## Query Correctness Run` sections. A test run that leaked into the tracked file shows up here.

- [ ] **Step 5: Scan every commit on the branch for closing keywords**

Run: `git log master..HEAD --format='%B' | grep -inE '(close[sd]?|fix(e[sd])?|resolve[sd]?)[[:space:]]*:?[[:space:]]*#?[0-9]+'`

Expected: **no output.** A hit means a commit message will auto-close an issue on merge regardless of the sentence's intent — rewrite the message before pushing. Re-run this after any commit added later in the branch's life, not just once.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/benchmark.md
git commit -m "Tell benchmark.md readers what an absent residue section means

Part of issues 275 and 276. The nightly does not run the residue probe, so
most entries will have no Provisional Residue section beside them, and
that absence must not read as a measured zero. Also records that Query
Correctness sections before today were measured over a graph whose
ingestion health was discarded.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification before the PR

Beyond the per-task gates:

1. **Ablation on the delegation (Task 4).** The suite's per-clause tests prove the clauses arrive; confirm the delegation is real rather than coincidental by temporarily replacing `_ingestion_exit_code(ingestion)` with `0` in `run_query_benchmark._exit_code` and re-running `pytest tests/test_at_scale_query_benchmark.py::TestExitCode -v`. Expected: **all four clause cases fail.** Revert the edit. A guard is not a guard until you have watched it fail.
2. **A real end-to-end query-benchmark run is not required** and should not be attempted casually: it ingests this repo's full history through the pinned ground-truth commit and takes tens of minutes. The nightly workflow exercises it.
3. **The residue probe's end-to-end path is covered** by `TestMain`, which builds real graphs — no manual run needed.
4. **PR body carries the closing keywords**, not any commit message: `Closes #275` and `Closes #276` at the end. Verify with `gh pr view --json closingIssuesReferences` before merging, and confirm no other issue number appears in that list.
