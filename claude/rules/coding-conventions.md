# Coding Conventions

## Python

Stack: `uv` (packages, Python versions, CLI tools), `ruff` (lint + format), `ty` (types, beta), `just` (tasks), `cyclopts` (CLIs), `pydantic-settings` (config/env), `pydantic` (validation), `pytest`, `httpx`, `anyio`.

Invoke via `uv run` (`--no-sync` when deps are unchanged); read `.eval` logs with Inspect AI's `read_eval_log()`. **Never call `sys.path.insert` at import time — it crashes the session.** Pass data as pydantic `BaseModel`/`dataclass`, not `pd.DataFrame`; JSONL for intermediates, pandas only at the pipeline edge. Copy shared configs, don't mutate them. Rewrite shell in Python past ~50 lines.

## Any Language

**Parallelize embarrassingly parallel loops by default** — background N independent iterations and wait (`asyncio.gather`, `Promise.all`, `cmd & … wait`). Sequential only for real ordering dependencies, shared mutable state, or OS-level exclusivity.

`shellcheck` before committing, `# shellcheck shell=bash` atop zsh scripts. UTC/ISO-8601 timestamps: `$(utc_date)`, `$(utc_timestamp)`. TypeScript over JS; bun over npm; Biome over ESLint+Prettier.

Available: `rg` `fd` `fzf` `bat` `eza` `z` `delta` `jq` `jless` `dust` `duf` `sd` `trash` `gws`; `any2md <input>` → Markdown. Visual output: `docs/visual-layout-quality.md`.
