# Position-Indexed Preload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound the forward walk's preload state by the introducing commit's
linearization position instead of by author-date valid-time, closing #238's
unrecoverable data-loss direction, and retract `:introduced-by` on close so
closed entities stop resurrecting as ghosts (#231).

**Architecture:** The preload query gains one bound variable — the introducing
commit's `:hash` — which serves both fixes at once. #238 uses it to filter
rows by `hash_to_pos[hash] <= watermark_pos`; #231 uses it as the retract value
for `[ident :introduced-by commit]`. The valid-time bound is not deleted but
widened to the monotone envelope `T_hi(W) = max(ts[0..W])` and demoted: the
conjunctive position clause is what closes the data-loss direction, so the date
clause only governs how widely entities closed above W are re-admitted.

**Tech Stack:** Python 3, `minigraf` Datalog/bi-temporal graph backend, pytest
(`pytest-asyncio`), real-backend-only tests.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-04-position-indexed-preload-design.md`. Read it before Task 1.
- **`mcp` is capped `<2.0.0`.** Do not raise it. 2.0.0 removed `Server.list_tools` and broke CI for four days.
- **Real backend only.** Never `MagicMock` a `MiniGrafDb`. See `docs/testing-conventions.md`. Use the `real_db` fixture for unit tests; a real file-backed graph for multi-run/persistence tests.
- **`:contains` / `:parent` / `:depends-on` must be one transact per triple** — minigraf#287 (EAVT keys omit value bytes). Do not batch them.
- **No closing keywords for #238.** Every commit message and the PR body must say `Refs #238`, never `Closes`/`Fixes`. GitHub scans both, and on this project a *negated* "does not close #N" has still auto-closed an issue. #238 stays open, blocked on #245. `Closes #231` is correct and intended.
- **All ingest timestamps are `"%Y-%m-%dT%H:%M:%SZ"`** (fixed-width UTC, from `_git_commits`), so lexicographic string comparison equals chronological comparison. `max()` over these strings is safe.
- **Run the full suite before each commit:** `python -m pytest tests/test_mcp_server.py -q --tb=no`
- **"All pass" means "no NEW failures", not zero failures.** This dev environment is missing the tree-sitter grammars for ~14 languages, so **120 tests fail on this branch's base commit** — all in language-parser classes (`TestHaskell*`, `TestLua*`, `TestElixir*`, `TestCpp*`, `TestGo*`, `TestRuby*`, `TestSwift*`, `TestScala*`, `TestKotlin*`, `TestPhp*`, `TestJava*`, `TestJsFamily*`, `TestRustGoC*`, `TestFieldClassContainment`) plus `TestMcpToolWiring::test_call_tool_memory_finalize_turn`. CI installs the grammars and is green. The authoritative baseline list is `.superpowers/sdd/2026-08-04-position-indexed-preload/baseline-failures.txt`; compare against it with:
  ```bash
  python -m pytest tests/test_mcp_server.py -q --tb=no 2>&1 | grep '^FAILED' | sed 's/^FAILED //' | sort \
    | comm -13 .superpowers/sdd/2026-08-04-position-indexed-preload/baseline-failures.txt -
  ```
  Empty output = clean. **Never `--deselect` a test to make a run look green**, and never touch a language-parser test — none of them are in scope for any task here.
- **No XFAIL and no XPASS lines** may appear after Task 1.

## File Structure

- **`mcp_server.py`** (modify only) — every production change lands here. The file is ~10,360 lines; the spec does not call for a split, and unilaterally restructuring it is out of scope.
  - `_entity_ident_is_live` — new helper, next to `_entity_introduced_by_query` (~line 5422).
  - `_resolve_introduced_by` — new helper, next to `_build_close_triples` (~line 4645).
  - `_build_close_triples` (~4645), `_forget_closed_entity` (~4717), `_build_code_triples` (~6871), `_preload_known_entities` (~6983), `_preload_unresolved_dep_idents` (~7117), `_preload_known_deps` (~7256), `_preload_pinned_commits` (~7346), `_ForwardWalkState` (~9376), `_load_ingestion_preload_state` (~7420), `_reverse_apply` (~7856), `_forward_apply` (~8223), `_run_ingestion` (~9430).
- **`tests/test_mcp_server.py`** (modify only) — all tests. Follows the existing single-suite convention.
- **`docs/superpowers/specs/2026-08-04-position-indexed-preload-design.md`** — already committed, reference only.

Line numbers are from master `15c781f` and will drift as tasks land. Locate by
symbol name, not by line.

---

### Task 1: Gate the reverse walk on `:ident` liveness (#231's ghost)

Fixes the ghost on its own: a closed entity has no live `:ident`, so
`_build_code_triples` takes its introduction branch and re-asserts
`:ident`/`:description`/`:path` instead of emitting only `:modified-in`.

**Files:**
- Modify: `mcp_server.py` — add `_entity_ident_is_live` near `_entity_introduced_by_query`; change the gate in `_reverse_apply`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_entity_ident_is_live(db: Any, entity_ident: str) -> bool`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`, in the class that holds the other
`_entity_introduced_by_query` tests:

```python
class TestEntityIdentIsLive:
    """#231: the reverse walk's known-entity gate must test LIVENESS, not lineage.

    _build_close_triples never retracted :introduced-by, so a closed-and-purged
    entity kept that fact forever and _entity_introduced_by_query answered
    "known" for it. Gating on a live :ident instead is correct regardless of
    whether any close site remembers to retract :introduced-by.
    """

    def test_live_ident_is_live(self, real_db):
        import mcp_server
        real_db.execute('(transact [[:function/a-py-f :ident ":function/a-py-f"]])')
        assert mcp_server._entity_ident_is_live(real_db, ":function/a-py-f") is True

    def test_absent_ident_is_not_live(self, real_db):
        import mcp_server
        assert mcp_server._entity_ident_is_live(real_db, ":function/a-py-f") is False

    def test_closed_ident_is_not_live_even_with_live_introduced_by(self, real_db):
        """The exact #231 shape: :ident closed, :introduced-by left behind."""
        import mcp_server
        real_db.execute(
            '(transact {:valid-from "2020-01-01T00:00:00Z"} '
            '[[:function/a-py-f :ident ":function/a-py-f"] '
            '[:function/a-py-f :introduced-by :commit/c1]])'
        )
        mcp_server._ingest_close(
            real_db,
            ['[:function/a-py-f :ident ":function/a-py-f"]'],
            "2020-01-01T00:00:00Z",
            "2020-01-02T00:00:00Z",
            "close f",
        )
        assert mcp_server._entity_introduced_by_query(real_db, ":function/a-py-f") is not None, (
            "precondition: the stale :introduced-by is what made the old gate unsound"
        )
        assert mcp_server._entity_ident_is_live(real_db, ":function/a-py-f") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py::TestEntityIdentIsLive -v`
Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_entity_ident_is_live'`

- [ ] **Step 3: Write minimal implementation**

Add immediately above `_entity_introduced_by_values_query` in `mcp_server.py`:

```python
def _entity_ident_is_live(db: Any, entity_ident: str) -> bool:
    """True iff entity_ident currently has a live :ident fact.

    The reverse walk's "do I already know this entity?" gate (#231). It used
    to ask _entity_introduced_by_query(db, ident) is not None, which was
    unsound: _build_close_triples never retracted :introduced-by, so a
    closed-and-purged entity kept that fact forever and the gate answered
    "known" for it. _build_code_triples then took its "already known" branch
    and emitted only :modified-in -- the entity was resurrected with lineage
    but no identity, invisible to nearly every query, and
    _correction_sweep_apply could not repair it either (it reconciles lineage
    only and never emits structural facts).

    Task 4 of this change does make close sites retract :introduced-by, which
    would make the old gate correct too. This gate stays on :ident anyway: the
    question it asks IS liveness, and coupling it to a lineage attribute is
    what made #231 possible. It also stays correct if a future close site
    forgets :introduced-by.

    Current-time query by design -- an entity live in a CLOSED window is
    exactly the resurrection case this must answer False for.
    """
    raw = _db_execute(db, f"(query [:find ?i :where [{entity_ident} :ident ?i]])")
    return bool(json.loads(raw).get("results", []))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py::TestEntityIdentIsLive -v`
Expected: 3 passed

- [ ] **Step 5: Switch the gate**

In `_reverse_apply` (~line 7997), replace:

```python
        known_before: Dict[str, str] = {
            ident: "known" for ident in candidate_idents
            if _entity_introduced_by_query(db, ident) is not None
        }
```

with:

```python
        # #231: LIVENESS, not lineage. _entity_introduced_by_query was unsound
        # here -- a closed-and-purged entity kept its :introduced-by forever,
        # so this gate answered "known" for it, _build_code_triples took its
        # "already known" branch, and the entity came back as a ghost with no
        # current :ident. Same one query per candidate ident, so #239's cost
        # profile is unchanged.
        known_before: Dict[str, str] = {
            ident: "known" for ident in candidate_idents
            if _entity_ident_is_live(db, ident)
        }
```

- [ ] **Step 6: Remove the three markers**

In `tests/test_mcp_server.py`:

1. `test_reused_path_new_entity_is_not_a_ghost` (~11841): delete the
   `monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", f"{10**6}:1")` line and
   the `monkeypatch` parameter stays (still used by `_ingest_and_open`). Replace
   its long docstring — which describes the bug as unfixed — with:

```python
        """The module re-created at the reused path a.py must have a CURRENT
        :ident fact (join target for nearly every query).

        Ran forward-only-pinned (MINIGRAF_INGEST_STREAM_RATIO=10**6:1) until
        #231 was fixed, because _reverse_apply's known-entity gate keyed on
        :introduced-by -- which _build_close_triples never retracted -- and so
        answered "known" for a closed-and-purged ident. The gate now tests
        live :ident (#231), so this runs at the shipping default.
        """
```

2. `test_reused_path_new_entity_is_not_a_ghost_at_default_ratio` (~11907):
   delete the entire `@pytest.mark.xfail(...)` decorator. Keep the
   `monkeypatch.delenv` line and the docstring's first paragraph; replace the
   second paragraph (which explains the alarm) with:

```python
        The two tests are now identical in outcome and kept separate only
        because the pin above documents which configuration used to hide the
        ghost. The strict=True xfail that guarded this was removed with #231.
        """
```

3. Update the class-level comment at ~18056 that references
   "out-of-scope phase-2b defect (`_build_close_triples` never retracts
   `:introduced-by`)" to say it was fixed by #231.

