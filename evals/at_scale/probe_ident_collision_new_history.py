# evals/at_scale/probe_ident_collision_new_history.py
"""#267 census: does history under the SHIPPED R3 ident rule collide?

#263 chose R3 (`_canonical_ident`: '_' kept inside the charset, hyphen-run
collapse dropped) on a measurement, not a proof. Its zero residual over 674
commits was the explicit, accepted cost of rejecting R4's hash suffix, and
`_canonical_ident`'s own docstring says so: "That zero is MEASURED, NOT PROVEN
BY CONSTRUCTION -- a contrived path/name combination can still collide."
`a/b.py` and `a-b.py` both reach `:module/a-b-py`.

Nothing was watching for that. `TestIdentCollisionRegression263` guards a fixed
9-pair corpus, so it catches a REGRESSION in the rule and cannot discover
anything new; `probe_ident_collision_census.py` is frozen at the PRE-#263 rule
and reproduces the historical experiment forever. This probe is the third row:
the same collection stage, production's live baseline, run over all of history.

WHY THIS IS A SEPARATE FILE AND NOT A FLAG ON THE FROZEN ONE. That artifact IS
the audit that chose R3. Its PREDICTIONS block was registered before any data
existed, and P3/P4 are claims about R5 and R2 as measured against the OLD
baseline. Re-pointing it at production would silently re-evaluate pre-registered
predictions against a different experiment while still printing them as "held".
The two must never share a baseline again, and that is pinned from both sides:
the frozen file's tests assert its rule is NOT production's, and this file's
assert that its rule IS.

WHY THERE IS NO --since BOUND. A collision is a property of a PAIR of inputs,
and the likely pair is a NEW entity against an OLD one -- an entity that has sat
in the tree for years. A `--since`-bounded collection sees only new-vs-new and
would report clean while missing exactly the case it was built for. The full
walk is also cheap: 835 commits in 94s (2026-09-01), which is noise beside the
nightly's ~50-minute ingestion step. If it ever stops fitting, the answer is a
cached input manifest keyed on head_commit that new commits are unioned into --
that preserves new-vs-old; a `--since` bound does not, at any price.

WHAT THIS REPORTS AND WHAT IT DOES NOT. Offenders only. No candidate-rule
bake-off and no predictions block: the rule choice is made, and both of those
belong to the experiment that made it.

READ-ONLY BY CONSTRUCTION, inherited from the collection stage: no MiniGrafDb
handle is opened, no facts are written, and mcp_server is read but never
mutated.

EXIT CODE. A collision is a FINDING, not an invalid run, so by default it exits
0 -- the same reasoning as the frozen probe's exit gate. `--fail-on-collision`
is for callers that need it to be loud (the at-scale nightly passes it, so a
find reaches a human through #295's issue-filing path rather than sitting in a
green run's log). `measurement_invalid` is a separate axis and exits 1
regardless of the flag: a run that walked nothing must never read as clean.

See docs/superpowers/specs/2026-08-14-ident-rule-r3-and-format-version-design.md
and evals/at_scale/benchmark.md.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import mcp_server  # noqa: E402

# The collection stage, the input shape, the grouping and the shape classifier
# are reused verbatim from the frozen probe -- they are the parts that describe
# HISTORY, not the parts that describe the pre-#263 RULE. `current_ident`,
# `_slug_current`, `RULES` and `PREDICTIONS` are the frozen half and are
# deliberately NOT imported here; importing `current_ident` would make this
# probe measure a rule nobody ships.
from evals.at_scale.probe_ident_collision_census import (  # noqa: E402
    ENTITY_TYPES,
    SHAPES,
    EntityInput,
    classify_shapes,
    collect_inputs,
    group_by_ident,
    offenders,
)

__all__ = [
    "ENTITY_TYPES",
    "SHAPES",
    "EntityInput",
    "build_report",
    "classify_shapes",
    "collect_inputs",
    "group_by_ident",
    "main",
    "measurement_invalid",
    "offenders",
    "production_ident",
]


def production_ident(inp: EntityInput) -> str:
    """The ident production mints for this input, TODAY.

    A live call into `mcp_server._code_ident`, never a hand copy. The frozen
    probe keeps a hand copy on purpose -- it must reproduce a historical rule
    that production has moved off. This one must track production, so a copy
    would be a defect: it would drift the day the rule changes and go on
    reporting a clean census of a rule nobody ships.

    Producer "import" needs no separate path: it stores the raw specifier in
    file_path with name None, and `_code_ident(t, spec, None)` is exactly the
    bare `_canonical_ident(t, spec)` that `_resolve_module_ident` calls
    (mcp_server.py:4250, 4285).
    """
    return mcp_server._code_ident(inp.entity_type, inp.file_path, inp.name)


def build_report(
    inputs: Sequence[EntityInput],
    diagnostics: Dict[str, Any],
    repo_path: str,
) -> Dict[str, Any]:
    """Assemble the committed artifact.

    Carries branch, head_commit AND ignore_patterns because all three decide
    which idents were counted and none is recoverable from the numbers
    afterwards. With no --branch, head_commit is mainline's tip rather than the
    checked-out branch's -- see collect_inputs.
    """
    groups = group_by_ident(inputs, production_ident)
    all_offenders = offenders(groups)

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
                1 for _ident, members in groups.items()
                if members[0].entity_type == entity_type
            ),
            "count": len(rows),
            # Verbatim members, not just a count. A nonzero count here reopens
            # the R3-vs-R4 decision, and that work starts from the data rather
            # than from re-deriving it by hand. There is no truncation policy
            # because there is nothing to truncate yet; if a run ever produces
            # enough offenders for that to matter, the volume is itself the
            # finding.
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

    return {
        "repo_path": repo_path,
        "branch": diagnostics["branch"],
        "head_commit": diagnostics["head_commit"],
        # Indexed, not .get()-with-default: a silent [] here would record "no
        # patterns were applied" for a run whose patterns simply were not
        # threaded, which is exactly the ambiguity this field exists to end.
        "ignore_patterns": diagnostics["ignore_patterns"],
        "commits": diagnostics["commits"],
        "extraction_failures": diagnostics["extraction_failures"],
        "failed_commits": diagnostics["failed_commits"],
        # Counted over DISTINCT EntityInputs, whose key is the 4-tuple
        # including `producer`, so one (type, path, name) triple reached from
        # two producers counts twice. Read it as "distinct inputs".
        "triples_total": len(inputs),
        "idents_total": len(groups),
        "offenders": per_type,
        "offenders_total": len(all_offenders),
        "offenders_by_shape": shape_counts,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


def measurement_invalid(report: Dict[str, Any]) -> Optional[str]:
    """Why this run cannot be believed, or None.

    VALIDITY ONLY. A nonzero collision count is the number #267 asks for and
    must NEVER reach this gate -- use --fail-on-collision if a caller needs a
    find to be loud. Do not widen this to make a run "pass".

    The zero-inputs clause is not hypothetical. While this probe was being
    written, a driver without multiprocessing's spawn guard killed every worker
    and printed a confident "0 collisions" over 835 commits with 0 inputs
    collected. A census whose collection failed outright reports the same
    number as a clean history.
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
            "bound rather than the exact number this census exists to produce."
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Census real history for _code_ident collisions under the SHIPPED "
            "R3 rule. Reports offenders; does not choose a rule."
        )
    )
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--json-out", "--output", dest="json_out", default=None)
    parser.add_argument(
        "--fail-on-collision",
        action="store_true",
        help=(
            "Exit 1 if any collision is found. OFF by default: a collision is a "
            "measurement, not an invalid run. The at-scale nightly passes this "
            "so a find reaches a human instead of sitting in a green log."
        ),
    )
    args = parser.parse_args()

    inputs, diagnostics = collect_inputs(args.repo_path, args.branch, jobs=args.jobs)
    report = build_report(inputs, diagnostics, args.repo_path)

    print(json.dumps(report, indent=2))
    print()
    print(f"repo:                   {report['repo_path']} @ {report['branch']}")
    # Printed next to the branch on purpose. With no --branch this is
    # mainline's tip rather than the checked-out branch's, which reads as a
    # stale run unless both are visible together.
    print(f"head:                   {report['head_commit']}")
    print(f"ignore patterns:        {report['ignore_patterns'] or '(none)'}")
    print(f"commits:                {report['commits']}")
    print(f"extraction failures:    {report['extraction_failures']}")
    print(f"distinct inputs:        {report['triples_total']}")
    print(f"distinct idents:        {report['idents_total']}")
    print()
    print("OFFENDERS UNDER THE SHIPPED RULE (idents reachable from >1 input)")
    for entity_type in ENTITY_TYPES:
        row = report["offenders"][entity_type]
        print(f"  {entity_type:<12} {row['count']:>6} of {row['idents_total']}")
    print(f"  {'TOTAL':<12} {report['offenders_total']:>6}")
    print()
    print("BY SHAPE (an offender may carry more than one label)")
    for shape in SHAPES:
        print(f"  {shape:<20} {report['offenders_by_shape'][shape]:>6}")

    if report["offenders_total"]:
        print()
        print("COLLIDING IDENTS")
        for entity_type in ENTITY_TYPES:
            for ident, members in report["offenders"][entity_type]["idents"].items():
                print(f"  {ident}")
                for member in members:
                    name = member["name"] or "(none)"
                    print(
                        f"      {member['producer']:<8} {member['file_path']}  "
                        f"name={name}"
                    )
        print()
        print(
            "R3's zero residual was MEASURED over 674 commits, never proven by\n"
            "construction -- this is that measurement coming back nonzero, not a\n"
            "broken probe. Reopen #263's rule choice with these pairs in hand."
        )

    reason = measurement_invalid(report)
    if reason:
        print()
        print(f"INVALID MEASUREMENT. {reason}")
        print("Do not adjust this gate to make a run pass.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        print(f"\nWrote {args.json_out}")

    if reason:
        return 1
    if args.fail_on_collision and report["offenders_total"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
