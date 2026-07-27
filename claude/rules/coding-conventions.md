# Coding Conventions

## Python

| Need | Tool |
|---|---|
| Packages, Python versions, CLI tools | `uv` |
| Lint + format | `ruff` |
| Type check | `ty` |
| Task runner | `just` |
| CLI | `cyclopts` |
| Config / env | `pydantic-settings` |
| Validation | `pydantic` |
| Testing | `pytest` |
| HTTP | `httpx` |
| Async | `anyio` |

Invoke tools via `uv run` — avoids stale `VIRTUAL_ENV`. `uv run --no-sync` when deps are unchanged. Read `.eval` files with Inspect AI's `read_eval_log()`.

**Never call `sys.path.insert` directly — it crashes the Claude Code session.** Wrap it in a helper invoked only under `if __name__ == "__main__":`.

Pass data as pydantic `BaseModel`/`dataclass`, not `pd.DataFrame`; JSONL for intermediates; pandas only at the pipeline edge for metrics. Copy shared configs/prompts rather than mutating them. Rewrite shell in Python past ~50 lines.

## Shell

`shellcheck` before committing; `# shellcheck shell=bash` at the top of zsh scripts. fzf pickers: `--bind 'space:toggle'` for multi-select; `--bind "load:pos(N)+select"` needs fzf 0.54+.

## Any Language

**Parallelize embarrassingly parallel loops by default** — background N independent iterations and wait, rather than looping (`asyncio.gather`, `Promise.all`, `cmd & … wait`). Stay sequential only for a genuine ordering dependency, shared mutable state, or OS-level exclusivity (keystroke UI automation needs the app frontmost).

UTC and ISO-8601 for all timestamps: `$(utc_date)` → `YYYY-MM-DD`, `$(utc_timestamp)` → `YYYY-MM-DD_HH-MM-SS`.

TypeScript over JavaScript; bun/bunx over npm/npx; Biome over ESLint + Prettier. Installs: Homebrew on macOS, apt/dnf/pacman on Linux, then ecosystem-native (`uv tool`, `cargo`, `bun`). Avoid nix, Flatpak, Snap.

Available: `rg` `fd` `fzf` `bat` `eza` `z` `delta` `jq` `jless` `dust` `duf` `sd` (over `sed`) `trash` (over `rm`) `gws`. `any2md <input>` converts to Markdown — arxiv id, file, directory, URL, or `-c` for clipboard.

Visual output (TikZ, CSS, Slidev, matplotlib): `docs/visual-layout-quality.md`.
