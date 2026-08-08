# Cloud Setup

Setup scripts for cloud machines: RunPod containers (below) and Hetzner Cloud servers (§ Hetzner Cloud).

## Two-Script Flow

```
create-user.sh   ← infra: non-root user + SSH + /workspace symlinks in runpod mode (idempotent)
setup.sh         ← tools: zsh/vim/tmux (hard) + dotfiles/claude/gh/uv/tailscale (soft)
```

**First boot (run as root):**
```bash
# 1. Create user (infra only — fast, idempotent)
curl -fsSL https://raw.githubusercontent.com/k-kit/dotfiles/main/scripts/cloud/create-user.sh | bash

# 2. Install tools (branch required)
curl -fsSL https://raw.githubusercontent.com/k-kit/dotfiles/main/scripts/cloud/setup.sh | bash -s -- main

# 3. Switch to user
su - k-kit
```

**After pod restart (RunPod only — recreates user + symlinks lost from ephemeral /home):**
```bash
curl -fsSL https://raw.githubusercontent.com/k-kit/dotfiles/main/scripts/cloud/restart.sh | bash
su - k-kit
```
On a VPS this no-ops with a message — /home persists across reboots, so there is nothing to restore.

**If you have permission issues** (ran things as root):
```bash
curl -fsSL https://raw.githubusercontent.com/k-kit/dotfiles/main/scripts/cloud/fix_permissions.sh | bash
su - k-kit
```

## What Each Script Does

### create-user.sh

Idempotent — safe to re-run (also runs on `restart.sh`).

- `apt install sudo zsh openssh-server`
- Creates non-root user with zsh as login shell, NOPASSWD sudo
- Symlinks `/workspace/{code,.claude,.local,.config}` into `~/` (runpod mode only — vps mode skips this, `/home` is already persistent)
- Configures sshd (PubkeyAuthentication, StrictModes on volume-mounted FSes)
- Installs SSH authorized_keys from GitHub + root's keys
- Generates outbound `~/.ssh/id_ed25519` for git/gh

### setup.sh

Tiered installs — **zsh/vim/tmux** fail loud; everything else warns and continues.

| Tier | Tools |
|------|-------|
| **Hard** (abort on fail) | zsh, vim, tmux |
| **Soft** (warn + continue) | mosh, rsync, locale, uv, dotfiles, gh, claude, tailscale, BWS token, gh auth |

**Dropped vs old monolithic setup.sh:** Node.js 24, bun, Codex CLI. Add manually if needed.

## Hetzner vs RunPod

Both scripts detect the environment and log the mode (`Mode: runpod|vps (auto-detected|explicit)`). Auto-detection: `/workspace` exists or `RUNPOD_POD_ID` is set → `runpod`; otherwise `vps`. Override with `CLOUD_MODE=runpod|vps`.

| | RunPod (`runpod`) | Hetzner / VPS (`vps`) |
|---|---|---|
| `/home` | Ephemeral — lost on container restart | Persistent, real disk |
| Persistence | `/workspace` volume; `~/{code,.claude,.local,.config}` symlinked into it | `/home` itself — no symlinks created |
| After restart | `restart.sh` recreates user + symlinks | Nothing needed; `restart.sh` no-ops |
| Tailscale hostname | `runpod-<hostname>` (container names are random hex) | `<hostname>` as-is |
| sshd StrictModes | Often disabled (volume FS ignores chmod) | Stays enabled (chmod works) |
| Root SSH keys | Injected by RunPod, merged into user's `authorized_keys` | Injected by cloud-init, merged the same way |

## RunPod Architecture

```
/home/k-kit/          ← local FS (ephemeral — recreated by create-user.sh on restart)
├── .ssh/              ← local FS
├── code/              → /workspace/code    (persists)
├── .claude/           → /workspace/.claude (persists)
├── .local/            → /workspace/.local  (persists)
└── .config/           → /workspace/.config (persists)
```

## Configuration

Override via env vars:

