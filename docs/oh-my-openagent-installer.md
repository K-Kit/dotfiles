# oh-my-openagent installer

`scripts/setup/oh-my-openagent.py` installs the [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) CLI into a user-local, versioned directory, verifying the artifact before anything is unpacked. Dry run by default; nothing is written until you pass `--apply`.

## Quick start

```sh
scripts/setup/oh-my-openagent.py status --remote     # what is installed, and what the registry offers
scripts/setup/oh-my-openagent.py install             # dry run — prints the plan, writes nothing
scripts/setup/oh-my-openagent.py install --apply     # actually install
oh-my-openagent --version                            # run it yourself; the installer never does
```

## Commands

| Command | Does |
|---|---|
| `status` | Local state: installed versions, `current`, launcher freshness, whether `npm` and `openssl` are present. `--remote` also resolves the dist-tag and reports provenance |
| `install` | Resolve, download, verify, `npm install` into `versions/<version>`, repoint `current`, write the launcher |
| `update` | Same as install, after checking whether the resolved version is already current |
| `uninstall` | Remove every version, the `current` symlink, and the launcher — but only if the launcher is byte-identical to one this installer wrote |

Every command is a dry run until `--apply` (`status` never writes at all). Re-running `install --apply` is idempotent: an already-complete version proposes no work, and a deleted launcher is the only thing regenerated.

## Layout

```
~/.local/share/oh-my-openagent/
  versions/4.19.4/          # npm --prefix target: lib/node_modules/… plus bin/ shims
  current -> versions/4.19.4
~/.local/bin/oh-my-openagent   # generated POSIX-sh launcher, execs through `current`
```

Because `current` is a symlink, update and rollback are a symlink swap — the previous version stays on disk until you uninstall it. The launcher carries a "managed by" banner and is regenerated whenever it drifts; if a file of that name exists that this installer did not write, it is left strictly alone, including by `uninstall`.

## Configuration

Precedence is command-line flags > `--config` TOML > built-in defaults. Copy `config/oh-my-openagent.toml.example`, which documents every key and whose values are all the defaults, so an empty file behaves identically. Unknown keys are an error, and any key whose name looks like a credential (`password`, `secret`, `token`, `passphrase`, `credential`) is rejected at parse time — this installer never handles oh-my-openagent's API keys.

Shared flags work on either side of the subcommand, so `install --version 4.19.4` and `--version 4.19.4 install` are equivalent.

## Verification, and exactly what it proves

npm is the **only** distribution channel for this project. All 30 GitHub releases publish zero assets, and the platform packages (`oh-my-opencode-linux-x64` and siblings) are ~4 KB stubs containing `bin/.gitkeep`, a 3.9 KB JS shim and a `package.json` — not binaries. There is therefore no upstream `SHA256SUMS`, no GitHub release artifact, and no author-published detached signature. The verification material is what the npm registry publishes, and the installer uses all of it that it can:

| Check | Status | What it actually proves |
|---|---|---|
| sha512 vs `dist.integrity` | Mandatory, always | The bytes match what the registry's metadata says. Since the metadata and the tarball URL come from the same response, this proves **transport integrity, not authenticity** — a compromised registry could serve a consistent pair |
| npm registry ECDSA signature | On by default, `--no-verify-signature` to skip | The registry (key `SHA256:DhQ8wR5APBvFHLF/+Tc+AYvPOdTpcIDqOhxsBHRwC7U`, fetched live by keyid, never hardcoded) attests to `<name>@<version>:<integrity>`. This is **npm's** key, not the author's — it proves npm served this, not that the author wrote it |
| SLSA provenance | Reported, **not verified** | The package publishes `https://slsa.dev/provenance/v1` and npm publish-attestation predicates via GitHub Actions OIDC. Verifying them needs sigstore; run `npm audit signatures` yourself if you want that |
| min-release-age | 7 days by default | Mirrors the repo-wide quarantine, so a freshly-published (possibly compromised) version is not installed on the day it lands |

