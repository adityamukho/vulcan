# Reverse-Walk Write Amplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the reverse-bulk-fill walk (#222 Stage A) from ~6,970 transactions per commit down to the forward walk's ~12, by making its per-commit write count O(1) in the number of entities touched.

**Architecture:** Three independent levers, measured by a per-call-site profile (see the spec). (1) Structural re-dating moves out of `_reverse_apply`'s provisional-move loop and into `_correction_sweep_apply`'s confirm branch, where it runs once per entity for the whole region instead of once per entity per commit. (2) The `:type/candidate-diff` record path is deleted — it has no production reader. (3) The surviving per-entity writes (the provisional guess move, the lineage markers, the retroactive `:modified-in`, the sweep's confirm) are batched into one call per commit, which is safe because facts differing in entity do not collide in minigraf's EAVT pending index.

**Tech Stack:** Python 3, `minigraf` (bi-temporal Datalog graph), `pytest`, tree-sitter. Single file under change: `mcp_server.py`, plus `tests/test_mcp_server.py`.

## Global Constraints

- **Design spec:** `docs/superpowers/specs/2026-07-31-reverse-walk-write-amplification-design.md`. Read it before Task 1. Every task's requirements implicitly include it.
- **Real-backend-only tests.** See `docs/testing-conventions.md`. Use the `real_db` fixture. No mocking of `minigraf`, `_transact`, `_retract`, or `_db_execute` for the purpose of *avoiding* the backend — wrapping them to *count* real calls (as `execute_spy` at `tests/test_mcp_server.py:64` already does) is fine and is used below.
- **`:contains` facts are transacted and retracted ONE TRIPLE PER CALL, on both sides.** Minigraf's EAVT pending index omits value bytes, so facts sharing `(entity, attribute, valid_from)` in one call collapse to the last. `[module :contains fn]` has the *module* as its entity, so a module's children all collide with each other. This is minigraf#287 / #222 phase 2b1, and it cost five of six containment edges when it was got wrong. **Never batch `:contains`.**
- **Facts differing in *entity* do not collide.** Every batch this plan introduces is over distinct entities. `_reverse_apply` already relies on this for `authoritative_modified_triples`.
- **`:parent` edges are also one transact per parent** — a merge commit has two and they share `(entity, attribute, valid_from)`. Do not batch them either.
- **Do not push.** Commit locally only. No `git push`, no `gh pr create`.
- **Run the full suite** (`.venv/bin/python -m pytest tests/test_mcp_server.py -q`) before each commit. The suite must be green at every task boundary.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `mcp_server.py:5259-5331` | `_candidate_diff_ident/_persist/_read/_clear` | **Deleted** (Task 1) |
| `mcp_server.py:5012-5067` | `_frontier_load` — one-time migration site | Gains `_candidate_diff_purge_legacy` call (Task 1) |
| `mcp_server.py:6689-6702` | `_precompute_file_triples`'s `body_hashes` | **Deleted** — existed only for candidate-diff (Task 1) |
| `mcp_server.py:5341-5398` | `_entity_introduced_by_set_provisional` | Becomes a wrapper over a new batch function (Task 3) |
| `mcp_server.py:5401-5434` | `_re_date_structural_facts` | Unchanged body; docstring updated (Task 2) |
| `mcp_server.py:5158-5171` | `_lineage_confirm` | Becomes a wrapper over a new batch function (Task 4) |
| `mcp_server.py:~7700-7995` | `_reverse_apply` | Loses re-dating and candidate-diff; gains batching (Tasks 1–4) |
| `mcp_server.py:8820-8941` | `_correction_sweep_apply` | Gains re-dating in case 1; loses candidate-diff; batches confirm (Tasks 1, 2, 4) |
| `tests/test_mcp_server.py` | All tests | Classes deleted, moved, and added (all tasks) |

---

### Task 1: Delete the candidate-diff path

`_candidate_diff_read`'s only callers are `_candidate_diff_persist` and `_candidate_diff_clear` themselves — no production code consults the persisted body hash. 2a specced these so 2c could confirm a candidate without re-parsing; 2c as built re-parses and reads `precomputed["unchanged_idents"]`. The profile attributes 34% of Stage A's wall time (172.7 s of 502 s, almost all in `_candidate_diff_clear`'s retracts) to maintaining them.

**Files:**
- Modify: `mcp_server.py` — delete `_candidate_diff_ident/_persist/_read/_clear` (5259–5331); add `_candidate_diff_purge_legacy`; call it from `_frontier_load` (5012–5067); remove six call sites; delete `body_hashes` from `_precompute_file_triples` (6689–6702) and `body_hash_by_ident` from `_reverse_apply`
- Test: `tests/test_mcp_server.py` — delete `TestCandidateDiff` (6306), delete `TestReverseFillCandidateDiffLifecycle` (14526), add `TestCandidateDiffLegacyPurge`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `_candidate_diff_purge_legacy(db: Any, index_con: Optional[Any] = None) -> int` — returns the number of records purged. Nothing else in this plan calls it; it exists for its test and for `_frontier_load`.

- [ ] **Step 1: Write the failing test for the legacy purge**

Add to `tests/test_mcp_server.py`, replacing the deleted `TestCandidateDiff` class:

```python
class TestCandidateDiffLegacyPurge:
    """#233: the :type/candidate-diff record path is deleted, so nothing
    clears the records a phase-2d graph already holds -- and they ARE
    indexed (fact_index filters nothing by prefix; _MEMORY_PREFIXES affects
    scoring only), so they would stay retrievable scratch noise forever.
    _frontier_load is already the one-time-migration site."""

    def _seed_legacy_record(self, db, commit_hash, entity_ident, body_hash):
        """Write a candidate-diff record the way the deleted
        _candidate_diff_persist used to, so the purge has something real to
        find on a graph written by phase 2d."""
        import mcp_server
        ident = f":candidate/{commit_hash[:12]}-{entity_ident.lstrip(':').replace('/', '-')}"
        commit_ident = f":commit/{commit_hash[:12]}"
        mcp_server._transact(
            db,
            "[" + " ".join([
                f"[{ident} :entity-type :type/candidate-diff]",
                f"[{ident} :commit {commit_ident}]",
                f"[{ident} :entity {entity_ident}]",
                f'[{ident} :body-hash "{body_hash}"]',
            ]) + "]",
            "2026-01-01T00:00:00Z",
        )
        return ident

    def _live_record_count(self, db):
        import mcp_server
        raw = mcp_server._db_execute(
            db, "(query [:find (count ?e) :where [?e :entity-type :type/candidate-diff]])"
        )
        results = json.loads(raw)["results"]
        return results[0][0] if results else 0

    def test_purges_pre_existing_records(self, real_db):
        import mcp_server
        self._seed_legacy_record(real_db, "a" * 40, ":function/src-auth-py-login", "h1")
        self._seed_legacy_record(real_db, "b" * 40, ":function/src-auth-py-login", "h2")
        assert self._live_record_count(real_db) == 2

        purged = mcp_server._candidate_diff_purge_legacy(real_db)

        assert purged == 2
        assert self._live_record_count(real_db) == 0

    def test_noop_on_a_graph_with_no_records(self, real_db):
        import mcp_server
        assert mcp_server._candidate_diff_purge_legacy(real_db) == 0

    def test_frontier_load_purges_on_an_existing_graph(self, real_db, tmp_path):
        """The purge has to run from _frontier_load, not just exist -- an
        already-migrated graph is loaded by every subsequent run and is
        exactly where these orphans live."""
        import mcp_server
        import frontier_registry
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def login(): pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "c0"], cwd=repo, check=True, capture_output=True)
        linearization = frontier_registry.build_linearization(str(repo))
        self._seed_legacy_record(real_db, "a" * 40, ":function/src-auth-py-login", "h1")

        mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")

        assert self._live_record_count(real_db) == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestCandidateDiffLegacyPurge -q`
Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_candidate_diff_purge_legacy'`

- [ ] **Step 3: Delete the four candidate-diff helpers**

In `mcp_server.py`, delete the whole block from `def _candidate_diff_ident(` through the end of `_candidate_diff_clear` (currently lines 5259–5331, ending just before `def _entity_introduced_by_query`). Replace it with the purge function:

```python
def _candidate_diff_purge_legacy(db: Any, index_con: Optional[Any] = None) -> int:
    """One-time cleanup for graphs written by #222 phase 2d, which persisted
    a :type/candidate-diff record per (claimed commit, candidate entity)
    pair. #233 deleted that path outright -- 2a specced the records so 2c
    could confirm a candidate by hash comparison instead of re-parsing, but
    2c as built re-parses on the process pool and reads
    precomputed["unchanged_idents"], so nothing ever read them.

    Without this, the records a 2d graph already holds are orphaned: the
    writer AND the clearer are both gone. They are not inert -- fact_index
    filters nothing by prefix (_MEMORY_PREFIXES affects scoring, not
    inclusion), so they stay retrievable scratch noise indefinitely.

    Called unconditionally from _frontier_load rather than gated on a
    watermark: it is one query per load, cheap when there is nothing to
    purge, and gating it would need a new watermark whose only job is to
    record that a one-time deletion happened.

    Returns the number of records purged. Records for distinct :candidate/
    entities do not share (entity, attribute, valid_from), so the whole
    purge is one collision-free _retract.
    """
    raw = _db_execute(
        db, "(query [:find ?e ?c ?ent ?h :where "
            "[?e :entity-type :type/candidate-diff] [?e :commit ?c] "
            "[?e :entity ?ent] [?e :body-hash ?h]])"
    )
    rows = json.loads(raw).get("results", [])
    if not rows:
        return 0
    facts = []
    for ident, commit_ident, entity_ident, body_hash in rows:
        facts.extend([
            f"[{ident} :entity-type :type/candidate-diff]",
            f"[{ident} :commit {commit_ident}]",
            f"[{ident} :entity {entity_ident}]",
            f'[{ident} :body-hash "{_edn_escape(body_hash)}"]',
        ])
    _retract(db, "[" + " ".join(facts) + "]", index_con=index_con)
    return len(rows)
```

- [ ] **Step 4: Call the purge from `_frontier_load`**

In `_frontier_load`, immediately after the existing `_lineage_confirmed_through_migrate` call (currently line 5027):

```python
    _lineage_confirmed_through_migrate(db, run_ts_iso, index_con=index_con)
    _candidate_diff_purge_legacy(db, index_con=index_con)
```

- [ ] **Step 5: Run the purge tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestCandidateDiffLegacyPurge -q`
Expected: PASS (3 tests)

- [ ] **Step 6: Remove the six call sites**

The suite is currently red — the helpers are gone but callers remain. Remove each:

**a. `_forward_reconcile_provisional`** — delete the `_candidate_diff_clear` call and its comment block (currently step 4, around line 5485–5490), and renumber the following comment from `# 5.` to `# 4.`.

**b. `_reverse_apply`, `new_candidates` loop** — becomes:

```python
    for ident in new_candidates:
        _entity_introduced_by_set_provisional(
            db, ident, commit_ident, commit_ts_iso, index_con=index_con,
            pos=pos, pos_by_commit_ident=pos_by_commit_ident,
        )
```

**c. `_reverse_apply`, `provisional_moves` loop** — delete the `if ident in body_hash_by_ident:` / `_candidate_diff_persist(...)` block, and delete the `_candidate_diff_clear` call together with its "The superseded candidate-diff record is stale..." comment block (currently around lines 7965–7972). The `if superseded_pos is None or superseded_pos > pos:` branch keeps only the `_re_date_structural_facts` call — Task 2 removes that too.

**d. `_correction_sweep_apply`** — delete both `_candidate_diff_clear` lines (currently 8896 and 8924). In case 1 the body becomes just `_lineage_confirm(db, ident, index_con=index_con)`; in case 3's single-value branch, drop the trailing clear.

**e. `_reverse_apply`'s `body_hash_by_ident`** — now unused. Delete its declaration (`body_hash_by_ident: Dict[str, str] = {}`) and the `body_hash_by_ident.update(precomputed.get("body_hashes", {}))` line in the file loop.

**f. `_precompute_file_triples`** — delete the `body_hashes` computation block (the comment starting `# #222 phase 2b: per-entity body hash...` through `body_hashes = {}` in the `except`) and the `"body_hashes": body_hashes,` entry from the returned dict. `unchanged_idents` calls `_normalized_body_hash` independently and is unaffected — leave it alone.

- [ ] **Step 7: Delete the two obsolete test classes**

Delete `TestReverseFillCandidateDiffLifecycle` (currently `tests/test_mcp_server.py:14526`) in full — it asserts one live candidate-diff record per guess, a property of a path that no longer exists. `TestCandidateDiff` was already replaced in Step 1.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: PASS. If anything else references `_candidate_diff_*` or `body_hashes`, grep and remove it:
`grep -rn "_candidate_diff\|body_hashes" mcp_server.py tests/ evals/`

- [ ] **Step 9: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Delete the candidate-diff record path (#233)

_candidate_diff_read's only callers were _candidate_diff_persist and
_candidate_diff_clear themselves. 2a specced these records so 2c could
confirm a candidate by hash comparison instead of re-parsing, but 2c as
built re-parses on the process pool and reads
precomputed[\"unchanged_idents\"] -- the persisted body hash was never
consulted by any production path.

Per-call-site profiling of 12 commits of the reverse walk attributes 34%
of its wall time (172.7s of 502s, almost all in _candidate_diff_clear's
retracts) to maintaining them.

_precompute_file_triples's body_hashes existed only to feed this path and
goes with it; unchanged_idents computes its own hashes and is untouched.

Adds _candidate_diff_purge_legacy, called from _frontier_load alongside
the existing one-time migrations: a 2d graph's records are orphaned once
both the writer and the clearer are gone, and they are indexed, so
without this they stay retrievable scratch noise indefinitely.

Refs #233"
```

---

### Task 2: Defer structural re-dating to the correction sweep

`_re_date_structural_facts` fires on every provisional move, so a long-lived entity is fully re-dated once per touch. It is 48% of Stage A's wall time (241.3 s of 502 s; 17,250 retracts at ~13 ms each). The correction sweep already visits every commit in the reverse region exactly once, ascending, and — by its gap-closed precondition — with Stream 2's guess final, so the commit at which an entity reaches case 1 *is* its introduction.

**Files:**
- Modify: `mcp_server.py` — remove the `_re_date_structural_facts` call from `_reverse_apply`; add it to `_correction_sweep_apply` case 1; update three docstrings
- Test: `tests/test_mcp_server.py` — extend `TestReverseFillValidTimeParity` (14566), add its negative counterpart, add a sweep re-date test

**Interfaces:**
- Consumes: Task 1's cleaned-up `_correction_sweep_apply` case 1 (`_lineage_confirm(db, ident, index_con=index_con)` alone)
- Produces: `_correction_sweep_apply` now needs each candidate's structural triples, built per file into a local `candidate_triples_by_ident: Dict[str, List[str]]` exactly as `_reverse_apply` does. Task 4 batches the `_lineage_confirm` in this same branch.

- [ ] **Step 1: Write the failing tests**

Replace the existing `TestReverseFillValidTimeParity` class body with the following (keep the class name — its `test_structural_facts_are_re_dated_when_the_guess_moves_earlier` method is being moved to a later point in the pipeline, not deleted):

```python
class TestReverseFillValidTimeParity:
    def _repo_with_three_commits(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for i in range(3):
            (repo / "auth.py").write_text(f"def login():\n    return {i}\n")
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"c{i}"], cwd=repo, check=True, capture_output=True)
        return repo

    def _attrs_at(self, real_db, entity_ident, ts_iso):
        import mcp_server
        raw = mcp_server._db_execute(
            real_db, f'(query [:find ?a :valid-at "{ts_iso}" :where [{entity_ident} ?a ?v]])'
        )
        return {row[0] for row in json.loads(raw)["results"]}

    def test_structural_facts_are_re_dated_by_the_correction_sweep(self, real_db, tmp_path):
        """2b wrote an entity's structural facts once, at the timestamp of
        the commit where the walk first SIGHTED it. That leaves a valid-time
        window where :introduced-by is live for an entity with no type, name
        or file -- so ":as-of the introduction" answers "this entity did not
        exist", while ":when was it introduced" answers with that very
        commit.

        #233 moved the repair from _reverse_apply (which did it on every
        provisional move: 48% of Stage A's wall time) to the correction
        sweep's confirm branch, which visits each entity's introduction
        exactly once. So the invariant now holds after Stage B, not after
        Stage A -- this test asserts the end state, and its negative
        counterpart below pins the Stage A window deliberately."""
        import mcp_server
        import frontier_registry
        repo = self._repo_with_three_commits(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_bulk_fill_walk(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )

        mcp_server._correction_sweep_walk(
            real_db, str(repo), linearization, commit_metadata,
        )

        login = mcp_server._code_ident("function", "auth.py", "login")
        attrs = self._attrs_at(real_db, login, commit_metadata[0][1])
        assert ":introduced-by" in attrs
        assert ":entity-type" in attrs, (
            "an entity live at its own introduction must carry its structure there too; "
            f"got only {sorted(attrs)}"
        )
        assert ":file" in attrs

    def test_stage_a_alone_leaves_the_window_open(self, real_db, tmp_path):
        """The other half of the moved invariant. Deferring the re-date is a
        real behaviour change and the window it opens is deliberate, so pin
        it in both directions -- otherwise a future change could re-introduce
        eager re-dating (and its 48% cost) with the suite still green."""
        import mcp_server
        import frontier_registry
        repo = self._repo_with_three_commits(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")

        mcp_server._reverse_bulk_fill_walk(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )

        login = mcp_server._code_ident("function", "auth.py", "login")
        assert mcp_server._entity_introduced_by_query(real_db, login) == \
            f":commit/{linearization[0][:12]}"
        assert mcp_server._lineage_is_provisional(real_db, login) is True
        attrs = self._attrs_at(real_db, login, commit_metadata[0][1])
        assert ":entity-type" not in attrs, (
            "Stage A must NOT re-date -- that is the amplification #233 removed"
        )

    def test_reverse_walk_issues_no_re_dating_writes(self, real_db, tmp_path):
        """The cost assertion behind the behaviour assertions above:
        _re_date_structural_facts must not be reached at all during Stage A.
        At 17,250 retracts over 12 real commits (~13ms each) this was the
        single largest line in the profile."""
        import mcp_server
        import frontier_registry
        repo = self._repo_with_three_commits(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")

        calls = []
        real_re_date = mcp_server._re_date_structural_facts
        mcp_server._re_date_structural_facts = lambda *a, **k: (
            calls.append(a) or real_re_date(*a, **k)
        )
        try:
            mcp_server._reverse_bulk_fill_walk(
                real_db, str(repo), linearization, commit_metadata, allocator,
            )
        finally:
            mcp_server._re_date_structural_facts = real_re_date

        assert calls == []
```

Add a sweep-side test to `TestCorrectionSweepApply`:

```python
    def test_case_one_re_dates_structural_facts(self, real_db, tmp_path):
        """#233: the sweep's confirm branch is where re-dating now happens.
        It already has the triples -- precomputed carries
        module_candidate_triples and (ident, name, triples) per entries key
        -- and previously read only the idents out of it."""
        import mcp_server
        import frontier_registry
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for i in range(3):
            (repo / "auth.py").write_text(f"def login():\n    return {i}\n")
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"c{i}"], cwd=repo, check=True, capture_output=True)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_bulk_fill_walk(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        login = mcp_server._code_ident("function", "auth.py", "login")

        file_results, _g, _m, _r = mcp_server._extract_commit(str(repo), linearization[0], ())
        mcp_server._correction_sweep_apply(
            real_db, linearization[0], commit_metadata[0][1], file_results,
        )

        assert mcp_server._lineage_is_provisional(real_db, login) is False
        raw = mcp_server._db_execute(
            real_db,
            f'(query [:find ?a :valid-at "{commit_metadata[0][1]}" :where [{login} ?a ?v]])',
        )
        attrs = {row[0] for row in json.loads(raw)["results"]}
        assert ":entity-type" in attrs
        assert ":file" in attrs

    def test_case_one_re_dates_the_contains_edge(self, real_db, tmp_path):
        """A child's own candidate triples carry its [parent :contains child]
        edge, so re-dating a child must re-date its containment edge with it.
        This is the edge minigraf#287 destroys if the re-date ever batches
        multiple children of one module into a single call."""
        import mcp_server
        import frontier_registry
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for i in range(3):
            (repo / "auth.py").write_text(
                f"def a():\n    return {i}\n\ndef b():\n    return {i}\n\ndef c():\n    return {i}\n"
            )
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"c{i}"], cwd=repo, check=True, capture_output=True)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_bulk_fill_walk(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )

        mcp_server._correction_sweep_walk(
            real_db, str(repo), linearization, commit_metadata,
        )

        module = mcp_server._code_ident("module", "auth.py")
        raw = mcp_server._db_execute(
            real_db,
            f'(query [:find ?child :valid-at "{commit_metadata[0][1]}" '
            f'  :where [{module} :contains ?child]])',
        )
        children = {row[0] for row in json.loads(raw)["results"]}
        assert len(children) == 3, f"all three containment edges must survive; got {sorted(children)}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestReverseFillValidTimeParity tests/test_mcp_server.py::TestCorrectionSweepApply -q`
Expected: `test_stage_a_alone_leaves_the_window_open`, `test_reverse_walk_issues_no_re_dating_writes`, `test_case_one_re_dates_structural_facts`, and `test_case_one_re_dates_the_contains_edge` FAIL. `test_structural_facts_are_re_dated_by_the_correction_sweep` may already pass (Stage A still re-dates eagerly) — that is expected and it must still pass at the end.

- [ ] **Step 3: Remove the re-date from `_reverse_apply`**

In the `provisional_moves` loop, the `if superseded_pos is None or superseded_pos > pos:` branch loses its entire body — the `_re_date_structural_facts` call and its comment block. The branch's only remaining purpose was that call, so delete the branch too. The loop's tail becomes:

```python
    for ident, superseded_ident in provisional_moves:
        _entity_introduced_by_set_provisional(
            db, ident, commit_ident, commit_ts_iso, index_con=index_con,
            pos=pos, pos_by_commit_ident=pos_by_commit_ident,
        )
        if superseded_ident is not None and superseded_ident != commit_ident:
            superseded_pos = pos_by_commit_ident.get(superseded_ident)
            if superseded_pos is not None and superseded_pos <= pos:
                # The move was refused (or would be): the "superseded" guess
                # is not actually later than this commit, so asserting it as
                # a modification would claim the entity changed at or before
                # its own introduction.
                continue
            superseded_ts = ts_by_commit_ident.get(superseded_ident)
            if superseded_ts is None:
                # Falling back to THIS commit's timestamp back-dates the edge
                # to before the modification it describes -- a fact asserted
                # valid before it was true. Skip and say so instead.
                print(
                    f"[_reverse_apply] skipping retroactive :modified-in "
                    f"for {ident} at {superseded_ident}: no timestamp in commit_metadata",
                    file=sys.stderr,
                )
                continue
            _transact(
                db, f"[[{ident} :modified-in {superseded_ident}]]", superseded_ts, index_con=index_con,
            )
```

`candidate_triples_by_ident` in `_reverse_apply` is now unused — delete its declaration and both population sites (the `module_ident` assignment and the `entries_key` loop) in the file loop.

- [ ] **Step 4: Add the re-date to `_correction_sweep_apply`**

Inside the per-file loop, alongside the existing `candidate_idents` and `unchanged_idents` construction, build the triple map:

```python
        # #233: the sweep re-dates structural facts, which _reverse_apply
        # used to do eagerly on every provisional move (48% of Stage A's
        # wall time -- 17,250 retracts over 12 real commits). This pass
        # already visits every commit in the reverse region exactly once,
        # ascending, and its gap-closed precondition means Stream 2's guess
        # is final -- so the commit at which an entity reaches case 1 IS its
        # introduction, and re-dating there is once per entity for the whole
        # region. precomputed already carried these triples; only the idents
        # were being read out of it.
        candidate_triples_by_ident: Dict[str, List[str]] = {
            precomputed["module_ident"]: list(precomputed["module_candidate_triples"])
        }
        for entries_key in ("function_entries", "class_entries", "global_entries", "field_entries"):
            for entry_ident, _entry_name, entry_triples in precomputed[entries_key]:
                candidate_triples_by_ident[entry_ident] = list(entry_triples)
```

Then in case 1, before the confirm:

```python
                if introduced_by_values == {commit_ident}:
                    # Case 1: the provisional guess matches this commit --
                    # confirm. The :introduced-by fact itself is untouched,
                    # since its value was already correct.
                    _re_date_structural_facts(
                        db,
                        [t for t in candidate_triples_by_ident.get(ident, [])
                         if ":introduced-by" not in t],
                        commit_ts_iso,
                        index_con=index_con,
                    )
                    _lineage_confirm(db, ident, index_con=index_con)
```

- [ ] **Step 5: Update the three docstrings**

**a. `_re_date_structural_facts`** — its docstring currently names `_reverse_apply` as a caller. Replace that sentence:

```
    Used whenever a provisional :introduced-by guess is resolved to an
    earlier commit -- by _correction_sweep_apply when the sweep confirms an
    entity at its introduction, and by _forward_reconcile_provisional when
    the forward walk reaches the true introduction. In both cases the facts
    were written at the timestamp of the commit where the entity was first
    SIGHTED, which is later than the introduction now is, leaving a valid
    time window where :introduced-by is live for an entity with no type,
    name or file -- so ":as-of the introduction" reports it as nonexistent.

    _reverse_apply deliberately does NOT call this (#233): it used to, on
    every provisional move, which re-dated a long-lived entity once per
    touch instead of once. Both callers above fire once per entity.
```

**b. `_reverse_apply`** — add to its docstring, after the existing paragraph about provisional guesses:

```
    Does NOT re-date structural facts as a guess moves earlier (#233). They
    stay at the timestamp of the commit where this walk first SIGHTED the
    entity, which is later than the provisional :introduced-by, so ":as-of
    the provisional introduction" reports the entity as nonexistent for as
    long as it stays provisional. The correction sweep closes that window
    when it confirms. This is the same "temporarily dangling, and
    convergent" shape as the :parent edges below, and the region is already
    excluded from 2a's trust predicate -- but note that an INTERRUPTED run
    now leaves a wider inconsistent window than it did before #233, closed
    by the next run's sweep.
```

**c. `_correction_sweep_apply`** — add after the "Never calls `_extract_commit` itself" paragraph:

```
    Re-dates each confirmed entity's structural facts to the introduction
    commit (#233), which _reverse_apply used to do eagerly on every
    provisional move. Sound here and only here: the gap-closed precondition
    means Stream 2's guess is final, so an entity reaching case 1 is at its
    introduction, and this pass visits each commit exactly once ascending.
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestReverseFillValidTimeParity tests/test_mcp_server.py::TestCorrectionSweepApply -q`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: PASS. `TestStageBCorrectionSweep` is the most likely place for a real regression — it drives Stage A and Stage B together and is the integration check that the invariant genuinely still holds end to end.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Re-date structural facts in the sweep, not on every move (#233)

_re_date_structural_facts fired from _reverse_apply on every provisional
move, so a long-lived entity was fully re-dated once per touch: 17,250
retracts over 12 commits of this repo, 225.9s of the 502s spent inside
write calls, 48% of Stage A's wall time.

The correction sweep already visits every commit in the reverse region
exactly once, ascending, and its gap-closed precondition means Stream 2's
guess is final -- so the commit at which an entity reaches case 1 IS its
introduction. Re-dating there is once per entity for the whole region,
and rides on the induction the 2c spec already makes to justify
confirming at all. The sweep also already carried the triples in
precomputed and was reading only the idents out of them.

_forward_reconcile_provisional keeps its own call: that path already
fires once per entity.

Behaviour change, pinned in both directions: \"an entity live at its own
introduction carries its structure there too\" now holds after Stage B
rather than after Stage A. During Stage A the window is deliberately
open, which is what provisional means -- the region is excluded from 2a's
trust predicate either way -- but an interrupted run now leaves a wider
inconsistent window, closed by the next run's sweep.

Refs #233"
```

---

### Task 3: Batch the provisional guess move and its lineage markers

`_entity_introduced_by_set_provisional` issues one retract plus one transact per moved entity (8,618 retracts / 55.1 s, 10,122 transacts / 8.3 s over 12 commits), and `_lineage_mark_provisional` one transact per newly-marked entity. All are over distinct entities, so all are batchable.

**Files:**
- Modify: `mcp_server.py` — add `_entity_introduced_by_set_provisional_batch`; reduce `_entity_introduced_by_set_provisional` to a delegating wrapper; add `_lineage_mark_provisional_batch`; call the batch from `_reverse_apply`'s two loops
- Test: `tests/test_mcp_server.py` — add `TestEntityIntroducedBySetProvisionalBatch`

**Interfaces:**
- Consumes: Task 1's `_reverse_apply` loops (no candidate-diff calls), Task 2's `provisional_moves` loop shape
- Produces:
  - `_lineage_mark_provisional_batch(db, entity_idents: Sequence[str], commit_ts_iso: str, index_con=None) -> None`
  - `_entity_introduced_by_set_provisional_batch(db, entity_idents: Sequence[str], commit_ident: str, commit_ts_iso: str, index_con=None, pos: Optional[int] = None, pos_by_commit_ident: Optional[Dict[str, int]] = None) -> Set[str]` — returns the idents whose guess actually moved (or was first asserted)
  - `_entity_introduced_by_set_provisional(...)` keeps its exact current signature and becomes `_entity_introduced_by_set_provisional_batch(db, [entity_ident], ...)`

- [ ] **Step 1: Write the failing tests**

Add after the existing `TestEntityIntroducedBySetProvisional` class:

```python
class TestEntityIntroducedBySetProvisionalBatch:
    """#233: _reverse_apply's two loops were the only production callers of
    the per-ident function, at one retract + one transact each -- 8,618
    retracts over 12 commits of this repo, and retracts cost ~13ms against
    a transact's ~1ms. The batch is the implementation now; the per-ident
    function delegates to it so the gates cannot drift apart."""

    def test_first_assert_batches_distinct_entities(self, real_db):
        import mcp_server
        idents = [f":function/src-auth-py-f{i}" for i in range(5)]

        moved = mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, idents, ":commit/h2", "2026-01-03T00:00:00Z",
        )

        assert moved == set(idents)
        for ident in idents:
            assert mcp_server._entity_introduced_by_query(real_db, ident) == ":commit/h2"
            assert mcp_server._lineage_is_provisional(real_db, ident) is True

    def test_batch_issues_a_constant_number_of_write_calls(self, real_db):
        """The whole point. Five entities must not cost five retracts."""
        import mcp_server
        idents = [f":function/src-auth-py-f{i}" for i in range(5)]
        mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, idents, ":commit/h2", "2026-01-03T00:00:00Z",
        )

        calls = []
        real_transact, real_retract = mcp_server._transact, mcp_server._retract
        mcp_server._transact = lambda *a, **k: (calls.append("t") or real_transact(*a, **k))
        mcp_server._retract = lambda *a, **k: (calls.append("r") or real_retract(*a, **k))
        try:
            mcp_server._entity_introduced_by_set_provisional_batch(
                real_db, idents, ":commit/h0", "2026-01-01T00:00:00Z",
            )
        finally:
            mcp_server._transact, mcp_server._retract = real_transact, real_retract

        assert calls.count("r") == 1, f"one retract for the whole batch; got {calls}"
        assert calls.count("t") <= 2, f"at most guess + markers; got {calls}"

    def test_moving_the_batch_earlier_leaves_one_value_each(self, real_db):
        import mcp_server
        idents = [f":function/src-auth-py-f{i}" for i in range(3)]
        mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, idents, ":commit/h2", "2026-01-03T00:00:00Z",
        )

        mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, idents, ":commit/h0", "2026-01-01T00:00:00Z",
        )

        for ident in idents:
            assert mcp_server._entity_introduced_by_query(real_db, ident) == ":commit/h0"
            raw = mcp_server._db_execute(
                real_db, f"(query [:find (count ?c) :where [{ident} :introduced-by ?c]])"
            )
            assert json.loads(raw)["results"] == [[1]]

    def test_monotonicity_refusal_is_per_ident_not_per_batch(self, real_db):
        """The gate that must not become batch-wide. One ident whose guess
        would move LATER must be left alone while the rest of the batch
        still applies -- batching must not turn a refusal into a dropped
        batch, nor a refused ident into an included one."""
        import mcp_server
        pinned = ":function/src-auth-py-pinned"
        movable = ":function/src-auth-py-movable"
        pos_by_commit_ident = {":commit/h0": 0, ":commit/h1": 1, ":commit/h2": 2}
        # pinned already sits at the EARLIEST commit; h1 would move it later.
        mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, [pinned], ":commit/h0", "2026-01-01T00:00:00Z",
            pos=0, pos_by_commit_ident=pos_by_commit_ident,
        )
        mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, [movable], ":commit/h2", "2026-01-03T00:00:00Z",
            pos=2, pos_by_commit_ident=pos_by_commit_ident,
        )

        moved = mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, [pinned, movable], ":commit/h1", "2026-01-02T00:00:00Z",
            pos=1, pos_by_commit_ident=pos_by_commit_ident,
        )

        assert moved == {movable}
        assert mcp_server._entity_introduced_by_query(real_db, pinned) == ":commit/h0"
        assert mcp_server._entity_introduced_by_query(real_db, movable) == ":commit/h1"

    def test_authoritative_entities_are_never_touched(self, real_db):
        import mcp_server
        authoritative = ":function/src-auth-py-authoritative"
        provisional = ":function/src-auth-py-provisional"
        mcp_server._transact(
            real_db, f"[[{authoritative} :introduced-by :commit/h5]]", "2026-01-06T00:00:00Z",
        )
        mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, [provisional], ":commit/h2", "2026-01-03T00:00:00Z",
        )

        moved = mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, [authoritative, provisional], ":commit/h0", "2026-01-01T00:00:00Z",
        )

        assert moved == {provisional}
        assert mcp_server._entity_introduced_by_query(real_db, authoritative) == ":commit/h5"
        assert mcp_server._lineage_is_provisional(real_db, authoritative) is False

    def test_same_value_is_idempotent_but_still_marks(self, real_db):
        import mcp_server
        ident = ":function/src-auth-py-login"

        mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, [ident], ":commit/h1", "2026-01-02T00:00:00Z",
        )
        mcp_server._entity_introduced_by_set_provisional_batch(
            real_db, [ident], ":commit/h1", "2026-01-02T00:00:01Z",
        )

        raw = mcp_server._db_execute(
            real_db, f"(query [:find (count ?c) :where [{ident} :introduced-by ?c]])"
        )
        assert json.loads(raw)["results"] == [[1]]
        assert mcp_server._lineage_is_provisional(real_db, ident) is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestEntityIntroducedBySetProvisionalBatch -q`
Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_entity_introduced_by_set_provisional_batch'`

