"""Guards on SKILL.md's example code.

SKILL.md is the agent-facing contract: an agent reads it top-to-bottom and
copies the first call shape it sees. Nothing executes those examples, so they
rot silently, and by 2026-09-02 nearly every one of them was dead (#322):

  * `from minigraf import query, transact` opened the "Tools" section and
    three "Examples" blocks. That import has never worked -- the installed
    `minigraf` package exports `MiniGrafDb`, `MiniGrafError` and
    `minigraf_ffi`, and there is no `minigraf.py` in this repo.
  * A "Or via CLI" block invoked `python minigraf.py transact ...`, the same
    file that does not exist.
  * The bare `query(...)` / `transact(...)` / `retract(...)` calls that
    followed each import were unreachable for the same reason.
  * A closing note at the very bottom of the file said all of this -- 900
    lines after the first example that contradicted it.

The real interface is the MCP tools, which take NAMED arguments. So the guard
is not a spell-check on a banned word list alone: every call in a `python`
fence is matched against the live `mcp_server._TOOLS` schema, which is what
makes `minigraf_query("...")` (positional, no such thing over MCP) a failure
as much as `query("...")` is.
"""

import ast
import os
import re
import textwrap

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_MD = os.path.join(REPO_ROOT, "SKILL.md")

# Names that only ever existed in the dead examples. `query`/`transact`/
# `retract`/`rule` are the bare forms; the live tools carry a prefix.
_DEAD_CALLEES = {"query", "transact", "retract", "rule", "report_issue"}


def _skill_text() -> str:
    with open(SKILL_MD, encoding="utf-8") as f:
        return f.read()


def _python_blocks(text: str) -> list:
    """Every ```python fence, dedented, with its 1-based starting line."""
    blocks = []
    for m in re.finditer(r"^[ \t]*```python\n(.*?)^[ \t]*```", text, re.S | re.M):
        line_no = text.count("\n", 0, m.start()) + 1
        blocks.append((line_no, textwrap.dedent(m.group(1))))
    return blocks


def _calls(text: str):
    """(line_no, ast.Call) for every top-level-name call in every python fence."""
    for line_no, src in _python_blocks(text):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            # `test_every_python_fence_parses` owns that failure; reporting it
            # from five call-shape tests as well buries the one useful message.
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                yield line_no + node.lineno - 1, node


def _tools() -> dict:
    import mcp_server
    return {t.name: t.inputSchema for t in mcp_server._TOOLS}


class TestNoDeadInterface:
    """The forms that never worked, in the text and in the examples."""

    def test_no_minigraf_import_outside_the_note_that_disowns_it(self):
        """`from minigraf import ...` may appear only as prose saying it is dead."""
        offenders = [
            (i, line)
            for i, line in enumerate(_skill_text().split("\n"), 1)
            if "from minigraf import" in line and "`" not in line
        ]
        assert not offenders, (
            "SKILL.md shows `from minigraf import ...` as runnable code; the "
            f"installed package has no such API: {offenders}"
        )

    def test_no_minigraf_py_invocation(self):
        """There is no `minigraf.py`, so nothing may be run from one."""
        offenders = [
            (line_no, src.strip())
            for line_no, src in _python_blocks(_skill_text())
            if "minigraf.py" in src
        ]
        offenders += [
            (i, line)
            for i, line in enumerate(_skill_text().split("\n"), 1)
            if re.search(r"python\s+minigraf\.py", line)
        ]
        assert not offenders, f"SKILL.md invokes a nonexistent minigraf.py: {offenders}"

    def test_no_bare_calls_to_the_dead_names(self):
        offenders = [
            (line_no, node.func.id)
            for line_no, node in _calls(_skill_text())
            if node.func.id in _DEAD_CALLEES
        ]
        assert not offenders, (
            "SKILL.md example calls a bare name that no interface exports; use the "
            f"`minigraf_`/`memory_` MCP tool instead: {offenders}"
        )


class TestExamplesMatchLiveToolSchemas:
    """Every example call is a real tool, called the way MCP calls it."""

    def test_every_tool_call_names_a_live_tool(self):
        tools = _tools()
        offenders = [
            (line_no, node.func.id)
            for line_no, node in _calls(_skill_text())
            if (node.func.id.startswith("minigraf_") or node.func.id.startswith("memory_"))
            and node.func.id not in tools
        ]
        assert not offenders, (
            f"SKILL.md calls a tool that is not in mcp_server._TOOLS: {offenders}"
        )

    def test_every_tool_call_uses_named_arguments_only(self):
        """MCP passes an arguments OBJECT -- there is no positional form."""
        tools = _tools()
        offenders = [
            (line_no, node.func.id)
            for line_no, node in _calls(_skill_text())
            if node.func.id in tools and node.args
        ]
        assert not offenders, (
            "SKILL.md shows a positional argument to an MCP tool; every argument "
            f"crosses the wire by name: {offenders}"
        )

    def test_every_named_argument_is_declared_by_the_tool(self):
        tools = _tools()
        offenders = []
        for line_no, node in _calls(_skill_text()):
            schema = tools.get(node.func.id)
            if schema is None:
                continue
            declared = set(schema.get("properties", {}))
            for kw in node.keywords:
                if kw.arg not in declared:
                    offenders.append((line_no, node.func.id, kw.arg, sorted(declared)))
        assert not offenders, (
            f"SKILL.md passes an argument the tool does not declare: {offenders}"
        )

    def test_every_required_argument_is_shown(self):
        """An example that omits a required argument teaches a rejected call."""
        tools = _tools()
        offenders = []
        for line_no, node in _calls(_skill_text()):
            schema = tools.get(node.func.id)
            if schema is None:
                continue
            missing = set(schema.get("required", [])) - {kw.arg for kw in node.keywords}
            if missing:
                offenders.append((line_no, node.func.id, sorted(missing)))
        assert not offenders, (
            f"SKILL.md example omits a required tool argument: {offenders}"
        )


class TestSkillDocIsWellFormed:
    """The rot that hid behind the dead examples."""

    def test_code_fences_are_balanced(self):
        """An unterminated fence swallows the sections after it.

        The `linked` example's fence was open from its own block through the
        whole next section, so `### Find all entities of a given type` and its
        example rendered as code rather than as a heading.
        """
        open_at = None
        for i, line in enumerate(_skill_text().split("\n"), 1):
            if re.match(r"^[ \t]*```", line):
                open_at = None if open_at else (i, line.strip())
        assert open_at is None, f"unterminated code fence opened at line {open_at}"

    @pytest.mark.parametrize("block", _python_blocks(_skill_text()))
    def test_every_python_fence_parses(self, block):
        line_no, src = block
        try:
            ast.parse(src)
        except SyntaxError as exc:
            pytest.fail(f"```python fence at line {line_no} does not parse: {exc}")
