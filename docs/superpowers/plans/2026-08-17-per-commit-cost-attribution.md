# Per-commit ingestion cost attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether ingestion's per-commit cost genuinely grows with history length once per-commit work size is controlled for, and decide #260 on the result.

**Architecture:** An env-gated hook in `_run_ingestion`'s per-commit loop appends one JSONL record per commit (timings + `len()`-based work counters + checkpoint-counter deltas). `run_ingestion_benchmark.py` gains a `--trace-path` that arms it. A pure analysis module fits `apply_s = a + b·W` within each third of processed order; a probe drives the run, applies a pre-registered control gate and verdict, and writes an artifact plus a `benchmark.md` entry.

**Tech Stack:** Python 3.10+, `minigraf>=1.2.3`, pytest, stdlib only (the OLS fit is ~15 lines — no numpy/scipy dependency is added).

**Spec:** `docs/superpowers/specs/2026-08-17-per-commit-cost-attribution-design.md`. Read it before Task 5; the pre-registered block is normative and must be copied verbatim, not re-derived.

## Global Constraints

- **ALWAYS use `.venv/bin/python`.** System `python` carries minigraf 1.1.1 against this project's `>=1.2.3` floor, where these queries run ~7x slower. That misconfiguration has already produced a retracted diagnosis on #239 and a fabricated test baseline. Every command in this plan uses `.venv/bin/python`.
- **Real backend, always.** No `MagicMock` fake of `MiniGrafDb`. See `docs/testing-conventions.md`. Use the `real_db` fixture for in-memory, or a file-backed `MiniGrafDb.open()` against `tmp_path` for anything needing persistence across opens.
- **Never hold a graph handle past its lease.** `with db_lease() as db:` / `async with db_lease_async() as db:`, and call `mcp_server._reset_db_state()` at test teardown. A grep guard enforces that tests never assign `mcp_server._db`.
- **The hook must add no `await`, take no lock, and not touch the lease.** Timing and `len()` only. The per-commit loop is the most dangerous place in `mcp_server.py` to change handle lifetime (#251/#253); read the invariant comment above `_db_native_lock` before editing there.
- **No closing keywords for #260 in any commit message or PR body.** `Refs #260` only. This project has had three auto-close incidents; the keyword/`#N` pair is scanned in both commit messages and the PR body, and a negated or deliberating sentence still closes. Verify with `gh pr view --json closingIssuesReferences` before any merge.
- **Fresh graph path, always.** Never re-ingest into an existing graph, its `.wal`, or its fact index. `resolve_graph_path` already enforces this; reuse it.
- **Pre-registered constants are frozen.** Once Task 5 lands, `W`, the thresholds, and the control gate may not be changed in response to data. If a run suggests they were wrong, fork the experiment — do not re-baseline it (`probe_ident_collision_census.py`'s frozen `PREDICTIONS` exists for this reason).

## File Structure

| File | Responsibility |
|---|---|
| `mcp_server.py` (modify) | `_trace_work_counters`, `_IngestTrace`, `_ingest_trace_from_env`, and the loop wiring. Instrumentation only. |
| `evals/at_scale/run_ingestion_benchmark.py` (modify) | `--trace-path` CLI flag; arms the env var, records the path in metrics. |
| `evals/at_scale/trace_fit.py` (create) | Pure analysis: the frozen pre-registered constants, the OLS fit, the thirds split, the control gate, the verdict. No I/O. |
| `evals/at_scale/probe_per_commit_cost.py` (create) | CLI: read a trace + metrics, call `trace_fit`, write the artifact. |
| `evals/at_scale/report.py` (modify) | `append_trace_fit_report` — renders the verdict into `benchmark.md`. |
| `tests/test_mcp_server.py` (modify) | Tasks 1–3 tests. |
| `tests/test_at_scale_ingestion_benchmark.py` (modify) | Task 4 tests. |
| `tests/test_at_scale_trace_fit.py` (create) | Tasks 5–6 tests — pure, fast, no DB. |
| `tests/test_at_scale_report.py` (modify) | Task 7 tests. |
| `evals/at_scale/benchmark.md` (modify) | Reader's note + the run's entry. |
| `evals/at_scale/results/260-per-commit-cost-attribution.json` (create) | The result artifact. |
| `CLAUDE.md` (modify) | Documents `MINIGRAF_INGEST_TRACE_PATH`. |

`trace_fit.py` is deliberately separate from the probe. The fit is where every pre-registered decision lives and where the ablations must bite; keeping it pure and I/O-free is what makes it testable in milliseconds against synthetic traces instead of behind a 30-minute ingestion.

---

### Task 1: `_trace_work_counters` — work-size extraction

**Files:**
- Modify: `mcp_server.py` (new function, place immediately above `_checkpoint_duty_from_env`, ~line 3660)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_trace_work_counters(extracted_files: Sequence[tuple]) -> Dict[str, Any]`, returning keys `files_by_status` (dict), `n_modules`, `n_functions`, `n_classes`, `n_globals`, `n_fields`, `n_imports_total`, `n_imports_resolved`, `n_unchanged_idents`, `idents_considered` — all `int` except `files_by_status`. Task 2 embeds this dict into each record.

**Background the implementer needs.** `extracted_files` is `_extract_commit`'s `file_results`: one `(status, file_path, extracted, precomputed, old_path)` tuple per changed file (see its docstring at `mcp_server.py:8963-8975`). `status` is one of `A`/`M`/`D`/`R`. For a `D` (deleted) file, `extracted` and `precomputed` are **both `None`** — the main thread only needs the path to know what to close. `precomputed` is `_precompute_file_triples`' return dict (`mcp_server.py:7605-7614`), whose relevant keys are `function_entries`, `class_entries`, `global_entries`, `field_entries` (lists of tuples), `resolved_imports` (list of `(import_name, dep_ident, is_resolved)`), and `unchanged_idents` (a set).

`idents_considered` is the spec's frozen `W`: one module ident per file that has a `precomputed`, plus one per extracted entity, plus one per **resolved** import.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class TestTraceWorkCounters:
    """#260: _trace_work_counters turns _extract_commit's file_results into the
    work-size counters the per-commit trace records."""

    @staticmethod
    def _precomputed(functions=0, classes=0, globals_=0, fields=0,
                     imports=(), unchanged=()):
        """A _precompute_file_triples-shaped dict with the given cardinalities.

        Only lengths matter to the counters, so the entries are placeholders of
        the right arity: (ident, name, candidate_triples) for entity lists and
        (import_name, dep_ident, is_resolved) for resolved_imports.
        """
        return {
            "module_ident": ":module/x",
            "function_entries": [(f":function/f{i}", f"f{i}", []) for i in range(functions)],
            "class_entries": [(f":class/c{i}", f"c{i}", []) for i in range(classes)],
            "global_entries": [(f":variable/g{i}", f"g{i}", []) for i in range(globals_)],
            "field_entries": [(f":field/d{i}", f"d{i}", []) for i in range(fields)],
            "resolved_imports": list(imports),
            "unchanged_idents": set(unchanged),
        }

    def test_counts_entities_and_module_across_files(self):
        files = [
            ("A", "a.py", {}, self._precomputed(functions=3, classes=1), ""),
            ("M", "b.py", {}, self._precomputed(functions=2, globals_=4, fields=5), ""),
        ]
        c = mcp_server._trace_work_counters(files)
        assert c["n_modules"] == 2
        assert c["n_functions"] == 5
        assert c["n_classes"] == 1
        assert c["n_globals"] == 4
        assert c["n_fields"] == 5
        assert c["files_by_status"] == {"A": 1, "M": 1}

    def test_deleted_file_has_no_precomputed_and_contributes_no_module(self):
        """A 'D' entry carries extracted=None and precomputed=None. It must count
        as a touched file but must NOT contribute a module ident -- there is no
        module being introduced, and counting one would inflate W on exactly the
        commits that do the least entity work."""
        files = [
            ("D", "gone.py", None, None, ""),
            ("M", "b.py", {}, self._precomputed(functions=1), ""),
        ]
        c = mcp_server._trace_work_counters(files)
        assert c["files_by_status"] == {"D": 1, "M": 1}
        assert c["n_modules"] == 1
        assert c["idents_considered"] == 2  # 1 module + 1 function

    def test_imports_split_resolved_from_total(self):
        files = [("M", "b.py", {}, self._precomputed(imports=[
            ("os", ":module/os", False),
            ("pkg.mod", ":module/pkg-mod", True),
            ("other", ":module/other", True),
        ]), "")]
        c = mcp_server._trace_work_counters(files)
        assert c["n_imports_total"] == 3
        assert c["n_imports_resolved"] == 2

    def test_idents_considered_is_the_frozen_W_formula(self):
        """W = sum over files (1 + functions + classes + globals + fields)
        + resolved imports. Pinned as an explicit arithmetic identity, not as a
        recomputation of the implementation -- this is the spec's frozen work
        metric and a drift here silently redefines the whole experiment."""
        files = [
            ("A", "a.py", {}, self._precomputed(functions=3, classes=2), ""),
            ("M", "b.py", {}, self._precomputed(globals_=1, fields=4, imports=[
                ("x", ":module/x", True), ("y", ":module/y", False),
            ]), ""),
        ]
        c = mcp_server._trace_work_counters(files)
        assert c["idents_considered"] == (1 + 3 + 2) + (1 + 1 + 4) + 1
        assert c["idents_considered"] == (
            c["n_modules"] + c["n_functions"] + c["n_classes"]
            + c["n_globals"] + c["n_fields"] + c["n_imports_resolved"]
        )

    def test_unchanged_idents_counted_but_excluded_from_W(self):
        """unchanged_idents is #221's body-diff narrowing. It is exploratory
        signal in the trace, NOT part of W -- W counts idents CONSIDERED, and an
        unchanged ident is still considered before being narrowed out."""
        files = [("M", "b.py", {}, self._precomputed(
            functions=4, unchanged=[":function/a", ":function/b"]), "")]
        c = mcp_server._trace_work_counters(files)
        assert c["n_unchanged_idents"] == 2
        assert c["idents_considered"] == 5  # 1 module + 4 functions, unchanged not subtracted

    def test_empty_file_list_is_all_zeros(self):
        c = mcp_server._trace_work_counters([])
        assert c["idents_considered"] == 0
        assert c["files_by_status"] == {}

    def test_renamed_file_counts_by_its_own_status(self):
        files = [("R", "new.py", {}, self._precomputed(functions=2), "old.py")]
        c = mcp_server._trace_work_counters(files)
        assert c["files_by_status"] == {"R": 1}
        assert c["idents_considered"] == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestTraceWorkCounters -v`
Expected: all 7 FAIL with `AttributeError: module 'mcp_server' has no attribute '_trace_work_counters'`

- [ ] **Step 3: Write the implementation**

Insert into `mcp_server.py` immediately above `def _checkpoint_duty_from_env`:

```python
def _trace_work_counters(extracted_files: Sequence[tuple]) -> Dict[str, Any]:
    """Per-commit work size, for the #260 cost trace. Pure; len() only.

    extracted_files is _extract_commit's file_results -- one
    (status, file_path, extracted, precomputed, old_path) per changed file. A
    "D" entry carries extracted=None and precomputed=None, so it counts as a
    touched file and contributes nothing else: there is no module being
    introduced and no entities to consider.

    `idents_considered` is #260's frozen work metric W (see
    docs/superpowers/specs/2026-08-17-per-commit-cost-attribution-design.md):
    one module ident per file that has a precomputed, plus one per extracted
    entity, plus one per RESOLVED import. It is the unit _build_code_triples
    iterates. Unresolved imports are excluded because they take the
    external-dependency fallback rather than an entity path;
    n_imports_total - n_imports_resolved is kept as exploratory signal.

    n_unchanged_idents (#221's body-diff narrowing) is likewise exploratory and
    deliberately NOT subtracted from W: W counts idents CONSIDERED, and an
    unchanged ident is considered before it is narrowed out.

    W is FROZEN. Changing this arithmetic after a trace exists redefines the
    experiment; fork it instead.
    """
    files_by_status: Dict[str, int] = {}
    n_modules = n_functions = n_classes = n_globals = n_fields = 0
    n_imports_total = n_imports_resolved = 0
    n_unchanged_idents = 0

    for status, _file_path, _extracted, precomputed, _old_path in extracted_files:
        files_by_status[status] = files_by_status.get(status, 0) + 1
        if not precomputed:
            continue
        n_modules += 1
        n_functions += len(precomputed["function_entries"])
        n_classes += len(precomputed["class_entries"])
        n_globals += len(precomputed["global_entries"])
        n_fields += len(precomputed["field_entries"])
        for _import_name, _dep_ident, is_resolved in precomputed["resolved_imports"]:
            n_imports_total += 1
            if is_resolved:
                n_imports_resolved += 1
        n_unchanged_idents += len(precomputed.get("unchanged_idents", ()))

    return {
        "files_by_status": files_by_status,
        "n_modules": n_modules,
        "n_functions": n_functions,
        "n_classes": n_classes,
        "n_globals": n_globals,
        "n_fields": n_fields,
        "n_imports_total": n_imports_total,
        "n_imports_resolved": n_imports_resolved,
        "n_unchanged_idents": n_unchanged_idents,
        "idents_considered": (
            n_modules + n_functions + n_classes + n_globals + n_fields
            + n_imports_resolved
        ),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestTraceWorkCounters -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _trace_work_counters for the #260 per-commit cost trace

Turns _extract_commit's file_results into the work-size counters the trace
records, including idents_considered -- #260's frozen work metric W. A 'D'
entry contributes a touched-file count and nothing else, since it carries no
precomputed and introduces no module.

Refs #260"
```

---

### Task 2: `_IngestTrace` — the record writer

**Files:**
- Modify: `mcp_server.py` (new class, immediately below `_trace_work_counters`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_trace_work_counters` from Task 1.
- Produces:
  - `_IngestTrace(path: str, clock: Callable[[], float] = time.monotonic)`
  - `.emit(pos: int, tag: str, commit_hash: str, await_s: float, apply_s: float, extracted_files: Sequence[tuple], policy: Optional["_CheckpointPolicy"]) -> None`
  - `.close() -> None` (idempotent)
  - `.records: int` (count emitted)

**Background the implementer needs.** `_CheckpointPolicy` (`mcp_server.py:3688`) exposes **cumulative** `checkpoints: int` and `total_seconds: float`. Per-commit checkpoint cost is therefore a **delta** against the values seen at the previous emit — the policy gains no state. `policy` may be `None`: `_run_ingestion` clears `_ingest_checkpoint_policy` at its terminal paths, and tests may drive `emit` without one. A `None` policy must record deltas of `0`, not crash.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class TestIngestTrace:
    """#260: _IngestTrace appends one JSON object per commit to a JSONL file."""

    @staticmethod
    def _files(functions=1):
        return [("M", "b.py", {}, {
            "module_ident": ":module/b",
            "function_entries": [(f":function/f{i}", f"f{i}", []) for i in range(functions)],
            "class_entries": [], "global_entries": [], "field_entries": [],
            "resolved_imports": [], "unchanged_idents": set(),
        }, "")]

    def _read(self, path):
        import json
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_emits_one_json_object_per_commit(self, tmp_path):
        path = tmp_path / "trace.jsonl"
        trace = mcp_server._IngestTrace(str(path))
        trace.emit(0, "fwd", "aaa", 0.1, 0.2, self._files(), None)
        trace.emit(766, "rev", "bbb", 0.3, 0.4, self._files(functions=3), None)
        trace.close()
        records = self._read(path)
        assert len(records) == 2
        assert trace.records == 2
        assert [r["pos"] for r in records] == [0, 766]
        assert [r["tag"] for r in records] == ["fwd", "rev"]
        assert [r["hash"] for r in records] == ["aaa", "bbb"]

    def test_record_carries_timings_and_work_counters(self, tmp_path):
        path = tmp_path / "trace.jsonl"
        trace = mcp_server._IngestTrace(str(path))
        trace.emit(5, "fwd", "abc", 0.25, 1.5, self._files(functions=4), None)
        trace.close()
        r = self._read(path)[0]
        assert r["await_s"] == 0.25
        assert r["apply_s"] == 1.5
        # Task 1's counters are embedded, not recomputed here.
        assert r["idents_considered"] == 5  # 1 module + 4 functions
        assert r["n_functions"] == 4
        assert r["files_by_status"] == {"M": 1}

    def test_t_since_start_is_measured_from_construction(self, tmp_path):
        """A fake clock, so this asserts the arithmetic rather than a duration."""
        ticks = iter([100.0, 100.0, 103.5, 107.25])
        trace = mcp_server._IngestTrace(str(tmp_path / "t.jsonl"), clock=lambda: next(ticks))
        trace.emit(0, "fwd", "a", 0.0, 0.0, [], None)
        trace.emit(1, "fwd", "b", 0.0, 0.0, [], None)
        trace.close()
        records = self._read(tmp_path / "t.jsonl")
        assert records[0]["t_since_start"] == 3.5
        assert records[1]["t_since_start"] == 7.25

    def test_checkpoint_deltas_are_differences_not_totals(self, tmp_path):
        """The policy's counters are CUMULATIVE. The trace must record the
        per-commit delta -- recording the totals would make every downstream
        per-checkpoint mean monotonically wrong, and the control gate reads
        exactly these two fields."""
        path = tmp_path / "trace.jsonl"
        policy = mcp_server._CheckpointPolicy(0.05)
        trace = mcp_server._IngestTrace(str(path))

        policy.checkpoints, policy.total_seconds = 1, 0.50
        trace.emit(0, "fwd", "a", 0.0, 0.0, [], policy)
        policy.checkpoints, policy.total_seconds = 1, 0.50   # no checkpoint here
        trace.emit(1, "fwd", "b", 0.0, 0.0, [], policy)
        policy.checkpoints, policy.total_seconds = 3, 2.75
        trace.emit(2, "fwd", "c", 0.0, 0.0, [], policy)
        trace.close()

        records = self._read(path)
        assert [r["ckpt_d_count"] for r in records] == [1, 0, 2]
        assert [round(r["ckpt_d_seconds"], 6) for r in records] == [0.50, 0.0, 2.25]

    def test_none_policy_records_zero_deltas_and_does_not_raise(self, tmp_path):
        """_run_ingestion clears _ingest_checkpoint_policy at its terminal
        paths, so emit must tolerate None rather than making the trace the
        reason a run dies."""
        path = tmp_path / "trace.jsonl"
        trace = mcp_server._IngestTrace(str(path))
        trace.emit(0, "fwd", "a", 0.0, 0.0, [], None)
        trace.close()
        r = self._read(path)[0]
        assert r["ckpt_d_count"] == 0
        assert r["ckpt_d_seconds"] == 0.0

    def test_close_is_idempotent(self, tmp_path):
        """_run_ingestion has TWO terminal finally sites that both clear the
        policy, so close() can genuinely be reached twice."""
        trace = mcp_server._IngestTrace(str(tmp_path / "t.jsonl"))
        trace.emit(0, "fwd", "a", 0.0, 0.0, [], None)
        trace.close()
        trace.close()  # must not raise

    def test_records_survive_without_close(self, tmp_path):
        """A killed run must leave a readable partial trace -- that is the whole
        point of JSONL over a single JSON document. Each emit flushes."""
        path = tmp_path / "trace.jsonl"
        trace = mcp_server._IngestTrace(str(path))
        trace.emit(0, "fwd", "a", 0.0, 0.0, [], None)
        assert len(self._read(path)) == 1  # readable before close()
        trace.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestTrace -v`
Expected: all 7 FAIL with `AttributeError: module 'mcp_server' has no attribute '_IngestTrace'`

- [ ] **Step 3: Write the implementation**

Insert into `mcp_server.py` immediately below `_trace_work_counters`:

```python
class _IngestTrace:
    """Per-commit cost trace for #260, armed by MINIGRAF_INGEST_TRACE_PATH.

    One JSON object per line, appended as each commit's write half finishes.
    JSONL rather than a single document so a killed run still leaves a
    readable partial trace -- at-scale runs take ~30 minutes and the
    interesting ones are sometimes the ones that die.

    Checkpoint cost is recorded as a DELTA of _CheckpointPolicy's cumulative
    `checkpoints`/`total_seconds` across each commit, so the policy gains no
    state of its own. `policy` may be None (the two terminal finally sites in
    _run_ingestion clear it), which records zero deltas rather than raising:
    an instrument must never be the reason a run dies.

    Writes only, no locks, no awaits, no DB access -- see this module's
    _db_native_lock invariant comment for why the per-commit loop tolerates
    nothing else.
    """

    def __init__(self, path: str, clock: "Callable[[], float]" = time.monotonic) -> None:
        self._fh: Optional[Any] = open(path, "a", encoding="utf-8")
        self._clock = clock
        self._started_at = clock()
        self._ckpt_count = 0
        self._ckpt_seconds = 0.0
        self.records = 0

    def emit(
        self,
        pos: int,
        tag: str,
        commit_hash: str,
        await_s: float,
        apply_s: float,
        extracted_files: Sequence[tuple],
        policy: Optional["_CheckpointPolicy"],
    ) -> None:
        if self._fh is None:
            return
        if policy is None:
            d_count, d_seconds = 0, 0.0
        else:
            d_count = policy.checkpoints - self._ckpt_count
            d_seconds = policy.total_seconds - self._ckpt_seconds
            self._ckpt_count = policy.checkpoints
            self._ckpt_seconds = policy.total_seconds

        record: Dict[str, Any] = {
            "pos": pos,
            "tag": tag,
            "hash": commit_hash,
            "t_since_start": self._clock() - self._started_at,
            "await_s": await_s,
            "apply_s": apply_s,
            "ckpt_d_count": d_count,
            "ckpt_d_seconds": d_seconds,
        }
        record.update(_trace_work_counters(extracted_files))
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()
        self.records += 1

    def close(self) -> None:
        """Idempotent -- _run_ingestion has two terminal paths that both
        release the trace, and either may run first."""
        if self._fh is None:
            return
        try:
            self._fh.close()
        finally:
            self._fh = None
```

If `Callable` is not already imported in `mcp_server.py`'s `typing` import, add it. Check with `grep -n "^from typing import" mcp_server.py` — `_CheckpointPolicy.__init__` already annotates `clock: "Callable[[], float]"`, so it is available.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestTrace -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _IngestTrace, the #260 per-commit JSONL cost writer

Records timings, Task 1's work counters, and per-commit DELTAS of
_CheckpointPolicy's cumulative counters. JSONL and a flush per record so a
killed at-scale run still leaves a readable partial trace; close() is
idempotent because _run_ingestion has two terminal paths that release it.

Refs #260"
```

---

### Task 3: Wire the trace into `_run_ingestion`

**Files:**
- Modify: `mcp_server.py` — module global (~line 3686), `global` declaration (line 10876), arming site (~line 10982), loop body (lines 11079–11158), both terminal finally sites (lines 11352, 11393)
- Modify: `CLAUDE.md` — env var documentation
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_IngestTrace` from Task 2.
- Produces: module global `_ingest_trace: Optional["_IngestTrace"]`; `_ingest_trace_from_env() -> Optional[_IngestTrace]`. Task 4 arms this by setting `MINIGRAF_INGEST_TRACE_PATH`.

**Background the implementer needs.** The exact anchors, verified on this branch:

| line | code |
|---|---|
| 3686 | `_ingest_checkpoint_policy: Optional["_CheckpointPolicy"] = None` |
| 10876 | `global _ingest_progress, _ingest_checkpoint_policy` |
| 10982 | `_ingest_checkpoint_policy = _CheckpointPolicy(_checkpoint_duty_from_env())` |
| 11079 | `tag, pos, fut = pending.popleft()` |
| 11090 | `extracted_files, gitlink_changes, gitmodules_map, renamed_pairs = await fut` |
| 11112 | `_ingest_progress["processed"] += 1` (the extraction-failure skip path, which `continue`s) |
| 11126 | `async with db_lease_async() as db:` (the write half) |
| 11158 | `_ingest_progress["processed"] += 1` (the success path) |
| 11352, 11393 | `_ingest_checkpoint_policy = None` (the two terminal finally sites) |

Two deliberate scope decisions to preserve:

1. **No record for extraction-failure skips.** The path at 11112 `continue`s having done no write work; emitting a record with `apply_s=0` would inject zero-cost points into the fit. The skip is already visible on stderr (`[_run_ingestion] skipping unreadable commit ...`) and counted by #256's `stderr_capture.py`.
2. **`apply_s` includes the lease acquire.** The timer starts before `async with db_lease_async()`, so `apply_s` covers acquire + the executor call. That is honest — the acquire is real serial per-commit cost — and it is stated in the code comment so nobody later reads `apply_s` as pure write time.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`. These drive a **real ingestion over a real git repo**, following the file-backed pattern in `docs/testing-conventions.md`.

Use the existing **`git_repo` fixture** (`tests/test_mcp_server.py:6873`) — two commits, `auth.py` (`def login`) and `models.py` (`class User`). Do not write a new repo builder; there is no shared helper, and the ~19 inlined `git init` sites are per-test scaffolding rather than a reusable one.

Copy the driving pattern from `TestRunIngestionShutdown` (`tests/test_mcp_server.py:13236`), which is the cluster `docs/testing-conventions.md` points at: `@pytest.mark.asyncio async def`, `await mcp_server._run_ingestion(str(git_repo), "HEAD")` — **not** `asyncio.run` — with `_reset_db_state()` then `open_db()` then an `_ingest_progress` reset before the call.

```python
class TestIngestTraceWiring:
    """#260: MINIGRAF_INGEST_TRACE_PATH arms the per-commit trace, and its
    absence leaves ingestion byte-for-byte unchanged."""

    def test_no_env_var_means_no_trace_and_no_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MINIGRAF_INGEST_TRACE_PATH", raising=False)
        assert mcp_server._ingest_trace_from_env() is None

    def test_env_var_returns_a_trace_writing_to_that_path(self, tmp_path, monkeypatch):
        path = tmp_path / "t.jsonl"
        monkeypatch.setenv("MINIGRAF_INGEST_TRACE_PATH", str(path))
        trace = mcp_server._ingest_trace_from_env()
        assert trace is not None
        try:
            trace.emit(0, "fwd", "a", 0.0, 0.0, [], None)
        finally:
            trace.close()
        assert path.exists()

    def test_unopenable_path_degrades_to_none_and_warns(self, tmp_path, monkeypatch, capsys):
        """A bad trace path must not be the reason a repository never ingests --
        same principle as _parse_stream_ratio and _checkpoint_duty_from_env,
        both of which degrade rather than raise."""
        monkeypatch.setenv(
            "MINIGRAF_INGEST_TRACE_PATH", str(tmp_path / "no" / "such" / "dir" / "t.jsonl")
        )
        assert mcp_server._ingest_trace_from_env() is None
        assert "MINIGRAF_INGEST_TRACE_PATH" in capsys.readouterr().err

    @staticmethod
    def _drive(git_repo, graph_path):
        """_run_ingestion over a real file-backed graph, per
        TestRunIngestionShutdown's pattern."""
        import mcp_server
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph_path))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }

    @pytest.mark.asyncio
    async def test_real_ingestion_writes_one_record_per_applied_commit(
        self, git_repo, tmp_path, monkeypatch
    ):
        """The load-bearing test: a real _run_ingestion over a real repo emits a
        record per commit whose write half ran, with a tag from the claimer and
        a positive apply_s.

        git_repo has exactly 2 commits (auth.py's `def login`, models.py's
        `class User`), so the record count and the pos set are both exact.
        """
        import json
        import mcp_server
        trace_path = tmp_path / "trace.jsonl"
        monkeypatch.setenv("MINIGRAF_INGEST_TRACE_PATH", str(trace_path))

        self._drive(git_repo, git_repo / "memory.graph")
        try:
            await mcp_server._run_ingestion(str(git_repo), "HEAD")
        finally:
            mcp_server._reset_db_state()

        records = [json.loads(l) for l in trace_path.read_text().splitlines() if l.strip()]
        assert len(records) == 2
        assert {r["tag"] for r in records} <= {"fwd", "rev"}
        assert sorted(r["pos"] for r in records) == [0, 1]
        assert all(r["apply_s"] > 0.0 for r in records)
        assert all(r["await_s"] >= 0.0 for r in records)
        # Work counters must be real, not all-zero: git_repo introduces a
        # function and a class. A trace of zeros would fit perfectly and mean
        # nothing -- this is the positive control for the instrument itself.
        assert sum(r["idents_considered"] for r in records) > 0

    @pytest.mark.asyncio
    async def test_trace_is_closed_and_global_cleared_after_a_run(
        self, git_repo, tmp_path, monkeypatch
    ):
        """Mirrors _ingest_checkpoint_policy's contract: the global must not
        outlive the run, or a later interactive transact writes into a finished
        run's trace file."""
        import mcp_server
        monkeypatch.setenv("MINIGRAF_INGEST_TRACE_PATH", str(tmp_path / "t.jsonl"))
        self._drive(git_repo, git_repo / "memory.graph")
        try:
            await mcp_server._run_ingestion(str(git_repo), "HEAD")
        finally:
            mcp_server._reset_db_state()
        assert mcp_server._ingest_trace is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestTraceWiring -v`
Expected: FAIL — `_ingest_trace_from_env` and `_ingest_trace` do not exist yet.

- [ ] **Step 3: Add the module global and the env reader**

At `mcp_server.py:3686`, below `_ingest_checkpoint_policy`:

```python
_ingest_trace: Optional["_IngestTrace"] = None
```

Below `_IngestTrace` (from Task 2), add:

```python
def _ingest_trace_from_env() -> Optional["_IngestTrace"]:
    """Read MINIGRAF_INGEST_TRACE_PATH and open a trace, or None.

    Degrades to None on any open failure rather than raising, matching
    _checkpoint_duty_from_env and _parse_stream_ratio: an unwritable trace path
    is a typo in an instrument, and must not become the reason a repository
    never ingests (#260).
    """
    raw = os.environ.get("MINIGRAF_INGEST_TRACE_PATH")
    if not raw:
        return None
    try:
        return _IngestTrace(raw)
    except OSError as e:
        print(
            f"[_run_ingestion] cannot open MINIGRAF_INGEST_TRACE_PATH={raw!r} "
            f"({e}); per-commit tracing disabled",
            file=sys.stderr,
        )
        return None
