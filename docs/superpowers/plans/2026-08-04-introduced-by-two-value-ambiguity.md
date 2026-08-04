# Two-value `:introduced-by` ambiguity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the forward walk minting a second `:introduced-by` alongside the reverse stream's provisional guess, collapse the multiplicity on graphs already corrupted, and make any residual ambiguity visible in logs.

**Architecture:** Three independent changes to `mcp_server.py`. (1) `_forward_apply`'s non-`lifecycle_only` branch drops its `state.provisional_idents` prefilter so `_lineage_is_provisional` is the sole authority — the prefilter is a run-start snapshot and is empty of same-run guesses. (2) `_correction_sweep_apply` gains an optional `pos_by_commit_ident` map and, when given it, collapses ≥2 `:introduced-by` values down to the minimum-position one before its existing case 1/2/3 branching runs. (3) A new `_entity_introduced_by_values_query` returns all values; `_entity_introduced_by_query` delegates to it and warns on >1 without raising.

**Tech Stack:** Python 3, `pytest`, real `minigraf` backend (no mocks), git fixtures built with `subprocess`.

Spec: `docs/superpowers/specs/2026-08-04-introduced-by-two-value-ambiguity-design.md`
Issue: #235. Branch: `fix-235-two-value-introduced-by` (already created, spec already committed as `77e8630`).

## Global Constraints

- **Real backend only.** Every test uses the `real_db` fixture or a real file-backed `MiniGrafDb.open()`. Never a `MagicMock` of `MiniGrafDb`. Never assert on mock call arguments — always re-query the DB and assert on persisted state. See `docs/testing-conventions.md`.
- **`_entity_introduced_by_query` must never raise on >1 value.** Both walks call it mid-run, before Stage B; raising would hard-fail ingestion on exactly the corrupted graphs the repair exists to heal.
- **`pos_by_commit_ident` must be the LAST parameter of `_correction_sweep_apply`.** The Stage B driver at `mcp_server.py:9574` calls it through `run_in_executor` with seven *positional* arguments (`db, sweep_hash, sweep_ts, sweep_files, index_con, skipped, False`). Inserting a parameter before `update_watermark` silently shifts them.
- **Repair collapses multiplicity only; it never confirms.** After collapsing, cases 1/2/3 run unchanged on the surviving single value.
- **Do not write closing keywords** (`fixes`, `closes`, `resolves`) for #235 in any commit message or PR body — including negated forms. Reference it as `(#235)` only. #235 is closed manually after the benchmark entry lands.
- Run the full suite with `python -m pytest tests/test_mcp_server.py -q` from the repo root.

---

### Task 1: All-values query helper, loud on ambiguity

**Files:**
- Modify: `mcp_server.py:5378-5383` (`_entity_introduced_by_query`)
- Test: `tests/test_mcp_server.py` (new class `TestEntityIntroducedByValuesQuery`, place it immediately after `TestEntityIntroducedBySetProvisional`, which ends at line 6505)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_entity_introduced_by_values_query(db: Any, entity_ident: str) -> List[str]` — every live `:introduced-by` value for `entity_ident`, in the backend's unspecified order, `[]` if none. Task 3 uses it in `_correction_sweep_apply`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class TestEntityIntroducedByValuesQuery:
    """#235: an entity can hold two live :introduced-by facts. The
    single-value query silently returned the first of them, so the
    ambiguity was invisible; the all-values query is what the correction
    sweep's repair path needs to collapse it."""

    def test_values_query_returns_empty_for_unknown_entity(self, real_db):
        import mcp_server
        assert mcp_server._entity_introduced_by_values_query(real_db, ":function/nope") == []

    def test_values_query_returns_single_value(self, real_db):
        import mcp_server
        entity_ident = ":function/src-auth-py-login"
        mcp_server._transact(
            real_db, f"[[{entity_ident} :introduced-by :commit/h0]]", "2026-01-01T00:00:00Z",
        )
        assert mcp_server._entity_introduced_by_values_query(real_db, entity_ident) == [":commit/h0"]

    def test_values_query_returns_both_values_of_a_corrupted_entity(self, real_db):
        import mcp_server
        entity_ident = ":function/src-auth-py-login"
        mcp_server._transact(
            real_db, f"[[{entity_ident} :introduced-by :commit/h0]]", "2026-01-01T00:00:00Z",
        )
        mcp_server._transact(
            real_db, f"[[{entity_ident} :introduced-by :commit/h5]]", "2026-01-05T00:00:00Z",
        )
        assert sorted(
            mcp_server._entity_introduced_by_values_query(real_db, entity_ident)
        ) == [":commit/h0", ":commit/h5"]

    def test_single_value_query_warns_on_ambiguity_and_still_returns_a_value(
        self, real_db, capsys,
    ):
        """Must NOT raise: both walks call this mid-run, before Stage B,
        so raising would hard-fail ingestion on the very graphs the
        sweep's repair exists to heal."""
        import mcp_server
        entity_ident = ":function/src-auth-py-login"
        mcp_server._transact(
            real_db, f"[[{entity_ident} :introduced-by :commit/h0]]", "2026-01-01T00:00:00Z",
        )
        mcp_server._transact(
            real_db, f"[[{entity_ident} :introduced-by :commit/h5]]", "2026-01-05T00:00:00Z",
        )
        capsys.readouterr()  # discard anything the seeding wrote

        result = mcp_server._entity_introduced_by_query(real_db, entity_ident)

        assert result in (":commit/h0", ":commit/h5")
        err = capsys.readouterr().err
        assert entity_ident in err
        assert ":commit/h0" in err and ":commit/h5" in err

    def test_single_value_query_is_silent_for_one_value(self, real_db, capsys):
        import mcp_server
        entity_ident = ":function/src-auth-py-login"
        mcp_server._transact(
            real_db, f"[[{entity_ident} :introduced-by :commit/h0]]", "2026-01-01T00:00:00Z",
        )
        capsys.readouterr()

        assert mcp_server._entity_introduced_by_query(real_db, entity_ident) == ":commit/h0"
        assert "introduced-by" not in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestEntityIntroducedByValuesQuery -v`
Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_entity_introduced_by_values_query'` for the first three, and the warning test fails with an empty `err`.

