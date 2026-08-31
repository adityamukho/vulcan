"""#260: the per-commit cost fit. Pure functions over synthetic traces -- no DB,
no ingestion, milliseconds."""

import json

import pytest

from evals.at_scale import trace_fit


def rec(w, apply_s, ckpt_count=0, ckpt_seconds=0.0, tag="fwd", pos=0):
    return {
        "pos": pos, "tag": tag, "hash": "x", "t_since_start": 0.0,
        "await_s": 0.0, "apply_s": apply_s,
        "ckpt_d_count": ckpt_count, "ckpt_d_seconds": ckpt_seconds,
        trace_fit.W_KEY: w,
    }


def group(n, a, b, w_lo=10, w_hi=500, ckpt_every=10, ckpt_seconds=1.0):
    """n records drawn from apply_s = a + b*W, with W spread across [w_lo, w_hi].

    The spread is what makes a and b identifiable -- it stands in for the real
    run's fwd/rev interleave, where small-work and large-work commits arrive at
    the same graph size.
    """
    out = []
    for i in range(n):
        w = w_lo + (w_hi - w_lo) * i / max(n - 1, 1)
        out.append(rec(
            w, a + b * w,
            ckpt_count=1 if i % ckpt_every == 0 else 0,
            ckpt_seconds=ckpt_seconds if i % ckpt_every == 0 else 0.0,
        ))
    return out


class TestFitLine:
    def test_recovers_a_and_b_from_an_exact_line(self):
        f = trace_fit.fit_line([1.0, 2.0, 3.0, 4.0] * 10, [3.0, 5.0, 7.0, 9.0] * 10)
        assert f["a"] == pytest.approx(1.0)
        assert f["b"] == pytest.approx(2.0)
        assert f["r2"] == pytest.approx(1.0)
        assert f["n"] == 40

    def test_zero_variance_in_W_is_unidentifiable_not_a_crash(self):
        """Every record with the same W leaves a and b unseparable. Returning a
        number here would invent an intercept from nothing."""
        assert trace_fit.fit_line([5.0] * 40, [1.0] * 40) is None

    def test_too_few_points_returns_none(self):
        n = trace_fit.MIN_POINTS_PER_GROUP - 1
        assert trace_fit.fit_line(list(range(n)), list(range(n))) is None

    def test_exactly_min_points_is_enough(self):
        n = trace_fit.MIN_POINTS_PER_GROUP
        assert trace_fit.fit_line(list(range(n)), list(range(n))) is not None


class TestGrowthRatio:
    def test_plain_ratio(self):
        assert trace_fit.growth_ratio(2.0, 5.0) == pytest.approx(2.5)

    @pytest.mark.parametrize("first", [0.0, -0.5])
    def test_non_positive_denominator_is_undefined(self, first):
        """An OLS intercept can legitimately come out negative or zero. A ratio
        across zero flips sign and reads as a small number -- which would report
        'flat' for a parameter that is not flat."""
        assert trace_fit.growth_ratio(first, 5.0) is None

    def test_none_operand_propagates(self):
        assert trace_fit.growth_ratio(None, 5.0) is None
        assert trace_fit.growth_ratio(2.0, None) is None


class TestVerdict:
    def test_both_flat_is_confounded(self):
        v, why = trace_fit.verdict(1.1, 1.2)
        assert v == "CONFOUNDED"

    def test_growing_intercept_alone_is_real(self):
        """a grows, b flat: fixed per-commit cost rising with graph size. This
        disjunct must be load-bearing on its own."""
        v, why = trace_fit.verdict(2.4, 1.05)
        assert v == "REAL"
        assert "a" in why

    def test_growing_slope_alone_is_real(self):
        """b grows, a flat: cost per unit work rising. The OTHER disjunct, tested
        with the first held flat."""
        v, why = trace_fit.verdict(1.05, 2.4)
        assert v == "REAL"
        assert "b" in why

    def test_middle_band_is_inconclusive(self):
        v, why = trace_fit.verdict(1.7, 1.2)
        assert v == "INCONCLUSIVE"

    def test_boundaries_are_exact(self):
        assert trace_fit.verdict(1.49, 1.49)[0] == "CONFOUNDED"
        assert trace_fit.verdict(1.5, 1.0)[0] == "INCONCLUSIVE"
        assert trace_fit.verdict(2.0, 1.0)[0] == "REAL"

    def test_undefined_ratio_is_inconclusive_never_confounded(self):
        """A None ratio means the fit could not be read. Treating that as flat
        would let a failed measurement argue for closing the issue."""
        assert trace_fit.verdict(None, 1.1)[0] == "INCONCLUSIVE"
        assert trace_fit.verdict(1.1, None)[0] == "INCONCLUSIVE"


