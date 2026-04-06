# Temporal Reasoning - Roadmap

## Phase 1 (Complete ✓)
- Python CLI wrapper (`minigraf_tool.py`)
- Tool schemas (query.json, transact.json)
- Operational prompts (system.txt, fewshots.txt)
- Test harness

## Phase 2 (Complete ✓)

| Priority | Item | Description | Effort | Status |
|----------|------|-------------|--------|--------|
| 1 | HTTP server | Axum HTTP wrapper for better agent performance | Medium | Complete ✓ |
| 2 | Write policy enforcer | Validate reason required before transact | Low | Complete ✓ |
| 3 | report_issue tool | Auto-file GitHub issues on failures | Low | Complete ✓ |
| 4 | install.py | One-command setup script | Low | Complete ✓ |

## Future Phase 3+
- WASM bindings (browser + edge)
- Mobile embedding
- Claude Code MCP integration
- Codex/OpenAI adapters