| Variable | Default | Description |
|----------|---------|-------------|
| `CLOUD_MODE` | auto-detected | `runpod` or `vps` — overrides environment detection |
| `USERNAME` | `k-kit` (or `DOTFILES_USERNAME`) | Non-root username |
| `GITHUB_USER` | `k-kit` | GitHub username (for SSH key import) |
| `DOTFILES_REPO` | `https://github.com/k-kit/dotfiles.git` | Dotfiles repo |
| `DOTFILES_BRANCH` | (required in setup.sh) | Branch to clone |
| `BWS_TOKEN` | (unset) | BWS access token (non-interactive) |
| `TAILSCALE_AUTH_KEY` | (unset) | Tailscale auth key (non-interactive) |
| `GITHUB_TOKEN` | (unset) | GitHub PAT — runs `gh auth login --with-token` on stdin, then `gh auth setup-git`. Never put this in cloud-init user-data; the metadata service serves user-data to every local user |
| `INTERACTIVE` | `0` | Set `1` / pass `-i` to prompt for secrets |
| `GITHUB_AUTH` | `0` | Set `1` / pass `--github-auth` to auth gh inline (interactive web flow) |

### Non-interactive by default

`setup.sh` never blocks on a prompt — safe for `curl | bash` with no TTY.
Supply secrets via env (`BWS_TOKEN=…`, `TAILSCALE_AUTH_KEY=…`) or set them up after login.
Pass `-i` / `--interactive` to prompt on a box with a real terminal.

Secret resolution order in setup.sh: env var → fnox (`~/fnox.toml`, only if the fnox CLI and the age key at `~/.config/fnox/age.txt` are both on the box — the key is never in the repo) → interactive prompt (`-i`) → skip with a post-boot hint.

## Hetzner Cloud

`hetzner-cloud-init.yaml` is a cloud-init user-data file that runs the same two-script flow automatically on first boot. Unlike RunPod, Hetzner `/home` is persistent, so there are no `/workspace` symlinks (create-user.sh only makes them when `/workspace` exists) and no restart dance — the machine survives reboots with its home directory intact.

It works by writing identity to `/etc/dotfiles-bootstrap.env` (`USERNAME`/`GITHUB_USER`/`DOTFILES_REPO`/`DOTFILES_BRANCH`, matching `config.sh`) plus a `/usr/local/sbin/dotfiles-bootstrap` script that curls `create-user.sh` and `setup.sh main` from the repo. A marker file (`/var/lib/dotfiles-bootstrap.done`) short-circuits re-runs; both downstream scripts are idempotent anyway.

**Prerequisite:** the bootstrap curls `scripts/cloud/*` from GitHub at the branch named in `DOTFILES_BRANCH` — changes to these scripts must be **pushed** before a new server can use them. `hz create --branch <name>` points a throwaway box at an unmerged branch, which is how to test a change to this flow without merging it to `main` first.

### End to end: what is automatic, what is not

`hz small dev-box` now runs the whole path and blocks until it can tell you whether it worked. What it does *not* do is the short list below, and each item is a deliberate gate rather than an omission.

| Step | Status |
|---|---|
| Server create, SSH key, `~/.ssh/config` entry | automatic |
| User, sudo, sshd, authorized_keys from GitHub | automatic (`create-user.sh`) |
| zsh/vim/tmux, uv, gh, claude, tailscale, mosh | automatic (`setup.sh`) |
| dotfiles clone + `install.sh` + `deploy.sh` | automatic |
| logind `KillUserProcesses=no` + linger | automatic |
| Idle-shutdown watchdog | automatic with `--idle-shutdown` (opt-in — it powers a machine off) |
| Tailscale join | automatic when `TAILSCALE_AUTH_KEY` resolves from fnox |
| BWS token | automatic **once `BWS_TOKEN` exists in fnox** — it does not today, so this is currently manual (`secrets-init-bws`). Add it with `fnox set BWS_TOKEN` |
| `gh auth login` | opt-in via `hz create --github-auth`; otherwise manual |
| `claude` login | **manual, irreducible** — it is an interactive OAuth flow |
| Adding the box's generated `~/.ssh/id_ed25519.pub` to GitHub | manual — only needed for SSH-protocol git pushes; `--github-auth` covers HTTPS pushes via the gh credential helper |
| Pushing changes to these scripts before creating a box | manual — the bootstrap fetches them from GitHub, not from your working tree |

**fnox does not work on a fresh box.** `setup.sh` can resolve secrets from fnox, but only when the age key is present at `~/.config/fnox/age.txt`, and that key is never in the repo and nothing in the bootstrap puts it there. So on a first boot the fnox branch is always a no-op, and every secret must arrive either through user-data (rendered by `hetzner-user-data.sh` from fnox *on your machine*) or over SSH afterwards. Copy the age key across by hand if you want on-box fnox.

