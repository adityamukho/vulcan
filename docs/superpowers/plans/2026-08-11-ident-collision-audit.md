# #263 Ident Collision Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only audit that counts, exactly, how many `_code_ident` values in this repository's real history are reachable from more than one distinct `(entity_type, file_path, name)` input — and scores five candidate replacement derivations against the same data.

**Architecture:** One pass over branch history drives `mcp_server._extract_commit` (already read-only, stateless and DB-free) on a `ProcessPoolExecutor`, harvesting every `(entity_type, file_path, name)` input that ever existed plus every unresolved-import specifier. Those inputs are then grouped by the ident the current rule produces (Stage 2, with per-offender shape classification) and re-grouped under each candidate rule (Stage 3, scoring residual collisions and rename cost). Nothing is written to any graph; no `MiniGrafDb` handle is opened.

**Tech Stack:** Python 3.10+, pytest, `mcp_server`'s existing tree-sitter extraction pipeline, `concurrent.futures.ProcessPoolExecutor`.

**Spec:** `docs/superpowers/specs/2026-08-11-ident-collision-audit-design.md` — read it before Task 1. Where this plan and the spec disagree, the spec wins.

## Global Constraints

- **Interpreter is always `.venv/bin/python`.** The system `python` carries minigraf 1.1.1 against a `minigraf>=1.2.3` floor; it fakes ~122 test failures and misdiagnoses runs. Every command in this plan uses `.venv/bin/python`.
- **This audit opens no `MiniGrafDb` handle and writes no facts.** It is read-only by construction. Do not add a graph query "for cross-checking" — the spec's scope decisions rule that out, with a reason.
- **Never monkeypatch `mcp_server._canonical_ident` or `mcp_server._code_ident`.** Candidate rules are standalone pure functions in the probe module. The audit must not mutate the module it measures.
- **Entity types are exactly** `("module", "function", "class", "variable", "field")`. `external-dependency` is not a sixth type — it shares the `:module/` namespace (`mcp_server.py:7396`).
- **Where a mapping mirrors `_precompute_file_triples`** (`mcp_server.py:7044-7129`), that function is authoritative. Re-read it rather than trusting this plan's table.
- **Commit messages must contain no GitHub closing keyword** (`close`/`closes`/`closed`/`fix`/`fixes`/`fixed`/`resolve`/`resolves`/`resolved`). Use `Refs #263`. The keyword/`#N` pair is scanned across blank lines, so avoid the words entirely.
- **Every commit ends with:**
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01QRGNkzJc5FdfpbzAh4ZZzK
  ```
- **Branch:** `audit-263-ident-collision-census` (already created, spec already committed on it).
- **Ablation requirement.** Every test below names the counterfactual it was checked against. A test that passes against both the real implementation and a degenerate stub proves nothing — if you cannot make a test fail by degrading the code it guards, the test is wrong and must be rewritten before the task is done.

---

### Task 1: Input model, current-rule grouping, and offenders

**Files:**
- Create: `evals/at_scale/probe_ident_collision_census.py`
- Test: `tests/test_at_scale_ident_collision_census.py`

**Interfaces:**
- Consumes: `mcp_server._code_ident` (read-only import).
- Produces:
  - `EntityInput(NamedTuple)` with fields `entity_type: str`, `producer: str`, `file_path: str`, `name: Optional[str]`
  - `ENTITY_TYPES: Tuple[str, ...]`
  - `PRODUCERS: Tuple[str, ...]`
  - `raw_value(inp: EntityInput) -> str`
  - `current_ident(inp: EntityInput) -> str`
  - `group_by_ident(inputs: Iterable[EntityInput], ident_fn) -> Dict[str, List[EntityInput]]`
  - `offenders(groups: Dict[str, List[EntityInput]]) -> Dict[str, List[EntityInput]]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_at_scale_ident_collision_census.py`:

```python
# tests/test_at_scale_ident_collision_census.py
"""Unit tests for the #263 _code_ident collision audit's analysis primitives.

The audit's headline number cannot be asserted -- it is what the audit exists
to discover. What CAN be asserted are the components that could silently
produce a WRONG number: the ident grouping, the shape classifier, and the
candidate-rule scorer.

Every test names the counterfactual it was checked against. A test that passes
against a degenerate stub as well as against the real implementation proves
nothing and does not belong here.
"""

import pytest

from evals.at_scale.probe_ident_collision_census import (
    EntityInput,
    current_ident,
    group_by_ident,
    offenders,
    raw_value,
)


# The three collisions #257's census actually found on this repository's
# history, as their (file_path, name) inputs. Used as a fixture throughout:
# a run that does not reproduce these is not measuring the right thing.
KNOWN_COLLISIONS = [
    ("tests/test_mcp_server.py", "_commit", "commit"),
    ("tests/test_mcp_server.py", "_snapshot", "snapshot"),
    ("evals/at_scale/profile_forward_reconcile_attribution.py", "_main", "main"),
]


def _fn(file_path, name):
    return EntityInput("function", "code", file_path, name)


class TestRawValue:
    def test_named_input_uses_the_double_colon_separator(self):
        """raw_value must reproduce _code_ident's own input construction
        (mcp_server.py:4298-4301) exactly -- the candidate rules in Task 3 all
        re-slug this string, so a different separator here would score every
        rule against a value production never actually builds.
        """
        assert raw_value(_fn("a/b.py", "c")) == "a/b.py::c"

    def test_unnamed_input_is_the_bare_path(self):
        assert raw_value(EntityInput("module", "code", "a/b.py", None)) == "a/b.py"


class TestCurrentIdentCollapse:
    def test_private_and_public_helper_collapse_onto_one_ident(self):
        """This is #263's mechanism. Counterfactual: if _canonical_ident did
        not collapse consecutive hyphens, these two would differ
        ('...py---commit' vs '...py--commit') and this assert would fail.
        """
        private = current_ident(_fn("tests/test_mcp_server.py", "_commit"))
        public = current_ident(_fn("tests/test_mcp_server.py", "commit"))
        assert private == public == ":function/tests-test-mcp-server-py-commit"

    def test_unrelated_names_in_one_file_stay_distinct(self):
        """Guards against a stub current_ident that returns a constant, which
        would pass the collapse test above and make every grouping test
        vacuous.
        """
        assert current_ident(_fn("a/b.py", "alpha")) != current_ident(_fn("a/b.py", "beta"))


class TestGroupAndOffenders:
    def test_the_three_known_collisions_are_reported_as_offenders(self):
        inputs = []
        for file_path, private, public in KNOWN_COLLISIONS:
            inputs.append(_fn(file_path, private))
            inputs.append(_fn(file_path, public))
        found = offenders(group_by_ident(inputs, current_ident))
        assert len(found) == 3
        for members in found.values():
            assert len(members) == 2

    def test_public_members_alone_yield_zero_offenders(self):
        """The counterfactual for the test above. Feeding only the public half
        of each pair must report nothing -- otherwise the offender count is
        being produced by something other than the collision.
        """
        inputs = [_fn(file_path, public) for file_path, _, public in KNOWN_COLLISIONS]
        assert offenders(group_by_ident(inputs, current_ident)) == {}

    def test_the_same_input_twice_is_not_an_offender(self):
        """A name unchanged across 400 commits arrives 400 times. Counting
        occurrences instead of DISTINCT inputs would report the whole
        repository as colliding.
        """
        inputs = [_fn("a/b.py", "c")] * 400
        assert offenders(group_by_ident(inputs, current_ident)) == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v