```

- [ ] **Step 4: Wire it into `_run_ingestion`**

At line 10876, widen the `global` declaration:

```python
    global _ingest_progress, _ingest_checkpoint_policy, _ingest_trace
```

At line 10982, immediately after the checkpoint policy is built:

```python
        _ingest_checkpoint_policy = _CheckpointPolicy(_checkpoint_duty_from_env())
        _ingest_trace = _ingest_trace_from_env()
```

In the loop body, wrap the two measured spans. Before the `try:` that precedes line 11090:

```python
                    _trace_t_await = time.perf_counter()
                    try:
                        extracted_files, gitlink_changes, gitmodules_map, renamed_pairs = await fut
```

and immediately after that `try/except` block ends (after `submit_next()` at line 11115):

```python
                    _trace_await_s = time.perf_counter() - _trace_t_await
```

Wrap the write half. Replace line 11126's `async with db_lease_async() as db:` with:

```python
                    # #260: apply_s deliberately spans the lease ACQUIRE as
                    # well as the executor call -- the acquire is real serial
                    # per-commit cost, and a reader must not take apply_s for
                    # pure write time.
                    _trace_t_apply = time.perf_counter()
                    async with db_lease_async() as db:
```

and immediately after that `async with` block ends (before line 11158's `_ingest_progress["processed"] += 1`):

```python
                    if _ingest_trace is not None:
                        _ingest_trace.emit(
                            pos, tag, commit_hash,
                            _trace_await_s,
                            time.perf_counter() - _trace_t_apply,
                            extracted_files,
                            _ingest_checkpoint_policy,
                        )
                    _ingest_progress["processed"] += 1
