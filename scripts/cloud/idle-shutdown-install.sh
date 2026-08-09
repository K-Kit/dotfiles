#!/bin/bash
# On-box idle auto-shutdown watchdog for Hetzner dev servers — OPT-IN, default off.
#
# Installs a systemd service + timer that runs `systemctl poweroff` after N
# hours (default 2) with no activity. A powered-off Hetzner server keeps its
# disk and IP (still billed for those, not for CPU/RAM) — wake it with
# `hcloud server poweron <name>` / `hz poweron`.
#
# Counts as activity: ~/.keep-alive, an established SSH connection, a tmux pane
# running something other than a bare shell (detached agent work — the case the
# earlier connection-only check powered off mid-run), or a login whose tty has
# seen output within the idle window. An idle leftover tmux session and a
# forgotten idle login both count as INACTIVE, on purpose.
#
# Usage (as root on the box; `hz idle ...` pipes this file over SSH):
#   idle-shutdown-install.sh install [hours]   # install + enable (default 2h)
#   idle-shutdown-install.sh uninstall         # disable + remove everything
#   idle-shutdown-install.sh status            # watchdog state, last activity
#
# Inhibit without uninstalling: `touch ~/.keep-alive` (any user's home or
# /root); remove the file to re-arm. State lives in /run, so a reboot resets
# the idle clock and OnBootSec gives a 15-minute grace period.
set -euo pipefail

CMD="${1:-install}"
case "$CMD" in
    install)
        HOURS="${2:-2}"
        if ! [[ "$HOURS" =~ ^[0-9]+$ ]] || [[ "$HOURS" -lt 1 ]]; then
            echo "idle-shutdown: hours must be a positive integer (got '$HOURS')" >&2
            exit 1
        fi
        printf 'IDLE_HOURS=%s\n' "$HOURS" > /etc/idle-shutdown.conf

        cat > /usr/local/sbin/idle-shutdown-check <<'CHECK'
#!/bin/bash
# Poweroff after IDLE_HOURS with no activity. Written by idle-shutdown-install.sh
# — edit /etc/idle-shutdown.conf, not this file.
#
# "Activity" is deliberately about work happening, not about artefacts existing.
# A detached tmux session running an agent must keep the box alive; an empty
# tmux session left behind by `cw`, or a forgotten idle login, must not.
set -u
IDLE_HOURS=2
# shellcheck disable=SC1091
[ -f /etc/idle-shutdown.conf ] && . /etc/idle-shutdown.conf
STATE=/run/idle-shutdown.last-active