```

Expected: collection error — `ModuleNotFoundError: No module named 'evals.at_scale.probe_ident_collision_census'`.

- [ ] **Step 3: Write the minimal implementation**

Create `evals/at_scale/probe_ident_collision_census.py`:

```python
# evals/at_scale/probe_ident_collision_census.py
"""#263 audit: how many _code_ident values on real history are reachable from
more than one distinct (entity_type, file_path, name) input?

_canonical_ident (mcp_server.py:4090) maps every character outside [a-z0-9-]
to a hyphen and then collapses runs of hyphens. _code_ident builds its input as
f"{file_path}::{name}", so the separator and a leading underscore in the name
both become hyphens and the collapse merges them:

    tests/test_mcp_server.py::_commit -> tests-test-mcp-server-py-commit
    tests/test_mcp_server.py::commit  -> tests-test-mcp-server-py-commit

WHY THIS AUDIT IS SOURCE-DERIVED AND NOT GRAPH-DERIVED. When two entities
collide, the second one parsed takes _build_code_triples' `ident in
entity_valid_from` branch, which appends a :modified-in triple and NOTHING
else -- no :entity-type, no :ident, no :description, no :file. The graph
therefore holds no record of the losing entity's (file_path, name) pair at
all, and widening #257's census key from :description to (:file, :description)
does not recover it. The three collisions #257 found were visible only because
those entities were closed and reopened, which re-runs the introduction branch.
Any graph-side count is a bound; only the inputs give an exact one.

READ-ONLY BY CONSTRUCTION. This module opens no MiniGrafDb handle, writes no
facts, and never mutates mcp_server. The candidate rules below are standalone
pure functions, deliberately NOT monkeypatches of _canonical_ident -- an audit
must not mutate the module it measures.

See docs/superpowers/specs/2026-08-11-ident-collision-audit-design.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import mcp_server  # noqa: E402


# The five types _code_ident actually produces. external-dependency is NOT a
# sixth: it shares the :module/ namespace (mcp_server.py:7396), which is why
# the module bucket below is pooled across producers rather than split.
ENTITY_TYPES = ("module", "function", "class", "variable", "field")

# Which call site produced an input. Only ever used to label a collision as
# cross-producer -- it is deliberately NOT part of the ident, because the
# whole point is that these three feed one namespace.
#   "code"    -- _code_ident over a parsed file (mcp_server.py:7044-7122)
#   "gitlink" -- _code_ident("module", path) for a submodule (mcp_server.py:9556)
#   "import"  -- _canonical_ident("module", specifier), the unresolved-import
#                fallback (mcp_server.py:4250, 4285)
PRODUCERS = ("code", "gitlink", "import")


class EntityInput(NamedTuple):
    """One (entity_type, file_path, name) triple that ever reached _code_ident.

    For producer "import", file_path holds the raw import specifier and name is
    None -- _canonical_ident("module", spec) is exactly _code_ident("module",
    spec, None), so no separate ident path is needed.
    """

    entity_type: str
    producer: str
    file_path: str
    name: Optional[str]


def raw_value(inp: EntityInput) -> str:
    """The string _code_ident slugs, reproducing mcp_server.py:4298-4301."""
    if inp.name:
        return f"{inp.file_path}::{inp.name}"
    return inp.file_path


def current_ident(inp: EntityInput) -> str:
    """The ident production builds for this input, today."""
    return mcp_server._code_ident(inp.entity_type, inp.file_path, inp.name)


def group_by_ident(
    inputs: Iterable[EntityInput],
    ident_fn: Callable[[EntityInput], str],
) -> Dict[str, List[EntityInput]]:
    """Group DISTINCT inputs by the ident ident_fn assigns them.

    Distinct is load-bearing: an unchanged name arrives once per commit that
    touched its file, and counting occurrences would report every entity in
    the repository as colliding with itself.
    """
    groups: Dict[str, Dict[EntityInput, None] ] = {}
    for inp in inputs:
        groups.setdefault(ident_fn(inp), {})[inp] = None
    return {ident: list(members) for ident, members in groups.items()}


def offenders(
    groups: Dict[str, List[EntityInput]],
) -> Dict[str, List[EntityInput]]:
    """The groups reachable from more than one distinct input."""
    return {
        ident: members for ident, members in groups.items() if len(members) > 1
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Prove the ablation**

Temporarily change `group_by_ident` to count occurrences rather than distinct inputs (replace the inner `Dict[EntityInput, None]` with a `list` and `.append`). Re-run.

Expected: `test_the_same_input_twice_is_not_an_offender` FAILS. Revert the change and confirm the suite is green again. If the test passed under the degraded version, it is not guarding anything — rewrite it before continuing.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_ident_collision_census.py tests/test_at_scale_ident_collision_census.py
git commit -F - <<'EOF'
Add the #263 audit's input model and current-rule grouping

EntityInput carries the producer alongside the (entity_type, file_path,
name) triple. The producer is deliberately not part of the ident: the
:module/ namespace is shared by in-tree modules, gitlinks and unresolved
imports, so pooling them is what makes a cross-producer collision visible.

group_by_ident groups DISTINCT inputs. An unchanged name arrives once per
commit that touched its file, so counting occurrences would report every
entity as colliding with itself.

Refs #263

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QRGNkzJc5FdfpbzAh4ZZzK
EOF
```

---

### Task 2: Collision shape classification

**Files:**
- Modify: `evals/at_scale/probe_ident_collision_census.py`
- Test: `tests/test_at_scale_ident_collision_census.py`

**Interfaces:**
- Consumes: `EntityInput`, `raw_value` from Task 1.
- Produces:
  - `SHAPES: Tuple[str, ...]` = `("leading-underscore", "case-only", "separator-vs-path", "cross-producer", "other")`
  - `classify_shapes(members: Sequence[EntityInput]) -> Set[str]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_at_scale_ident_collision_census.py`:

```python
from evals.at_scale.probe_ident_collision_census import (  # noqa: E402
    SHAPES,
    classify_shapes,
)


class TestClassifyShapes:
    def test_private_public_pair_in_one_file_is_leading_underscore(self):
        members = [_fn("a/b.py", "_foo"), _fn("a/b.py", "foo")]
        assert "leading-underscore" in classify_shapes(members)

    def test_private_public_field_on_one_class_is_leading_underscore(self):
        """Fields arrive qualified as 'Cls.field', so the private marker sits
        on the LAST dot-segment, not at the string's start. A classifier using
        a bare lstrip('_') on the whole qualified name reports 'other' here.
        """
        members = [
            EntityInput("field", "code", "a/b.py", "Cls._x"),
            EntityInput("field", "code", "a/b.py", "Cls.x"),
        ]
        assert "leading-underscore" in classify_shapes(members)

    def test_case_only_pair_is_not_labelled_leading_underscore(self):
        """The labels must separate. A classifier returning a constant label
        passes the two tests above and fails this one.
        """
        members = [_fn("a/b.py", "Foo"), _fn("a/b.py", "foo")]
        shapes = classify_shapes(members)
        assert "case-only" in shapes
        assert "leading-underscore" not in shapes

    def test_inputs_from_different_files_are_separator_vs_path(self):
        """The path/name boundary fell differently on the two inputs -- the
        case _code_ident's own docstring anticipates.
        """
        members = [_fn("a/b.py", "c"), _fn("a/b_py", "c")]
        assert "separator-vs-path" in classify_shapes(members)

    def test_import_and_in_tree_module_are_cross_producer(self):
        members = [
            EntityInput("module", "code", "a/b.py", None),
            EntityInput("module", "import", "a.b.py", None),
        ]
        assert "cross-producer" in classify_shapes(members)

    def test_private_pascal_case_beside_public_snake_case_is_leading_underscore(self):
        """_Config/config and _Handler/handler are ordinary Python and they DO
        collide (both -> :function/a-b-py-foo for _Foo/foo). An exact-case
        _strip_private comparison drops them into "other", which is reserved
        for UNPREDICTED families -- hiding a predicted one.

        Counterfactual: with _strip_private compared exact-case, this pair
        yields {"other"} and this test fails.
        """
        members = [_fn("a/b.py", "_Foo"), _fn("a/b.py", "foo")]
        assert current_ident(members[0]) == current_ident(members[1])
        assert "leading-underscore" in classify_shapes(members)

    def test_a_producer_only_difference_is_not_a_case_collision(self):
        """Two inputs whose raw values are byte-identical differ only in
        producer -- exactly the cross-producer collision this audit exists to
        find. Labelling them "case-only" asserts a case difference that is not
        there.

        Counterfactual: without the `a_raw != b_raw` guard, casefold equality
        holds trivially and "case-only" is emitted.
        """
        members = [
            EntityInput("module", "code", "vendor/x", None),
            EntityInput("module", "gitlink", "vendor/x", None),
        ]
        shapes = classify_shapes(members)
        assert "cross-producer" in shapes
        assert "case-only" not in shapes

    def test_an_unclassifiable_pair_falls_through_to_other(self):
        """'other' is the interesting bucket -- it is where a collision nobody
        predicted shows up. It must be reachable, not vestigial.
        """
        members = [_fn("a/b.py", "x-y"), _fn("a/b.py", "x.y")]
        assert classify_shapes(members) == {"other"}

    def test_every_emitted_label_is_declared_in_SHAPES(self):
        members = [_fn("a/b.py", "_foo"), _fn("a/b.py", "foo")]
        assert classify_shapes(members) <= set(SHAPES)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v -k Shapes
```

Expected: `ImportError: cannot import name 'SHAPES'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `evals/at_scale/probe_ident_collision_census.py`:

```python
from itertools import combinations
from typing import Sequence, Set

SHAPES = (
    "leading-underscore",
    "case-only",
    "separator-vs-path",
    "cross-producer",
    "other",
)


def _strip_private(name: Optional[str]) -> str:
    """Drop leading underscores from the LAST dot-segment of a name.

    Fields reach _code_ident qualified as "Cls.field" (mcp_server.py:7098), so
    the private marker sits mid-string. A bare name.lstrip("_") would miss
    "Cls._x" against "Cls.x" entirely.
    """
    if not name:
        return ""
    head, dot, last = name.rpartition(".")
    stripped = last.lstrip("_")
    return f"{head}{dot}{stripped}" if dot else stripped


def _pair_shapes(a: EntityInput, b: EntityInput) -> Set[str]:
    """Every collision family this pair belongs to.

    The checks are INDEPENDENT, not an elif chain. A pair can belong to more
    than one family and the caller returns a set, so making them exclusive
    would silently drop the second label -- and, worse, would let the winner
    depend on check order rather than on the data.

    Each check also carries an inequality guard. Without one, "case-only"
    fires on two inputs whose raw values are byte-identical (a pair differing
    only in producer -- exactly the cross-producer collision this audit exists
    to find), asserting a case difference that is not there.
    """
    shapes: Set[str] = set()
    if a.producer != b.producer:
        shapes.add("cross-producer")
    a_raw, b_raw = raw_value(a), raw_value(b)
    if a_raw != b_raw and a_raw.casefold() == b_raw.casefold():
        shapes.add("case-only")
    # casefold on BOTH sides, so a private PascalCase helper beside a public
    # snake_case one (_Config/config, _Handler/handler -- ordinary Python) is
    # still recognised as the underscore family. An exact-case comparison here
    # drops those into "other", which is reserved for UNPREDICTED families and
    # so would hide a predicted one.
    if (
        a.name != b.name
        and a.file_path == b.file_path
        and _strip_private(a.name).casefold() == _strip_private(b.name).casefold()
    ):
        shapes.add("leading-underscore")
    if a.file_path != b.file_path or (a.name is None) != (b.name is None):
        # The two inputs put the path/name boundary in different places, so
        # the ident cannot say where the path ended -- the collision family
        # _code_ident's docstring already anticipates.
        shapes.add("separator-vs-path")
    return shapes


def classify_shapes(members: Sequence[EntityInput]) -> Set[str]:
    """Label an offender by every collision family present among its inputs.

    An offender may carry more than one label (a cross-producer collision is
    usually also something else), so this returns a set rather than picking a
    single winner. "other" is emitted only when nothing else applied: it is
    where an unpredicted collision family would surface, and collapsing it
    into any named label would hide exactly the finding worth having.
    """
    shapes: Set[str] = set()
    for a, b in combinations(members, 2):
        shapes |= _pair_shapes(a, b)
    if not shapes - {"cross-producer"}:
        shapes.add("other")
    return shapes
```

An earlier draft of this plan made these checks an `elif` chain, on the claim
that `_Foo`/`foo` is "genuinely both case-only and leading-underscore" so the
narrower label should win. **That premise is false and the chain is a defect.**
Trace it: `a/b.py::_Foo`.casefold() is `a/b.py::_foo`, which does not equal
`a/b.py::foo`, so `case-only` never fires on that pair at all; the extra
underscore breaks the character correspondence `casefold` needs. Under
exact-case `_strip_private`, `"Foo" != "foo"` kills `leading-underscore` too,
and the pair lands in `"other"` despite genuinely colliding. Independent checks
with casefolded `_strip_private` are what the code above does instead.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v
```

Expected: 16 passed.

- [ ] **Step 5: Prove the ablation**

Three degradations, each reverted before the next.

1. Replace `classify_shapes`' body with `return {"leading-underscore"}`. Expected FAIL: `test_case_only_pair_is_not_labelled_leading_underscore`, `test_an_unclassifiable_pair_falls_through_to_other`, `test_inputs_from_different_files_are_separator_vs_path`, `test_import_and_in_tree_module_are_cross_producer`, `test_a_producer_only_difference_is_not_a_case_collision`. (A constant label cannot satisfy any of the label-specific assertions — count them all rather than stopping at the first two.)
2. Replace `_strip_private` with `return (name or "").lstrip("_")`. Expected FAIL: `test_private_public_field_on_one_class_is_leading_underscore` only.
3. Drop the `a_raw != b_raw` guard from the `case-only` check, and separately drop `.casefold()` from the `_strip_private` comparison. Expected FAIL: `test_a_producer_only_difference_is_not_a_case_collision` for the first, `test_private_pascal_case_beside_public_snake_case_is_leading_underscore` for the second.

Degradation 3 is the one that matters most: it is the pair of defects an earlier draft of this plan actively mandated.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_ident_collision_census.py tests/test_at_scale_ident_collision_census.py
git commit -F - <<'EOF'
Add the #263 audit's collision shape classifier

Labels each offender by every collision family present among its inputs,
not by a single winner -- a cross-producer collision is usually also
something else.

_strip_private works on the last dot-segment because fields reach
_code_ident qualified as "Cls.field", so a bare lstrip("_") on the whole
name would miss "Cls._x" against "Cls.x".

"other" is emitted only when nothing named applied. It is where an
unpredicted collision family would surface, so it stays reachable.

Refs #263

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QRGNkzJc5FdfpbzAh4ZZzK
EOF
```

---

### Task 3: Candidate rules and scoring

**Files:**
- Modify: `evals/at_scale/probe_ident_collision_census.py`
- Test: `tests/test_at_scale_ident_collision_census.py`

**Interfaces:**
- Consumes: `EntityInput`, `raw_value`, `current_ident`, `group_by_ident`, `offenders` from Task 1.
- Produces:
  - `RULES: Dict[str, Tuple[str, Callable[[EntityInput], str]]]` — rule id → `(description, ident_fn)`, keys `"R1".."R5"`
  - `score_rule(inputs, ident_fn, baseline_groups) -> Dict[str, int]` returning `{"residual": int, "renames": int}`
  - `score_all_rules(inputs, baseline_groups) -> Dict[str, Dict]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_at_scale_ident_collision_census.py`:

```python
from evals.at_scale.probe_ident_collision_census import (  # noqa: E402
    RULES,
    score_all_rules,
    score_rule,
)


def _known_collision_inputs():
    inputs = []
    for file_path, private, public in KNOWN_COLLISIONS:
        inputs.append(_fn(file_path, private))
        inputs.append(_fn(file_path, public))
    return inputs


class TestCandidateRules:
    @pytest.mark.parametrize("rule_id", ["R1", "R2", "R3", "R4"])
    def test_separating_rules_leave_no_residual(self, rule_id):
        inputs = _known_collision_inputs()
        _, ident_fn = RULES[rule_id]
        assert offenders(group_by_ident(inputs, ident_fn)) == {}

    def test_R5_still_collides_and_is_the_scorer_s_known_negative(self):
        """R5 slugs path and name independently, so strip("-") still eats the
        leading underscore and _commit/commit both reduce to "commit". R5 is
        in the table precisely BECAUSE it does not work: a scorer that reports
        R5 clean is broken, and every other row it produced is suspect.
        """
        inputs = _known_collision_inputs()
        _, ident_fn = RULES["R5"]
        assert len(offenders(group_by_ident(inputs, ident_fn))) == 3

    @pytest.mark.parametrize("rule_id", ["R1", "R2", "R3", "R4"])
    def test_every_separating_rule_renames_something(self, rule_id):
        """A rule that renames nothing cannot have changed derivation, so a
        zero residual from it would be arithmetic rather than a finding.
        """
        inputs = _known_collision_inputs()
        baseline = group_by_ident(inputs, current_ident)
        _, ident_fn = RULES[rule_id]
        assert score_rule(inputs, ident_fn, baseline)["renames"] > 0

    def test_R4_renames_every_ident(self):
        """Every ident gains a hash suffix, so the rename count equals the
        baseline ident count. Counterfactual: a renames metric that counted
        only COLLIDING idents would report 3, not 3-of-3 plus the rest.
        """
        inputs = _known_collision_inputs() + [_fn("z/z.py", "solo")]
        baseline = group_by_ident(inputs, current_ident)
        _, ident_fn = RULES["R4"]
        scored = score_rule(inputs, ident_fn, baseline)
        assert scored["renames"] == len(baseline)
        assert scored["residual"] == 0

    def test_R2_leaves_a_plain_module_ident_unrenamed(self):
        """R2 only drops the hyphen collapse. A module input has no '::'
        separator, so a path with no adjacent non-alphanumerics is untouched.
        This is what makes R2's rename cost differ by entity type.
        """
        module = EntityInput("module", "code", "a/b.py", None)
        _, ident_fn = RULES["R2"]
        assert ident_fn(module) == current_ident(module)

    def test_R1_does_rename_that_same_module_ident(self):
        """The counterfactual for the test above: R1 changes the charset, so
        an underscore anywhere in the path moves the ident even with no name.
        """
        module = EntityInput("module", "code", "a/b_c.py", None)
        _, ident_fn = RULES["R1"]
        assert ident_fn(module) != current_ident(module)

    def test_score_all_rules_covers_every_declared_rule(self):
        inputs = _known_collision_inputs()
        baseline = group_by_ident(inputs, current_ident)
        scored = score_all_rules(inputs, baseline)
        assert set(scored) == set(RULES)
        for row in scored.values():
            assert set(row) == {"description", "residual", "renames"}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v -k CandidateRules
```

Expected: `ImportError: cannot import name 'RULES'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `evals/at_scale/probe_ident_collision_census.py`:

```python
import hashlib
import re
from typing import Tuple


def _slug_current(value: str) -> str:
    """_canonical_ident's slug, verbatim (mcp_server.py:4097-4098)."""
    slug = re.sub(r"[^a-z0-9-]", "-", value.lower())
    return re.sub(r"-+", "-", slug).strip("-")


