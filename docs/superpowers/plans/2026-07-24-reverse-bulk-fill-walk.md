# Reverse-Bulk-Fill Walk (Stream 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build sub-phase 2b of #222's phase 2: Stream 2's actual reverse-bulk-fill walk, which claims commits from the frontier gap's high end and writes structural facts, `:modified-in` edges, and provisional `:introduced-by` lineage using phase 2a's primitives. No caller wired into `_run_ingestion` yet — 2d wires the real concurrency.

**Architecture:** Reuses `_extract_commit` (already a pure, direction-agnostic per-commit diff/parse) and `_build_code_triples` (already gates "write structural facts once" purely by dict membership) unchanged. The only new mechanism is a DB-query-based substitute for forward walk's in-memory `entity_valid_from` accumulation, since reverse walk has no accumulated-forward state to consult — "is this entity already known" becomes "does it already have an `:introduced-by` fact", and 2a's `_lineage_is_provisional` distinguishes a confirmed fact from a movable guess.

**Tech Stack:** Python 3, minigraf Datalog (via existing `_db_execute`/`_transact`/`_retract`/`_edn_escape`/`_db_checkpoint` helpers), `frontier_registry.FrontierAllocator` (phase 1), 2a's lineage/candidate-diff primitives, pytest, real git repos under `tmp_path`.

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-07-24-reverse-bulk-fill-walk-design.md` — every fix and decision in it is binding. In particular:
  - Out of scope for this sub-phase: `:depends-on` edges, renames (`:renamed-from`/`:renamed-to`), and deletion/close handling in the reverse direction. Skip `"D"` and `"R"` status files entirely in the new walk.
  - `:introduced-by` is written via ONE code path only in the new reverse-walk functions: `_entity_introduced_by_set_provisional` (Task 1). `_build_code_triples`'s own candidate `:introduced-by` triples must be filtered out of whatever gets transacted by the reverse walk, so there is never a second writer for the same attribute.
  - Every new write in Tasks 1/3/4 uses the internal `_transact`/`_retract` helpers directly, never `handle_minigraf_transact`/`handle_minigraf_retract` (`:introduced-by`/`:modified-in` are already-registered attributes on registered types, so this constraint is about matching existing internal-engine style, not schema/audit safety — see the design spec's "Schema/audit safety" section).
  - Each claimed commit's full write (structural facts, lineage moves, candidate-diff persists, frontier claim) is followed by exactly one `_db_checkpoint(db)` call, mirroring `_run_ingestion`'s own one-checkpoint-per-commit cadence (see the design spec's "Resume-safety / atomicity boundary" section).
- Follow `docs/testing-conventions.md`: every test uses a real `MiniGrafDb` (`real_db` fixture) and, where a git repo is needed, a real repo under `tmp_path` — never mocked.
- No caller wired into `_run_ingestion`. Nothing outside the new functions themselves changes existing behavior.

---

### Task 1: `_entity_introduced_by_query` / `_entity_introduced_by_set_provisional`

**Files:**
- Modify: `mcp_server.py` — insert immediately after `_candidate_diff_clear` (currently ends at line 5225), before `_LAST_RUN_KEYWORD_ATTRS` (currently line 5228).
- Test: `tests/test_mcp_server.py` — new `TestEntityIntroducedBySetProvisional` class.

**Interfaces:**
- Consumes: `_db_execute(db, datalog) -> str`, `_transact(db, datalog_facts, valid_from, index_con=None) -> str`, `_retract(db, datalog_facts, index_con=None) -> str`, `_lineage_mark_provisional(db, entity_ident, commit_ts_iso, index_con=None) -> None`, `_lineage_is_provisional(db, entity_ident) -> bool` (all pre-existing, phase 2a).
- Produces:
  - `_entity_introduced_by_query(db, entity_ident: str) -> Optional[str]`
  - `_entity_introduced_by_set_provisional(db, entity_ident: str, commit_ident: str, commit_ts_iso: str, index_con=None) -> None`

- [ ] **Step 1: Write the failing tests**

Add this test class to `tests/test_mcp_server.py` (near the existing `TestLineageProvisionalMarker`/`TestCandidateDiff` classes):

```python
class TestEntityIntroducedBySetProvisional:
    def test_query_absent_returns_none(self, real_db):
        import mcp_server
        assert mcp_server._entity_introduced_by_query(real_db, ":function/src-auth-py-login") is None

    def test_first_assert_is_provisional(self, real_db):
        import mcp_server
        db = real_db
        entity_ident = ":function/src-auth-py-login"

        mcp_server._entity_introduced_by_set_provisional(db, entity_ident, ":commit/h2", "2026-01-03T00:00:00Z")

        assert mcp_server._entity_introduced_by_query(db, entity_ident) == ":commit/h2"
        assert mcp_server._lineage_is_provisional(db, entity_ident) is True

    def test_moving_provisional_value_retracts_old_and_asserts_new(self, real_db):
        import mcp_server
        db = real_db
        entity_ident = ":function/src-auth-py-login"

        mcp_server._entity_introduced_by_set_provisional(db, entity_ident, ":commit/h2", "2026-01-03T00:00:00Z")
        mcp_server._entity_introduced_by_set_provisional(db, entity_ident, ":commit/h0", "2026-01-01T00:00:00Z")

        assert mcp_server._entity_introduced_by_query(db, entity_ident) == ":commit/h0"
        raw = mcp_server._db_execute(
            db, f"(query [:find (count ?c) :where [{entity_ident} :introduced-by ?c]])"
        )
        assert json.loads(raw)["results"] == [[1]]

    def test_same_value_twice_is_idempotent(self, real_db):
        import mcp_server
        db = real_db
        entity_ident = ":function/src-auth-py-login"

        mcp_server._entity_introduced_by_set_provisional(db, entity_ident, ":commit/h1", "2026-01-02T00:00:00Z")
        mcp_server._entity_introduced_by_set_provisional(db, entity_ident, ":commit/h1", "2026-01-02T00:00:01Z")

        raw = mcp_server._db_execute(
            db, f"(query [:find (count ?c) :where [{entity_ident} :introduced-by ?c]])"
        )
        assert json.loads(raw)["results"] == [[1]]

    def test_never_clobbers_an_authoritative_fact(self, real_db):
        import mcp_server
        db = real_db
        entity_ident = ":function/src-auth-py-login"

        # Simulate forward walk (Stream 1) having already confirmed this
        # entity authoritatively -- a plain _transact, no lineage marker.
        mcp_server._transact(
            db, f"[[{entity_ident} :introduced-by :commit/original]]", "2026-01-01T00:00:00Z",
        )

        mcp_server._entity_introduced_by_set_provisional(db, entity_ident, ":commit/h5", "2026-01-05T00:00:00Z")

        assert mcp_server._entity_introduced_by_query(db, entity_ident) == ":commit/original"
        assert mcp_server._lineage_is_provisional(db, entity_ident) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestEntityIntroducedBySetProvisional -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_entity_introduced_by_query'`.

