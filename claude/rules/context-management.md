# Context Management

- **Delegate on breadth, not on file size.** A wide sweep — many files, unknown location, several naming conventions — is worth a subagent (`Explore` / `efficient-explorer`): you keep the conclusion, not the file dumps. Reading one file whose path you already know is not, however long it is. (The line-count thresholds this rule used to carry assumed a much smaller window — don't reinstate them.)
- **Read with `offset`/`limit` when you know which part you need**, and Grep first when you are still locating it. That is a speed and signal-to-noise habit, not a context-safety one — don't escalate it into a delegation.
- **PDFs: bound the read or delegate.** Read's `pages` parameter (max 20/request, required past 10 pages) makes a targeted PDF read safe inline. An unbounded read of a large PDF can still consume the whole window — hand those to a subagent.
- **Verbose or long-running commands** (builds, `pytest -v`, experiments): `run_in_background` — it detaches, survives across turns, and re-invokes on exit. Reserve tmux for work that must outlive the session itself. Never run them synchronously in main context.

Delegation limits: `rules/agents-and-delegation.md`.