### Where the checkout lives

The cloud checkout is `~/code/dotfiles`, matching a workstation, and that is a decision rather than an accident. `install.sh` and `deploy.sh` derive `DOT_DIR` from their own location (`deploy.sh` uses `realpath "$0"`), so the machinery itself would work from `~/dotfiles` and moving it would be a one-line change in `setup.sh`.

This used to be justified by a bug, and that justification is now gone. `config/zshrc.sh` assigned `DOT_DIR` **without exporting it** — while the bash branch of `deploy.sh` had always written `export DOT_DIR=…` — so every deployed consumer written as `${DOT_DIR:-$HOME/code/dotfiles}` fell back to the literal whenever it was not launched from a login zsh, which is the normal case for a Claude Code hook. That was an argument for adding the `export`, not for freezing the path, and the `export` is now there.

What still argues for staying put is the reference count and path parity. Roughly nineteen files name `code/dotfiles`, and several are formats that cannot hold fallback logic at all — `config/auto-mode-proxy.plist`, `config/systemd-user/vault-sync-tripwire.service`, `config/zed/settings.json`, `codex/rules/*.rules`. Moving means a mechanical edit of every one of them for no functional gain, and it costs the property that a command pasted from a workstation into a cloud box works unchanged.

Backward compatibility, so the location is no longer load-bearing: `claude/hooks/context_auto_apply.sh` now derives `DOT_DIR` from its own `realpath` when it arrives unset. `~/.claude` is a symlink to `$DOT_DIR/claude`, so two levels up from the resolved hook path *is* the checkout, wherever it lives; the hardcoded literal survives only as a last resort for a box without `realpath`. If the checkout ever does move, that hook keeps working, and `tools/claude-tools/src/ignore/mod.rs` already probes `code/dotfiles`, `dotfiles` and `.dotfiles` in turn. `tests/test_cloud_provisioning.sh` pins the export, the resolver's behaviour against a fixture symlink layout, and the clone path in `setup.sh`.

### Console (paste)

Create Server → pick an Ubuntu image and your SSH key → expand the **Cloud config** text box (under SSH keys / networking options) → paste the full contents of `hetzner-cloud-init.yaml` → create. The SSH key you select in the console lands in root's `authorized_keys`, and create-user.sh merges root's keys into the new user's — so that key logs you in as `k-kit` too.

### CLI (hcloud)

```bash
hcloud server create \
    --name dev-box \
    --type cx23 \
    --image ubuntu-24.04 \
    --ssh-key <your-key-name> \
    --user-data-from-file scripts/cloud/hetzner-cloud-init.yaml
```

(`cx23` is the cheapest 4 GB shared-vCPU type as of 2026-08-08 — the lineup changes, so check `hcloud server-type list`. `cx22` is gone.)

### Secrets (optional, post-boot)

Don't paste secrets into the console. After first login: `secrets-init-bws` for the BWS token, `sudo tailscale up --ssh --authkey <key>` for Tailscale. If you accept root-readable secrets on disk, you can instead uncomment `BWS_TOKEN`/`TAILSCALE_AUTH_KEY` in the env block before pasting and setup.sh will consume them.

### Secrets via fnox (no pasting)

`hetzner-user-data.sh` renders `hetzner-cloud-init.yaml` with the env-block secrets resolved by name from fnox (`~/fnox.toml`, age-encrypted), so no key is ever pasted or typed. The Hetzner API token also comes from fnox (secret name `HERTZNER` — hcloud reads `HCLOUD_TOKEN`):

```bash
scripts/cloud/hetzner-user-data.sh > "$TMPDIR/user-data.yaml"
HCLOUD_TOKEN="$(fnox get HERTZNER)" hcloud server create \
    --name dev-box --type cx23 --image ubuntu-24.04 \
    --ssh-key <your-key-name> --user-data-from-file "$TMPDIR/user-data.yaml"
rm "$TMPDIR/user-data.yaml"
```

Injected when resolvable: `TAILSCALE_AUTH_KEY`, `BWS_TOKEN` (add `BWS_TOKEN` to fnox with `fnox set BWS_TOKEN` to enable it). Unresolved names stay commented and the post-boot path above still applies. The rendered file carries the same root-readable/metadata-service exposure as hand-filling the env block — delete it after `server create`, and the script refuses to print secrets to a terminal.