- [ ] **Step 7: Run the affected tests**

Run: `python -m pytest tests/test_mcp_server.py::TestClosedEntityLifecyclePurge -v`
Expected: all pass, **no XFAIL and no XPASS lines**. An `XPASS` means a
decorator was missed.

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Gate the reverse walk on :ident liveness, not :introduced-by (#231)

_build_close_triples never retracted :introduced-by, so a closed-and-purged
entity kept that fact forever and _reverse_apply's known-entity gate answered
"known" for it -- resurrecting it with lineage but no identity. The gate now
tests a live :ident, which is the question it was actually asking.

Removes the strict xfail and both forward-only pins on
TestClosedEntityLifecyclePurge's ghost tests.

Refs #231

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Teach `_build_close_triples` to retract `:introduced-by`

**Files:**
- Modify: `mcp_server.py` — `_build_close_triples`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_build_close_triples(..., *, introduced_by: Optional[str] = None)` — appends `[{ident} :introduced-by {introduced_by}]` when `introduced_by` is not None.

- [ ] **Step 1: Write the failing test**

Add near the existing `test_build_close_triples_*` tests (~line 8303):

```python
    def test_build_close_triples_closes_introduced_by_when_given(self):
        import mcp_server
        module_ident = mcp_server._code_ident("module", "auth.py")
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        triples = mcp_server._build_close_triples(
            fn_ident, "login", module_ident, introduced_by=":commit/abc123def456",
        )
        assert f"[{fn_ident} :introduced-by :commit/abc123def456]" in triples

    def test_build_close_triples_omits_introduced_by_when_not_given(self):
        """Opt-in for the same reason close_entity_type is: unresolved-import
        stubs reuse the module ident prefix and never carry :introduced-by, so
        deriving one would retract a fact that was never asserted."""
        import mcp_server
        module_ident = mcp_server._code_ident("module", "auth.py")
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        triples = mcp_server._build_close_triples(fn_ident, "login", module_ident)
        assert not any(":introduced-by" in t for t in triples)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py -k build_close_triples_closes_introduced_by -v`
Expected: FAIL — `TypeError: _build_close_triples() got an unexpected keyword argument 'introduced_by'`

- [ ] **Step 3: Write minimal implementation**

In `_build_close_triples`, add the parameter to the keyword-only block:

```python
    is_static: Optional[bool] = None,
    introduced_by: Optional[str] = None,
) -> List[str]:
```

and append before `return triples`:

```python
    if introduced_by is not None:
        triples.append(f"[{ident} :introduced-by {introduced_by}]")
    return triples