- [ ] **Step 3: Add `_lineage_mark_provisional_batch`**

Immediately after `_lineage_mark_provisional`:

```python
def _lineage_mark_provisional_batch(
    db: Any, entity_idents: Sequence[str], commit_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Batched _lineage_mark_provisional (#233). Same query-before-write
    per ident -- an entity already marked is skipped, never duplicated --
    but the facts for every genuinely-new marker go in ONE _transact.

    Collision-free: each marker is its own :lineage/... companion entity, so
    no two facts in the batch share (entity, attribute, valid_from).
    """
    facts = []
    for entity_ident in entity_idents:
        if _lineage_is_provisional(db, entity_ident):
            continue
        ident = _lineage_marker_ident(entity_ident)
        facts.extend([
            f"[{ident} :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}]",
            f"[{ident} :entity {entity_ident}]",
            f"[{ident} :status :provisional]",
        ])
    if facts:
        _transact(db, "[" + " ".join(facts) + "]", commit_ts_iso, index_con=index_con)
```

- [ ] **Step 4: Add the batch and reduce the per-ident function to a wrapper**

Replace the body of `_entity_introduced_by_set_provisional` and add the batch above it:

```python
def _entity_introduced_by_set_provisional_batch(
    db: Any,
    entity_idents: Sequence[str],
    commit_ident: str,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
    pos: Optional[int] = None,
    pos_by_commit_ident: Optional[Dict[str, int]] = None,
) -> Set[str]:
    """Assert or move a PROVISIONAL :introduced-by to commit_ident for many
    entities at once (#233), in one _retract and at most two _transact
    calls instead of two writes per ident. _reverse_apply's two loops were
    the only production callers of the per-ident form, at ~1,265 idents per
    commit on a real repository; retracts cost ~13ms against a transact's
    ~1ms, so the retract batching is the load-bearing half.

    Returns the set of idents whose guess was actually asserted or moved.

    Every gate is per-ident and is applied in the same order the per-ident
    function used, because batching must not turn one ident's refusal into
    the whole batch being dropped, nor a refused ident into an included one:

      - authoritative (a value exists and _lineage_is_provisional is False)
        -- never touched; reverse walk must never clobber a confirmed fact
      - value already equals commit_ident -- no write, but still marked
      - the monotonicity refusal (a guess may only move EARLIER), with its
        own stderr line per refused ident

    Collision-free: within each batch the facts differ in entity, and only
    facts sharing (entity, attribute, valid_from) collapse in minigraf's
    EAVT pending index. :contains is the attribute where that bites, and it
    is not written here.
    """
    to_retract: List[str] = []
    to_transact: List[str] = []
    to_mark: List[str] = []
    moved: Set[str] = set()

    for entity_ident in entity_idents:
        current = _entity_introduced_by_query(db, entity_ident)
        if current is not None and not _lineage_is_provisional(db, entity_ident):
            continue  # authoritative -- never touch
        if current == commit_ident:
            to_mark.append(entity_ident)
            continue
        if current is not None and pos is not None and pos_by_commit_ident is not None:
            current_pos = pos_by_commit_ident.get(current)
            if current_pos is not None and pos >= current_pos:
                print(
                    f"[_entity_introduced_by_set_provisional] refusing to move "
                    f"{entity_ident}'s guess from {current} (position {current_pos}) "
                    f"to {commit_ident} (position {pos}): a guess may only move earlier",
                    file=sys.stderr,
                )
                continue
        if current is not None:
            to_retract.append(f"[{entity_ident} :introduced-by {current}]")
        to_transact.append(f"[{entity_ident} :introduced-by {commit_ident}]")
        to_mark.append(entity_ident)
        moved.add(entity_ident)

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    if to_transact:
        _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)
    _lineage_mark_provisional_batch(db, to_mark, commit_ts_iso, index_con=index_con)
    return moved


def _entity_introduced_by_set_provisional(
    db: Any,
    entity_ident: str,
    commit_ident: str,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
    pos: Optional[int] = None,
    pos_by_commit_ident: Optional[Dict[str, int]] = None,
) -> None:
    """One-entity form of _entity_introduced_by_set_provisional_batch --
    see that function for the gates. A delegating wrapper rather than a
    parallel implementation (#233) so the batched and unbatched paths
    cannot drift apart; the batch is the only production caller shape.
    """
    _entity_introduced_by_set_provisional_batch(
        db, [entity_ident], commit_ident, commit_ts_iso,
        index_con=index_con, pos=pos, pos_by_commit_ident=pos_by_commit_ident,
    )
```

