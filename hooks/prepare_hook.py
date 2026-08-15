#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook — inject memory context before each turn.

Claude Code calls this script with the user's message on stdin (JSON) and expects
a JSON response with optional additionalContext. The context is prepended to the
agent's working context for this turn.

Usage (hooks/claude-code.json):
  "command": "python PATH_TO_REPO/hooks/prepare_hook.py"
"""
import json
import os
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        data = {}

    prompt = data.get("message", "") or data.get("prompt", "")
    context = ""

    if prompt:
        try:
            import mcp_server
            # No explicit open: handle_memory_prepare_turn takes its own lease,
            # which carries the same retry/backoff and stale-lock self-heal
            # get_db() used to provide, and releases when it returns so the
            # next turn's hook process can acquire the file lock (#255).
            context = mcp_server.handle_memory_prepare_turn(prompt)
        except Exception:
            pass  # Never block the turn on memory errors

    print(json.dumps({"continue": True, "additionalContext": context}))


if __name__ == "__main__":
    main()