class TestControlGate:
    def test_growing_checkpoint_duration_passes(self):
        first = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.10) for _ in range(10)]
        last = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.50) for _ in range(10)]
        g = trace_fit.control_gate(first, last)
        assert g["passed"] is True
        assert g["growth"] == pytest.approx(5.0)

    def test_flat_checkpoint_duration_fails_the_gate(self):
        """THE ABLATION FOR THE WHOLE EXPERIMENT. Checkpoint cost is documented
        O(graph size); a method that cannot see it grow has failed open, and a
        flat verdict from it means nothing. This must be red."""
        first = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.20) for _ in range(10)]
        last = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.20) for _ in range(10)]
        g = trace_fit.control_gate(first, last)
        assert g["passed"] is False
        assert g["growth"] == pytest.approx(1.0)

    def test_too_few_checkpoints_is_unevaluable_not_a_pass(self):
        first = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.1) for _ in range(2)]
        last = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=9.9) for _ in range(2)]
        g = trace_fit.control_gate(first, last)
        assert g["passed"] is False
        assert "checkpoint" in g["reason"].lower()

    def test_gate_reads_per_checkpoint_mean_not_total(self):
        """Total checkpoint time is DESIGNED not to grow -- the duty policy holds
        it to a fixed fraction of wall clock. Gating on the total would fail on
        healthy behaviour. Here totals are equal and the means differ 4x, and
        both groups individually clear CONTROL_MIN_CHECKPOINTS so the mean
        comparison is unconfounded by the checkpoint-count floor."""
        first = [rec(10, 1.0, ckpt_count=4, ckpt_seconds=0.2) for _ in range(5)]  # count=20, total=1.0, mean=0.05
        last = [rec(10, 1.0, ckpt_count=1, ckpt_seconds=0.2) for _ in range(5)]  # count=5, total=1.0, mean=0.20
        g = trace_fit.control_gate(first, last)
        assert g["growth"] == pytest.approx(4.0)
        assert g["passed"] is True

    def test_one_short_group_fails_even_if_combined_count_clears_the_floor(self):
        """THE PROOF THAT THE FLOOR IS PER-GROUP, NOT COMBINED. first has only 4
        checkpoints (below CONTROL_MIN_CHECKPOINTS=5) while last has 20 -- their
        combined count is 24, well over 5. A combined-count gate would wrongly
        pass this; the per-group gate must not be rescued by the other side."""
        first = [rec(10, 1.0, ckpt_count=4, ckpt_seconds=0.2)]
        last = [rec(10, 1.0, ckpt_count=20, ckpt_seconds=4.0)]
        g = trace_fit.control_gate(first, last)
        assert g["passed"] is False
        # M7: "checkpoint" alone is in BOTH unevaluable reasons (this one and
        # the growth-not-positive one), so it cannot discriminate which branch
        # fired; passed is False either way. Pin the exact reason instead --
        # it is the "need >= N checkpoints per group" branch, not the other.
        assert g["reason"] == (
            "unevaluable: need >= 5 checkpoints per group, saw 4 (first) and 20 (last)"
        )


