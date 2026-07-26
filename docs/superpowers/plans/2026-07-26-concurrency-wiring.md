# Concurrency Wiring (#222 phase 2d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire #222's frontier allocator, reverse-bulk-fill walk and correction sweep into `_run_ingestion` so a real ingest interleaves a forward-truth stream with a recent-first reverse stream, then sweeps the reverse region to authoritative.

**Architecture:** `_run_ingestion` becomes a three-stage driver. Stage A is one coroutine over a single tagged prefetch pipeline — `submit_next()` alternates `allocator.claim_low()` / `claim_high()` by a configurable ratio, submits every `_extract_commit` to the existing `ProcessPoolExecutor`, and dispatches each drained entry by tag to `_forward_apply` or `_reverse_apply` on the existing single-worker `write_executor`. Stage B drives phase 2c's three sweep pieces asynchronously on their correct executors. Stage C is the existing tag/last-run bookkeeping.

**Tech Stack:** Python 3, asyncio, `concurrent.futures` (ProcessPoolExecutor + single-worker ThreadPoolExecutor), `minigraf` (Rust-backed bi-temporal graph DB), tree-sitter, pytest.

**Spec:** `docs/superpowers/specs/2026-07-26-concurrency-wiring-design.md` — read it before starting. Every task below implements a section of it.

## Global Constraints