- [ ] **Step 3: Implement**

Replace `mcp_server.py:5378-5383` in full:

```python
def _entity_introduced_by_values_query(db: Any, entity_ident: str) -> List[str]:
    """Every live :introduced-by value for entity_ident, in the backend's
    unspecified order; [] if it has none.

    A list rather than a single value because an entity CAN hold more than
    one (#235): the forward walk used to mint a second alongside the reverse
    stream's provisional guess. _correction_sweep_apply's repair path needs
    to see all of them to collapse them.
    """
    raw = _db_execute(db, f"(query [:find ?c :where [{entity_ident} :introduced-by ?c]])")
    return [row[0] for row in json.loads(raw).get("results", [])]


def _entity_introduced_by_query(db: Any, entity_ident: str) -> Optional[str]:
    """Return entity_ident's current :introduced-by value (a commit ident
    string), or None if it has none yet.

    An entity holding two values is corrupt (#235). Which of them is
    returned here is UNSPECIFIED -- the backend imposes no ordering -- so
    this warns and returns an arbitrary one rather than pretending the
    graph is well-formed. It deliberately does NOT raise: both walks call
    this during a run, long before Stage B's repair pass, so raising would
    hard-fail ingestion on exactly the graphs that repair exists to heal.
    Position-based selection of the survivor lives in
    _correction_sweep_apply, which has the linearization positions this
    function does not.
    """
    values = _entity_introduced_by_values_query(db, entity_ident)
    if len(values) > 1:
        print(
            f"[_entity_introduced_by] {entity_ident} has {len(values)} live "
            f":introduced-by values {sorted(values)} -- returning an arbitrary "
            "one (#235); the correction sweep repairs this",
            file=sys.stderr,
        )
    return values[0] if values else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestEntityIntroducedByValuesQuery -v`
Expected: 5 passed.

- [ ] **Step 5: Run the neighbouring suites for regressions**

