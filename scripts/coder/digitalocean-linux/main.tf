terraform {
  required_providers {
    coder = {
      source = "coder/coder"
    }
    digitalocean = {
      source = "digitalocean/digitalocean"
    }
  }
}

provider "coder" {}

# DigitalOcean personal access token.
#
# Leave unset to authenticate the provisioner from the DIGITALOCEAN_TOKEN
# environment variable (the upstream Coder convention -- export it wherever
# `coder server` or `coder provisioner start` runs). Set it as a template
# variable instead if you want the token to travel with the template version:
#
#   coder templates push digitalocean-linux --var do_token="$(fnox get digitalocean)"
#
# Note that a template variable is stored server-side by Coder.
variable "do_token" {
  type        = string
  description = "DigitalOcean API token. Leave empty to use the DIGITALOCEAN_TOKEN environment variable of the provisioner."
  sensitive   = true
  default     = ""
}

# Optional, unlike the upstream example: leaving it empty means workspaces land
# in the account's default project instead of failing the build for want of a
# --var. Find yours with `doctl projects list`.
variable "project_uuid" {
  type        = string
  description = "DigitalOcean project ID to file workspace resources under (`doctl projects list`). Empty uses the default project."
  sensitive   = true
  default     = ""

  validation {
    # UUIDv4 string length, or empty to opt out.
    condition     = var.project_uuid == "" || length(var.project_uuid) == 36
    error_message = "Invalid DigitalOcean project ID: expected a 36-character UUID or an empty string."
  }
}

# Optional. Coder reaches the workspace through the agent, not over port 22 --
# this is the break-glass path, and is what stops DigitalOcean emailing a root
# password on images that require a key. `doctl compute ssh-key list`.
variable "ssh_key_id" {
  type        = number
  description = "DigitalOcean SSH key ID to inject at droplet creation. 0 for none."
  sensitive   = true
  default     = 0

  validation {
    condition     = var.ssh_key_id >= 0
    error_message = "Invalid DigitalOcean SSH key ID, a non-negative number is required."
  }
}

variable "dotfiles_uri" {
  type        = string
  description = "Default dotfiles repository offered to workspace owners. Empty means no default -- the user is still prompted and can leave it blank to skip."
  default     = ""
}

provider "digitalocean" {
  # Falls back to $DIGITALOCEAN_TOKEN when the variable is empty.
  token = var.do_token != "" ? var.do_token : null
}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

locals {
  username = lower(data.coder_workspace_owner.me.name)
}

data "coder_parameter" "region" {
  name         = "region"
  display_name = "Region"
  description  = "Where the workspace is created. Only regions that support block-storage volumes are listed."
  icon         = "/emojis/1f30e.png"
  type         = "string"
  default      = "ams3"
  mutable      = false

  option {
    name  = "Australia (Sydney)"
    value = "syd1"
    icon  = "/emojis/1f1e6-1f1fa.png"
  }
  option {
    name  = "Canada (Toronto)"
    value = "tor1"
    icon  = "/emojis/1f1e8-1f1e6.png"
  }
  option {
    name  = "Germany (Frankfurt)"
    value = "fra1"
    icon  = "/emojis/1f1e9-1f1ea.png"
  }
  option {
    name  = "India (Bangalore)"
    value = "blr1"
    icon  = "/emojis/1f1ee-1f1f3.png"
  }
  option {
    name  = "Netherlands (Amsterdam)"
    value = "ams3"
    icon  = "/emojis/1f1f3-1f1f1.png"
  }
  option {
    name  = "Singapore"
    value = "sgp1"
    icon  = "/emojis/1f1f8-1f1ec.png"
  }
  option {
    name  = "United Kingdom (London)"
    value = "lon1"
    icon  = "/emojis/1f1ec-1f1e7.png"
  }
  option {
    name  = "United States (California - 3)"
    value = "sfo3"
    icon  = "/emojis/1f1fa-1f1f8.png"
  }
  option {
    name  = "United States (New York - 3)"
    value = "nyc3"
    icon  = "/emojis/1f1fa-1f1f8.png"
  }
}