- **Branch:** `design-222-phase2d-concurrency-wiring` (already created, spec already committed as `9439908`). Work in place on this branch — do NOT create a git worktree.
- **Never write `Fix #222`, `Fixes #222`, `Closes #222` or `Resolves #222` in any commit message.** GitHub treats a closing keyword in *any* branch commit as an auto-close on merge, regardless of PR body. This has already bitten this project twice. Use `#222 phase 2d: ...` phrasing.
- **Tests are real-backend only** per `docs/testing-conventions.md`: real `MiniGrafDb` via the `real_db` fixture (in-memory) or a real file-backed `MiniGrafDb.open()` against `tmp_path` when state must survive across opens. Never `MagicMock` the DB. Never assert on mock call arguments — always re-query and assert on persisted facts.
- **All new test classes go in `tests/test_mcp_server.py`**, following the existing class-per-concern idiom (see `TestReverseFillClaimAndProcess` at line 13954 for the fixture pattern).
- **Never call `handle_minigraf_transact` / `handle_minigraf_retract`** from ingestion code. Use internal `_transact` / `_retract`. The public handlers' schema gate rejects `:type/lineage-marker`, `:type/candidate-diff` and `:type/ingest-interval`, all deliberately unregistered in `MINIGRAF_SCHEMA`.
- **EAVT collision rule:** minigraf's pending index omits value bytes, so facts sharing `(entity, attribute, valid_from)` in ONE `_transact` collapse to the last. `:contains`, `:depends-on` and `:parent` must be transacted **one triple per call**, on the retract side too. Facts differing in entity do not collide.
- **Positions, never timestamps**, for any ordering comparison. Committer dates are non-monotonic in topological order (this repo's own history has a six-day inversion).
- **Run the full suite before every commit:** `python -m pytest tests/test_mcp_server.py tests/test_frontier_registry.py -q`. Baseline on this branch is **968 passing, 0 failing**. The rule for every task is: **zero failures, and the passing count goes up by exactly the number of tests that task added — never down.** Confirm the baseline yourself before starting Task 1 rather than trusting this number; if it differs, use what you measured. Absolute counts are deliberately not restated per-task, because a task that adds one extra test case would otherwise read as a failure.

---

### Task 1: Stream ratio configuration

**Files:**
- Modify: `mcp_server.py` (add near the other ingestion env-var reads, around line 8086)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing
- Produces: `_parse_stream_ratio(raw: Optional[str]) -> Tuple[int, int]` returning `(forward_per_round, reverse_per_round)`; module constant `_DEFAULT_STREAM_RATIO = (1, 1)`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
class TestParseStreamRatio:
    def test_default_when_unset(self):
        import mcp_server
        assert mcp_server._parse_stream_ratio(None) == (1, 1)

    def test_parses_explicit_ratios(self):
        import mcp_server
        assert mcp_server._parse_stream_ratio("1:1") == (1, 1)
        assert mcp_server._parse_stream_ratio("1:3") == (1, 3)
        assert mcp_server._parse_stream_ratio("3:1") == (3, 1)
        assert mcp_server._parse_stream_ratio(" 2 : 5 ") == (2, 5)

    def test_malformed_falls_back_to_default_and_logs_once(self, capsys):
        import mcp_server
        for bad in ("x", "", "1", "1:2:3", "0:1", "1:0", "-1:2", "a:b", "1.5:2"):
            assert mcp_server._parse_stream_ratio(bad) == (1, 1), bad
        err = capsys.readouterr().err
        # One line per bad value, and each names the offending value.
        assert err.count("MINIGRAF_INGEST_STREAM_RATIO") == 9

    def test_does_not_raise_on_any_input(self):
        """A bad env var must never be the reason a repo never ingests --
        this runs inside a background coroutine with no user in the loop."""
        import mcp_server
        assert mcp_server._parse_stream_ratio("\x00\xff") == (1, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestParseStreamRatio -q`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_parse_stream_ratio'`

- [ ] **Step 3: Write the implementation**

Add to `mcp_server.py`, immediately above `_run_ingestion`:

```python
_DEFAULT_STREAM_RATIO = (1, 1)


def _parse_stream_ratio(raw: Optional[str]) -> Tuple[int, int]:
    """Parse MINIGRAF_INGEST_STREAM_RATIO ("F:R") into (forward_per_round,
    reverse_per_round), falling back to 1:1 on anything malformed.

    Never raises. This is read inside the background ingestion coroutine,
    where a bad env var must degrade to the default rather than become the
    reason a repository never ingests at all.

    The ratio trades total work against how fast recent history becomes
    usable: a commit the forward stream claims is parsed once and is
    authoritative immediately, while a commit the reverse stream claims is
    parsed twice (reverse walk, then the correction sweep's own
    _extract_commit call). Total parse cost is N * (1 + reverse_fraction).
    """
    if raw is None:
        return _DEFAULT_STREAM_RATIO
    try:
        forward_str, reverse_str = raw.split(":")
        forward, reverse = int(forward_str.strip()), int(reverse_str.strip())
        if forward < 1 or reverse < 1:
            raise ValueError("both sides must be >= 1")
        return forward, reverse
    except Exception as e:
        print(
            f"[_run_ingestion] ignoring malformed MINIGRAF_INGEST_STREAM_RATIO "
            f"{raw!r} ({e}); using {_DEFAULT_STREAM_RATIO[0]}:{_DEFAULT_STREAM_RATIO[1]}",
            file=sys.stderr,
        )
        return _DEFAULT_STREAM_RATIO
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestParseStreamRatio -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add #222 phase 2d: stream ratio configuration"
```

---

### Task 2: Preload the provisional ident set

**Files:**
- Modify: `mcp_server.py:6918-6946` (`_load_ingestion_preload_state`), and its single caller's unpack at `mcp_server.py:8061-8065`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_lineage_mark_provisional` (2a), `_LINEAGE_MARKER_ENTITY_TYPE`
- Produces: `_preload_provisional_idents(db: Any) -> Set[str]`; `_load_ingestion_preload_state` returns a **13**-tuple, with `provisional_idents` appended last

**Why this exists:** the forward walk decides "is this entity new" from the in-memory `entity_valid_from` dict, preloaded once at run start. On a **resumed** run, Stream 2's structural facts from the previous run are already in that dict, so `_build_code_triples` suppresses the introduction entirely and the entity keeps a wrong provisional `:introduced-by` forever. That failure is silent — there is no duplicate fact to notice. This set is what lets Task 5 detect it.

- [ ] **Step 1: Write the failing tests**

```python
class TestPreloadProvisionalIdents:
    def test_empty_when_no_markers(self, real_db):
        import mcp_server
        assert mcp_server._preload_provisional_idents(real_db) == set()

    def test_returns_every_marked_ident(self, real_db):
        import mcp_server
        mcp_server._lineage_mark_provisional(real_db, ":code/fn-a", "2026-01-01T00:00:00Z")
        mcp_server._lineage_mark_provisional(real_db, ":code/fn-b", "2026-01-01T00:00:00Z")
        assert mcp_server._preload_provisional_idents(real_db) == {":code/fn-a", ":code/fn-b"}

    def test_confirmed_idents_drop_out(self, real_db):
        """_lineage_confirm retracts the whole companion entity rather than
        flipping :status, so the set form must agree with the per-ident
        _lineage_is_provisional check the reconciliation relies on."""
        import mcp_server
        mcp_server._lineage_mark_provisional(real_db, ":code/fn-a", "2026-01-01T00:00:00Z")
        mcp_server._lineage_mark_provisional(real_db, ":code/fn-b", "2026-01-01T00:00:00Z")
        mcp_server._lineage_confirm(real_db, ":code/fn-a")
        assert mcp_server._preload_provisional_idents(real_db) == {":code/fn-b"}
        assert mcp_server._lineage_is_provisional(real_db, ":code/fn-a") is False

    def test_load_ingestion_preload_state_returns_it_last(self, real_db, tmp_path):
        import mcp_server
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        mcp_server._lineage_mark_provisional(real_db, ":code/fn-a", "2026-01-01T00:00:00Z")
        result = mcp_server._load_ingestion_preload_state(str(repo))
        assert len(result) == 13
        assert result[-1] == {":code/fn-a"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestPreloadProvisionalIdents -q`
Expected: FAIL with `AttributeError: ... '_preload_provisional_idents'`

- [ ] **Step 3: Write the implementation**

Add to `mcp_server.py` next to the other `_preload_*` functions:

```python
def _preload_provisional_idents(db: Any) -> Set[str]:
    """Every tracked ident that currently has a :type/lineage-marker
    companion entity, i.e. whose :introduced-by is a provisional guess.

    No :status clause is needed: the marker exists ONLY while the entity is
    provisional -- _lineage_confirm retracts the whole companion entity
    rather than flipping its :status -- so existence is the test, exactly as
    _lineage_is_provisional does per-ident. Keeping the two queries the same
    shape is deliberate: this set is consulted where a per-ident check is
    too expensive, and the two must never disagree.
    """
    raw = _db_execute(
        db,
        f"(query [:find ?e :where [?m :entity-type {_LINEAGE_MARKER_ENTITY_TYPE}] [?m :entity ?e]])",
    )
    return {row[0] for row in json.loads(raw).get("results", [])}
```

In `_load_ingestion_preload_state`, add before the `return`:

```python
    provisional_idents = _preload_provisional_idents(db)
```

and append `provisional_idents,` as the last element of the returned tuple.

- [ ] **Step 4: Widen the caller's unpack**

In `_run_ingestion` at `mcp_server.py:8061-8065`, add `provisional_idents,` as the last name in the destructuring assignment. It is unused until Task 5 — add `# consumed by _forward_apply (Task 5)` so the unused name is not mistaken for a leftover.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestPreloadProvisionalIdents -q`
Expected: 4 passed

Run: `python -m pytest tests/test_mcp_server.py tests/test_frontier_registry.py -q`
Expected: zero failures, count up by 4. A `ValueError: too many values to unpack` here means Step 4 was missed.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add #222 phase 2d: preload the provisional ident set"
```

---

### Task 3: Forward-introduction reconciliation helper

**Files:**
- Modify: `mcp_server.py` (add immediately after `_entity_introduced_by_set_provisional`, around line 5320)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_entity_introduced_by_query`, `_lineage_confirm`, `_candidate_diff_clear`, `_transact`, `_retract` (all 2a/2b)
- Produces:
  ```python
  def _forward_reconcile_provisional(
      db: Any,
      entity_ident: str,
      structural_triples: List[str],
      true_commit_ts_iso: str,
      ts_by_commit_ident: Dict[str, str],
      index_con: Optional[Any] = None,
  ) -> Optional[str]:
  ```
  Returns the superseded (guessed) commit ident it reconciled away, or `None` if the entity was not provisional.

**Why:** this is the correctness core of 2d. Without it, an entity alive at `HEAD` but introduced low in history ends up with **two** `:introduced-by` values, which `_correction_sweep_apply` reads as an ambiguous count, hits its case-2 fail-safe on, and leaves provisional forever. It also closes the "wrong provisional guess" case 2c proved it could not handle inside its own walk and left explicitly unowned.

- [ ] **Step 1: Write the failing tests**

```python
class TestForwardReconcileProvisional:
    def _seed_provisional(self, real_db):
        """An entity Stream 2 discovered at a late commit and guessed wrong."""
        import mcp_server
        guess_ident = ":commit/bbbbbbbbbbbb"
        structural = [
            ":code/fn-login :entity-type :type/function",
            ':code/fn-login :name "login"',
            ":module/auth :contains :code/fn-login",
        ]
        structural_triples = [f"[{t}]" for t in structural]
        mcp_server._transact(
            real_db, "[" + " ".join(f"[{t}]" for t in structural if ":contains" not in t) + "]",
            "2026-06-01T00:00:00Z",
        )
        mcp_server._transact(
            real_db, "[[:module/auth :contains :code/fn-login]]", "2026-06-01T00:00:00Z",
        )
        mcp_server._transact(
            real_db, f"[[:code/fn-login :introduced-by {guess_ident}]]", "2026-06-01T00:00:00Z",
        )
        mcp_server._lineage_mark_provisional(real_db, ":code/fn-login", "2026-06-01T00:00:00Z")
        mcp_server._candidate_diff_persist(
            real_db, "bbbbbbbbbbbb" + "0" * 28, ":code/fn-login", "hash-b", "2026-06-01T00:00:00Z",
        )
        return guess_ident, structural_triples

    def test_returns_none_and_writes_nothing_when_not_provisional(self, real_db):
        import mcp_server
        mcp_server._transact(
            real_db, "[[:code/fn-x :introduced-by :commit/aaaaaaaaaaaa]]", "2026-01-01T00:00:00Z",
        )
        result = mcp_server._forward_reconcile_provisional(
            real_db, ":code/fn-x", [], "2026-01-01T00:00:00Z", {},
        )
        assert result is None
        assert mcp_server._entity_introduced_by_query(real_db, ":code/fn-x") == ":commit/aaaaaaaaaaaa"

    def test_retracts_the_provisional_guess(self, real_db):
        import mcp_server
        guess_ident, structural = self._seed_provisional(real_db)
        mcp_server._forward_reconcile_provisional(
            real_db, ":code/fn-login", structural, "2026-01-01T00:00:00Z",
            {guess_ident: "2026-06-01T00:00:00Z"},
        )
        # No :introduced-by left at all -- the caller writes the authoritative
        # one immediately after, via the normal forward emission path.
        assert mcp_server._entity_introduced_by_query(real_db, ":code/fn-login") is None

    def test_confirms_the_marker_and_clears_the_candidate_diff(self, real_db):
        import mcp_server
        guess_ident, structural = self._seed_provisional(real_db)
        returned = mcp_server._forward_reconcile_provisional(
            real_db, ":code/fn-login", structural, "2026-01-01T00:00:00Z",
            {guess_ident: "2026-06-01T00:00:00Z"},
        )
        assert returned == guess_ident
        assert mcp_server._lineage_is_provisional(real_db, ":code/fn-login") is False
        assert mcp_server._candidate_diff_read(
            real_db, "bbbbbbbbbbbb" + "0" * 28, ":code/fn-login",
        ) is None

    def test_writes_modified_in_at_the_guess_commits_own_timestamp(self, real_db):
        """The guess commit is now known to be a genuine modification rather
        than the introduction. Dating the edge at the TRUE introduction's
        timestamp would assert a fact valid before it was true."""
        import mcp_server
        guess_ident, structural = self._seed_provisional(real_db)
        mcp_server._forward_reconcile_provisional(
            real_db, ":code/fn-login", structural, "2026-01-01T00:00:00Z",
            {guess_ident: "2026-06-01T00:00:00Z"},
        )
        raw = mcp_server._db_execute(
            real_db,
            '(query [:find ?c :valid-at "2026-06-02T00:00:00Z" '
            ':where [:code/fn-login :modified-in ?c]])',
        )
        assert [row[0] for row in json.loads(raw)["results"]] == [guess_ident]
        # Not yet true the day BEFORE the guess commit.
        raw_before = mcp_server._db_execute(
            real_db,
            '(query [:find ?c :valid-at "2026-05-31T00:00:00Z" '
            ':where [:code/fn-login :modified-in ?c]])',
        )
        assert json.loads(raw_before)["results"] == []

    def test_re_dates_structural_facts_to_the_true_introduction(self, real_db):
        """Otherwise there is a valid-time window where :introduced-by is live
        for an entity with no type, name or file, so an :as-of query at the
        true introduction reports the entity as nonexistent."""
        import mcp_server
        guess_ident, structural = self._seed_provisional(real_db)
        mcp_server._forward_reconcile_provisional(
            real_db, ":code/fn-login", structural, "2026-01-01T00:00:00Z",
            {guess_ident: "2026-06-01T00:00:00Z"},
        )
        raw = mcp_server._db_execute(
            real_db,
            '(query [:find ?n :valid-at "2026-02-01T00:00:00Z" '
            ':where [:code/fn-login :name ?n]])',
        )
        assert [row[0] for row in json.loads(raw)["results"]] == ["login"]

    def test_contains_edge_survives_re_dating(self, real_db):
        """:contains shares (entity, attribute, valid_from) with any sibling
        edge, so it must be retracted and re-transacted one triple per call
        -- batching silently keeps only the last (#222 phase 2b1)."""
        import mcp_server
        guess_ident, structural = self._seed_provisional(real_db)
        mcp_server._forward_reconcile_provisional(
            real_db, ":code/fn-login", structural, "2026-01-01T00:00:00Z",
            {guess_ident: "2026-06-01T00:00:00Z"},
        )
        raw = mcp_server._db_execute(
            real_db,
            '(query [:find ?c :valid-at "2026-02-01T00:00:00Z" '
            ':where [:module/auth :contains ?c]])',
        )
        assert [row[0] for row in json.loads(raw)["results"]] == [":code/fn-login"]

    def test_skips_modified_in_when_guess_timestamp_unknown(self, real_db, capsys):
        """Falling back to the true introduction's timestamp would back-date
        the edge to before the modification it describes. Skip and say so."""
        import mcp_server
        guess_ident, structural = self._seed_provisional(real_db)
        mcp_server._forward_reconcile_provisional(
            real_db, ":code/fn-login", structural, "2026-01-01T00:00:00Z", {},
        )
        raw = mcp_server._db_execute(
            real_db, "(query [:find ?c :where [:code/fn-login :modified-in ?c]])",
        )
        assert json.loads(raw)["results"] == []
        assert "no timestamp in commit_metadata" in capsys.readouterr().err
        # The rest of the reconciliation still happened.
        assert mcp_server._lineage_is_provisional(real_db, ":code/fn-login") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestForwardReconcileProvisional -q`
Expected: FAIL with `AttributeError: ... '_forward_reconcile_provisional'`

- [ ] **Step 3: Write the implementation**

```python
def _forward_reconcile_provisional(
    db: Any,
    entity_ident: str,
    structural_triples: List[str],
    true_commit_ts_iso: str,
    ts_by_commit_ident: Dict[str, str],
    index_con: Optional[Any] = None,
) -> Optional[str]:
    """#222 phase 2d: the forward walk has reached entity_ident's TRUE
    introduction and found a provisional guess left by Stream 2. Clear the
    guess so the caller's normal authoritative emission is the only
    :introduced-by fact that survives.

    Returns the superseded guess's commit ident, or None if entity_ident was
    not provisional (in which case nothing is written at all).

    Deliberately does NOT write the authoritative :introduced-by itself --
    the caller's ordinary _build_code_triples output does that, so there is
    exactly one code path that mints an authoritative introduction and this
    function cannot drift from it.

    Mirrors the supersede path in _reverse_fill_claim_and_process: the same
    re-dating of structural facts, the same one-transact-per-:contains
    splitting, and the same refusal to back-date a :modified-in edge whose
    commit timestamp is unknown.
    """
    guess_ident = _entity_introduced_by_query(db, entity_ident)
    if not _lineage_is_provisional(db, entity_ident):
        return None

    # 1. Drop the guess. The caller writes the real one immediately after.
    if guess_ident is not None:
        _retract(db, f"[[{entity_ident} :introduced-by {guess_ident}]]", index_con=index_con)

    # 2. Re-date structural facts to the true (earlier) introduction. Stream 2
    # wrote them at the timestamp of the commit where it first SIGHTED the
    # entity, leaving a valid-time window where :introduced-by is live for an
    # entity with no type, name or file. _retract targets live rows
    # regardless of their original valid_from, so this is a straight
    # re-assert at the earlier timestamp. :contains goes one per call on BOTH
    # sides (EAVT collision, #222 phase 2b1).
    structural = [t for t in structural_triples if ":introduced-by" not in t]
    structural_contains = [t for t in structural if ":contains" in t]
    structural_other = [t for t in structural if ":contains" not in t]
    if structural_other:
        _retract(db, "[" + " ".join(structural_other) + "]", index_con=index_con)
        _transact(db, "[" + " ".join(structural_other) + "]", true_commit_ts_iso, index_con=index_con)
    for structural_triple in structural_contains:
        _retract(db, "[" + structural_triple + "]", index_con=index_con)
        _transact(db, "[" + structural_triple + "]", true_commit_ts_iso, index_con=index_con)

    # 3. The entity's lineage is authoritative from here on.
    _lineage_confirm(db, entity_ident, index_con=index_con)

    if guess_ident is None:
        return None

    # 4. The candidate-diff record is stale the moment the guess moves off it,
    # and this is the cheapest place to drop it -- guess_ident is right here.
    _candidate_diff_clear(db, guess_ident[len(":commit/"):], entity_ident, index_con=index_con)

    # 5. The guess commit is now known to be a genuine modification rather
    # than the introduction, so it earns the :modified-in edge Stream 2
    # withheld from it. Dated at ITS OWN timestamp, not this commit's.
    #
    # This inherits 2b's documented over-assertion: #221's unchanged-body
    # narrowing cannot be re-checked against the guess commit's own diff,
    # because only that commit's own parse carries the data. That is already
    # owned -- the guess commit lies inside frontier-high's territory, so the
    # correction sweep visits it, finds exactly one :introduced-by (case 3),
    # and retracts this edge if its own parse says the body was unchanged.
    guess_ts = ts_by_commit_ident.get(guess_ident)
    if guess_ts is None:
        print(
            f"[_forward_reconcile_provisional] skipping retroactive :modified-in "
            f"for {entity_ident} at {guess_ident}: no timestamp in commit_metadata",
            file=sys.stderr,
        )
        return guess_ident
    _transact(
        db, f"[[{entity_ident} :modified-in {guess_ident}]]", guess_ts, index_con=index_con,
    )
    return guess_ident
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestForwardReconcileProvisional -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add #222 phase 2d: forward-introduction reconciliation helper"
```

---

### Task 4: Extract the per-commit forward body (pure move, no behaviour change)

**Files:**
- Modify: `mcp_server.py:8219-8592` (the `try:` block inside `while pending:`) → new `_forward_apply`; add `_ForwardWalkState` dataclass
- Test: existing suite must pass **unchanged** — this task adds no new tests

**Interfaces:**
- Consumes: everything the current inline body consumes
- Produces:
  ```python
  @dataclass
  class _ForwardWalkState:
      entity_valid_from: Dict[str, str]
      entity_descriptions: Dict[str, str]
      file_entities: Dict[str, list]
      file_deps: Dict[str, set]
      dep_valid_from: Dict[tuple, str]
      pinned_commit_state: Dict[str, tuple]
      field_class_ident: Dict[str, str]
      field_static_ident: Dict[str, bool]
      submodule_paths: Dict[str, str]
      unresolved_dep_idents: Dict[str, str]

  def _forward_apply(
      db: Any,
      repo_path: str,
      state: "_ForwardWalkState",
      commit: Tuple[str, str, str, str],          # (hash, ts_iso, author, subject)
      extracted: Tuple[list, list, dict, list],   # _extract_commit's 4-tuple
      index_con: Optional[Any] = None,
  ) -> None:
  ```

**This is the single largest implementation risk in the phase.** It is a mechanical move, and it must stay one: do not fix, tidy, or restructure anything inside the moved code in this task. The whole point of doing it separately is that the existing 968 tests are the proof it was faithful.

- [ ] **Step 1: Add the dataclass and construct it at the call site**

Add `_ForwardWalkState` (fields exactly as above, in the same order `_load_ingestion_preload_state` returns them) immediately above `_run_ingestion`. In `_run_ingestion`, after the preload unpack, build:

```python
        state = _ForwardWalkState(
            entity_valid_from=entity_valid_from,
            entity_descriptions=entity_descriptions,
            file_entities=file_entities,
            file_deps=file_deps,
            dep_valid_from=dep_valid_from,
            pinned_commit_state=pinned_commit_state,
            field_class_ident=field_class_ident,
            field_static_ident=field_static_ident,
            submodule_paths=submodule_paths,
            unresolved_dep_idents=unresolved_dep_idents,
        )
```

- [ ] **Step 2: Move the body**

Cut `mcp_server.py:8220-8592` — everything inside the `try:` at 8219, from `add_triples: List[str] = [` through the `_commit_index_writer_safe(index_con)` line — into the new `_forward_apply`, placed immediately after `_reverse_bulk_fill_walk`. Apply exactly these substitutions and no others:

| In the moved code | Becomes |
|---|---|
| `await loop.run_in_executor(write_executor, F, *args)` | `F(*args)` — the whole body now runs as ONE submission to `write_executor` instead of ~10 |
| bare `entity_valid_from`, `entity_descriptions`, `file_entities`, `file_deps`, `dep_valid_from`, `pinned_commit_state`, `field_class_ident`, `field_static_ident`, `submodule_paths`, `unresolved_dep_idents` | `state.<same name>` |
| `commit_hash`, `commit_ts_iso`, `author`, `subject` | unpacked from `commit` at the top of `_forward_apply` |
| `extracted_files`, `gitlink_changes`, `gitmodules_map`, `renamed_pairs` | unpacked from `extracted` at the top |
| `commit_ident` | recomputed at the top: `commit_ident = f":commit/{commit_hash[:12]}"` |

Fusing the ~10 executor submissions into one is **better** for the event loop, not worse: it awaits one future instead of ten round-trips, while the write thread stays busy for the same total time.

- [ ] **Step 3: Call it from the loop**

Replace the excised block at the call site with:

```python
                    db = await _ensure_db_async()
                    try:
                        await loop.run_in_executor(
                            write_executor, _forward_apply, db, repo_path, state,
                            (commit_hash, commit_ts_iso, author, subject),
                            (extracted_files, gitlink_changes, gitmodules_map, renamed_pairs),
                            index_con,
                        )
                    except Exception as e:
```

leaving the existing `except`/`finally` handlers at 8594-8612 exactly as they are.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py tests/test_frontier_registry.py -q`
Expected: zero failures and **exactly the same count as after Task 3** — this task adds no tests, and the existing suite is the only proof the move was faithful. **Any failure here is an extraction error, not a test problem.** Diff the moved code against `git show HEAD~1:mcp_server.py` rather than adjusting a test.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py
git commit -m "Add #222 phase 2d: extract _forward_apply from _run_ingestion

Pure mechanical move of the per-commit write section, plus a
_ForwardWalkState dataclass for the ten mutable preload dicts. No
behaviour change -- the existing suite is the proof. The ten separate
write_executor submissions become one, which is fewer event-loop
round-trips for the same write-thread time."
```

---

### Task 5: Wire reconciliation into the forward walk

**Files:**
- Modify: `mcp_server.py` (`_ForwardWalkState`, `_forward_apply`, and the `_run_ingestion` construction site)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_forward_reconcile_provisional` (Task 3), `_preload_provisional_idents` (Task 2), `_ForwardWalkState`/`_forward_apply` (Task 4)
- Produces: `_ForwardWalkState` gains `provisional_idents: Set[str]` and `ts_by_commit_ident: Dict[str, str]`; `_forward_apply` reconciles before emitting

- [ ] **Step 1: Write the failing test**

```python
class TestForwardApplyReconcilesProvisional:
    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def login():\n    return 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h0"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def login():\n    return 2\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h1"], cwd=repo, check=True, capture_output=True)
        return repo

    def test_forward_apply_supersedes_a_provisional_guess(self, real_db, tmp_path):
        """Stream 2 claimed h1 and guessed login() was introduced there. The
        forward walk then reaches h0, the TRUE introduction. Exactly one
        :introduced-by must survive, naming h0, with a confirmed marker."""
        import mcp_server, frontier_registry
        repo = self._repo(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")

        # Stream 2 claims the newest commit (h1) and guesses.
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True
        assert mcp_server._entity_introduced_by_query(real_db, fn_ident) == f":commit/{linearization[1][:12]}"

        # Stream 1 now reaches h0, the true introduction.
        state = mcp_server._ForwardWalkState(
            entity_valid_from={}, entity_descriptions={}, file_entities={},
            file_deps={}, dep_valid_from={}, pinned_commit_state={},
            field_class_ident={}, field_static_ident={}, submodule_paths={},
            unresolved_dep_idents={},
            provisional_idents=mcp_server._preload_provisional_idents(real_db),
            ts_by_commit_ident={f":commit/{h[:12]}": ts for h, ts, _a, _s in commit_metadata},
        )
        extracted = mcp_server._extract_commit(str(repo), linearization[0], ())
        mcp_server._forward_apply(
            real_db, str(repo), state, commit_metadata[0], extracted,
        )

        raw = mcp_server._db_execute(
            real_db, f"(query [:find ?c :where [{fn_ident} :introduced-by ?c]])",
        )
        values = {row[0] for row in json.loads(raw)["results"]}
        assert values == {f":commit/{linearization[0][:12]}"}, "exactly one, naming h0"
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is False
        # h1 is now a genuine modification.
        raw_mod = mcp_server._db_execute(
            real_db, f"(query [:find ?c :where [{fn_ident} :modified-in ?c]])",
        )
        assert f":commit/{linearization[1][:12]}" in {row[0] for row in json.loads(raw_mod)["results"]}

    def test_resumed_run_variant_is_not_suppressed_by_entity_valid_from(self, real_db, tmp_path):
        """The silent failure mode: on a RESUMED run, Stream 2's structural
        facts are already in the preloaded entity_valid_from, so
        _build_code_triples would suppress the introduction entirely and the
        wrong provisional guess would survive forever."""
        import mcp_server, frontier_registry
        repo = self._repo(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")

        # Simulate the resumed-run preload: entity_valid_from ALREADY knows
        # this entity, because Stream 2 wrote its structural facts last run.
        state = mcp_server._ForwardWalkState(
            entity_valid_from={fn_ident: "2026-06-01T00:00:00Z"},
            entity_descriptions={fn_ident: "login"}, file_entities={"auth.py": [fn_ident]},
            file_deps={}, dep_valid_from={}, pinned_commit_state={},
            field_class_ident={}, field_static_ident={}, submodule_paths={},
            unresolved_dep_idents={},
            provisional_idents={fn_ident},
            ts_by_commit_ident={f":commit/{h[:12]}": ts for h, ts, _a, _s in commit_metadata},
        )
        extracted = mcp_server._extract_commit(str(repo), linearization[0], ())
        mcp_server._forward_apply(
            real_db, str(repo), state, commit_metadata[0], extracted,
        )

        raw = mcp_server._db_execute(
            real_db, f"(query [:find ?c :where [{fn_ident} :introduced-by ?c]])",
        )
        assert {row[0] for row in json.loads(raw)["results"]} == {f":commit/{linearization[0][:12]}"}
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is False

    def test_without_provisional_idents_the_resumed_case_regresses(self, real_db, tmp_path):
        """Pins that provisional_idents is actually consulted: with it empty,
        the resumed-run case leaves the wrong guess in place. This is the
        direct regression test for the silent failure mode."""
        import mcp_server, frontier_registry
        repo = self._repo(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        state = mcp_server._ForwardWalkState(
            entity_valid_from={fn_ident: "2026-06-01T00:00:00Z"},
            entity_descriptions={fn_ident: "login"}, file_entities={"auth.py": [fn_ident]},
            file_deps={}, dep_valid_from={}, pinned_commit_state={},
            field_class_ident={}, field_static_ident={}, submodule_paths={},
            unresolved_dep_idents={},
            provisional_idents=set(),   # the bug
            ts_by_commit_ident={f":commit/{h[:12]}": ts for h, ts, _a, _s in commit_metadata},
        )
        extracted = mcp_server._extract_commit(str(repo), linearization[0], ())
        mcp_server._forward_apply(
            real_db, str(repo), state, commit_metadata[0], extracted,
        )
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True
        assert mcp_server._entity_introduced_by_query(real_db, fn_ident) == f":commit/{linearization[1][:12]}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestForwardApplyReconcilesProvisional -q`
Expected: FAIL — `_ForwardWalkState` has no `provisional_idents` field

- [ ] **Step 3: Widen the dataclass**

Add to `_ForwardWalkState`:

```python
    provisional_idents: Set[str] = field(default_factory=set)
    ts_by_commit_ident: Dict[str, str] = field(default_factory=dict)
```

(`from dataclasses import field` if not already imported.) Populate both in `_run_ingestion`: `provisional_idents=provisional_idents` from Task 2's unpack, and `ts_by_commit_ident={f":commit/{h[:12]}": ts for h, ts, _a, _s in commit_metadata}` — note this needs the **full-history** `commit_metadata`, which Task 8 introduces; until then build it from the existing `commits` list.

- [ ] **Step 4: Reconcile inside `_forward_apply`**

In the `else:  # A or M or R` branch, immediately **before** the `triples = _build_code_triples(...)` call, add:

```python
                # #222 phase 2d: an entity Stream 2 already introduced
                # provisionally is NOT authoritatively introduced, so the
                # forward walk must treat it as new. Popping it out of
                # entity_valid_from is what makes _build_code_triples's own
                # gate (entity_valid_from membership) agree -- deliberately
                # in preference to widening that function's signature, since
                # its gate means "is this the introduction" for a forward
                # walk and nothing else should depend on that meaning.
                #
                # It is also semantically right on its own terms: the
                # valid_from Stream 2 recorded is a wrong guess, and must
                # never be used as an orig_ts for a close.
                reconcilable = [
                    ident for ident in _forward_candidate_idents(precomputed)
                    if ident in state.provisional_idents
                ]
                for ident in reconcilable:
                    state.entity_valid_from.pop(ident, None)
                    _forward_reconcile_provisional(
                        db, ident,
                        _forward_structural_triples(precomputed, ident),
                        commit_ts_iso, state.ts_by_commit_ident, index_con=index_con,
                    )
                    state.provisional_idents.discard(ident)
```

Add the two small helpers next to `_forward_apply` (both mirror the candidate-ident and candidate-triple collection `_reverse_fill_claim_and_process` already does, so the three walks agree on what an entity's candidate set is):

```python
def _forward_candidate_idents(precomputed: Dict[str, Any]) -> List[str]:
    """Every entity ident a parsed file contributes -- module plus all four
    child categories. Same collection _reverse_fill_claim_and_process and
    _correction_sweep_apply perform; kept as one function so the three walks
    cannot drift on what an entity's candidate set is."""
    return (
        [precomputed["module_ident"]]
        + [ident for ident, _name, _t in precomputed["function_entries"]]
        + [ident for ident, _name, _t in precomputed["class_entries"]]
        + [ident for ident, _name, _t in precomputed["global_entries"]]
        + [ident for ident, _name, _t in precomputed["field_entries"]]
    )


def _forward_structural_triples(precomputed: Dict[str, Any], ident: str) -> List[str]:
    """ident's own candidate triples, for re-dating. A child's own list
    carries its [parent :contains child] edge, so re-dating a child re-dates
    its containment edge with it (#222 phase 2b1)."""
    if ident == precomputed["module_ident"]:
        return list(precomputed["module_candidate_triples"])
    for entries_key in ("function_entries", "class_entries", "global_entries", "field_entries"):
        for entry_ident, _entry_name, entry_triples in precomputed[entries_key]:
            if entry_ident == ident:
                return list(entry_triples)
    return []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestForwardApplyReconcilesProvisional -q`
Expected: 3 passed

Run: `python -m pytest tests/test_mcp_server.py tests/test_frontier_registry.py -q`
Expected: zero failures, count up by 3

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add #222 phase 2d: reconcile provisional lineage in the forward walk"
```

---

### Task 6: Split `_reverse_apply` out of `_reverse_fill_claim_and_process`

**Files:**
- Modify: `mcp_server.py:7323-7638` (`_reverse_fill_claim_and_process`)
- Test: existing `TestReverseFill*` classes must pass **unchanged**, plus one new composition test

**Interfaces:**
- Produces:
  ```python
  def _reverse_apply(
      db: Any,
      repo_path: str,
      linearization: List[str],
      commit_metadata: List[Tuple[str, str, str, str]],
      pos: int,
      file_results: List[tuple],
      index_con: Optional[Any] = None,
  ) -> str:
  ```
  Returns the applied commit's hash. `_reverse_fill_claim_and_process` keeps its signature and becomes `claim_high()` + `_extract_commit()` + `_reverse_apply()`.

**Why:** `_reverse_fill_claim_and_process` calls `_extract_commit` inside its own body. Submitted to `write_executor` — a *thread* — that tree-sitter parse holds the GIL for its whole duration, which is exactly the event-loop starvation #116 introduced the process pool to fix. 2c flagged this conflation and left it (out of scope); 2d cannot.

- [ ] **Step 1: Write the failing composition test**

```python
class TestReverseApplySplit:
    def test_split_pieces_compose_to_the_same_graph_as_the_wrapper(self, tmp_path):
        """Two independent graphs from one repo fixture -- the walk mutates
        the state a second run would start from, so this cannot be two
        passes over one graph."""
        import mcp_server, frontier_registry
        from minigraf import MiniGrafDb

        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "auth.py").write_text("def login():\n    return 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h0"], cwd=repo, check=True, capture_output=True)

        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)

        def snapshot(db):
            raw = mcp_server._db_execute(db, "(query [:find ?e ?a ?v :where [?e ?a ?v]])")
            return sorted(tuple(row) for row in json.loads(raw)["results"])

        db_a = MiniGrafDb.open(str(tmp_path / "a.graph"))
        alloc_a = mcp_server._frontier_load(db_a, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_fill_claim_and_process(
            db_a, str(repo), linearization, commit_metadata, alloc_a,
        )

        db_b = MiniGrafDb.open(str(tmp_path / "b.graph"))
        alloc_b = mcp_server._frontier_load(db_b, linearization, "2026-01-04T00:00:00Z")
        pos = alloc_b.claim_high()
        file_results, _g, _m, _r = mcp_server._extract_commit(str(repo), linearization[pos], ())
        applied = mcp_server._reverse_apply(
            db_b, str(repo), linearization, commit_metadata, pos, file_results,
        )
        assert applied == linearization[pos]
        assert snapshot(db_a) == snapshot(db_b)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py::TestReverseApplySplit -q`
Expected: FAIL with `AttributeError: ... '_reverse_apply'`

- [ ] **Step 3: Perform the split**

Rename the existing function body to `_reverse_apply` with the signature above, then:
- **Delete** from it: the `pos = allocator.claim_high()` / `if pos is None: return None` lines, and the `_extract_commit` call. `pos` and `file_results` are now parameters.
- **Keep** in it: the `commit_metadata`/`linearization` length and alignment `ValueError` guards (they must run before any write), everything from `all_triples` onward, `_frontier_persist_claim(..., from_low=False, ...)`, and the single `_db_checkpoint(db)`.
- Change the return type from `Optional[str]` to `str`.

Then reduce `_reverse_fill_claim_and_process` to:

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
    """Synchronous convenience wrapper: claim one position from the gap's
    high end and process it. Kept for tests and _reverse_bulk_fill_walk.

    **2d must not call this from async code** -- it fuses the CPU-bound
    tree-sitter parse and the DB-bound writes into one function body, so it
    can only be scheduled onto one executor as a unit. On write_executor (a
    thread) that parse holds the GIL for its whole duration, which is
    exactly the event-loop starvation #116 introduced the process pool to
    fix. 2d awaits _extract_commit on the process pool and _reverse_apply on
    write_executor instead. Same caveat 2c's own wrappers carry.

    Returns the claimed commit's hash, or None if the gap was already empty.
    """
    if len(commit_metadata) != len(linearization):
        raise ValueError(
            "commit_metadata must be full-history and positionally aligned with "
            f"linearization (got {len(commit_metadata)} entries vs {len(linearization)}); "
            "pass _git_commits(repo, watermark_hash=None)"
        )
    pos = allocator.claim_high()
    if pos is None:
        return None
    file_results, _gitlink_changes, _gitmodules_map, _renamed_pairs = _extract_commit(
        repo_path, linearization[pos], ignore_patterns
    )
    return _reverse_apply(
        db, repo_path, linearization, commit_metadata, pos, file_results, index_con=index_con,
    )
```

Note the length guard is duplicated deliberately: it must fire **before** `claim_high()` consumes a position, and `_reverse_apply` needs its own copy because 2d calls it directly.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_mcp_server.py -k "ReverseFill or ReverseApply" -q`
Expected: all pass, including every pre-existing `TestReverseFill*` test unchanged

Run: `python -m pytest tests/test_mcp_server.py tests/test_frontier_registry.py -q`
Expected: zero failures, count up by 1

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add #222 phase 2d: split _reverse_apply from its claim wrapper"
```

---

### Task 7: The round-robin claim generator

**Files:**
- Modify: `mcp_server.py` (next to `_parse_stream_ratio`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `frontier_registry.FrontierAllocator`, `_parse_stream_ratio`
- Produces:
  ```python
  class _RoundRobinClaimer:
      def __init__(self, allocator, forward_per_round: int, reverse_per_round: int): ...
      def next_claim(self) -> Optional[Tuple[str, int]]:
          """('fwd'|'rev', pos), or None once the gap is empty."""
  ```

- [ ] **Step 1: Write the failing tests**

```python
class TestRoundRobinClaimer:
    def _claimer(self, total, forward, reverse):
        import mcp_server, frontier_registry
        allocator = frontier_registry.FrontierAllocator(total, [])
        return mcp_server._RoundRobinClaimer(allocator, forward, reverse), allocator

    def _drain(self, claimer):
        out = []
        while True:
            claim = claimer.next_claim()
            if claim is None:
                return out
            out.append(claim)

    def test_one_to_one_alternates(self):
        claimer, _ = self._claimer(6, 1, 1)
        assert self._drain(claimer) == [
            ("fwd", 0), ("rev", 5), ("fwd", 1), ("rev", 4), ("fwd", 2), ("rev", 3),
        ]

    def test_one_to_three(self):
        claimer, _ = self._claimer(8, 1, 3)
        assert self._drain(claimer) == [
            ("fwd", 0), ("rev", 7), ("rev", 6), ("rev", 5),
            ("fwd", 1), ("rev", 4), ("rev", 3), ("rev", 2),
        ]

    def test_three_to_one(self):
        claimer, _ = self._claimer(8, 3, 1)
        assert self._drain(claimer) == [
            ("fwd", 0), ("fwd", 1), ("fwd", 2), ("rev", 7),
            ("fwd", 3), ("fwd", 4), ("fwd", 5), ("rev", 6),
        ]

    def test_every_position_claimed_exactly_once(self):
        for total in (1, 2, 3, 7, 20, 33):
            for ratio in ((1, 1), (1, 3), (3, 1), (2, 5)):
                claimer, _ = self._claimer(total, *ratio)
                claims = self._drain(claimer)
                positions = [pos for _tag, pos in claims]
                assert sorted(positions) == list(range(total)), (total, ratio)

    def test_forward_ascending_reverse_descending(self):
        claimer, _ = self._claimer(20, 2, 3)
        claims = self._drain(claimer)
        fwd = [pos for tag, pos in claims if tag == "fwd"]
        rev = [pos for tag, pos in claims if tag == "rev"]
        assert fwd == sorted(fwd), "forward state machine requires ascending"
        assert rev == sorted(rev, reverse=True), "2b's monotonicity guard requires descending"

    def test_empty_linearization_yields_nothing(self):
        claimer, _ = self._claimer(0, 1, 1)
        assert claimer.next_claim() is None

    def test_single_position_goes_to_whoever_asks_first(self):
        claimer, _ = self._claimer(1, 1, 1)
        assert claimer.next_claim() == ("fwd", 0)
        assert claimer.next_claim() is None

    def test_pre_drained_gap_yields_nothing_immediately(self):
        """Resume case: the previous run already covered everything."""
        claimer, allocator = self._claimer(5, 1, 1)
        while allocator.claim_low() is not None:
            pass
        assert claimer.next_claim() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestRoundRobinClaimer -q`
Expected: FAIL with `AttributeError: ... '_RoundRobinClaimer'`

- [ ] **Step 3: Write the implementation**

```python
class _RoundRobinClaimer:
    """#222 phase 2d: hands out positions from the shared gap, alternating
    forward and reverse by a fixed ratio.

    This IS the fairness mechanism. Because claims are handed out in one
    deterministic sequence rather than raced between two tasks, starvation
    is not expressible and the interleave is directly assertable in a test.
    """

    def __init__(
        self,
        allocator: "frontier_registry.FrontierAllocator",
        forward_per_round: int,
        reverse_per_round: int,
    ):
        self._allocator = allocator
        self._forward_per_round = forward_per_round
        self._reverse_per_round = reverse_per_round
        self._taken_in_phase = 0
        self._forward_phase = True

    def next_claim(self) -> Optional[Tuple[str, int]]:
        """('fwd', pos) or ('rev', pos), or None once the gap is empty.

        There is deliberately no "the other side might still have work"
        fallback: claim_low() and claim_high() both return None on exactly
        the same condition, is_gap_empty(), so they can only ever return
        None together. A fallthrough would be dead code reading as if the
        two frontiers could exhaust independently -- they cannot; they share
        one gap.
        """
        if self._forward_phase:
            pos = self._allocator.claim_low()
            tag = "fwd"
            limit = self._forward_per_round
        else:
            pos = self._allocator.claim_high()
            tag = "rev"
            limit = self._reverse_per_round
        if pos is None:
            return None

        self._taken_in_phase += 1
        if self._taken_in_phase >= limit:
            self._taken_in_phase = 0
            self._forward_phase = not self._forward_phase
        return tag, pos
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestRoundRobinClaimer -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add #222 phase 2d: round-robin claim generator"
```

---

### Task 8: Stage A — allocator-driven tagged pipeline

**Files:**
- Modify: `mcp_server.py:8055-8215` (`_run_ingestion`'s setup and `submit_next`/drain loop)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_RoundRobinClaimer` (7), `_forward_apply` (4/5), `_reverse_apply` (6), `_frontier_load`/`_frontier_persist_claim` (1), `frontier_registry.build_linearization`
- Produces: `_run_ingestion` drives both streams; forward path additionally calls `_frontier_persist_claim(from_low=True)` and `_lineage_confirmed_through_update`

- [ ] **Step 1: Replace the commit source**

In `_run_ingestion`, replace `commits = _git_commits(repo_path, watermark, branch)` with:

```python
        linearization = frontier_registry.build_linearization(repo_path, branch)
        # FULL history, positionally aligned with linearization. Both
        # _reverse_apply and _correction_sweep_select_position index it
        # positionally, and _reverse_apply raises ValueError on a length
        # mismatch -- a watermark-relative list is exactly the wrong thing
        # to hand them.
        commit_metadata = _git_commits(repo_path, None, branch)
```

`watermark` is still read by the preload and still used by `_frontier_seed_from_watermark` inside `_frontier_load`; do not delete it.

- [ ] **Step 2: Load the frontier and build the claimer**

After `_ingest_progress` initialisation and after `index_con` is opened (the frontier load writes, so it needs `index_con` and a live `db`):

```python
            run_ts_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"
            db = await _ensure_db_async()
            allocator = await loop.run_in_executor(
                write_executor, _frontier_load, db, linearization, run_ts_iso, index_con,
            )
            claimer = _RoundRobinClaimer(
                allocator, *_parse_stream_ratio(os.environ.get("MINIGRAF_INGEST_STREAM_RATIO"))
            )
```

- [ ] **Step 3: Make `submit_next` allocator-driven**

Replace the existing `commits_iter`/`submit_next` pair with:

```python
                pending: Any = deque()

                def submit_next() -> bool:
                    claim = claimer.next_claim()
                    if claim is None:
                        return False
                    tag, pos = claim
                    fut = loop.run_in_executor(
                        executor, _extract_commit, repo_path, linearization[pos], ignore_patterns
                    )
                    pending.append((tag, pos, fut))
                    return True
```

The drain loop's `popleft` becomes `tag, pos, fut = pending.popleft()`, and `commit_hash, commit_ts_iso, author, subject = commit_metadata[pos]`. The extraction-failure `except` branch is unchanged apart from that unpack.

- [ ] **Step 4: Dispatch by tag**

Replace the single `_forward_apply` call from Task 4 with:

```python
                    db = await _ensure_db_async()
                    try:
                        if tag == "fwd":
                            await loop.run_in_executor(
                                write_executor, _forward_apply, db, repo_path, state,
                                commit_metadata[pos],
                                (extracted_files, gitlink_changes, gitmodules_map, renamed_pairs),
                                index_con, linearization, pos,
                            )
                        else:
                            await loop.run_in_executor(
                                write_executor, _reverse_apply, db, repo_path, linearization,
                                commit_metadata, pos, extracted_files, index_con,
                            )
                    except Exception as e:
```

- [ ] **Step 5: Persist the forward claim**

Widen `_forward_apply` with `linearization: Optional[List[str]] = None, pos: Optional[int] = None`, and after its existing `_watermark_update` call add:

```python
    if linearization is not None and pos is not None:
        _frontier_persist_claim(
            db, linearization, pos, from_low=True,
            commit_ts_iso=commit_ts_iso, index_con=index_con,
        )
    # The virgin positions this walk claims are authoritative on first write,
    # so lineage is confirmed contiguously from C0 through here. The sweep
    # folds its own region in later (Task 9); this watermark must not be
    # advanced past the forward frontier before that happens.
    _lineage_confirmed_through_update(db, commit_hash, commit_ts_iso, index_con=index_con)
```

placed **before** the existing `_db_checkpoint(db)` so one checkpoint still covers the whole commit.

- [ ] **Step 6: Write the tests**

```python
class TestStageAInterleave:
    def _repo(self, tmp_path, n_commits=6):
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for i in range(n_commits):
            (repo / "auth.py").write_text(f"def login():\n    return {i}\n")
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"h{i}"], cwd=repo, check=True, capture_output=True)
        return repo

    @pytest.mark.asyncio
    async def test_both_frontiers_advance_and_meet(self, tmp_path, monkeypatch):
        """The load-bearing property: a real run must leave frontier-low and
        frontier-high adjacent, covering every position exactly once."""
        import mcp_server, frontier_registry
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path)
        graph = tmp_path / "g.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph))
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:1")

        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None

        db = MiniGrafDb.open(str(graph))
        linearization = frontier_registry.build_linearization(str(repo))
        low = mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT)
        high = mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_HIGH_IDENT)
        assert low is not None and high is not None, "both streams must have claimed"
        pos = {h: i for i, h in enumerate(linearization)}
        assert pos[low[0]] == 0
        assert pos[high[1]] == len(linearization) - 1
        assert pos[low[1]] + 1 == pos[high[0]], "the two frontiers must be adjacent"

    @pytest.mark.asyncio
    async def test_forward_heavy_ratio_shifts_the_meeting_point(self, tmp_path, monkeypatch):
        import mcp_server, frontier_registry
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path, n_commits=8)
        graph = tmp_path / "g.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph))
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "3:1")

        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None

        db = MiniGrafDb.open(str(graph))
        linearization = frontier_registry.build_linearization(str(repo))
        pos = {h: i for i, h in enumerate(linearization)}
        low = mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT)
        # 3:1 over 8 positions -> forward takes 6, reverse takes 2.
        assert pos[low[1]] == 5

    @pytest.mark.asyncio
    async def test_watermark_matches_frontier_low_hi(self, tmp_path, monkeypatch):
        """:ingestion/watermark keeps its contiguous-from-C0 meaning: it is
        advanced by the forward stream only, never by the reverse one."""
        import mcp_server
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path)
        graph = tmp_path / "g.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph))
        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None

        db = MiniGrafDb.open(str(graph))
        low = mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_LOW_IDENT)
        assert mcp_server._watermark_query(db) == low[1]

    @pytest.mark.asyncio
    async def test_single_commit_repo(self, tmp_path, monkeypatch):
        import mcp_server
        repo = self._repo(tmp_path, n_commits=1)
        monkeypatch.setattr(mcp_server, "_graph_path", str(tmp_path / "g.graph"))
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["status"] == "complete"

    @pytest.mark.asyncio
    async def test_second_run_on_unchanged_repo_is_a_no_op(self, tmp_path, monkeypatch):
        """Resume with an already-empty gap: Stage A must never begin, not
        spin or re-process."""
        import mcp_server
        repo = self._repo(tmp_path)
        monkeypatch.setattr(mcp_server, "_graph_path", str(tmp_path / "g.graph"))
        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["status"] == "complete"
```

Check whether the file already uses `pytest.mark.asyncio` or an explicit `asyncio.run` idiom for the existing `_run_ingestion` tests (see `TestRunIngestionShutdown`) and follow whichever is already there rather than introducing a second style.

- [ ] **Step 7: Run the tests**

Run: `python -m pytest tests/test_mcp_server.py::TestStageAInterleave -q`
Expected: 5 passed

Run: `python -m pytest tests/test_mcp_server.py tests/test_frontier_registry.py -q`
Expected: all pass. Existing `_run_ingestion` tests that assumed a watermark-relative commit list may need their *expectations* updated — but only where the change is genuinely the new intended behaviour. If a test fails because lineage is now provisional in the upper region, that is correct and expected; Task 9 is what makes it authoritative again.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add #222 phase 2d: allocator-driven tagged pipeline (Stage A)"
```

---

### Task 9: Stage B — asynchronous correction sweep

**Files:**
- Modify: `mcp_server.py` (`_run_ingestion`, after the Stage A drain loop)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_correction_sweep_select_position`, `_extract_commit`, `_correction_sweep_apply`, `_correction_sweep_log_summary` (all 2c), `_correction_sweep_through_query`, `_frontier_read_bounds`, `_lineage_confirmed_through_update`
- Produces: nothing new — this is a driver

- [ ] **Step 1: Write the failing tests**

```python
class TestStageBCorrectionSweep:
    def _repo(self, tmp_path, n_commits=6):
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for i in range(n_commits):
            (repo / "auth.py").write_text(
                f"def login():\n    return {i}\n\ndef helper_{i}():\n    pass\n"
            )
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"h{i}"], cwd=repo, check=True, capture_output=True)
        return repo

    @pytest.mark.asyncio
    async def test_completed_run_leaves_no_provisional_markers(self, tmp_path, monkeypatch):
        """The headline postcondition of phase 2: after a full ingest,
        every entity's lineage is authoritative."""
        import mcp_server
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path)
        graph = tmp_path / "g.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph))
        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None

        db = MiniGrafDb.open(str(graph))
        assert mcp_server._preload_provisional_idents(db) == set()

    @pytest.mark.asyncio
    async def test_no_entity_has_two_introduced_by_values(self, tmp_path, monkeypatch):
        import mcp_server
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path)
        graph = tmp_path / "g.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph))
        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None

        db = MiniGrafDb.open(str(graph))
        raw = mcp_server._db_execute(db, "(query [:find ?e ?c :where [?e :introduced-by ?c]])")
        pairs = [tuple(row) for row in json.loads(raw)["results"]]
        entities = [e for e, _c in pairs]
        assert len(entities) == len(set(entities)), f"duplicate :introduced-by: {pairs}"

    @pytest.mark.asyncio
    async def test_lineage_confirmed_through_reaches_head(self, tmp_path, monkeypatch):
        import mcp_server, frontier_registry
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path)
        graph = tmp_path / "g.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph))
        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None

        db = MiniGrafDb.open(str(graph))
        linearization = frontier_registry.build_linearization(str(repo))
        assert mcp_server._lineage_confirmed_through_query(db) == linearization[-1]

    def test_fold_guard_does_not_fire_when_frontier_high_is_absent(self, real_db, tmp_path):
        """_correction_sweep_select_position returns None for six reasons,
        only one of which is 'reached the ceiling'. Folding the watermark on
        any None would claim lineage is confirmed through HEAD in exactly
        the cases where the sweep did no work at all."""
        import mcp_server, frontier_registry
        repo = self._repo(tmp_path, n_commits=3)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        # frontier-high never created: forward claimed everything.
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        assert mcp_server._correction_sweep_select_position(
            real_db, linearization, commit_metadata,
        ) is None
        assert mcp_server._should_fold_lineage_watermark(real_db, linearization) is False

    def test_fold_guard_fires_only_when_sweep_reached_the_ceiling(self, real_db, tmp_path):
        import mcp_server, frontier_registry
        repo = self._repo(tmp_path, n_commits=3)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        # Reverse claims the top two, forward the bottom one -> gap closed.
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        pos = allocator.claim_low()
        mcp_server._frontier_persist_claim(
            real_db, linearization, pos, from_low=True,
            commit_ts_iso=commit_metadata[pos][1],
        )
        assert mcp_server._should_fold_lineage_watermark(real_db, linearization) is False
        mcp_server._correction_sweep_walk(
            real_db, str(repo), linearization, commit_metadata,
        )
        assert mcp_server._should_fold_lineage_watermark(real_db, linearization) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestStageBCorrectionSweep -q`
Expected: FAIL — `_should_fold_lineage_watermark` undefined, and the completed-run assertions fail because no sweep runs yet

- [ ] **Step 3: Write the fold guard**

```python
def _should_fold_lineage_watermark(db: Any, linearization: List[str]) -> bool:
    """True iff the correction sweep genuinely reached frontier-high's own
    :hi-hash, so :ingestion/lineage-confirmed-through may be folded forward
    to it.

    Stage B's loop exit alone must NOT trigger the fold.
    _correction_sweep_select_position returns None for six different
    reasons, only one of which is "reached the ceiling": it also returns
    None when frontier-high is absent, when either boundary hash is stale
    (rewritten history), when the gap is still open, and when
    commit_metadata violates its contract. Folding on any None would report
    lineage as confirmed through HEAD in exactly the situations where the
    sweep did no work at all.
    """
    high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
    if high_bounds is None:
        return False
    through_hash = _correction_sweep_through_query(db)
    if through_hash is None:
        return False
    return through_hash == high_bounds[1] and through_hash in set(linearization)
```

- [ ] **Step 4: Write the Stage B driver**

In `_run_ingestion`, immediately after the `while pending:` loop ends and before the `if completed_all:` tag/last-run block:

```python
                # Stage B: the correction sweep. A third, strictly
                # SEQUENTIAL pass, not a third concurrent task -- claim_low()
                # and claim_high() partition one shared gap, so no sequence of
                # forward claims can reach territory the reverse stream
                # already claimed, and the sweep's own precondition is that
                # the gap is already closed.
                #
                # Drives 2c's three pieces directly on their correct
                # executors. _correction_sweep_claim_and_process and
                # _correction_sweep_walk must NOT be used here: both fuse the
                # CPU-bound parse and the DB-bound writes into one body.
                if completed_all:
                    _ingest_progress["phase"] = "sweeping"
                    hash_to_pos = {h: i for i, h in enumerate(linearization)}
                    skipped = 0
                    db = await _ensure_db_async()
                    try:
                        while not _shutdown_requested.is_set():
                            selected = await loop.run_in_executor(
                                write_executor, _correction_sweep_select_position,
                                db, linearization, commit_metadata, hash_to_pos,
                            )
                            if selected is None:
                                break
                            sweep_hash, sweep_ts = selected
                            try:
                                sweep_files, _g, _m, _r = await loop.run_in_executor(
                                    executor, _extract_commit, repo_path, sweep_hash, ignore_patterns,
                                )
                                skipped += await loop.run_in_executor(
                                    write_executor, _correction_sweep_apply,
                                    db, sweep_hash, sweep_ts, sweep_files, index_con, skipped,
                                )
                            except concurrent.futures.process.BrokenProcessPool:
                                raise
                            except Exception as e:
                                # A sweep-step failure aborts Stage B only.
                                # Stage A's work is already persisted, and the
                                # next run resumes from the sweep watermark.
                                print(
                                    f"[_run_ingestion] correction sweep aborted at {sweep_hash}: {e}",
                                    file=sys.stderr,
                                )
                                completed_all = False
                                break
                            await asyncio.sleep(0)  # yield to event loop
                        if _shutdown_requested.is_set():
                            completed_all = False
                        _correction_sweep_log_summary(skipped)
                        if completed_all and _should_fold_lineage_watermark(db, linearization):
                            await loop.run_in_executor(
                                write_executor, _lineage_confirmed_through_update,
                                db, linearization[-1], commit_metadata[-1][1], index_con,
                            )
                            await loop.run_in_executor(write_executor, _db_checkpoint, db)
                    finally:
                        _db = None
```

`hash_to_pos` is built once and threaded in, and `skipped` is threaded through every `_correction_sweep_apply` call — the two obligations 2c's spec places on 2d's own loop. `_correction_sweep_log_summary` is called exactly once so an operator grepping for that line does not have to know which loop drove the sweep.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_mcp_server.py::TestStageBCorrectionSweep -q`
Expected: 5 passed

Run: `python -m pytest tests/test_mcp_server.py tests/test_frontier_registry.py -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add #222 phase 2d: asynchronous correction sweep (Stage B)"
```

---

### Task 10: Progress phase, shutdown, and staging discipline

**Files:**
- Modify: `mcp_server.py` (`_run_ingestion`, `_ingest_progress` initialisation around line 8079)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: everything above
- Produces: `_ingest_progress["phase"]` ∈ `{"converging", "sweeping"}`

- [ ] **Step 1: Write the failing tests**

```python
class TestStagingAndShutdown:
    def _repo(self, tmp_path, n_commits=6):
        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for i in range(n_commits):
            (repo / "auth.py").write_text(f"def login():\n    return {i}\n")
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"h{i}"], cwd=repo, check=True, capture_output=True)
        return repo

    @pytest.mark.asyncio
    async def test_phase_reaches_sweeping_and_processed_never_exceeds_total(self, tmp_path, monkeypatch):
        """Stage B's commits are re-visits of positions Stage A already
        counted -- counting them again would push processed past total."""
        import mcp_server
        repo = self._repo(tmp_path)
        monkeypatch.setattr(mcp_server, "_graph_path", str(tmp_path / "g.graph"))
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["phase"] == "sweeping"
        assert mcp_server._ingest_progress["processed"] <= mcp_server._ingest_progress["total"]

    @pytest.mark.asyncio
    async def test_sweep_does_not_start_before_stage_a_drains(self, tmp_path, monkeypatch):
        """The sweep's precondition is a CLOSED gap. Stage A pre-claims up to
        pipeline_depth positions ahead of persisting them, so the in-memory
        gap can close while commits are still in flight -- the DB-read
        precondition is what makes this safe, and this test pins it."""
        import mcp_server
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path)
        graph = tmp_path / "g.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph))

        observed = []
        real_apply = mcp_server._correction_sweep_apply

        def spy(db, *args, **kwargs):
            observed.append(mcp_server._ingest_progress["phase"])
            return real_apply(db, *args, **kwargs)

        monkeypatch.setattr(mcp_server, "_correction_sweep_apply", spy)
        await mcp_server._run_ingestion(str(repo), "master")
        assert observed, "the sweep must actually have run"
        assert set(observed) == {"sweeping"}

    @pytest.mark.asyncio
    async def test_shutdown_during_stage_a_stops_and_resumes(self, tmp_path, monkeypatch):
        import mcp_server, frontier_registry
        from minigraf import MiniGrafDb
        repo = self._repo(tmp_path, n_commits=10)
        graph = tmp_path / "g.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(graph))

        real_forward = mcp_server._forward_apply
        calls = {"n": 0}

        def stopping_forward(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                mcp_server._shutdown_requested.set()
            return real_forward(*args, **kwargs)

        monkeypatch.setattr(mcp_server, "_forward_apply", stopping_forward)
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["status"] == "stopped"
        mcp_server._db = None

        # A run stopped in Stage A must NOT claim confirmed lineage at HEAD.
        db = MiniGrafDb.open(str(graph))
        linearization = frontier_registry.build_linearization(str(repo))
        assert mcp_server._lineage_confirmed_through_query(db) != linearization[-1]
        db = None
        mcp_server._db = None

        monkeypatch.setattr(mcp_server, "_forward_apply", real_forward)
        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["status"] == "complete"
        mcp_server._db = None
        db2 = MiniGrafDb.open(str(graph))
        assert mcp_server._preload_provisional_idents(db2) == set()
        assert mcp_server._lineage_confirmed_through_query(db2) == linearization[-1]

    @pytest.mark.asyncio
    async def test_neither_sync_wrapper_is_called_from_run_ingestion(self, tmp_path, monkeypatch):
        """2c and 2b both document that their sync wrappers must not be
        called from async code -- each fuses a CPU-bound parse with DB-bound
        writes into one executor-schedulable unit."""
        import mcp_server
        repo = self._repo(tmp_path)
        monkeypatch.setattr(mcp_server, "_graph_path", str(tmp_path / "g.graph"))

        for name in (
            "_correction_sweep_claim_and_process",
            "_correction_sweep_walk",
            "_reverse_bulk_fill_walk",
            "_reverse_fill_claim_and_process",
        ):
            def boom(*a, _n=name, **k):
                raise AssertionError(f"{_n} must not be called from _run_ingestion")
            monkeypatch.setattr(mcp_server, name, boom)

        await mcp_server._run_ingestion(str(repo), "master")
        assert mcp_server._ingest_progress["status"] == "complete"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestStagingAndShutdown -q`
Expected: FAIL — `KeyError: 'phase'`

- [ ] **Step 3: Add the phase key**

Next to `_ingest_progress["status"] = "running"` in `_run_ingestion`, add:

```python
        _ingest_progress["phase"] = "converging"
```

Stage B already sets it to `"sweeping"` (Task 9, Step 4). Add `"phase": None` to the module-level `_ingest_progress` initialiser so a consumer reading it before any run does not `KeyError`.

Confirm `_ingest_progress["processed"] += 1` remains where it is — once per drained pipeline entry, covering both tags — and that Stage B adds nothing to it.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_mcp_server.py::TestStagingAndShutdown -q`
Expected: 4 passed

Run: `python -m pytest tests/test_mcp_server.py tests/test_frontier_registry.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add #222 phase 2d: progress phase, shutdown, staging discipline"
```

---

### Task 11: End-to-end parity and documentation

**Files:**
- Test: `tests/test_mcp_server.py`
- Modify: `SKILL.md`, `CLAUDE.md` if either documents ingestion behaviour that has changed

**Interfaces:** none — this task only verifies and documents

- [ ] **Step 1: Write the parity test**

```python
class TestMultiStreamParityWithForwardOnly:
    @pytest.mark.asyncio
    async def test_introduced_by_matches_a_forward_only_ingest(self, tmp_path, monkeypatch):
        """The whole converging design is only worth anything if it lands
        the same lineage a plain forward walk would. Ratio 1:1 splits the
        history down the middle, so this exercises the reconciliation, the
        sweep and the meeting point in one assertion."""
        import mcp_server, frontier_registry
        from minigraf import MiniGrafDb

        repo = tmp_path / "repo"
        repo.mkdir()
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for i in range(8):
            (repo / "auth.py").write_text(
                f"def login():\n    return {i}\n\ndef helper_{i}():\n    pass\n"
            )
            (repo / "util.py").write_text(f"CONST = {i}\n")
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"h{i}"], cwd=repo, check=True, capture_output=True)

        def introduced_by_map(path):
            db = MiniGrafDb.open(str(path))
            raw = mcp_server._db_execute(db, "(query [:find ?e ?c :where [?e :introduced-by ?c]])")
            return {row[0]: row[1] for row in json.loads(raw)["results"]}

        multi = tmp_path / "multi.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(multi))
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:1")
        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None
        multi_map = introduced_by_map(multi)
        mcp_server._db = None

        # Forward-only: give the forward stream the entire range by making
        # the reverse side never claim anything.
        forward_only = tmp_path / "fwd.graph"
        monkeypatch.setattr(mcp_server, "_graph_path", str(forward_only))
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", f"{10**6}:1")
        await mcp_server._run_ingestion(str(repo), "master")
        mcp_server._db = None
        forward_map = introduced_by_map(forward_only)

        assert multi_map == forward_map
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_mcp_server.py::TestMultiStreamParityWithForwardOnly -q`
Expected: PASS

If `:introduced-by` diverges, that is a real bug in Tasks 5/9, not a test to relax. Trace the specific entity: query its `:introduced-by` in both graphs, then check whether it was reconciled (`_preload_provisional_idents` on the multi graph) and whether the sweep visited its guess commit (`_correction_sweep_through_query`).

- [ ] **Step 3: Check the docs**

Grep for ingestion documentation that the new env var or the three-stage behaviour makes stale:

```bash
grep -rn "MINIGRAF_INGEST\|watermark\|ingest_status\|forward-only" SKILL.md CLAUDE.md README.md 2>/dev/null
```

Add `MINIGRAF_INGEST_STREAM_RATIO` wherever `MINIGRAF_INGEST_WORKERS` is already documented. If nothing documents ingestion internals, make no change — do not invent a docs section for this.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py tests/test_frontier_registry.py -q`
Expected: all pass, no regressions against the 968 baseline

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_server.py SKILL.md CLAUDE.md
git commit -m "Add #222 phase 2d: end-to-end parity with forward-only ingest"
```

---

## Wrap-up (after Task 11)

- [ ] Run `superpowers:requesting-code-review` over the whole branch before opening the PR. Every prior sub-phase of #222 had real defects caught by a whole-branch review — 2b's review found three High defects that became sub-phase 2b1, and 2c's spec was revised eight times.
- [ ] **Audit every commit message on the branch** for `Fix #222` / `Closes #222` / `Resolves #222` before merging: `git log master..HEAD --format=%B | grep -niE "(fix|close|resolve)[sd]? #222"`. Expected: no output.
- [ ] Open the PR with a body that does **not** contain a closing keyword. #222 stays open until phase 5.
- [ ] Update the memory file `project_222_multistream_ingestion_phases.md`: mark 2d done, record what 2d actually deferred (the frontier-high→low fold, Stage B pipelining), and note that phase 2 as a whole is complete.
