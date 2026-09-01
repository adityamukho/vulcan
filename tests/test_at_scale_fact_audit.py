# tests/test_at_scale_fact_audit.py
"""#302: the fact-index cross-check, and the corruption stderr cannot see.

Real backend throughout (docs/testing-conventions.md): every graph here is a
real file-backed minigraf graph with its real SQLite fact index alongside,
written through mcp_server's own handlers. A fake would defeat the point --
the whole subject is what the two storage engines disagree about.
"""

import shutil
import sqlite3

import pytest

from evals.at_scale.fact_audit import audit_graph_against_index, entity_uuid
from evals.at_scale.stderr_capture import scan_ingestion_stderr, tee_stderr

PAGE_SIZE = 4096


def _write_graph(tmp_path, monkeypatch, facts):
    """Build a real file-backed graph + fact index and return their paths.

    Checkpointed and unbound before returning: the corruption tests copy the
    graph FILE, so anything still in the WAL would make them measure the WAL's
    absence instead of the garbled page.
    """
    import mcp_server

    graph_path = tmp_path / "t.graph"
    monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(graph_path))
    monkeypatch.delenv("MINIGRAF_INDEX_PATH", raising=False)
    mcp_server._reset_db_state()
    mcp_server.open_db(str(graph_path))
    for fact in facts:
        result = mcp_server.handle_minigraf_transact(fact, reason="fact audit test")
        assert result.get("ok"), result
    with mcp_server.db_lease() as db:
        db.checkpoint()
    mcp_server._reset_db_state()
    return graph_path, tmp_path / "t.graph.fts.sqlite3"


def _bind(graph_path):
    import mcp_server

    mcp_server._reset_db_state()
    mcp_server.open_db(str(graph_path))


