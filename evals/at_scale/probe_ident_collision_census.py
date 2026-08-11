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
import multiprocessing
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import combinations
from pathlib import Path
from typing import (
    Any,
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


# ---------------------------------------------------------------------------
# Stage 1: collect the inputs, through the real extractor
# ---------------------------------------------------------------------------


def inputs_from_commit_extraction(
    file_results: Sequence[tuple],
    gitlink_changes: Sequence[tuple],
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

    for _status, file_path, extracted, precomputed, _old_path in file_results:
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

    # add/bump/remove all reach one `for kind, sha, path` loop that takes
    # _code_ident("module", path) unconditionally (mcp_server.py:9555-9556),
    # so every kind contributes.
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

    The pool uses an explicit "spawn" context for the same reason
    _run_ingestion does (mcp_server.py:10536-10546): workers are created lazily
    while other threads may already be alive in this process (this runs under
    pytest, and Task 5 puts a CLI on top of it), and forking with a thread
    holding a lock deadlocks the child forever. It also makes the start method
    independent of the platform default, which changed in 3.14.
    """
    resolved_branch = branch or mcp_server._default_git_branch(repo_path)
    ignore_patterns = mcp_server._load_ignore_patterns(repo_path)
    commits = mcp_server._git_commits(repo_path, None, resolved_branch)
    # _git_commits walks --topo-order --reverse, so hashes[-1] is the branch
    # tip. Taken from the walked list, not a separate rev-parse, so the
    # reported head cannot disagree with what was actually measured.
    hashes = [row[0] for row in commits]

    seen: Dict[EntityInput, None] = {}
    failed: List[str] = []

    with ProcessPoolExecutor(
        max_workers=jobs, mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        futures = {
            executor.submit(
                mcp_server._extract_commit, repo_path, commit_hash, ignore_patterns
            ): commit_hash
            for commit_hash in hashes
        }
        # as_completed drains in completion order, so a straggler at position 0
        # does not hold the loop. That alone bounds NOTHING, though: a Future
        # keeps its _result after result() returns, and this dict keeps a
        # strong reference to every future for the whole `with` block, so peak
        # memory would be identical to iterating futures.items(). The pop is
        # what actually drops the reference and lets a consumed result be
        # collected. Safe because as_completed snapshots its argument before
        # the first loop body runs.
        for future in as_completed(futures):
            commit_hash = futures.pop(future)
            try:
                file_results, gitlink_changes, _gitmodules, _renamed = future.result()
            except Exception:
                # Per-commit failure must not abort the audit -- it is a
                # measurement diagnostic, and the report's exit gate decides
                # whether there were too many for the run to mean anything.
                failed.append(commit_hash)
                continue
            for inp in inputs_from_commit_extraction(file_results, gitlink_changes):
                seen[inp] = None

    # Completion order is whatever the pool happened to finish first; sort back
    # into walk order so a re-run's recorded diagnostic is byte-comparable.
    walk_order = {commit_hash: i for i, commit_hash in enumerate(hashes)}
    failed.sort(key=walk_order.__getitem__)

    # `seen` was filled in result-consumption order, i.e. pool completion order,
    # which varies run to run once jobs > 1. This audit's output is a COMMITTED
    # artifact, so scheduling must not reach it: impose a total ordering on the
    # inputs themselves. name is Optional, and sorting None against str raises,
    # hence `or ""`.
    ordered = sorted(
        seen,
        key=lambda inp: (inp.entity_type, inp.producer, inp.file_path, inp.name or ""),
    )

    return ordered, {
        "head_commit": hashes[-1] if hashes else None,
        "branch": resolved_branch,
        "commits": len(hashes),
        "extraction_failures": len(failed),
        "failed_commits": failed,
    }