- [ ] **Step 3: Add the functions to `mcp_server.py`**

Insert immediately after `_candidate_diff_clear` (before `_LAST_RUN_KEYWORD_ATTRS`):

```python
def _entity_introduced_by_query(db: Any, entity_ident: str) -> Optional[str]:
    """Return entity_ident's current :introduced-by value (a commit ident
    string), or None if it has none yet."""
    raw = _db_execute(db, f"(query [:find ?c :where [{entity_ident} :introduced-by ?c]])")
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _entity_introduced_by_set_provisional(
    db: Any,
    entity_ident: str,
    commit_ident: str,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """Assert or move entity_ident's PROVISIONAL :introduced-by to
    commit_ident. Never touches an entity whose :introduced-by is already
    authoritative (a fact exists and _lineage_is_provisional is False) --
    reverse walk (#222 phase 2b) must never clobber a fact Stream 1 has
    already confirmed. Idempotent: no-ops the fact write (but still ensures
    the marker is present) if the current value already equals
    commit_ident. Query-before-write, retract-then-reassert only if the
    value genuinely changed -- mirrors _watermark_update's pattern, since
    re-transacting the same (entity, attribute, value) at a new valid_from
    creates a duplicate live datom under minigraf's write semantics.
    """
    current = _entity_introduced_by_query(db, entity_ident)
    if current is not None and not _lineage_is_provisional(db, entity_ident):
        return  # authoritative -- never touch
    if current == commit_ident:
        _lineage_mark_provisional(db, entity_ident, commit_ts_iso, index_con=index_con)
        return
    if current is not None:
        _retract(db, f"[[{entity_ident} :introduced-by {current}]]", index_con=index_con)
    _transact(db, f"[[{entity_ident} :introduced-by {commit_ident}]]", commit_ts_iso, index_con=index_con)
    _lineage_mark_provisional(db, entity_ident, commit_ts_iso, index_con=index_con)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestEntityIntroducedBySetProvisional -v`
Expected: PASS (all 5 tests).

Also run the full existing suite to confirm no regression:

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: PASS (no new failures beyond pre-existing, unrelated ones).

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _entity_introduced_by_query/_set_provisional for #222 phase 2b