```

Leave the extraction-failure path at 11112 alone — it emits no record by design.

At **both** terminal finally sites (11352 and 11393), beside each `_ingest_checkpoint_policy = None`:

```python
            if _ingest_trace is not None:
                _ingest_trace.close()
            _ingest_trace = None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestIngestTraceWiring -v`
Expected: 5 passed

- [ ] **Step 6: Prove the no-env-var path is genuinely inert**

This is the ablation for this task. An instrument that is "off" but still changes behaviour is worse than no instrument.

Run the full git-ingestion test cluster with the env var explicitly unset:

```bash
env -u MINIGRAF_INGEST_TRACE_PATH .venv/bin/python -m pytest \
    tests/test_mcp_server.py -k "ingest" -q
```

Expected: same pass count as on `master` for that selector. Record both numbers in the commit message. If they differ, the hook is not inert — stop and fix it rather than adjusting the test.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: the master baseline (**1498 passed, 1 xfailed**) plus this branch's 19 new tests → 1517 passed, 1 xfailed. The 1 xfail is #257's permanent guard and must stay xfailed.

- [ ] **Step 8: Document the env var in `CLAUDE.md`**

In the `## Graph Storage` section, after the `MINIGRAF_INGEST_CHECKPOINT_DUTY` paragraph, add:

