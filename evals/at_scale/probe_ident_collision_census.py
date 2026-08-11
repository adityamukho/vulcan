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
