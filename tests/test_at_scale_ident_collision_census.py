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

import subprocess
import sys

import pytest

from evals.at_scale import probe_ident_collision_census
from evals.at_scale.probe_ident_collision_census import (
    ENTITY_TYPES,
    PREDICTIONS,
    RULES,
    SHAPES,
    EntityInput,
    build_report,
    classify_shapes,
    collect_inputs,
    current_ident,
    evaluate_predictions,
    group_by_ident,
    inputs_from_commit_extraction,
    main,
    measurement_invalid,
    offenders,
    raw_value,
    score_all_rules,
    score_rule,
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


class TestInputsFromCommitExtraction:
    def test_deleted_files_carry_no_extraction_and_are_skipped(self):
        """_extract_commit returns extracted=None and precomputed=None for a
        "D" entry (contract at mcp_server.py:8477-8480, built at 8639).
        Dereferencing either would raise; treating the path as a live module
        input would invent an entity.
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
        """_gitlink_changes emits add/bump/remove (mcp_server.py:4597); all
        three fall into one `for kind, sha, path` loop that takes
        _code_ident("module", path) unconditionally (mcp_server.py:9555-9556),
        so all three name a submodule entity at that path.
        """
        got = inputs_from_commit_extraction(
            [], [("add", "sha1", "vendor/x"), ("bump", "sha2", "vendor/y"),
                 ("remove", "sha3", "vendor/z")]
        )
        paths = {inp.file_path for inp in got if inp.producer == "gitlink"}
        assert paths == {"vendor/x", "vendor/y", "vendor/z"}


def _make_repo(tmp_path, commits):
    """Build a single-branch git repo, applying each {path: text} dict as one
    commit. Shared by the collect_inputs tests, which need a REAL repo because
    they drive the real _extract_commit rather than a stub.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    for i, files in enumerate(commits):
        for name, text in files.items():
            (repo / name).write_text(text)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=repo, check=True)
    return repo


class TestCollectInputsEndToEnd:
    def test_a_private_public_pair_collides_through_real_extraction(self, tmp_path):
        """Drives the REAL _extract_commit over a purpose-built repo. Every
        other test in this file would still pass if Stage 1 collected the wrong
        triples -- this is the one that catches the audit mis-driving the
        extractor.

        Counterfactual: with only `def helper` in the file, offenders is empty.
        """
        repo = _make_repo(
            tmp_path,
            [{"mod.py": "def _helper():\n    pass\n\ndef helper():\n    pass\n"}],
        )

        inputs, diagnostics = collect_inputs(str(repo), "main", jobs=1)

        assert diagnostics["commits"] == 1
        assert diagnostics["extraction_failures"] == 0
        # The resolved ignore patterns decide which files were counted at all,
        # so a recorded run that omits them cannot explain why a re-run on the
        # same head_commit disagreed. Presence (not contents) is what is
        # pinned: the list depends on the environment's MINIGRAF_INGEST_IGNORE
        # and on any .temporalignore, which is exactly why it is recorded.
        assert isinstance(diagnostics["ignore_patterns"], list)
        found = offenders(group_by_ident(inputs, current_ident))
        assert len(found) == 1
        names = {inp.name for inp in next(iter(found.values()))}
        assert names == {"_helper", "helper"}

    def test_the_returned_inputs_do_not_depend_on_pool_scheduling(self, tmp_path):
        """The audit's output is a COMMITTED artifact, so which worker finished
        first must not reach it.

        Counterfactual: returning `list(seen)` -- consumption order -- fails the
        sorted-form assert even at jobs=1, because each file's module input is
        collected before the functions parsed out of it while "function" sorts
        before "module". With jobs > 1 it additionally varies between two
        otherwise identical runs, which is what the equality assert pins.
        """
        repo = _make_repo(
            tmp_path,
            [
                {"alpha.py": "import os\n\n\ndef a():\n    pass\n"},
                {"beta.py": "class B:\n    def m(self):\n        pass\n"},
            ],
        )

        first, _ = collect_inputs(str(repo), "main", jobs=2)
        second, _ = collect_inputs(str(repo), "main", jobs=2)

        # Guards the two asserts below against passing vacuously on an empty
        # or single-element list.
        assert len(first) > 4
        assert first == second
        assert first == sorted(
            first,
            key=lambda inp: (inp.entity_type, inp.producer, inp.file_path, inp.name or ""),
        )


