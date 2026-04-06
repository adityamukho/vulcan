# Temporal Reasoning - Roadmap

## Phase 1 (Complete ✓)
- Python CLI wrapper (`minigraf_tool.py`)
- Tool schemas (query.json, transact.json)
- Operational prompts (system.txt, fewshots.txt)
- Test harness

## Phase 2 (In Progress)

| Priority | Item | Description | Effort | Status |
|----------|------|-------------|--------|--------|
| 1 | HTTP server | Axum HTTP wrapper for better agent performance | Medium | In Progress |
| 2 | Write policy enforcer | Validate reason required before transact | Low | - |
| 3 | report_issue tool | Auto-file GitHub issues on failures | Low | - |
| 4 | install.py | One-command setup script | Low | - |

## Future Phase 3+
- WASM bindings (browser + edge)
- Mobile embedding
- Claude Code MCP integration
- Codex/OpenAI adapters
