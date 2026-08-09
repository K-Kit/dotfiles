#!/bin/bash
# Render hetzner-cloud-init.yaml with secrets resolved BY NAME from fnox
# (~/fnox.toml, age-encrypted) so nothing is pasted by hand. Emits cloud-init
# user-data on stdout; refuses to write to a terminal when secrets are in play.
#
# Usage:
#   scripts/cloud/hetzner-user-data.sh > "$TMPDIR/user-data.yaml"
#   HCLOUD_TOKEN="$(fnox get HERTZNER)" hcloud server create \
#       --name dev-box --type cx23 --image ubuntu-24.04 \
#       --ssh-key <key-name> --user-data-from-file "$TMPDIR/user-data.yaml"
#
#   scripts/cloud/hetzner-user-data.sh --no-secrets   # plain template passthrough
#   scripts/cloud/hetzner-user-data.sh --branch my-wip > "$TMPDIR/user-data.yaml"
#
# Options:
#   --no-secrets      emit the template verbatim, resolving nothing
#   --branch <name>   dotfiles branch the box bootstraps from (default: main)
#
# Secrets injected when resolvable: TAILSCALE_AUTH_KEY, BWS_TOKEN. Unresolved
# names stay commented out in the template (post-boot setup still works).
#
# Deliberately NOT injected: GITHUB_TOKEN. user-data is served by the instance
# metadata service to every local user, so a PAT here is readable by anything
# on the box. `hz create --github-auth` pushes it over SSH after boot instead.
#
# Same exposure caveat as filling the env block by hand: user-data is readable
# from the instance metadata service and lands root-readable (0600) on disk.
set -euo pipefail

TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hetzner-cloud-init.yaml"
INJECT=1
BRANCH="main"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-secrets) INJECT=0; shift ;;
        --branch) BRANCH="${2:?--branch needs a value}"; shift 2 ;;
        --branch=*) BRANCH="${1#*=}"; shift ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: ${1} (--no-secrets, --branch <name>)" >&2; exit 1 ;;
    esac
done

# The branch name is interpolated into the YAML and later into a URL on the
# box; keep it to what git actually allows in a ref so neither can be escaped.
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] \
    || { echo "Invalid branch name: $BRANCH" >&2; exit 1; }

if [[ "$INJECT" == "1" && -t 1 ]]; then
    echo "stdout is a terminal and output may contain secrets — redirect it, e.g.:" >&2
    echo "  $0 > \"\$TMPDIR/user-data.yaml\"" >&2
    exit 1
fi

fnox_get() {
    command -v fnox >/dev/null 2>&1 || return 0
    FNOX_NON_INTERACTIVE=true fnox get "$1" 2>/dev/null || true
}

TS="" BWS=""
if [[ "$INJECT" == "1" ]]; then
    command -v fnox >/dev/null 2>&1 || echo "fnox not installed — emitting template without secrets" >&2
    TS="$(fnox_get TAILSCALE_AUTH_KEY)"
    BWS="$(fnox_get BWS_TOKEN)"
    [[ -n "$TS" ]]  && echo "+ TAILSCALE_AUTH_KEY injected (from fnox)" >&2 \
                    || echo "- TAILSCALE_AUTH_KEY not resolved — left commented" >&2
    [[ -n "$BWS" ]] && echo "+ BWS_TOKEN injected (from fnox)" >&2 \
                    || echo "- BWS_TOKEN not resolved — left commented" >&2
fi

# Uncomment-and-fill the env block lines, preserving indentation. Values are
# single-quoted for the `set -a; . env-file` sourcing in dotfiles-bootstrap.
while IFS= read -r line; do
    if [[ -n "$TS" && "$line" == *"#TAILSCALE_AUTH_KEY="* ]]; then
        line="${line%%#*}TAILSCALE_AUTH_KEY='$TS'"
    elif [[ -n "$BWS" && "$line" == *"#BWS_TOKEN="* ]]; then
        line="${line%%#*}BWS_TOKEN='$BWS'"
    elif [[ "$line" == *"DOTFILES_BRANCH=main" ]]; then
        line="${line%DOTFILES_BRANCH=main}DOTFILES_BRANCH=$BRANCH"
    fi
    printf '%s\n' "$line"
done < "$TEMPLATE"
