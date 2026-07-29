#!/bin/bash
# Setup weekly staleness audit of the auto-loaded instruction tier
# Runs every Sunday at 10:30 AM (just after the dependency audit)
set -euo pipefail

# Get directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOT_DIR="$(cd "$SCRIPT_DIR/.." && cd .. && pwd)"
AUDIT_BIN="$DOT_DIR/scripts/audit/stale-claims.sh"

# Source scheduler abstraction
source "$DOT_DIR/scripts/scheduler/scheduler.sh"

JOB_ID="stale-claims"

# Logging (uses scheduler's internal prefix to avoid conflicts)
log_step() { echo -e "${BLUE}==>${NC} $1"; }

uninstall() {
    unschedule "$JOB_ID" 2>/dev/null || true
}

install() {
    log_step "Setting up weekly stale-claims audit..."

    if [[ ! -f "$AUDIT_BIN" ]]; then
        _sched_log_warn "Binary not found at $AUDIT_BIN. Skipping."
        return 1
    fi

    # Sunday at 10:30 AM
    schedule_weekly "$JOB_ID" "$AUDIT_BIN" 0 10 30
}

# Always uninstall first to ensure clean state
uninstall >/dev/null 2>&1 || true

# If only uninstalling, exit
if [[ "${1:-}" == "--uninstall" ]]; then
    _sched_log_info "Stale-claims audit uninstalled."
    exit 0
fi

# Otherwise install
install