The signature result is deliberately tri-state and never silent: **verified** says so, **failed** is a hard refusal that `--allow-unverified` does *not* override, and **unavailable** (no `openssl`, or the keyid is absent from the published key set) blocks the install unless you pass `--allow-unverified`, which prints a loud warning.

The honest summary: this installer gives you reproducibility, integrity, and npm-attested delivery. It does **not** give you author authenticity, because upstream publishes nothing that would support it.

## What it deliberately does not do

It installs the CLI and stops. It does not run upstream's own `oh-my-openagent install` / `npx lazycodex-ai install` step, which writes `~/.codex/config.toml`, edits `opencode.json`, creates `~/.codex/agents/` and `~/.codex/plugins/cache/sisyphuslabs/`, and symlinks about a dozen component binaries (`omo-rules`, `omo-lsp`, `omo-ultrawork`, `ulw`, `lazycodex-executor-verify` and others) into `~/.local/bin`. Run that yourself if you want it, with full sight of what it changes.

It also never curl-pipes to a shell, never executes the downloaded artifact (including in tests), never edits a shell startup file — it warns if `~/.local/bin` is off your PATH and leaves the fix to you — and never touches user data in `~/.omo`, `~/.codex`, or `opencode.json`, including on uninstall.

Only the `oh-my-openagent` command is linked. The package's `bin` map also claims `omo`, `lazycodex`, `lazycodex-ai` and `oh-my-opencode`; `omo` in particular is an unrelated package by a different author, so linking it would be a name collision waiting to happen.

Lifecycle scripts stay off (`--ignore-scripts`, matching the global `~/.npmrc`). If npm refuses a transitive dependency under its own quarantine, that refusal is reported with the offending output and the install stops — it is never bypassed.

## Upstream facts worth knowing before you install

The license is **SUL-1.0**, which is not an OSI-approved licence; GitHub's licence detection reports `NOASSERTION`. Read [LICENSE.md](https://github.com/code-yeongyu/oh-my-openagent/blob/dev/LICENSE.md) and decide for yourself.

**Anonymous telemetry is on by default.** Upstream documents one PostHog event at most once per UTC day per machine, keyed by a SHA256-hashed installation id rather than the hostname. Opt out with `"telemetry": false` in the oh-my-openagent config, or `OMO_DISABLE_POSTHOG=1`, or `OMO_SEND_ANONYMOUS_TELEMETRY=0`; the Codex Light edition additionally honours `OMO_CODEX_DISABLE_POSTHOG=1` and `OMO_CODEX_SEND_ANONYMOUS_TELEMETRY=0`, and the global flags cover it too. None of this is under this installer's control — it is the program's own runtime behaviour.

Its own configuration and state live at `~/.omo/omo.jsonc` (with `~/.omo/teams/`, `~/.omo/rules/`) and per-project `.omo/`, plus `opencode.json` and `~/.codex/**` depending on which edition you enable.

Upstream's update mechanism is re-running its npx/npm installer, or `codex plugin marketplace upgrade sisyphuslabs` for the Codex edition. This installer's `update` replaces the first of those for the CLI itself.

Node.js is required, since the payload is a Node program with 17 runtime and 12 optional dependencies. The published manifest declares no `engines` constraint, so there is no minimum version to enforce.

## Tests

```sh
python3 tests/test_oh_my_openagent.py
```

204 assertions, hermetic and offline: no test performs network I/O or executes a downloaded artifact. The npm path runs against a fake `npm` on `PATH` that reproduces the real `-g --prefix` layout; the signature path generates a real EC keypair with `openssl` in-test and asserts that a valid signature verifies, a flipped byte fails, and a signature bound to a different version fails, so the crypto is exercised rather than mocked. The generated launcher is written to a temp file and shellchecked as an assertion.

`tests/` has no runner and no CI workflow in this repo — these files are executed directly.
