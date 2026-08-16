// Bake a dev-box image on DigitalOcean, Hetzner Cloud and/or AWS: everything
// scripts/cloud/setup.sh does at boot, done once at image-build time instead.
//
// ONE build block drives ALL THREE providers, deliberately. The provisioners are
// written once and shared; there is no per-provider copy to drift. A second copy
// of provisioning logic is how the statusline / hz-default-type / coder-parity
// bugs happened, three times.
//
// The provisioners run scripts/cloud/{create-user,setup}.sh VERBATIM — uploaded,
// not reimplemented. If setup.sh changes, this image just needs a rebuild;
// nothing here needs editing.
//
//   packer init .
//   packer validate .
//   DIGITALOCEAN_TOKEN=... packer build -only='dev-box.digitalocean.dev_box' .
//   HCLOUD_TOKEN=...       packer build -only='dev-box.hcloud.dev_box' .
//   (standard AWS creds)   packer build -only='dev-box.amazon-ebs.dev_box' .
//
// A bare `packer build .` builds ALL THREE and needs every credential.
// `packer validate -only=<anything>` exits 0 even when the address matches no
// build — only `packer build` actually errors on a typo. Never check an -only
// address with validate.
//
// NO SECRETS ARE BAKED IN. TAILSCALE_AUTH_KEY / BWS_TOKEN / GITHUB_TOKEN are
// deliberately NOT passed to the build — a snapshot is readable by anything that
// can create a server from it. setup.sh's fnox_get() is a quiet no-op without
// the age key, which is exactly what we want here; secrets arrive at boot
// (cloud-init user-data / Coder), never in the image.

packer {
  required_plugins {
    digitalocean = {
      version = ">= 1.4.1"
      source  = "github.com/digitalocean/digitalocean"
    }
    hcloud = {
      version = ">= 1.6.0"
      source  = "github.com/hetznercloud/hcloud"
    }
    amazon = {
      version = ">= 1.3.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

// ── Shared ────────────────────────────────────────────────────────────────────

variable "username" {
  type        = string
  default     = "k-kit"
  description = "Non-root user create-user.sh provisions. Must match DOTFILES_USERNAME elsewhere."

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_-]{0,31}$", var.username))
    error_message = "Username must be a Linux account name: lowercase letters, digits, underscore, or hyphen."
  }
}

variable "dotfiles_branch" {
  type        = string
  default     = "main"
  description = "Dotfiles branch setup.sh checks out. setup.sh requires this explicitly; there is no implicit default."

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$", var.dotfiles_branch))
    error_message = "Dotfiles_branch must be a shell-safe Git ref containing only letters, digits, dot, underscore, slash, or hyphen."
  }
}

variable "snapshot_name" {
  type        = string
  default     = ""
  description = "Snapshot name. Empty means dev-box-<branch>-<YYYYMMDD-HHMMSS>."
}

locals {
  snapshot_name = var.snapshot_name != "" ? var.snapshot_name : "dev-box-${var.dotfiles_branch}-${legacy_isotime("20060102-150405")}"

  // DO and Hetzner connect as root; AWS connects as `ubuntu` because Ubuntu's
  // official AMIs disable root SSH. create-user.sh and setup.sh both abort
  // unless they are root, so every shell provisioner escalates — `sudo -E` is a
  // harmless no-op when already root, which keeps ONE provisioner set covering
  // all three providers instead of forking them by connection user.
  // {{ }} is Go template syntax, evaluated by Packer, not HCL interpolation.
  sudo_exec = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E bash '{{ .Path }}'"
}

// ── DigitalOcean ──────────────────────────────────────────────────────────────

variable "do_token" {
  type        = string
  sensitive   = true
  default     = env("DIGITALOCEAN_TOKEN")
  description = "DigitalOcean API token. Defaults to $DIGITALOCEAN_TOKEN; get it with `fnox get digitalocean`."
}

variable "do_region" {
  type        = string
  default     = "ams3"
  description = "DO build region. Matches the Coder DO template default. A snapshot is usable from any region once distributed."
}

variable "do_size" {
  type        = string
  default     = "s-2vcpu-4gb"
  description = "Droplet size used only for the BUILD. Bigger than the runtime default because apt+uv are the slow part; it is destroyed afterwards."
}

variable "do_image" {
  type        = string
  default     = "ubuntu-24-04-x64"
  description = "DigitalOcean base image slug to provision on top of."
}

