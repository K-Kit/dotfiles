#!/usr/bin/env python3
"""PostToolUse(Bash) hook: tally repeat runs of throwaway scripts.

Pairs with simplify_nudge.sh (Stop), which turns the tally into a suggestion to
promote a scratch script into a permanent, reusable component. The signal we
want is "this throwaway earned its keep", not "a script exists":

  * only executions count — writing and editing a script is iteration, not reuse
    (that's what the simplify_mark_dirty.sh / simplify_nudge.sh pair covers);
  * a run whose file is byte-identical (same mtime) to the previous run is a
    "stable" run. Edit-run-edit-run debugging loops produce runs but no stable
    runs, so they never reach the promotion threshold.

State lives in a per-session JSON file under TMPDIR, same convention as the
dirty marker. Per-session (not per-user) keeps cleanup free and avoids nudging
about a script someone abandoned last week. Concurrent Bash calls can race on
the read-modify-write; the loser silently loses one increment, which is a fine
outcome for a nudge and not worth a lock file.

Always exits 0 and never blocks — a tracking hook must not break a Bash call.
"""

import json
import os
import re
import shlex
import sys

SCRIPT_RE = re.compile(r"\.(py|sh|bash|zsh|js|mjs|cjs|ts|rb|pl|R)$")

# Throwaway *locations*: a tmp/scratch path segment anywhere, or macOS's
# /var/folders TMPDIR. Includes the agent scratchpad (/tmp/claude-*/…).
SCRATCH_DIR_RE = re.compile(r"(^|/)(tmp|temp|scratch|scratchpad|\.tmp|var/folders)(/|$)")
# Throwaway *names*: tmp_foo.py, scratch-check.sh, oneoff_backfill.py.
SCRATCH_NAME_RE = re.compile(r"^(tmp|temp|scratch|throwaway|oneoff|one_off)[-_.]")
# Vendored trees hold plenty of tmp/ paths that are nobody's scratch work.
VENDOR_RE = re.compile(r"/(node_modules|site-packages|\.venv|venv|\.git|dist|build)/")
# A script whose own directory is a permanent home already lives where a
# promotion would put it. Matters because repos and worktrees get checked out
# under /tmp, which would otherwise make every script in them look scratch.
PERMANENT_PARENTS = {"bin", "sbin", "custom_bins", "tools", "scripts", "libexec"}

# A candidate path only counts when something actually ran it: an interpreter
# in front of it, or the path itself in command position (./tmp/x.sh).
INTERPRETERS = {
    "python", "python3", "uv", "uvx", "run", "bash", "sh", "zsh", "fish",
    "node", "bun", "deno", "tsx", "ts-node", "ruby", "perl", "Rscript",
    "pytest", "poetry", "pdm", "source", ".",
}
# Tokens after which the next word starts a fresh command.
CMD_START_AFTER = {
    "&&", "||", ";", "|", "|&", "&", "(", ")", "{", "}",
    "then", "else", "do", "time", "nohup", "env", "exec", "xargs", "command",
}


def is_scratch(path: str) -> bool:
    if not SCRIPT_RE.search(path) or VENDOR_RE.search(path):
        return False
    parent = os.path.dirname(path)
    if os.path.basename(parent) in PERMANENT_PARENTS:
        return False
    if SCRATCH_DIR_RE.search(parent):
        return True
    return bool(SCRATCH_NAME_RE.match(os.path.basename(path)))


def scripts_run(command: str, cwd: str) -> list:
    """Absolute paths of scratch scripts this command executes (deduped)."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        # Unbalanced quotes (heredocs, mostly) — a dumb split still finds paths.
        tokens = command.split()

    found, at_cmd_start = [], True
    for i, token in enumerate(tokens):
        if token in CMD_START_AFTER:
            at_cmd_start = True
            continue
        prev = tokens[i - 1] if i else ""
        executed = at_cmd_start or os.path.basename(prev) in INTERPRETERS
        at_cmd_start = False
        if executed and is_scratch(token):
            resolved = os.path.normpath(os.path.join(cwd, os.path.expanduser(token)))
            if resolved not in found:
                found.append(resolved)
    return found


def state_path(session_id: str) -> str:
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, f"claude-simplify-reuse-{session_id}.json")


def load_state(path: str) -> dict:
    try:
        with open(path) as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def record(state: dict, script: str) -> None:
    try:
        mtime = os.path.getmtime(script)
    except OSError:
        mtime = 0.0  # deleted or never on disk — still counts as a run

    entry = state.get(script)
    if not isinstance(entry, dict):
        entry = {"runs": 0, "stable": 0, "mtime": None, "notified": False}
    entry["runs"] = int(entry.get("runs") or 0) + 1
    if entry.get("mtime") == mtime:
        entry["stable"] = int(entry.get("stable") or 0) + 1
    entry["mtime"] = mtime
    state[script] = entry


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    session_id = payload.get("session_id") or ""
    tool_input = payload.get("tool_input")
    if not session_id or not isinstance(tool_input, dict):
        return

    command = tool_input.get("command") or ""
    if not isinstance(command, str) or not command:
        return

    cwd = payload.get("cwd") or os.getcwd()
    scripts = scripts_run(command, cwd if isinstance(cwd, str) else os.getcwd())
    if not scripts:
        return

    path = state_path(session_id)
    state = load_state(path)
    for script in scripts:
        record(state, script)
    save_state(path, state)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
