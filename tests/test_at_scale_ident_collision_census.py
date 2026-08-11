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
from evals.at_scale.probe_ident_collision_census import (  # noqa: E402
    SHAPES,
    classify_shapes,
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