Confirm `Sequence` and `Set` are in the `typing` import line at the top of `mcp_server.py`; add them if not.

- [ ] **Step 5: Call the batch from `_reverse_apply`'s two loops**

Replace the `new_candidates` loop and the head of the `provisional_moves` loop:

```python
    _entity_introduced_by_set_provisional_batch(
        db, new_candidates, commit_ident, commit_ts_iso, index_con=index_con,
        pos=pos, pos_by_commit_ident=pos_by_commit_ident,
    )
    _entity_introduced_by_set_provisional_batch(
        db, [ident for ident, _superseded in provisional_moves], commit_ident,
        commit_ts_iso, index_con=index_con,
        pos=pos, pos_by_commit_ident=pos_by_commit_ident,
    )

    for ident, superseded_ident in provisional_moves:
        if superseded_ident is not None and superseded_ident != commit_ident:
            # ... body unchanged from what Task 2 Step 3 left in the file:
            # the superseded_pos refusal, the missing-timestamp skip, and
            # the per-ident `_transact` of the retroactive :modified-in.
            # Task 4 replaces this whole loop; leave it alone here.
```

Only the two `_entity_introduced_by_set_provisional` calls change in this
step. The `provisional_moves` loop keeps the body Task 2 gave it, minus its
now-hoisted `_entity_introduced_by_set_provisional` call.