# x86 droplets only, matching coder_agent.arch = "amd64" below.
data "coder_parameter" "droplet_size" {
  name         = "droplet_size"
  display_name = "Droplet size"
  description  = "Shared-CPU droplet size. The 512 MB tier is not offered in every region and is too small for most language servers."
  icon         = "/icon/memory.svg"
  type         = "string"
  default      = "s-1vcpu-2gb"
  mutable      = false

  option {
    name  = "1 vCPU, 512 MB RAM (cheapest, limited regions)"
    value = "s-1vcpu-512mb-10gb"
  }
  option {
    name  = "1 vCPU, 1 GB RAM"
    value = "s-1vcpu-1gb"
  }
  option {
    name  = "1 vCPU, 2 GB RAM"
    value = "s-1vcpu-2gb"
  }
  option {
    name  = "2 vCPU, 2 GB RAM"
    value = "s-2vcpu-2gb"
  }
  option {
    name  = "2 vCPU, 4 GB RAM"
    value = "s-2vcpu-4gb"
  }
  option {
    name  = "4 vCPU, 8 GB RAM"
    value = "s-4vcpu-8gb"
  }
}

data "coder_parameter" "droplet_image" {
  name         = "droplet_image"
  display_name = "Droplet image"
  description  = "Base image for the workspace droplet."
  icon         = "/icon/ubuntu.svg"
  type         = "string"
  default      = "ubuntu-24-04-x64"
  mutable      = false

  option {
    name  = "Ubuntu 26.04 (LTS)"
    value = "ubuntu-26-04-x64"
    icon  = "/icon/ubuntu.svg"
  }
  option {
    name  = "Ubuntu 24.04 (LTS)"
    value = "ubuntu-24-04-x64"
    icon  = "/icon/ubuntu.svg"
  }
  option {
    name  = "Ubuntu 22.04 (LTS)"
    value = "ubuntu-22-04-x64"
    icon  = "/icon/ubuntu.svg"
  }
  option {
    name  = "Debian 13"
    value = "debian-13-x64"
    icon  = "/icon/debian.svg"
  }
}

data "coder_parameter" "home_volume_size" {
  name         = "home_volume_size"
  display_name = "Home volume size"
  description  = "Size of the persistent /home volume, in GB."
  type         = "number"
  default      = "20"
  mutable      = false

  validation {
    min = 1
    max = 100 # Volumes larger than 100 GB require a DigitalOcean support ticket.
  }
}

resource "coder_agent" "main" {
  os   = "linux"
  arch = "amd64"

  # The home mount is `nofail`, so a failed mount never stops the boot -- the
  # workspace comes up looking healthy on the ephemeral root disk and everything
  # written to /home is thrown away at the next stop. coder-home-prepare warns
  # about this to the console, where nobody sees it; failing here turns it into a
  # visible red startup script in the UI.
  #
  # Deliberately non-blocking despite the provider recommending "blocking": the
  # whole point is to stay reachable so the volume can be investigated by hand.
  startup_script_behavior = "non-blocking"
  startup_script          = <<-EOT
    set -eu
    home="/home/${local.username}"
    if ! mountpoint -q "$home"; then
      echo "FATAL: $home is not mounted from the persistent volume." >&2
      echo "Anything written there is LOST when this workspace stops." >&2
      echo "Check: journalctl -u cloud-final, then /usr/local/sbin/coder-home-prepare" >&2
      exit 1
    fi
    echo "Persistent home OK: $home"
  EOT

  metadata {
    key          = "cpu"
    display_name = "CPU Usage"
    interval     = 5
    timeout      = 5
    script       = "coder stat cpu"
  }
  metadata {
    key          = "memory"
    display_name = "Memory Usage"
    interval     = 5
    timeout      = 5
    script       = "coder stat mem"
  }
  metadata {
    key          = "home"
    display_name = "Home Usage"
    interval     = 600 # every 10 minutes
    timeout      = 30  # df can take a while on large filesystems
    script       = "coder stat disk --path /home/${local.username}"
  }
}