def _garble(graph_path, index_path, dest_dir, page):
    """Copy the pair into dest_dir and overwrite one page with 0xff."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    graph = dest_dir / graph_path.name
    index = dest_dir / index_path.name
    shutil.copyfile(graph_path, graph)
    shutil.copyfile(index_path, index)
    with open(graph, "r+b") as f:
        f.seek(page * PAGE_SIZE)
        f.write(b"\xff" * PAGE_SIZE)
    return graph, index


@pytest.fixture
def graph_pair(tmp_path, monkeypatch):
    """A graph with enough entities to spread across many pages.

    30 is not arbitrary: a sweep of every page of a 300-entity graph found
    each entity's facts on its own page, so a graph this size guarantees the
    garbled page below lands on real data rather than on free space.
    """
    facts = [
        f'[[:decision/d{i} :description "value number {i} with text to fill the page"]]'
        for i in range(30)
    ]
    graph_path, index_path = _write_graph(tmp_path, monkeypatch, facts)
    yield graph_path, index_path
    import mcp_server

    mcp_server._reset_db_state()


class TestCleanGraph:
    def test_a_clean_graph_diverges_by_exactly_zero(self, graph_pair):
        """Zero, not "small". The gate in _exit_code has no tolerance, so a
        clean run that diverged even by one would be permanent false red."""
        graph_path, index_path = graph_pair
        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["audit_error"] is None
        assert result["graph_facts"] > 0
        assert result["divergence"] == 0, result["missing_from_graph_sample"]

    def test_both_witnesses_see_the_same_number_of_facts(self, graph_pair):
        graph_path, index_path = graph_pair
        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["graph_facts"] == result["index_current_rows"]


class TestNormalizationsAreLoadBearing:
    """Each test here first asserts the PRECONDITION that makes its
    normalization necessary. Without that, a passing assertion of "divergence
    == 0" proves only that the case never arose (#302 review of the earlier
    probe, which measured 138 phantom divergences for exactly this reason)."""

    def test_an_int_valued_fact_does_not_diverge(self, tmp_path, monkeypatch):
        import mcp_server

        graph_path, index_path = _write_graph(
            tmp_path, monkeypatch, ['[[:ingestion/format-version :version 1]]']
        )
        _bind(graph_path)
        rows = mcp_server.handle_minigraf_query(
            "[:find ?v :where [?e :version ?v]]"
        )["results"]
        # The precondition: minigraf hands back a typed value while the index
        # stored the datalog text '1'. If this ever becomes a str, the str()
        # in _graph_facts stops being load-bearing and this test stops
        # testing anything.
        assert rows and isinstance(rows[0][0], int), rows

        result = audit_graph_against_index(str(index_path))
        assert result["divergence"] == 0, result["missing_from_graph_sample"]
        mcp_server._reset_db_state()

    def test_a_keyword_entity_with_no_ident_fact_does_not_diverge(
        self, tmp_path, monkeypatch
    ):
        import mcp_server

        graph_path, index_path = _write_graph(
            tmp_path, monkeypatch,
            ['[[:ingestion/frontier-low :description "low watermark"]]'],
        )
        _bind(graph_path)
        idents = mcp_server.handle_minigraf_query(
            "[:find ?i :where [?e :ident ?i]]"
        )["results"]
        # The precondition: only memory-prefixed entities get a
        # self-referencing :ident (_ensure_memory_idents), and :ingestion/ is
        # not one, so nothing in the graph names this entity. Mapping graph
        # UUIDs BACK to idents would leave it unmatched; entity_uuid() maps
        # the index's ident forward instead.
        assert idents == [], idents
        assert entity_uuid(":ingestion/frontier-low") != ":ingestion/frontier-low"

        result = audit_graph_against_index(str(index_path))
        assert result["divergence"] == 0, result["missing_from_graph_sample"]
        mcp_server._reset_db_state()


class TestBooleanFactsAreAudited:
    """#303 closed the hole this class used to document.

    `_FACTS_TRIPLE_PATTERN` had no `true`/`false` alternative, so a
    boolean-valued triple reached the graph and never the index -- 83 of them
    on the 822-commit at-scale graph, all `:static`. The audit counted them as
    `unindexed_boolean_facts` and kept them OUT of `divergence`, because the
    alternative was a permanently red gate.

    That exclusion is now DELETED rather than zeroed, and these tests hold the
    line at deleted: a key that always reports 0 reads like a covered case
    while covering nothing, and the exclusion was a blind spot -- a genuinely
    lost `:static` fact was indistinguishable from one the index could never
    hold. The last test here is the one that could not exist before.
    """

    @pytest.fixture
    def boolean_graph(self, tmp_path, monkeypatch):
        graph_path, index_path = _write_graph(
            tmp_path, monkeypatch,
            ['[[:decision/d0 :description "has a boolean sibling"]]'],
        )
        # Transacted through _transact directly rather than the public
        # handler: the point is what the index deriver does with a bare
        # boolean, and going through the same path production ingestion uses
        # is what makes that real rather than staged.
        import mcp_server

        _bind(graph_path)
        with mcp_server.db_lease() as db:
            mcp_server._transact(
                db, "[[:function/f :static true]]",
                valid_from=mcp_server._now_utc_ms(),
            )
            db.checkpoint()
        yield graph_path, index_path
        mcp_server._reset_db_state()

    def test_the_boolean_fact_reaches_the_index(self, boolean_graph):
        """The precondition, inverted. This assertion is what used to say
        `== 0`."""
        _, index_path = boolean_graph
        con = sqlite3.connect(index_path)
        try:
            rows = con.execute(
                "SELECT value FROM facts_dedup WHERE attribute = ':static'"
            ).fetchall()
        finally:
            con.close()
        assert rows == [("true",)]

    def test_the_two_witnesses_agree_without_an_exclusion(self, boolean_graph):
        """minigraf returns Python `True`, the index holds the EDN text
        `'true'`. The audit normalizes the graph side to the index's spelling,
        so the fact simply matches -- no exclusion, no divergence."""
        graph_path, index_path = boolean_graph
        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["missing_from_index"] == 0
        assert result["missing_from_graph"] == 0
        assert result["divergence"] == 0

    def test_the_exclusion_key_is_gone_not_merely_zero(self, boolean_graph):
        """A key reporting 0 forever would read as a covered case. #303 says
        delete it, not widen it."""
        graph_path, index_path = boolean_graph
        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert "unindexed_boolean_facts" not in result

    def test_a_lost_boolean_fact_is_now_reported_as_divergence(
        self, boolean_graph
    ):
        """The payoff, and the test that could not be written before: while
        the exclusion stood, a `:static` fact the graph lost and a `:static`
        fact the index could never hold produced the same number."""
        graph_path, index_path = boolean_graph
        con = sqlite3.connect(index_path)
        try:
            con.execute("DELETE FROM facts_dedup WHERE attribute = ':static'")
            con.commit()
        finally:
            con.close()

        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["missing_from_index"] == 1
        assert result["divergence"] == 1

    def test_a_string_valued_true_keeps_its_capitalization(
        self, tmp_path, monkeypatch
    ):
        """The normalization lowercases by Python TYPE, not by text. A fact
        whose value is the string "True" is stored and returned as "True" on
        both sides; a blanket `.lower()` would make the graph say 'true' while
        the index says 'True' and report a divergence that is not there."""
        import mcp_server

        graph_path, index_path = _write_graph(
            tmp_path, monkeypatch, ['[[:decision/d0 :description "True"]]']
        )
        con = sqlite3.connect(index_path)
        try:
            stored = con.execute(
                "SELECT value FROM facts_dedup WHERE attribute = ':description'"
            ).fetchall()
        finally:
            con.close()
        assert stored == [("True",)]

        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["divergence"] == 0
        mcp_server._reset_db_state()

    def test_losing_a_string_valued_true_still_counts(
        self, tmp_path, monkeypatch
    ):
        """The other half of the same guard: "True" the string must stay
        auditable, not get swept up by anything that recognizes booleans."""
        import mcp_server

        graph_path, index_path = _write_graph(
            tmp_path, monkeypatch, ['[[:decision/d0 :description "True"]]']
        )
        con = sqlite3.connect(index_path)
        try:
            con.execute("DELETE FROM facts_dedup WHERE value = 'True'")
            con.commit()
        finally:
            con.close()

        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["missing_from_index"] == 1
        assert result["divergence"] == 1
        mcp_server._reset_db_state()


class TestGarbledPage:
    """#302's actual failure: a graph that quietly stops producing facts."""

    def test_a_garbled_fact_page_is_detected(self, graph_pair, tmp_path):
        graph_path, index_path = graph_pair
        graph, index = _garble(graph_path, index_path, tmp_path / "corrupt", page=1)
        _bind(graph)
        result = audit_graph_against_index(str(index))
        assert result["missing_from_graph"] > 0, result
        assert result["divergence"] > 0

    def test_the_graph_really_lost_facts_the_index_still_holds(
        self, graph_pair, tmp_path
    ):
        """Not the same assertion as above: this one pins the DIRECTION, so a
        detector that fired on some unrelated asymmetry would not pass."""
        graph_path, index_path = graph_pair
        _bind(graph_path)
        before = audit_graph_against_index(str(index_path))

        graph, index = _garble(graph_path, index_path, tmp_path / "corrupt", page=1)
        _bind(graph)
        after = audit_graph_against_index(str(index))

        assert after["graph_facts"] < before["graph_facts"]
        assert after["index_current_rows"] == before["index_current_rows"]

    def test_the_corruption_prints_nothing_on_stderr(self, graph_pair, tmp_path):
        """The reason this module exists, asserted directly: the same graph
        that the audit catches produces no #251 signature, so the tier's only
        other detector reads it as a clean run."""
        graph_path, index_path = graph_pair
        graph, index = _garble(graph_path, index_path, tmp_path / "corrupt", page=1)
        _bind(graph)
        with tee_stderr() as captured:
            result = audit_graph_against_index(str(index))
        scanned = scan_ingestion_stderr(captured.text())

        assert result["divergence"] > 0
        assert scanned["error_signals"] == [], scanned["error_signals"]