source "digitalocean" "dev_box" {
  api_token     = var.do_token
  region        = var.do_region
  size          = var.do_size
  image         = var.do_image
  ssh_username  = "root"
  snapshot_name = local.snapshot_name

  droplet_name = "packer-build-${local.snapshot_name}"
  tags         = ["packer", "dev-box"]
}

// ── Hetzner Cloud ─────────────────────────────────────────────────────────────

variable "hcloud_token" {
  type        = string
  sensitive   = true
  default     = env("HCLOUD_TOKEN")
  description = "Hetzner Cloud API token. Defaults to $HCLOUD_TOKEN; get it with `fnox get HERTZNER`."
}

// hcloud_location and hcloud_server_type are COUPLED — changing one may require
// changing the other. Hetzner partitions server families by region and there is
// no type valid in every location: cx* is EU-only (fsn1/nbg1/hel1), cpx1*/cpx2*/
// cpx3* is US-only (ash/hil), cpx*2 is EU+Singapore. An earlier attempt to pick a
// "universal default" chose cpx21, which is US-only, and broke the default pair.
// fsn1 + cx23 is the pair the Coder Hetzner template also defaults to.
variable "hcloud_location" {
  type        = string
  default     = "fsn1"
  description = "Hetzner build location. Coupled to hcloud_server_type — see the comment above."
}

variable "hcloud_server_type" {
  type        = string
  default     = "cx23"
  description = "Hetzner server type for the BUILD only; destroyed afterwards. x86 — an ARM (cax*) type would produce an image the amd64 tooling cannot run."
}

variable "hcloud_image" {
  type        = string
  default     = "ubuntu-24.04"
  description = "Hetzner base image name to provision on top of."
}

source "hcloud" "dev_box" {
  token         = var.hcloud_token
  location      = var.hcloud_location
  server_type   = var.hcloud_server_type
  image         = var.hcloud_image
  ssh_username  = "root"
  snapshot_name = local.snapshot_name

  server_name     = "packer-build-${local.snapshot_name}"
  snapshot_labels = { packer = "true", role = "dev-box" }
}

// ── AWS ───────────────────────────────────────────────────────────────────────

// No token variable here on purpose. The amazon builder reads the standard AWS
// credential chain (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_PROFILE,
// ~/.aws/credentials, instance profile) — inventing a var would create a second,
// worse way to authenticate alongside the one every other AWS tool already uses.

variable "aws_region" {
  type        = string
  default     = "eu-central-1"
  description = "AWS build region. An AMI is region-local — it must be copied to be usable elsewhere, unlike a DO snapshot."
}

variable "aws_instance_type" {
  type        = string
  default     = "t3.medium"
  description = "Build instance type (2 vCPU / 4 GiB, matching the DO build size). Destroyed after the snapshot."
}

// Canonical's owner ID. Hardcoded rather than looked up: `owners` is what stops
// the name filter matching someone else's image called ubuntu-noble-*.
variable "aws_ami_owner" {
  type        = string
  default     = "099720109477"
  description = "AMI owner filter — Canonical. Do not widen this."
}

variable "aws_ami_name_filter" {
  type        = string
  default     = "ubuntu/images/*ubuntu-noble-24.04-amd64-server-*"
  description = "Base AMI name glob. Wildcarded on the volume-type segment (hvm-ssd vs hvm-ssd-gp3) because it differs by region."
}

source "amazon-ebs" "dev_box" {
  region        = var.aws_region
  instance_type = var.aws_instance_type
  ami_name      = local.snapshot_name

  // most_recent is required: the filter matches every daily build of noble.
  source_ami_filter {
    filters = {
      name                = var.aws_ami_name_filter
      root-device-type    = "ebs"
      virtualization-type = "hvm"
      architecture        = "x86_64"
    }
    owners      = [var.aws_ami_owner]
    most_recent = true
  }

  // Ubuntu's AMIs disable root SSH — this connects as `ubuntu` and the
  // provisioners escalate. See the execute_command note in the build block.
  ssh_username = "ubuntu"

  tags = {
    Name   = local.snapshot_name
    packer = "true"
    role   = "dev-box"
  }
}

// ── Provisioners: written once, run on every source above ─────────────────────

