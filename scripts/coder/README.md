# Coder templates

Starter [Coder](https://coder.com) templates that provision cloud VMs as workspaces, for either a self-hosted Coder deployment or Coder Cloud. Opt-in and self-contained: nothing here runs unless you push a template, and the existing `curl | bash` flow in [`scripts/cloud/`](../cloud/README.md) is untouched.

| Template | Provider | Default box |
|---|---|---|
| [`hetzner-linux/`](hetzner-linux/README.md) | `hetznercloud/hcloud` | `cpx21` (3 vCPU / 4 GB) in `fsn1`, Ubuntu 24.04 |
| [`digitalocean-linux/`](digitalocean-linux/README.md) | `digitalocean/digitalocean` | `s-1vcpu-2gb` in `ams3`, Ubuntu 24.04 |

Both follow the same shape as Coder's own [`digitalocean-linux`](https://github.com/coder/coder/tree/main/examples/templates/digitalocean-linux) example — an ephemeral VM plus a persistent volume mounted at `/home/<owner>` — so stopping a workspace destroys the VM and keeps your files. Coder ships no Hetzner example; that one is written to match.

## Which of these do I want?

| I want... | Use |
|---|---|
| A dev box I SSH into, set up once, and keep | [`scripts/cloud/`](../cloud/README.md) — `hetzner-cloud-init.yaml` + `hz` |
| Workspaces I create and destroy on demand, in a browser or IDE | these templates |
| A GPU pod | [`scripts/cloud/`](../cloud/README.md) — RunPod mode |

The two paths do not share code and do not conflict. `scripts/cloud/` provisions a whole personal machine at boot; Coder provisions a workspace that its agent manages. Personalization here goes through the `dotfiles` module (`coder dotfiles -y <url>` → this repo's `install.sh`), not the boot-time bootstrap.

## Setup

```bash
# The Coder CLI (self-hosted server or Coder Cloud)
curl -L https://coder.com/install.sh | sh
coder login https://coder.example.com
```

## Where the cloud token has to live

Terraform runs on the **Coder provisioner**, not on your laptop. Exporting `HCLOUD_TOKEN` in the shell you run `coder templates push` from does nothing — the push only uploads a tarball. Two options:

**1. Provisioner environment** (upstream's assumption, best for self-hosted). Export the token wherever `coder server` or `coder provisioner start` runs:

```bash
HCLOUD_TOKEN="$(fnox get HERTZNER)" coder provisioner start
DIGITALOCEAN_TOKEN="$(fnox get digitalocean)" coder provisioner start
```

**2. Template variable** (best when you do not control the provisioner's environment). The token is uploaded with the template version and stored server-side by Coder:

```bash
coder templates push hetzner-linux \
  -d scripts/coder/hetzner-linux \
  --var hcloud_token="$(fnox get HERTZNER)"

coder templates push digitalocean-linux \
  -d scripts/coder/digitalocean-linux \
  --var do_token="$(fnox get digitalocean)"
```

Both templates leave the variable empty by default and fall back to the environment variable, so option 1 needs no `--var` at all.

`HERTZNER` and `digitalocean` are [fnox](../../config/fnox.toml) secret names — `fnox get` decrypts one to stdout. Never paste the value into a file in this repo; it is public.

## Pushing a template

```bash
# From the repo root. First push creates the template; later pushes add a version.
coder templates push hetzner-linux -d scripts/coder/hetzner-linux -m "initial"
coder templates push digitalocean-linux -d scripts/coder/digitalocean-linux -m "initial"

coder create --template hetzner-linux my-workspace
```

Useful flags (`coder templates push --help`): `--var`/`--variables-file` for Terraform variables, `--name` to label the version, `-y` to skip confirmation, `--activate=false` to upload without making it the default.

Neither template directory ships a `.terraform.lock.hcl`, so the push warns about unpinned provider versions. Either accept the warning with `--ignore-lockfile`, or generate a lockfile once and commit it:

```bash
cd scripts/coder/hetzner-linux && terraform init   # writes .terraform.lock.hcl
```

A lockfile is the better answer for anything you actually depend on — it pins provider checksums so a template version rebuilds identically.

## Idle shutdown

Use Coder's own autostop rather than an in-VM watchdog. It is a template setting, so it applies to every workspace built from the template, and it works with the ephemeral-VM design above: on autostop the VM is destroyed and only the volume keeps costing money.

```bash
# Stop workspaces 2 hours after they start unless the user extends
coder templates edit hetzner-linux --default-ttl 2h

# Require a weekly restart, so long-lived workspaces pick up template updates
coder templates edit hetzner-linux --autostop-requirement-weekdays sunday
```

Related flags: `--allow-user-autostop` (default true) lets users override the TTL per workspace, `--autostop-reminder` sets the warning lead time, `--failure-ttl` cleans up after a failed start. Run `coder templates edit --help` for the full set — some are licensed features.

This repo's own `scripts/cloud/idle-shutdown-install.sh` watchdog is for plain VMs that nothing else supervises. Do not install it inside a Coder workspace; it would fight the agent.

## Editing these templates

They are meant as starting points. Two things to be careful about:

- **`coder_agent.arch` must match the VM's architecture.** Both templates hardcode `amd64` and offer only x86 machine types. Adding an ARM option (Hetzner `cax*`) without flipping `arch` to `arm64` produces a workspace that builds successfully and never connects — nothing catches it at plan time.
- **Never format the volume from cloud-init.** Terraform creates the filesystem once at volume-create time; the cloud-init scripts only mount it. A `mkfs` in the boot path would wipe `/home` on every rebuild.
- **In a `.tftpl`, only `$${` is an escape.** `$$var` renders literally as `$$var` — the shell's PID followed by text. Shell variables are written with a single `$`.
- **The machine-type default has to stay valid for every location.** Coder cannot validate one parameter against another, so a Hetzner `cx*` default silently pairs with `ash`/`hil`/`sin` to make a combination that only fails at apply time. That is why the default is `cpx21` and not the cheaper EU-only `cx23`.

## Testing

```bash
uv run tests/test_coder_templates.py     # 68 assertions, hermetic
uv run tests/mutate_coder_templates.py   # proves the suite can fail
```

There is no Terraform or OpenTofu on this machine, so `terraform validate` is not available. `tests/test_coder_templates.py` is the substitute: it renders each `.tftpl` the way `templatefile()` would, checks the `main.tf` ↔ template variable map in both directions, parses the result as cloud-init YAML, shellchecks the scripts embedded in it, and asserts the agent token never lands anywhere world-readable. It also pins doc/code parity for the default machine type in both READMEs — the `hz` lesson, where the code default drifted from the documented one and billed for a bigger box.

`tests/mutate_coder_templates.py` breaks each of those properties in turn — `cx23`, `blocking`, `0644`, `cp -r` — using values the tools would genuinely accept, and fails if the suite stays green. An illegal value would only prove that a membership check works. It mutates a staged copy under `$TMPDIR`, never your working tree. Both templates are mutated, not just Hetzner: the suite asserts the same properties for each, so mutating one half would leave the other half's assertions unproven.

If you do have Terraform or OpenTofu, these are still worth running before a push:

```bash
terraform fmt -recursive scripts/coder
cd scripts/coder/hetzner-linux && terraform init -backend=false && terraform validate
```
