# tests/test_at_scale_ident_collision_new_history.py
"""Unit tests for the #267 census of NEW history under the shipped R3 rule.

The headline number cannot be asserted -- discovering it is the point. What CAN
be asserted is everything that could silently produce a WRONG number: that the
baseline really is production's rule and not the frozen probe's, that a
constructed collision is reported, that the exit code separates "found
something" from "measured nothing", and that `--fail-on-collision` actually
changes the exit code.

Every test names the counterfactual it was checked against. A test that passes
against a degenerate stub as well as against the real implementation proves
nothing and does not belong here.
"""

import subprocess
import sys

import pytest

import mcp_server
from evals.at_scale import probe_ident_collision_new_history
from evals.at_scale.probe_ident_collision_census import (
    EntityInput,
    current_ident,
)
from evals.at_scale.probe_ident_collision_new_history import (
    ENTITY_TYPES,
    SHAPES,
    build_report,
    collect_inputs,
    group_by_ident,
    main,
    measurement_invalid,
    offenders,
    production_ident,
)


def _fn(file_path, name):
    return EntityInput("function", "code", file_path, name)


def _mod(file_path):
    return EntityInput("module", "code", file_path, None)


# `a/b.py` and `a-b.py` both slug to `a-b-py` under R3: the separator '/' and a
# literal '-' are both outside [a-z0-9_-] / already inside it and land on the
# same character. This is the pair evals/at_scale/benchmark.md already names as
# the reason R3's zero is MEASURED and not proven by construction, so it is the
# honest positive control for a census that exists to find such a thing.
R3_COLLIDING_PATHS = ("a/b.py", "a-b.py")


def _r3_collision_inputs():
    return [_mod(path) for path in R3_COLLIDING_PATHS]


class TestBaselineIsProduction:
    """The one property that separates this probe from the frozen one.

    probe_ident_collision_census.py is FROZEN at the pre-#263 rule so its
    pre-registered predictions keep meaning what they meant. This probe is the
    opposite: it exists to track production, and the moment it stops doing so
    it is measuring a rule nobody ships. The frozen file asserts "I am NOT
    production"; this file asserts "I AM". Both directions are pinned so the
    two can never quietly share a baseline again.
    """

    ADVERSARIAL = [
        _mod(""),
        _mod("::"),
        _mod("---"),
        _mod("mcp.server"),
        _mod("mcp_server"),
        _mod("a//b..c__d--e"),
        _fn("tests/test_mcp_server.py", "_commit"),
        _fn("tests/test_mcp_server.py", "commit"),
        _fn("MixedCase/Path.PY", "HelperName"),
        _fn("a/b.py", "Cls._x"),
        _fn("9lives/2fast.py", "_3d"),
        _fn("evals/at_scale/probe.py", "__init__"),
        EntityInput("module", "import", "os.path", None),
        EntityInput("field", "code", "a/b.py", "Cls.field"),
    ]

    @pytest.mark.parametrize("inp", ADVERSARIAL, ids=lambda i: f"{i.file_path}::{i.name}")
    def test_the_baseline_is_productions_code_ident(self, inp):
        assert production_ident(inp) == mcp_server._code_ident(
            inp.entity_type, inp.file_path, inp.name
        )

    def test_the_baseline_is_not_the_frozen_probes_rule(self):
        """The inverse of the frozen file's
        test_the_frozen_rule_is_no_longer_productions_rule. If this ever fails,
        either #263 has been undone or this probe has been re-pointed at the
        frozen baseline -- and in both cases the census is measuring history
        under a rule that is not shipped.
        """
        inp = _fn("tests/test_mcp_server.py", "_commit")
        assert production_ident(inp) != current_ident(inp)

    def test_the_two_baselines_genuinely_disagree_on_this_corpus(self):
        """Positive control for the test above. Without it, a corpus on which
        the frozen rule and production happen to agree would make that
        assertion unfalsifiable -- it would be asserting a difference that was
        never there to find. `_commit` is the pair the pre-#263 rule collapsed
        onto `commit` and R3 separates.
        """
        private, public = (
            _fn("tests/test_mcp_server.py", "_commit"),
            _fn("tests/test_mcp_server.py", "commit"),
        )
        assert current_ident(private) == current_ident(public)
        assert production_ident(private) != production_ident(public)


