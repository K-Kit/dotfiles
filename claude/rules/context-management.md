# Context Management

- **PDFs: bound the read or delegate.** Read's `pages` parameter (max 20/request, required past 10 pages) makes a targeted PDF read safe inline. An unbounded read of a large PDF can still consume the whole window — hand those to a subagent.
- **Files >500 lines: never Read without `offset`/`limit`** — delegate to a subagent if you need the whole file. The 500-line bar is our convention, not a harness setting.
- **200-500 lines:** Grep first, then targeted Read.
- **Verbose or long-running commands** (builds, `pytest -v`, experiments): `run_in_background` — it detaches, survives across turns, and re-invokes on exit. Reserve tmux for work that must outlive the session itself. Never run them synchronously in main context.
