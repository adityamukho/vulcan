# Issue #60 Lazy Graph Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove import-time graph directory creation from `minigraf_tool` while preserving file-backed behavior for explicit paths and default-path writes.

**Architecture:** Keep the wrapper file-backed, but separate path resolution from path materialization. The module will compute the default graph path without filesystem side effects and create parent directories only in write paths that need them.

**Tech Stack:** Python 3, pytest, unittest.mock, minigraf CLI wrapper

---

### Task 1: Add a regression test for import-time side effects

**Files:**
- Modify: `tests/test_advanced.py`
- Test: `tests/test_advanced.py`

- [ ] **Step 1: Write the failing test**

```python
def test_import_does_not_create_default_graph_dir(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    module_name = "minigraf_tool"
    import sys
    sys.modules.pop(module_name, None)

    with patch.dict(os.environ, {"HOME": str(fake_home)}, clear=False), \
         patch("pathlib.Path.mkdir", side_effect=AssertionError("mkdir should not run during import")):
        imported = importlib.import_module(module_name)

    expected = fake_home / ".local" / "share" / "temporal-reasoning" / "memory.graph"
    assert imported.DEFAULT_GRAPH_PATH == str(expected)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_advanced.py::test_import_does_not_create_default_graph_dir -q`
Expected: FAIL because `minigraf_tool` currently calls `Path.mkdir()` during import.

- [ ] **Step 3: Write minimal implementation**

```python
def _resolve_default_graph_path() -> str:
    ...
    return str(graph_dir / "memory.graph")


def _ensure_parent_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
```

Update the module to use `_resolve_default_graph_path()` at import time and call `_ensure_parent_dir()` only from write paths such as `transact()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_advanced.py::test_import_does_not_create_default_graph_dir -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-04-08-issue-60-lazy-graph-path.md tests/test_advanced.py minigraf_tool.py
git commit -m "fix: avoid import-time graph path creation"
```

### Task 2: Verify default-path write behavior still works

**Files:**
- Modify: `tests/test_advanced.py`
- Modify: `minigraf_tool.py`
- Test: `tests/test_advanced.py`

- [ ] **Step 1: Write the failing test**

```python
def test_transact_creates_default_graph_parent_dir_lazily(tmp_path, mock_minigraf):
    fake_graph = tmp_path / "nested" / "memory.graph"

    with patch.object(minigraf_tool, "DEFAULT_GRAPH_PATH", str(fake_graph)):
        result = minigraf_tool.transact(
            "[[:test :person/name \"Alice\"]]",
            reason="create graph on first write"
        )

    assert result["ok"]
    assert fake_graph.parent.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_advanced.py::test_transact_creates_default_graph_parent_dir_lazily -q`
Expected: FAIL because the wrapper does not currently ensure parent directory creation at write time.

- [ ] **Step 3: Write minimal implementation**

```python
def transact(...):
    ...
    _ensure_parent_dir(path)
    result = _run_minigraf(["--file", path], input_data=full_tx)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_advanced.py::test_transact_creates_default_graph_parent_dir_lazily -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_advanced.py minigraf_tool.py
git commit -m "test: cover lazy default graph directory creation"
```

### Task 3: Run regression verification

**Files:**
- Test: `tests/test_advanced.py`
- Test: `tests/test_minigraf_tool.py`

- [ ] **Step 1: Run focused tests**

Run: `pytest tests/test_advanced.py tests/test_minigraf_tool.py -q`
Expected: PASS

- [ ] **Step 2: Run full suite**

Run: `pytest -q`
Expected: PASS with all tests green

- [ ] **Step 3: Inspect diff**

Run: `git diff -- minigraf_tool.py tests/test_advanced.py docs/superpowers/plans/2026-04-08-issue-60-lazy-graph-path.md`
Expected: Only the lazy-path fix, tests, and plan doc changes appear

- [ ] **Step 4: Commit**

```bash
git add minigraf_tool.py tests/test_advanced.py docs/superpowers/plans/2026-04-08-issue-60-lazy-graph-path.md
git commit -m "fix: defer graph directory creation until write time"
```