### hz CLI

`custom_bins/hz` wraps the whole flow: token from fnox (`HERTZNER`, or an existing `HCLOUD_TOKEN`), user-data rendered by `hetzner-user-data.sh`, and a managed `Host` block in `~/.ssh/config` per server. Config edits are confined to `# BEGIN hz <alias>` … `# END hz <alias>` marker blocks (idempotent upserts, user content untouched), and the pre-hz config is backed up once to `~/.ssh/config.hz-backup`.

```bash
hz list                                   # name, status, ipv4, type, idle label
hz small dev-box                          # preset: create + ssh-add + 2h idle-shutdown, then `ssh dev-box`
hz medium dev-box                         # same, 8 vCPU / 16 GB
hz max dev-box --yes                      # same, 48 dedicated vCPU / 192 GB (--yes required — 155x small's rate)
hz medium dev-box --idle-hours 4          # any create option overrides the preset
hz create dev-box --yes                   # no preset: bare defaults + ssh-add; --yes required — it bills
hz create dev-box --yes --idle-shutdown   # + auto-poweroff after 2h idle (tune: --idle-hours N)
hz create dev-box --yes --branch wip      # bootstrap from a non-main dotfiles branch
hz create dev-box --yes --github-auth     # + push `fnox get GITHUB_TOKEN` over SSH and run gh auth login
hz create dev-box --yes --no-wait         # return as soon as the server exists
hz up dev-box                             # shortcut for --idle-shutdown --yes (bills!)
hz wait dev-box                           # block until cloud-init + bootstrap finish, report the verdict
hz ssh-add dev-box --user root --alias hz-1
hz ssh-sync --dry-run                     # Host entry per running server; flags stale entries
hz idle enable dev-box --hours 2          # retrofit the watchdog onto an existing server
hz idle status dev-box                    # label + which activity signals are live
hz delete dev-box --yes                   # delete server + its ssh config entries
hz poweron dev-box                        # wake a powered-off server
```

`create` now **blocks until the bootstrap finishes** and reports whether it succeeded, instead of printing a "next steps" list for you to run by hand. It polls sshd first (a fresh box refuses connections for the first ~20–30 s, which is not an error), then waits on `cloud-init status`, then reads the one-word verdict the bootstrap leaves in `/var/lib/dotfiles-bootstrap.status`. On failure it prints the last 30 lines of `/var/log/dotfiles-bootstrap.log`. The whole thing is bounded by `--timeout` (default 900 s) and times out with "retry: `hz wait <name>`" rather than hanging; `--no-wait` restores the old fire-and-forget behaviour.

`--github-auth` is opt-in because it moves a credential onto a remote machine. The token is read from fnox (`GITHUB_TOKEN`) on your machine and pushed over SSH **on stdin** after the box is up — never through user-data, because Hetzner's metadata service serves user-data to every local user on the box, and never through argv, which is world-readable via `/proc`.

#### Size presets

| Preset | Type | Spec | EUR/h (hel1) | ~EUR/mo |
|---|---|---|---|---|
| `small` | `cx23` | 2 vCPU shared, 4 GB, 40 GB | 0.0104 | 6.49 |
| `medium` | `cx43` | 8 vCPU shared, 16 GB, 160 GB | 0.0296 | 18.49 |
| `max` | `ccx63` | 48 vCPU dedicated, 192 GB, 960 GB | 1.6138 | 1007 |

All three create the server, add the `~/.ssh/config` entry, and enable the 2h idle-shutdown watchdog. `small` and `medium` imply `--yes`; `max` demands it explicitly, because an idle-shutdown failure there costs ~EUR 39/day rather than ~EUR 0.25. Prices are EUR at `hel1`, checked 2026-08-08 — re-check with `hcloud server-type describe <type>`, and edit `preset_type`/`preset_spec` in `custom_bins/hz` to change the lineup. x86 shared is deliberate: ARM (`cax*`) is cheaper per core, but the dotfiles bootstrap has only been exercised on x86. Default location is `hel1` (override with `--location` or `HZ_LOCATION`); note `cx*` types are EU-only, while `cpx*`/`ccx*` also serve `ash`/`hil`/`sin`.

### Idle auto-shutdown (opt-in, default OFF)

