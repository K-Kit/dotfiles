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

# A NON-git temp dir, so the wrapper's auto-cd-to-git-root does not relocate us
# and change which .claude/channels directory is detected.
d=""
for root in /tmp/claude /tmp "${TMPDIR:-}"; do
  [[ -n "$root" ]] || continue
  mkdir -p "$root" 2>/dev/null || continue
  d=$(mktemp -d -p "$root" 2>/dev/null) && [[ -d "$d" ]] && break
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

cd / || true
rm -rf "$d"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
