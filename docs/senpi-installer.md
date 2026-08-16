# Senpi Installer

`scripts/setup/senpi.py` installs [Senpi](https://github.com/code-yeongyu/senpi) — an opinionated MIT-licensed fork of `badlogic/pi-mono`, upstream-labelled **Experimental** — from the project's own GitHub release tarballs, user-locally and with checksum verification. It is a standalone script, not a `deploy.sh` component: Senpi is an optional tool, and nothing in a normal deploy should be pulling a 120 MB binary.

## Usage

```
python3 scripts/setup/senpi.py status
python3 scripts/setup/senpi.py install --apply
python3 scripts/setup/senpi.py install --version v2026.8.14 --apply
python3 scripts/setup/senpi.py update --apply
python3 scripts/setup/senpi.py uninstall --apply
```

Every mutating verb is **dry-run by default** and prints its plan; `--apply` is what writes. `status` never writes at all. The printed plan and the executed steps are one list in the code, not two hand-synced ones, so what a dry run shows is what `--apply` does.

| Flag | Effect |
|---|---|
| `--install-dir DIR` | Root for versioned payloads. Default `~/.local/share/senpi` |
| `--bin-dir DIR` | Where the `senpi` launcher goes. Default `~/.local/bin` |
| `--version V` | Pin a release (`v2026.8.14` or `2026.8.14`). Default: resolve latest |
| `--apply` | Actually write / remove |
| `--allow-unverified` | Proceed when upstream published no checksum for the asset. Prints a loud warning; off by default |

`--install-dir` and `--bin-dir` are independent. Two roots sharing one `--bin-dir` means the last install wins: the launcher is regenerated to point at whichever root was installed most recently, and `status` on the older root then reports its launcher as stale or foreign — that is another root owning it, not tampering. Give each root its own `--bin-dir` if you want both reachable.

## Layout

```
~/.local/share/senpi/
  versions/v2026.8.14/pi/{pi,package.json,photon_rs_bg.wasm,theme/,examples/}
  current -> versions/v2026.8.14
~/.local/bin/senpi          # 5-line sh wrapper: exec "$SENPI_ROOT/current/pi/pi" "$@"
```

The release payload is a directory, not a lone binary — `pi/pi` loads `photon_rs_bg.wasm` and `theme/` from beside itself at runtime — so the whole tree is installed under a versioned directory and reached through a wrapper. A bare symlink onto PATH would work only if the executable resolved its siblings through `realpath(argv[0])` rather than `dirname(argv[0])`, which cannot be determined without running an unaudited 120 MB binary. The wrapper is correct under either resolution strategy.

Keeping versions side by side makes rollback a re-run: `install --version <older> --apply` repoints `current` without re-downloading a payload that is already present.

## Safety properties

- **No curl-pipe-shell.** Everything goes through Python's `urllib` with an explicit timeout; nothing downloaded is ever executed, including to read a version number (that comes from the bundled `pi/package.json`).
- **Checksums are mandatory by default.** The per-release `SHA256SUMS` is fetched and the asset's digest compared before extraction. A missing entry is a refusal, not a warning, unless `--allow-unverified` is passed; a mismatch is always fatal.
- **The tag is resolved once.** `latest` becomes a concrete tag via the GitHub releases API, and both the asset URL and the `SHA256SUMS` URL are built from that one tag — otherwise a release landing mid-run would have the artifact verified against the wrong checksum file.
- **Archives are validated member by member**: `..` components, links escaping the tree, non-regular members, and anything not anchored under the expected `pi/` root are refused. One bad member rejects the whole archive — extraction runs into a staging directory beside the destination and is renamed into place only after every member has validated, so a refused archive leaves nothing at the destination. `tarfile`'s `data` filter is used on 3.12+ in addition, not instead.
- **Extraction is staged, then published by rename.** The payload is unpacked into a staging directory *inside* `versions/` (same filesystem) and `os.replace`d into its final name, so the version dir appears in one step rather than filling up in place. This is a design property of the download path; that path is not covered by the offline test suite and has not been exercised end to end on this machine.
- **Deletion is narrow.** `uninstall` removes only directories that resolve strictly inside the install root; `/`, `$HOME`, its parent, and the root itself are refused outright, as is anything reached by following a symlink out of the tree. A `senpi` launcher this script did not write is reported and left alone. User data under `~/.senpi` is never touched.
- **Shell startup files are never modified.** If `~/.local/bin` is not on `PATH`, the script says so and stops there.

## Verification limitations

Read this before treating an install as trusted.

The GitHub release channel publishes `SHA256SUMS` and **no signature material** — no `.sig`, `.asc`, `.pem`, or in-toto attestation was present on release `v2026.8.14`. The checksum file is served from the same origin as the artifacts it covers, so verifying it proves the download was not corrupted or tampered with *in transit*; it does **not** prove provenance. Anyone who could replace a release asset could replace the checksum line alongside it.

The stronger-provenance channel is npm. `@code-yeongyu/senpi` carries a registry signature and an SLSA provenance v1 attestation (`npm view @code-yeongyu/senpi dist`, `npm audit signatures`). This installer deliberately does not use it: npm requires node >= 24, writes into a global prefix rather than a user-local versioned root, offers no architecture selection, and would need `--ignore-scripts` handling. If provenance matters more to you than any of that, install manually instead:

```sh
npm install -g --ignore-scripts @code-yeongyu/senpi
```

Two further caveats. Upstream labels the project **Experimental**, and the executable is a ~120 MB bun-compiled binary that this repo has not audited. Upstream's own install documentation is a maintainer release runbook for named machines, not general-user guidance, so this script does not mirror it.

## Verifying the install yourself

The script never executes the downloaded binary, so it cannot and does not claim the executable actually starts. After `install --apply`, confirm that yourself:

```sh
senpi --version
```

Senpi also self-updates (`senpi update senpi`, which queries `code-yeongyu/senpi` releases). Prefer this script's `update` verb, which keeps the versioned layout and the checksum check; a self-update writes wherever Senpi decides to.

## Not yet integrated

The README's tool listing and `CLAUDE.md`'s "Common Tasks" table do not mention this script — both files were being edited concurrently when it was added. A one-line row in each is the remaining integration:

```
| Install Senpi (optional coding agent) | `python3 scripts/setup/senpi.py install --apply` — dry run without `--apply`. Docs: `docs/senpi-installer.md` |
```

## Tests

`tests/test_senpi.py` — 149 hermetic offline assertions covering argument parsing, platform/architecture normalization and asset selection, tag validation (including tags arriving from the GitHub API), `SHA256SUMS` parsing, archive-member refusal (traversal, absolute paths, escaping symlinks), the plan/execute invariant, dry-run behaviour, a real `--apply` install over an already-present payload (symlink target, launcher contents and mode), idempotency, update/version handling, launcher generation (including a `shellcheck` pass on the generated wrapper), and deletion safety. No test touches the network or runs a downloaded artifact.

```sh
python3 tests/test_senpi.py
```