`idle-shutdown-install.sh` installs an on-box systemd timer (5-minute checks, 15-minute boot grace) that runs `systemctl poweroff` after N hours (default 2) with no activity. `hz create --idle-shutdown` ships it inside cloud-init; `hz idle enable <server>` retrofits it over SSH as root; `hz idle disable` removes it. Enabled servers carry the label `dotfiles.idle-shutdown=<N>h`, which `hz list` shows in the IDLE column.

**What counts as activity.** The check is about work happening, not about artefacts existing — which is the difference between a watchdog that kills your run and one that never fires:

| Signal | Counts as active |
|---|---|
| `~/.keep-alive` in any home or `/root` | yes — the explicit manual inhibitor |
| An established TCP connection to sshd | yes — somebody is connected right now |
| A tmux pane whose foreground process is **not** a bare shell | yes — this is the detached-agent case |
| A tmux session sitting at an idle shell | **no** — `cw` and tmux-resurrect leave these behind, and counting them would make the watchdog a silent no-op on a box that keeps billing |
| A login whose tty has seen output within the idle window | yes |
| A login whose tty has been silent for the whole window | **no** — the earlier unconditional `who` check returned true for as long as a forgotten session stayed open |

The previous check only looked at SSH connections and `who`, so a **detached** tmux session running a long agent workflow with nobody attached scored as idle and the box powered off mid-run. That is the case the tmux signal above exists for.

`hz idle status <server>` prints each signal and the resulting verdict (it calls the installed checker with `--explain`, so there is no second copy of the logic to drift).

- Inhibit without uninstalling: `touch ~/.keep-alive` on the box (any user's home or `/root`); remove the file to re-arm. Still the right tool for work that produces no output for hours — a silent compile has no tmux foreground process to detect if it is not running under tmux.
- A powered-off server keeps its disk and IP — those still bill, CPU/RAM don't. Wake it with `hz poweron <name>` (= `hcloud server poweron <name>`).
- A reboot resets the idle clock (state lives in `/run`).

### Long detached sessions

Two systemd defaults decide whether an agent run survives the SSH session that started it, and `setup.sh` pins both rather than trusting the distro:

- `KillUserProcesses=no` via `/etc/systemd/logind.conf.d/10-dotfiles-keep-user-processes.conf`. Ubuntu already defaults to `no`; the drop-in makes it explicit. logind is deliberately **not** restarted during setup — restarting it from inside an SSH session is the one thing that could drop that session — so the drop-in takes effect from the next boot, which costs nothing given the default already matches.
- `loginctl enable-linger <user>`, so the per-user systemd manager (and anything in its scope) keeps running after the last logout.

On the tmux side, `config/tmux.conf` sets `history-limit 100000` (up from 10000, which was chosen for mobile use and truncates a long agent run's early output — the part usually worth reading) and pins `destroy-unattached off`.

### Verify / debug

```bash
hz wait dev-box                               # sshd, then cloud-init, then the bootstrap verdict
ssh root@<ip> cat /var/lib/dotfiles-bootstrap.status   # ok | running | failed-create-user | failed-setup
ssh root@<ip> tail -f /var/log/dotfiles-bootstrap.log
ssh k-kit@<ip>                                # the end state that matters
```

Force a full re-run: `rm /var/lib/dotfiles-bootstrap.done && /usr/local/sbin/dotfiles-bootstrap` (as root). Cloud-init's own logs are at `/var/log/cloud-init-output.log`.

### Tests

`bash tests/test_cloud_provisioning.sh` — hermetic, creates nothing, calls no API. It covers user-data rendering (including that the branch substitution hits exactly one line and that no `GITHUB_TOKEN` ever appears), the `hz create` argument surface via the pre-`--yes` confirmation line, the tmux limits, and the idle-shutdown activity check run for real against a live tmux socket in a fixture `TMUX_TMPDIR`. It also asserts that `runcmd:` is still the last top-level key in `hetzner-cloud-init.yaml`, because `hz` ships the idle watchdog by string-appending a list item to the end of the rendered file — add a top-level key after `runcmd:` and that append silently lands in the wrong block.

### Cheap smoke test

Create the smallest shared-vCPU server with the CLI command above, wait for `cloud-init status --wait` to report done, confirm `ssh k-kit@<ip>` gives a zsh shell with dotfiles deployed (`ls ~/code/dotfiles`), then `hcloud server delete dev-box`. A few minutes of the smallest instance costs on the order of a cent — Hetzner bills hourly.