build {
  name = "dev-box"
  sources = [
    "source.digitalocean.dev_box",
    "source.hcloud.dev_box",
    "source.amazon-ebs.dev_box",
  ]

  // Upload the real scripts rather than inlining a copy of them. Verified against
  // the docker builder: this lands them flat at /tmp/cloud/setup.sh, NOT nested
  // at /tmp/cloud/cloud/setup.sh.
  provisioner "file" {
    source      = "${path.root}/../cloud"
    destination = "/tmp/cloud"
  }

  // create-user.sh is a prerequisite: setup.sh aborts with "User not found".
  // CLOUD_MODE=vps is forced — auto-detection keys off /workspace, and a build
  // server must never be mistaken for a RunPod container.
  provisioner "shell" {
    // /bin/sh on Ubuntu is dash, and Packer's default inline_shebang is
    // "/bin/sh -e" — `set -o pipefail` is an illegal option there and kills the
    // script on its first line. Verified against the docker builder, not assumed.
    //
    // execute_command now names `bash` explicitly, so the shebang is bypassed and
    // these inline_shebang lines are belt-and-braces: they only matter if the
    // execute_command is ever removed. Note `sudo -E bash '<path>'` also drops the
    // shebang's `-e`; every inline block opens with `set -euo pipefail` for that.
    inline_shebang  = "/bin/bash -e"
    execute_command = local.sudo_exec
    environment_vars = [
      "USERNAME=${var.username}",
      "CLOUD_MODE=vps",
      "DEBIAN_FRONTEND=noninteractive",
    ]
    inline = [
      "set -euo pipefail",
      "chmod +x /tmp/cloud/*.sh",
      "cloud-init status --wait || true",
      "/tmp/cloud/create-user.sh",
    ]
  }

  provisioner "shell" {
    inline_shebang  = "/bin/bash -e"
    execute_command = local.sudo_exec
    environment_vars = [
      "USERNAME=${var.username}",
      "CLOUD_MODE=vps",
      "DEBIAN_FRONTEND=noninteractive",
    ]
    // setup.sh takes the branch positionally and is tiered: HARD failures abort
    // the build (correct — a broken image should not be snapshotted), SOFT ones warn.
    inline = [
      "set -euo pipefail",
      "/tmp/cloud/setup.sh ${var.dotfiles_branch}",
    ]
  }

  // Scrub anything that must be unique per machine. Without this, every server
  // launched from the snapshot shares one SSH host identity and one user private
  // key — create-user.sh generates both, which is right for a one-off box and
  // wrong for an image.
  provisioner "shell" {
    inline_shebang  = "/bin/bash -e"
    execute_command = local.sudo_exec
    inline = [
      "set -euo pipefail",
      "echo '── Scrubbing per-machine identity ──'",

      // Host keys: regenerated on first boot by cloud-init's ssh module.
      "rm -f /etc/ssh/ssh_host_*",

      // The user keypair create-user.sh generates. authorized_keys is left alone:
      // those are PUBLIC keys fetched from github.com/<user>.keys, and baking them
      // is the point — it is what makes the image directly loginable.
      "rm -f /home/${var.username}/.ssh/id_ed25519 /home/${var.username}/.ssh/id_ed25519.pub",

      // Packer's own throwaway keypair, injected into whichever account it
      // connected as (root on DO/Hetzner, ubuntu on AWS). Its private half is
      // destroyed with the build, so this is hygiene rather than a hole — but a
      // stale build key in a shared image ages badly. Safe to remove because
      // login does NOT depend on it: create-user.sh bakes the GitHub public keys
      // into ${var.username}, independent of cloud-init re-adding anything.
      "rm -f /root/.ssh/authorized_keys /home/ubuntu/.ssh/authorized_keys",

      // Any secret that leaked in despite the no-secrets rule above.
      "rm -f /home/${var.username}/.config/fnox/age.txt",
      "rm -rf /root/.config/fnox",

      // Re-arm cloud-init so the snapshot behaves like a fresh image.
      "cloud-init clean --logs --seed || true",

      // Truncating (not deleting) /etc/machine-id is what tells systemd to
      // generate a fresh one at first boot. /var/lib/dbus/machine-id is normally
      // a symlink to it on Ubuntu 24.04, so it is re-pointed rather than left
      // dangling — removing it outright leaves dbus without an id on first boot.
      "truncate -s 0 /etc/machine-id",
      "ln -sf /etc/machine-id /var/lib/dbus/machine-id",

      "rm -rf /tmp/cloud",
      "apt-get clean",
      "rm -rf /var/lib/apt/lists/*",
      "find /var/log -type f -exec truncate -s 0 {} + || true",
      "rm -f /root/.bash_history /home/${var.username}/.bash_history /home/${var.username}/.zsh_history",
      "echo '✓ scrubbed'",
    ]
  }

  post-processor "manifest" {
    output     = "${path.root}/manifest.json"
    strip_path = true
  }
}