- [ ] **Step 6: Run the batch tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestEntityIntroducedBySetProvisionalBatch tests/test_mcp_server.py::TestEntityIntroducedBySetProvisional tests/test_mcp_server.py::TestLineageProvisionalMarker -q`
Expected: PASS. `TestEntityIntroducedBySetProvisional` and `TestLineageProvisionalMarker` are unchanged and prove the wrapper preserves per-ident behaviour.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Batch the provisional guess move and its lineage markers (#233)

_entity_introduced_by_set_provisional issued one retract plus one transact
per entity, called once per candidate per commit from _reverse_apply's two
loops -- ~1,265 idents per commit on this repo, 8,618 retracts over 12
commits. Retracts cost ~13ms against a transact's ~1ms, so the retract is
the load-bearing half.

Facts differing in entity do not collide in minigraf's EAVT pending index
-- only facts sharing (entity, attribute, valid_from) collapse, which is
why :contains has to stay one triple per call and :introduced-by does not.
_reverse_apply already batched authoritative_modified_triples on exactly
this reasoning.

The batch is the implementation and the per-ident function delegates to
it, so the authoritative-skip, already-equal and monotonicity gates cannot
drift between the two. All three stay per-ident: batching must not turn
one ident's refusal into a dropped batch, nor a refused ident into an
included one.

Refs #233"
```

