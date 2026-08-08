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
| Server type | `cx23` | 2 vCPU / 4 GB. `cpx11` is cheaper; **CX types exist only in the EU locations** — pick a CPX type for `ash`/`hil`/`sin` |
| Image | `ubuntu-24.04` | Ubuntu 22.04 and Debian 12 also offered |
| Home volume size | `20` GB | Hetzner volumes start at 10 GB |

Template variables (`--var`) rather than workspace parameters: `hcloud_token`, `ssh_keys`, `dotfiles_uri`.

## ARM (CAX) server types

`coder_agent.main` hardcodes `arch = "amd64"`, and the agent's init script downloads a binary for that architecture. Adding a `cax*` option to the `server_type` parameter without also changing `arch` to `arm64` produces a workspace that **builds successfully and never connects** — there is no build-time error to catch it.

To run on ARM (`cax11` is 2 vCPU / 4 GB and cheaper than `cx23`), either flip `arch` to `arm64` and offer only CAX types, or derive it from the selected type. Do not mix the two behind one parameter with a fixed `arch`.

## Dotfiles

The [`dotfiles` module](https://registry.coder.com/modules/coder/dotfiles) prompts each workspace owner for a repository URL and runs `coder dotfiles -y <url>` inside the workspace, which executes that repo's `install.sh`. Leave the prompt blank to skip it.

Set a default for everyone with `--var dotfiles_uri=https://github.com/k-kit/dotfiles.git`. This is deliberately *not* the full `scripts/cloud/hetzner-cloud-init.yaml` bootstrap: that flow creates its own user and provisions the whole machine at boot, which fights Coder's agent model. Use it for a plain Hetzner box, and this template for a Coder workspace.