```markdown
Per-commit cost trace: `MINIGRAF_INGEST_TRACE_PATH` (unset by default).

When set, ingestion appends one JSON object per applied commit — timings, work
counters, and per-commit checkpoint deltas — for #260's cost attribution. Unset
means no trace and no file. Commits skipped for extraction failure emit no
record, so a trace is not a commit census; `stderr_capture.py` counts those.
Read with `evals/at_scale/probe_per_commit_cost.py`.
```

- [ ] **Step 9: Check `SKILL.md`**

Run: `grep -n "MINIGRAF_" SKILL.md`

`MINIGRAF_INGEST_TRACE_PATH` is an eval-side instrument with no user-facing tool surface, so **no change is expected**. Confirm that by looking, not by assuming — this project's standing rule is to check docs sync on every change. If `SKILL.md` does document the other `MINIGRAF_*` env vars, add this one for consistency and say so in the commit message.

- [ ] **Step 10: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py CLAUDE.md
git commit -m "Arm the #260 per-commit trace from MINIGRAF_INGEST_TRACE_PATH

_run_ingestion opens a trace beside the checkpoint policy, emits a record per
commit whose write half ran, and closes it at both terminal finally sites.
Unset env var means no trace and no file: verified by running the ingest test
cluster with the var explicitly unset and matching master's pass count.

apply_s spans the lease acquire as well as the executor call, since the
acquire is real serial per-commit cost. Extraction-failure skips emit no
record by design -- they do no write work, and zero-cost points would
contaminate the fit.

Refs #260"
```

---

### Task 4: `--trace-path` on the ingestion benchmark

**Files:**
- Modify: `evals/at_scale/run_ingestion_benchmark.py` (`run_ingestion_benchmark` ~line 121, `main` ~line 404)
- Test: `tests/test_at_scale_ingestion_benchmark.py` — append to the existing `TestRunIngestionBenchmark` class (line 135), whose `test_records_the_resolved_graph_path` (line 143) is the direct analogue to copy. That module has its **own** `git_repo` fixture at line 119; use it, and pass `poll_interval=0.05` as every test there does.

**Interfaces:**
- Consumes: the `MINIGRAF_INGEST_TRACE_PATH` contract from Task 3.
- Produces: `run_ingestion_benchmark(..., trace_path: Optional[Path] = None)`; a `trace_path` key in the returned metrics dict (absolute string, or absent when untraced). Task 6 reads that key.

**Background the implementer needs.** `run_ingestion_benchmark` (line 121) is `async` and returns a metrics dict built around line 260 (`"graph_path": str(Path(graph_path).resolve())`). `main` (line 404) parses args, wraps the call in `resolve_graph_path`, then calls `write_json_result` and `append_ingestion_report`.

The trace must be armed **before** `_run_ingestion` starts and the env var must not leak to the rest of the process — set it around the call and restore it, rather than mutating `os.environ` for the process lifetime.

- [ ] **Step 1: Write the failing test**

```python
class TestBenchmarkTracePath:
    """#260: --trace-path arms the per-commit trace and records where it went."""

    @pytest.mark.asyncio
    async def test_metrics_record_the_resolved_trace_path(self, git_repo, tmp_path):
        """Provenance. #275's whole lesson was that a benchmark which does not
        record what it measured produces an artifact nobody can interpret."""
        graph_path = tmp_path / "g.graph"
        trace = tmp_path / "trace.jsonl"
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", graph_path, poll_interval=0.05, trace_path=trace,
        )
        assert metrics["trace_path"] == str(trace.resolve())
        assert trace.exists()

    @pytest.mark.asyncio
    async def test_untraced_run_records_no_trace_path_key(self, git_repo, tmp_path):
        """Absent must mean 'not traced', never 'traced and empty' -- the same
        three-state discipline benchmark.md's residue rows use."""
        metrics = await run_ingestion_benchmark(
            str(git_repo), "HEAD", tmp_path / "g.graph", poll_interval=0.05,
        )
        assert "trace_path" not in metrics

    @pytest.mark.asyncio
    async def test_env_var_does_not_leak_past_the_run(
        self, git_repo, tmp_path, monkeypatch
    ):
        import os
        monkeypatch.delenv("MINIGRAF_INGEST_TRACE_PATH", raising=False)
        await run_ingestion_benchmark(
            str(git_repo), "HEAD", tmp_path / "g.graph", poll_interval=0.05,
            trace_path=tmp_path / "t.jsonl",
        )
        assert "MINIGRAF_INGEST_TRACE_PATH" not in os.environ
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_at_scale_ingestion_benchmark.py::TestBenchmarkTracePath -v`
Expected: FAIL with `TypeError: run_ingestion_benchmark() got an unexpected keyword argument 'trace_path'`

- [ ] **Step 3: Implement**

Widen the signature at line 121:

```python
async def run_ingestion_benchmark(
    repo_path: str,
    branch: Optional[str],
    graph_path: Path,
    poll_interval: float = 0.5,
    duty_factor: float = 10.0,
    compare_ignore: bool = False,
    trace_path: Optional[Path] = None,
) -> dict[str, Any]:
```

Extend the docstring with:

```
    trace_path, if given, arms mcp_server's per-commit cost trace (#260) via
    MINIGRAF_INGEST_TRACE_PATH for the duration of this call only, and is
    reported back as metrics["trace_path"]. Absent from metrics means the run
    was NOT traced -- never "traced and empty".
```

Arm it around the ingestion. Set the env var immediately before the `_run_ingestion` drive and restore in a `finally`:

```python
    _prior_trace_env = os.environ.get("MINIGRAF_INGEST_TRACE_PATH")
    if trace_path is not None:
        os.environ["MINIGRAF_INGEST_TRACE_PATH"] = str(Path(trace_path).resolve())
    try:
        ...  # the existing body that drives _run_ingestion
    finally:
        if trace_path is not None:
            if _prior_trace_env is None:
                os.environ.pop("MINIGRAF_INGEST_TRACE_PATH", None)
            else:
                os.environ["MINIGRAF_INGEST_TRACE_PATH"] = _prior_trace_env
```

In the metrics dict near line 260, beside `"graph_path"`:

```python
        **({"trace_path": str(Path(trace_path).resolve())} if trace_path is not None else {}),
```

In `main()`, add the flag beside `--graph-path`:

```python
    parser.add_argument(
        "--trace-path", default=None,
        help="Append a per-commit cost trace (JSONL) here (#260). Read it with "
             "evals/at_scale/probe_per_commit_cost.py. Off by default.",
    )
```

and pass it through:

```python
                trace_path=Path(args.trace_path) if args.trace_path else None,
```

- [ ] **Step 4: Run to verify passing**

Run: `.venv/bin/python -m pytest tests/test_at_scale_ingestion_benchmark.py::TestBenchmarkTracePath -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/run_ingestion_benchmark.py tests/
git commit -m "Add --trace-path to the ingestion benchmark for #260

Arms MINIGRAF_INGEST_TRACE_PATH for the duration of one run and restores the
prior value afterwards, and reports the resolved path back as
metrics['trace_path']. The key is ABSENT on an untraced run rather than empty,
keeping 'not traced' distinguishable from 'traced and found nothing' -- the
same three-state discipline benchmark.md's residue rows use.

