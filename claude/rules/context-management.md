# Context Management

- **PDFs: always a subagent.** One PDF read in main context can consume the entire window.
- **Files >500 lines: never Read without `offset`/`limit`** — delegate to a subagent if you need the whole file. Threshold configurable via `CLAUDE_READ_THRESHOLD` (default 500).
- **200-500 lines:** Grep first, then targeted Read.
- **Verbose or long-running commands** (builds, `pytest -v`, experiments): tmux-cli if >5 min (survives disconnects), else `run_in_background`. Never synchronously in main context.
