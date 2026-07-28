# aliases/tmux.sh — tmux session aliases and chmod helpers

#-------------------------------------------------------------
# tmux
#-------------------------------------------------------------

alias ta="tmux attach"
alias taa="tmux attach -t"
alias tad="tmux attach -d -t"
alias td="tmux detach"
alias tn="tmux new-session -s"
alias ts="tmux new-session -s"
alias tl="tmux list-sessions"
alias tkill="tmux kill-server"
alias tdel="tmux kill-session -t"

# ls/tree aliases → config/modern_tools.sh (single source of truth)

#-------------------------------------------------------------
# tmux-resume opt-in
#-------------------------------------------------------------
# tmux-resume (hourly) only sends keystrokes into windows whose name starts with
# `auto-`. Everything else is detected, logged, and left alone to stop at the
# rate limit. These helpers toggle that opt-in on the CURRENT window, which is
# the common case: you decide a session should run unattended after starting it,
# not before. Config + rationale: config/tmux-resume-patterns.conf
#
# `rename-window` also disables tmux's automatic-rename for the window, so the
# name sticks instead of being overwritten by the running command.

# tauto [topic] — opt this window in. Defaults to prefixing the current name.
tauto () {
  [ -n "$TMUX" ] || { echo "tauto: not inside tmux" >&2; return 1; }
  local cur; cur="$(tmux display-message -p '#{window_name}')"
  local topic="${1:-$cur}"
  case "$topic" in auto-*) ;; *) topic="auto-$topic" ;; esac
  tmux rename-window "$topic" && echo "opted in: $topic"
}

# tnoauto — opt this window back out.
tnoauto () {
  [ -n "$TMUX" ] || { echo "tnoauto: not inside tmux" >&2; return 1; }
  local cur; cur="$(tmux display-message -p '#{window_name}')"
  case "$cur" in
    auto-*) tmux rename-window "${cur#auto-}" && echo "opted out: ${cur#auto-}" ;;
    *) echo "already opted out: $cur" ;;
  esac
}

# tautols — every window that will be auto-resumed, across all sessions.
tautols () {
  local out
  # Tab-delimited so names containing spaces stay one field, as tmux-resume reads them.
  # awk, not `grep -P '\tauto-'` — BSD grep on macOS has no -P.
  out="$(tmux list-windows -a -F '#{session_name}:#{window_index}	#{window_name}' 2>/dev/null \
    | awk -F'\t' '$2 ~ /^auto-/' || true)"
  if [ -z "$out" ]; then echo "(no windows opted in)"; else printf '%s\n' "$out"; fi
}

#-------------------------------------------------------------
# chmod
#-------------------------------------------------------------

chw () {
  if [ "$#" -eq 1 ]; then
    chmod a+w $1
  else
    echo "Usage: chw <dir>" >&2
  fi
}
chx () {
  if [ "$#" -eq 1 ]; then
    chmod a+x $1
  else
    echo "Usage: chx <dir>" >&2
  fi
}