class TestSplitThirds:
    def test_equal_counts_in_emission_order(self):
        records = [rec(i, 1.0, pos=i) for i in range(300)]
        a, b, c = trace_fit.split_thirds(records)
        assert len(a) == len(b) == len(c) == 100
        assert [r["pos"] for r in a] == list(range(100))
        assert [r["pos"] for r in c] == list(range(200, 300))

    def test_remainder_goes_to_the_middle_group(self):
        records = [rec(i, 1.0, pos=i) for i in range(302)]
        a, b, c = trace_fit.split_thirds(records)
        assert (len(a), len(b), len(c)) == (100, 102, 100)
        # First and last must stay the same size -- they are what the verdict
        # compares, and an uneven pair would bias the ratio.

    @pytest.mark.parametrize("n", [0, 1, 2])
    def test_fewer_than_three_records_puts_everything_in_the_first_group(self, n):
        """M8: pins CURRENT behaviour rather than changing it. `size = n // 3`
        is 0 for n < 3, and the early return then reads `records, [], []` --
        the first group gets every record, middle and last are empty.

        Kept as-is rather than "fixed" to return three near-empty groups: real
        traces are hundreds of records (MIN_POINTS_PER_GROUP=30 alone rules
        out anything this small ever reaching fit_line with a usable group),
        so this path only matters for a pathologically short or truncated
        trace -- where every group is going to read "not identifiable" via
        fit_line's own MIN_POINTS_PER_GROUP floor regardless of which group
        the leftover records land in. Changing the split shape here would
        touch trace_fit's core logic for a case that cannot affect a real
        verdict; pinning it documents the choice instead."""
        records = [rec(i, 1.0, pos=i) for i in range(n)]
        a, b, c = trace_fit.split_thirds(records)
        assert a == records
        assert b == []
        assert c == []


class TestAnalyse:
    def test_flat_trace_reports_confounded_when_control_passes(self):
        records = (
            group(120, a=0.5, b=0.001, ckpt_seconds=0.10)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.30)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.50)
        )
        out = trace_fit.analyse(records)
        assert out["control_gate"]["passed"] is True
        assert out["verdict"] == "CONFOUNDED"

    def test_growing_intercept_reports_real(self):
        records = (
            group(120, a=0.5, b=0.001, ckpt_seconds=0.10)
            + group(120, a=1.0, b=0.001, ckpt_seconds=0.30)
            + group(120, a=2.0, b=0.001, ckpt_seconds=0.50)
        )
        out = trace_fit.analyse(records)
        assert out["control_gate"]["passed"] is True
        assert out["verdict"] == "REAL"
        assert out["a_ratio"] == pytest.approx(4.0, rel=0.05)

    def test_void_when_the_control_gate_fails_regardless_of_the_fit(self):
        """A void run does not get to report CONFOUNDED. This is the guard
        against a broken measurement arguing for closing #260."""
        records = (
            group(120, a=0.5, b=0.001, ckpt_seconds=0.20)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.20)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.20)
        )
        out = trace_fit.analyse(records)
        assert out["control_gate"]["passed"] is False
        assert out["verdict"] == "VOID"

    def test_frozen_constants_have_their_spec_values(self):
        """These are pre-registered. A change here silently redefines the
        experiment, so pin them as literals against the spec."""
        assert trace_fit.W_KEY == "idents_considered"
        assert trace_fit.CLOSE_BELOW == 1.5
        assert trace_fit.REAL_AT == 2.0
        assert trace_fit.CONTROL_MIN_GROWTH == 2.0
        assert trace_fit.CONTROL_MIN_CHECKPOINTS == 5
        assert trace_fit.MIN_POINTS_PER_GROUP == 30