class TestOffendersUnderProduction:
    def test_a_constructed_r3_collision_is_reported(self):
        """The probe's reason to exist: R3's zero residual is measured over
        history, not proven, so a path pair like this still collides. If this
        census could not see it, a real one appearing in new history would be
        just as invisible.

        Counterfactual: with `a/b.py` and `c/d.py` the offender set is empty --
        pinned by the test below.
        """
        found = offenders(group_by_ident(_r3_collision_inputs(), production_ident))

        assert len(found) == 1
        assert set(found) == {":module/a-b-py"}
        assert {m.file_path for m in found[":module/a-b-py"]} == set(R3_COLLIDING_PATHS)

    def test_unrelated_paths_are_not_offenders(self):
        """The counterfactual for the test above. Without it, an `offenders`
        degraded to "return every group" passes it.
        """
        found = offenders(
            group_by_ident([_mod("a/b.py"), _mod("c/d.py")], production_ident)
        )
        assert found == {}

    def test_the_263_underscore_family_no_longer_collides_under_production(self):
        """The same claim TestIdentCollisionRegression263 makes, asserted
        through THIS probe's grouping path rather than through _code_ident
        directly. A census whose grouping re-collapsed private names would
        report the #263 family as live offenders forever and drown any genuine
        new finding.
        """
        pairs = [
            _fn("tests/test_mcp_server.py", "_commit"),
            _fn("tests/test_mcp_server.py", "commit"),
            _fn("tests/test_mcp_server.py", "_snapshot"),
            _fn("tests/test_mcp_server.py", "snapshot"),
        ]
        assert offenders(group_by_ident(pairs, production_ident)) == {}
        # Positive control: these four DID collapse onto two idents under the
        # rule #263 replaced, so the clean result above is a property of R3 and
        # not of a corpus that never collided.
        assert len(offenders(group_by_ident(pairs, current_ident))) == 2


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
        """A nonzero count escalates this to fix design, and that work starts
        from the data rather than re-deriving it by hand. Counterfactual: a
        report carrying only counts passes every other test in this class.
        """
        report = _report_over(_r3_collision_inputs())
        rows = report["offenders"]["module"]["idents"]
        assert sorted(m["file_path"] for m in rows[":module/a-b-py"]) == [
            "a-b.py", "a/b.py"
        ]

    def test_every_entity_type_appears_even_at_zero(self):
        """A missing key and a zero are different claims. Only the second says
        "measured, found none"."""
        report = _report_over([_fn("a/b.py", "solo")])
        assert set(report["offenders"]) == set(ENTITY_TYPES)
        assert report["offenders"]["class"]["count"] == 0

    def test_every_shape_appears_even_at_zero(self):
        report = _report_over(_r3_collision_inputs())
        assert set(report["offenders_by_shape"]) == set(SHAPES)

    def test_no_candidate_rules_and_no_predictions(self):
        """The rule choice is MADE. A candidate bake-off here would re-run the
        #263 experiment against a different baseline, and a predictions block
        would print "held" for statements nobody registered before the data
        existed -- the exact property the frozen probe was frozen to protect.
        """
        report = _report_over(_r3_collision_inputs())
        assert "candidates" not in report
        assert "predictions" not in report

    def test_what_shaped_the_file_set_is_recorded(self):
        """The resolved patterns decide which files were counted at all, so two
        runs on one head_commit can legitimately disagree and only this field
        explains why. Counterfactual: a build_report that dropped the key
        passes every other test in this class.
        """
        report = _report_over([_fn("a/b.py", "solo")], ignore_patterns=["vendor/*"])
        assert report["ignore_patterns"] == ["vendor/*"]

    def test_branch_and_head_commit_are_both_recorded(self):
        """collect_inputs resolves an unspecified branch through
        _default_git_branch, so head_commit is mainline's tip and NOT the
        working branch's. Recorded together that reads as deliberate; with
        either missing it reads as a stale run.
        """
        report = _report_over([_fn("a/b.py", "solo")])
        assert report["branch"] == "main"
        assert report["head_commit"] == "abc123"

    def test_counts_are_over_distinct_inputs(self):
        report = _report_over(_r3_collision_inputs() + [_fn("z/z.py", "solo")])
        assert report["triples_total"] == 3
        assert report["idents_total"] == 2
        assert report["offenders_total"] == 1


class TestMeasurementInvalid:
    def test_a_clean_run_with_collisions_is_still_valid(self):
        """The gate is about VALIDITY, never about the finding. Finding a
        collision is what this probe is for; a run that found one measured
        correctly.
        """
        report = _report_over(_r3_collision_inputs())
        assert report["offenders_total"] == 1
        assert measurement_invalid(report) is None

    def test_zero_commits_is_invalid(self):
        report = _report_over(_r3_collision_inputs(), commits=0)
        assert "Zero commits" in measurement_invalid(report)

    def test_zero_inputs_is_invalid(self):
        """The failure mode this probe was actually bitten by while being
        written: a spawn-start bug made every worker die, and the run printed a
        confident "0 collisions" over 835 commits and 0 inputs. A census that
        collected nothing must never be readable as a clean result.
        """
        report = _report_over([], commits=835)
        assert "Zero inputs" in measurement_invalid(report)

    def test_too_many_extraction_failures_is_invalid(self):
        report = _report_over(_r3_collision_inputs(), commits=100, extraction_failures=2)
        assert "2 of 100" in measurement_invalid(report)

    def test_a_tolerable_extraction_failure_rate_stays_valid(self):
        """The counterfactual for the test above: the threshold is 1%, so a
        gate hard-wired to "any failure is invalid" fails here.
        """
        report = _report_over(_r3_collision_inputs(), commits=100, extraction_failures=1)
        assert measurement_invalid(report) is None


