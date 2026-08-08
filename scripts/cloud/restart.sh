#!/bin/bash
# Quick restore after container restart — delegates to create-user.sh
# RunPod containers lose /etc/passwd and /home on restart; this recreates them.
# RunPod-only: on a VPS (Hetzner etc.) /home and /etc/passwd persist, so there
# is nothing to restore — the script no-ops with a message. Force a re-run of
# create-user.sh anyway (it's idempotent) with CLOUD_MODE=runpod, or just run
# create-user.sh directly.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/k-kit/dotfiles/main/scripts/cloud/restart.sh | bash
#   curl -fsSL ... | USERNAME=dev bash

USERNAME="${1:-${USERNAME:-${DOTFILES_USERNAME:-k-kit}}}"
export USERNAME

if [[ ! -d /workspace && -z "${RUNPOD_POD_ID:-}" && "${CLOUD_MODE:-}" != "runpod" ]]; then
    echo "VPS mode: /home is persistent — nothing to restore after a reboot."
    echo "To (re-)provision anyway, run create-user.sh directly; it is idempotent."
    exit 0
fi

CREATE_USER_URL="https://raw.githubusercontent.com/k-kit/dotfiles/main/scripts/cloud/create-user.sh"
curl -fsSL "$CREATE_USER_URL" | bash

echo ""
echo "Switch: su - $USERNAME"
