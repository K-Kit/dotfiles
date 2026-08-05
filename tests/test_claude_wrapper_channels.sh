#!/usr/bin/env zsh
# shellcheck shell=bash
# Tests that the claude() wrapper composes its auto-detected --channels flags
# with a caller-supplied `--` terminator.
#
# Why this exists: the wrapper appended `--channels ...` to the END of argv.
# claude-spawn passes `--` before the seed prompt so that a dash-leading prompt
# cannot be parsed as a flag, and everything after `--` is positional — so the
# appended channel flags silently stopped being options and became prompt text.
# The session came up with no channel and a polluted prompt, with no error.
#
# This drives the REAL wrapper with a stubbed `claude` on PATH, so it reads the
# argv actually handed to the binary rather than asserting on a printed string.

emulate -L zsh 2>/dev/null || true
setopt no_nomatch 2>/dev/null || true

SCRIPT_DIR="${0:A:h}"
# Optional argument: path to the wrapper under test. Used to point the suite at
# an older copy and confirm these assertions actually fail against it — a test
# that cannot fail is not evidence.
WRAPPER="${1:-$SCRIPT_DIR/../config/aliases/claude.sh}"

pass=0
fail=0
ok()  { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; fail=$((fail + 1)); }

printf 'claude() wrapper: --channels vs the -- terminator\n'

# `timeout` is GNU-only; stock macOS has neither it nor gtimeout unless coreutils
# is installed. Without a fallback the hang test below simply would not run on
# macOS — and a hang test that does not run on the platform is worse than none,
# because the suite still reports green. Returns 124 on timeout, like GNU.
run_limited() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
    return $?
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
    return $?
  fi
  "$@" &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [[ "$waited" -ge "$secs" ]]; then
      kill -9 "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 124
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid"
  return $?
}

# A NON-git temp dir, so the wrapper's auto-cd-to-git-root does not relocate us
# and change which .claude/channels directory is detected.
d=""
for root in /tmp/claude /tmp "${TMPDIR:-}"; do
  [[ -n "$root" ]] || continue
  mkdir -p "$root" 2>/dev/null || continue
  # Full template, not `-p`: that flag is GNU-only and this must run on macOS.
  d=$(mktemp -d "$root/claude-wrapper-test.XXXXXX" 2>/dev/null) && [[ -d "$d" ]] && break
  d=""
done
if [[ -z "$d" ]]; then
  printf '  SKIP (no writable temp directory)\n'
  exit 0
fi

mkdir -p "$d/bin" "$d/.claude/channels/telegram"
: >"$d/.claude/channels/telegram/.env"   # makes the telegram channel auto-detect

# The stub answers --version/--help (the wrapper probes those to build its
# subcommand cache) and records argv for everything else.
{
  printf '#!/usr/bin/env bash\n'
  # shellcheck disable=SC2016  # writing a script; expansion happens when it runs
  printf 'case "$1" in\n'
  printf '  --version) echo "9.9.9 (test stub)"; exit 0 ;;\n'
  printf '  --help)    exit 0 ;;\n'
  printf 'esac\n'
  printf 'printf "ARG:%%s\\n" "$@" >"%s/argv.txt"\n' "$d"
} >"$d/bin/claude"
chmod +x "$d/bin/claude"

export PATH="$d/bin:$PATH"
activate_venv() { :; }   # the wrapper calls this; keep the output clean

cd "$d" || exit 1
# shellcheck source=/dev/null
source "$WRAPPER"

claude --remote-control=rc-name -- 'my seed prompt' >/dev/null 2>&1 || true
got=$(cat "$d/argv.txt" 2>/dev/null || echo "")

if [[ -z "$got" ]]; then
  bad "wrapper reaches the stub" "stub was never invoked"
else
  # The channel flag must be an OPTION, i.e. positioned before the terminator.
  if [[ "$got" == *"ARG:--channels"*"ARG:--"$'\n'* ]]; then
    ok "channels land before the -- terminator"
  else
    bad "channels land before the -- terminator" "argv was:"$'\n'"$got"
  fi

  case "$got" in
    *"ARG:--channels"*) ok "channels flag survives at all" ;;
    *) bad "channels flag survives at all" "no --channels in argv" ;;
  esac

  case "$got" in
    *"ARG:my seed prompt"*) ok "prompt arrives intact" ;;
    *) bad "prompt arrives intact" "argv was:"$'\n'"$got" ;;
  esac

  # The regression this guards: the prompt must be the LAST argument, with no
  # channel plumbing trailing it as extra positional words.
  last=$(printf '%s\n' "$got" | grep '^ARG:' | tail -1)
  if [[ "$last" == "ARG:my seed prompt" ]]; then
    ok "nothing trails the prompt"
  else
    bad "nothing trails the prompt" "last arg was: $last"
  fi
fi

# A seed prompt of exactly `-t` used to be parsed as the wrapper's OWN task
# flag. No value followed, so `shift 2` shifted nothing and returned non-zero,
# and the parse loop spun forever — the spawned session simply never started.
# `--` must stop the wrapper reading further arguments as its own.
: >"$d/argv.txt"
printf 'activate_venv() { :; }\nsource %s\nclaude -- -t\n' "$WRAPPER" >"$d/t-case.zsh"
run_limited 10 zsh "$d/t-case.zsh" >/dev/null 2>&1
t_rc=$?

# Judge by what the agent received, not by exit status — the wrapper can exit
# non-zero for reasons unrelated to this (venv activation, channel setup) and
# that would mask the actual question. Only 124 means timeout, i.e. the hang.
if [[ "$t_rc" -eq 124 ]]; then
  bad "a seed of exactly -t terminates" "wrapper hung (timed out) — the parse loop is spinning"
else
  got2=$(cat "$d/argv.txt" 2>/dev/null || echo "")
  case "$got2" in
    *"ARG:-t"*) ok "a seed of exactly -t reaches the agent" ;;
    *) bad "a seed of exactly -t reaches the agent" "argv was: ${got2:-<empty>}" ;;
  esac
fi

cd / || true
rm -rf "$d"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
