#!/usr/bin/env python3
"""Wire the tranche-1 harness hooks (H1-H3, F1-F4) into claude/settings.json.

Why a script rather than a hand-edit or a worktree merge:

  * `claude/settings.json` is symlinked to `~/.claude/settings.json` and is
    DUAL-WRITTEN — Claude Code rewrites it at runtime. A diff prepared against
    a stale copy can silently clobber a concurrent write, so the edit has to be
    computed against whatever is on disk at apply time. That also rules out
    letting the change arrive via a worktree merge.
  * Every hook entry is a `$HOME/.claude/hooks/...` path. `~/.claude` resolves
    to the MAIN checkout, so wiring a hook whose script exists only in a
    worktree yields an entry pointing at nothing — which is exactly the
    dead-matcher failure this tranche is fixing, except on a blocking hook,
    where "silently absent" means "silently permits". This script refuses to
    wire a path it cannot stat.

Idempotent: re-running after a partial apply adds only what is missing.
Dry-run by default; pass --apply to write.

    python3 scripts/setup/wire_harness_hooks.py            # show the plan
    python3 scripts/setup/wire_harness_hooks.py --apply    # write it
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
HOOKS_DIR = Path.home() / ".claude" / "hooks"

# Per .claude/rules/dotfiles-settings.md — a file missing any of these is the
# degraded stub, not the real settings. Refuse to touch it.
REQUIRED_KEYS = ("statusLine", "hooks", "permissions")

# H3: this matcher names a tool that does not exist. The live Gmail connector
# exposes `create_draft`, not `gmail_create_draft`, so the hook never fired.
DEAD_MATCHER = "mcp__claude_ai_Gmail__gmail_create_draft"
LIVE_MATCHER = "mcp__claude_ai_Gmail__create_draft"

# MCP deletions that block_gws_delete.sh now recognises. Wiring these is what
# makes its tool_name branch reachable — without a matcher the branch is dead.
MCP_DELETE_TOOLS = (
    "mcp__claude_ai_Google_Calendar__delete_event",
    "mcp__claude_ai_Gmail__delete_label",
)

# A script can be PRESENT and EXECUTABLE and still be the wrong version. The
# existence check below catches a hook that was never deployed; it cannot catch
# a hook that was deployed before the capability we are about to wire existed.
# block_gws_delete.sh is exactly that case: the old copy handles only `command`
# and returns early on MCP input, so wiring the MCP matchers against it yields a
# blocking matcher that silently permits — the failure this tranche exists to
# remove. Each marker is a string that only the NEW version contains.
CAPABILITY_MARKERS = {
    "block_gws_delete.sh": (
        "Google_Calendar__delete_event",
        "the MCP tool_name branch (H2)",
    ),
    "block_unsafe_install.py": (
        "--no-quarantine",
        "the four supply-chain gates (H1)",
    ),
}

# (event, matcher, script, timeout, anchor)
# `anchor`: insert immediately before the entry whose command contains this
# string; None appends. Blockers go ahead of nudges so a refused call short-
# circuits before anything advisory runs.
WIRINGS = [
    ("PreToolUse", "Bash", "block_unsafe_install.py", 5, "warn_dep_install.sh"),
    ("PreToolUse", "Write", "guard_existing_code.sh", 5, None),
    ("PostToolUse", "Write|Edit", "nudge_synthetic_data.py", 5, None),
    ("PostToolUse", "Write|Edit", "nudge_hyperparam_provenance.py", 5, None),
    ("Stop", None, "nudge_number_provenance.py", 5, None),
]


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def entry_for(script: str, timeout: int) -> dict:
    return {
        "type": "command",
        "command": f"$HOME/.claude/hooks/{script}",
        "timeout": timeout,
    }


def find_block(blocks: list, matcher: str | None) -> dict | None:
    """Locate the hook block for a matcher. `None` matches a block with no
    matcher key (the Stop-style catch-all)."""
    for block in blocks:
        if matcher is None:
            if "matcher" not in block:
                return block
        elif block.get("matcher") == matcher:
            return block
    return None


def has_command(block: dict, script: str) -> bool:
    return any(script in h.get("command", "") for h in block.get("hooks", []))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    if not SETTINGS.exists():
        fail(f"{SETTINGS} does not exist")

    raw_before = SETTINGS.read_text()
    try:
        data = json.loads(raw_before)
    except json.JSONDecodeError as exc:
        fail(f"{SETTINGS} is not valid JSON: {exc}")

    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        fail(
            f"{SETTINGS} is missing {missing} — this looks like a degraded stub, "
            "not the real settings file. Refusing to edit. See "
            ".claude/rules/dotfiles-settings.md"
        )

    # Refuse to wire any hook whose script is not actually on disk at the path
    # the entry will name. A blocking hook that points at nothing permits.
    scripts = {w[2] for w in WIRINGS} | {"block_gws_delete.sh"}
    absent = sorted(s for s in scripts if not (HOOKS_DIR / s).exists())
    if absent:
        fail(
            f"these hook scripts are not present in {HOOKS_DIR}: {absent}\n"
            "They exist only in the worktree. Merge the hook scripts to the main "
            "checkout first — wiring them now would create dead entries."
        )
    not_exec = sorted(s for s in scripts if not os.access(HOOKS_DIR / s, os.X_OK))
    if not_exec:
        fail(f"these hook scripts are not executable: {not_exec}")

    stale = []
    for script, (marker, what) in CAPABILITY_MARKERS.items():
        if script not in scripts:
            continue
        if marker not in (HOOKS_DIR / script).read_text():
            stale.append(f"{script} (missing {what})")
    if stale:
        fail(
            "these deployed hook scripts predate the capability being wired:\n  "
            + "\n  ".join(stale)
            + f"\nThe file in {HOOKS_DIR} is an older version. Merge the updated "
            "script to the main checkout first — wiring a matcher to a hook that "
            "cannot handle it produces a blocker that silently permits."
        )

    hooks = data["hooks"]
    changes: list[str] = []

    # --- H3: repoint the dead Gmail matcher ---------------------------------
    for block in hooks.get("PreToolUse", []):
        if block.get("matcher") == DEAD_MATCHER:
            block["matcher"] = LIVE_MATCHER
            changes.append(f"PreToolUse: matcher {DEAD_MATCHER} -> {LIVE_MATCHER}")

    # --- H1/F1-F4: insert hook entries --------------------------------------
    for event, matcher, script, timeout, anchor in WIRINGS:
        blocks = hooks.setdefault(event, [])
        block = find_block(blocks, matcher)
        if block is None:
            fail(f"no {event} block found for matcher {matcher!r} — wire it by hand")
        if has_command(block, script):
            continue

        entries = block.setdefault("hooks", [])
        idx = len(entries)
        if anchor:
            for i, h in enumerate(entries):
                if anchor in h.get("command", ""):
                    idx = i
                    break
        entries.insert(idx, entry_for(script, timeout))
        where = f" before {anchor}" if anchor else ""
        changes.append(f"{event}[{matcher}]: + {script}{where}")

    # --- H2: matchers that make the MCP branch reachable --------------------
    pre = hooks.setdefault("PreToolUse", [])
    for tool in MCP_DELETE_TOOLS:
        block = find_block(pre, tool)
        if block and has_command(block, "block_gws_delete.sh"):
            continue
        if block is None:
            block = {"matcher": tool, "hooks": []}
            pre.append(block)
        block.setdefault("hooks", []).append(entry_for("block_gws_delete.sh", 5))
        changes.append(f"PreToolUse[{tool}]: + block_gws_delete.sh")

    if not changes:
        print("Already wired — nothing to do.")
        return

    print(f"{'Applying' if args.apply else 'Would apply'} {len(changes)} change(s) to {SETTINGS}:")
    for c in changes:
        print(f"  - {c}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return

    # The file is dual-written: bail if it changed while we were computing.
    if SETTINGS.read_text() != raw_before:
        fail("settings.json changed on disk mid-run (dual-write). Re-run.")

    backup = SETTINGS.with_suffix(f".json.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(SETTINGS, backup)

    # Write to a temp file in the same dir, then atomically replace, so a
    # concurrent reader never sees a half-written file. Resolve the symlink
    # first — os.replace on the link path would replace the link itself.
    target = SETTINGS.resolve()
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, target)

    print(f"\nWritten. Backup: {backup}")
    print("Restart Claude Code (or start a new session) for the hooks to load.")


if __name__ == "__main__":
    main()
