---
display_name: DigitalOcean Droplet (Linux)
description: Provision DigitalOcean Droplets as Coder workspaces, with a persistent /home volume
icon: /icon/do.png
maintainer_github: k-kit
tags: [vm, linux, digitalocean]
---

# Remote development on DigitalOcean Droplets

Provisions a DigitalOcean Droplet as a [Coder workspace](https://coder.com/docs/user-guides/workspace-management). Tracks the official [`digitalocean-linux`](https://github.com/coder/coder/tree/main/examples/templates/digitalocean-linux) example, with four changes: the API token can be passed as a template variable, the project ID is optional, image/size/region options are refreshed against the live DigitalOcean API, and the owner's Coder SSH key is installed for break-glass access.

## Architecture

- `digitalocean_droplet` — the workspace VM. Gated on `data.coder_workspace.me.start_count`, so **stopping the workspace destroys the droplet**.
- `digitalocean_volume` — persistent `/home/<owner>`, formatted `ext4` once by Terraform and attached at droplet creation.
- cloud-init (`cloud-config.yaml.tftpl`) — creates the user, mounts the volume by label, then starts the Coder agent as a systemd unit.

Anything outside `/home` is lost on stop. Bake tools into a custom image, or use a [startup script](https://registry.terraform.io/providers/coder/coder/latest/docs/resources/script), if you need them to persist.

## Prerequisites

- A DigitalOcean [personal access token](https://docs.digitalocean.com/reference/api/create-personal-access-token) with write scope.
- Optionally, a project ID (`doctl projects list`) to file workspaces under, and an SSH key ID (`doctl compute ssh-key list`).

## Authentication

Terraform runs on the Coder provisioner, not on your laptop, so `DIGITALOCEAN_TOKEN` has to be set **where `coder server` or `coder provisioner start` runs** — exporting it in the shell you run `coder templates push` from does nothing.

If that is awkward, pass the token as a template variable instead. It is then stored server-side with the template version:

```bash
coder templates push digitalocean-linux \
  -d scripts/coder/digitalocean-linux \
  --var do_token="$(fnox get digitalocean)"
```

`digitalocean` is this repo's fnox secret name for the DO token — see [`scripts/coder/README.md`](../README.md).

Note the two different variable names: the Terraform provider reads `DIGITALOCEAN_TOKEN`, while the `doctl` CLI reads `DIGITALOCEAN_ACCESS_TOKEN`.

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| Region | `ams3` | Only regions that support block-storage volumes are listed |
| Droplet size | `s-1vcpu-2gb` | `s-1vcpu-512mb-10gb` is cheapest but is not offered in every region and is too small for most language servers |
| Droplet image | `ubuntu-24-04-x64` | Ubuntu 26.04/22.04 and Debian 13 also offered |
| Home volume size | `20` GB | Over 100 GB requires a DigitalOcean support ticket |

Template variables (`--var`) rather than workspace parameters: `do_token`, `project_uuid`, `ssh_key_id`, `dotfiles_uri`.

Unlike upstream, `project_uuid` defaults to empty and the `digitalocean_project_resources` resource is skipped when it is — so `coder templates push` followed by a workspace build works with no `--var` at all.

## Persistent /home, and how you find out when it isn't

The volume is attached at droplet creation and mounted by filesystem label, so it is present by the time cloud-init's `mounts` module runs. The filesystem is created once by Terraform's `initial_filesystem_type` and never here, so no rebuild can `mkfs` over your files.

Mount options stay minimal on purpose: ext4 has no `uid`/`gid` options, and passing them makes the mount fail outright. Combined with `nofail`, that failure is silent — `/home` quietly stays on the ephemeral root disk and is lost when the workspace stops.

`coder_agent.main.startup_script` closes that gap. It runs `mountpoint -q /home/<owner>` and exits non-zero when the home directory is not on the volume, which Coder surfaces as a **failed startup script** in the UI — `coder-home-prepare` also warns, but only to a boot console nobody reads. The guard is deliberately `non-blocking` despite the provider recommending `blocking`, because the point is to stay reachable so you can investigate.

## The agent token

`CODER_AGENT_TOKEN` authenticates to the Coder deployment as this workspace, so it is written to `/etc/coder-agent.env` at mode `0600` and pulled in with `EnvironmentFile=`, rather than sitting inline in the unit. Mode on the unit file is not enough on its own: systemd reports `Environment=` values to any local user through `systemctl show coder-agent`, while it never echoes the contents of an `EnvironmentFile`. systemd reads that file as root before dropping to `User=`, so the agent user needs no access to it.

## ARM droplets

`coder_agent.main` hardcodes `arch = "amd64"` and every size option above is x86. If you add an ARM droplet size, flip `arch` to `arm64` at the same time — a mismatch produces a workspace that builds successfully and never connects.

## Dotfiles

The [`dotfiles` module](https://registry.coder.com/modules/coder/dotfiles) prompts each workspace owner for a repository URL and runs `coder dotfiles -y <url>` inside the workspace. Leave the prompt blank to skip it, or set a default for everyone with `--var dotfiles_uri=https://github.com/k-kit/dotfiles.git`.
