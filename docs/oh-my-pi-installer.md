# oh-my-pi installer

`scripts/setup/oh-my-pi.py` installs [oh-my-pi](https://github.com/can1357/oh-my-pi) — the `omp` CLI — from GitHub release binaries, verifying each download against the `SHA256SUMS.txt` published in the same release. Every subcommand is a dry run until you pass `--apply`.

## Quick start

```bash
scripts/setup/oh-my-pi.py status                    # what's installed (no network, no writes)
scripts/setup/oh-my-pi.py install                   # dry run: prints the plan
scripts/setup/oh-my-pi.py install --apply           # actually install the latest release
scripts/setup/oh-my-pi.py status --check-latest     # is there a newer tag?
scripts/setup/oh-my-pi.py update --apply            # move to the latest release
scripts/setup/oh-my-pi.py uninstall --apply         # remove everything this script installed
```

Pin a version instead of tracking latest with `install --version v17.3.4 --apply`, and remove one version without touching the rest with `uninstall --version v17.3.4 --apply`.

`update` is `install` plus a "something must already be installed" precondition, so it takes `--version` too — and `update --version <tag>` moves you to exactly that tag, **including backwards**. That is deliberate: a pinned downgrade is the fastest way out of a bad release. If you meant "newest", omit the flag.

## Why this exists rather than upstream's installer

Upstream offers `curl -fsSL https://omp.sh/install | sh`, a Homebrew tap (`can1357/tap`), `bun install -g`, Nix and mise. This repo forbids curl-pipe-shell and forbids third-party Homebrew taps without explicit approval, which leaves the GitHub release binaries as the only compliant channel.

The stronger reason is verification. Reading the served `omp.sh/install` script on 2026-08-15, it downloads the binary and runs it — it performs no checksum check of any kind. This script fetches `SHA256SUMS.txt` from the same release tag as the asset, refuses to install on a mismatch, and refuses to install at all if the checksum file is missing unless you explicitly pass `--allow-unverified`.

## What it will not do

- **Never executes the downloaded artifact**, not even `omp --version` as a smoke test. Upstream's installer does run it; this one prints the command for you to run yourself.
- **Never edits shell startup files.** If `--bin-dir` is not on your `PATH`, or you want completions, it prints the lines and you add them.
- **Never touches `~/.omp`**, which is where oh-my-pi keeps its own config and provider credentials (`~/.omp/agent/config.yml`, `~/.omp/agent/models.yml`). Uninstall leaves it in place; you remove it by hand if you want it gone.
- **Never clobbers an `omp` it did not create.** Upstream's `curl | sh` writes a real ~180 MB binary to `$HOME/.local/bin/omp`, exactly where this script wants its launcher symlink. If something unmanaged is already there, install refuses and tells you; `--force` *moves it aside* to `omp.bak-<timestamp>` rather than deleting it.
- **Never handles secrets.** A TOML key whose name looks like a credential is rejected outright.

## Layout

```
~/.local/share/oh-my-pi/          # --install-dir
├── versions/
│   └── v17.3.4/
│       ├── omp                   # the release binary, mode 0755
│       └── metadata.json         # tag, asset, sha256, verified, installed_at
└── current -> versions/v17.3.4   # relative, so the tree is relocatable

~/.local/bin/omp -> ~/.local/share/oh-my-pi/current/omp    # --bin-dir, absolute
```

Downloads land in a staging directory under `versions/`, are verified there, and are then moved into place with `os.replace`, so an interrupted install cannot leave a half-written version directory looking installed.

`metadata.json` exists because the binary is never run: without a recorded digest, "already installed" would mean nothing more than "a file with the right name exists", and a truncated download would be indistinguishable from a good one. `install` and `update` compare the recorded digest against the release's published digest and skip the download when they match, which is what makes re-running them idempotent.

## Platform selection

Assets are named `omp-{linux,darwin}[-musl]-{x64,arm64}`. Detection is:

| Signal | How |
|---|---|
| OS | `platform.system()`, Linux and macOS only — Windows assets exist upstream and are deliberately unsupported here |
| Arch | `platform.machine()`, plus `sysctl hw.optional.arm64` on macOS so a Rosetta shell doesn't silently install the x64 build on an Apple Silicon Mac |
| libc | `/etc/alpine-release` or a `/lib/ld-musl-*.so.1` loader means musl; nothing is executed to find out |

Override any of them with `--os`, `--arch`, `--libc` when you are preparing a tree for a host you are not running on. The generated asset name is checked against the keys of the release's own `SHA256SUMS.txt`, so if upstream renames an asset you get one clear error instead of a 404 — there is no second copy of the platform table to drift.

musl builds link `libstdc++` and `libgcc` dynamically, so Alpine needs `apk add libstdc++ libgcc`. The script prints this when it selects a musl build.

## Configuration

Config is opt-in: the file is only read when you pass `-c`. There is no implicit default path.

```bash
cp config/oh-my-pi.toml.example ~/.config/oh-my-pi.toml
scripts/setup/oh-my-pi.py -c ~/.config/oh-my-pi.toml install --apply
```

Precedence is **command-line flag > TOML > defaults**. Unknown keys are an error rather than a warning, types are checked (a bare `true` is not accepted for a string field), and secret-looking key names are refused. The full key list with comments is in [`config/oh-my-pi.toml.example`](../config/oh-my-pi.toml.example).

## Shell integration

Nothing is written to your rc files. After installing, add whichever of these you want yourself:

```bash
export PATH="$HOME/.local/bin:$PATH"
eval "$(omp completions zsh)"          # or bash
omp completions fish > ~/.config/fish/completions/omp.fish
```

## Provenance: what verification does and does not prove

Facts established from upstream on 2026-08-15:

- The project is MIT licensed.
- Release assets are built and uploaded by `github-actions[bot]`, not hand-uploaded.
- Every release ships `SHA256SUMS.txt` in coreutils format covering all assets, and the GitHub API additionally exposes a per-asset `digest: sha256:…`.
- The repository publishes **no** build attestations — `GET /repos/can1357/oh-my-pi/attestations/sha256:<digest>` returns 404 — and no detached signature, Sigstore bundle, or minisign key.

So the guarantee is integrity, not independent provenance. Checking the asset against `SHA256SUMS.txt` catches corruption, truncation, and a tampered CDN response. It does **not** catch a compromised release, because the checksum file lives in the same release as the binary it describes and is unsigned: anyone who could replace the binary could replace the sums file alongside it. There is no `gh attestation verify` step to recommend, because there is nothing to verify against.

The practical mitigations are the ones already available: pin a tag with `--version` rather than tracking latest, and re-run `status` to see the digest that was actually installed.

## Tests

`tests/test_oh_my_pi.py` — hermetic and offline. The network layer is poisoned at import time so an accidental fetch fails loudly, every test synthesizes its own tree under a temp dir, and no test executes a downloaded artifact (the "binary" in the fixtures is a text file).

```bash
python3 tests/test_oh_my_pi.py
```
