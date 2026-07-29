#!/bin/bash
# Setup periodic hide-idle-apps polling: apps left covered up are hidden, then
# have their windows closed, then are quit, each after its own delay.
# macOS only. Poll interval comes from config/hide-idle.conf; every per-app
# policy (which rungs apply, and after how long) from config/app-lifecycle.yaml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
HIDE_BIN="$DOT_DIR/custom_bins/hide-idle-apps"
TUNABLES_CONF="$DOT_DIR/config/hide-idle.conf"
HELPER_SRC="$DOT_DIR/tools/window-exposure/main.swift"
HELPER_BIN="$DOT_DIR/tools/window-exposure/window-exposure"

source "$DOT_DIR/scripts/scheduler/scheduler.sh"

JOB_ID="hide-idle-apps"
# The FILE is authoritative for the installed job, and an inherited
# HIDE_IDLE_POLL_SECONDS is deliberately not honoured here.
#
# It cannot be: launchd's ProgramArguments is a bare argv with no shell and no
# environment of ours, so an override would set the plist's StartInterval and
# never reach the job. hide-idle-apps would re-read this file, get 60, derive
# MAX_GAP=180, and then read every 300s wake as a skipped interval - resetting
# every app's timers on each poll, so the automation would never act at all.
# One value both the plist and the runtime can see is the only safe kind.
_poll_override="${HIDE_IDLE_POLL_SECONDS:-}"
HIDE_IDLE_POLL_SECONDS=60
# shellcheck source=/dev/null
[[ -f "$TUNABLES_CONF" ]] && source "$TUNABLES_CONF"
if [[ -n "$_poll_override" && "$_poll_override" != "$HIDE_IDLE_POLL_SECONDS" ]]; then
    _sched_log_warn "Ignoring HIDE_IDLE_POLL_SECONDS=$_poll_override from the environment:"
    _sched_log_warn "it cannot reach a launchd job. Set HIDE_IDLE_POLL_SECONDS in $TUNABLES_CONF instead."
fi

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