class TestTheOtherDirection:
    def test_a_fact_the_index_never_witnessed_is_reported(self, graph_pair):
        """missing_from_index. A garbled page can INVENT facts (measured: one
        target added 33), and an index write can fail silently -- _index_write
        swallows its own exceptions by design."""
        graph_path, index_path = graph_pair
        con = sqlite3.connect(index_path)
        try:
            rowid = con.execute(
                "SELECT rowid FROM facts_dedup WHERE valid_to = '' LIMIT 1"
            ).fetchone()[0]
            con.execute("DELETE FROM facts_dedup WHERE rowid = ?", (rowid,))
            con.commit()
        finally:
            con.close()

        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["missing_from_index"] == 1, result["missing_from_index_sample"]
        assert result["missing_from_graph"] == 0


class TestFailuresAreRecordedNotRaised:
    def test_a_scan_that_raises_is_recorded(self, graph_pair):
        graph_path, index_path = graph_pair
        _bind(graph_path)

        def boom(_query):
            raise RuntimeError("Page 7 out of bounds (total pages: 3)")

        result = audit_graph_against_index(str(index_path), query_fn=boom)
        assert result["audit_error"] is not None
        assert "Page 7 out of bounds" in result["audit_error"]

    def test_a_scan_that_returns_not_ok_is_recorded(self, graph_pair):
        graph_path, index_path = graph_pair
        _bind(graph_path)
        result = audit_graph_against_index(
            str(index_path), query_fn=lambda _q: {"ok": False, "error": "nope"}
        )
        assert result["audit_error"] is not None

    def test_auditing_the_wrong_graphs_index_is_refused_not_reported_as_loss(
        self, graph_pair, tmp_path
    ):
        """The graph side comes from whatever mcp_server is bound to, which no
        argument names. Two unrelated graphs share almost no facts, so getting
        this wrong would report near-total divergence -- a false red that
        looks exactly like the catastrophe this gate exists to catch."""
        graph_path, index_path = graph_pair
        _bind(graph_path)
        result = audit_graph_against_index(
            str(index_path), expected_graph_path=str(tmp_path / "other.graph")
        )
        assert result["audit_error"] is not None
        assert "refusing to audit" in result["audit_error"]
        assert result["divergence"] == 0

    def test_the_expected_path_matching_the_binding_audits_normally(self, graph_pair):
        graph_path, index_path = graph_pair
        _bind(graph_path)
        result = audit_graph_against_index(
            str(index_path), expected_graph_path=str(graph_path)
        )
        assert result["audit_error"] is None
        assert result["divergence"] == 0

    def test_a_missing_index_is_recorded(self, graph_pair, tmp_path):
        graph_path, _ = graph_pair
        _bind(graph_path)
        result = audit_graph_against_index(str(tmp_path / "does-not-exist.sqlite3"))
        assert result["audit_error"] is not None