```

Add to the docstring, after the `entity_type_kw` paragraph:

```
    introduced_by is the entity's :introduced-by commit ident, closed alongside
    everything else (#231). Opt-in for the same reason close_entity_type is:
    unresolved-import stubs reuse the module ident prefix but never have an
    :introduced-by fact (see _forward_apply's dep-edge handling), so deriving
    one here would retract a fact that was never asserted. Callers get the
    value from _resolve_introduced_by, which prefers the walk state and falls
    back to a DB read.

    Leaving this fact open was the whole of #231: a closed-and-purged entity
    still answered a bare [?e :introduced-by ?c] query, which made
    _entity_introduced_by_query an unsound liveness test.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py -k build_close_triples -v`
Expected: all pass (the new two plus the five existing ones).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: all pass — no call site passes the new argument yet.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Let _build_close_triples close :introduced-by (#231)

Opt-in keyword, like close_entity_type and file_value, because
unresolved-import stubs share the module ident prefix and never carry an
:introduced-by fact. No call site passes it yet.

Refs #231

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Track the introducing commit in the forward walk state

**Files:**
- Modify: `mcp_server.py` — `_ForwardWalkState`, `_build_code_triples`, `_forget_closed_entity`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `_ForwardWalkState.entity_introduced_by: Dict[str, str]` (field with `default_factory=dict`, declared after `ts_by_commit_ident`)
  - `_build_code_triples(..., entity_introduced_by: Optional[Dict[str, str]] = None)` — 11th parameter, populated at all five introduction branches
  - `_forget_closed_entity(..., entity_introduced_by: Optional[Dict[str, str]] = None)` — 8th parameter, popped when not None

- [ ] **Step 1: Write the failing test**

```python
class TestEntityIntroducedByState:
    """#231/#238: the forward walk tracks each entity's introducing commit ident
    so close sites have the value they must retract, and #238's preload can seed
    it. Mirrors entity_valid_from exactly -- set at introduction, popped on close.
    """

    EXTRACTED = {"functions": ["f"], "classes": [], "imports": []}

    def test_introduction_records_the_commit_ident(self):
        import mcp_server
        module_ident = mcp_server._code_ident("module", "a.py")
        fn_ident = mcp_server._code_ident("function", "a.py", "f")
        commit_ident = ":commit/aaaaaaaaaaaa"
        precomputed = mcp_server._precompute_file_triples(
            "a.py", self.EXTRACTED, commit_ident, {})
        entity_introduced_by = {}
        mcp_server._build_code_triples(
            "a.py", self.EXTRACTED, "2020-01-01T00:00:00Z",
            {}, {}, {},
            commit_ident,
            precomputed,
            None, None,
            entity_introduced_by,
        )
        assert entity_introduced_by[module_ident] == commit_ident
        assert entity_introduced_by[fn_ident] == commit_ident

    def test_already_known_entity_does_not_overwrite_the_commit_ident(self):
        """The introducing commit is written ONCE, like :introduced-by itself."""
        import mcp_server
        module_ident = mcp_server._code_ident("module", "a.py")
        fn_ident = mcp_server._code_ident("function", "a.py", "f")
        entity_valid_from = {module_ident: "2020-01-01T00:00:00Z",
                             fn_ident: "2020-01-01T00:00:00Z"}
        entity_introduced_by = {module_ident: ":commit/aaaaaaaaaaaa",
                                fn_ident: ":commit/aaaaaaaaaaaa"}
        precomputed = mcp_server._precompute_file_triples(
            "a.py", self.EXTRACTED, ":commit/bbbbbbbbbbbb", {})
        mcp_server._build_code_triples(
            "a.py", self.EXTRACTED, "2020-01-02T00:00:00Z",
            entity_valid_from, {}, {},
            ":commit/bbbbbbbbbbbb",
            precomputed,
            None, None,
            entity_introduced_by,
        )
        assert entity_introduced_by[fn_ident] == ":commit/aaaaaaaaaaaa"

    def test_none_is_accepted_so_the_reverse_walk_is_unaffected(self):
        """_reverse_apply owns :introduced-by timing itself and must not have a
        forward-biased guess written into its state."""
        import mcp_server
        commit_ident = ":commit/aaaaaaaaaaaa"
        precomputed = mcp_server._precompute_file_triples(
            "a.py", self.EXTRACTED, commit_ident, {})
        mcp_server._build_code_triples(
            "a.py", self.EXTRACTED, "2020-01-01T00:00:00Z",
            {}, {}, {},
            commit_ident,
            precomputed,
            None, None,
            None,
        )  # must not raise

    def test_forget_closed_entity_pops_it(self):
        import mcp_server
        entity_introduced_by = {":function/a-py-f": ":commit/aaaaaaaaaaaa"}
        mcp_server._forget_closed_entity(
            ":function/a-py-f", None, {}, {}, {}, {}, None, entity_introduced_by,
        )
        assert entity_introduced_by == {}

    def test_state_defaults_to_an_empty_dict(self):
        import mcp_server
        state = mcp_server._ForwardWalkState(
            entity_valid_from={}, entity_descriptions={}, file_entities={},
            file_deps={}, dep_valid_from={}, pinned_commit_state={},
            field_class_ident={}, field_static_ident={}, submodule_paths={},
            unresolved_dep_idents={},
        )
        assert state.entity_introduced_by == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py::TestEntityIntroducedByState -v`
Expected: FAIL — `TypeError: _build_code_triples() takes 10 positional arguments but 11 were given`

If `_precompute_file_triples`' signature differs from the helper above, fix the
helper to match the real signature — copy the call shape from an existing
`_build_code_triples` test in the suite rather than guessing.

- [ ] **Step 3: Add the state field**

In `_ForwardWalkState`, after `ts_by_commit_ident`:

```python
    # #231/#238: ident -> the :commit/... ident that introduced it. Mirrors
    # entity_valid_from (which holds the same introduction's TIMESTAMP) and is
    # maintained at exactly the same points: set at _build_code_triples'
    # introduction branches, popped by _forget_closed_entity, seeded by
    # _preload_known_entities. Close sites need it as the retract VALUE for
    # [ident :introduced-by commit] -- a retract needs the exact value, and
    # entity_valid_from only carries the timestamp.
    #
    # Entities the REVERSE walk introduced during this run are absent here:
    # they were never written through the forward state. Close sites must
    # therefore go through _resolve_introduced_by, which falls back to a DB
    # read, or #231 survives for exactly those entities when Stage B's
    # lifecycle pass closes them.
    entity_introduced_by: Dict[str, str] = field(default_factory=dict)
```

- [ ] **Step 4: Populate it in `_build_code_triples`**

Add the parameter:

```python
    field_static_ident: Optional[Dict[str, bool]] = None,
    entity_introduced_by: Optional[Dict[str, str]] = None,
) -> List[str]:
```

In the `if is_new_module:` branch, after `entity_valid_from[module_ident] = commit_ts_iso`:

```python
        if entity_introduced_by is not None:
            entity_introduced_by[module_ident] = commit_ident
```

Then in each of the four child loops (`function_entries`, `class_entries`,
`global_entries`, `field_entries`), inside the `if X_ident not in entity_valid_from:`
branch, after `entity_valid_from[X_ident] = commit_ts_iso`, add the same two
lines with the loop's own ident variable — `fn_ident`, `cls_ident`,
`gvar_ident`, `field_ident` respectively. Five sites total.

Add to the docstring:

```
    entity_introduced_by, when supplied, records ident -> commit_ident at every
    introduction branch -- the same five places entity_valid_from is written,
    and written once for the same reason. Close sites need the commit IDENT to
    retract [ident :introduced-by commit] (#231); entity_valid_from only has
    the timestamp. Optional and defaulting to None because _reverse_apply
    filters this function's :introduced-by output out entirely and owns that
    attribute's write timing itself, so a forward-biased guess must never
    reach its state.
```

- [ ] **Step 5: Pop it in `_forget_closed_entity`**

Add the parameter after `field_static_ident`:

```python
    field_static_ident: Optional[Dict[str, bool]] = None,
    entity_introduced_by: Optional[Dict[str, str]] = None,
) -> None:
```

and next to the other pops:

```python
    if entity_introduced_by is not None:
        entity_introduced_by.pop(ident, None)
```

Add to its docstring's bullet list:

```
    - entity_introduced_by: holds the ident's introducing commit for #231's
      close-time retract. A stale entry would make a re-introduction at the
      same ident retract the OLD introduction's :introduced-by on its next
      close -- a fact that no longer exists at that value.
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py::TestEntityIntroducedByState -v`
Expected: 5 passed

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: all pass. Both `_build_code_triples` call sites (`_reverse_apply` at
~8003, `_forward_apply` at ~8451) still pass 10 positional arguments, so the
new parameter defaults to `None` at both and nothing changes yet.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Track each entity's introducing commit ident in the forward walk state (#231)

A retract needs the exact value, and entity_valid_from only carries the
introduction TIMESTAMP -- so close sites had no way to retract
[ident :introduced-by commit]. Adds entity_introduced_by alongside it,
maintained at exactly the same points. No close site reads it yet.

Refs #231

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Retract `:introduced-by` at all six close sites

**Files:**
- Modify: `mcp_server.py` — add `_resolve_introduced_by`; wire `_forward_apply`'s six close sites
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Task 2's `_build_close_triples(introduced_by=...)`, Task 3's `_ForwardWalkState.entity_introduced_by`.
- Produces: `_resolve_introduced_by(db: Any, state: _ForwardWalkState, ident: str) -> Optional[str]`

- [ ] **Step 1: Write the failing test**

```python
class TestCloseRetractsIntroducedBy:
    """#231: every close site must retract :introduced-by, including for
    entities the reverse walk introduced (which the forward state never saw).
    """

    def test_resolve_prefers_the_walk_state(self, real_db):
        import mcp_server
        state = mcp_server._ForwardWalkState(
            entity_valid_from={}, entity_descriptions={}, file_entities={},
            file_deps={}, dep_valid_from={}, pinned_commit_state={},
            field_class_ident={}, field_static_ident={}, submodule_paths={},
            unresolved_dep_idents={},
        )
        state.entity_introduced_by[":function/a-py-f"] = ":commit/aaaaaaaaaaaa"
        real_db.execute('(transact [[:function/a-py-f :introduced-by :commit/zzzzzzzzzzzz]])')
        assert mcp_server._resolve_introduced_by(
            real_db, state, ":function/a-py-f") == ":commit/aaaaaaaaaaaa"

    def test_resolve_falls_back_to_the_db_for_reverse_introduced_entities(self, real_db):
        """Stage B closes entities Stream 2 introduced this run. They were never
        written through the forward state, so without this fallback #231 would
        survive for exactly those."""
        import mcp_server
        state = mcp_server._ForwardWalkState(
            entity_valid_from={}, entity_descriptions={}, file_entities={},
            file_deps={}, dep_valid_from={}, pinned_commit_state={},
            field_class_ident={}, field_static_ident={}, submodule_paths={},
            unresolved_dep_idents={},
        )
        real_db.execute('(transact [[:function/a-py-f :introduced-by :commit/zzzzzzzzzzzz]])')
        assert mcp_server._resolve_introduced_by(
            real_db, state, ":function/a-py-f") == ":commit/zzzzzzzzzzzz"

    def test_resolve_returns_none_for_an_entity_with_no_lineage(self, real_db):
        """Unresolved-import stubs never get :introduced-by."""
        import mcp_server
        state = mcp_server._ForwardWalkState(
            entity_valid_from={}, entity_descriptions={}, file_entities={},
            file_deps={}, dep_valid_from={}, pinned_commit_state={},
            field_class_ident={}, field_static_ident={}, submodule_paths={},
            unresolved_dep_idents={},
        )
        assert mcp_server._resolve_introduced_by(real_db, state, ":module/pkg-missing") is None
```

Then the end-to-end assertion, added to `TestClosedEntityLifecyclePurge`
(which already has `_ingest_and_open` and `_results`):

```python
    @pytest.mark.asyncio
    @pytest.mark.parametrize("variant", ["rename", "delete"])
    async def test_closed_entity_has_no_live_introduced_by(self, tmp_path, monkeypatch, variant):
        """#231: the original function f, closed at commit2, must not answer a
        bare [?e :introduced-by ?c] query afterwards."""
        repo = _reused_path_repo(tmp_path / "repo", variant)
        db = await self._ingest_and_open(repo, monkeypatch)

        import mcp_server
        f_ident = mcp_server._code_ident("function", "a.py", "f")
        live = self._results(db, f'(query [:find ?c :where [{f_ident} :introduced-by ?c]])')
        assert live == [], f"[{variant}] closed entity kept a live :introduced-by: {live}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py::TestCloseRetractsIntroducedBy tests/test_mcp_server.py::TestClosedEntityLifecyclePurge::test_closed_entity_has_no_live_introduced_by -v`
Expected: the three `_resolve_introduced_by` tests FAIL with `AttributeError`;
`test_closed_entity_has_no_live_introduced_by` FAILS with a non-empty list.

- [ ] **Step 3: Add the resolver**

Immediately above `_build_close_triples` in `mcp_server.py`:

```python
def _resolve_introduced_by(
    db: Any, state: "_ForwardWalkState", ident: str
) -> Optional[str]:
    """The commit ident to retract for ident's :introduced-by at a close site.

    Prefers the walk state, which the forward walk maintains for free at every
    introduction. Falls back to a DB read for entities the state never saw --
    principally entities Stream 2 introduced during THIS run, which Stage B's
    _forward_apply(lifecycle_only=True) then closes. Without the fallback #231
    would survive for exactly those.

    The fallback is a per-CLOSE read, not per-ident-per-commit, so it does not
    feed #239's hot path (1.33M :introduced-by point queries, 33.6% of at-scale
    wall clock). Do not hoist it into a preloaded set: #235 removed a
    provisional-ident prefilter for being wrong in both directions, and the
    same argument applies to any run-start snapshot of lineage.

    Returns None when the entity genuinely has no :introduced-by (an
    unresolved-import stub), which _build_close_triples treats as "do not
    retract".
    """
    known = state.entity_introduced_by.get(ident)
    if known is not None:
        return known
    return _entity_introduced_by_query(db, ident)
```

- [ ] **Step 4: Wire the six close sites**

In `_forward_apply`, every `_build_close_triples(...)` call gains
`introduced_by=_resolve_introduced_by(db, state, <ident>)`, and every paired
`_forget_closed_entity(...)` call gains `state.entity_introduced_by` as its
eighth argument.

The six pairs, by the ident each closes:

| Site | Ident variable | Context |
| --- | --- | --- |
| ~8320 / ~8327 | `ident` | `status == "D"`, whole-file deletion loop |
| ~8357 / ~8369 | `old_module_ident` | `status == "R"`, old module |
| ~8496 / ~8505 | `ident` | `removed_idents` loop (child removed, file survives) |
| ~8564 / ~8572 | `old_ident` | `renamed_pairs` loop |
| ~8603 / ~8610 | `ident` | `renamed_old_paths` pass |
| ~8677 / ~8687 | `ext_ident` | gitlink `"remove"` |

Example, for the `status == "D"` site:

```python
                close_items.append(
                    (_build_close_triples(
                        ident, desc, module_ident,
                        state.field_class_ident.get(ident),
                        close_entity_type=True, file_value=file_path,
                        is_static=state.field_static_ident.get(ident),
                        introduced_by=_resolve_introduced_by(db, state, ident),
                    ), orig_ts)
                )
                _forget_closed_entity(
                    ident, file_path, state.entity_valid_from,
                    state.entity_descriptions, state.field_class_ident, state.file_entities,
                    state.field_static_ident, state.entity_introduced_by,
                )
```

The gitlink `"remove"` site (~8677) currently passes only six arguments to
`_forget_closed_entity` (no `field_static_ident`). Pass
`state.field_static_ident` explicitly there so
`state.entity_introduced_by` lands in the right position:

```python
            _forget_closed_entity(
                ext_ident, path, state.entity_valid_from,
                state.entity_descriptions, state.field_class_ident, state.file_entities,
                state.field_static_ident, state.entity_introduced_by,
            )
```

Do **not** add `introduced_by` to the two `:depends-on` close sites or the
`:pinned-commit` close site — those close a single non-entity triple and pass a
literal list, not `_build_close_triples` output.

- [ ] **Step 5: Also pass the state dict into `_forward_apply`'s `_build_code_triples` call**

At ~8451, add `state.entity_introduced_by` as the 11th positional argument so
the forward walk actually populates it. Leave `_reverse_apply`'s call (~8003)
alone — it passes nothing, so it keeps the `None` default, which is required.

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py::TestCloseRetractsIntroducedBy tests/test_mcp_server.py::TestClosedEntityLifecyclePurge -v`
Expected: all pass.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: all pass. If a bi-temporal history test fails, check that
`_ingest_close` re-transacts the `:introduced-by` with the entity's window —
history must be preserved, only the *current* view changes.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Retract :introduced-by at every entity close site (#231)

Closes #231. A closed-and-purged entity no longer answers a bare
[?e :introduced-by ?c] query; the historical fact is preserved with its valid
window by _ingest_close, so point-in-time queries are unaffected.

Entities Stream 2 introduced during the same run were never written through the
forward walk state, so close sites resolve the retract value through
_resolve_introduced_by, which falls back to a per-close DB read.

Closes #231

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Discard the lineage marker on close

**Files:**
- Modify: `mcp_server.py` — `_forward_apply`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Task 4's wired close sites.
- Produces: nothing new; `_forward_apply` collects closed idents and calls the existing `_lineage_confirm_batch` once per commit.

- [ ] **Step 1: Write the failing test**

```python
class TestCloseDiscardsLineageMarker:
    """A closed entity must not leave its :type/lineage-marker behind.

    _lineage_is_provisional is the SOLE authority for reconcilability since
    #235, so a stale marker makes a re-introduction at the same ident read as
    provisional -- and _forward_reconcile_provisional then re-dates structural
    facts against a lineage guess that belongs to the DEAD entity.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("variant", ["rename", "delete"])
    async def test_reintroduced_ident_is_not_provisional(self, tmp_path, monkeypatch, variant):
        import mcp_server
        repo = _reused_path_repo(tmp_path / "repo", variant)
        graph = str(repo / "memory.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        mcp_server._db = None
        mcp_server._graph_path = graph
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None, "prior_ingested": 0,
        }
        await mcp_server._run_ingestion(str(repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete", mcp_server._ingest_progress

        db = mcp_server.get_db()
        module_ident = mcp_server._code_ident("module", "a.py")
        assert mcp_server._lineage_is_provisional(db, module_ident) is False, (
            f"[{variant}] re-created module inherited a stale provisional marker"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py::TestCloseDiscardsLineageMarker -v`
Expected: FAIL for at least one variant. **If both variants pass**, the fixture
does not reach a state where a provisional entity is closed. Do not delete the
test — keep it as a guard, note in its docstring that it passes pre-fix on this
fixture, and implement Step 3 anyway; the batch call is still required for the
at-scale path where Stage B closes provisional entities.

- [ ] **Step 3: Collect and batch the discard**

In `_forward_apply`, next to `close_items`:

```python
    closed_idents: List[str] = []  # idents closed this commit (lineage discard)
```

At each of the six close sites, immediately after the `_forget_closed_entity`
call, append the same ident variable used there (`ident`, `old_module_ident`,
`ident`, `old_ident`, `ident`, `ext_ident`):

```python
                closed_idents.append(ident)
```

Then, immediately after the loop that transacts `close_items` (find it by the
`_ingest_close(` call in `_forward_apply`), add:

```python
    # A closed entity must not leave its :type/lineage-marker behind:
    # _lineage_is_provisional is the sole authority for reconcilability
    # (#235), so a stale marker makes a re-introduction at the same ident read
    # as provisional and hands _forward_reconcile_provisional a guess that
    # belongs to the dead entity.
    #
    # Batched once per commit rather than per close site: the batch form
    # issues ONE retract for the whole set (idents with no marker are skipped),
    # where six per-site calls would issue up to six. "confirm" is the
    # existing name for "retract the marker" -- semantically it is a discard
    # here, but delegating to the batch keeps the two from drifting (#233).
    if closed_idents:
        _lineage_confirm_batch(db, closed_idents, index_con=index_con)
```

Verify `index_con` is in scope in `_forward_apply`; if the parameter is named
differently, use the local name.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py::TestCloseDiscardsLineageMarker -v`
Expected: 2 passed

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: all pass. Pay attention to `TestReverseFillValidTimeParity` and the
correction-sweep suites — they pin provisional-marker behaviour.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Discard a closed entity's lineage marker (#231)

_lineage_is_provisional is the sole authority for reconcilability since #235,
so a marker left behind by a close makes a re-introduction at the same ident
read as provisional and hands _forward_reconcile_provisional a lineage guess
belonging to the dead entity. Batched once per commit.

Refs #231

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Position-filter `_preload_known_entities`

The core of #238.

**Files:**
- Modify: `mcp_server.py` — `_preload_known_entities`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_preload_known_entities(db, repo_path, valid_at=None, hash_to_pos=None, watermark_pos=None) -> tuple` returning **five** values in this order: `(entity_valid_from, entity_descriptions, entity_introduced_by, file_entities, submodule_paths)`.

**`submodule_paths` MUST stay last.** An existing test at
`tests/test_mcp_server.py:9390` destructures with `*_, submodule_paths = ...`,
which silently binds the wrong value if anything is appended after it. Insert
`entity_introduced_by` third, not last.

- [ ] **Step 1: Write the failing tests**

```python
class TestPreloadKnownEntitiesPositionBound:
    """#238: the preload's resume bound must be POSITION-indexed, not
    author-date valid-time.

    Author dates are not monotonic in topological order (_git_commits reads
    %at, not %ct; a rebase, cherry-pick or late-merged branch carries the
    original author date forward). Measured on this repo: of 552 watermark
    positions, 6 (118-123) have a strictly-earlier-dated LATER position, and
    124-128 are confirmed descendants of them.

    Linearization used throughout: position 0 = c0, 1 = c1 (the watermark),
    2 = c2. c2 is ABOVE the watermark but dated EARLIER than c1 -- the
    inversion.
    """

    LINEARIZATION = ["c0" * 20, "c1" * 20, "c2" * 20]
    HASH_TO_POS = {h: i for i, h in enumerate(LINEARIZATION)}
    WATERMARK_POS = 1
    T_HI = "2026-05-02T00:00:00Z"  # max(ts[0..1]) -- the monotone envelope

    def _seed(self, real_db):
        """c0 @ 2026-04-01, c1 (watermark) @ 2026-05-02, c2 (above) @ 2026-04-26.

        below_w:  introduced at c0 -- must always be present.
        later_dated_below_w: introduced at c1's own date; today's ts(W) bound
            keeps it, but a commit at/below W dated LATER than W would drop out.
        above_w:  introduced at c2, dated EARLIER than the watermark -- today's
            bound keeps it, which is #238's DATA-LOSS direction.
        """
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z"} '
            f'[[:commit/c0 :hash "{self.LINEARIZATION[0]}"] '
            '[:commit/c0 :date "2026-04-01T00:00:00Z"] '
            '[:module/below-w :entity-type :type/module] '
            '[:module/below-w :ident ":module/below-w"] '
            '[:module/below-w :path "below.py"] '
            '[:module/below-w :description "below.py"] '
            '[:module/below-w :introduced-by :commit/c0]])'
        )
        real_db.execute(
            '(transact {:valid-from "2026-05-02T00:00:00Z"} '
            f'[[:commit/c1 :hash "{self.LINEARIZATION[1]}"] '
            '[:commit/c1 :date "2026-05-02T00:00:00Z"] '
            '[:module/later-dated-below-w :entity-type :type/module] '
            '[:module/later-dated-below-w :ident ":module/later-dated-below-w"] '
            '[:module/later-dated-below-w :path "later.py"] '
            '[:module/later-dated-below-w :description "later.py"] '
            '[:module/later-dated-below-w :introduced-by :commit/c1]])'
        )
        # Above the watermark, dated EARLIER than it: the side-branch shape.
        real_db.execute(
            '(transact {:valid-from "2026-04-26T00:00:00Z"} '
            f'[[:commit/c2 :hash "{self.LINEARIZATION[2]}"] '
            '[:commit/c2 :date "2026-04-26T00:00:00Z"] '
            '[:module/above-w :entity-type :type/module] '
            '[:module/above-w :ident ":module/above-w"] '
            '[:module/above-w :path "above.py"] '
            '[:module/above-w :description "above.py"] '
            '[:module/above-w :introduced-by :commit/c2]])'
        )

    def test_entity_introduced_above_the_watermark_is_excluded(self, real_db, tmp_path):
        """#238's DATA-LOSS direction. Wrongly included, this entity is absent
        from the parse of the earlier commit being replayed, so the forward walk
        closes and _forget_closed_entity-purges it with an orig_ts LATER than
        the close's valid_to: an inverted valid interval, permanent loss."""
        import mcp_server
        self._seed(real_db)
        entity_valid_from, *_ = mcp_server._preload_known_entities(
            real_db, str(tmp_path), valid_at=self.T_HI,
            hash_to_pos=self.HASH_TO_POS, watermark_pos=self.WATERMARK_POS,
        )
        assert ":module/above-w" not in entity_valid_from
        assert ":module/below-w" in entity_valid_from

    def test_entity_at_the_watermark_position_is_included(self, real_db, tmp_path):
        """#238's benign direction. Excluded, replay mints a duplicate
        :introduced-by."""
        import mcp_server
        self._seed(real_db)
        entity_valid_from, *_ = mcp_server._preload_known_entities(
            real_db, str(tmp_path), valid_at=self.T_HI,
            hash_to_pos=self.HASH_TO_POS, watermark_pos=self.WATERMARK_POS,
        )
        assert ":module/later-dated-below-w" in entity_valid_from

    def test_the_envelope_alone_does_not_close_the_hole(self, real_db, tmp_path):
        """THE test that pins the design. Widening the date bound to the
        monotone envelope WITHOUT the position clause re-admits the above-W
        entity -- the 'add-back union' #238 warns produces a change that looks
        like a fix and isn't. The position clause is what closes data loss;
        the date bound only governs how widely entities closed above W are
        re-admitted. Do not delete this test to make a refactor pass."""
        import mcp_server
        self._seed(real_db)
        entity_valid_from, *_ = mcp_server._preload_known_entities(
            real_db, str(tmp_path), valid_at=self.T_HI,
        )
        assert ":module/above-w" in entity_valid_from

    def test_all_bounds_none_restores_unrestricted_behaviour(self, real_db, tmp_path):
        """A fresh graph has no watermark and wants the pre-#222 behaviour."""
        import mcp_server
        self._seed(real_db)
        entity_valid_from, *_ = mcp_server._preload_known_entities(
            real_db, str(tmp_path),
        )
        assert ":module/above-w" in entity_valid_from
        assert ":module/below-w" in entity_valid_from

    def test_unknown_hash_is_excluded(self, real_db, tmp_path):
        """An introducing commit absent from this linearization -- a rewritten
        or foreign history. Excluding is the benign direction."""
        import mcp_server
        self._seed(real_db)
        entity_valid_from, *_ = mcp_server._preload_known_entities(
            real_db, str(tmp_path), valid_at=self.T_HI,
            hash_to_pos={self.LINEARIZATION[0]: 0}, watermark_pos=self.WATERMARK_POS,
        )
        assert ":module/later-dated-below-w" not in entity_valid_from
        assert ":module/below-w" in entity_valid_from

    def test_returns_the_introducing_commit_ident(self, real_db, tmp_path):
        """#231's retract value, from #238's new bound variable."""
        import mcp_server
        self._seed(real_db)
        _vf, _desc, entity_introduced_by, _fe, _sp = mcp_server._preload_known_entities(
            real_db, str(tmp_path), valid_at=self.T_HI,
            hash_to_pos=self.HASH_TO_POS, watermark_pos=self.WATERMARK_POS,
        )
        expected = f":commit/{self.LINEARIZATION[0][:12]}"
        assert entity_introduced_by[":module/below-w"] == expected

    def test_submodule_paths_stays_last(self, real_db, tmp_path):
        """Guards tests that destructure with `*_, submodule_paths = ...`."""
        import mcp_server
        self._seed(real_db)
        result = mcp_server._preload_known_entities(real_db, str(tmp_path))
        assert len(result) == 5
        *_, submodule_paths = result
        assert isinstance(submodule_paths, dict)
        assert all(k.startswith(":module/") for k in submodule_paths)

    def test_stale_live_introduced_by_does_not_pull_a_dead_entity_in(self, real_db, tmp_path):
        """Pins the spec's no-migration claim.

        Graphs written before #231 hold closed entities whose :introduced-by
        was never retracted. That stale fact must not by itself pull such an
        entity into the preload: this query also requires the entity's :ident,
        :path and :description to be visible at the same bound, so the row
        appears only when the :ident window covers the bound -- i.e. exactly
        when the entity was closed ABOVE the watermark and SHOULD be included.

        Here below-w is closed at 2026-04-15, below the envelope, with its
        :introduced-by deliberately left open the way a pre-#231 graph would.
        """
        import mcp_server
        self._seed(real_db)
        mcp_server._ingest_close(
            real_db,
            ['[:module/below-w :ident ":module/below-w"]',
             '[:module/below-w :path "below.py"]',
             '[:module/below-w :description "below.py"]'],
            "2026-04-01T00:00:00Z",
            "2026-04-15T00:00:00Z",
            "close below-w without retracting :introduced-by (pre-#231 shape)",
        )
        assert mcp_server._entity_introduced_by_query(real_db, ":module/below-w") is not None, (
            "precondition: the stale live :introduced-by is the pre-#231 residue"
        )
        entity_valid_from, *_ = mcp_server._preload_known_entities(
            real_db, str(tmp_path), valid_at=self.T_HI,
            hash_to_pos=self.HASH_TO_POS, watermark_pos=self.WATERMARK_POS,
        )
        assert ":module/below-w" not in entity_valid_from
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestPreloadKnownEntitiesPositionBound -v`
Expected: FAIL — `TypeError: _preload_known_entities() got an unexpected
keyword argument 'hash_to_pos'` for most, and a 4-vs-5 unpack error for
`test_returns_the_introducing_commit_ident`.

- [ ] **Step 3: Implement**

Change the signature:

```python
def _preload_known_entities(
    db: Any,
    repo_path: str,
    valid_at: Optional[str] = None,
    hash_to_pos: Optional[Dict[str, int]] = None,
    watermark_pos: Optional[int] = None,
) -> tuple:
```

Add the new local beside the others:

```python
    entity_introduced_by: Dict[str, str] = {}
```

Add `?hash` to the query's `:find` and `[?c :hash ?hash]` to its `:where`:

```python
                f'(query [:find ?ident ?path ?desc ?date ?hash '
                f'{valid_at_clause}'
                f':where [?e :entity-type :type/{entity_type}] '
                f'[?e :ident ?ident] '
                f'[?e :{path_attr} ?path] '
                f'[?e :description ?desc] '
                f'[?e :introduced-by ?c] '
                f'[?c :date ?date] '
                f'[?c :hash ?hash]])',
```

Replace the row loop:

```python
            rows = json.loads(raw).get("results", [])
            for ident, path, desc, date, hash_ in rows:
                # #238: the resume bound, POSITION-indexed. This clause is
                # CONJUNCTIVE over every row -- that is what makes the widened
                # valid_at envelope safe, and it is the distinction #238
                # insists on. Wrong-INCLUSION (the unrecoverable direction) is
                # caused solely by the introduction end, which this closes
                # exactly; the date bound above only governs how widely
                # entities closed ABOVE the watermark are re-admitted. Never
                # turn this into an "add-back" branch beside the date bound --
                # that re-admits the benign direction and leaves data loss
                # wide open.
                #
                # pos is None means the introducing commit is not in this
                # linearization (a rewritten or foreign history): exclude,
                # which is the benign direction.
                if watermark_pos is not None:
                    pos = hash_to_pos.get(hash_) if hash_to_pos is not None else None
                    if pos is None or pos > watermark_pos:
                        continue
                entity_valid_from[ident] = date
                entity_descriptions[ident] = desc
                entity_introduced_by[ident] = f":commit/{hash_[:12]}"
                file_entities.setdefault(path, [])
                if ident not in file_entities[path]:
                    file_entities[path].append(ident)
                if entity_type == "external-dependency":
                    submodule_paths[ident] = path
```

Change the return:

```python
    return (
        entity_valid_from, entity_descriptions, entity_introduced_by,
        file_entities, submodule_paths,
    )
```

Replace the `valid_at` docstring block. Delete the old "Known residual" and
"The residual therefore runs in BOTH directions" paragraphs entirely — they
describe the bug this task fixes — and put in their place:

```
    valid_at + hash_to_pos + watermark_pos together bound this query to the
    graph as it stood at the forward walk's RESUME POSITION. Before the
    two-stream ingest a current-graph preload and a resume-position preload
    were the same thing; the reverse stream broke that by writing structural
    facts across the whole frontier-high region, with Stage B's lifecycle pass
    then applying that region's deletions and renames. Both directions of the
    mismatch corrupt the graph, and they are NOT equally severe:

      * an entity wrongly INCLUDED (born in the reverse region) is absent from
        the parse of the earlier commit being replayed, so it is closed and
        _forget_closed_entity-purged with an orig_ts LATER than the close's
        valid_to -- an inverted valid interval. UNRECOVERABLE.
      * an entity wrongly EXCLUDED (closed in the reverse region) makes replay
        take _build_code_triples' introduction branch and mint a second live
        :introduced-by. RECOVERABLE -- #235's correction sweep repairs it, and
        a still-provisional entity is reconciled in place by
        _forward_reconcile_provisional rather than duplicated.

    Wrong-inclusion is caused SOLELY by the introduction end, and the
    introduction position is exactly recoverable: [?e :introduced-by ?c]
    [?c :hash ?hash] -> hash_to_pos[?hash]. So watermark_pos closes the
    unrecoverable direction exactly, with no fact-model change (#238).

    The close END is not recoverable at all -- _ingest_close records a close
    as valid_to = commit_ts_iso and holds no reference to the closing commit.
    valid_at therefore stays, but is DEMOTED: it no longer carries the safety
    property, only "how widely do we re-admit entities closed above the
    watermark". Callers pass the monotone envelope T_hi(W) = max(ts[0..W])
    rather than ts(W), the widest value that still excludes every close at or
    below W (a close at position p <= W has valid_to = ts[p] <= T_hi(W), and
    :valid-at's half-open semantics require valid_at < valid_to).

    This is a REPLACEMENT of the old ts(W) bound, not a union with it. Read
    #238 before changing it: widening the date bound alone is the "add-back"
    that looks like a fix and isn't, and
    test_the_envelope_alone_does_not_close_the_hole pins exactly that.

    Residual, deliberately accepted: an entity introduced at or below W,
    deleted or renamed above W with a close date earlier than T_hi(W), where a
    prior run's Stage B already applied that deletion. It is excluded, so
    replay mints a duplicate :introduced-by -- the recoverable direction, left
    to #235's sweep.

    :depends-on and :pinned-commit carry no commit reference of any kind, so
    _preload_known_deps and _preload_pinned_commits get no position clause and
    stay at ts(W). Tracked as #245; do NOT widen them to the envelope without
    one, which would make their data-loss direction worse.

    Passing all three as None restores the pre-#222 behaviour exactly, which
    is what a fresh graph (no watermark) wants.
```

Also update the `Returns` line at the end of the docstring:

```
    Returns (entity_valid_from, entity_descriptions, entity_introduced_by,
    file_entities, submodule_paths). entity_introduced_by maps ident -> the
    :commit/... ident that introduced it, derived from the same ?hash the
    position clause uses -- #231's close-time retract value. submodule_paths
    stays LAST: an existing test destructures with `*_, submodule_paths`.
```

- [ ] **Step 4: Fix the existing callers and tests for the new arity**

`_load_ingestion_preload_state` unpacks four values; make it five (full wiring
is Task 8, this step only keeps the module importable and the suite green):

```python
    (
        entity_valid_from, entity_descriptions, entity_introduced_by,
        file_entities, submodule_paths,
    ) = _preload_known_entities(db, repo_path, valid_at=resume_valid_at)
```

and add `entity_introduced_by` to its return tuple, after `entity_descriptions`.
Then in `_run_ingestion`, add it to the matching unpack at ~9442 and pass
`entity_introduced_by=entity_introduced_by` to the `_ForwardWalkState(...)`
constructor.

Then grep the test file for other unpackings:

```bash
grep -n "_preload_known_entities(" tests/test_mcp_server.py
```

Fix each. `*_, submodule_paths = ...` sites need no change.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestPreloadKnownEntitiesPositionBound -v`
Expected: 8 passed

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: all pass. `_load_ingestion_preload_state` still passes `ts(W)` as
`valid_at` and no position arguments, so ingestion behaviour is unchanged until
Task 8.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Position-filter the forward walk's entity preload (#238)

Binds the introducing commit's :hash and excludes any entity whose
linearization position is above the resume position. That clause is
conjunctive over every row, so it closes #238's unrecoverable data-loss
direction exactly and lets the valid-time bound be widened to the monotone
envelope instead of carrying the safety property itself.

The same bound variable yields each entity's introducing commit ident, which
#231's close sites need as their retract value.

_load_ingestion_preload_state does not pass the new bounds yet.

Refs #238
Refs #231

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Decouple `_preload_unresolved_dep_idents`' subtrahend

Required by Task 6, not optional: once `submodule_paths` is position-filtered, a
real submodule born above the watermark drops out of the subtrahend but stays in
the minuend and is misclassified as a stub — the bogus-`:resolves-to` failure
that function's own docstring warns about, reintroduced.

**Files:**
- Modify: `mcp_server.py` — `_preload_unresolved_dep_idents`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_preload_unresolved_dep_idents(db, valid_at=None) -> Dict[str, str]` — the `submodule_paths` parameter is **removed**; the function now runs its own unbounded `:path`-bearing query as the subtrahend.

- [ ] **Step 1: Write the failing test**

Replace the body of the existing
`test_unresolved_stubs_share_the_resume_bound_with_submodule_paths` (~9345) —
its assertion depends on the bounded subtrahend this task removes — with:

```python
    def test_above_watermark_submodule_is_not_misclassified_as_a_stub(
        self, real_db, tmp_path,
    ):
        """#238 follow-on: stub-ness is "has no :path", a property of the
        ENTITY, not of the resume position.

        This function is a minuend whose subtrahend used to be
        _preload_known_entities' submodule_paths. Once that output is
        position-filtered (#238), a real submodule born above the watermark
        drops out of the subtrahend while staying in the minuend, and is
        misclassified as a stub -- reaching state.unresolved_dep_idents, where
        the replayed gitlink "add" handler's _submodule_path_matches_import
        check fires on it (a submodule's :description is `name or path`) and
        mints a bogus [:module/vendor-lib-extra :resolves-to :module/vendor-lib].

        The subtrahend is therefore its own UNBOUNDED :path query.
        """
        import mcp_server

        real_db.execute(
            '(transact {:valid-from "2026-01-01T00:00:00Z"} '
            '[[:module/vendor-lib :entity-type :type/external-dependency] '
            '[:module/vendor-lib :ident ":module/vendor-lib"] '
            '[:module/vendor-lib :path "vendor/lib"] '
            '[:module/vendor-lib :description "vendor/lib"] '
            # A genuine unresolved-import stub: no :path, ever.
            '[:module/pkg-missing :entity-type :type/external-dependency] '
            '[:module/pkg-missing :ident ":module/pkg-missing"] '
            '[:module/pkg-missing :description "pkg.missing"]])'
        )
        # Above the watermark: what a prior run's Stage B leaves behind.
        real_db.execute(
            '(transact {:valid-from "2026-06-01T00:00:00Z"} '
            '[[:module/vendor-lib-extra :entity-type :type/external-dependency] '
            '[:module/vendor-lib-extra :ident ":module/vendor-lib-extra"] '
            '[:module/vendor-lib-extra :path "vendor/lib/extra"] '
            '[:module/vendor-lib-extra :description "vendor/lib/extra"]])'
        )

        stubs = mcp_server._preload_unresolved_dep_idents(
            real_db, valid_at="2026-03-01T00:00:00Z",
        )
        assert stubs == {":module/pkg-missing": "pkg.missing"}, (
            "a real (:path-bearing) submodule must never be classified as a stub, "
            "regardless of which side of the watermark it was born on"
        )

    def test_stub_above_the_watermark_is_excluded_by_the_bound(self, real_db, tmp_path):
        """For stubs, MISSING is benign (a link the forward-only oracle would
        not have made either) while EXTRA is harmful (a bogus :resolves-to), so
        this minuend keeps the NARROWER ts(W) bound rather than #238's widened
        envelope."""
        import mcp_server
        real_db.execute(
            '(transact {:valid-from "2026-06-01T00:00:00Z"} '
            '[[:module/pkg-late :entity-type :type/external-dependency] '
            '[:module/pkg-late :ident ":module/pkg-late"] '
            '[:module/pkg-late :description "pkg.late"]])'
        )
        stubs = mcp_server._preload_unresolved_dep_idents(
            real_db, valid_at="2026-03-01T00:00:00Z",
        )
        assert stubs == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py -k "not_misclassified_as_a_stub or stub_above_the_watermark" -v`
Expected: FAIL — `TypeError: _preload_unresolved_dep_idents() got an unexpected
keyword argument 'valid_at'` (it is currently the third *positional* parameter
after `submodule_paths`).

- [ ] **Step 3: Implement**

```python
def _preload_unresolved_dep_idents(
    db: Any, valid_at: Optional[str] = None
) -> Dict[str, str]:
    """Reload ident -> import_name for every unresolved-import stub (#112).

    _preload_known_entities' external-dependency branch requires a :path fact,
    which only real submodule entities have — unresolved-import stubs (see
    _forward_apply's dep-edge handling) never get one, so they're invisible to
    that query. This runs the same :entity-type match WITHOUT the :path
    requirement, then subtracts the idents that DO have a :path to get exactly
    the stub idents, restart-safe.

    Needed so a submodule added in a later, separate ingestion run can still
    find and link any stub created in an earlier run (see the gitlink "add"
    handling in _forward_apply) — without this, only same-run stubs would ever
    get linked.

    The subtrahend is its OWN unbounded query, deliberately not
    _preload_known_entities' submodule_paths (#238). Stub-ness is "has no
    :path" — a property of the entity, not of the resume position — and once
    submodule_paths became position-filtered, sharing it would drop a real
    submodule born above the watermark out of the subtrahend while it stayed
    in the minuend. That misclassifies it as a stub, reaching
    state.unresolved_dep_idents, where the replayed gitlink "add" handler's
    _submodule_path_matches_import check can fire on it (a submodule's
    :description is `name or path`) and mint a bogus
    [:module/sub-b :resolves-to :module/sub-a].

    The MINUEND keeps the narrow ts(W) bound rather than #238's widened
    envelope, because this set's asymmetry runs the other way from
    _preload_known_entities': an EXTRA entry is the bogus edge above, while a
    MISSING one is merely a link the forward-only oracle would not have made
    at that position either. Narrower is safer here.
    """
    unresolved: Dict[str, str] = {}
    valid_at_clause = f':valid-at "{_edn_escape(valid_at)}" ' if valid_at else ""
    path_bearing: set = set()
    try:
        raw = _db_execute(
            db,
            "(query [:find ?ident "
            ":where "
            "[?e :entity-type :type/external-dependency] "
            "[?e :ident ?ident] "
            "[?e :path ?path]])",
        )
        path_bearing = {row[0] for row in json.loads(raw).get("results", [])}
    except Exception:
        return unresolved
    try:
        raw = _db_execute(
            db,
            "(query [:find ?ident ?desc "
            f"{valid_at_clause}"
            ":where "
            "[?e :entity-type :type/external-dependency] "
            "[?e :ident ?ident] "
            "[?e :description ?desc]])",
        )
        for ident, desc in json.loads(raw).get("results", []):
            if ident not in path_bearing:
                unresolved[ident] = desc
    except Exception:
        pass
    return unresolved
```

The subtrahend query's `except` returns early rather than falling through: an
empty `path_bearing` would classify every real submodule as a stub, which is
the harmful direction.

- [ ] **Step 4: Update the caller**

In `_load_ingestion_preload_state`, drop the `submodule_paths` argument:

```python
    unresolved_dep_idents = _preload_unresolved_dep_idents(
        db, valid_at=resume_valid_at,
    )
```

and delete the now-stale comment above it ("Same bound as
_preload_known_entities above, and not optional: this preload subtracts
submodule_paths, which that call already bounded."), replacing it with:

```python
    # Independent of _preload_known_entities' bound now (#238): the subtrahend
    # is that function's own unbounded :path query, so stub classification no
    # longer moves with the resume position. Keeps ts(W), not the envelope --
    # see its docstring for why narrower is safer for this set.
```

This also means the call no longer has to follow `_preload_known_entities`.
Leave its position alone anyway — reordering is churn.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py -k "unresolved or stub" -v`
Expected: all pass.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Decouple stub classification from the resume bound (#238)

_preload_unresolved_dep_idents subtracted _preload_known_entities'
submodule_paths. Once that output is position-filtered, a real submodule born
above the watermark drops out of the subtrahend while staying in the minuend
and is misclassified as a stub -- reaching the gitlink "add" handler, which
mints a bogus :resolves-to.

The subtrahend is now its own unbounded :path query. Stub-ness is a property
of the entity, not of the resume position.

Refs #238

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Wire the linearization into the preload

**Files:**
- Modify: `mcp_server.py` — `_load_ingestion_preload_state`, `_run_ingestion`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Task 6's `_preload_known_entities` signature, Task 3's state field.
- Produces: `_load_ingestion_preload_state(repo_path: str, linearization: List[str], commit_metadata: List[Tuple[str, str, str, str]]) -> tuple` — returns 13 values, `entity_introduced_by` inserted after `entity_descriptions`.

- [ ] **Step 1: Write the failing test**

```python
class TestPreloadStateLinearizationWiring:
    """#238: the linearization must reach the preload.

    It did not before, for a purely mechanical reason:
    _load_ingestion_preload_state ran BEFORE build_linearization was called, so
    the positions did not exist yet. build_linearization needs only repo_path
    and branch, no DB handle, so both git enumerations move above the preload
    block -- and above the DB open, so the graph file lock is held no longer
    than it was.
    """

    def test_envelope_is_the_max_date_at_or_below_the_watermark(self):
        """T_hi(W) = max(ts[0..W]), not ts(W). Timestamps are fixed-width UTC
        from _git_commits, so lexicographic max is chronological max."""
        import mcp_server
        commit_metadata = [
            ("a" * 40, "2026-04-01T00:00:00Z", "a@e", "c0"),
            ("b" * 40, "2026-05-02T00:00:00Z", "a@e", "c1"),
            ("c" * 40, "2026-04-26T00:00:00Z", "a@e", "c2"),
        ]
        assert mcp_server._resume_envelope(commit_metadata, 1) == "2026-05-02T00:00:00Z"
        # The inversion: at W=0 the envelope is ts(0), and c2 (position 2,
        # dated EARLIER than c1) is still correctly above it by POSITION.
        assert mcp_server._resume_envelope(commit_metadata, 0) == "2026-04-01T00:00:00Z"

    def test_envelope_of_none_position_is_none(self):
        import mcp_server
        assert mcp_server._resume_envelope([], None) is None

    @pytest.mark.asyncio
    async def test_preload_state_accepts_and_uses_the_linearization(self, git_repo, monkeypatch):
        """Smoke test that the new parameters are threaded, on a real graph."""
        import mcp_server
        from minigraf import frontier_registry
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._ingest_progress = {
            "status": "idle", "processed": 0, "total": 0,
            "current_commit": "", "error": None,
        }
        await mcp_server._run_ingestion(str(git_repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete"

        mcp_server._db = None
        linearization = frontier_registry.build_linearization(str(git_repo), "HEAD")
        commit_metadata = mcp_server._git_commits(str(git_repo), None, "HEAD")
        result = mcp_server._load_ingestion_preload_state(
            str(git_repo), linearization, commit_metadata,
        )
        assert len(result) == 13
        # (watermark, prior_ingested, entity_valid_from, entity_descriptions,
        #  entity_introduced_by, ...) -- index 4, not 3.
        entity_introduced_by = result[4]
        assert entity_introduced_by, "the preload must seed entity_introduced_by"
        assert all(v.startswith(":commit/") for v in entity_introduced_by.values())

    @pytest.mark.asyncio
    async def test_misaligned_metadata_raises(self, git_repo):
        """A misaligned pair silently mis-filters the ENTIRE preload, which is
        worse than the misattribution _reverse_apply's own check prevents."""
        import mcp_server
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(git_repo / "memory.graph"))
        mcp_server._db = None
        with pytest.raises(ValueError, match="positionally aligned"):
            mcp_server._load_ingestion_preload_state(
                str(git_repo), ["a" * 40, "b" * 40], [("a" * 40, "2026-01-01T00:00:00Z", "e", "s")],
            )
```

If the suite has no `git_repo` fixture with more than one commit, reuse whatever
multi-commit fixture `test_resumes_from_watermark_after_shutdown` uses.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestPreloadStateLinearizationWiring -v`
Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_resume_envelope'`

- [ ] **Step 3: Add the envelope helper**

Immediately above `_load_ingestion_preload_state`:

```python
def _resume_envelope(
    commit_metadata: List[Tuple[str, str, str, str]], watermark_pos: Optional[int]
) -> Optional[str]:
    """T_hi(W) = max(ts[0..W]) -- the monotone envelope of every author date at
    or below the resume position, and the valid-time bound
    _preload_known_entities takes (#238).

    NOT ts(W). Author dates are not monotonic in topological order
    (_git_commits reads %at, not %ct), so ts(W) does not cleanly separate "at
    or below the resume position" from "above it". The envelope is the widest
    bound that still excludes every close at or below W: such a close has
    valid_to = ts[p] <= T_hi(W), and :valid-at's half-open semantics require
    valid_at < valid_to.

    Widening the bound this way is only safe BECAUSE _preload_known_entities
    also applies a conjunctive position clause. Alone it is the "add-back
    union" #238 warns produces a change that looks like a fix and isn't -- see
    test_the_envelope_alone_does_not_close_the_hole.

    Timestamps are fixed-width UTC ("%Y-%m-%dT%H:%M:%SZ") from _git_commits, so
    lexicographic max is chronological max.

    Returns None for a None position (a fresh graph, no watermark), which
    degrades the preload to its pre-#222 unrestricted form.
    """
    if watermark_pos is None:
        return None
    window = commit_metadata[: watermark_pos + 1]
    if not window:
        return None
    return max(ts for _hash, ts, _author, _subject in window)
```

- [ ] **Step 4: Rewrite `_load_ingestion_preload_state`**

New signature and body head:

```python
def _load_ingestion_preload_state(
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
) -> tuple:
```

Add to its docstring, after the existing paragraphs:

```
    linearization and commit_metadata are #238's resume bound. They are
    supplied by the caller rather than built here because the git enumeration
    must not run while this function holds the graph file lock -- see
    _run_ingestion, which now runs both enumerations before the preload rather
    than after it. That ordering, not any preference for valid-time, is the
    whole reason the bound used to be expressed in author dates: the
    linearization simply did not exist yet when this ran.

    Both are validated for positional alignment before use. _reverse_apply
    performs the same check for the same reason (silent, systematic error), but
    the consequence here is worse: a misaligned pair mis-filters the ENTIRE
    preload rather than misattributing one commit.
```

Replace the bound computation. Delete the old `resume_valid_at` comment block
(the one ending "...rather than to an empty state.") and put in its place:

```python
    if len(commit_metadata) != len(linearization):
        raise ValueError(
            "commit_metadata must be positionally aligned with linearization "
            f"(got {len(commit_metadata)} entries vs {len(linearization)}); "
            "a misaligned pair mis-filters the entire preload (#238)"
        )
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    watermark_pos = hash_to_pos.get(watermark) if watermark is not None else None

    # #238: two DIFFERENT bounds now, deliberately.
    #
    # resume_valid_at is ts(W), the watermark commit's own :date. It is what
    # :depends-on and :pinned-commit still use, because those facts carry no
    # commit reference of any kind and so admit no position clause. Widening
    # THEM to the envelope without one would make their data-loss direction
    # worse -- exactly the union #238 forbids. Tracked as #245.
    #
    # entity_valid_at is the monotone envelope T_hi(W) = max(ts[0..W]), which
    # is safe only because _preload_known_entities pairs it with the
    # conjunctive position clause below. A None watermark_pos (fresh graph, or
    # a watermark absent from this linearization -- a rewritten history)
    # degrades both to the pre-#222 unrestricted queries rather than to an
    # empty state.
    resume_valid_at = _commit_date_query(db, watermark)
    resume_valid_at_ms = _iso_to_epoch_ms(resume_valid_at)
    entity_valid_at = _resume_envelope(commit_metadata, watermark_pos)
    if entity_valid_at is None:
        entity_valid_at = resume_valid_at
```

Then the preload calls:

```python
    (
        entity_valid_from, entity_descriptions, entity_introduced_by,
        file_entities, submodule_paths,
    ) = _preload_known_entities(
        db, repo_path, valid_at=entity_valid_at,
        hash_to_pos=hash_to_pos, watermark_pos=watermark_pos,
    )
```

and the return tuple, with `entity_introduced_by` third:

```python
    return (
        watermark, prior_ingested, entity_valid_from, entity_descriptions,
        entity_introduced_by, file_entities, file_deps, dep_valid_from,
        pinned_commit_state, field_class_ident, field_static_ident,
        submodule_paths, unresolved_dep_idents,
    )
```

- [ ] **Step 5: Reorder and rewire `_run_ingestion`**

Move `linearization = frontier_registry.build_linearization(...)`,
`commit_metadata = _git_commits(repo_path, None, branch)` and
`ignore_patterns = _load_ignore_patterns(repo_path)` to **above** the
`with concurrent.futures.ThreadPoolExecutor(max_workers=1) as preload_executor:`
block, and delete them from their old position below `_db = None`. Keep the
`# FULL history, positionally aligned with linearization.` comment with
`commit_metadata`, and add above `build_linearization`:

```python
        # Enumerated BEFORE the preload (#238), which needs the positions to
        # bound its queries. Above the DB open too, not merely above the
        # `_db = None` lock release, so the graph file lock is held for no
        # longer than it was. These are git subprocesses and touch no DB.
```

Update the unpack:

```python
            (
                watermark, prior_ingested, entity_valid_from, entity_descriptions,
                entity_introduced_by, file_entities, file_deps, dep_valid_from,
                pinned_commit_state, field_class_ident, field_static_ident,
                submodule_paths, unresolved_dep_idents,
            ) = await loop.run_in_executor(
                preload_executor, _load_ingestion_preload_state,
                repo_path, linearization, commit_metadata,
            )
```

and add to the `_ForwardWalkState(...)` constructor call:

```python
            entity_introduced_by=entity_introduced_by,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestPreloadStateLinearizationWiring -v`
Expected: 4 passed

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: all pass. Any test calling `_load_ingestion_preload_state` directly
needs the two new arguments — grep for it and fix each.

- [ ] **Step 8: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Thread the linearization into the forward walk preload (#238)

The bound was expressed in author-date valid-time only because
_load_ingestion_preload_state ran twelve lines BEFORE build_linearization, so
the positions did not exist yet. Both git enumerations now run before the
preload -- and before the DB open, so the graph file lock is held no longer
than before.

The entity preload takes the monotone envelope max(ts[0..W]) plus the position
clause. :depends-on and :pinned-commit keep ts(W): they carry no commit
reference, so they admit no position clause, and widening them alone would
make their data-loss direction worse. Tracked as #245.

Refs #238
Refs #245

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: End-to-end resume regression test

#238 requires this specifically: "any regression test for this needs to
construct the resume explicitly, not rely on a fresh ingestion." Every per-task
review during #222 phase 2d saw only fresh runs and structurally could not
observe the bug.

**Files:**
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `_inverted_author_date_repo(path) -> Path` fixture builder.

- [ ] **Step 1: Write the test**

Place the builder next to `_reused_path_repo`:

```python
def _inverted_author_date_repo(path):
    """A repo whose topological order and AUTHOR-date order disagree.

    Reproduces #238's measured shape on this repository: positions 118-123 have
    a strictly-earlier-dated LATER position (124-128, a side branch authored
    2026-04-26/27 that lands topologically after the 2026-05-02 merges, and is
    a confirmed descendant of them).

    Four commits, topological order c0 -> c1 -> c2 -> c3, with c2 and c3
    authored EARLIER than c1:

        pos 0  c0  base.py            @ 2026-04-01
        pos 1  c1  mid.py             @ 2026-05-02   <- the resume watermark
        pos 2  c2  late.py            @ 2026-04-26   <- above W, dated earlier
        pos 3  c3  modifies base.py   @ 2026-04-27   <- above W, dated earlier

    GIT_AUTHOR_DATE drives valid-time (_git_commits reads %at);
    GIT_COMMITTER_DATE is kept monotonic so the topological order is
    unambiguous.
    """
    path.mkdir(parents=True, exist_ok=True)
    _subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path,
                    check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=path,
                    check=True, capture_output=True)

    def commit(filename, body, author_date, committer_date):
        (path / filename).write_text(body)
        _subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        env = {**os.environ,
               "GIT_AUTHOR_DATE": author_date, "GIT_COMMITTER_DATE": committer_date}
        _subprocess.run(["git", "commit", "-m", filename], cwd=path,
                        check=True, capture_output=True, env=env)

    commit("base.py", "def base_fn():\n    return 1\n",
           "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z")
    commit("mid.py", "def mid_fn():\n    return 2\n",
           "2026-05-02T00:00:00Z", "2026-05-03T00:00:00Z")
    commit("late.py", "def late_fn():\n    return 3\n",
           "2026-04-26T00:00:00Z", "2026-05-04T00:00:00Z")
    commit("base.py", "def base_fn():\n    return 1\n\ndef base_fn2():\n    return 4\n",
           "2026-04-27T00:00:00Z", "2026-05-05T00:00:00Z")
    return path
```

Then the test class:

```python
class TestResumeWithInvertedAuthorDates:
    """#238 end-to-end: a RESUMED run whose watermark sits at an inverted
    position must not corrupt the graph.

    The failure needs a resumed run; a fresh ingestion cannot show it. It is
    constructed explicitly here, which is what #238 asks for.
    """

    def _progress(self):
        return {"status": "idle", "processed": 0, "total": 0,
                "current_commit": "", "error": None, "prior_ingested": 0}

    @staticmethod
    def _results(db, datalog):
        return json.loads(db.execute(datalog))["results"]

    @pytest.mark.asyncio
    async def test_resumed_run_does_not_close_a_future_entity(self, tmp_path, monkeypatch):
        """The DATA-LOSS direction. late_fn is introduced at position 2, above
        the watermark, dated EARLIER than it. Under the old author-date bound
        it stayed in the preload snapshot, was absent from the parse of the
        earlier commit being replayed, and was closed and
        _forget_closed_entity-purged -- with an orig_ts LATER than the close's
        valid_to, an inverted valid interval."""
        import mcp_server
        repo = _inverted_author_date_repo(tmp_path / "repo")
        graph = str(repo / "memory.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        # Forward-only, so the watermark advances one commit at a time and the
        # resume position is deterministic.
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", f"{10**6}:1")
        mcp_server._db = None
        mcp_server._graph_path = graph

        # Run 1: stop after the first commit, leaving the watermark at pos 0.
        mcp_server._ingest_progress = self._progress()
        original_sleep = asyncio.sleep
        stop_once = {"done": False}

        async def stop_after_first(t):
            if not stop_once["done"]:
                stop_once["done"] = True
                mcp_server._shutdown_requested.set()
            await original_sleep(t)

        with patch("mcp_server.asyncio.sleep", stop_after_first):
            await mcp_server._run_ingestion(str(repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "stopped"

        # Run 2: resume and finish.
        mcp_server._ingest_progress = self._progress()
        await mcp_server._run_ingestion(str(repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete", mcp_server._ingest_progress

        mcp_server._db = None
        from minigraf import MiniGrafDb
        db = MiniGrafDb.open(graph)

        for ident in [
            mcp_server._code_ident("function", "late.py", "late_fn"),
            mcp_server._code_ident("function", "base.py", "base_fn2"),
            mcp_server._code_ident("function", "mid.py", "mid_fn"),
        ]:
            live = self._results(db, f'(query [:find ?i :where [{ident} :ident ?i]])')
            assert live == [[ident]], (
                f"{ident} lost its :ident across a resumed run at an inverted "
                f"author-date position (#238): {live}"
            )

    @pytest.mark.asyncio
    async def test_resumed_run_mints_no_duplicate_introduced_by(self, tmp_path, monkeypatch):
        """The other direction. No entity may hold two live :introduced-by
        values -- that is #235's corruption, reachable through #238's preload."""
        import mcp_server
        repo = _inverted_author_date_repo(tmp_path / "repo")
        graph = str(repo / "memory.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", f"{10**6}:1")
        mcp_server._db = None
        mcp_server._graph_path = graph

        mcp_server._ingest_progress = self._progress()
        original_sleep = asyncio.sleep
        stop_once = {"done": False}

        async def stop_after_first(t):
            if not stop_once["done"]:
                stop_once["done"] = True
                mcp_server._shutdown_requested.set()
            await original_sleep(t)

        with patch("mcp_server.asyncio.sleep", stop_after_first):
            await mcp_server._run_ingestion(str(repo), "HEAD")
        mcp_server._ingest_progress = self._progress()
        await mcp_server._run_ingestion(str(repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete"

        mcp_server._db = None
        from minigraf import MiniGrafDb
        db = MiniGrafDb.open(graph)

        rows = self._results(
            db, '(query [:find ?i (count ?c) :where [?e :ident ?i] [?e :introduced-by ?c]])')
        multi = [(i, n) for i, n in rows if n > 1]
        assert multi == [], f"entities with more than one live :introduced-by: {multi}"

    @pytest.mark.asyncio
    async def test_resumed_run_writes_no_inverted_valid_interval(self, tmp_path, monkeypatch):
        """The purge's signature: a close whose valid_to precedes its
        valid_from."""
        import mcp_server
        repo = _inverted_author_date_repo(tmp_path / "repo")
        graph = str(repo / "memory.graph")
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", graph)
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", f"{10**6}:1")
        mcp_server._db = None
        mcp_server._graph_path = graph

        mcp_server._ingest_progress = self._progress()
        original_sleep = asyncio.sleep
        stop_once = {"done": False}

        async def stop_after_first(t):
            if not stop_once["done"]:
                stop_once["done"] = True
                mcp_server._shutdown_requested.set()
            await original_sleep(t)

        with patch("mcp_server.asyncio.sleep", stop_after_first):
            await mcp_server._run_ingestion(str(repo), "HEAD")
        mcp_server._ingest_progress = self._progress()
        await mcp_server._run_ingestion(str(repo), "HEAD")
        assert mcp_server._ingest_progress["status"] == "complete"

        mcp_server._db = None
        from minigraf import MiniGrafDb
        db = MiniGrafDb.open(graph)

        rows = self._results(
            db,
            '(query [:find ?i ?vf ?vt :any-valid-time '
            ':where [?e :ident ?i] [?e :db/valid-from ?vf] [?e :db/valid-to ?vt]])',
        )
        inverted = [(i, vf, vt) for i, vf, vt in rows if vt is not None and vt < vf]
        assert inverted == [], f"inverted valid intervals on :ident facts: {inverted}"
```

`os` and `_subprocess` are already imported at module scope in the test file —
`git_repo_diamond_clock_skewed` uses both. Match that fixture's style
(`capture_output=True` everywhere, `{**os.environ, ...}` for the date env).

- [ ] **Step 2: Run the tests**

Run: `python -m pytest tests/test_mcp_server.py::TestResumeWithInvertedAuthorDates -v`
Expected: 3 passed.

- [ ] **Step 3: Prove the tests actually pin the bug**

Temporarily revert the position clause — in `_preload_known_entities`, comment
out the `if watermark_pos is not None:` block — and re-run:

Run: `python -m pytest tests/test_mcp_server.py::TestResumeWithInvertedAuthorDates -v`
Expected: **at least one FAILS.** If all three still pass, the fixture does not
reach the inverted window; adjust the stop position (which commit run 1 stops
after) or add a commit until one fails, then restore the block.

Do not proceed until you have seen a failure here. A green regression test that
cannot fail is worse than none — it is the exact structural blind spot #238
describes.

- [ ] **Step 4: Restore the position clause and re-run**

Run: `python -m pytest tests/test_mcp_server.py::TestResumeWithInvertedAuthorDates -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/test_mcp_server.py -x -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Add a resumed-run regression test for inverted author dates (#238)

Constructs the resume explicitly, as #238 requires: a fresh ingestion cannot
show this failure, which is why every per-task review during #222 phase 2d
missed it. Fixture reproduces the measured shape -- later positions authored
earlier than the watermark.

Verified to fail with the position clause commented out.

Refs #238

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Record the #245 residual and finish the docs pass

**Files:**
- Modify: `mcp_server.py` — `_preload_known_deps`, `_preload_pinned_commits` docstrings
- Modify: `docs/superpowers/specs/2026-08-04-position-indexed-preload-design.md` (only if implementation diverged)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Annotate `_preload_known_deps`**

Replace its `valid_at_ms` paragraph's closing with an added note:

```
    #238/#245: this bound is still ts(W), NOT the monotone envelope
    _preload_known_entities now takes, and it has no position clause. A
    :depends-on fact carries no commit reference of any kind -- its
    introduction timestamp comes from its own :db/valid-from -- so there is
    nothing to join to a :hash and no position to filter on. Widening it to
    the envelope WITHOUT a position clause would make its data-loss direction
    worse, which is exactly the union #238 forbids. It therefore retains
    #238's residual in both directions. Tracked as #245; do not "fix" this by
    widening the bound.
```

- [ ] **Step 2: Annotate `_preload_pinned_commits`**

Same, adapted:

```
    #238/#245: still ts(W), with no position clause, for the same reason
    _preload_known_deps has none -- a :pinned-commit fact carries no commit
    reference to derive a linearization position from. Retains #238's residual
    in both directions: a bump recorded above the watermark can be closed
    against the wrong prior SHA. Tracked as #245. Do not widen this to
    _preload_known_entities' envelope without a position clause.
```

- [ ] **Step 3: Check the docs that could have drifted**

```bash
grep -rn "introduced-by\|preload\|watermark" SKILL.md CLAUDE.md
```

Per the spec, neither should need a change — no query syntax, attribute, or
tool surface changed. **Confirm that by reading the hits**, and if any describes
the preload bound or claims closed entities keep `:introduced-by`, fix it.

- [ ] **Step 4: Reconcile the spec with what was built**

Re-read `docs/superpowers/specs/2026-08-04-position-indexed-preload-design.md`
against the diff:

```bash
git diff master...HEAD -- mcp_server.py | head -400
```

If the implementation diverged (a different helper name, a different bound
placement), amend the spec so it describes what shipped. Do not amend the spec
to hide a divergence you should have flagged instead.

- [ ] **Step 5: Run the full suite one final time**

Run: `python -m pytest tests/test_mcp_server.py -q`
Expected: all pass, **no XPASS**, no XFAIL from the three markers removed in
Task 1.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py docs/
git commit -m "$(cat <<'EOF'
Record the :depends-on / :pinned-commit residual as #245

Both preload sites keep the ts(W) author-date bound because their facts carry
no commit reference to derive a linearization position from, so widening them
to #238's envelope without a position clause would make their data-loss
direction worse rather than better.

Refs #238
Refs #245

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification before the PR

- [ ] `python -m pytest tests/test_mcp_server.py -q` — all pass, no XPASS, no unexpected XFAIL.
- [ ] `grep -rn "xfail" tests/test_mcp_server.py | grep -i "introduced-by\|ghost"` — no hits.
- [ ] `grep -n "MINIGRAF_INGEST_STREAM_RATIO" tests/test_mcp_server.py` — the only remaining forward-only pins are ones unrelated to #231.
- [ ] `git log master..HEAD --format=%B | grep -iE "clos(e|es|ed)|fix(es|ed)?" ` — confirm **no** closing keyword targets #238. `Closes #231` in Task 4 is intended and correct.
- [ ] PR body says `Refs #238`, `Refs #245`, `Closes #231`. Never a closing keyword for #238, not even negated — a negated "does not close #N" has auto-closed an issue on this project.
- [ ] Branch protection: master needs an approving review on top of green CI. Ask before using `--admin`.

## Not in this plan

- **`:depends-on` / `:pinned-commit` position filtering** — #245, which #238 is blocked on.
- **The at-scale acceptance gate** (`evals/at_scale/run_ingestion_benchmark.py`) is **not runnable** until #242 is fixed: its in-flight poller blocks the event loop and can starve the ingestion it measures. Two #235 runs were killed at 3h54m and 36m on code measured at +3%. Do not gate this work on it. This change adds no per-ident-per-commit query, so it should be cost-neutral; if a cost check is wanted, use `evals/at_scale/profile_forward_reconcile_attribution.py`.
- **Cleaning stale live `:introduced-by` from existing graphs** — #244.
