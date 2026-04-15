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

## Why Not Just Read Git History?

An agent with git access can already answer simple temporal questions: `git show <commit>:ROADMAP.md`, `git diff v1..v2`, `git blame`. For small projects with short histories, that is often enough.

This project adds value where git structurally cannot help:

**Cross-cutting semantic queries.** Git is organized by commit — time slices of the whole repo. To answer "when did module A first depend on module B?", an agent must check out every commit, parse the code at each one, and scan. That is O(commits × parse time) and blows the context window on any real codebase. The graph inverts the index — facts are stored by entity, so you query forward from the entity directly.

**Semantic structure survives text changes.** Git sees line diffs. It does not know a function moved from `auth.py` to `middleware/auth.py` — it sees a deletion and an addition. `git blame` breaks on renames. The graph stores `:calls` and `:depends-on` edges that are entity-addressed, not file-addressed. Refactors do not break the history.

**Agent-authored facts do not exist in git.** Decisions, constraints, and observations that an agent logged but never committed to a file have no representation in git. The graph is the only place where these coexist with code structure as queryable facts.

**Cross-layer joins.** There is no way to ask git: "list all dependency changes that happened after the decision to switch databases." The decision lives in agent memory; the structural change lives in git; they are in separate systems with no shared query surface. In the graph, both are datoms and a single Datalog join connects them.

The graph is built *from* git but answers queries git cannot express. Value scales with history length, structural complexity, and frequency of cross-cutting or cross-layer queries.

## Future Phase 4+ — Code Structure Evolution

Extend the bi-temporal graph to store code structure extracted from git history, enabling temporal queries over how a codebase evolved and why.

### Ingestion

- Walk git log and replay commits in order, extracting AST-level structure at each commit (functions, classes, modules, call edges, dependency edges) using tree-sitter or equivalent
- Transact each commit as a minigraf transaction: new edges added, removed edges retracted, with the commit hash, author, and message stored as the reason
- Support incremental re-ingestion (only process commits since last-known transaction) for use in CI or post-commit hooks
- Map git commit timestamps to minigraf valid-time so wall-clock `as-of` queries work alongside transaction-number queries

### Queries this unlocks

- Point-in-time structure: what did the call graph / dependency graph look like at commit X or date Y?
- Delta queries: which edges appeared or disappeared between two commits?
- Coupled evolution: which modules changed together most frequently (implicit coupling)?
- Decision correlation: which structural changes happened after a given agent decision was logged?
- Regression tracing: when did a specific dependency or coupling first appear?

### Reasoning layer

- Agent-facing query patterns (fewshots / skill prompts) for common insights: circular dependency detection, high-churn modules, blast radius of a proposed change
- Cross-layer queries that join code structure edges with agent decision datoms in the same graph — e.g., "show dependency changes that occurred after the database migration decision"
- Natural-language question templates mapped to Datalog patterns so agents can ask structural questions without writing raw Datalog

## Marketplace Publishing ✓

Published as a GitHub-hosted Claude Code plugin. Users add the repo to `extraKnownMarketplaces` in `settings.json` — see README for instructions.

Pre-built binary support landed in minigraf v0.19.0 (2026-04-14), removing the `cargo`/Rust installation barrier. `install.py` now downloads the correct binary automatically for Linux x86_64, Linux aarch64, macOS arm64, macOS x86_64, and Windows. Skill description reframed to lead with user benefit.