Query-before-write helper pair for reading/moving an entity's
:introduced-by fact while it's still provisional. Never touches an
already-authoritative fact (Stream 1's confirmed lineage is untouchable).
Idempotent by query-before-write, writes via internal _transact/_retract
only. No caller yet -- Task 3 wires this into the reverse walk."
```

---

### Task 2: `_precompute_file_triples` exposes per-entity body hashes

**Files:**
- Modify: `mcp_server.py` — `_precompute_file_triples` (currently starts at line 6235, return statement at lines 6385-6396).
- Test: `tests/test_mcp_server.py` — new tests added to the existing `TestPrecomputeFileTriplesBodyDiff` class (currently starts at line 8426).

**Interfaces:**
- Consumes: `_normalized_body_hash(node) -> str` (pre-existing, #221), `new_entity_nodes` (an existing parameter of `_precompute_file_triples`, already populated by `_extract_commit` for every `"A"`/`"M"`/`"R"` file — see the design spec's "Why `_extract_commit` needs no changes" section).
- Produces: a new `"body_hashes": Dict[str, str]` key in `_precompute_file_triples`'s returned dict, mapping each function/class/variable/field ident present in `new_entity_nodes` to its `_normalized_body_hash`. (Module idents are deliberately excluded — there is no tree-sitter node to hash for a whole file; the design spec's candidate-diff persistence is function/class/variable/field-only, matching #221's own `unchanged_idents` category scope.)

- [ ] **Step 1: Write the failing tests**

Add these tests to the existing `TestPrecomputeFileTriplesBodyDiff` class in `tests/test_mcp_server.py` (it already has a `_python_parser`/`_all_nodes` helper pair — reuse them, don't redefine):

```python
    def test_body_hashes_populated_for_every_new_side_function(self):
        import mcp_server
        parser = self._python_parser()
        new_nodes = self._all_nodes(parser, b"def login(user):\n    return user.ok\n")
        extracted = {"functions": ["login"], "classes": [], "imports": []}
        result = mcp_server._precompute_file_triples(
            "auth.py", extracted, ":commit/c1", {}, new_entity_nodes=new_nodes,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        assert result["body_hashes"][fn_ident] == mcp_server._normalized_body_hash(new_nodes["function"]["login"])

    def test_body_hashes_match_across_textually_identical_bodies(self):
        import mcp_server
        parser = self._python_parser()
        nodes_a = self._all_nodes(parser, b"def login(user):\n    return user.ok\n")
        nodes_b = self._all_nodes(parser, b"def login(user):\n\n    return   user.ok\n")
        extracted = {"functions": ["login"], "classes": [], "imports": []}
        result_a = mcp_server._precompute_file_triples(
            "auth.py", extracted, ":commit/c1", {}, new_entity_nodes=nodes_a,
        )
        result_b = mcp_server._precompute_file_triples(
            "auth.py", extracted, ":commit/c2", {}, new_entity_nodes=nodes_b,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        assert result_a["body_hashes"][fn_ident] == result_b["body_hashes"][fn_ident]

    def test_body_hashes_absent_without_new_entity_nodes(self):
        import mcp_server
        extracted = {"functions": ["login"], "classes": [], "imports": []}
        result = mcp_server._precompute_file_triples("auth.py", extracted, ":commit/c1", {})
        assert result["body_hashes"] == {}

    def test_module_ident_never_appears_in_body_hashes(self):
        import mcp_server
        parser = self._python_parser()
        new_nodes = self._all_nodes(parser, b"def login(user):\n    return user.ok\n")
        extracted = {"functions": ["login"], "classes": [], "imports": []}
        result = mcp_server._precompute_file_triples(
            "auth.py", extracted, ":commit/c1", {}, new_entity_nodes=new_nodes,
        )
        module_ident = mcp_server._code_ident("module", "auth.py")
        assert module_ident not in result["body_hashes"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestPrecomputeFileTriplesBodyDiff -v`
Expected: FAIL with `KeyError: 'body_hashes'`.

- [ ] **Step 3: Add `body_hashes` computation to `_precompute_file_triples`**

Modify the existing block immediately before the function's `return` statement (currently lines 6372-6396) — change:

```python
    unchanged_idents: Set[str] = set()
    try:
        old_nodes = old_entity_nodes or {}
        new_nodes = new_entity_nodes or {}
        for category in ("function", "class", "variable", "field"):
            old_cat = old_nodes.get(category, {})
            new_cat = new_nodes.get(category, {})
            for name in old_cat.keys() & new_cat.keys():
                if _normalized_body_hash(old_cat[name]) == _normalized_body_hash(new_cat[name]):
                    unchanged_idents.add(_code_ident(category, file_path, name))
    except Exception:
        unchanged_idents = set()

    return {
        "module_ident": module_ident,
        "module_candidate_triples": module_candidate_triples,
        "function_entries": function_entries,
        "class_entries": class_entries,
        "global_entries": global_entries,
        "field_entries": field_entries,
        "field_class_map": field_class_map,
        "field_static_map": field_static_map,
        "resolved_imports": resolved_imports,
        "unchanged_idents": unchanged_idents,
    }
```

to:

```python
    unchanged_idents: Set[str] = set()
    try:
        old_nodes = old_entity_nodes or {}
        new_nodes = new_entity_nodes or {}
        for category in ("function", "class", "variable", "field"):
            old_cat = old_nodes.get(category, {})
            new_cat = new_nodes.get(category, {})
            for name in old_cat.keys() & new_cat.keys():
                if _normalized_body_hash(old_cat[name]) == _normalized_body_hash(new_cat[name]):
                    unchanged_idents.add(_code_ident(category, file_path, name))
    except Exception:
        unchanged_idents = set()

    # #222 phase 2b: per-entity body hash for every entity present on the
    # NEW side, keyed the same way unchanged_idents is above -- lets the
    # reverse-bulk-fill walk persist a candidate-diff record (body hash)
    # for an entity without needing the raw tree-sitter node itself.
    # Module-level entries are deliberately excluded: there is no
    # tree-sitter node to hash for a whole file, and candidate-diff
    # persistence is function/class/variable/field-only (matching
    # unchanged_idents' own category scope).
    body_hashes: Dict[str, str] = {}
    try:
        new_nodes_for_hash = new_entity_nodes or {}
        for category in ("function", "class", "variable", "field"):
            for name, node in new_nodes_for_hash.get(category, {}).items():
                body_hashes[_code_ident(category, file_path, name)] = _normalized_body_hash(node)
    except Exception:
        body_hashes = {}

    return {
        "module_ident": module_ident,
        "module_candidate_triples": module_candidate_triples,
        "function_entries": function_entries,
        "class_entries": class_entries,
        "global_entries": global_entries,
        "field_entries": field_entries,
        "field_class_map": field_class_map,
        "field_static_map": field_static_map,
        "resolved_imports": resolved_imports,
        "unchanged_idents": unchanged_idents,
        "body_hashes": body_hashes,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestPrecomputeFileTriplesBodyDiff -v`
Expected: PASS (all tests in the class, including the 4 new ones).

Also run the full existing suite, and specifically every existing caller of `_precompute_file_triples`/`_build_code_triples`/`_extract_commit` (a new dict key is additive and must not break any existing consumer that doesn't know about it):

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add per-entity body_hashes to _precompute_file_triples for #222 phase 2b

Additive dict key, computed from new_entity_nodes (already parsed for
every A/M/R file, no new plumbing) the same way #221's unchanged_idents
already is. Lets the reverse-bulk-fill walk (Task 3) persist a
candidate-diff body hash without needing the raw tree-sitter node.
Module idents excluded -- no node to hash for a whole file."
```

---

### Task 3: `_reverse_fill_claim_and_process`

**Files:**
- Modify: `mcp_server.py` — insert immediately after `_extract_commit` (currently ends at line 7192), before `_run_ingestion` (currently line 7195).
- Test: `tests/test_mcp_server.py` — new `TestReverseFillClaimAndProcess` class.

**Interfaces:**
- Consumes: `_extract_commit(repo_path, commit_hash, ignore_patterns) -> tuple` (pre-existing), `_build_code_triples(...)` (pre-existing), `_entity_introduced_by_query`/`_entity_introduced_by_set_provisional` (Task 1), `_lineage_is_provisional`/`_candidate_diff_persist` (phase 2a), `_frontier_persist_claim` (phase 1), `_db_checkpoint(db)` (pre-existing), `frontier_registry.FrontierAllocator.claim_high() -> Optional[int]` (phase 1).
- Produces: `_reverse_fill_claim_and_process(db, repo_path: str, linearization: List[str], commit_metadata: List[Tuple[str, str, str, str]], allocator: "frontier_registry.FrontierAllocator", ignore_patterns: Sequence[str] = (), index_con=None) -> Optional[str]`

`commit_metadata` is `linearization`-position-indexed, same shape `_git_commits` already returns: `(hash, ts_iso, author, subject)` per position. Tests build it with `_git_commits(str(repo), watermark_hash=None)` against the same repo `linearization` came from — both use `git log --topo-order --reverse`, so the two lists share the same order/indices.

- [ ] **Step 1: Write the failing tests**

Add near the top of `tests/test_mcp_server.py` if not already present: `import frontier_registry` (already added in phase 1's Task 3 — check first, don't duplicate the import).

Add this test class:

```python
class TestReverseFillClaimAndProcess:
    def _repo_with_evolving_function(self, tmp_path):
        """Three commits, each genuinely changing auth.py's login() body (not
        just whitespace -- #221's unchanged-body detection would otherwise
        suppress :modified-in and complicate the assertions below), plus an
        unrelated function added at h1/h2 so the file is in each commit's
        diff for a real reason."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

        (repo / "auth.py").write_text("def login():\n    return 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h0"], cwd=repo, check=True, capture_output=True)

        (repo / "auth.py").write_text("def login():\n    return 2\n\ndef extra():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h1"], cwd=repo, check=True, capture_output=True)

        (repo / "auth.py").write_text(
            "def login():\n    return 3\n\ndef extra():\n    pass\n\ndef more():\n    pass\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h2"], cwd=repo, check=True, capture_output=True)
        return repo

    def _allocator_and_metadata(self, repo, real_db):
        import mcp_server
        import frontier_registry
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        return linearization, commit_metadata, allocator

    def test_gap_empty_returns_none_and_writes_nothing(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        linearization, commit_metadata, allocator = self._allocator_and_metadata(repo, real_db)
        # Drain the gap entirely first (forward-claim everything low).
        while allocator.claim_low() is not None:
            pass

        result = mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        assert result is None

    def test_single_claim_writes_structure_and_provisional_introduced_by(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        linearization, commit_metadata, allocator = self._allocator_and_metadata(repo, real_db)

        claimed_hash = mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        assert claimed_hash == linearization[-1]  # newest commit (h2) claimed first

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        commit_ident = f":commit/{claimed_hash[:12]}"
        assert mcp_server._entity_introduced_by_query(real_db, fn_ident) == commit_ident
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True
        assert mcp_server._candidate_diff_read(real_db, claimed_hash, fn_ident) is not None

    def test_walking_backward_moves_introduced_by_to_the_oldest_commit(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        linearization, commit_metadata, allocator = self._allocator_and_metadata(repo, real_db)

        for _ in range(3):
            mcp_server._reverse_fill_claim_and_process(
                real_db, str(repo), linearization, commit_metadata, allocator,
            )

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        oldest_hash, mid_hash, newest_hash = linearization[0], linearization[1], linearization[2]
        assert mcp_server._entity_introduced_by_query(real_db, fn_ident) == f":commit/{oldest_hash[:12]}"
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True
        raw = mcp_server._db_execute(
            real_db, f"(query [:find (count ?c) :where [{fn_ident} :introduced-by ?c]])"
        )
        assert json.loads(raw)["results"] == [[1]]
        assert mcp_server._candidate_diff_read(real_db, oldest_hash, fn_ident) is not None

        # The converged :modified-in set must match what forward walk would
        # have produced: every later touch except the true introduction
        # commit itself (h0 gets none -- see the design spec's corrected
        # "Per-commit algorithm" section and the final whole-branch
        # review's Critical finding #1 that this test was added to catch).
        raw = mcp_server._db_execute(
            real_db, f"(query [:find ?c :where [{fn_ident} :modified-in ?c]])"
        )
        modified_in_commits = {row[0] for row in json.loads(raw)["results"]}
        assert modified_in_commits == {f":commit/{mid_hash[:12]}", f":commit/{newest_hash[:12]}"}

    def test_two_entities_in_one_commit_get_different_classifications(self, real_db, tmp_path):
        """A single claimed commit can simultaneously introduce a brand-new
        entity and add a real touch to an already-authoritative one --
        the two must not cross-contaminate each other's handling."""
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        linearization, commit_metadata, allocator = self._allocator_and_metadata(repo, real_db)

        extra_ident = mcp_server._code_ident("function", "auth.py", "extra")
        # Simulate Stream 1 having already confirmed extra() authoritatively,
        # before Stream 2 ever runs -- extra() first appears at h1, and this
        # commit (h2, claimed first) also touches it (its body is identical
        # at h1/h2, but it still shares the file with a genuinely new touch).
        mcp_server._transact(
            real_db, f"[[{extra_ident} :introduced-by :commit/preexisting]]", "2025-01-01T00:00:00Z",
        )

        claimed_hash = mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        commit_ident = f":commit/{claimed_hash[:12]}"

        more_ident = mcp_server._code_ident("function", "auth.py", "more")
        # more() is genuinely new at h2 -- first sighting, provisional.
        assert mcp_server._entity_introduced_by_query(real_db, more_ident) == commit_ident
        assert mcp_server._lineage_is_provisional(real_db, more_ident) is True

        # extra() stays authoritative and untouched on :introduced-by.
        assert mcp_server._entity_introduced_by_query(real_db, extra_ident) == ":commit/preexisting"
        assert mcp_server._lineage_is_provisional(real_db, extra_ident) is False

    def test_already_authoritative_entity_only_gets_modified_in(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        linearization, commit_metadata, allocator = self._allocator_and_metadata(repo, real_db)

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        # Simulate Stream 1 having already confirmed login() authoritatively
        # at the oldest commit, before Stream 2 ever runs.
        mcp_server._transact(
            real_db, f"[[{fn_ident} :introduced-by :commit/preexisting]]", "2025-01-01T00:00:00Z",
        )

        claimed_hash = mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        commit_ident = f":commit/{claimed_hash[:12]}"

        assert mcp_server._entity_introduced_by_query(real_db, fn_ident) == ":commit/preexisting"
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is False
        raw = mcp_server._db_execute(
            real_db, f"(query [:find ?c :where [{fn_ident} :modified-in ?c]])"
        )
        assert [commit_ident] in json.loads(raw)["results"]

    def test_frontier_high_interval_advances_by_one(self, real_db, tmp_path):
        import mcp_server
        import frontier_registry
        repo = self._repo_with_evolving_function(tmp_path)
        linearization, commit_metadata, allocator = self._allocator_and_metadata(repo, real_db)

        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )

        bounds = mcp_server._frontier_read_bounds(real_db, mcp_server._FRONTIER_HIGH_IDENT)
        assert bounds is not None
        _lo_hash, hi_hash = bounds
        assert hi_hash == linearization[-1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestReverseFillClaimAndProcess -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_reverse_fill_claim_and_process'`.

- [ ] **Step 3: Add `_reverse_fill_claim_and_process` to `mcp_server.py`**

Insert immediately after `_extract_commit` (before `_run_ingestion`):

```python
def _reverse_fill_claim_and_process(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    allocator: "frontier_registry.FrontierAllocator",
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> Optional[str]:
    """#222 phase 2b: claim exactly one position from the gap's high end
    (allocator.claim_high()) and process that one commit -- structural
    facts, :modified-in edges, provisional :introduced-by, and candidate-diff
    records for every entity the commit's "A"/"M" files touch. "D"/"R"
    files are skipped entirely (deletions and renames are out of scope for
    this sub-phase -- see the design spec).

    Reuses _extract_commit/_build_code_triples unchanged for entity
    discovery/parsing (direction-agnostic). BOTH :introduced-by and
    :modified-in are filtered out of _build_code_triples's own output and
    written by this function instead -- _build_code_triples's gate for
    both attributes is entity_valid_from membership, which for forward
    walk coincides with "is this the introduction commit" but for reverse
    walk does not: the first commit reverse walk sees an entity at is the
    newest touch (not the introduction), and the commit where reverse walk
    finally stops finding an even-earlier occurrence (the true
    introduction) is by then already "known" from later, already-visited
    commits. Reusing _build_code_triples's :modified-in emission verbatim
    would misclassify both ends. See the design spec's "Per-commit
    algorithm" section (revised after the final whole-branch review found
    this defect) for the full derivation.

    :introduced-by: a newly-discovered entity's guess is asserted via
    _entity_introduced_by_set_provisional and gets no :modified-in yet
    (mirrors forward walk never emitting :modified-in at an entity's own
    introduction commit). When an already-provisional entity is found at
    an even earlier commit, its guess moves to this commit (also getting
    no :modified-in yet) and the PREVIOUSLY-guessed commit -- now
    confirmed to be a genuine modification, not the introduction --
    retroactively receives a :modified-in fact using its OWN commit
    timestamp (looked up from commit_metadata), not this commit's.
    Already-authoritative entities are never touched, but a genuine later
    touch always gets :modified-in (unless #221's unchanged-body check
    says the body is provably unchanged this commit).

    Known, documented limitation: the retroactive :modified-in for a
    superseded commit does not re-check #221's unchanged-body narrowing
    against THAT commit's own diff (only this call's diff has that data) --
    it is asserted unconditionally. This can rarely over-assert an edge
    forward walk would have suppressed; it can never produce a missing or
    misattributed edge. See the design spec for why this is an accepted
    simplification for this sub-phase.

    Returns the claimed commit's hash, or None if the gap was already
    empty (allocator.claim_high() returned None) -- caller's signal to
    stop. Writes for one commit are followed by exactly one
    _db_checkpoint(db) call, after _frontier_persist_claim records the
    claim -- mirrors _run_ingestion's one-checkpoint-per-commit cadence
    (see the design spec's "Resume-safety / atomicity boundary" section).
    """
    pos = allocator.claim_high()
    if pos is None:
        return None

    commit_hash, commit_ts_iso, author, subject = commit_metadata[pos]
    commit_ident = f":commit/{commit_hash[:12]}"

    file_results, _gitlink_changes, _gitmodules_map, _renamed_pairs = _extract_commit(
        repo_path, commit_hash, ignore_patterns
    )

    all_triples: List[str] = [
        f"[{commit_ident} :entity-type :type/commit]",
        f'[{commit_ident} :ident "{commit_ident}"]',
        f'[{commit_ident} :description "{_edn_escape(subject[:120])}"]',
        f'[{commit_ident} :hash "{commit_hash}"]',
        f'[{commit_ident} :author "{_edn_escape(author)}"]',
        f'[{commit_ident} :subject "{_edn_escape(subject[:200])}"]',
        f'[{commit_ident} :date "{commit_ts_iso}"]',
    ]
    new_candidates: List[str] = []
    provisional_moves: List[Tuple[str, str]] = []  # (ident, superseded_commit_ident)
    already_authoritative_touched: List[str] = []
    body_hash_by_ident: Dict[str, str] = {}
    unchanged_by_ident: Dict[str, bool] = {}

    for status, file_path, extracted, precomputed, _old_path in file_results:
        if status not in ("A", "M"):
            continue  # "D"/"R" deferred -- see design spec scope

        candidate_idents = (
            [precomputed["module_ident"]]
            + [ident for ident, _name, _t in precomputed["function_entries"]]
            + [ident for ident, _name, _t in precomputed["class_entries"]]
            + [ident for ident, _name, _t in precomputed["global_entries"]]
            + [ident for ident, _name, _t in precomputed["field_entries"]]
        )
        known_before: Dict[str, str] = {
            ident: "known" for ident in candidate_idents
            if _entity_introduced_by_query(db, ident) is not None
        }
        known_before_snapshot = set(known_before.keys())

        triples = _build_code_triples(
            file_path, extracted, commit_ts_iso, known_before, {}, {}, commit_ident,
            precomputed, {}, {},
        )
        # This walk owns the write timing of BOTH attributes itself now --
        # filter both out of _build_code_triples's forward-biased gating.
        all_triples.extend(
            t for t in triples if ":introduced-by" not in t and ":modified-in" not in t
        )

        unchanged_idents = precomputed.get("unchanged_idents", set())
        for ident in candidate_idents:
            unchanged_by_ident[ident] = ident in unchanged_idents

        new_candidates.extend(set(known_before.keys()) - known_before_snapshot)
        for ident in set(candidate_idents) & known_before_snapshot:
            if _lineage_is_provisional(db, ident):
                superseded_ident = _entity_introduced_by_query(db, ident)
                provisional_moves.append((ident, superseded_ident))
            else:
                already_authoritative_touched.append(ident)

        body_hash_by_ident.update(precomputed.get("body_hashes", {}))

    _transact(db, "[" + " ".join(all_triples) + "]", commit_ts_iso, index_con=index_con)

    authoritative_modified_triples = [
        f"[{ident} :modified-in {commit_ident}]"
        for ident in already_authoritative_touched
        if not unchanged_by_ident.get(ident, False)
    ]
    if authoritative_modified_triples:
        _transact(
            db, "[" + " ".join(authoritative_modified_triples) + "]", commit_ts_iso, index_con=index_con,
        )

    for ident in new_candidates:
        _entity_introduced_by_set_provisional(db, ident, commit_ident, commit_ts_iso, index_con=index_con)
        if ident in body_hash_by_ident:
            _candidate_diff_persist(
                db, commit_hash, ident, body_hash_by_ident[ident], commit_ts_iso, index_con=index_con,
            )

    ts_by_commit_ident = {f":commit/{h[:12]}": ts for h, ts, _a, _s in commit_metadata}
    for ident, superseded_ident in provisional_moves:
        _entity_introduced_by_set_provisional(db, ident, commit_ident, commit_ts_iso, index_con=index_con)
        if ident in body_hash_by_ident:
            _candidate_diff_persist(
                db, commit_hash, ident, body_hash_by_ident[ident], commit_ts_iso, index_con=index_con,
            )
        if superseded_ident is not None and superseded_ident != commit_ident:
            superseded_ts = ts_by_commit_ident.get(superseded_ident, commit_ts_iso)
            _transact(
                db, f"[[{ident} :modified-in {superseded_ident}]]", superseded_ts, index_con=index_con,
            )

    _frontier_persist_claim(db, linearization, pos, from_low=False, commit_ts_iso=commit_ts_iso, index_con=index_con)
    _db_checkpoint(db)
    return commit_hash
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestReverseFillClaimAndProcess -v`
Expected: PASS (all 6 tests).

Also run the full existing suite:

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _reverse_fill_claim_and_process for #222 phase 2b

Stream 2's per-commit reverse-fill step: claims one position from the
frontier gap's high end and writes structure + :modified-in + provisional
:introduced-by, reusing _extract_commit/_build_code_triples unchanged.
_entity_introduced_by_set_provisional (Task 1) is the sole writer of
:introduced-by in this walk -- _build_code_triples's own candidate
triples for that attribute are filtered out before transacting. D/R
files and dependency edges are out of scope (see design spec). No caller
yet -- Task 4 adds the driving loop."
```

---

### Task 4: `_reverse_bulk_fill_walk`

**Files:**
- Modify: `mcp_server.py` — insert immediately after `_reverse_fill_claim_and_process`.
- Test: `tests/test_mcp_server.py` — new `TestReverseBulkFillWalk` class.

**Interfaces:**
- Consumes: `_reverse_fill_claim_and_process` (Task 3).
- Produces: `_reverse_bulk_fill_walk(db, repo_path: str, linearization: List[str], commit_metadata: List[Tuple[str, str, str, str]], allocator: "frontier_registry.FrontierAllocator", ignore_patterns: Sequence[str] = (), index_con=None) -> int`

- [ ] **Step 1: Write the failing tests**

Add this test class to `tests/test_mcp_server.py`:

```python
class TestReverseBulkFillWalk:
    def test_walks_until_gap_closes_and_returns_count(self, real_db, tmp_path):
        import mcp_server
        import frontier_registry
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "a.py").write_text("def a(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h0"], cwd=repo, check=True, capture_output=True)
        (repo / "b.py").write_text("def b(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h1"], cwd=repo, check=True, capture_output=True)
        (repo / "c.py").write_text("def c(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h2"], cwd=repo, check=True, capture_output=True)

        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")

        count = mcp_server._reverse_bulk_fill_walk(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )

        assert count == 3
        assert allocator.is_gap_empty() is True
        fn_ident_a = mcp_server._code_ident("function", "a.py", "a")
        assert mcp_server._entity_introduced_by_query(real_db, fn_ident_a) == f":commit/{linearization[0][:12]}"

    def test_gap_already_empty_returns_zero(self, real_db, tmp_path):
        import mcp_server
        import frontier_registry
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "a.py").write_text("def a(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h0"], cwd=repo, check=True, capture_output=True)

        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-02T00:00:00Z")
        while allocator.claim_low() is not None:
            pass  # drain the gap forward first

        count = mcp_server._reverse_bulk_fill_walk(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        assert count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestReverseBulkFillWalk -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_reverse_bulk_fill_walk'`.

- [ ] **Step 3: Add `_reverse_bulk_fill_walk` to `mcp_server.py`**

Insert immediately after `_reverse_fill_claim_and_process`:

```python
def _reverse_bulk_fill_walk(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    allocator: "frontier_registry.FrontierAllocator",
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> int:
    """#222 phase 2b: repeatedly call _reverse_fill_claim_and_process until
    the gap closes. Returns the count of commits processed. No caller in
    this sub-phase -- 2d wires this into the real concurrent ingestion
    loop alongside the forward stream.
    """
    count = 0
    while True:
        result = _reverse_fill_claim_and_process(
            db, repo_path, linearization, commit_metadata, allocator,
            ignore_patterns=ignore_patterns, index_con=index_con,
        )
        if result is None:
            break
        count += 1
    return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestReverseBulkFillWalk -v`
Expected: PASS (both tests).

Also run the full existing suite one final time:

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _reverse_bulk_fill_walk driving loop for #222 phase 2b

Thin loop over _reverse_fill_claim_and_process until the frontier gap
closes. Completes phase 2b: Stream 2's reverse-bulk-fill walk exists as
a standalone, independently-testable unit. No caller wired into
_run_ingestion -- 2d wires the real concurrency."
```

## Self-Review Notes

- **Spec coverage:** Schema/audit safety (no new attributes, matched against `MINIGRAF_SCHEMA`) — not a task, since it required no code change, just verification (done during design). Resume-safety/atomicity boundary — covered by Task 3's single `_db_checkpoint` call placed after all writes including the frontier claim. The `_entity_introduced_by_set_provisional` sole-writer requirement — covered by Task 3's explicit `:introduced-by` filter on `_build_code_triples`'s output. The core algorithm (structural gate, first-sighting vs. move-earlier vs. authoritative-untouched) — covered by Task 3 and directly tested by `TestReverseFillClaimAndProcess`'s four scenario tests. The driving loop — Task 4. Explicitly deferred scope (deps/renames/deletions) — enforced by Task 3's `if status not in ("A", "M"): continue` guard.
- **Type consistency:** `entity_ident`/`commit_ident`/`commit_ts_iso`/`index_con` naming matches phase 1/2a's existing convention throughout. `commit_metadata`'s shape (`List[Tuple[str, str, str, str]]`, position-indexed to `linearization`) is used identically in Tasks 3 and 4 and in their tests, built via `_git_commits(repo_path, watermark_hash=None)` in every test — confirmed both `build_linearization` and `_git_commits` use `git log --topo-order --reverse`, so their orderings/indices align.
- **No placeholders:** every step has complete, runnable code — no TBD/TODO markers, no "similar to Task N" shorthand. Test repo fixtures are written out in full in each task that needs one (not shared via a new pytest fixture, since each task's repo shape differs slightly and the design favors explicit, readable test setup over cross-task fixture coupling).
