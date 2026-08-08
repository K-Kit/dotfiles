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
| `INTERACTIVE` | `0` | Set `1` / pass `-i` to prompt for secrets |
| `GITHUB_AUTH` | `0` | Set `1` / pass `--github-auth` to auth gh inline |

### Non-interactive by default

`setup.sh` never blocks on a prompt — safe for `curl | bash` with no TTY.
Supply secrets via env (`BWS_TOKEN=…`, `TAILSCALE_AUTH_KEY=…`) or set them up after login.
Pass `-i` / `--interactive` to prompt on a box with a real terminal.

Secret resolution order in setup.sh: env var → fnox (`~/fnox.toml`, only if the fnox CLI and the age key at `~/.config/fnox/age.txt` are both on the box — the key is never in the repo) → interactive prompt (`-i`) → skip with a post-boot hint.

## Hetzner Cloud

`hetzner-cloud-init.yaml` is a cloud-init user-data file that runs the same two-script flow automatically on first boot. Unlike RunPod, Hetzner `/home` is persistent, so there are no `/workspace` symlinks (create-user.sh only makes them when `/workspace` exists) and no restart dance — the machine survives reboots with its home directory intact.

It works by writing identity to `/etc/dotfiles-bootstrap.env` (`USERNAME`/`GITHUB_USER`/`DOTFILES_REPO`/`DOTFILES_BRANCH`, matching `config.sh`) plus a `/usr/local/sbin/dotfiles-bootstrap` script that curls `create-user.sh` and `setup.sh main` from the repo. A marker file (`/var/lib/dotfiles-bootstrap.done`) short-circuits re-runs; both downstream scripts are idempotent anyway.

**Prerequisite:** the bootstrap curls `scripts/cloud/*` from the `main` branch on GitHub — changes to these scripts must be pushed before a new server can use them.

### Console (paste)

Create Server → pick an Ubuntu image and your SSH key → expand the **Cloud config** text box (under SSH keys / networking options) → paste the full contents of `hetzner-cloud-init.yaml` → create. The SSH key you select in the console lands in root's `authorized_keys`, and create-user.sh merges root's keys into the new user's — so that key logs you in as `k-kit` too.

### CLI (hcloud)

```bash
hcloud server create \
    --name dev-box \
    --type cx22 \
    --image ubuntu-24.04 \
    --ssh-key <your-key-name> \
    --user-data-from-file scripts/cloud/hetzner-cloud-init.yaml
```

(`cx22` was the cheapest shared-vCPU type at time of writing — check `hcloud server-type list` for the current lineup.)

### Secrets (optional, post-boot)

Don't paste secrets into the console. After first login: `secrets-init-bws` for the BWS token, `sudo tailscale up --ssh --authkey <key>` for Tailscale. If you accept root-readable secrets on disk, you can instead uncomment `BWS_TOKEN`/`TAILSCALE_AUTH_KEY` in the env block before pasting and setup.sh will consume them.

### Secrets via fnox (no pasting)

`hetzner-user-data.sh` renders `hetzner-cloud-init.yaml` with the env-block secrets resolved by name from fnox (`~/fnox.toml`, age-encrypted), so no key is ever pasted or typed. The Hetzner API token also comes from fnox (secret name `HERTZNER` — hcloud reads `HCLOUD_TOKEN`):

```bash
scripts/cloud/hetzner-user-data.sh > "$TMPDIR/user-data.yaml"
HCLOUD_TOKEN="$(fnox get HERTZNER)" hcloud server create \
    --name dev-box --type cx22 --image ubuntu-24.04 \
    --ssh-key <your-key-name> --user-data-from-file "$TMPDIR/user-data.yaml"
rm "$TMPDIR/user-data.yaml"
```

Injected when resolvable: `TAILSCALE_AUTH_KEY`, `BWS_TOKEN` (add `BWS_TOKEN` to fnox with `fnox set BWS_TOKEN` to enable it). Unresolved names stay commented and the post-boot path above still applies. The rendered file carries the same root-readable/metadata-service exposure as hand-filling the env block — delete it after `server create`, and the script refuses to print secrets to a terminal.

### hz CLI

`custom_bins/hz` wraps the whole flow: token from fnox (`HERTZNER`, or an existing `HCLOUD_TOKEN`), user-data rendered by `hetzner-user-data.sh`, and a managed `Host` block in `~/.ssh/config` per server. Config edits are confined to `# BEGIN hz <alias>` … `# END hz <alias>` marker blocks (idempotent upserts, user content untouched), and the pre-hz config is backed up once to `~/.ssh/config.hz-backup`.

```bash
hz list                                   # name, status, ipv4, type, idle label
hz create dev-box --yes                   # create (cx22/ubuntu-24.04) + ssh-add; --yes required — it bills
hz create dev-box --yes --idle-shutdown   # + auto-poweroff after 2h idle (tune: --idle-hours N)
hz ssh-add dev-box --user root --alias hz-1
hz ssh-sync --dry-run                     # Host entry per running server; flags stale entries
hz idle enable dev-box --hours 2          # retrofit the watchdog onto an existing server
hz idle status dev-box                    # label + on-box watchdog state
hz delete dev-box --yes                   # delete server + its ssh config entries
hz poweron dev-box                        # wake a powered-off server
```

### Idle auto-shutdown (opt-in, default OFF)

`idle-shutdown-install.sh` installs an on-box systemd timer (5-minute checks, 15-minute boot grace) that runs `systemctl poweroff` after N hours (default 2) with no established SSH connections and no logged-in sessions. `hz create --idle-shutdown` ships it inside cloud-init; `hz idle enable <server>` retrofits it over SSH as root; `hz idle disable` removes it. Enabled servers carry the label `dotfiles.idle-shutdown=<N>h`, which `hz list` shows in the IDLE column.

- Inhibit without uninstalling: `touch ~/.keep-alive` on the box (any user's home or `/root`); remove the file to re-arm.
- A powered-off server keeps its disk and IP — those still bill, CPU/RAM don't. Wake it with `hz poweron <name>` (= `hcloud server poweron <name>`).
- A reboot resets the idle clock (state lives in `/run`).

### Verify / debug

```bash
ssh root@<ip> cloud-init status --wait        # blocks until first boot finishes
ssh root@<ip> tail -f /var/log/dotfiles-bootstrap.log
ssh k-kit@<ip>                                # the end state that matters
```

Force a full re-run: `rm /var/lib/dotfiles-bootstrap.done && /usr/local/sbin/dotfiles-bootstrap` (as root). Cloud-init's own logs are at `/var/log/cloud-init-output.log`.

### Cheap smoke test

Create the smallest shared-vCPU server with the CLI command above, wait for `cloud-init status --wait` to report done, confirm `ssh k-kit@<ip>` gives a zsh shell with dotfiles deployed (`ls ~/code/dotfiles`), then `hcloud server delete dev-box`. A few minutes of the smallest instance costs on the order of a cent — Hetzner bills hourly.
