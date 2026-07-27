#!/bin/bash
# Setup daily Claude Code plugin-cache cleanup.
#
# Plugin install/update abandons a bare `.git` clone per operation in
# cache/temp_git_*, and leaves superseded plugin versions behind. Both accrue at
# roughly 6MB/day and nothing reclaims them between deploys.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=scripts/scheduler/scheduler.sh
source "$DOT_DIR/scripts/scheduler/scheduler.sh"

JOB_ID="claude-cache-clean"
# schedule_daily embeds the command as a single launchd ProgramArguments string,
# so `claude-cache-clean --apply` cannot be scheduled directly on macOS — the
# wrapper supplies the flag.
WRAPPER="$DOT_DIR/custom_bins/claude-cache-clean-apply"

log_step() { echo -e "${BLUE}==>${NC} $1"; }

uninstall() {
    unschedule "$JOB_ID" 2>/dev/null || true
}

install() {
    log_step "Setting up Claude plugin-cache cleanup..."

    if [[ ! -f "$WRAPPER" ]]; then
        _sched_log_warn "Wrapper not found at $WRAPPER. Skipping."
        return 1
    fi

    chmod +x "$WRAPPER"
    schedule_daily "$JOB_ID" "$WRAPPER" 3 0
}

# Always uninstall first to ensure clean state
uninstall >/dev/null 2>&1 || true

# If only uninstalling, exit
if [[ "${1:-}" == "--uninstall" ]]; then
    _sched_log_info "Claude plugin-cache cleanup uninstalled."
    exit 0
fi

install
