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