Refs #260"
```

---

### Task 5: `trace_fit.py` — the frozen analysis

**Files:**
- Create: `evals/at_scale/trace_fit.py`
- Test: `tests/test_at_scale_trace_fit.py`

**Interfaces:**
- Consumes: the record shape from Task 2.
- Produces:
  - Constants `W_KEY = "idents_considered"`, `CLOSE_BELOW = 1.5`, `REAL_AT = 2.0`, `CONTROL_MIN_GROWTH = 2.0`, `CONTROL_MIN_CHECKPOINTS = 5`, `MIN_POINTS_PER_GROUP = 30`
  - `split_thirds(records: list[dict]) -> tuple[list, list, list]`
  - `fit_line(xs: list[float], ys: list[float]) -> Optional[dict]` → `{"a","b","r2","n"}`
  - `growth_ratio(first: Optional[float], last: Optional[float]) -> Optional[float]`
  - `control_gate(first: list[dict], last: list[dict]) -> dict` → `{"passed","growth","mean_first","mean_last","n_first","n_last","reason"}`
  - `verdict(a_ratio, b_ratio) -> tuple[str, str]` → one of `"CONFOUNDED"`, `"REAL"`, `"INCONCLUSIVE"`, plus a reason string
  - `analyse(records: list[dict]) -> dict` — the whole pipeline
- Task 6 calls `analyse` and serialises its return.

**Read the spec's "Pre-registered before the run" section before writing this file.** Every constant above is normative. Copy the values; do not re-derive them.

**Three traps this task must avoid, each of which has bitten this project:**

1. **A zero-variance group makes `a` and `b` unidentifiable.** If every record in a group has the same `W`, `sxx == 0` and OLS divides by zero. `fit_line` must return `None`, not a number. This is exactly the decorrelation precondition the spec relies on.
2. **An OLS intercept can be negative,** which makes `a_last/a_first` meaningless (a ratio across zero flips sign). `growth_ratio` must return `None` when the denominator is `<= 0`, and `verdict` must read a `None` ratio as INCONCLUSIVE rather than silently dropping it.
3. **`verdict`'s "either ≥ 2.0x" is a disjunction.** A test where *both* `a` and `b` grow leaves only one disjunct load-bearing — the other could be deleted and the test would still pass. Each disjunct needs its own test with the other held flat.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_at_scale_trace_fit.py`:

```python
"""#260: the per-commit cost fit. Pure functions over synthetic traces -- no DB,
no ingestion, milliseconds."""

import pytest

from evals.at_scale import trace_fit


def rec(w, apply_s, ckpt_count=0, ckpt_seconds=0.0, tag="fwd", pos=0):
    return {
        "pos": pos, "tag": tag, "hash": "x", "t_since_start": 0.0,
        "await_s": 0.0, "apply_s": apply_s,
        "ckpt_d_count": ckpt_count, "ckpt_d_seconds": ckpt_seconds,
        trace_fit.W_KEY: w,
    }


def group(n, a, b, w_lo=10, w_hi=500, ckpt_every=10, ckpt_seconds=1.0):
    """n records drawn from apply_s = a + b*W, with W spread across [w_lo, w_hi].

    The spread is what makes a and b identifiable -- it stands in for the real
    run's fwd/rev interleave, where small-work and large-work commits arrive at
    the same graph size.
    """
    out = []
    for i in range(n):
        w = w_lo + (w_hi - w_lo) * i / max(n - 1, 1)
        out.append(rec(
            w, a + b * w,
            ckpt_count=1 if i % ckpt_every == 0 else 0,
            ckpt_seconds=ckpt_seconds if i % ckpt_every == 0 else 0.0,
        ))
    return out


class TestFitLine:
    def test_recovers_a_and_b_from_an_exact_line(self):
        f = trace_fit.fit_line([1.0, 2.0, 3.0, 4.0] * 10, [3.0, 5.0, 7.0, 9.0] * 10)
        assert f["a"] == pytest.approx(1.0)
        assert f["b"] == pytest.approx(2.0)
        assert f["r2"] == pytest.approx(1.0)
        assert f["n"] == 40

    def test_zero_variance_in_W_is_unidentifiable_not_a_crash(self):
        """Every record with the same W leaves a and b unseparable. Returning a
        number here would invent an intercept from nothing."""
        assert trace_fit.fit_line([5.0] * 40, [1.0] * 40) is None

    def test_too_few_points_returns_none(self):
        n = trace_fit.MIN_POINTS_PER_GROUP - 1
        assert trace_fit.fit_line(list(range(n)), list(range(n))) is None

    def test_exactly_min_points_is_enough(self):
        n = trace_fit.MIN_POINTS_PER_GROUP
        assert trace_fit.fit_line(list(range(n)), list(range(n))) is not None


class TestGrowthRatio:
    def test_plain_ratio(self):
        assert trace_fit.growth_ratio(2.0, 5.0) == pytest.approx(2.5)

    @pytest.mark.parametrize("first", [0.0, -0.5])
    def test_non_positive_denominator_is_undefined(self, first):
        """An OLS intercept can legitimately come out negative or zero. A ratio
        across zero flips sign and reads as a small number -- which would report
        'flat' for a parameter that is not flat."""
        assert trace_fit.growth_ratio(first, 5.0) is None

    def test_none_operand_propagates(self):
        assert trace_fit.growth_ratio(None, 5.0) is None
        assert trace_fit.growth_ratio(2.0, None) is None


class TestVerdict:
    def test_both_flat_is_confounded(self):
        v, why = trace_fit.verdict(1.1, 1.2)
        assert v == "CONFOUNDED"

    def test_growing_intercept_alone_is_real(self):
        """a grows, b flat: fixed per-commit cost rising with graph size. This
        disjunct must be load-bearing on its own."""
        v, why = trace_fit.verdict(2.4, 1.05)
        assert v == "REAL"
        assert "a" in why

    def test_growing_slope_alone_is_real(self):
        """b grows, a flat: cost per unit work rising. The OTHER disjunct, tested
        with the first held flat."""
        v, why = trace_fit.verdict(1.05, 2.4)
        assert v == "REAL"
        assert "b" in why

    def test_middle_band_is_inconclusive(self):
        v, why = trace_fit.verdict(1.7, 1.2)
        assert v == "INCONCLUSIVE"

    def test_boundaries_are_exact(self):
        assert trace_fit.verdict(1.49, 1.49)[0] == "CONFOUNDED"
        assert trace_fit.verdict(1.5, 1.0)[0] == "INCONCLUSIVE"
        assert trace_fit.verdict(2.0, 1.0)[0] == "REAL"

    def test_undefined_ratio_is_inconclusive_never_confounded(self):
        """A None ratio means the fit could not be read. Treating that as flat
        would let a failed measurement argue for closing the issue."""
        assert trace_fit.verdict(None, 1.1)[0] == "INCONCLUSIVE"
        assert trace_fit.verdict(1.1, None)[0] == "INCONCLUSIVE"


class TestControlGate:
    def test_growing_checkpoint_duration_passes(self):
        first = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.10) for _ in range(10)]
        last = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.50) for _ in range(10)]
        g = trace_gate = trace_fit.control_gate(first, last)
        assert g["passed"] is True
        assert g["growth"] == pytest.approx(5.0)

    def test_flat_checkpoint_duration_fails_the_gate(self):
        """THE ABLATION FOR THE WHOLE EXPERIMENT. Checkpoint cost is documented
        O(graph size); a method that cannot see it grow has failed open, and a
        flat verdict from it means nothing. This must be red."""
        first = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.20) for _ in range(10)]
        last = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.20) for _ in range(10)]
        g = trace_fit.control_gate(first, last)
        assert g["passed"] is False
        assert g["growth"] == pytest.approx(1.0)

    def test_too_few_checkpoints_is_unevaluable_not_a_pass(self):
        first = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.1) for _ in range(2)]
        last = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=9.9) for _ in range(2)]
        g = trace_fit.control_gate(first, last)
        assert g["passed"] is False
        assert "checkpoint" in g["reason"].lower()

    def test_gate_reads_per_checkpoint_mean_not_total(self):
        """Total checkpoint time is DESIGNED not to grow -- the duty policy holds
        it to a fixed fraction of wall clock. Gating on the total would fail on
        healthy behaviour. Here totals are equal and the means differ 10x."""
        first = [rec(10, 1.0, ckpt_count=10, ckpt_seconds=1.0)]
        last = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=1.0)]
        g = trace_fit.control_gate(first, last)
        assert g["growth"] == pytest.approx(10.0)
        assert g["passed"] is True


class TestSplitThirds:
    def test_equal_counts_in_emission_order(self):
        records = [rec(i, 1.0, pos=i) for i in range(300)]
        a, b, c = trace_fit.split_thirds(records)
        assert len(a) == len(b) == len(c) == 100
        assert [r["pos"] for r in a] == list(range(100))
        assert [r["pos"] for r in c] == list(range(200, 300))

    def test_remainder_goes_to_the_middle_group(self):
        records = [rec(i, 1.0, pos=i) for i in range(302)]
        a, b, c = trace_fit.split_thirds(records)
        assert (len(a), len(b), len(c)) == (100, 102, 100)
        # First and last must stay the same size -- they are what the verdict
        # compares, and an uneven pair would bias the ratio.


class TestAnalyse:
    def test_flat_trace_reports_confounded_when_control_passes(self):
        records = (
            group(120, a=0.5, b=0.001, ckpt_seconds=0.10)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.30)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.50)
        )
        out = trace_fit.analyse(records)
        assert out["control_gate"]["passed"] is True
        assert out["verdict"] == "CONFOUNDED"

    def test_growing_intercept_reports_real(self):
        records = (
            group(120, a=0.5, b=0.001, ckpt_seconds=0.10)
            + group(120, a=1.0, b=0.001, ckpt_seconds=0.30)
            + group(120, a=2.0, b=0.001, ckpt_seconds=0.50)
        )
        out = trace_fit.analyse(records)
        assert out["control_gate"]["passed"] is True
        assert out["verdict"] == "REAL"
        assert out["a_ratio"] == pytest.approx(4.0, rel=0.05)

    def test_void_when_the_control_gate_fails_regardless_of_the_fit(self):
        """A void run does not get to report CONFOUNDED. This is the guard
        against a broken measurement arguing for closing #260."""
        records = (
            group(120, a=0.5, b=0.001, ckpt_seconds=0.20)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.20)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.20)
        )
        out = trace_fit.analyse(records)
        assert out["control_gate"]["passed"] is False
        assert out["verdict"] == "VOID"

    def test_frozen_constants_have_their_spec_values(self):
        """These are pre-registered. A change here silently redefines the
        experiment, so pin them as literals against the spec."""
        assert trace_fit.W_KEY == "idents_considered"
        assert trace_fit.CLOSE_BELOW == 1.5
        assert trace_fit.REAL_AT == 2.0
        assert trace_fit.CONTROL_MIN_GROWTH == 2.0
        assert trace_fit.CONTROL_MIN_CHECKPOINTS == 5
        assert trace_fit.MIN_POINTS_PER_GROUP == 30
```