class TestProbeIO:
    """#260 probe: reading a trace and assembling the artifact. The fit itself
    is tested in TestAnalyse -- this covers only I/O and provenance."""

    def test_read_trace_skips_blank_lines(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('{"pos": 0}\n\n{"pos": 1}\n')
        from evals.at_scale import probe_per_commit_cost as probe
        assert [r["pos"] for r in probe.read_trace(p)] == [0, 1]

    def test_read_trace_tolerates_a_truncated_final_line(self, tmp_path):
        """A killed run leaves a half-written last record. That must cost one
        record, not the whole 30-minute trace."""
        p = tmp_path / "t.jsonl"
        p.write_text('{"pos": 0, "apply_s": 1.0}\n{"pos": 1, "apply')
        from evals.at_scale import probe_per_commit_cost as probe
        records = probe.read_trace(p)
        assert len(records) == 1

    def test_read_trace_treats_an_interior_malformed_line_as_fatal(self, tmp_path):
        """Corruption in the MIDDLE of the trace is not truncation. Silently
        dropping an interior record would bias the fit invisibly, so this must
        raise rather than being forgiven the way a truncated final line is."""
        p = tmp_path / "t.jsonl"
        p.write_text(
            '{"pos": 0, "apply_s": 1.0}\nnot json\n{"pos": 2, "apply_s": 1.0}\n'
        )
        from evals.at_scale import probe_per_commit_cost as probe
        with pytest.raises(SystemExit):
            probe.read_trace(p)

    def test_read_trace_refuses_an_empty_trace(self, tmp_path):
        """Zero records must be a hard error, not a verdict. An empty trace and
        a flat trace are not the same finding."""
        p = tmp_path / "t.jsonl"
        p.write_text("")
        from evals.at_scale import probe_per_commit_cost as probe
        with pytest.raises(SystemExit):
            probe.read_trace(p)

    def test_result_carries_interpreter_and_minigraf_provenance(self, tmp_path):
        from evals.at_scale import probe_per_commit_cost as probe
        records = group(120, a=0.5, b=0.001, ckpt_seconds=0.1) \
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.3) \
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.5)
        result = probe.build_result(
            records, {"commits_ingested": 360}, {}, trace_path="/tmp/t.jsonl",
        )
        assert "executable" in result["provenance"]
        assert "minigraf_version" in result["provenance"]
        assert result["provenance"]["stream_ratio"] == "1:1"
        assert result["verdict"] == "CONFOUNDED"

    def test_result_records_the_source_metrics_keys_it_used(self, tmp_path):
        """M6: `group(...) * 3` produced three identical copies of one group,
        not three distinct groups the way its sibling
        (test_result_carries_interpreter_and_minigraf_provenance) does --
        harmless for this test's own assertion (it only reads
        commits_ingested), but a landmine for anyone who later copies this
        fixture to test something group-shape-sensitive."""
        from evals.at_scale import probe_per_commit_cost as probe
        records = (
            group(120, a=0.5, b=0.001, ckpt_seconds=0.1)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.3)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.5)
        )
        result = probe.build_result(
            records, {"commits_ingested": 42}, {}, trace_path="/tmp/t.jsonl",
        )
        assert result["commits_ingested"] == 42

    def _fitted_records(self):
        return (
            group(120, a=0.5, b=0.001, ckpt_seconds=0.1)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.3)
            + group(120, a=0.5, b=0.001, ckpt_seconds=0.5)
        )

    # -- I2: carry the run's ingestion-health fields --------------------

    def test_ingestion_health_keys_absent_from_metrics_stay_absent(self):
        """Three-state discipline (#275/#276): a metrics dict that never had
        these keys (an older/partial run) must not have them coerced into
        existence -- e.g. as an empty list or False -- which would read as
        "measured clean" rather than "not recorded"."""
        from evals.at_scale import probe_per_commit_cost as probe
        result = probe.build_result(
            self._fitted_records(), {"commits_ingested": 42}, {},
            trace_path="/tmp/t.jsonl",
        )
        for key in (
            "skipped_commits", "error_signals", "stderr_capture_complete",
            "poll_duty_fraction", "checkpoint_summary", "ingest_error",
        ):
            assert key not in result

    def test_ingestion_health_keys_present_in_metrics_are_carried_through(self):
        """I2: these were the exact keys run_ingestion_benchmark.py:293-301
        added and the probe used to discard -- most importantly
        error_signals, where a #251 `Page N out of bounds` signature would
        show up."""
        from evals.at_scale import probe_per_commit_cost as probe
        metrics = {
            "commits_ingested": 42,
            "skipped_commits": ["deadbeef"],
            "error_signals": [{"pattern": "page_out_of_bounds", "line": "boom"}],
            "stderr_capture_complete": False,
            "poll_duty_fraction": 0.03,
            "checkpoint_summary": {"checkpoints": 5, "total_seconds": 1.2},
            # #270. _run_ingestion swallows its exception into
            # _ingest_progress["error"] and prints nothing, so for a run that
            # died before Stage A this is the ONLY record of what happened --
            # error_signals is empty precisely because nothing reached fd 2.
            "ingest_error": "RuntimeError: injected pre-policy failure",
        }
        result = probe.build_result(
            self._fitted_records(), metrics, {}, trace_path="/tmp/t.jsonl",
        )
        assert result["skipped_commits"] == ["deadbeef"]
        assert result["error_signals"] == [
            {"pattern": "page_out_of_bounds", "line": "boom"}
        ]
        # False, not coerced away or dropped -- absence and False must render
        # differently downstream.
        assert result["stderr_capture_complete"] is False
        assert result["poll_duty_fraction"] == pytest.approx(0.03)
        assert result["checkpoint_summary"] == {"checkpoints": 5, "total_seconds": 1.2}
        assert result["ingest_error"] == "RuntimeError: injected pre-policy failure"

    # -- I3: record the trace actually read ------------------------------

    def test_trace_path_records_the_trace_actually_read(self):
        from evals.at_scale import probe_per_commit_cost as probe
        result = probe.build_result(
            self._fitted_records(), {"commits_ingested": 42}, {},
            trace_path="/tmp/actual/trace.jsonl",
        )
        assert result["trace_path"] == "/tmp/actual/trace.jsonl"

    def test_trace_path_disagreement_warns_on_stderr_naming_both(self, capsys):
        """The --metrics re-analyse path can point --trace at a different
        file than the one the metrics JSON itself recorded (a stale metrics
        JSON re-pointed at a fresh trace, or vice versa). The artifact must
        record the trace ACTUALLY READ, and the disagreement must be visible,
        not silently swallowed."""
        from evals.at_scale import probe_per_commit_cost as probe
        result = probe.build_result(
            self._fitted_records(),
            {"commits_ingested": 42, "trace_path": "/tmp/stale/trace.jsonl"},
            {},
            trace_path="/tmp/actual/trace.jsonl",
        )
        assert result["trace_path"] == "/tmp/actual/trace.jsonl"
        err = capsys.readouterr().err
        assert "/tmp/actual/trace.jsonl" in err
        assert "/tmp/stale/trace.jsonl" in err

    def test_trace_path_agreement_is_silent(self, capsys):
        from evals.at_scale import probe_per_commit_cost as probe
        probe.build_result(
            self._fitted_records(),
            {"commits_ingested": 42, "trace_path": "/tmp/actual/trace.jsonl"},
            {},
            trace_path="/tmp/actual/trace.jsonl",
        )
        assert capsys.readouterr().err == ""

    # -- I4: --run refuses an existing --trace path -----------------------

    def test_refuse_existing_trace_for_run_raises_and_names_the_path(self, tmp_path):
        from evals.at_scale import probe_per_commit_cost as probe
        trace_path = tmp_path / "trace.jsonl"
        trace_path.write_text('{"pos": 0}\n')
        with pytest.raises(SystemExit) as exc_info:
            probe._refuse_existing_trace_for_run(trace_path)
        assert str(trace_path) in str(exc_info.value)
        assert "fresh" in str(exc_info.value)

    def test_refuse_existing_trace_for_run_allows_a_fresh_path(self, tmp_path):
        from evals.at_scale import probe_per_commit_cost as probe
        probe._refuse_existing_trace_for_run(tmp_path / "does-not-exist.jsonl")

    def test_main_with_run_refuses_before_touching_ingestion(self, tmp_path, monkeypatch):
        """The guard must fire before main() imports run_ingestion_benchmark
        or drives anything -- otherwise a reused --trace path would only be
        caught after a 30-minute run, which is the whole defect."""
        from evals.at_scale import probe_per_commit_cost as probe
        trace_path = tmp_path / "trace.jsonl"
        trace_path.write_text('{"pos": 0}\n')
        with pytest.raises(SystemExit) as exc_info:
            probe.main([
                "--run",
                "--graph-path", str(tmp_path / "g.graph"),
                "--trace", str(trace_path),
            ])
        assert str(trace_path) in str(exc_info.value)

    def test_main_without_run_allows_an_existing_trace(self, tmp_path):
        """The --trace/--metrics re-analyse path must still be able to read
        an existing trace -- that is its whole purpose. I4's guard is scoped
        to --run only."""
        from evals.at_scale import probe_per_commit_cost as probe
        trace_path = tmp_path / "trace.jsonl"
        lines = [json.dumps(r) for r in self._fitted_records()]
        trace_path.write_text("\n".join(lines) + "\n")
        metrics_path = tmp_path / "metrics.json"
        metrics_path.write_text(json.dumps({"commits_ingested": 360}))
        report_path = tmp_path / "benchmark.md"
        out_path = tmp_path / "out.json"
        rc = probe.main([
            "--trace", str(trace_path),
            "--metrics", str(metrics_path),
            "--out", str(out_path),
            "--report-path", str(report_path),
        ])
        assert rc == 0
        assert out_path.exists()
        assert "Per-Commit Cost Fit" in report_path.read_text()

    # -- I5: the pre-registered await_s/W correlation sanity check --------

    def test_pearson_perfect_positive_correlation(self):
        from evals.at_scale import probe_per_commit_cost as probe
        xs = [float(i) for i in range(30)]
        ys = [2.0 * x + 1.0 for x in xs]
        assert probe._pearson(xs, ys) == pytest.approx(1.0)

    def test_pearson_zero_variance_is_none(self):
        """Same convention as trace_fit.fit_line: zero variance cannot
        support a correlation, and inventing 0.0 would read as "measured and
        uncorrelated" rather than "could not be measured"."""
        from evals.at_scale import probe_per_commit_cost as probe
        assert probe._pearson([5.0] * 10, [1.0, 2.0] * 5) is None
        assert probe._pearson([1.0, 2.0] * 5, [5.0] * 10) is None

    def test_build_result_records_await_vs_w_correlation_and_totals(self):
        """I5: nothing computed this before -- the design spec pre-registered
        it as a sanity check on W's meaning, but the deliverable never wired
        it in. await_s = 2*W here gives a clean positive correlation in
        every third; the totals let a reader see why a weak real-world
        correlation would be uninformative rather than falsifying (await_s
        tiny relative to apply_s means the loop rarely stalls waiting on
        extraction)."""
        from evals.at_scale import probe_per_commit_cost as probe

        def rec(w, tag="fwd"):
            return {
                "pos": 0, "tag": tag, "hash": "x", "t_since_start": 0.0,
                "await_s": 2.0 * w, "apply_s": 1.0 + 0.001 * w,
                "ckpt_d_count": 1, "ckpt_d_seconds": 0.1,
                trace_fit.W_KEY: w,
            }

        records = [rec(float(w)) for w in range(90)]
        result = probe.build_result(
            records, {"commits_ingested": 90}, {}, trace_path="/tmp/t.jsonl",
        )
        exploratory = result["exploratory"]
        assert exploratory["await_s_total_seconds"] == pytest.approx(
            sum(2.0 * w for w in range(90))
        )
        assert exploratory["apply_s_total_seconds"] == pytest.approx(
            sum(1.0 + 0.001 * w for w in range(90))
        )
        assert exploratory["await_s_to_apply_s_ratio"] == pytest.approx(
            exploratory["await_s_total_seconds"] / exploratory["apply_s_total_seconds"]
        )
        pearson = exploratory["pearson_await_s_vs_W"]
        assert pearson["overall"] == pytest.approx(1.0)
        assert pearson["first_third"] == pytest.approx(1.0)
        assert pearson["middle_third"] == pytest.approx(1.0)
        assert pearson["last_third"] == pytest.approx(1.0)

    def test_build_result_await_s_to_apply_s_ratio_is_none_when_apply_s_is_zero(self):
        from evals.at_scale import probe_per_commit_cost as probe

        def rec(w):
            return {
                "pos": 0, "tag": "fwd", "hash": "x", "t_since_start": 0.0,
                "await_s": 0.0, "apply_s": 0.0,
                "ckpt_d_count": 0, "ckpt_d_seconds": 0.0,
                trace_fit.W_KEY: w,
            }

        records = [rec(float(w)) for w in range(30)]
        result = probe.build_result(
            records, {"commits_ingested": 30}, {}, trace_path="/tmp/t.jsonl",
        )
        assert result["exploratory"]["await_s_to_apply_s_ratio"] is None
