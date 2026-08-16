# Dev-box images (Packer)

Bakes an image containing everything `scripts/cloud/setup.sh` installs, so a new dev box boots ready instead of provisioning for minutes. Targets **DigitalOcean**, **Hetzner Cloud** and **AWS** from a single template.

## Build

```
packer init .
DIGITALOCEAN_TOKEN=<token> packer build -only='dev-box.digitalocean.dev_box' .
HCLOUD_TOKEN=<token>       packer build -only='dev-box.hcloud.dev_box' .
AWS_PROFILE=<profile>      packer build -only='dev-box.amazon-ebs.dev_box' .
```

A bare `packer build .` builds **all three** and needs every credential.

AWS has no token variable on purpose — the `amazon-ebs` builder reads the standard credential chain (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `~/.aws/credentials`, instance profile). Adding one would create a second, worse way to authenticate alongside the one every other AWS tool already uses.

`fnox get digitalocean` / `fnox get HERTZNER` substitute for `<token>` **only on a machine that has both the `fnox` CLI and the age key** at `~/.config/fnox/age.txt`. Nothing in this repo provisions either: `fnox` comes from `brew install fnox`, and the age key is copied across by hand — `secrets-init` only takes `bws|project` and has no fnox mode, so it cannot create it. `deploy.sh`'s fnox component links the config and then just tells you which of the two is missing.

All three `-only` addresses are verified against the live APIs — each selects its own builder and reaches an authentication error, while the other two stay unselected.

A misspelled `-only` address is safe with `build` (Packer v1.16.0: "Error: No builds to run", exit 1, and it helpfully lists the valid names) but **not** with `validate`, which downgrades it to a warning and exits 0. So never use `packer validate -only=...` to check that an address resolves — it will pass on anything.

Overrides: `-var dotfiles_branch=some-branch`, `-var snapshot_name=my-image`, and the per-provider `do_*` / `hcloud_*` / `aws_*` vars. Default image name is `dev-box-<branch>-<YYYYMMDD-HHMMSS>`.

Packer creates a throwaway server, provisions it, images it, and destroys it. The build sizes (`s-2vcpu-4gb`, `cx23`, `t3.medium`) are all larger than the runtime default, because apt and uv dominate the build and it is billed by the second.

## One template, all three providers, on purpose

All three sources share **one** provisioner block, so there is no live per-provider copy that can drift. (`archive/digitalocean-superseded/` is the earlier DO-only template, kept only as history — it is not built and should not be edited. Delete it once you are happy with the combined one.) This repo has been bitten three times by two-implementations-drifted bugs (statusline, `hz` default type, Coder template parity); a second copy of provisioning logic is the known failure mode here, so the structure forbids it rather than a comment asking you not to.

The same reasoning one level down: `scripts/cloud/create-user.sh` and `scripts/cloud/setup.sh` are uploaded and executed **as-is**, not reimplemented. Change provisioning by editing those and rebuilding — nothing in the template moves in step.

`create-user.sh` runs first because `setup.sh` aborts with "User not found" otherwise. `CLOUD_MODE=vps` is forced — auto-detection keys off `/workspace`, and a build server must never be taken for a RunPod container.

`setup.sh`'s HARD tier (zsh/vim/tmux) aborts the build on failure, which is what you want — a broken image should never be snapshotted. The SOFT tier warns and continues, so a transient failure in an optional package yields an image that is merely incomplete, not absent. Read the build log rather than assuming a green exit means everything installed.

## Hetzner: location and server type are coupled

There is **no Hetzner server type valid in every location** — Hetzner partitions the families by region:

| Family | Valid locations |
|---|---|
| `cx23` / `cx33` / `cx43` | EU only — `fsn1`, `nbg1`, `hel1` |
| `cpx11` / `cpx21` / `cpx31` | US only — `ash`, `hil` |
| `cpx22` / `cpx32` | EU and Singapore |

So "pick a universal default" is not an available move, and an earlier attempt to do it elsewhere in this repo chose `cpx21` — which is US-only — and broke the default pair. The default here is `fsn1` + `cx23`, the same pair the Coder Hetzner template uses. **Change one and you may have to change the other**; a bad pair fails at build time, not at validate time.

x86 only. An ARM (`cax*`) type would build fine and produce an image the amd64 tooling cannot run.

## AWS: two things that differ from the other two

**It connects as `ubuntu`, not `root`.** Ubuntu's official AMIs disable root SSH, while DO and Hetzner hand you root. `create-user.sh` and `setup.sh` both abort unless they are root, so rather than forking the provisioners by connection user, every shell provisioner runs through `local.sudo_exec` (`{{ .Vars }} sudo -E bash '{{ .Path }}'`) — a no-op escalation when already root. That is what keeps one provisioner set covering all three providers.

**An AMI is region-local.** A DO snapshot is usable from any region and a Hetzner snapshot from any location, but an AMI built in `eu-central-1` exists only there until it is explicitly copied. Build per region, or `aws ec2 copy-image` afterwards.