def _slug_keep_underscore(value: str) -> str:
    """R1: '_' joins the allowed charset, so a private marker survives."""
    slug = re.sub(r"[^a-z0-9_-]", "-", value.lower())
    return re.sub(r"-+", "-", slug).strip("-")


def _slug_no_collapse(value: str) -> str:
    """R2: hyphen runs are preserved, so separator arity carries the marker
    ('py---commit' against 'py--commit')."""
    return re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")


def _slug_keep_underscore_no_collapse(value: str) -> str:
    """R3: R1 and R2 together."""
    return re.sub(r"[^a-z0-9_-]", "-", value.lower()).strip("-")


def _ident_from_slug(slug_fn: Callable[[str], str]) -> Callable[[EntityInput], str]:
    def ident(inp: EntityInput) -> str:
        return f":{inp.entity_type}/{slug_fn(raw_value(inp))}"

    return ident


def _ident_hash_suffix(inp: EntityInput) -> str:
    """R4: the current slug plus 8 hex of sha256 over the RAW value.

    Hashing the raw value, before lowercasing, is what separates a case-only
    collision as well -- the slug alone cannot.
    """
    value = raw_value(inp)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f":{inp.entity_type}/{_slug_current(value)}-{digest}"


def _ident_independent_parts(inp: EntityInput) -> str:
    """R5, the CONTROL. Slugs file_path and name separately and joins them
    with a fixed token.

    This is expected to FAIL. _slug_current strips leading hyphens, so a name
    of "_commit" reduces to "commit" exactly as "commit" does, and the pair
    still collides. R5 exists so score_all_rules has a known-negative: if it
    ever reports zero residual on a history with leading-underscore pairs, the
    scorer is wrong and every other row it produced is suspect.
    """
    path_slug = _slug_current(inp.file_path)
    if inp.name is None:
        return f":{inp.entity_type}/{path_slug}"
    return f":{inp.entity_type}/{path_slug}--{_slug_current(inp.name)}"