# A tmux pane whose foreground process is not just a shell — i.e. detached work
# that nobody is connected to. Existence of a session is NOT enough: sessions
# outlive their work here (cw, tmux-resurrect), and counting them would make the
# watchdog a silent no-op on a box that keeps billing.
# root can open any user's 0700 tmux socket, so no privilege drop is needed.
tmux_running_work() {
    command -v tmux >/dev/null 2>&1 || return 1
    local sock cmd
    for sock in ${TMUX_TMPDIR:-/tmp}/tmux-*/*; do
        [ -S "$sock" ] || continue
        for cmd in $(tmux -S "$sock" list-panes -a -F '#{pane_current_command}' 2>/dev/null); do
            case "$cmd" in
                bash|-bash|sh|dash|zsh|-zsh|fish|ksh|tmux|login) ;;
                *) return 0 ;;
            esac
        done
    done
    return 1
}

# A login whose tty has seen output within the idle window. Bounded on purpose:
# an unconditional `who` check returns true for as long as a forgotten session
# stays open, which pins the box on indefinitely.
tty_recently_active() {
    local tty ts now limit
    now=$(date +%s)
    limit=$(( IDLE_HOURS * 3600 ))
    for tty in $(who 2>/dev/null | awk '{print $2}'); do
        ts=$(stat -c %Y "/dev/$tty" 2>/dev/null) || continue
        [ $(( now - ts )) -lt "$limit" ] && return 0
    done
    return 1
}

active() {
    for d in /root /home/*; do
        [ -e "$d/.keep-alive" ] && return 0
    done
    # established TCP connections to sshd — someone is connected right now
    [ -n "$(ss -H -o state established '( sport = :22 )' 2>/dev/null)" ] && return 0
    tmux_running_work && return 0
    tty_recently_active && return 0
    return 1
}

# --explain reports each signal and never powers off, so `hz idle status` can
# show why a box is (or is not) considered busy without a second copy of this
# logic living somewhere else.
if [ "${1:-}" = "--explain" ]; then
    for d in /root /home/*; do
        [ -e "$d/.keep-alive" ] && echo "keep-alive:     $d/.keep-alive"
    done
    echo "ssh sessions:   $(ss -H -o state established '( sport = :22 )' 2>/dev/null | wc -l)"
    if tmux_running_work; then echo "tmux work:      running"; else echo "tmux work:      none"; fi
    if tty_recently_active; then echo "tty activity:   within ${IDLE_HOURS}h"; else echo "tty activity:   none within ${IDLE_HOURS}h"; fi
    if active; then echo "verdict:        ACTIVE (idle clock resets)"; else echo "verdict:        idle"; fi
    exit 0
fi

if active || [ ! -f "$STATE" ]; then
    touch "$STATE"
    exit 0
fi

idle=$(( $(date +%s) - $(stat -c %Y "$STATE") ))
limit=$(( IDLE_HOURS * 3600 ))
if [ "$idle" -ge "$limit" ]; then
    logger -t idle-shutdown "idle ${idle}s >= limit ${limit}s — powering off"
    systemctl poweroff
fi
CHECK
        chmod 0755 /usr/local/sbin/idle-shutdown-check

        cat > /etc/systemd/system/idle-shutdown.service <<'UNIT'
[Unit]
Description=Power off when idle (no SSH sessions; inhibit with ~/.keep-alive)

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/idle-shutdown-check
UNIT

        cat > /etc/systemd/system/idle-shutdown.timer <<'UNIT'
[Unit]
Description=Periodic idle-shutdown check

[Timer]
OnBootSec=15min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT

        systemctl daemon-reload
        systemctl enable --now idle-shutdown.timer
        echo "idle-shutdown: ON — poweroff after ${HOURS}h with no SSH; inhibit with 'touch ~/.keep-alive'"
        ;;

    uninstall)
        systemctl disable --now idle-shutdown.timer 2>/dev/null || true
        rm -f /etc/systemd/system/idle-shutdown.service \
              /etc/systemd/system/idle-shutdown.timer \
              /usr/local/sbin/idle-shutdown-check \
              /etc/idle-shutdown.conf \
              /run/idle-shutdown.last-active
        systemctl daemon-reload
        echo "idle-shutdown: removed"
        ;;

    status)
        if systemctl is-enabled idle-shutdown.timer >/dev/null 2>&1; then
            IDLE_HOURS=2
            # shellcheck disable=SC1091
            [ -f /etc/idle-shutdown.conf ] && . /etc/idle-shutdown.conf
            echo "watchdog: enabled (IDLE_HOURS=${IDLE_HOURS}, timer $(systemctl is-active idle-shutdown.timer 2>/dev/null || echo unknown))"
            if [ -f /run/idle-shutdown.last-active ]; then
                echo "last activity: $(( $(date +%s) - $(stat -c %Y /run/idle-shutdown.last-active) ))s ago"
            else
                echo "last activity: no check has run yet"
            fi
            # single source of truth: ask the installed checker itself
            if [ -x /usr/local/sbin/idle-shutdown-check ]; then
                /usr/local/sbin/idle-shutdown-check --explain
            fi
        else
            echo "watchdog: not installed"
        fi
        ;;

    *)
        echo "usage: $0 [install [hours] | uninstall | status]" >&2
        exit 1
        ;;
esac