---

### Task 4: Batch the retroactive `:modified-in` and the sweep's confirm

The retroactive `:modified-in` loop issues one transact per entity (10,161 over 12 commits). Each edge carries the *superseded* commit's timestamp rather than this commit's, so a single batch is impossible — but grouping by `superseded_ts` collapses to a handful per commit, because entities in one file were almost always last sighted at the same commit. Separately, the sweep's `_lineage_confirm` is one retract per confirmed entity.

**Files:**
- Modify: `mcp_server.py` — group the retroactive `:modified-in` transacts in `_reverse_apply`; add `_lineage_confirm_batch`; use it in `_correction_sweep_apply`
- Test: `tests/test_mcp_server.py` — add tests to `TestReverseApplySplit` and `TestCorrectionSweepApply`

**Interfaces:**
- Consumes: Task 2's retroactive `:modified-in` body, Task 3's batching precedent
- Produces: `_lineage_confirm_batch(db, entity_idents: Sequence[str], index_con=None) -> None`; `_lineage_confirm(db, entity_ident, index_con=None)` keeps its signature and delegates

- [ ] **Step 1: Write the failing tests**

Add to `TestReverseApplySplit`:

```python
    def test_retroactive_modified_in_is_grouped_by_superseded_timestamp(self, real_db, tmp_path):
        """#233: one transact per entity here was 10,161 calls over 12
        commits of this repo. Each edge carries the SUPERSEDED commit's
        timestamp, not this commit's, so one batch is impossible -- but
        entities in one file were almost always last sighted at the same
        commit, so grouping by timestamp collapses to a handful."""
        import mcp_server
        import frontier_registry
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        bodies = "\n\n".join(f"def f{i}():\n    return 0" for i in range(12))
        for rev in range(2):
            (repo / "wide.py").write_text(bodies.replace("return 0", f"return {rev}") + "\n")
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"c{rev}"], cwd=repo, check=True, capture_output=True)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        # Claim the tip first so the second claim is a genuine move.
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )

        transacts = []
        real_transact = mcp_server._transact
        mcp_server._transact = lambda db_, facts, *a, **k: (
            transacts.append(facts) or real_transact(db_, facts, *a, **k)
        )
        try:
            mcp_server._reverse_fill_claim_and_process(
                real_db, str(repo), linearization, commit_metadata, allocator,
            )
        finally:
            mcp_server._transact = real_transact

        retroactive = [f for f in transacts if ":modified-in" in f]
        assert len(retroactive) <= 2, (
            f"grouped by superseded timestamp, not one per entity; got {len(retroactive)}"
        )

    def test_grouped_retroactive_modified_in_keeps_every_edge(self, real_db, tmp_path):
        """Grouping must not lose edges -- these facts differ in entity, so
        they do not collide, but that is the assumption worth pinning."""
        import mcp_server
        import frontier_registry
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        bodies = "\n\n".join(f"def f{i}():\n    return 0" for i in range(6))
        for rev in range(2):
            (repo / "wide.py").write_text(bodies.replace("return 0", f"return {rev}") + "\n")
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"c{rev}"], cwd=repo, check=True, capture_output=True)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")

        mcp_server._reverse_bulk_fill_walk(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )

        tip_ident = f":commit/{linearization[1][:12]}"
        raw = mcp_server._db_execute(
            real_db, f"(query [:find ?e :where [?e :modified-in {tip_ident}]])"
        )
        modified = {row[0] for row in json.loads(raw)["results"]}
        for i in range(6):
            ident = mcp_server._code_ident("function", "wide.py", f"f{i}")
            assert ident in modified, f"{ident} lost its retroactive edge; got {sorted(modified)}"
```