def _stub_collect(monkeypatch, inputs, **diag):
    diagnostics = {
        "head_commit": "deadbee", "branch": "master", "commits": 7,
        "extraction_failures": 0, "failed_commits": [],
        "ignore_patterns": ["vendor/*"],
    }
    diagnostics.update(diag)
    monkeypatch.setattr(
        probe_ident_collision_new_history,
        "collect_inputs",
        lambda repo_path, branch=None, jobs=None: (inputs, diagnostics),
    )


class TestCli:
    def test_a_collision_is_reported_and_exits_zero_by_default(self, monkeypatch, capsys):
        """The issue's own wording: finding a collision is a measurement, not
        an invalid run. The default exit code says so.
        """
        _stub_collect(monkeypatch, _r3_collision_inputs())
        monkeypatch.setattr(sys, "argv", ["probe", "--repo-path", "/repo"])

        assert main() == 0

        out = capsys.readouterr().out
        assert "master" in out
        assert "deadbee" in out
        assert "vendor/*" in out
        assert ":module/a-b-py" in out
        assert "INVALID MEASUREMENT" not in out

    def test_fail_on_collision_flips_the_exit_code_on_the_same_corpus(
        self, monkeypatch, capsys
    ):
        """Same inputs, same report, one flag. Paired with the test above, this
        pins the flag as the ONLY thing that changed the exit code --
        counterfactual: a main() that ignores the flag passes one of the two.
        """
        _stub_collect(monkeypatch, _r3_collision_inputs())
        monkeypatch.setattr(sys, "argv", ["probe", "--fail-on-collision"])

        assert main() == 1
        assert "INVALID MEASUREMENT" not in capsys.readouterr().out

    def test_fail_on_collision_exits_zero_when_nothing_collided(self, monkeypatch):
        """The other counterfactual: the flag must fail on the FINDING, not on
        its own presence. A main() that returned 1 whenever the flag was passed
        passes the test above and fails this one.
        """
        _stub_collect(monkeypatch, [_fn("a/b.py", "solo")])
        monkeypatch.setattr(sys, "argv", ["probe", "--fail-on-collision"])

        assert main() == 0

    def test_an_invalid_measurement_exits_nonzero_without_the_flag(
        self, monkeypatch, capsys
    ):
        """Validity and the finding are separate exits. A run that walked
        nothing must fail even when nobody asked to fail on collisions.
        """
        _stub_collect(monkeypatch, [], commits=0, head_commit=None)
        monkeypatch.setattr(sys, "argv", ["probe"])

        assert main() == 1
        assert "INVALID MEASUREMENT" in capsys.readouterr().out


def _make_repo(tmp_path, commits):
    """Build a single-branch git repo, applying each {path: text} dict as one
    commit. A REAL repo, because this drives the real _extract_commit."""
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
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=repo, check=True)
    return repo


class TestEndToEnd:
    def test_a_real_r3_collision_is_found_through_real_extraction(self, tmp_path):
        """Drives the REAL collection stage and the REAL production rule over a
        purpose-built repo. Every other test in this file would still pass if
        the probe mis-drove the extractor or grouped the wrong field.

        Counterfactual: rename `a-b.py` to `c-d.py` and offenders is empty.
        """
        repo = _make_repo(
            tmp_path,
            [{"a/b.py": "x = 1\n", "a-b.py": "y = 2\n"}],
        )

        inputs, diagnostics = collect_inputs(str(repo), "main", jobs=1)
        report = build_report(inputs, diagnostics, str(repo))

        assert report["commits"] == 1
        assert report["extraction_failures"] == 0
        assert measurement_invalid(report) is None
        assert report["offenders"]["module"]["count"] == 1
        assert ":module/a-b-py" in report["offenders"]["module"]["idents"]

    def test_a_repo_without_a_collision_reports_a_clean_census(self, tmp_path):
        """The counterfactual above, run for real. This is the shape every
        nightly run is expected to produce, so it must be distinguishable from
        a run that collected nothing -- hence the triples_total assert.
        """
        repo = _make_repo(
            tmp_path,
            [{"a/b.py": "x = 1\n", "c/d.py": "y = 2\n"}],
        )

        inputs, diagnostics = collect_inputs(str(repo), "main", jobs=1)
        report = build_report(inputs, diagnostics, str(repo))

        assert report["offenders_total"] == 0
        assert report["triples_total"] > 0
        assert measurement_invalid(report) is None