Note the deliberate typo to fix while implementing: `g = trace_gate = trace_fit.control_gate(...)` in `test_growing_checkpoint_duration_passes` should just be `g = trace_fit.control_gate(...)`.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_at_scale_trace_fit.py -v`
Expected: collection error — `evals.at_scale.trace_fit` does not exist.

- [ ] **Step 3: Implement `evals/at_scale/trace_fit.py`**

```python
"""#260: does per-commit ingestion cost grow with history length once per-commit
work size is controlled for?

Pure functions over a per-commit trace (mcp_server._IngestTrace's JSONL). No
I/O, no DB -- so every pre-registered decision here is testable in
milliseconds against synthetic traces rather than behind a 30-minute run.

THE CONSTANTS BELOW ARE PRE-REGISTERED. They were fixed in
docs/superpowers/specs/2026-08-17-per-commit-cost-attribution-design.md before
any trace existed. Do NOT adjust them in response to a result. If a run shows
they were badly chosen, FORK this experiment -- re-baselining a pre-registered
experiment re-evaluates its predictions against a different experiment while
still printing a verdict, which is why probe_ident_collision_census.py's
PREDICTIONS block is frozen rather than updated.

The model, within each third of processed order:

    apply_s = a + b * W        W = idents_considered

Two parameters, because the hypotheses have different shapes and a
zero-intercept "cost per unit work" ratio cannot separate them:

  a grows, b flat  -> per-commit FIXED cost grows with graph size
                      (checkpointing is the prime suspect: _CheckpointPolicy
                      documents db.checkpoint() as O(graph size), ~5.1 ms/MB)
  b grows          -> cost PER UNIT WORK grows with graph size
  both flat        -> the growth is input-driven; #260 is confounded

The fit is identifiable only because #222's converging streams put small-work
(fwd) and large-work (rev) commits at the SAME graph size inside every window.
A single-stream walk would make W and graph size collinear and no fit could
separate a from b -- which is why fit_line returns None rather than a number
when a group has no variance in W.
"""

from __future__ import annotations

from typing import Any, Optional

#: The frozen work metric. See _trace_work_counters for its arithmetic.
W_KEY = "idents_considered"

#: Both ratios strictly below this -> CONFOUNDED.
CLOSE_BELOW = 1.5

#: Either ratio at or above this -> REAL. Matches the threshold
#: bench_introduced_by_query_cost.py fixed for #239 before any data existed.
REAL_AT = 2.0

#: The positive control: mean per-checkpoint duration must grow at least this
#: much, or the method has failed open and the run is VOID.
CONTROL_MIN_GROWTH = 2.0

#: Fewer checkpoints than this in either group makes the control unevaluable.
CONTROL_MIN_CHECKPOINTS = 5

#: Fewer records than this in a group makes its fit untrustworthy.
MIN_POINTS_PER_GROUP = 30