The base AMI is pinned by owner (`099720109477`, Canonical) *and* name glob. The owner filter is the load-bearing half — without it the name glob would match anyone's image called `ubuntu-noble-*`. The glob wildcards the volume-type segment (`hvm-ssd` vs `hvm-ssd-gp3`) because it differs by region.

## No secrets are baked in

`TAILSCALE_AUTH_KEY`, `BWS_TOKEN` and `GITHUB_TOKEN` are deliberately **not** passed to the build. A snapshot is readable by anything that can launch a server from it, so a secret in the image is a secret published to the account. `setup.sh`'s `fnox_get()` is a quiet no-op without the age key, which is the desired behaviour here — secrets arrive at boot via cloud-init user-data or Coder, never in the image.

This is the same reasoning as the existing rule that a PAT must never travel via Hetzner cloud-init user-data, applied one layer earlier.

## What gets scrubbed before snapshotting

`create-user.sh` generates two things that are correct for a one-off box and wrong for an image, so the final provisioner removes them:

- **SSH host keys** (`/etc/ssh/ssh_host_*`) — otherwise every server from this image shares one host identity, and any of them can transparently impersonate another. cloud-init regenerates them on first boot.
- **The user's private key** (`~/.ssh/id_ed25519`) — otherwise every server ships the same private key.

- **Packer's own build keypair** (`/root/.ssh/authorized_keys`, `/home/ubuntu/.ssh/authorized_keys`) — its private half dies with the build, so this is hygiene rather than a hole, but a stale build key in a shared image ages badly.

`/home/<user>/.ssh/authorized_keys` is deliberately **kept**. Those are public keys fetched from `github.com/<user>.keys`, and baking them is the point — it is what makes the image directly loginable, and it is also why removing the build keys above cannot lock you out.

Also scrubbed: `machine-id` (truncated, not deleted — that is what makes systemd generate a fresh one, with the dbus symlink re-pointed rather than left dangling), cloud-init state, apt lists, logs, and shell history.

## Verification status

`packer init`, `packer validate` and `packer fmt -check` pass for all three providers.

Each `-only` address was pinned by running a real `packer build` with deliberately-invalid credentials. Each selected exactly one builder and reached live provider auth:

| Address | Reached |
|---|---|
| `dev-box.digitalocean.dev_box` | `POST https://api.digitalocean.com/v2/account/keys: 401 Unable to authenticate you` |
| `dev-box.hcloud.dev_box` | `Could not fetch server type 'cx23': the token you have provided is invalid` |
| `dev-box.amazon-ebs.dev_box` | `STS GetCallerIdentity … 403 InvalidClientTokenId` |

Reaching provider auth is the proof the address resolved. An earlier check that only ran `packer validate -only=...` was **worthless** — validate ignores `-only` and exits 0 on an address matching nothing.

**No complete `packer build` has ever run.** No credential for any of the three is available on this box: `fnox` is not installed and `~/.config/fnox/age.txt` does not exist. The first real build is the first real test; read its log rather than trusting the green validate.

AWS specifically fails at STS *before* the AMI lookup, so `aws_ami_name_filter` and `aws_ami_owner` have **not** been exercised against the API — a wrong glob would surface as "no AMI found" on the first credentialed run, not now.

Two provisioner behaviours `validate` does not check were verified separately, by reproducing them against the local docker builder:

- **The `file` provisioner's directory semantics.** `source = ".../cloud"` with `destination = "/tmp/cloud"` produces a flat `/tmp/cloud/setup.sh`, which is what the later provisioners assume. It does *not* nest into `/tmp/cloud/cloud/`.
- **`inline_shebang` must be bash.** Packer's default is `/bin/sh -e`, `/bin/sh` on Ubuntu is dash, and `set -o pipefail` is an illegal option in dash — so the default would have killed every shell provisioner on its first line. All three set `inline_shebang = "/bin/bash -e"` explicitly.

Still unverified until a real build: every provider's API/auth, the AWS base-AMI filter, `sudo -E` escalation on a real AWS instance, `cloud-init status --wait` on a real server, whether `setup.sh` completes end-to-end on a fresh Ubuntu 24.04, and whether a server launched from the resulting image regenerates its host keys.

## Using the image

- **DigitalOcean** — `doctl compute image list-user` for the ID. Point the Coder DO template's `droplet_image` at it, or `doctl compute droplet create --image <id>`.
- **Hetzner** — `hcloud image list --type snapshot`. Point the Coder Hetzner template's `image` at the ID, or `hcloud server create --image <id>`.
- **AWS** — `aws ec2 describe-images --owners self`. `aws ec2 run-instances --image-id <ami>`, remembering it is usable only in the region it was built in.

## Rebuild cadence

The image goes stale as `setup.sh` and upstream packages move. It is a cache, not a source of truth — rebuild when `scripts/cloud/` changes meaningfully, or when the security updates baked in are old enough to matter.
