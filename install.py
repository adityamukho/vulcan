#!/usr/bin/env python3
"""
Installation script for temporal-reasoning skill.
Checks dependencies and provides next steps.
"""

import sys
import subprocess
import os


def check_python_version():
    """Check Python version is 3.8+."""
    if sys.version_info < (3, 8):
        print(f"ERROR: Python 3.8+ required, found {sys.version_info.major}.{sys.version_info.minor}")
        return False
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True


def check_minigraf():
    """Check if minigraf CLI is installed."""
    try:
        result = subprocess.run(
            ["minigraf", "--version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            print(f"✓ minigraf CLI: {version}")
            return True
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass

    print("✗ minigraf CLI not found")
    print()
    print("To install minigraf (>= 0.13.0):")
    print("  cargo install --git https://github.com/adityamukho/minigraf")
    print()
    print("Or use an older version:")
    print("  cargo install minigraf")
    return False


def check_tool_import():
    """Verify minigraf_tool can be imported."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("minigraf_tool")
        if spec is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, script_dir)
        import minigraf_tool
        print("✓ minigraf_tool module can be imported")
        return True
    except ImportError as e:
        print(f"✗ Cannot import minigraf_tool: {e}")
        return False


def main():
    print("=" * 50)
    print("Temporal-Reasoning Skill Setup")
    print("=" * 50)
    print()

    checks = [
        ("Python version", check_python_version),
        ("minigraf CLI", check_minigraf),
        ("Module import", check_tool_import),
    ]

    results = []
    for name, check_func in checks:
        print(f"Checking {name}...")
        results.append(check_func())
        print()

    if all(results):
        print("=" * 50)
        print("✓ Setup complete!")
        print("=" * 50)
        print()
        print("Usage:")
        print("  # As Python module:")
        print("  python -c \"from minigraf_tool import query, transact; print(query('[:find ?e :where [?e :test/name]]'))\"")
        print()
        print("  # As CLI:")
        print("  python minigraf_tool.py query '[:find ?e :where [?e :test/name]]'")
        print("  python minigraf_tool.py transact '[[:test :person/name \\\"Alice\\\"]]'")
        print()
        print("  # Import and use in code:")
        print("  from minigraf_tool import query, transact")
        print("  transact('[[:decision :arch/cache-strategy \\\"Redis\\\"]]', reason='fast in-memory caching')")
        print("  result = query('[:find ?s :where [_ :arch/cache-strategy ?s]]')")
    else:
        print("=" * 50)
        print("✗ Setup incomplete - fix errors above")
        print("=" * 50)
        sys.exit(1)


if __name__ == "__main__":
    main()