class TestIntroducedByDuplicates:
    """#287: the two-value :introduced-by corruption from #235.

    Read off the same full-graph scan the fact-index audit already runs, and
    reported under its OWN key rather than folded into `divergence` -- see
    evals/at_scale/introduced_by_audit.py for why the two are not the same
    kind of finding.
    """

    @pytest.fixture
    def duplicate_graph(self, tmp_path, monkeypatch):
        """One entity carrying two live :introduced-by values.

        The two facts are transacted in SEPARATE calls, and that is not
        stylistic: project-minigraf/minigraf#287 is still open, so batching
        two facts that share (entity, attribute, valid_from) into one transact
        silently keeps only the last -- the fixture would build a healthy
        graph and the test would pass for the wrong reason.
        """
        graph_path, index_path = _write_graph(
            tmp_path, monkeypatch,
            [
                '[[:function/f :ident ":function/f"]]',
                "[[:function/f :introduced-by :commit/aaa]]",
                "[[:function/f :introduced-by :commit/bbb]]",
            ],
        )
        yield graph_path, index_path
        import mcp_server

        mcp_server._reset_db_state()

    def test_a_clean_graph_reports_zero_rather_than_nothing(self, graph_pair):
        """Present and zero, not absent. A consumer must be able to tell "no
        affected entities" from "this metrics file predates the check"."""
        graph_path, index_path = graph_pair
        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["introduced_by_duplicates"] == {"entities": 0, "sample": []}

    def test_the_graph_really_holds_both_values(self, duplicate_graph):
        """The precondition. Without it a passing detection test proves only
        that minigraf kept one value and the audit agreed."""
        import mcp_server

        graph_path, _ = duplicate_graph
        _bind(graph_path)
        rows = mcp_server.handle_minigraf_query(
            "[:find ?c :where [:function/f :introduced-by ?c]]"
        )["results"]
        assert sorted(r[0] for r in rows) == [":commit/aaa", ":commit/bbb"], rows

    def test_the_duplicate_is_detected_and_named(self, duplicate_graph):
        graph_path, index_path = duplicate_graph
        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["introduced_by_duplicates"]["entities"] == 1
        assert result["introduced_by_duplicates"]["sample"] == [
            [":function/f", [":commit/aaa", ":commit/bbb"]]
        ]

    def test_the_corrupt_graph_still_diverges_by_zero(self, duplicate_graph):
        """Why this is not part of `divergence`: the index faithfully holds
        BOTH values, so the two witnesses agree perfectly about a graph that
        is wrong. A single number covering both findings would be zero here
        and the corruption would be invisible."""
        graph_path, index_path = duplicate_graph
        _bind(graph_path)
        result = audit_graph_against_index(str(index_path))
        assert result["divergence"] == 0
        assert result["audit_error"] is None
        assert result["introduced_by_duplicates"]["entities"] == 1

    def test_a_scan_that_failed_reports_none_not_zero(self, graph_pair):
        """A failed scan yields an empty fact set, and an empty fact set has
        no duplicates. Reporting 0 there would say "clean" about a graph
        nobody managed to read."""
        graph_path, index_path = graph_pair
        _bind(graph_path)

        def boom(_query):
            raise RuntimeError("Page 7 out of bounds (total pages: 3)")

        result = audit_graph_against_index(str(index_path), query_fn=boom)
        assert result["introduced_by_duplicates"] is None

    def test_the_wrong_graphs_index_reports_none_not_zero(self, graph_pair, tmp_path):
        """Same reasoning on the refusal path, which returns before any scan
        happens at all."""
        graph_path, index_path = graph_pair
        _bind(graph_path)
        result = audit_graph_against_index(
            str(index_path), expected_graph_path=str(tmp_path / "other.graph")
        )
        assert result["audit_error"] is not None
        assert result["introduced_by_duplicates"] is None
