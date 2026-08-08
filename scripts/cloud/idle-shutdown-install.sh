#!/bin/bash
# On-box idle auto-shutdown watchdog for Hetzner dev servers — OPT-IN, default off.
#
# Installs a systemd service + timer that runs `systemctl poweroff` after N
# hours (default 2) with no active SSH connections and no logged-in sessions.
# A powered-off Hetzner server keeps its disk and IP (still billed for those,
# not for CPU/RAM) — wake it with `hcloud server poweron <name>` / `hz poweron`.
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
# Poweroff after IDLE_HOURS with no SSH connections / logins / ~/.keep-alive.
# Written by idle-shutdown-install.sh — edit /etc/idle-shutdown.conf, not this.
set -u
IDLE_HOURS=2
# shellcheck disable=SC1091
[ -f /etc/idle-shutdown.conf ] && . /etc/idle-shutdown.conf
STATE=/run/idle-shutdown.last-active

active() {
    for d in /root /home/*; do
        [ -e "$d/.keep-alive" ] && return 0
    done
    # established TCP connections to sshd (covers detached-but-connected tmux clients)
    [ -n "$(ss -H -o state established '( sport = :22 )' 2>/dev/null)" ] && return 0
    # any logged-in session (serial console, etc.)
    [ -n "$(who 2>/dev/null)" ] && return 0
    return 1
}

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
            inhibitors=""
            for d in /root /home/*; do
                [ -e "$d/.keep-alive" ] && inhibitors="$inhibitors $d/.keep-alive"
            done
            if [ -n "$inhibitors" ]; then
                echo "inhibited by:$inhibitors"
            else
                echo "keep-alive: none"
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