Add to `TestCorrectionSweepApply`:

```python
    def test_confirm_is_batched_across_entities_at_one_commit(self, real_db, tmp_path):
        """#233: _lineage_confirm was one retract per confirmed entity, and
        retracts are the expensive op. The markers are distinct :lineage/...
        companion entities, so one call covers them all."""
        import mcp_server
        import frontier_registry
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        bodies = "\n\n".join(f"def f{i}():\n    return 0" for i in range(8))
        (repo / "wide.py").write_text(bodies + "\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "c0"], cwd=repo, check=True, capture_output=True)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_bulk_fill_walk(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        file_results, _g, _m, _r = mcp_server._extract_commit(str(repo), linearization[0], ())

        retracts = []
        real_retract = mcp_server._retract
        mcp_server._retract = lambda db_, facts, *a, **k: (
            retracts.append(facts) or real_retract(db_, facts, *a, **k)
        )
        try:
            mcp_server._correction_sweep_apply(
                real_db, linearization[0], commit_metadata[0][1], file_results,
            )
        finally:
            mcp_server._retract = real_retract

        marker_retracts = [f for f in retracts if ":type/lineage-marker" in f]
        assert len(marker_retracts) == 1, (
            f"one retract for every marker at this commit; got {len(marker_retracts)}"
        )
        for i in range(8):
            ident = mcp_server._code_ident("function", "wide.py", f"f{i}")
            assert mcp_server._lineage_is_provisional(real_db, ident) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestReverseApplySplit tests/test_mcp_server.py::TestCorrectionSweepApply -q`