# See https://registry.coder.com/modules/coder/code-server
module "code-server" {
  count    = data.coder_workspace.me.start_count
  source   = "registry.coder.com/coder/code-server/coder"
  version  = "~> 1.0"
  agent_id = coder_agent.main.id
  order    = 1
}

# See https://registry.coder.com/modules/coder/jetbrains
module "jetbrains" {
  count      = data.coder_workspace.me.start_count
  source     = "registry.coder.com/coder/jetbrains/coder"
  version    = "~> 1.0"
  agent_id   = coder_agent.main.id
  agent_name = "main"
  folder     = "/home/${local.username}"
}

# Optional personalization -- see the note in ../hetzner-linux/main.tf.
# https://registry.coder.com/modules/coder/dotfiles
module "dotfiles" {
  count                = data.coder_workspace.me.start_count
  source               = "registry.coder.com/coder/dotfiles/coder"
  version              = "~> 1.4"
  agent_id             = coder_agent.main.id
  default_dotfiles_uri = var.dotfiles_uri
}

resource "digitalocean_volume" "home_volume" {
  region                   = data.coder_parameter.region.value
  name                     = "coder-${data.coder_workspace.me.id}-home"
  size                     = data.coder_parameter.home_volume_size.value
  initial_filesystem_type  = "ext4"
  initial_filesystem_label = "coder-home"

  # Protect the volume from being deleted due to changes in attributes.
  lifecycle {
    ignore_changes = all
  }
}

resource "digitalocean_droplet" "workspace" {
  count  = data.coder_workspace.me.start_count
  region = data.coder_parameter.region.value
  name   = "coder-${local.username}-${lower(data.coder_workspace.me.name)}"
  image  = data.coder_parameter.droplet_image.value
  size   = data.coder_parameter.droplet_size.value

  volume_ids = [digitalocean_volume.home_volume.id]
  user_data = templatefile("${path.module}/cloud-config.yaml.tftpl", {
    username          = local.username
    home_volume_label = digitalocean_volume.home_volume.initial_filesystem_label
    ssh_public_key    = data.coder_workspace_owner.me.ssh_public_key
    init_script       = base64encode(coder_agent.main.init_script)
    coder_agent_token = coder_agent.main.token
  })

  ssh_keys = var.ssh_key_id > 0 ? [var.ssh_key_id] : []
}

resource "digitalocean_project_resources" "project" {
  count   = var.project_uuid == "" ? 0 : 1
  project = var.project_uuid

  # Workaround for terraform plan when using count.
  resources = length(digitalocean_droplet.workspace) > 0 ? [
    digitalocean_volume.home_volume.urn,
    digitalocean_droplet.workspace[0].urn
    ] : [
    digitalocean_volume.home_volume.urn
  ]
}

resource "coder_metadata" "workspace_info" {
  count       = data.coder_workspace.me.start_count
  resource_id = digitalocean_droplet.workspace[0].id

  item {
    key   = "region"
    value = digitalocean_droplet.workspace[0].region
  }
  item {
    key   = "size"
    value = digitalocean_droplet.workspace[0].size_slug
  }
  item {
    key   = "image"
    value = digitalocean_droplet.workspace[0].image
  }
  item {
    key   = "ipv4"
    value = digitalocean_droplet.workspace[0].ipv4_address
  }
}

resource "coder_metadata" "volume_info" {
  resource_id = digitalocean_volume.home_volume.id

  item {
    key   = "size"
    value = "${digitalocean_volume.home_volume.size} GiB"
  }
}
