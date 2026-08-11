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

import hashlib
import re
import sys
from itertools import combinations
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

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
    #
    # The guard compares NAME casefold, not exact name, and requires that
    # comparison to be UNEQUAL -- i.e. that the pair is not already
    # explainable by case alone. Without it, a pair with no underscore at
    # all (Foo/foo) also satisfies the stripped-casefold equality below,
    # since stripping is a no-op when there is nothing to strip, and
    # leading-underscore would fire on top of a plain case-only pair. That
    # is the same false-positive shape as Finding 1, just on the other
    # check.
    a_name_cf = (a.name or "").casefold()
    b_name_cf = (b.name or "").casefold()
    if (
        a_name_cf != b_name_cf
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

    An offender may carry more than one label -- the per-pair checks are
    independent, not exclusive, so a cross-producer collision is usually also
    something else. "other" is emitted only when nothing else applied: it is
    where an unpredicted collision family would surface, and collapsing it
    into any named label would hide exactly the finding worth having.
    """
    shapes: Set[str] = set()
    for a, b in combinations(members, 2):
        shapes |= _pair_shapes(a, b)
    if not shapes - {"cross-producer"}:
        shapes.add("other")
    return shapes


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
