# Temporal Reasoning - Roadmap

## Phase 1 (Complete ✓)
- Python CLI wrapper (`minigraf_tool.py`)
- Tool schemas (query.json, transact.json)
- Operational prompts (system.txt, fewshots.txt)
- Test harness

## Phase 2 (Complete ✓)

| Priority | Item | Description | Effort | Status |
|----------|------|-------------|--------|--------|
| 1 | Write policy enforcer | Validate reason required before transact | Low | Complete ✓ |
| 3 | report_issue tool | Auto-file GitHub issues on failures | Low | Complete ✓ |
| 4 | install.py | One-command setup script | Low | Complete ✓ |

## Future Phase 3+
- WASM bindings (browser + edge)
- Mobile embedding
- Claude Code MCP integration
- Codex/OpenAI adapters

## Marketplace Publishing

The skill is functionally complete and benchmarked. The blocking dependency for marketplace publication is **minigraf pre-built binaries**: `cargo install minigraf` requires a Rust toolchain, which is too high a barrier for general users. Will be published once minigraf ships binaries for common platforms (Linux x86_64, macOS arm64/x86_64, Windows). At that point, will also reframe the skill description to lead with the user benefit (no lost context between sessions) rather than the mechanism.