def _report_over(inputs, **diag):
    diagnostics = {
        "head_commit": "abc123", "branch": "main", "commits": 10,
        "extraction_failures": 0, "failed_commits": [],
        "ignore_patterns": [],
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

    def test_what_shaped_the_file_set_is_recorded(self):
        """collect_inputs resolves the ignore patterns from
        MINIGRAF_INGEST_IGNORE plus any .temporalignore, so they decide which
        files were counted. Two runs on one head_commit can legitimately
        disagree, and only this field explains why.

        Counterfactual: a build_report that dropped the key still passes every
        other test in this class, and its artifact silently loses the reason.
        """
        report = _report_over([_fn("a/b.py", "solo")], ignore_patterns=["vendor/*"])
        assert report["ignore_patterns"] == ["vendor/*"]

    def test_branch_and_head_commit_are_both_recorded(self):
        """The audit resolves an unspecified branch through
        _default_git_branch, so head_commit is mainline's tip and NOT the
        working branch's. Recorded together, that reads as deliberate; with
        either missing it reads as a stale run.
        """
        report = _report_over([_fn("a/b.py", "solo")])
        assert report["branch"] == "main"
        assert report["head_commit"] == "abc123"


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

    def test_a_prediction_can_actually_hold(self):
        """The counterfactual for the test above: an evaluator hard-wired to
        "failed" also makes a failed prediction meaningless, and would pass it.
        The fixture is three same-file private/public pairs, so
        leading-underscore is the only named shape present.
        """
        evaluated = evaluate_predictions(_report_over(_known_collision_inputs()))
        assert evaluated["P2"]["outcome"] == "held"


class TestCli:
    def test_the_cli_prints_branch_head_and_exits_zero_on_a_valid_run(
        self, monkeypatch, capsys
    ):
        """main's stdout is what a reader of the recorded run sees first. The
        branch/head pair must be in it, because with no --branch the head is
        mainline's tip rather than the checked-out branch's.

        collect_inputs is stubbed rather than run: driving real history here
        would take minutes and is Task 6's job. Everything downstream of it --
        build_report, the gate, the exit code -- is the real code.
        """
        monkeypatch.setattr(
            probe_ident_collision_census,
            "collect_inputs",
            lambda repo_path, branch=None, jobs=None: (
                _known_collision_inputs(),
                {
                    "head_commit": "deadbee", "branch": "master", "commits": 7,
                    "extraction_failures": 0, "failed_commits": [],
                    "ignore_patterns": ["vendor/*"],
                },
            ),
        )
        monkeypatch.setattr(sys, "argv", ["probe", "--repo-path", "/repo"])

        assert main() == 0

        out = capsys.readouterr().out
        assert "master" in out
        assert "deadbee" in out
        assert "vendor/*" in out
        # The finding itself must not be reported as an error.
        assert "INVALID MEASUREMENT" not in out

    def test_the_cli_exits_nonzero_only_on_an_invalid_measurement(
        self, monkeypatch, capsys
    ):
        """The counterfactual for the test above, and the pair that pins the
        exit code to VALIDITY rather than to the finding: same offenders, but
        nothing was walked.
        """
        monkeypatch.setattr(
            probe_ident_collision_census,
            "collect_inputs",
            lambda repo_path, branch=None, jobs=None: (
                [],
                {
                    "head_commit": None, "branch": "master", "commits": 0,
                    "extraction_failures": 0, "failed_commits": [],
                    "ignore_patterns": [],
                },
            ),
        )
        monkeypatch.setattr(sys, "argv", ["probe"])

        assert main() == 1
        assert "INVALID MEASUREMENT" in capsys.readouterr().out
