terraform {
  required_providers {
    coder = {
      source = "coder/coder"
    }
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.45"
    }
  }
}

provider "coder" {}

# Hetzner Cloud API token.
#
# Leave unset to authenticate the provisioner from the HCLOUD_TOKEN environment
# variable (the upstream Coder convention -- export it wherever `coder server`
# or `coder provisioner start` runs). Set it as a template variable instead if
# you want the token to travel with the template version:
#
#   coder templates push hetzner-linux --var hcloud_token="$(fnox get HERTZNER)"
#
# Note that a template variable is stored server-side by Coder.
variable "hcloud_token" {
  type        = string
  description = "Hetzner Cloud API token. Leave empty to use the HCLOUD_TOKEN environment variable of the provisioner."
  sensitive   = true
  default     = ""
}

# SSH key names or IDs that already exist in the Hetzner Cloud project. Purely
# optional: Coder reaches the workspace through the agent, not over port 22.
# Useful as a break-glass path when the agent fails to come up.
variable "ssh_keys" {
  type        = list(string)
  description = "Hetzner Cloud SSH key names or IDs to inject at server creation (`hcloud ssh-key list`)."
  default     = []
}

variable "dotfiles_uri" {
  type        = string
  description = "Default dotfiles repository offered to workspace owners. Empty means no default -- the user is still prompted and can leave it blank to skip."
  default     = ""
}

provider "hcloud" {
  # Falls back to $HCLOUD_TOKEN when the variable is empty.
  token = var.hcloud_token != "" ? var.hcloud_token : null
}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

locals {
  username = lower(data.coder_workspace_owner.me.name)

  # Hetzner exposes attached volumes at a stable by-id path.
  # See https://docs.hetzner.com/cloud/volumes/getting-started/mounting-volumes
  home_device = "/dev/disk/by-id/scsi-0HC_Volume_${hcloud_volume.home.id}"
}

data "coder_parameter" "location" {
  name         = "location"
  display_name = "Location"
  description  = "Hetzner Cloud location. CX server types only exist in the EU locations; use a CPX or CAX type in ash/hil/sin."
  icon         = "/emojis/1f30e.png"
  type         = "string"
  default      = "fsn1"
  mutable      = false

  option {
    name  = "Germany (Falkenstein)"
    value = "fsn1"
    icon  = "/emojis/1f1e9-1f1ea.png"
  }
  option {
    name  = "Germany (Nuremberg)"
    value = "nbg1"
    icon  = "/emojis/1f1e9-1f1ea.png"
  }
  option {
    name  = "Finland (Helsinki)"
    value = "hel1"
    icon  = "/emojis/1f1eb-1f1ee.png"
  }
  option {
    name  = "United States (Ashburn, VA)"
    value = "ash"
    icon  = "/emojis/1f1fa-1f1f8.png"
  }
  option {
    name  = "United States (Hillsboro, OR)"
    value = "hil"
    icon  = "/emojis/1f1fa-1f1f8.png"
  }
  option {
    name  = "Singapore"
    value = "sin"
    icon  = "/emojis/1f1f8-1f1ec.png"
  }
}

# x86 types only -- coder_agent.arch below is hardcoded to amd64. Adding an ARM
# (cax*) option here without also switching arch to arm64 produces a workspace
# that builds successfully and never connects. See README.md.
data "coder_parameter" "server_type" {
  name         = "server_type"
  display_name = "Server type"
  description  = "Shared-vCPU Hetzner server type. CX types are EU-only; CPX types are available in every location."
  icon         = "/icon/memory.svg"
  type         = "string"
  default      = "cx23"
  mutable      = false

  option {
    name  = "cpx11 -- 2 vCPU, 2 GB RAM, 40 GB (cheapest, all locations)"
    value = "cpx11"
  }
  option {
    name  = "cx23 -- 2 vCPU, 4 GB RAM, 40 GB (EU only)"
    value = "cx23"
  }
  option {
    name  = "cpx21 -- 3 vCPU, 4 GB RAM, 80 GB (all locations)"
    value = "cpx21"
  }
  option {
    name  = "cx33 -- 4 vCPU, 8 GB RAM, 80 GB (EU only)"
    value = "cx33"
  }
  option {
    name  = "cpx31 -- 4 vCPU, 8 GB RAM, 160 GB (all locations)"
    value = "cpx31"
  }
  option {
    name  = "cx43 -- 8 vCPU, 16 GB RAM, 160 GB (EU only)"
    value = "cx43"
  }
}

