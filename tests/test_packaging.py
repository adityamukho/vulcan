"""Guards on what the built distribution actually contains.

`[tool.setuptools] py-modules` is a hand-maintained list, and the repo is a
flat module layout with no package directory, so setuptools cannot discover a
new top-level module on its own. Adding `foo.py` next to `mcp_server.py` and
importing it works in every developer checkout and every CI run -- the repo
root is on `sys.path` -- and ships a distribution that raises
`ModuleNotFoundError` on import.

That is not hypothetical. `frontier_registry.py` landed 2026-07-24 (c3763ec)
and was imported from `mcp_server.py:38` without being added to `py-modules`;
1599 tests stayed green, and a wheel built from that tree died at
`import mcp_server`. It was caught only because #82 tried to document the
`uvx temporal-reasoning` install path and someone ran the command.
"""

import ast
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _declared_py_modules() -> list:
    """Read `[tool.setuptools] py-modules` out of pyproject.toml.

    Parsed by regex rather than tomllib: CI runs this suite on 3.10, where
    tomllib does not exist, and a third-party toml dependency is not worth
    one list of strings.
    """
    with open(os.path.join(REPO_ROOT, "pyproject.toml")) as f:
        text = f.read()
    match = re.search(r"^py-modules\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert match, "pyproject.toml has no [tool.setuptools] py-modules list"
    return re.findall(r'"([^"]+)"', match.group(1))


def _repo_root_modules() -> set:
    """Top-level module names importable from the repo root."""
    return {
        name[:-3]
        for name in os.listdir(REPO_ROOT)
        if name.endswith(".py") and not name.startswith("_")
    }


def _local_imports(module_name: str, local_modules: set) -> set:
    """Names `module_name` imports that resolve to a repo-root module.

    Walks the whole AST, not just module level: several imports in
    mcp_server.py are deferred inside functions, and a deferred import of an
    unshipped module fails just as hard, only later.
    """
    path = os.path.join(REPO_ROOT, f"{module_name}.py")
    if not os.path.exists(path):
        # Declared but absent. test_declared_modules_exist_on_disk owns that
        # diagnosis; raising FileNotFoundError here would report it twice, in
        # the less useful of the two forms.
        return set()
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in local_modules:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            # `level > 0` is a relative import, impossible in a flat layout.
            if node.level == 0 and node.module:
                root = node.module.split(".")[0]
                if root in local_modules:
                    found.add(root)
    return found


class TestPyModulesCoverage:
    def test_declared_modules_exist_on_disk(self):
        local_modules = _repo_root_modules()
        for name in _declared_py_modules():
            assert name in local_modules, (
                f"pyproject.toml ships '{name}', but {name}.py is not at the repo root"
            )

    def test_shipped_modules_import_only_shipped_modules(self):
        """Every repo-root module a shipped module imports is itself shipped.

        Transitive: a shipped module's local import is added to the frontier
        and checked in turn, so a two-hop miss is caught as well as a direct
        one.
        """
        declared = set(_declared_py_modules())
        local_modules = _repo_root_modules()

        missing = {}
        seen = set()
        frontier = list(declared)
        while frontier:
            name = frontier.pop()
            if name in seen:
                continue
            seen.add(name)
            for dep in _local_imports(name, local_modules):
                if dep not in declared:
                    missing.setdefault(dep, set()).add(name)
                elif dep not in seen:
                    frontier.append(dep)

        assert not missing, (
            "pyproject.toml [tool.setuptools] py-modules is missing modules that "
            "shipped code imports -- the built wheel will raise ModuleNotFoundError:\n"
            + "\n".join(
                f"  {dep}.py -- imported by {', '.join(sorted(importers))}"
                for dep, importers in sorted(missing.items())
            )
        )
