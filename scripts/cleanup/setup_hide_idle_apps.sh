#!/bin/bash
# Setup periodic hide-idle-apps polling (hide running apps not in the
# [hide-idle-exclude] section once they've had no visible window for N minutes).
# macOS only. Poll interval and threshold come from config/hide-idle.conf.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
HIDE_BIN="$DOT_DIR/custom_bins/hide-idle-apps"
TUNABLES_CONF="$DOT_DIR/config/hide-idle.conf"
HELPER_SRC="$DOT_DIR/tools/window-exposure/main.swift"
HELPER_BIN="$DOT_DIR/tools/window-exposure/window-exposure"

source "$DOT_DIR/scripts/scheduler/scheduler.sh"

JOB_ID="hide-idle-apps"
HIDE_IDLE_POLL_SECONDS=60
# shellcheck source=/dev/null
[[ -f "$TUNABLES_CONF" ]] && source "$TUNABLES_CONF"

uninstall() { unschedule "$JOB_ID" 2>/dev/null || true; }

# Build the exposure helper here so the first poll doesn't have to. The script
# can compile it lazily too, but launchd jobs run without a developer PATH.
build_helper() {
    [[ -f "$HELPER_SRC" ]] || return 1
    [[ -x "$HELPER_BIN" && "$HELPER_BIN" -nt "$HELPER_SRC" ]] && return 0
    command -v swiftc >/dev/null 2>&1 || return 1
    swiftc -O -o "$HELPER_BIN" "$HELPER_SRC" >/dev/null 2>&1
}

install() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        _sched_log_info "hide-idle-apps is macOS-only. Skipping."
        return 0
    fi
    if [[ ! -f "$HIDE_BIN" ]]; then
        _sched_log_warn "Binary not found at $HIDE_BIN. Skipping."
        return 1
    fi
    if ! build_helper; then
        _sched_log_warn "Could not build window-exposure helper (needs swiftc)."
        _sched_log_warn "hide-idle-apps hides nothing without it. Skipping."
        return 1
    fi
    schedule_interval "$JOB_ID" "$HIDE_BIN" "$HIDE_IDLE_POLL_SECONDS"
}

# Always uninstall first for clean state
uninstall >/dev/null 2>&1 || true

if [[ "${1:-}" == "--uninstall" ]]; then
    _sched_log_info "hide-idle-apps uninstalled."
    exit 0
fi

install