def split_thirds(records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Three equal-count groups in emission (processed) order.

    Not equal spans of wall clock and not equal spans of `pos`. Any remainder
    goes to the MIDDLE group, so the first and last stay the same size -- those
    two are what the verdict compares, and an uneven pair would bias the ratio.
    """
    n = len(records)
    size = n // 3
    if size == 0:
        return records, [], []
    return records[:size], records[size:n - size], records[n - size:]


def fit_line(xs: list[float], ys: list[float]) -> Optional[dict]:
    """OLS fit of y = a + b*x. None when the fit is not identifiable.

    Two None cases, both deliberate:
      - fewer than MIN_POINTS_PER_GROUP points;
      - zero variance in x, where a and b cannot be separated at all. Returning
        a number there would invent an intercept out of nothing.
    """
    n = len(xs)
    if n < MIN_POINTS_PER_GROUP or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0.0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = mean_y - b * mean_x
    sst = sum((y - mean_y) ** 2 for y in ys)
    ssr = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    return {
        "a": a,
        "b": b,
        "r2": (1.0 - ssr / sst) if sst > 0 else 0.0,
        "n": n,
    }


def growth_ratio(first: Optional[float], last: Optional[float]) -> Optional[float]:
    """last/first, or None when that is not a meaningful number.

    A non-positive denominator is None rather than a ratio: an OLS intercept
    can legitimately come out negative, and a ratio taken across zero flips
    sign and reads as a small number -- reporting "flat" for a parameter that
    is nothing of the kind.
    """
    if first is None or last is None:
        return None
    if first <= 0.0:
        return None
    return last / first


def control_gate(first: list[dict], last: list[dict]) -> dict:
    """Does mean per-checkpoint duration grow from the first third to the last?

    Checkpoint cost is documented O(graph size), so this MUST grow. A method
    that cannot detect growth here cannot be trusted when it reports flatness
    anywhere else -- it has failed open, and a clean-looking result from a
    broken measurement is this project's most repeated failure mode.

    Gates on the per-checkpoint MEAN, never the total. The duty policy holds
    aggregate checkpointing to a fixed FRACTION of wall clock as the graph
    grows, so the total is designed not to grow while each checkpoint still
    does; gating on the total would fail on healthy behaviour.
    """
    def mean(group: list[dict]) -> tuple[Optional[float], int]:
        count = sum(int(r.get("ckpt_d_count", 0)) for r in group)
        seconds = sum(float(r.get("ckpt_d_seconds", 0.0)) for r in group)
        if count < CONTROL_MIN_CHECKPOINTS:
            return None, count
        return seconds / count, count

    mean_first, n_first = mean(first)
    mean_last, n_last = mean(last)
    growth = growth_ratio(mean_first, mean_last)

    if mean_first is None or mean_last is None:
        reason = (
            f"unevaluable: need >= {CONTROL_MIN_CHECKPOINTS} checkpoints per "
            f"group, saw {n_first} (first) and {n_last} (last)"
        )
        passed = False
    elif growth is None:
        reason = "unevaluable: first-group mean per-checkpoint duration is not positive"
        passed = False
    elif growth < CONTROL_MIN_GROWTH:
        reason = (
            f"FAILED OPEN: mean per-checkpoint duration grew {growth:.2f}x, "
            f"below the {CONTROL_MIN_GROWTH}x this method must be able to see. "
            f"The run is VOID, not flat."
        )
        passed = False
    else:
        reason = f"passed: mean per-checkpoint duration grew {growth:.2f}x"
        passed = True

    return {
        "passed": passed,
        "growth": growth,
        "mean_first": mean_first,
        "mean_last": mean_last,
        "n_first": n_first,
        "n_last": n_last,
        "reason": reason,
    }


def verdict(a_ratio: Optional[float], b_ratio: Optional[float]) -> tuple[str, str]:
    """The pre-registered verdict on (a_last/a_first, b_last/b_first).

    REAL is checked FIRST because it is a disjunction: either parameter growing
    is enough, so a=2.5 with b=1.1 is REAL and not a mixed case.

    An undefined ratio reads INCONCLUSIVE, never CONFOUNDED. A fit that could
    not be read is not evidence of flatness, and letting it argue for closing
    #260 would be the same fail-open error the control gate exists to prevent.
    """
    if a_ratio is None or b_ratio is None:
        missing = "a" if a_ratio is None else "b"
        return "INCONCLUSIVE", (
            f"{missing}_ratio is undefined -- the fit could not be read, which "
            f"is not evidence of flatness"
        )
    grew = [name for name, r in (("a", a_ratio), ("b", b_ratio)) if r >= REAL_AT]
    if grew:
        return "REAL", (
            f"{' and '.join(grew)} grew >= {REAL_AT}x "
            f"(a={a_ratio:.2f}x, b={b_ratio:.2f}x)"
        )
    if a_ratio < CLOSE_BELOW and b_ratio < CLOSE_BELOW:
        return "CONFOUNDED", (
            f"both parameters below {CLOSE_BELOW}x "
            f"(a={a_ratio:.2f}x, b={b_ratio:.2f}x)"
        )
    return "INCONCLUSIVE", (
        f"in the {CLOSE_BELOW}-{REAL_AT}x band "
        f"(a={a_ratio:.2f}x, b={b_ratio:.2f}x)"
    )


def analyse(records: list[dict]) -> dict:
    """The whole pipeline: split, fit, gate, verdict.

    The control gate is applied LAST and OVERRIDES the fit. A run whose control
    failed reports VOID and does not get to report CONFOUNDED -- see
    control_gate for why that ordering is the point.
    """
    first, middle, last = split_thirds(records)
    fits = {}
    for label, group in (("first", first), ("middle", middle), ("last", last)):
        xs = [float(r[W_KEY]) for r in group]
        ys = [float(r["apply_s"]) for r in group]
        fits[label] = fit_line(xs, ys)

    def param(label: str, key: str) -> Optional[float]:
        f = fits.get(label)
        return None if f is None else f[key]

    a_ratio = growth_ratio(param("first", "a"), param("last", "a"))
    b_ratio = growth_ratio(param("first", "b"), param("last", "b"))
    gate = control_gate(first, last)
    name, why = verdict(a_ratio, b_ratio)
    if not gate["passed"]:
        name, why = "VOID", f"control gate did not pass: {gate['reason']}"

    return {
        "records": len(records),
        "group_sizes": {"first": len(first), "middle": len(middle), "last": len(last)},
        "fits": fits,
        "a_ratio": a_ratio,
        "b_ratio": b_ratio,
        "control_gate": gate,
        "verdict": name,
        "verdict_reason": why,
        "pre_registered": {
            "W_KEY": W_KEY,
            "CLOSE_BELOW": CLOSE_BELOW,
            "REAL_AT": REAL_AT,
            "CONTROL_MIN_GROWTH": CONTROL_MIN_GROWTH,
            "CONTROL_MIN_CHECKPOINTS": CONTROL_MIN_CHECKPOINTS,
            "MIN_POINTS_PER_GROUP": MIN_POINTS_PER_GROUP,
        },
    }
```

- [ ] **Step 4: Run to verify passing**

Run: `.venv/bin/python -m pytest tests/test_at_scale_trace_fit.py -v`
Expected: 24 passed

- [ ] **Step 5: Ablate the control gate**

The gate is the guard the whole experiment rests on, and per this project's
conventions a guard that has never been seen red guarantees nothing. Prove it
bites by breaking it deliberately:

In `control_gate`, temporarily change `elif growth < CONTROL_MIN_GROWTH:` to
`elif False:`. Then:

```bash
.venv/bin/python -m pytest tests/test_at_scale_trace_fit.py -q
```

Expected: `test_flat_checkpoint_duration_fails_the_gate` and
`test_void_when_the_control_gate_fails_regardless_of_the_fit` both FAIL, and
they fail on the assertion that names the defect (`assert g["passed"] is False`
and `assert out["verdict"] == "VOID"`), not on an unrelated error.

**Revert the change** and re-run to confirm green. Record the ablation result in
the commit message.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/trace_fit.py tests/test_at_scale_trace_fit.py
git commit -m "Add trace_fit: the frozen per-commit cost analysis for #260

Fits apply_s = a + b*W within each third of processed order, applies the
pre-registered checkpoint-duration control gate, and returns the verdict. All
constants come from the spec and were fixed before any trace existed.

Three fail-open holes closed by construction: a zero-variance group returns
None rather than an invented intercept; a non-positive denominator makes the
growth ratio None rather than a sign-flipped small number; and an undefined
ratio reads INCONCLUSIVE, never CONFOUNDED, so a fit that could not be read
cannot argue for closing the issue.

Ablation: neutering the gate's threshold comparison turns
test_flat_checkpoint_duration_fails_the_gate and
test_void_when_the_control_gate_fails_regardless_of_the_fit red on the
assertions that name the defect. Reverted, green.

Refs #260"
```

---

### Task 6: `probe_per_commit_cost.py` — the driver

**Files:**
- Create: `evals/at_scale/probe_per_commit_cost.py`
- Test: `tests/test_at_scale_trace_fit.py` (append a `TestProbeIO` class — the probe's I/O, not the fit)

**Interfaces:**
- Consumes: `trace_fit.analyse`; `run_ingestion_benchmark.resolve_graph_path` and `run_ingestion_benchmark.run_ingestion_benchmark` from Task 4.
- Produces: `read_trace(path) -> list[dict]`, `build_result(records, metrics, env) -> dict`, `main(argv=None) -> int`.

**Two modes**, because a 30-minute run must not have to be repeated to re-analyse:

- `--run` drives a fresh full-history benchmark with tracing on, then analyses.
- `--trace <path> --metrics <path>` analyses an existing trace.

**Provenance is mandatory in the artifact**: interpreter path, `minigraf` version, `MINIGRAF_INGEST_STREAM_RATIO` (pinned to the `1:1` default), `MINIGRAF_INGEST_CHECKPOINT_DUTY`, commit count, and the graph/trace paths. Bare `python` carrying minigraf 1.1.1 against a `>=1.2.3` floor has already cost this project a retracted diagnosis on #239; the artifact must prove which interpreter ran.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_at_scale_trace_fit.py`:

```python
class TestProbeIO:
    """#260 probe: reading a trace and assembling the artifact. The fit itself
    is tested in TestAnalyse -- this covers only I/O and provenance."""

    def test_read_trace_skips_blank_lines(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('{"pos": 0}\n\n{"pos": 1}\n')
        from evals.at_scale import probe_per_commit_cost as probe
        assert [r["pos"] for r in probe.read_trace(p)] == [0, 1]

    def test_read_trace_tolerates_a_truncated_final_line(self, tmp_path):
        """A killed run leaves a half-written last record. That must cost one
        record, not the whole 30-minute trace."""
        p = tmp_path / "t.jsonl"
        p.write_text('{"pos": 0, "apply_s": 1.0}\n{"pos": 1, "apply')
        from evals.at_scale import probe_per_commit_cost as probe
        records = probe.read_trace(p)
        assert len(records) == 1

    def test_read_trace_refuses_an_empty_trace(self, tmp_path):
        """Zero records must be a hard error, not a verdict. An empty trace and
        a flat trace are not the same finding."""
        p = tmp_path / "t.jsonl"
        p.write_text("")
        from evals.at_scale import probe_per_commit_cost as probe
        with pytest.raises(SystemExit):
            probe.read_trace(p)

    def test_result_carries_interpreter_and_minigraf_provenance(self, tmp_path):
        from evals.at_scale import probe_per_commit_cost as probe
        records = group(120, a=0.5, b=0.001, ckpt_seconds=0.1) \
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.3) \
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.5)
        result = probe.build_result(records, {"commits_ingested": 360}, {})
        assert "executable" in result["provenance"]
        assert "minigraf_version" in result["provenance"]
        assert result["provenance"]["stream_ratio"] == "1:1"
        assert result["verdict"] == "CONFOUNDED"

    def test_result_records_the_source_metrics_keys_it_used(self, tmp_path):
        from evals.at_scale import probe_per_commit_cost as probe
        records = group(120, a=0.5, b=0.001, ckpt_seconds=0.1) * 3
        result = probe.build_result(records, {"commits_ingested": 42}, {})
        assert result["commits_ingested"] == 42
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_at_scale_trace_fit.py::TestProbeIO -v`
Expected: collection/import error — the probe module does not exist.

- [ ] **Step 3: Implement `evals/at_scale/probe_per_commit_cost.py`**

```python
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
```

- [ ] **Step 4: Run to verify passing**

Run: `.venv/bin/python -m pytest tests/test_at_scale_trace_fit.py -v`
Expected: 29 passed

- [ ] **Step 5: Smoke-test the probe end to end on a tiny repo**

The full run comes in Task 8; this proves the wiring before spending 30 minutes.

```bash
rm -rf /tmp/260-smoke && mkdir -p /tmp/260-smoke
.venv/bin/python evals/at_scale/probe_per_commit_cost.py --run \
    --repo-path . --graph-path /tmp/260-smoke/g.graph \
    --trace /tmp/260-smoke/trace.jsonl \
    --out /tmp/260-smoke/result.json
```

Expected: a trace with one record per commit and a printed verdict. On this repo's full history the verdict is the real result; if you want a fast smoke test, point `--repo-path` at a small throwaway repo instead. Either way, confirm `trace.jsonl` has a record count matching `git rev-list --count master` minus any skipped commits, and that `result.json` carries a `provenance.executable` ending in `.venv/bin/python`.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_per_commit_cost.py tests/test_at_scale_trace_fit.py
git commit -m "Add probe_per_commit_cost, the #260 driver and artifact writer

Two modes: --run drives a fresh traced full-history benchmark through
resolve_graph_path's fresh-path refusal, and a trace+metrics pair re-analyses
without repeating a 30-minute run.

Three reading rules worth their code: a truncated FINAL line costs one record
(at-scale runs get killed mid-write) while an interior malformed line is fatal
(that is corruption, and silently dropping interior records would bias the fit
invisibly); an empty trace raises rather than reporting flat; and a VOID
verdict exits non-zero so a failed measurement is not recorded as a clean
result. Provenance records the interpreter and minigraf version, since bare
python against the >=1.2.3 floor has already faked one diagnosis on #239.

Refs #260"
```

---

### Task 7: `benchmark.md` rendering

**Files:**
- Modify: `evals/at_scale/report.py`
- Modify: `evals/at_scale/benchmark.md` (reader's note)
- Test: `tests/test_at_scale_report.py` — append a class beside `TestAppendResidueReport` (line 328), which is the direct template

**Interfaces:**
- Consumes: `build_result`'s return from Task 6.
- Produces: `append_trace_fit_report(result: dict, report_path: Path, json_out_path: Optional[Path] = None) -> None`, mirroring `append_residue_report`'s signature (`report.py:322`).

**Background the implementer needs.** Read `append_residue_report` (`report.py:322`) and `_residue_verdict_row` (`report.py:191`) first — this is the same shape, and the row helpers `_relative_to_report` (`:17`) and `_metrics_json_bullet` (`:38`) are already there to reuse.

**The three-state discipline is mandatory** and comes straight from #275/#276: an **absent** key renders `not measured`, an **empty** measured value renders as a measured zero/none, and a **populated** value renders the data. Absence must never render as a zero. The nightly does not run this probe, so most `benchmark.md` entries will have no trace-fit section at all — and the reader's note must say that absence means "not run", never "no growth".

- [ ] **Step 1: Write the failing test**

```python
class TestAppendTraceFitReport:
    def test_renders_verdict_ratios_and_control_gate(self, tmp_path):
        from evals.at_scale.report import append_trace_fit_report
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        append_trace_fit_report({
            "verdict": "CONFOUNDED",
            "verdict_reason": "both parameters below 1.5x (a=1.10x, b=1.05x)",
            "a_ratio": 1.10, "b_ratio": 1.05,
            "records": 760,
            "group_sizes": {"first": 253, "middle": 254, "last": 253},
            "control_gate": {"passed": True, "growth": 4.2, "reason": "passed: ..."},
            "commits_ingested": 767,
        }, report)
        text = report.read_text()
        assert "CONFOUNDED" in text
        assert "1.10" in text and "1.05" in text
        assert "4.2" in text or "4.20" in text

    def test_void_verdict_is_rendered_as_void_not_as_flat(self, tmp_path):
        """A void run must be unmistakable in the record. #276's whole lesson
        was that a self-misdescribing record is the defect."""
        from evals.at_scale.report import append_trace_fit_report
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        append_trace_fit_report({
            "verdict": "VOID",
            "verdict_reason": "control gate did not pass: FAILED OPEN ...",
            "a_ratio": 1.01, "b_ratio": 1.02, "records": 760,
            "group_sizes": {"first": 253, "middle": 254, "last": 253},
            "control_gate": {"passed": False, "growth": 1.1, "reason": "FAILED OPEN ..."},
        }, report)
        text = report.read_text()
        assert "VOID" in text
        assert "CONFOUNDED" not in text

    def test_absent_ratio_renders_not_measured_never_zero(self, tmp_path):
        from evals.at_scale.report import append_trace_fit_report
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n")
        append_trace_fit_report({
            "verdict": "INCONCLUSIVE",
            "verdict_reason": "a_ratio is undefined",
            "a_ratio": None, "b_ratio": 1.2, "records": 760,
            "group_sizes": {"first": 253, "middle": 254, "last": 253},
            "control_gate": {"passed": True, "growth": 3.0, "reason": "passed"},
        }, report)
        text = report.read_text()
        assert "not measured" in text
        assert "0.00" not in text

    def test_appends_rather_than_truncating(self, tmp_path):
        from evals.at_scale.report import append_trace_fit_report
        report = tmp_path / "benchmark.md"
        report.write_text("# At-Scale Code-Graph Benchmark\n\n## Existing entry\n")
        append_trace_fit_report({
            "verdict": "CONFOUNDED", "verdict_reason": "x",
            "a_ratio": 1.0, "b_ratio": 1.0, "records": 100,
            "group_sizes": {"first": 33, "middle": 34, "last": 33},
            "control_gate": {"passed": True, "growth": 3.0, "reason": "passed"},
        }, report)
        assert "## Existing entry" in report.read_text()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py::TestAppendTraceFitReport -v`
Expected: FAIL — `cannot import name 'append_trace_fit_report'`

- [ ] **Step 3: Implement `append_trace_fit_report` in `report.py`**

Follow `append_residue_report`'s structure exactly. Add a `_ratio_row(label, value)` helper that renders `not measured` for `None`, and never `0.00`:

```python
def _ratio_row(label: str, value: Any) -> str:
    """A growth ratio row. None renders `not measured`, NEVER 0.00.

    An undefined ratio means the fit could not be read. Rendering it as a
    number -- especially a small one -- would make a failed measurement read
    as a flat result, which is the exact defect #276 was filed about.
    """
    if value is None:
        return f"- {label}: not measured\n"
    return f"- {label}: {float(value):.2f}x\n"
```

The section itself should carry: the verdict and its reason, `a_ratio`/`b_ratio` via `_ratio_row`, the control-gate outcome and growth, record count and group sizes, and a `- Metrics JSON:` provenance bullet via `_metrics_json_bullet` when `json_out_path` is given.

- [ ] **Step 4: Run to verify passing**

Run: `.venv/bin/python -m pytest tests/test_at_scale_report.py -v`
Expected: all passing, including the existing report tests.

- [ ] **Step 5: Add the reader's note to `benchmark.md`**

Find the existing reader's note about the absent residue section (added by PR #278) and add an adjacent paragraph in the same voice:

```markdown
**An absent per-commit cost-fit section means the #260 probe was not run** —
never that cost was flat. The nightly does not run
`probe_per_commit_cost.py`, so most entries below have no such section. A
`VOID` verdict means the run's positive control failed and its numbers say
nothing; it is not a flat result.
```

Before writing this, **open an actual older entry** and confirm what it does and does not contain. PR #278 shipped a note claiming older entries "render `not recorded`" when in fact they were already-written text with no such line at all — on a branch whose thesis was that a self-misdescribing record is the defect. Do not repeat that.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/report.py evals/at_scale/benchmark.md tests/
git commit -m "Render the #260 per-commit cost fit into benchmark.md

append_trace_fit_report mirrors append_residue_report's shape and keeps the
three-state discipline: an absent ratio renders 'not measured', never 0.00, so
a fit that could not be read cannot read as a flat result. A VOID verdict
renders as VOID and never as CONFOUNDED.

The reader's note says an absent section means the probe was not run, verified
by opening an older entry rather than by reading the renderer.

Refs #260"
```

---

### Task 8: Run it, and answer #260

**Files:**
- Create: `evals/at_scale/results/260-per-commit-cost-attribution.json`
- Modify: `evals/at_scale/benchmark.md` (the run's entry)

**Interfaces:** consumes everything above. Produces the artifact and the verdict.

- [ ] **Step 1: Confirm the interpreter before spending 30 minutes**

```bash
.venv/bin/python -c "from importlib.metadata import version; print(version('minigraf'))"
grep -n 'minigraf>=' pyproject.toml
```

Expected: the installed version satisfies the `>=1.2.3` floor. If it does not, **stop** — every number from this run would be worthless, which is exactly what happened on #239.

- [ ] **Step 2: Confirm the full suite is green first**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: master's 1498 passed + this branch's new tests, 1 xfailed. A red suite invalidates the run.

- [ ] **Step 3: Run the traced full-history ingestion**

```bash
rm -rf /tmp/260-run && mkdir -p /tmp/260-run
.venv/bin/python evals/at_scale/probe_per_commit_cost.py --run \
    --repo-path . --branch master \
    --graph-path /tmp/260-run/g.graph \
    --trace /tmp/260-run/trace.jsonl \
    2>&1 | tee /tmp/260-run/console.log
```

Expected: ~30 minutes, ~767 commits. Run it in the background and poll with `ps -p <PID>` — **not** `pgrep -f`, which matches the polling shell's own command line and never goes false.

- [ ] **Step 4: Sanity-check the trace before reading the verdict**

```bash
.venv/bin/python - <<'EOF'
import json
rs=[json.loads(l) for l in open('/tmp/260-run/trace.jsonl') if l.strip()]
print("records:", len(rs))
print("tags:", {t: sum(1 for r in rs if r["tag"]==t) for t in ("fwd","rev")})
print("zero-work records:", sum(1 for r in rs if r["idents_considered"]==0))
print("zero-apply records:", sum(1 for r in rs if r["apply_s"]<=0))
print("checkpoints:", sum(r["ckpt_d_count"] for r in rs))
EOF
git rev-list --count master
```

Four things to check, each of which would invalidate the fit:
- record count close to `git rev-list --count master` (a large shortfall means commits were skipped — check the console log for `skipping`),
- both `fwd` and `rev` present and roughly balanced (the 1:1 claimer; a one-sided trace means the fit's decorrelation precondition failed),
- `zero-apply` is 0,
- total checkpoints comfortably above `2 * CONTROL_MIN_CHECKPOINTS`.

**Compare against `git rev-list --count master`, not `HEAD`** — the benchmark defaults to master, and this branch's own commits are not in it.

- [ ] **Step 5: Write the artifact and the benchmark.md entry**

The `--run` already wrote `results/260-per-commit-cost-attribution.json`. Append the report entry:

```bash
.venv/bin/python - <<'EOF'
import json
from pathlib import Path
from evals.at_scale.report import append_trace_fit_report
out = Path("evals/at_scale/results/260-per-commit-cost-attribution.json")
append_trace_fit_report(json.loads(out.read_text()),
                        Path("evals/at_scale/benchmark.md"), out)
EOF
```

- [ ] **Step 6: Commit the artifact**

```bash
git add evals/at_scale/results/260-per-commit-cost-attribution.json evals/at_scale/benchmark.md
git commit -m "Record the #260 per-commit cost attribution run

<N> commits, <T>s wall clock, verdict <V>. Control gate: <passed/failed>,
mean per-checkpoint duration grew <G>x.

<One paragraph on what the numbers say -- write this from the artifact, not
from the hypothesis. If the verdict contradicts the confound hypothesis in the
spec, say so plainly here.>

Refs #260"
```

- [ ] **Step 7: Post the verdict on #260**

Write a comment covering, in this order: the verdict and its reason; the control-gate result **stated alongside it, not omitted**; `a` and `b` with their fits' `r2` and group sizes; the confound measurement that motivated the run; and what should happen next.

Branch on the verdict:

- **CONFOUNDED** → recommend closing #260, and say explicitly that the growth is input-driven (whole-file extraction over a repository whose files grew) rather than a code defect. Note that Stage B is untraced and confounded by the same mechanism via the double-parse `N · (1 + reverse_fraction)`, so closing #260 does not clear Stage B.
- **REAL** → #260 stays open. Name which parameter moved and point the next piece of work at `profile_forward_reconcile_attribution.py`, which drives both stages and merges cProfile across threads.
- **INCONCLUSIVE** → report both parameters and put the decision to the user. Do not pick a side.
- **VOID** → post that the measurement failed its own control and give no verdict on the growth question.

**Do not use a closing keyword.** `gh issue comment 260` bodies do not auto-close, but the PR body does — so keep the PR body to `Refs #260` and verify with `gh pr view --json closingIssuesReferences` before merging.

- [ ] **Step 8: Update the project memory**

Update `project_222_multistream_ingestion_phases.md`: the #260 outcome, the confound numbers, the frozen constants' location, and where the trace hook lives. If the verdict is CONFOUNDED, record that #239's remaining priority is unchanged by it and that Stage B was never measured.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the trace hook and its record table → Tasks 1–3; `--trace-path` and provenance → Tasks 4, 6; the fresh-path rule → Task 6 (via `resolve_graph_path`); the model and the thirds fit → Task 5; the pre-registered block including `W`, the control gate and the verdict → Task 5; artifact → Task 6; `benchmark.md` → Task 7; the issue comment → Task 8; docs sync (CLAUDE.md + SKILL.md check) → Task 3. The spec's "Not measured, deliberately" scope decisions (extraction duration, Stage B) are carried as explicit non-goals in Task 3 and surfaced again in Task 8's CONFOUNDED branch. The `await_s`-correlates-with-`W` sanity check is Task 6's `exploratory.mean_await_s` plus Task 8's Step 4.

**Type consistency.** `_trace_work_counters` returns the dict Task 2 spreads into each record; `W_KEY = "idents_considered"` matches the key Task 1 produces; `control_gate` reads `ckpt_d_count`/`ckpt_d_seconds`, exactly the names Task 2 writes; `append_trace_fit_report(result, report_path, json_out_path=None)` matches `append_residue_report`'s existing signature; `run_ingestion_benchmark(..., trace_path=None)` is called with that keyword in Tasks 4 and 6.

**Placeholder scan: clean.** Every test module, fixture and line anchor below was read off this branch rather than guessed, after a first draft got three of them wrong:

| resolved | first draft had |
|---|---|
| `git_repo` fixture, `tests/test_mcp_server.py:6873`, **2 commits** | "build a 5-commit repo with the existing helper" — there is no shared builder, and the two commits make the record count exact |
| `@pytest.mark.asyncio async def` + `await mcp_server._run_ingestion(...)` | `asyncio.run(...)`, which is not this suite's pattern |
| `tests/test_at_scale_ingestion_benchmark.py` (own `git_repo` at :119, `poll_interval=0.05`) | `tests/test_at_scale_benchmark.py` |
| `tests/test_at_scale_report.py` (`TestAppendResidueReport` at :328) | `tests/test_report.py` |
| `tests/test_at_scale_trace_fit.py` | `tests/test_trace_fit.py` — the suite's convention is the `test_at_scale_*` prefix |

**The one remaining gap:** Task 8's commit message and issue comment are outcome-dependent and cannot be pre-written. Their required *content* is fully specified, including the branch per verdict.