Expected: the three new tests FAIL on their call-count assertions (`test_grouped_retroactive_modified_in_keeps_every_edge` may already pass — it is the guard, not the driver).

- [ ] **Step 3: Group the retroactive `:modified-in` transacts**

Replace the `provisional_moves` loop tail in `_reverse_apply`:

```python
    # #233: one transact per entity here was 10,161 calls over 12 commits of
    # this repo. Each edge is asserted at the SUPERSEDED commit's own
    # timestamp, not this commit's, so one batch is impossible -- but
    # entities touched by one commit were almost always last sighted at the
    # same commit, so grouping by that timestamp collapses to a handful.
    # Every per-ident gate below stays per-ident and is evaluated during
    # grouping. Facts differing in entity do not collide, so each group is
    # one safe transact.
    retroactive_by_ts: Dict[str, List[str]] = {}
    for ident, superseded_ident in provisional_moves:
        if superseded_ident is None or superseded_ident == commit_ident:
            continue
        superseded_pos = pos_by_commit_ident.get(superseded_ident)
        if superseded_pos is not None and superseded_pos <= pos:
            # The move was refused (or would be): the "superseded" guess is
            # not actually later than this commit, so asserting it as a
            # modification would claim the entity changed at or before its
            # own introduction.
            continue
        superseded_ts = ts_by_commit_ident.get(superseded_ident)
        if superseded_ts is None:
            # Falling back to THIS commit's timestamp back-dates the edge to
            # before the modification it describes -- a fact asserted valid
            # before it was true. Skip and say so instead.
            print(
                f"[_reverse_apply] skipping retroactive :modified-in "
                f"for {ident} at {superseded_ident}: no timestamp in commit_metadata",
                file=sys.stderr,
            )
            continue
        retroactive_by_ts.setdefault(superseded_ts, []).append(
            f"[{ident} :modified-in {superseded_ident}]"
        )
    for superseded_ts, triples in retroactive_by_ts.items():
        _transact(db, "[" + " ".join(triples) + "]", superseded_ts, index_con=index_con)
```

- [ ] **Step 4: Add `_lineage_confirm_batch` and delegate**

Replace `_lineage_confirm` with:

```python
def _lineage_confirm_batch(
    db: Any, entity_idents: Sequence[str], index_con: Optional[Any] = None
) -> None:
    """Batched _lineage_confirm (#233). Retracts the :type/lineage-marker
    companion entity's facts for every still-provisional ident in ONE
    _retract; idents with no marker are skipped, so callers can pass a whole
    commit's candidates unconditionally.

    Collision-free: each marker is its own :lineage/... companion entity.
    """
    facts = []
    for entity_ident in entity_idents:
        if not _lineage_is_provisional(db, entity_ident):
            continue
        ident = _lineage_marker_ident(entity_ident)
        facts.extend([
            f"[{ident} :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}]",
            f"[{ident} :entity {entity_ident}]",
            f"[{ident} :status :provisional]",
        ])
    if facts:
        _retract(db, "[" + " ".join(facts) + "]", index_con=index_con)


def _lineage_confirm(db: Any, entity_ident: str, index_con: Optional[Any] = None) -> None:
    """Retract the :type/lineage-marker companion entity's facts for
    entity_ident if present; no-op if absent, so callers can call this
    unconditionally without checking first. One-entity form of
    _lineage_confirm_batch, delegating so the two cannot drift (#233).
    """
    _lineage_confirm_batch(db, [entity_ident], index_con=index_con)
```

- [ ] **Step 5: Use the batch in `_correction_sweep_apply`**

Case 1 no longer confirms inline. Collect instead, and flush once per file loop iteration — the re-date from Task 2 stays inline, since `:contains` cannot be batched:

```python
        to_confirm: List[str] = []
        for ident in candidate_idents:
            ...
            if _lineage_is_provisional(db, ident):
                if introduced_by_values == {commit_ident}:
                    _re_date_structural_facts(
                        db,
                        [t for t in candidate_triples_by_ident.get(ident, [])
                         if ":introduced-by" not in t],
                        commit_ts_iso,
                        index_con=index_con,
                    )
                    to_confirm.append(ident)
                else:
                    # case 2: unchanged -- the skipped_events increment and
                    # its capped stderr line, exactly as they are today
                    ...
            else:
                # case 3 (already authoritative): unchanged -- the
                # single-value :modified-in assert/retract and the
                # ambiguous-value skip, exactly as they are today
                ...
        _lineage_confirm_batch(db, to_confirm, index_con=index_con)
```