RULES: Dict[str, Tuple[str, Callable[[EntityInput], str]]] = {
    "R1": (
        "keep underscores: charset becomes [^a-z0-9_-]",
        _ident_from_slug(_slug_keep_underscore),
    ),
    "R2": (
        "no hyphen collapse: drop re.sub(r'-+', '-', slug)",
        _ident_from_slug(_slug_no_collapse),
    ),
    "R3": (
        "R1 + R2: keep underscores and drop the collapse",
        _ident_from_slug(_slug_keep_underscore_no_collapse),
    ),
    "R4": (
        "hash suffix: current slug + '-' + sha256(raw value)[:8]",
        _ident_hash_suffix,
    ),
    "R5": (
        "CONTROL, expected to still collide: slug file_path and name "
        "independently, join with '--'",
        _ident_independent_parts,
    ),
}


def score_rule(
    inputs: Sequence[EntityInput],
    ident_fn: Callable[[EntityInput], str],
    baseline_groups: Dict[str, List[EntityInput]],
) -> Dict[str, int]:
    """Residual collisions and rename cost for one candidate rule.

    renames counts BASELINE idents, not inputs: a baseline ident is renamed
    when any input in its group maps somewhere other than that same ident.
    Rename cost is not a footnote to residual -- every change to derivation
    renames entities in every existing graph, and that cost is what decides
    forward-fix against migration.
    """
    residual = len(offenders(group_by_ident(inputs, ident_fn)))
    renames = sum(
        1
        for ident, members in baseline_groups.items()
        if any(ident_fn(member) != ident for member in members)
    )
    return {"residual": residual, "renames": renames}