data "coder_parameter" "image" {
  name         = "image"
  display_name = "Image"
  description  = "Base image for the workspace VM."
  icon         = "/icon/ubuntu.svg"
  type         = "string"
  default      = "ubuntu-24.04"
  mutable      = false

  option {
    name  = "Ubuntu 24.04 (LTS)"
    value = "ubuntu-24.04"
    icon  = "/icon/ubuntu.svg"
  }
  option {
    name  = "Ubuntu 22.04 (LTS)"
    value = "ubuntu-22.04"
    icon  = "/icon/ubuntu.svg"
  }
  option {
    name  = "Debian 12"
    value = "debian-12"
    icon  = "/icon/debian.svg"
  }
}

data "coder_parameter" "home_volume_size" {
  name         = "home_volume_size"
  display_name = "Home volume size"
  description  = "Size of the persistent /home volume, in GB. Hetzner volumes start at 10 GB."
  type         = "number"
  default      = "20"
  mutable      = false

  validation {
    min = 10
    max = 1024
  }
}

resource "coder_agent" "main" {
  os   = "linux"
  arch = "amd64"

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

# Optional personalization. Prompts the workspace owner for a dotfiles repo and
# runs `coder dotfiles -y <url>` inside the workspace, which executes this
# repo's install.sh if you point it here. Leaving the parameter blank skips it.
# See https://registry.coder.com/modules/coder/dotfiles
module "dotfiles" {
  count                = data.coder_workspace.me.start_count
  source               = "registry.coder.com/coder/dotfiles/coder"
  version              = "~> 1.4"
  agent_id             = coder_agent.main.id
  default_dotfiles_uri = var.dotfiles_uri
}

# Persistent /home. Created with `location` (not `server_id`) so it outlives the
# server, which is destroyed every time the workspace stops. Formatted once here;
# cloud-init only ever mounts it, never mkfs.
resource "hcloud_volume" "home" {
  name     = "coder-${data.coder_workspace.me.id}-home"
  location = data.coder_parameter.location.value
  size     = data.coder_parameter.home_volume_size.value
  format   = "ext4"

  labels = {
    coder_workspace_id = data.coder_workspace.me.id
    coder_owner        = local.username
  }

  # Protect the volume from being deleted due to changes in attributes.
  lifecycle {
    ignore_changes = all
  }
}

resource "hcloud_server" "workspace" {
  count       = data.coder_workspace.me.start_count
  name        = "coder-${local.username}-${lower(data.coder_workspace.me.name)}"
  location    = data.coder_parameter.location.value
  server_type = data.coder_parameter.server_type.value
  image       = data.coder_parameter.image.value
  ssh_keys    = var.ssh_keys

  user_data = templatefile("${path.module}/cloud-config.yaml.tftpl", {
    username          = local.username
    home_device       = local.home_device
    ssh_public_key    = data.coder_workspace_owner.me.ssh_public_key
    init_script       = base64encode(coder_agent.main.init_script)
    coder_agent_token = coder_agent.main.token
  })

  labels = {
    coder_workspace_id = data.coder_workspace.me.id
    coder_owner        = local.username
  }
}

resource "hcloud_volume_attachment" "home" {
  count     = data.coder_workspace.me.start_count
  volume_id = hcloud_volume.home.id
  server_id = hcloud_server.workspace[0].id
  automount = false
}

resource "coder_metadata" "workspace_info" {
  count       = data.coder_workspace.me.start_count
  resource_id = hcloud_server.workspace[0].id

  item {
    key   = "location"
    value = hcloud_server.workspace[0].location
  }
  item {
    key   = "type"
    value = hcloud_server.workspace[0].server_type
  }
  item {
    key   = "image"
    value = data.coder_parameter.image.value
  }
  item {
    key   = "ipv4"
    value = hcloud_server.workspace[0].ipv4_address
  }
}

resource "coder_metadata" "volume_info" {
  resource_id = hcloud_volume.home.id

  item {
    key   = "size"
    value = "${hcloud_volume.home.size} GB"
  }
}
