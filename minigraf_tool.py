#!/usr/bin/env python3
"""
Minigraf CLI wrapper for AI coding agents.

Provides query and transact functions for persistent bi-temporal graph memory.
Requires minigraf CLI (>= 0.13.0) to be on PATH.
"""

import subprocess
import json
import os
import tempfile
from typing import Optional, Dict, Any, List, Union

MINIGRAF_BIN = "minigraf"

DEFAULT_GRAPH_PATH = os.path.join(tempfile.gettempdir(), "minigraf_memory.graph")


class MinigrafError(Exception):
    """Error from minigraf operations."""
    pass


def _run_minigraf(args: List[str], input_data: Optional[str] = None) -> Dict[str, Any]:
    """Run minigraf CLI and return parsed result."""
    try:
        result = subprocess.run(
            [MINIGRAF_BIN] + args,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
            return {"ok": False, "error": error_msg or "Unknown error"}
        
        return {"ok": True, "output": result.stdout.strip()}
    except FileNotFoundError:
        return {"ok": False, "error": f"minigraf not found. Is it installed and on PATH?"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "minigraf command timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def query(datalog: str, as_of: Optional[Union[int, str]] = None, graph_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Query the graph memory with a Datalog query.
    
    Args:
        datalog: A valid Datalog query string
        as_of: Optional transaction count to query as of (temporal query)
        graph_path: Optional path to .graph file. Uses default temp location if not provided.
    
    Returns:
        Dict with 'ok', 'results' (list of results), and optional 'error'
    """
    path = graph_path or DEFAULT_GRAPH_PATH
    
    if not os.path.exists(path):
        return {"ok": False, "error": f"No graph file at {path}. Transact first."}
    
    # Handle temporal query
    if as_of is not None:
        as_of_clause = f":as-of {as_of}"
        if ":as-of" not in datalog:
            if ":find" in datalog:
                # Insert :as-of after :find clause
                datalog = datalog.replace(":find", ":find", 1)  # Find first :find
                # Find position after :find and insert
                find_idx = datalog.find(":find")
                after_find = datalog[find_idx + 5:]
                # Find first space or [ after :find
                next_space = len(after_find)
                for char in [' ', '[']:
                    idx = after_find.find(char)
                    if idx != -1 and idx < next_space:
                        next_space = idx
                datalog = datalog[:find_idx + 5 + next_space] + f" {as_of_clause} " + datalog[find_idx + 5 + next_space:]
            else:
                datalog = f"[{as_of_clause} {datalog}]"
    
    full_query = f"(query {datalog})"
    result = _run_minigraf(["--file", path], input_data=full_query)
    
    if not result.get("ok"):
        return result
    
    output = result["output"]
    
    if "No results found" in output:
        return {"ok": True, "results": []}
    
    lines = output.split("\n")
    if len(lines) < 3:
        return {"ok": True, "results": []}
    
    result_header = lines[0]
    separator = lines[1]
    
    col_count = result_header.count("?") + result_header.count(":")
    results = []
    
    for line in lines[2:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("---"):
            continue
        if stripped.startswith("No results") or stripped.endswith("found."):
            continue
        
        values = [v.strip() for v in line.split("|")]
        if len(values) >= col_count:
            results.append(values[:col_count])
    
    return {"ok": True, "results": results}


def transact(facts: str, reason: Optional[str] = None, graph_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Store facts in the graph memory.
    
    Args:
        facts: Datalog transact string with facts to store
        reason: Why this fact deserves long-term storage (for future validation)
        graph_path: Optional path to .graph file. Uses default temp location if not provided.
    
    Returns:
        Dict with 'ok', 'tx' (transaction count), and optional 'error'
    """
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason is required for all writes"}
    
    path = graph_path or DEFAULT_GRAPH_PATH
    
    full_tx = f"(transact {facts})"
    result = _run_minigraf(["--file", path], input_data=full_tx)
    
    if not result.get("ok"):
        return result
    
    output = result["output"]
    
    if "Transacted successfully" in output:
        tx_match = output.split("tx:")[1].strip().rstrip(")") if "tx:" in output else "unknown"
        return {"ok": True, "tx": tx_match, "reason": reason}
    
    return {"ok": True, "output": output}


def temporal_query(datalog: str, as_of: Union[int, str], graph_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Query the graph as of a specific transaction time.
    
    Args:
        datalog: A valid Datalog query string
        as_of: Transaction count to query as of
        graph_path: Optional path to .graph file
    
    Returns:
        Dict with query results
    """
    as_of_clause = f":as-of {as_of}"
    if ":as-of" not in datalog:
        if ":find" in datalog:
            datalog = datalog.replace(":find", f":find {as_of_clause} :find".replace(" :find", ""), 1)
        else:
            datalog = f"[{as_of_clause} {datalog}]"
    
    return query(datalog, graph_path)


def reset(graph_path: Optional[str] = None) -> Dict[str, Any]:
    """Delete the graph file to start fresh."""
    path = graph_path or DEFAULT_GRAPH_PATH
    if os.path.exists(path):
        os.remove(path)
        return {"ok": True, "deleted": path}
    return {"ok": True, "deleted": None, "note": "No file to delete"}


def get_graph_path() -> str:
    """Return the default graph path."""
    return DEFAULT_GRAPH_PATH


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: minigraf_tool.py <command> [args]")
        print("Commands: query, transact, reset, path")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "query":
        if len(sys.argv) < 3:
            print("Usage: minigraf_tool.py query '<datalog>' [--as-of <tx>]")
            sys.exit(1)
        datalog = sys.argv[2]
        as_of = None
        if "--as-of" in sys.argv:
            idx = sys.argv.index("--as-of")
            if idx + 1 < len(sys.argv):
                as_of = sys.argv[idx + 1]
        result = query(datalog, as_of=as_of)
        print(json.dumps(result, indent=2))
    elif cmd == "transact":
        if len(sys.argv) < 3:
            print("Usage: minigraf_tool.py transact '<facts>' [--reason '<reason>']")
            sys.exit(1)
        facts = sys.argv[2]
        reason = None
        if "--reason" in sys.argv:
            idx = sys.argv.index("--reason")
            if idx + 1 < len(sys.argv):
                reason = sys.argv[idx + 1]
        result = transact(facts, reason=reason)
        print(json.dumps(result, indent=2))
    elif cmd == "reset":
        result = reset()
        print(json.dumps(result, indent=2))
    elif cmd == "path":
        print(get_graph_path())
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)