Run: `python -m pytest tests/test_mcp_server.py -q -k "IntroducedBy or Lineage or CorrectionSweep or ReverseApply or ReverseFill"`
Expected: all pass. No behaviour changed — only an added stderr line on already-corrupt input.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add an all-values :introduced-by query and warn on ambiguity (#235)

_entity_introduced_by_query silently returned results[0][0], so an entity
holding two live values looked well-formed to every caller. It now
delegates to _entity_introduced_by_values_query and logs when it has to
pick arbitrarily. It must not raise: both walks call it mid-run, before
Stage B, so raising would hard-fail ingestion on the corrupted graphs the
sweep's repair pass exists to heal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Stop minting the second `:introduced-by`

**Files:**
- Modify: `mcp_server.py:8306-8361` (comment block + the non-`lifecycle_only` prefilter) and `mcp_server.py:8390` (eviction loop)
- Modify: `tests/test_mcp_server.py:16619` (the preload-ordering bug in the existing test)
- Test: `tests/test_mcp_server.py` — new test in `TestForwardApplyReconcilesProvisional` (class starts at line 16580), and a new class `TestNoDuplicateIntroducedByAfterFullIngest` placed immediately before `TestMultiStreamParityWithForwardOnly` (line 17517)

**Interfaces:**
- Consumes: nothing from Task 1 (independent — Tasks 1 and 2 may be done in either order).
- Produces: no new symbols. `_ForwardWalkState.provisional_idents` survives, still populated by `_preload_provisional_idents`, still drained by the eviction loop, but is no longer consulted as a gate.

- [ ] **Step 1: Fix the existing test's preload ordering, and add the fresh-ingest test**

In `tests/test_mcp_server.py`, `test_forward_apply_supersedes_a_provisional_guess` currently builds its state with `provisional_idents=mcp_server._preload_provisional_idents(real_db)` at line 16619 — *after* the reverse stream ran. That is why the bug is invisible to it. Change that one line to:

```python
            # #235: production preloads at RUN START, before Stream 2 has
            # written anything, so on a fresh ingest this set is EMPTY. Calling
            # _preload_provisional_idents here -- after the reverse stream ran --
            # handed the forward walk a snapshot that already contained the
            # guess, which no real fresh run ever has, and hid the second-mint
            # bug entirely.
            provisional_idents=set(),
```

Then add this test to the same class, after `test_forward_apply_does_not_duplicate_for_a_stale_provisional_snapshot_entry`:

```python
    def test_reconciles_a_guess_made_during_this_same_run(self, real_db, tmp_path):
        """#235: the prefilter was a run-start snapshot, empty on a fresh
        ingest. Stream 2's guesses are written during that same run, so the
        prefilter dropped every one of them before the DB-authoritative
        _lineage_is_provisional check could see it -- and
        _build_code_triples then minted a SECOND :introduced-by alongside
        the guess. The DB must be the sole authority."""
        import mcp_server, frontier_registry
        repo = self._repo(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")

        # Snapshot taken FIRST, exactly as _run_ingestion does -- empty.
        snapshot = mcp_server._preload_provisional_idents(real_db)
        assert snapshot == set(), "a fresh graph has no provisional idents yet"

        # Only now does Stream 2 claim h1 and guess.
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True

        state = mcp_server._ForwardWalkState(
            entity_valid_from={}, entity_descriptions={}, file_entities={},
            file_deps={}, dep_valid_from={}, pinned_commit_state={},
            field_class_ident={}, field_static_ident={}, submodule_paths={},
            unresolved_dep_idents={},
            provisional_idents=snapshot,
            ts_by_commit_ident={f":commit/{h[:12]}": ts for h, ts, _a, _s in commit_metadata},
        )
        extracted = mcp_server._extract_commit(str(repo), linearization[0], ())
        mcp_server._forward_apply(real_db, str(repo), state, commit_metadata[0], extracted)

        values = mcp_server._entity_introduced_by_values_query(real_db, fn_ident)
        assert values == [f":commit/{linearization[0][:12]}"], (
            f"exactly one :introduced-by, naming h0; got {sorted(values)}"
        )
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is False

    def test_evicts_a_stale_snapshot_entry_that_is_no_longer_provisional(
        self, real_db, tmp_path,
    ):
        """The eviction invariant survives dropping the prefilter: every
        candidate examined leaves state.provisional_idents, whether or not
        the DB agreed it was provisional. A stale entry left behind is
        retried, and fails the same way, on every later commit touching the
        entity (see the comment at mcp_server.py:8345)."""
        import mcp_server, frontier_registry
        repo = self._repo(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        # Authoritative in the DB (a plain transact, no lineage marker) but
        # still listed by a stale snapshot -- what a resumed run sees after a
        # previous run's sweep confirmed the entity.
        mcp_server._transact(
            real_db, f"[[{fn_ident} :introduced-by :commit/already]]", "2026-01-01T00:00:00Z",
        )
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is False

        state = mcp_server._ForwardWalkState(
            entity_valid_from={}, entity_descriptions={}, file_entities={},
            file_deps={}, dep_valid_from={}, pinned_commit_state={},
            field_class_ident={}, field_static_ident={}, submodule_paths={},
            unresolved_dep_idents={},
            provisional_idents={fn_ident},
            ts_by_commit_ident={f":commit/{h[:12]}": ts for h, ts, _a, _s in commit_metadata},
        )
        extracted = mcp_server._extract_commit(str(repo), linearization[0], ())
        mcp_server._forward_apply(real_db, str(repo), state, commit_metadata[0], extracted)

        assert fn_ident not in state.provisional_idents, "stale entry must be evicted"
```

- [ ] **Step 2: Write the whole-run integration oracle**

This is the test that reproduces the issue's reported numbers. Add a new class immediately before `TestMultiStreamParityWithForwardOnly` (line 17517):

```python
class TestNoDuplicateIntroducedByAfterFullIngest:
    """#235's reproduction, as a standing oracle.

    A full converging ingest -- both streams plus Stage B -- must leave no
    entity holding two live :introduced-by facts. On master this yields six
    such entities on a 14-commit history, and the pair never converges: every
    later reader sees only one value, retracts that one, and asserts a fresh
    one, so the corruption regenerates rather than healing.

    The fixture matters. `born_N` functions accumulate one per commit, so
    each is introduced at a different position and the reverse stream's
    guesses land above the forward walk's true introductions; the five
    long-lived functions are edited every commit so they stay candidates all
    the way down. That combination is what produces same-run provisional
    guesses for entities the forward walk later reaches.
    """

    N_COMMITS = 14

    def _reset_progress(self):
        import mcp_server
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
            "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
        }

    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init", "-b", "master"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for i in range(self.N_COMMITS):
            body = "".join(
                f"def stay_{k}():\n    return {i}\n\n" for k in range(5)
            ) + "".join(
                f"def born_{b}():\n    return {b}\n\n" for b in range(i + 1)
            )
            (repo / "mod.py").write_text(body)
            ts = f"2021-03-{i + 1:02d}T00:00:00Z"
            env = {**os.environ, "GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts}
            _subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
            _subprocess.run(
                ["git", "commit", "-m", f"c{i}"], cwd=repo, check=True,
                capture_output=True, env=env,
            )
        return repo

    @pytest.mark.asyncio
    async def test_full_ingest_leaves_no_entity_with_two_introduced_by(
        self, tmp_path, monkeypatch, capsys,
    ):
        import mcp_server
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path)
        graph_path = tmp_path / "memory.graph"

        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:1")
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph_path))
        mcp_server._db = None
        self._reset_progress()
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["status"] == "complete", mcp_server._ingest_progress
        mcp_server._db = None  # release the file lock before reopening

        db = MiniGrafDb.open(str(graph_path))
        try:
            raw = mcp_server._db_execute(
                db, "(query [:find ?e (count ?c) :where [?e :introduced-by ?c]])",
            )
        finally:
            db = None
        ambiguous = sorted(
            (row[0], row[1]) for row in json.loads(raw)["results"] if row[1] > 1
        )
        assert ambiguous == [], f"entities with multiple :introduced-by: {ambiguous}"

        err = capsys.readouterr().err
        assert "left provisional" not in err, err
        assert "left unreconciled" not in err, err
```

- [ ] **Step 3: Run all three to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestNoDuplicateIntroducedByAfterFullIngest tests/test_mcp_server.py::TestForwardApplyReconcilesProvisional -v`
Expected: FAIL.
- `test_full_ingest_leaves_no_entity_with_two_introduced_by` — `ambiguous` is a non-empty list (the issue reports 6 entities at 14 commits).
- `test_reconciles_a_guess_made_during_this_same_run` — two values instead of one.
- `test_forward_apply_supersedes_a_provisional_guess` — now fails too, because Step 1 removed the ordering that was hiding the bug. That failure is the point: confirm it, don't "fix" the test back.
- `test_evicts_a_stale_snapshot_entry_that_is_no_longer_provisional` — should PASS already (today's eviction loop drains `candidate_in_set`, which contains that ident). It is a guard against the Step 4 change, not a reproduction.

- [ ] **Step 4: Implement — drop the prefilter**

Replace `mcp_server.py:8306-8326` (the comment paragraph starting `# state.provisional_idents is a PRELOAD SNAPSHOT`) with:

```python
            # #235: state.provisional_idents is NOT consulted as a gate here.
            # It is a PRELOAD SNAPSHOT (see _preload_provisional_idents) taken
            # at run start, so on a fresh ingest it is EMPTY -- and Stream 2
            # writes its guesses during that same run. Using it as a prefilter
            # dropped every same-run guess before the DB-authoritative check
            # below could see it, _forward_reconcile_provisional never fired,
            # and _build_code_triples minted a SECOND :introduced-by alongside
            # the guess. A prefilter's false negatives are unrecoverable
            # precisely because the authority never runs.
            #
            # The snapshot is also stale-POSITIVE: by the time this commit is
            # reached an ident it lists may already have been confirmed
            # authoritative in the DB (reconciled earlier in this same forward
            # pass, or by a previous run's correction sweep). That is why
            # _lineage_is_provisional stays the authority rather than being
            # dropped in favour of a set -- popping entity_valid_from for an
            # already-authoritative ident hands it to _build_code_triples as
            # "new" and mints the same second fact, while
            # _forward_reconcile_provisional no-ops on it.
            #
            # The set survives only so a RESUMED run's genuinely-stale entries
            # drain: every candidate examined is evicted below regardless of
            # whether it survives the DB check, because an entry left in place
            # would be retried, and fail the same way, on every later commit
            # that touches the entity.
```

Then replace the `else:` branch at `mcp_server.py:8353-8361` with:

```python
            else:
                candidate_in_set = _forward_candidate_idents(precomputed)
                reconcilable = [
                    ident for ident in candidate_in_set
                    if _lineage_is_provisional(db, ident)
                ]
```

Leave the `lifecycle_only` branch, the `structural_triples_by_ident` build, the `for ident in reconcilable:` loop and the `for ident in candidate_in_set:` eviction loop exactly as they are. `candidate_in_set` is now the full candidate list, which is what makes eviction drain correctly.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestNoDuplicateIntroducedByAfterFullIngest tests/test_mcp_server.py::TestForwardApplyReconcilesProvisional -v`
Expected: all pass, `ambiguous == []`.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -q`
Expected: all pass. Watch `TestMultiStreamParityWithForwardOnly`, `TestReverseFillValidTimeParity` and `TestStageBCorrectionSweep` in particular — they are the guards that the converging walk still lands what a forward-only walk would.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Let the DB decide which idents are reconcilable (#235)

_forward_apply's non-lifecycle_only branch prefiltered reconciliation
candidates through state.provisional_idents, a snapshot taken at run
start and therefore empty on a fresh ingest. Stream 2's guesses are
written during that same run, so the prefilter dropped every one before
_lineage_is_provisional could see it, and _build_code_triples minted a
second :introduced-by alongside the guess.

The set stays only to drain a resumed run's stale entries; the eviction
loop now covers the full candidate list.

The existing supersedes-a-guess test preloaded the snapshot AFTER the
reverse stream ran, which no real fresh run does -- that ordering is what
kept the bug invisible to it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Repair already-corrupted graphs in the correction sweep

**Files:**
- Modify: `mcp_server.py:8874-8881` (signature), the docstring, and the per-ident loop at `mcp_server.py:8963-8975`
- Modify: `mcp_server.py:9078-9081` (`_correction_sweep_claim_and_process`'s call) and `mcp_server.py:9046-9055` (its signature)
- Modify: `mcp_server.py:9573-9577` (Stage B driver's `run_in_executor` call)
- Test: `tests/test_mcp_server.py`, new tests in `TestCorrectionSweepApply` (class starts at line 15669)

**Interfaces:**
- Consumes: `_entity_introduced_by_values_query(db, entity_ident) -> List[str]` from Task 1.
- Produces: `_correction_sweep_apply(db, commit_hash, commit_ts_iso, file_results, index_con=None, skipped_so_far=0, update_watermark=True, pos_by_commit_ident=None) -> int` — `pos_by_commit_ident` is `Optional[Dict[str, int]]` mapping `:commit/<12-char-hash>` to linearization position, exactly the map `_reverse_apply` builds at `mcp_server.py:7896`. `_correction_sweep_claim_and_process` gains the same keyword and forwards it.

- [ ] **Step 1: Write the failing tests**

Add to `TestCorrectionSweepApply` in `tests/test_mcp_server.py`. `self._repo_with_evolving_function` (line 15675) and `self._extract` (line 15699) already exist in that class.

```python
    def _pos_map(self, repo):
        import mcp_server, frontier_registry
        linearization = frontier_registry.build_linearization(str(repo))
        return {f":commit/{h[:12]}": i for i, h in enumerate(linearization)}, linearization

    def test_repair_keeps_the_lowest_position_introduced_by(self, real_db, tmp_path):
        """#235: an entity carrying two live :introduced-by values is
        collapsed to the one at the LOWEST linearization position. The
        reverse stream's guess is a sighting at or above the true
        introduction -- the same direction its own monotonicity rule
        encodes -- so the earlier value is the introduction."""
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        pos_map, linearization = self._pos_map(repo)
        h0, h2 = f":commit/{linearization[0][:12]}", f":commit/{linearization[2][:12]}"

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by {h2}]]", "2026-01-03T00:00:00Z")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by {h0}]]", "2026-01-01T00:00:00Z")
        assert len(mcp_server._entity_introduced_by_values_query(real_db, fn_ident)) == 2

        # The sweep meets the entity at h1 -- NEITHER of its two values.
        # That is the real shape: the second mint happens in the forward
        # region, which Stage B never sweeps, so a rule keyed on "is this
        # commit one of the values" would never fire.
        h1_hash, h1_ts = linearization[1], "2026-01-02T00:00:00Z"
        mcp_server._correction_sweep_apply(
            real_db, h1_hash, h1_ts, self._extract(repo, h1_hash),
            pos_by_commit_ident=pos_map,
        )

        assert mcp_server._entity_introduced_by_values_query(real_db, fn_ident) == [h0]

    def test_repair_is_inert_without_a_position_map(self, real_db, tmp_path):
        """Gated: every existing caller and test keeps today's fail-safe
        behaviour until it opts in."""
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        _pos_map, linearization = self._pos_map(repo)
        h0, h2 = f":commit/{linearization[0][:12]}", f":commit/{linearization[2][:12]}"

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by {h2}]]", "2026-01-03T00:00:00Z")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by {h0}]]", "2026-01-01T00:00:00Z")

        h1_hash = linearization[1]
        mcp_server._correction_sweep_apply(
            real_db, h1_hash, "2026-01-02T00:00:00Z", self._extract(repo, h1_hash),
        )

        assert sorted(mcp_server._entity_introduced_by_values_query(real_db, fn_ident)) == sorted([h0, h2])

    def test_repair_sorts_an_unknown_commit_ident_last(self, real_db, tmp_path):
        """A value absent from the map must never be chosen over a known
        one, and must never raise."""
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        pos_map, linearization = self._pos_map(repo)
        h0 = f":commit/{linearization[0][:12]}"

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by :commit/deadbeef0000]]", "2026-01-03T00:00:00Z")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by {h0}]]", "2026-01-01T00:00:00Z")

        h1_hash = linearization[1]
        mcp_server._correction_sweep_apply(
            real_db, h1_hash, "2026-01-02T00:00:00Z", self._extract(repo, h1_hash),
            pos_by_commit_ident=pos_map,
        )

        assert mcp_server._entity_introduced_by_values_query(real_db, fn_ident) == [h0]

    def test_repair_then_case_one_confirms_in_the_same_call(self, real_db, tmp_path):
        """Repair collapses multiplicity only. If the survivor equals the
        commit being swept and the entity is still provisional, case 1 runs
        on the repaired value and confirms it -- without repair it would
        have hit case 2's fail-safe skip."""
        import mcp_server, frontier_registry
        repo = self._repo_with_evolving_function(tmp_path)
        pos_map, linearization = self._pos_map(repo)
        h0, h2 = f":commit/{linearization[0][:12]}", f":commit/{linearization[2][:12]}"

        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        # Provisional at h0 (marker created), then a stray second value at h2.
        mcp_server._entity_introduced_by_set_provisional(real_db, fn_ident, h0, "2026-01-01T00:00:00Z")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by {h2}]]", "2026-01-03T00:00:00Z")
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True

        h0_hash = linearization[0]
        skipped = mcp_server._correction_sweep_apply(
            real_db, h0_hash, "2026-01-01T00:00:00Z", self._extract(repo, h0_hash),
            pos_by_commit_ident=pos_map,
        )

        assert mcp_server._entity_introduced_by_values_query(real_db, fn_ident) == [h0]
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is False
        assert skipped == 0, "repaired then confirmed -- nothing left provisional"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepApply -v -k "repair"`
Expected: FAIL — `TypeError: _correction_sweep_apply() got an unexpected keyword argument 'pos_by_commit_ident'` for three of them; `test_repair_is_inert_without_a_position_map` passes already (it asserts today's behaviour).

- [ ] **Step 3: Implement the signature and the collapse**

Append the parameter to `_correction_sweep_apply` at `mcp_server.py:8874-8881` — **last, after `update_watermark`**, because the Stage B driver passes seven positional arguments:

```python
def _correction_sweep_apply(
    db: Any,
    commit_hash: str,
    commit_ts_iso: str,
    file_results: List[tuple],
    index_con: Optional[Any] = None,
    skipped_so_far: int = 0,
    update_watermark: bool = True,
    pos_by_commit_ident: Optional[Dict[str, int]] = None,
) -> int:
```

Add this paragraph to the docstring, immediately before the `update_watermark=False` paragraph:

```
    pos_by_commit_ident (#235) enables the repair of graphs that already
    carry two live :introduced-by facts for one entity -- corruption an
    earlier version of _forward_apply created and which nothing converges,
    since every later reader sees one value, retracts it, and asserts a
    fresh one. When supplied, an ident holding 2+ values is collapsed to
    the one at the LOWEST linearization position before the case 1/2/3
    branching below runs; the survivor is then handled exactly as a
    single-valued entity would be. Repair collapses multiplicity only, it
    never confirms.

    Earliest-by-position is the right survivor because the reverse stream's
    guess is a sighting at or above the true introduction --
    _entity_introduced_by_set_provisional_batch's monotonicity rule already
    encodes that direction -- while the spurious second mint landed at the
    true introduction itself.

    Positions are required rather than derivable from arrival order: the
    second mint happens in the FORWARD region, which this sweep never
    visits, so it meets these entities at commits that are neither of their
    two values. A value absent from the map sorts last, so an unrecognised
    commit ident can never win and can never raise. Left None (the default)
    the repair is inert and every caller keeps the pre-#235 fail-safe.
    Best-effort: an entity this sweep never visits is not repaired.
```

Then, in the per-ident loop, replace `mcp_server.py:8963-8965` — the two lines that read the values, currently:

```python
        for ident in candidate_idents:
            raw = _db_execute(db, f"(query [:find ?c :where [{ident} :introduced-by ?c]])")
            introduced_by_values = {row[0] for row in json.loads(raw).get("results", [])}
```

with:

```python
        for ident in candidate_idents:
            introduced_by_values = set(_entity_introduced_by_values_query(db, ident))

            # #235 repair: collapse a corrupted multi-valued entity BEFORE the
            # case branching below, so cases 1/2/3 always see a well-formed
            # entity and need no multi-value handling of their own.
            if pos_by_commit_ident is not None and len(introduced_by_values) > 1:
                survivor = min(
                    sorted(introduced_by_values),
                    key=lambda c: pos_by_commit_ident.get(c, len(pos_by_commit_ident)),
                )
                doomed = sorted(introduced_by_values - {survivor})
                _retract(
                    db,
                    "[" + " ".join(f"[{ident} :introduced-by {c}]" for c in doomed) + "]",
                    index_con=index_con,
                )
                print(
                    f"[_correction_sweep] repaired {ident}: kept {survivor}, "
                    f"retracted {doomed} (#235)",
                    file=sys.stderr,
                )
                introduced_by_values = {survivor}
```

`sorted(...)` inside `min` makes the choice deterministic when two values tie on position (only reachable if both are absent from the map).

- [ ] **Step 4: Run the new tests**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepApply -v`
Expected: all pass, including the pre-existing tests in that class — the collapse is inert for them because none passes `pos_by_commit_ident`.

- [ ] **Step 5: Wire the two callers**

In `_correction_sweep_claim_and_process` (`mcp_server.py:9046-9055`), add the keyword last in its signature:

```python
    skipped_so_far: int = 0,
    pos_by_commit_ident: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[str, int]]:
```

and forward it in its call at `mcp_server.py:9078-9081`:

```python
    skipped_events = _correction_sweep_apply(
        db, commit_hash, commit_ts_iso, file_results,
        index_con=index_con, skipped_so_far=skipped_so_far,
        pos_by_commit_ident=pos_by_commit_ident,
    )
```

In `_run_ingestion`'s Stage B loop, build the map once *before* the `while` loop that contains the `_correction_sweep_select_position` call at `mcp_server.py:9558`, alongside the other per-run maps:

```python
                        # #235: the sweep's repair path needs linearization
                        # positions to pick which of a corrupted entity's
                        # :introduced-by values survives. Same construction
                        # _reverse_apply uses (mcp_server.py:7896).
                        sweep_pos_by_commit_ident = {
                            f":commit/{h[:12]}": i
                            for i, (h, _t, _a, _s) in enumerate(commit_metadata)
                        }
```

and extend the positional `run_in_executor` call at `mcp_server.py:9573-9577` with the eighth argument:

```python
                                skipped += await loop.run_in_executor(
                                    write_executor, _correction_sweep_apply,
                                    db, sweep_hash, sweep_ts, sweep_files, index_con, skipped,
                                    False, sweep_pos_by_commit_ident,
                                )
```

- [ ] **Step 6: Write the end-to-end repair test**

Add to `TestNoDuplicateIntroducedByAfterFullIngest` (created in Task 2):

```python
    @pytest.mark.asyncio
    async def test_a_pre_corrupted_graph_is_repaired_by_the_next_ingest(
        self, tmp_path, monkeypatch,
    ):
        """Fix-forward alone leaves graphs already in the field broken.
        Corrupt an entity by hand after a clean ingest, then re-ingest: the
        Stage B sweep must collapse it to the lower-position value."""
        import mcp_server
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path)
        graph_path = tmp_path / "memory.graph"

        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:1")
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph_path))
        mcp_server._db = None
        self._reset_progress()
        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None

        import frontier_registry
        linearization = frontier_registry.build_linearization(str(repo))
        fn_ident = mcp_server._code_ident("function", "mod.py", "stay_0")
        late = f":commit/{linearization[-1][:12]}"

        db = MiniGrafDb.open(str(graph_path))
        try:
            before = mcp_server._entity_introduced_by_values_query(db, fn_ident)
            assert len(before) == 1, before
            mcp_server._transact(db, f"[[{fn_ident} :introduced-by {late}]]", "2026-01-09T00:00:00Z")
            assert len(mcp_server._entity_introduced_by_values_query(db, fn_ident)) == 2
        finally:
            db = None
        mcp_server._db = None

        # Re-ingest the same graph. Stage B sweeps and repairs.
        self._reset_progress()
        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None

        db = MiniGrafDb.open(str(graph_path))
        try:
            after = mcp_server._entity_introduced_by_values_query(db, fn_ident)
        finally:
            db = None
        assert after == before, f"repair must keep the original value; got {after}"
```

If a completed graph's second `_run_ingestion` sweeps nothing (the sweep watermark already covers the history, so `_correction_sweep_select_position` returns None), this test cannot pass as written. In that case, do **not** weaken the assertion: instead corrupt the graph after an *interrupted* Stage A — copy the `_ingest_interrupted` helper from `TestMultiStreamParityWithForwardOnly` (`tests/test_mcp_server.py:17679`), which stops the run with the reverse stream's claims persisted and Stage B still pending, then let the resuming run sweep. Record whichever path you took in the commit message.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Repair two-value :introduced-by entities in the correction sweep (#235)

Dropping the prefilter stops new corruption but leaves graphs already in
the field broken, with no recovery short of re-ingesting from scratch.
_correction_sweep_apply now takes an optional pos_by_commit_ident and,
when given it, collapses an entity's 2+ :introduced-by values to the one
at the lowest linearization position before its case 1/2/3 branching
runs. Repair collapses multiplicity only; it never confirms.

Positions are needed rather than the ascending sweep's arrival order: the
spurious mint lands in the forward region, which Stage B never sweeps, so
the sweep meets these entities at commits that are neither of their
values. The parameter is last in the signature because the Stage B driver
passes seven positional arguments through run_in_executor.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Measure the at-scale cost and record it

The fix moves a graph point query onto every candidate ident of every "A"/"M" file in the forward walk — the read class #239 measures at 33.6% of ingestion wall clock. Nothing in the unit suite shows that. **Correctness is not gated on the number**; the measurement exists so #239 starts from a measured baseline and so a regression is attributed here rather than discovered later.

**Files:**
- Modify: the benchmark log under `evals/at_scale/results/` (recent commits `ca1718a` "Append the 20260803T095104Z run to the benchmark log" and `c076cc1` "Interpret the … entry in the log itself" show the append-and-interpret convention — read both with `git show`)

**Interfaces:**
- Consumes: the merged behaviour of Tasks 1–3.
- Produces: a benchmark log entry and a PR line quoting total ingestion seconds against #236's 1,600.55 s.

- [ ] **Step 1: Read the benchmark's own instructions**

Run: `cat evals/at_scale/benchmark.md`
Do not guess invocation or environment — that file is authoritative.

- [ ] **Step 2: Run the benchmark**

Run: `python evals/at_scale/run_ingestion_benchmark.py`
Expected: ~85 minutes. Run it in the background and check back rather than blocking.

- [ ] **Step 3: Append the entry and interpret it in the log**

Follow the format of the existing entries. State the total ingestion seconds, the delta against 1,600.55 s (#236's post-fix figure) and the 78.87 s forward-only baseline, and attribute any regression to the added `_lineage_is_provisional` call per forward candidate. If the query-correctness tier reports counts, note whether any entity still holds two `:introduced-by`.

- [ ] **Step 4: Commit**

```bash
git add evals/at_scale/
git commit -m "Record the post-#235 at-scale ingestion result

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Open the PR**

```bash
git push -u origin fix-235-two-value-introduced-by
gh pr create --title "Stop and repair two-value :introduced-by lineage (#235)" --body "..."
```

The body must reference #235 **without** a closing keyword — negated forms (`does not close #235`) close it too. Include the benchmark delta. `master` requires an approving review on top of green CI; ask before using `--admin` to bypass.

---

## Notes for the implementer

- `docs/testing-conventions.md` is short and worth reading before writing any test.
- `SKILL.md` documents user-facing query syntax and tool surface. This change adds no tool, no attribute and no query form, so no `SKILL.md` update is expected — confirm that rather than assuming it.
- `mcp` is pinned `<2.0.0` in `pyproject.toml` on purpose. Do not raise it.
- If a test needs a graph that survives across `MiniGrafDb.open()` calls, use a real file-backed DB, not the `real_db` fixture — `open_in_memory()` hands back a fresh store on every open.