The only edits in case 1 are: `_lineage_confirm(db, ident, index_con=index_con)` becomes `to_confirm.append(ident)`. Cases 2 and 3 are untouched. Declare `to_confirm: List[str] = []` at the top of the per-file loop body and flush it at the bottom of that same iteration, so a file's confirms are one call.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestReverseApplySplit tests/test_mcp_server.py::TestCorrectionSweepApply -q`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Batch the retroactive :modified-in and the sweep's confirm (#233)

The retroactive :modified-in loop issued one transact per entity: 10,161
calls over 12 commits of this repo. Each edge is asserted at the SUPERSEDED
commit's own timestamp rather than this commit's, so a single batch is
impossible -- but entities touched by one commit were almost always last
sighted at the same commit, so grouping by that timestamp collapses to a
handful per commit. Every per-ident gate (the superseded_pos <= pos
refusal, the self-introduction guard, the missing-timestamp skip and its
stderr line) is evaluated during grouping and stays per-ident.

_lineage_confirm was one retract per confirmed entity in the sweep, and
retracts cost ~13ms against a transact's ~1ms. The markers are distinct
:lineage/... companion entities, so one retract covers a whole file's
confirms. The per-ident function delegates to the batch.

The re-date stays inline per entity: its :contains half cannot be batched
(minigraf#287 -- [module :contains fn] has the module as entity, so a
module's children all collide), and after the previous commit it runs once
per entity for the whole region rather than once per touch.

Refs #233"
```

---

### Task 5: The budget regression test and the benchmark acceptance run

Phase 2's fixtures were all 6–10 commits and varied *commit* count. #233 is a per-entity scaling defect, so no fixture in the suite could have caught it. This task adds the test that would have, and runs the acceptance gate.

**Files:**
- Test: `tests/test_mcp_server.py` — add `TestReverseApplyWriteBudget`
- Modify: `evals/at_scale/benchmark.md` — record the post-fix numbers

**Interfaces:**
- Consumes: everything from Tasks 1–4
- Produces: nothing consumed downstream

- [ ] **Step 1: Write the budget test**

```python
class TestReverseApplyWriteBudget:
    """#233's regression test. The reverse walk was issuing ~6,970 write
    calls per commit against the forward walk's 12, because every per-entity
    write was per-entity-per-commit. Phase 2's fixtures were all 6-10
    commits and varied COMMIT count, so none of them could see it -- this
    varies ENTITY count at a fixed commit count, which is the axis the
    defect lived on."""

    def _repo_with_n_functions(self, tmp_path, n, name):
        repo = tmp_path / name
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for rev in range(2):
            body = "\n\n".join(f"def f{i}():\n    return {rev}" for i in range(n))
            (repo / "wide.py").write_text(body + "\n")
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"c{rev}"], cwd=repo, check=True, capture_output=True)
        return repo

    def _writes_for_the_second_claim(self, tmp_path, repo, graph_name):
        """Claim the tip (every entity is new), then count write calls for
        the claim below it (every entity is a provisional MOVE -- the path
        that scaled with entity count).

        Opens its own graph rather than reusing the real_db fixture's: this
        test runs two repos in one test body, and both name their file
        wide.py with functions f0.., so their idents would collide in a
        shared graph (and the second _frontier_load would read the first
        repo's now-stale bounds). The real_db fixture has already
        monkeypatched MiniGrafDb.open to hand back a fresh in-memory db per
        call, so open_db here is cheap and isolated."""
        import mcp_server
        import frontier_registry
        real_db = mcp_server.open_db(str(tmp_path / graph_name)) or mcp_server.get_db()
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )

        calls = []
        real_transact, real_retract = mcp_server._transact, mcp_server._retract
        mcp_server._transact = lambda *a, **k: (calls.append("t") or real_transact(*a, **k))
        mcp_server._retract = lambda *a, **k: (calls.append("r") or real_retract(*a, **k))
        try:
            mcp_server._reverse_fill_claim_and_process(
                real_db, str(repo), linearization, commit_metadata, allocator,
            )
        finally:
            mcp_server._transact, mcp_server._retract = real_transact, real_retract
        return len(calls)

    def test_per_commit_writes_do_not_scale_with_entity_count(self, real_db, tmp_path):
        small = self._writes_for_the_second_claim(
            tmp_path, self._repo_with_n_functions(tmp_path, 5, "small"), "small.graph"
        )
        large = self._writes_for_the_second_claim(
            tmp_path, self._repo_with_n_functions(tmp_path, 50, "large"), "large.graph"
        )

        assert large - small <= 5, (
            f"a 10x entity count must not multiply per-commit writes: "
            f"5 functions cost {small} write calls, 50 cost {large}. "
            f"Before #233 this ratio was ~10x."
        )

    def test_per_commit_writes_stay_in_the_forward_walk_s_range(self, real_db, tmp_path):
        """The absolute bar, not just the scaling one. The forward walk
        sustains ~12 writes per commit: one batched transact, one per
        :contains, one per :parent, watermark, checkpoint."""
        writes = self._writes_for_the_second_claim(
            tmp_path, self._repo_with_n_functions(tmp_path, 50, "wide"), "wide.graph"
        )

        assert writes <= 20, f"expected the forward walk's ~12 per commit; got {writes}"
```

- [ ] **Step 2: Run the budget test**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestReverseApplyWriteBudget -q`
Expected: PASS after Tasks 1–4.

If `test_per_commit_writes_stay_in_the_forward_walk_s_range` fails on a count between 20 and 40, do **not** relax the bound first — re-run the profiler to find the remaining per-entity call site and fix it:
`.venv/bin/python evals/at_scale/profile_reverse_walk_writes.py 3`

- [ ] **Step 3: Verify the test would have caught the original bug**

A regression test that has never been seen to fail is not yet a regression test. Swap in master's `mcp_server.py` and confirm it fails:

```bash
git checkout master -- mcp_server.py
.venv/bin/python -m pytest tests/test_mcp_server.py::TestReverseApplyWriteBudget -q
```
Expected: FAIL, on both methods.

Restore and re-confirm:
```bash
git checkout HEAD -- mcp_server.py
.venv/bin/python -m pytest tests/test_mcp_server.py::TestReverseApplyWriteBudget -q
```
Expected: PASS

Record the failing numbers from the first run — they belong in the commit message.

- [ ] **Step 4: Run the per-call-site profiler on real history**

Run: `.venv/bin/python evals/at_scale/profile_reverse_walk_writes.py 12`

Compare against the pre-fix baseline recorded in the spec (83,638 writes, 6,970 tx/commit, 532.1 s). Record the new table — it goes in the final commit message.

- [ ] **Step 5: Run the acceptance gate**

Run: `.venv/bin/python evals/at_scale/run_ingestion_benchmark.py --repo-path . --branch master`

This must **complete**. Compare against the recorded 2026-07-19 forward-only baseline: 498 commits, 78.87 s, 378.9 commits/min, 45,801,472 B graph.

The bar is not parity with 78.87 s — Stage A writes provisional lineage the baseline never wrote, and Stage B re-parses. The bar is that total ingestion completes in a time of the same order rather than a different one, with graph size within a small multiple of 45,801,472 B, and that neither number scales with entity *touches*.

- [ ] **Step 6: Run the full suite one final time**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q`
Expected: PASS

- [ ] **Step 7: Record the results and commit**

Add a row to `evals/at_scale/benchmark.md` recording the post-#233 run alongside the 2026-07-19 baseline and the killed 2d run, with the same columns the existing rows use.

```bash
git add tests/test_mcp_server.py evals/at_scale/benchmark.md
git commit -m "Add the #233 write-budget regression test and record the benchmark

Phase 2's fixtures were all 6-10 commits and varied COMMIT count. #233 was
a per-ENTITY scaling defect, so no test in the suite could see it: the
reverse walk issued ~6,970 write calls per commit against the forward
walk's 12, and a 10-commit fixture with three functions in it looks
perfectly healthy.

TestReverseApplyWriteBudget varies entity count at a fixed commit count --
5 functions against 50, both committed twice, counting write calls for the
second claim (where every entity is a provisional move, the path that
scaled). It asserts both the scaling property and the absolute bar.

Verified to fail against master's mcp_server.py.

Refs #233"
```

- [ ] **Step 8: Check the docs**

`SKILL.md` documents query syntax and the memory interface; none of this changes either, so it needs no edit — but confirm rather than assume:
`grep -n "candidate-diff\|provisional\|introduced-by" SKILL.md README.md ROADMAP.md`

If `ROADMAP.md` tracks #222's phases, note that #233 is resolved and phases 3–5 are unblocked. Commit any doc change separately.

---

## Done

The branch is `fix-233-reverse-walk-write-amplification`, already carrying the spec commit. **Do not push** — commit locally only until the user lifts that restriction. Report the profiler table and the benchmark result; the user will decide when it goes out.