def score_all_rules(
    inputs: Sequence[EntityInput],
    baseline_groups: Dict[str, List[EntityInput]],
) -> Dict[str, Dict]:
    scored = {}
    for rule_id, (description, ident_fn) in RULES.items():
        row = score_rule(inputs, ident_fn, baseline_groups)
        row["description"] = description
        scored[rule_id] = row
    return scored
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v
```

Expected: 27 passed.

- [ ] **Step 5: Prove the ablation**

Temporarily change `score_rule`'s `renames` to count only offender groups (`if len(members) > 1 and ...`). Re-run.

Expected: `test_R4_renames_every_ident` FAILS. Revert. Then temporarily change `_ident_independent_parts` to append the unslugged name; expected: `test_R5_still_collides_and_is_the_scorer_s_known_negative` FAILS. Revert both and confirm green.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_ident_collision_census.py tests/test_at_scale_ident_collision_census.py
git commit -F - <<'EOF'
Add the #263 audit's candidate rules and scorer

Five rules, each scored on residual collisions AND rename cost. Rename
cost is not a footnote: any change to derivation renames entities in
every existing graph, and that is what decides forward-fix against
migration.

R5 is a control expected to keep colliding -- slugging the name
independently still runs strip("-") over it, so _commit and commit both
reduce to "commit". It is the scorer's known-negative: a run reporting
R5 clean means the scorer is broken and every other row is suspect.

Rules are standalone pure functions, not monkeypatches of
_canonical_ident. An audit must not mutate the module it measures.

Refs #263

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QRGNkzJc5FdfpbzAh4ZZzK
EOF
```

---

### Task 4: Stage 1 — collect inputs through the real extractor

**Files:**
- Modify: `evals/at_scale/probe_ident_collision_census.py`
- Test: `tests/test_at_scale_ident_collision_census.py`

**Interfaces:**
- Consumes: `EntityInput` from Task 1; `mcp_server._extract_commit`, `mcp_server._git_commits`, `mcp_server._load_ignore_patterns`, `mcp_server._default_git_branch`.
- Produces:
  - `inputs_from_commit_extraction(file_results, gitlink_changes) -> List[EntityInput]`
  - `collect_inputs(repo_path, branch, jobs=None) -> Tuple[List[EntityInput], Dict[str, Any]]` where the diagnostics dict carries `commits`, `head_commit`, `extraction_failures`, `failed_commits`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_at_scale_ident_collision_census.py`:

```python
import subprocess  # noqa: E402

from evals.at_scale.probe_ident_collision_census import (  # noqa: E402
    collect_inputs,
    inputs_from_commit_extraction,
)


class TestInputsFromCommitExtraction:
    def test_deleted_files_carry_no_extraction_and_are_skipped(self):
        """_extract_commit returns extracted=None and precomputed=None for a
        "D" entry (mcp_server.py:8477-8480). Dereferencing either would raise;
        treating the path as a live module input would invent an entity.
        """
        file_results = [("D", "a/gone.py", None, None, "")]
        assert inputs_from_commit_extraction(file_results, []) == []

    def test_a_parsed_file_yields_module_and_every_named_entity(self):
        extracted = {
            "functions": ["foo", "_foo"],
            "classes": ["Cls"],
            "globals": ["CONST"],
            "fields": [("x", "Cls", False)],
            "imports": [],
            "calls": [],
        }
        precomputed = {"resolved_imports": []}
        got = inputs_from_commit_extraction(
            [("A", "a/b.py", extracted, precomputed, "")], []
        )
        assert EntityInput("module", "code", "a/b.py", None) in got
        assert EntityInput("function", "code", "a/b.py", "_foo") in got
        assert EntityInput("class", "code", "a/b.py", "Cls") in got
        assert EntityInput("variable", "code", "a/b.py", "CONST") in got

    def test_fields_are_qualified_with_their_owning_class(self):
        """_precompute_file_triples builds the field name as
        f"{owning_class}.{field_name}" (mcp_server.py:7098). An audit keying on
        the bare field name would merge Cls.x and Other.x and overstate the
        collision count.
        """
        extracted = {
            "functions": [], "classes": ["Cls"], "globals": [],
            "fields": [("x", "Cls", False)], "imports": [], "calls": [],
        }
        got = inputs_from_commit_extraction(
            [("M", "a/b.py", extracted, {"resolved_imports": []}, "")], []
        )
        assert EntityInput("field", "code", "a/b.py", "Cls.x") in got
        assert EntityInput("field", "code", "a/b.py", "x") not in got

    def test_only_unresolved_imports_become_import_inputs(self):
        """A resolved import already points at an in-tree module ident that the
        module producer contributes. Counting it again would manufacture a
        cross-producer collision on every internal import in the repository.
        """
        extracted = {
            "functions": [], "classes": [], "globals": [],
            "fields": [], "imports": [], "calls": [],
        }
        precomputed = {
            "resolved_imports": [
                ("a.b", ":module/a-b-py", True),
                ("requests", ":module/requests", False),
            ]
        }
        got = inputs_from_commit_extraction(
            [("A", "z.py", extracted, precomputed, "")], []
        )
        assert EntityInput("module", "import", "requests", None) in got
        assert EntityInput("module", "import", "a.b", None) not in got

    def test_gitlink_paths_of_every_kind_become_module_inputs(self):
        """_gitlink_changes emits add/bump/remove; all three name a submodule
        entity at that path (mcp_server.py:9556).
        """
        got = inputs_from_commit_extraction(
            [], [("add", "sha1", "vendor/x"), ("bump", "sha2", "vendor/y"),
                 ("remove", "sha3", "vendor/z")]
        )
        paths = {inp.file_path for inp in got if inp.producer == "gitlink"}
        assert paths == {"vendor/x", "vendor/y", "vendor/z"}


