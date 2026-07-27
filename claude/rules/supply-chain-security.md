# Supply Chain Security

## Environment Facts

- A **7-day `min-release-age` quarantine** is configured across npm, bun, pnpm, and uv. Packages published less than 7 days ago fail to install.
- Global `~/.npmrc` sets `ignore-scripts=true`. bun ignores lifecycle scripts by default.
- `UV_MALWARE_CHECK=1` is exported globally (requires uv >=0.11.16). It covers **lockfile syncs only** — `uv add` / `uv sync` — and does NOT run on `uv pip install` or `uv tool install`.
- API keys are scoped per-project via `setup-envrc` + direnv, never globally exported. BWS token lives at `~/.config/bws/token`.

## Never Without Explicit User Approval

- Adding a third-party Homebrew tap (official casks and Mac App Store only).
- Installing from an arbitrary URL or git repo.
- Re-enabling lifecycle scripts (`--ignore-scripts=false`, `ignore-scripts=false`, adding to bun's `trustedDependencies`).
- Bypassing the `min-release-age` quarantine, unsetting `UV_MALWARE_CHECK`, or passing `--no-quarantine` to a cask.

## When an Install Is Blocked

A quarantine or malware-check block is working as intended, not a bug. Tell the user which package and version was blocked and by which guard, then wait — never silently bypass it or suggest disabling the guard globally.
