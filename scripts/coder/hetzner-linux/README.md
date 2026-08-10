---
display_name: Hetzner Cloud (Linux)
description: Provision Hetzner Cloud servers as Coder workspaces, with a persistent /home volume
icon: /emojis/1f5a5.png
maintainer_github: k-kit
tags: [vm, linux, hetzner]
---

# Remote development on Hetzner Cloud

Provisions a Hetzner Cloud server as a [Coder workspace](https://coder.com/docs/user-guides/workspace-management). Coder ships no Hetzner example upstream, so this template follows the shape of the official [`digitalocean-linux`](https://github.com/coder/coder/tree/main/examples/templates/digitalocean-linux) example: an ephemeral VM plus a persistent volume mounted at `/home/<owner>`.

## Architecture

- `hcloud_server` — the workspace VM. Gated on `data.coder_workspace.me.start_count`, so **stopping the workspace destroys the server** and you stop paying for it.
- `hcloud_volume` — persistent `/home/<owner>`. Created with `location` rather than `server_id` so it survives the server, formatted `ext4` once by Terraform.
- `hcloud_volume_attachment` — reattaches the volume on each start.
- cloud-init (`cloud-config.yaml.tftpl`) — creates the user, waits for the volume device, mounts it, then starts the Coder agent as a systemd unit.

Anything outside `/home` is lost on stop. Bake tools into a snapshot image, or use a [startup script](https://registry.terraform.io/providers/coder/coder/latest/docs/resources/script), if you need them to persist.

## Prerequisites

- A Hetzner Cloud project and an API token with read/write access (Console → Security → API tokens).
- Optionally, an SSH key in the same project (`hcloud ssh-key list`) for break-glass access over port 22. Coder itself does not need it — it reaches the workspace through the agent.

## Authentication

Terraform runs on the Coder provisioner, not on your laptop, so `HCLOUD_TOKEN` has to be set **where `coder server` or `coder provisioner start` runs** — exporting it in the shell you run `coder templates push` from does nothing.

If that is awkward (Coder-hosted provisioners, for instance), pass the token as a template variable instead. It is then stored server-side with the template version:

```bash
coder templates push hetzner-linux \
  -d scripts/coder/hetzner-linux \
  --var hcloud_token="$(fnox get HERTZNER)"
```

`HERTZNER` is this repo's [fnox](../../../config/fnox.toml) secret name for the Hetzner token — see [`scripts/coder/README.md`](../README.md).

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| Location | `fsn1` | `fsn1`/`nbg1`/`hel1` (EU), `ash`/`hil` (US), `sin` (APAC) |
| Server type | `cx23` | 2 vCPU / 4 GB, EU only. Availability is regional — see below. Changing Location away from the EU means changing this too |
| Image | `ubuntu-24.04` | Ubuntu 22.04 and Debian 12 also offered |
| Home volume size | `20` GB | Hetzner volumes start at 10 GB |

Template variables (`--var`) rather than workspace parameters: `hcloud_token`, `ssh_keys`, `dotfiles_uri`.

## Location and server type are coupled

There is no shared-vCPU type that exists in every Hetzner location — the families are partitioned by region, so "just pick a universally-valid default" is not an available move:

| Location | Types |
|---|---|
| `fsn1`, `nbg1`, `hel1` (EU) | `cx23`, `cx33`, `cx43`, `cpx22`, `cpx32` |
| `ash`, `hil` (US) | `cpx11`, `cpx21`, `cpx31` |
| `sin` (Singapore) | `cpx12`, `cpx22`, `cpx32` |

Coder parameters cannot validate against one another, so nothing in the workspace form stops you from choosing an impossible pair. Terraform can: `hcloud_volume.home` carries a `precondition` that compares the two, and because both values are known before apply it is checked during **plan** — a bad pair is refused with an explanation and nothing is created. It sits on the volume rather than the server because the volume has no `count`, so it is evaluated even while the workspace is stopped; that is the case where a server-side check would let you bank a billable volume you cannot boot.

Only the CX rule is enforced. It is Hetzner's own documented constraint. The CPX generation split (`cpx*2` in the EU and Singapore, `cpx*1` in the US) is recorded in the option labels but deliberately not hard-blocked — a wrong entry in a plan-time precondition refuses a build that would have worked, which is worse than the apply-time error it would have replaced. Check a specific pair with `hcloud server-type describe <type>`.

## Persistent /home, and how you find out when it isn't

The volume is created with `location` rather than `server_id`, so it outlives the server that Coder destroys on every stop. It is formatted exactly once, by Terraform's `format` argument — `coder-home-mount` only ever mounts it, so no rebuild can run `mkfs` over your files.

The mount is `defaults,nofail`, and `coder-home-mount` exits 0 when the device never appears. Both are deliberate: a workspace that refuses to boot is worse than one on the wrong disk. The cost is that the failure is otherwise invisible — the workspace comes up healthy, `/home` is quietly on the ephemeral root disk, and everything written there is destroyed at the next stop.

`coder_agent.main.startup_script` closes that gap. It runs `mountpoint -q /home/<owner>` and exits non-zero when the home directory is not on the volume, which Coder surfaces as a **failed startup script** in the UI. It is deliberately `non-blocking` despite the provider recommending `blocking`, because the whole point is to stay reachable so you can look at the volume by hand.

Because the mount happens in `runcmd`, after cloud-init's `users-groups` module has already written `/etc/skel` to the root disk, the helper copies the skel files onto the volume afterwards — otherwise a brand-new workspace gets a home with no `.bashrc` or `.profile`. The copy is `cp -rn`, so an existing home is never overwritten.

## The agent token

`CODER_AGENT_TOKEN` authenticates to the Coder deployment as this workspace, so it is written to `/etc/coder-agent.env` at mode `0600` and pulled in with `EnvironmentFile=`, rather than sitting inline in the unit. Mode on the unit file is not enough on its own: systemd reports `Environment=` values to any local user through `systemctl show coder-agent`, while it never echoes the contents of an `EnvironmentFile`. systemd reads that file as root before dropping to `User=`, so the agent user needs no access to it.

## ARM (CAX) server types

`coder_agent.main` hardcodes `arch = "amd64"`, and the agent's init script downloads a binary for that architecture. Adding a `cax*` option to the `server_type` parameter without also changing `arch` to `arm64` produces a workspace that **builds successfully and never connects** — there is no build-time error to catch it.

To run on ARM (`cax11` is 2 vCPU / 4 GB and cheaper than `cx23`), either flip `arch` to `arm64` and offer only CAX types, or derive it from the selected type. Do not mix the two behind one parameter with a fixed `arch`. CAX types are EU-only, so they carry the same location coupling as CX.

## Dotfiles

The [`dotfiles` module](https://registry.coder.com/modules/coder/dotfiles) prompts each workspace owner for a repository URL and runs `coder dotfiles -y <url>` inside the workspace, which executes that repo's `install.sh`. Leave the prompt blank to skip it.

Set a default for everyone with `--var dotfiles_uri=https://github.com/k-kit/dotfiles.git`. This is deliberately *not* the full `scripts/cloud/hetzner-cloud-init.yaml` bootstrap: that flow creates its own user and provisions the whole machine at boot, which fights Coder's agent model. Use it for a plain Hetzner box, and this template for a Coder workspace.