class TestCollectInputsEndToEnd:
    def test_a_private_public_pair_collides_through_real_extraction(self, tmp_path):
        """Drives the REAL _extract_commit over a purpose-built repo. Every
        other test in this file would still pass if Stage 1 collected the wrong
        triples -- this is the one that catches the audit mis-driving the
        extractor.

        Counterfactual: with only `def foo` in the file, offenders is empty.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "mod.py").write_text("def _helper():\n    pass\n\ndef helper():\n    pass\n")
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "t"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "init"],
        ):
            subprocess.run(cmd, cwd=repo, check=True)

        inputs, diagnostics = collect_inputs(str(repo), "main", jobs=1)

        assert diagnostics["commits"] == 1
        assert diagnostics["extraction_failures"] == 0
        found = offenders(group_by_ident(inputs, current_ident))
        assert len(found) == 1
        names = {inp.name for inp in next(iter(found.values()))}
        assert names == {"_helper", "helper"}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v -k "CommitExtraction or EndToEnd"
```

Expected: `ImportError: cannot import name 'collect_inputs'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `evals/at_scale/probe_ident_collision_census.py`:

```python
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Sequence as _Sequence


def inputs_from_commit_extraction(
    file_results: _Sequence[tuple],
    gitlink_changes: _Sequence[tuple],
) -> List[EntityInput]:
    """Harvest every _code_ident input one commit's extraction implies.

    file_results entries are (status, file_path, extracted, precomputed,
    old_path) per _extract_commit's contract (mcp_server.py:8475-8484). A "D"
    entry carries extracted=None and precomputed=None and is skipped: the path
    names an entity being CLOSED, not one being introduced, and its inputs were
    already collected when it was introduced.

    The category-to-entity_type mapping and the field qualification mirror
    _precompute_file_triples (mcp_server.py:7044-7122). If that function and
    this one ever disagree, that one is right.
    """
    collected: List[EntityInput] = []

    for status, file_path, extracted, precomputed, _old_path in file_results:
        if extracted is None:
            continue
        collected.append(EntityInput("module", "code", file_path, None))
        for fn_name in extracted.get("functions", []):
            collected.append(EntityInput("function", "code", file_path, fn_name))
        for cls_name in extracted.get("classes", []):
            collected.append(EntityInput("class", "code", file_path, cls_name))
        for gvar_name in extracted.get("globals", []):
            collected.append(EntityInput("variable", "code", file_path, gvar_name))
        for field_name, owning_class, _is_static in extracted.get("fields", []):
            collected.append(
                EntityInput("field", "code", file_path, f"{owning_class}.{field_name}")
            )
        # Only UNRESOLVED imports. A resolved one already points at the in-tree
        # module ident the module producer above contributes, so counting it
        # again would manufacture a cross-producer collision on every internal
        # import in the repository.
        for import_name, _dep_ident, is_resolved in (
            (precomputed or {}).get("resolved_imports", [])
        ):
            if not is_resolved:
                collected.append(EntityInput("module", "import", import_name, None))

    # add/bump/remove all name a submodule entity at that path
    # (mcp_server.py:9556), so every kind contributes.
    for _kind, _sha, path in gitlink_changes:
        collected.append(EntityInput("module", "gitlink", path, None))

    return collected


def collect_inputs(
    repo_path: str,
    branch: Optional[str] = None,
    jobs: Optional[int] = None,
) -> Tuple[List[EntityInput], Dict[str, Any]]:
    """Walk the branch and collect every input that ever reached _code_ident.

    Drives mcp_server._extract_commit rather than re-implementing parsing.
    That function is documented read-only, stateless and DB-free
    (mcp_server.py:8455) and is exactly what _run_ingestion's worker pool
    runs, so this audit measures the code that actually produces idents in
    production rather than a reimplementation that could drift from it.

    Walking A/M/R across every commit reaches every version of every file that
    ever existed on the branch: each distinct blob at each path is introduced
    by exactly one such entry, so no separate initial-tree pass is needed.

    Inputs are deduplicated here. A name unchanged across 400 commits costs one
    entry, not 400.
    """
    resolved_branch = branch or mcp_server._default_git_branch(repo_path)
    ignore_patterns = mcp_server._load_ignore_patterns(repo_path)
    commits = mcp_server._git_commits(repo_path, None, resolved_branch)
    hashes = [row[0] for row in commits]

    seen: Dict[EntityInput, None] = {}
    failed: List[str] = []

    with ProcessPoolExecutor(max_workers=jobs) as executor:
        futures = {
            executor.submit(
                mcp_server._extract_commit, repo_path, commit_hash, ignore_patterns
            ): commit_hash
            for commit_hash in hashes
        }
        for future, commit_hash in futures.items():
            try:
                file_results, gitlink_changes, _gitmodules, _renamed = future.result()
            except Exception:
                # Per-commit failure must not abort the audit -- it is a
                # measurement diagnostic, and the exit gate below decides
                # whether there were too many for the run to mean anything.
                failed.append(commit_hash)
                continue
            for inp in inputs_from_commit_extraction(file_results, gitlink_changes):
                seen[inp] = None

    return list(seen), {
        # Taken from the walked list, not a separate git rev-parse, so it
        # cannot disagree with what was actually measured.
        "head_commit": hashes[-1] if hashes else None,
        "branch": resolved_branch,
        "commits": len(hashes),
        "extraction_failures": len(failed),
        "failed_commits": failed,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v
```

Expected: 33 passed.

- [ ] **Step 5: Prove the ablation**

Temporarily drop the `if not is_resolved:` guard so every import becomes an input. Re-run; expected: `test_only_unresolved_imports_become_import_inputs` FAILS. Revert. Then temporarily change the field loop to use the bare `field_name`; expected: `test_fields_are_qualified_with_their_owning_class` FAILS. Revert both.

Then run the end-to-end test's own counterfactual by hand: change the temp repo's file to contain only `def helper()`, confirm `offenders(...)` is empty, and restore the test.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_ident_collision_census.py tests/test_at_scale_ident_collision_census.py
git commit -F - <<'EOF'
Add the #263 audit's Stage 1 input collection

Drives mcp_server._extract_commit rather than re-implementing parsing.
That function is already read-only, stateless and DB-free, and is what
_run_ingestion's worker pool runs -- so the audit measures the code that
actually produces idents, not a reimplementation that could drift.

Walking A/M/R across every commit reaches every version of every file
that ever existed: each distinct blob at each path is introduced by
exactly one such entry, so no separate initial-tree pass is needed.

Only UNRESOLVED imports become import inputs. A resolved one already
points at the in-tree module ident the module producer contributes, so
counting it again would manufacture a cross-producer collision on every
internal import in the repository.

Refs #263

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QRGNkzJc5FdfpbzAh4ZZzK
EOF
```

---

### Task 5: Report assembly, predictions, CLI and exit gate

**Files:**
- Modify: `evals/at_scale/probe_ident_collision_census.py`
- Test: `tests/test_at_scale_ident_collision_census.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces:
  - `PREDICTIONS: Tuple[Tuple[str, str], ...]` — `(id, statement)` pairs
  - `evaluate_predictions(report) -> Dict[str, Dict]` — id → `{"statement", "outcome", "evidence"}` with outcome in `{"held", "failed"}`
  - `build_report(inputs, diagnostics, repo_path) -> Dict[str, Any]`
  - `measurement_invalid(report) -> Optional[str]` — None when valid, else the reason
  - `main() -> int`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_at_scale_ident_collision_census.py`:

```python
from evals.at_scale.probe_ident_collision_census import (  # noqa: E402
    PREDICTIONS,
    build_report,
    evaluate_predictions,
    measurement_invalid,
)


def _report_over(inputs, **diag):
    diagnostics = {
        "head_commit": "abc123", "branch": "main", "commits": 10,
        "extraction_failures": 0, "failed_commits": [],
    }
    diagnostics.update(diag)
    return build_report(inputs, diagnostics, "/repo")


class TestBuildReport:
    def test_offenders_are_reported_verbatim_not_only_counted(self):
        """A nonzero count escalates this to fix design, and that work should
        start from the data rather than re-deriving it by hand. Counterfactual:
        a report carrying only counts passes every other test in this class.
        """
        report = _report_over(_known_collision_inputs())
        rows = report["offenders"]["function"]["idents"]
        assert len(rows) == 3
        members = rows[":function/tests-test-mcp-server-py-commit"]
        assert sorted(m["name"] for m in members) == ["_commit", "commit"]
        assert all(m["file_path"] == "tests/test_mcp_server.py" for m in members)
        assert all(m["producer"] == "code" for m in members)

    def test_every_entity_type_appears_even_at_zero(self):
        """A missing key and a zero are different claims. Only the second one
        says "measured, found none".
        """
        report = _report_over([_fn("a/b.py", "solo")])
        assert set(report["offenders"]) == set(ENTITY_TYPES)
        assert report["offenders"]["class"]["count"] == 0

    def test_candidate_table_carries_every_rule(self):
        report = _report_over(_known_collision_inputs())
        assert set(report["candidates"]) == set(RULES)


class TestExitGate:
    def test_finding_collisions_is_not_an_invalid_measurement(self):
        """The exit code reflects measurement VALIDITY, never the finding.
        Collisions are the number #263 asks for and must never fail the run.
        Do not adjust this gate to make a run pass.
        """
        report = _report_over(_known_collision_inputs())
        assert report["offenders"]["function"]["count"] == 3
        assert measurement_invalid(report) is None

    def test_zero_commits_is_invalid(self):
        assert measurement_invalid(_report_over([], commits=0)) is not None

    def test_zero_triples_is_invalid(self):
        """Distinguishes "measured, found none" from "measured nothing". A run
        that collected no inputs cannot report zero collisions as a finding.
        """
        assert measurement_invalid(_report_over([])) is not None

    def test_extraction_failures_above_one_percent_are_invalid(self):
        inputs = _known_collision_inputs()
        assert measurement_invalid(
            _report_over(inputs, commits=100, extraction_failures=2)
        ) is not None
        assert measurement_invalid(
            _report_over(inputs, commits=100, extraction_failures=1)
        ) is None


class TestPredictions:
    def test_every_declared_prediction_is_evaluated(self):
        report = _report_over(_known_collision_inputs())
        evaluated = evaluate_predictions(report)
        assert set(evaluated) == {pid for pid, _ in PREDICTIONS}
        for row in evaluated.values():
            assert row["outcome"] in ("held", "failed")

    def test_a_prediction_can_actually_fail(self):
        """Predictions exist to be falsifiable. An evaluator that returns
        "held" unconditionally is worse than none, because it launders a
        failed prediction as confirmation. Three offenders is exactly the
        count P1 says the audit must EXCEED.
        """
        evaluated = evaluate_predictions(_report_over(_known_collision_inputs()))
        assert evaluated["P1"]["outcome"] == "failed"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v -k "BuildReport or ExitGate or Predictions"
```

Expected: `ImportError: cannot import name 'PREDICTIONS'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `evals/at_scale/probe_ident_collision_census.py`:

```python
import datetime
import json

# Fixed BEFORE any data exists, so the run cannot be rationalized afterwards.
# A prediction that fails is a finding about the design, not noise to be
# smoothed over: it is recorded in the result either way.
PREDICTIONS: Tuple[Tuple[str, str], ...] = (
    ("P1", "The true collision count exceeds the 3 #257's census found, "
           "because that census can only see collisions whose loser was "
           "closed and reopened."),
    ("P2", "leading-underscore is the dominant named shape among all "
           "offenders."),
    ("P3", "R5, the control, reports a nonzero residual at least as large as "
           "the leading-underscore offender count."),
    ("P4", "R2's rename count is proportionally lower on module idents than "
           "on function idents, because a module input has no '::' separator."),
    ("P5", "R4's rename count equals the total ident count."),
)


def build_report(
    inputs: Sequence[EntityInput],
    diagnostics: Dict[str, Any],
    repo_path: str,
) -> Dict[str, Any]:
    baseline = group_by_ident(inputs, current_ident)
    all_offenders = offenders(baseline)

    per_type: Dict[str, Dict[str, Any]] = {}
    for entity_type in ENTITY_TYPES:
        rows = {
            ident: members
            for ident, members in all_offenders.items()
            if members[0].entity_type == entity_type
        }
        per_type[entity_type] = {
            # A missing key and a zero are different claims; only the second
            # says "measured, found none". Every type appears unconditionally.
            "idents_total": sum(
                1 for _ident, members in baseline.items()
                if members[0].entity_type == entity_type
            ),
            "count": len(rows),
            "idents": {
                ident: [
                    {"producer": m.producer, "file_path": m.file_path, "name": m.name}
                    for m in members
                ]
                for ident, members in rows.items()
            },
        }

    shape_counts: Dict[str, int] = {shape: 0 for shape in SHAPES}
    for members in all_offenders.values():
        for shape in classify_shapes(members):
            shape_counts[shape] += 1

    report: Dict[str, Any] = {
        "repo_path": repo_path,
        "branch": diagnostics["branch"],
        "head_commit": diagnostics["head_commit"],
        "commits": diagnostics["commits"],
        "extraction_failures": diagnostics["extraction_failures"],
        "failed_commits": diagnostics["failed_commits"],
        "triples_total": len(inputs),
        "idents_total": len(baseline),
        "offenders": per_type,
        "offenders_total": len(all_offenders),
        "offenders_by_shape": shape_counts,
        "candidates": score_all_rules(inputs, baseline),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    report["predictions"] = evaluate_predictions(report)
    return report


def evaluate_predictions(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    statements = dict(PREDICTIONS)
    shape_counts = report["offenders_by_shape"]
    leading = shape_counts.get("leading-underscore", 0)

    def _row(pid: str, held: bool, evidence: str) -> Dict[str, Any]:
        return {
            "statement": statements[pid],
            "outcome": "held" if held else "failed",
            "evidence": evidence,
        }

    total = report["offenders_total"]
    dominant = max(
        (s for s in SHAPES if s != "other"),
        key=lambda s: shape_counts.get(s, 0),
        default="other",
    )
    r5 = report["candidates"]["R5"]["residual"]
    r4 = report["candidates"]["R4"]["renames"]
    r2 = report["candidates"]["R2"]["renames"]

    return {
        "P1": _row("P1", total > 3, f"offenders_total={total}"),
        "P2": _row(
            "P2",
            dominant == "leading-underscore",
            f"dominant shape={dominant} at {shape_counts.get(dominant, 0)}",
        ),
        "P3": _row("P3", r5 >= leading and r5 > 0,
                   f"R5 residual={r5}, leading-underscore offenders={leading}"),
        "P4": _row("P4", r2 < report["idents_total"],
                   f"R2 renames={r2} of {report['idents_total']} idents"),
        "P5": _row("P5", r4 == report["idents_total"],
                   f"R4 renames={r4} of {report['idents_total']} idents"),
    }


def measurement_invalid(report: Dict[str, Any]) -> Optional[str]:
    """Why this run cannot be believed, or None.

    VALIDITY ONLY. A nonzero collision count is the number #263 asks for and
    must NEVER fail the run -- finding collisions is this audit's entire
    purpose. Do not widen this gate to make a run "pass".
    """
    if report["commits"] == 0:
        return "Zero commits walked. Nothing was measured."
    if report["triples_total"] == 0:
        return (
            "Zero inputs collected across "
            f"{report['commits']} commits. A run that collected nothing "
            "cannot report zero collisions as a finding."
        )
    failures = report["extraction_failures"]
    if failures > report["commits"] * 0.01:
        return (
            f"_extract_commit raised on {failures} of {report['commits']} "
            "commits (>1%). The input set is incomplete, so the count is a "
            "bound rather than the exact number this audit exists to produce."
        )
    return None


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Count, exactly, how many _code_ident values on real history are "
            "reachable from more than one (entity_type, file_path, name) input."
        )
    )
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--json-out", "--output", dest="json_out", default=None)
    args = parser.parse_args()

    inputs, diagnostics = collect_inputs(args.repo_path, args.branch, jobs=args.jobs)
    report = build_report(inputs, diagnostics, args.repo_path)

    print(json.dumps(report, indent=2))
    print()
    print(f"repo:                   {report['repo_path']} @ {report['branch']}")
    print(f"head:                   {report['head_commit']}")
    print(f"commits:                {report['commits']}")
    print(f"extraction failures:    {report['extraction_failures']}")
    print(f"distinct inputs:        {report['triples_total']}")
    print(f"distinct idents:        {report['idents_total']}")
    print()
    print("OFFENDERS (idents reachable from >1 distinct input)")
    for entity_type in ENTITY_TYPES:
        row = report["offenders"][entity_type]
        print(f"  {entity_type:<12} {row['count']:>6} of {row['idents_total']}")
    print(f"  {'TOTAL':<12} {report['offenders_total']:>6}")
    print()
    print("BY SHAPE (an offender may carry more than one label)")
    for shape in SHAPES:
        print(f"  {shape:<20} {report['offenders_by_shape'][shape]:>6}")
    print()
    print("CANDIDATE RULES")
    print(f"  {'rule':<6} {'residual':>9} {'renames':>9}   description")
    for rule_id in sorted(RULES):
        row = report["candidates"][rule_id]
        print(
            f"  {rule_id:<6} {row['residual']:>9} {row['renames']:>9}   "
            f"{row['description']}"
        )
    print()
    print("PREDICTIONS (fixed before the run; a failure is a finding)")
    for pid, _ in PREDICTIONS:
        row = report["predictions"][pid]
        print(f"  {pid}  {row['outcome']:<7} {row['evidence']}")

    if report["candidates"]["R5"]["residual"] == 0 and (
        report["offenders_by_shape"]["leading-underscore"] > 0
    ):
        print()
        print(
            "NOTE: R5 is the CONTROL and reported zero residual on a history\n"
            "that HAS leading-underscore offenders. R5 slugs the name\n"
            "independently, and strip('-') still eats the leading underscore,\n"
            "so it cannot separate them. The scorer is wrong and every other\n"
            "row in the candidate table is suspect. Do not act on this run."
        )

    reason = measurement_invalid(report)
    if reason:
        print()
        print(f"INVALID MEASUREMENT. {reason}")
        print("Do not adjust this gate to make a run pass.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        print(f"\nWrote {args.json_out}")
    return 1 if reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Also add `ENTITY_TYPES` to the test file's import block if not already present.

- [ ] **Step 3b: Hoist every import to the top of both files**

Tasks 1–5 each appended their own imports next to the code that needed them,
so both files now carry `import` statements scattered through the body. Move
them all into the single import block at the top of each file, keeping
`mcp_server`'s import after the `sys.path` insert (it depends on it) and
collapsing the duplicate `typing` imports into one line. This is a review
finding waiting to happen — the #257 probe's test file took the same
correction.

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -q
```

Expected: still green after the move.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_at_scale_ident_collision_census.py -v
```

Expected: 43 passed.

- [ ] **Step 5: Prove the ablation**

Temporarily make `measurement_invalid` also return a reason when
`report["offenders_total"] > 0`. Re-run; expected:
`test_finding_collisions_is_not_an_invalid_measurement` FAILS — that is the
test standing between this audit and an exit gate that punishes it for finding
what it was built to find. Revert.

Then temporarily make `evaluate_predictions` return `"held"` unconditionally;
expected: `test_a_prediction_can_actually_fail` FAILS. Revert both.

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/python -m pytest tests/ -q
```

Expected: no new failures relative to the branch point. Record the count.

- [ ] **Step 7: Commit**

```bash
git add evals/at_scale/probe_ident_collision_census.py tests/test_at_scale_ident_collision_census.py
git commit -F - <<'EOF'
Add the #263 audit's report, predictions, CLI and exit gate

The exit gate is validity-only: zero commits, zero inputs, or >1%
extraction failures. A nonzero collision count is the number #263 asks
for and never fails the run.

Predictions are fixed before any data exists and evaluated mechanically,
so a failed one is recorded rather than smoothed over. The CLI prints a
loud note when R5 -- the control -- comes back clean on a history that
has leading-underscore offenders, since that means the scorer is broken.

Refs #263

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QRGNkzJc5FdfpbzAh4ZZzK
EOF
```

---

### Task 6: Run the audit and record the measurement

**Files:**
- Create: `evals/at_scale/results/263-ident-collision-census.json`
- Modify: `evals/at_scale/benchmark.md`

**Interfaces:**
- Consumes: `main()` from Task 5. Produces no new code surface.

- [ ] **Step 1: Run the audit against this repository**

```bash
.venv/bin/python evals/at_scale/probe_ident_collision_census.py \
  --repo-path . \
  --json-out evals/at_scale/results/263-ident-collision-census.json
```

Expected: exit 0. If it exits 1, read the INVALID MEASUREMENT reason and address the cause — do not relax the gate.

- [ ] **Step 2: Check the control before believing anything else**

Confirm from the output that `R5` has a **nonzero** residual. If it is zero while `leading-underscore` is nonzero, the scorer is broken; stop and go back to Task 3.

- [ ] **Step 3: Confirm the known collisions reproduce**

```bash
.venv/bin/python -c "
import json
r = json.load(open('evals/at_scale/results/263-ident-collision-census.json'))
for ident in (
    ':function/tests-test-mcp-server-py-commit',
    ':function/tests-test-mcp-server-py-snapshot',
    ':function/evals-at-scale-profile-forward-reconcile-attribution-py-main',
):
    print(ident, ident in r['offenders']['function']['idents'])
"
```

Expected: all three `True`. These are the collisions #257 independently observed; an audit that misses them is not measuring the right thing, regardless of what its total says.

- [ ] **Step 4: Record the result in `evals/at_scale/benchmark.md`**

Add a subsection alongside the existing one-off probe entries, following how `probe_dep_preload_exposure.py` is described there. State: the headline offender count, the shape breakdown, the candidate table, **every prediction's outcome including the failed ones**, and the one-line conclusion on whether a fix needs a migration for existing graphs or only a forward change.

Do not write the conclusion before reading the numbers.

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/results/263-ident-collision-census.json evals/at_scale/benchmark.md
git commit -F - <<'EOF'
Record the #263 ident collision measurement

<Replace this line with the actual headline numbers: offenders total and
per type, the shape breakdown, and which candidate rules came back clean
at what rename cost. Name every prediction that failed.>

Refs #263

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QRGNkzJc5FdfpbzAh4ZZzK
EOF
```

- [ ] **Step 6: Post the finding to #263**

```bash
gh issue comment 263 --body-file <(...)
```

Report the offender count, the shape breakdown, the candidate table, the failed predictions, and the migration-versus-forward-fix conclusion. Do not use any GitHub closing keyword — the audit does not settle the bug, it sizes it.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: "Why the graph cannot answer this" → Task 4's module docstring and the source-derived collection; ":module/ namespace shared by three producers" → Task 1's `PRODUCERS` and Task 4's gitlink/import harvesting; "Stage 1" → Task 4; "Stage 2" → Tasks 1–2; "Stage 3" → Task 3; "Predictions" → Task 5; "Report fields" → Task 5's `build_report`; "Exit code" → Task 5's `measurement_invalid`; "Testing" (all six numbered requirements) → the tests in Tasks 1–4 (test 1 → Task 1, test 2 → Task 3, test 3 → Task 3, test 4 → Task 2, test 5 → Task 2 and Task 4, test 6 → Task 4's `TestCollectInputsEndToEnd`); "Deliverables" → Tasks 1–6, with `benchmark.md` in Task 6 and CLAUDE.md correctly untouched.

**Placeholder scan.** One deliberate placeholder remains: Task 6 Step 5's commit body and Step 6's issue comment cannot be written before the numbers exist. Both are marked as such with explicit instructions on what to fill in. Every code step carries real code.

**Type consistency.** `EntityInput` fields are `(entity_type, producer, file_path, name)` in Task 1 and used in that order in Tasks 2, 4 and 5. `group_by_ident` returns `Dict[str, List[EntityInput]]` in Task 1 and is consumed as such by `offenders`, `score_rule` and `build_report`. `RULES` values are `(description, ident_fn)` tuples in Task 3 and unpacked in that order in Task 5's CLI. `collect_inputs` returns `(inputs, diagnostics)` in Task 4 and is destructured that way in Task 5's `main`.
