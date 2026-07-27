#!/usr/bin/env python3
"""Tests for wire_harness_hooks.py.

The applier is a blocker, and every way a blocker can be wrong degrades to
"silently permits": a missing script, a non-executable script, or — the case
that motivated this suite — a script that is PRESENT but is an older version
that cannot handle the matcher being wired. So the assertions here are mostly
about the applier REFUSING, not about it succeeding.

Hermetic: synthesizes its own hooks dir and settings.json. Never touches
~/.claude. Run directly:

    python3 scripts/setup/test_wire_harness_hooks.py
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent

# importlib rather than sys.path.insert — the latter is banned repo-wide and
# has crashed Claude Code sessions.
_spec = importlib.util.spec_from_file_location("wh", HERE / "wire_harness_hooks.py")
wh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wh)

PASS = 0
FAIL = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {desc}" + (f" ({detail})" if detail else ""))


def writable_tmp() -> str:
    """$TMPDIR is not reliably writable — sandboxes pin it to a read-only
    runtime dir and can mount /tmp read-only too."""
    for cand in (os.environ.get("TMPDIR"), "/tmp/claude", "/tmp", "."):
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            return tempfile.mkdtemp(dir=cand, prefix="wire-test.")
        except OSError:
            continue
    raise SystemExit("no writable temp dir found")


# --- fixtures ---------------------------------------------------------------

# The applier checks content, so a stub only needs the marker (or not).
FRESH = {
    "block_gws_delete.sh": "#!/bin/sh\ncase $TOOL in mcp__*Google_Calendar__delete_event) exit 2;; esac\n",
    "block_unsafe_install.py": "#!/usr/bin/env python3\n# blocks --no-quarantine\n",
}
STALE = {
    # The real pre-MCP hook: command-only, returns early on MCP input.
    "block_gws_delete.sh": '#!/bin/sh\n[ -z "$CMD" ] && exit 0\n',
    "block_unsafe_install.py": "#!/usr/bin/env python3\n# older gates only\n",
}


def make_hooks_dir(root: Path, stale: tuple = (), omit: tuple = (), nonexec: tuple = ()) -> Path:
    hooks = root / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    names = {w[2] for w in wh.WIRINGS} | {"block_gws_delete.sh", "warn_dep_install.sh"}
    for name in names:
        if name in omit:
            continue
        body = (STALE if name in stale else FRESH).get(name, "#!/bin/sh\nexit 0\n")
        path = hooks / name
        path.write_text(body)
        path.chmod(0o644 if name in nonexec else 0o755)
    return hooks


def make_settings(root: Path, drop_key: str | None = None) -> Path:
    data = {
        "statusLine": {"type": "command", "command": "statusline.sh"},
        "permissions": {"deny": []},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "$HOME/.claude/hooks/block_gws_delete.sh"},
                        {"type": "command", "command": "$HOME/.claude/hooks/warn_dep_install.sh"},
                    ],
                },
                {"matcher": "Write", "hooks": []},
                {"matcher": wh.DEAD_MATCHER, "hooks": [
                    {"type": "command", "command": "$HOME/.claude/hooks/nudge_html_email.sh"}]},
            ],
            "PostToolUse": [{"matcher": "Write|Edit", "hooks": []}],
            "Stop": [{"hooks": []}],
        },
    }
    if drop_key:
        del data[drop_key]
    path = root / "settings.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def run(hooks: Path, settings: Path, apply: bool = False):
    """Invoke main() with the module's paths redirected. Returns (rc, output)."""
    wh.HOOKS_DIR, wh.SETTINGS = hooks, settings
    argv, sys.argv = sys.argv, ["wire", "--apply"] if apply else ["wire"]
    buf = io.StringIO()
    rc = 0
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            wh.main()
    except SystemExit as exc:
        rc = exc.code or 0
    finally:
        sys.argv = argv
    return rc, buf.getvalue()


# --- tests ------------------------------------------------------------------

def main() -> None:
    root = Path(writable_tmp())
    try:
        # 1. The case that motivated the guard: present, executable, WRONG VERSION.
        d = root / "stale"
        stale_hooks = make_hooks_dir(d, stale=("block_gws_delete.sh",))
        stale_settings = make_settings(d)
        rc, out = run(stale_hooks, stale_settings)
        check("stale hook refuses", rc == 1, f"exit {rc}")
        check("stale hook names the capability", "predate the capability" in out, out[:120])
        check("stale hook names the file", "block_gws_delete.sh" in out)

        # 1b. Non-vacuity. The fixture above is caught ONLY by the content check —
        # the file exists and is executable, so it sails past every other gate.
        # Remove the markers and the same input must be ALLOWED; if it still
        # refuses, something else is catching it and this test proves nothing.
        saved, wh.CAPABILITY_MARKERS = wh.CAPABILITY_MARKERS, {}
        try:
            rc_bare, _ = run(stale_hooks, stale_settings)
        finally:
            wh.CAPABILITY_MARKERS = saved
        check("stale test is not vacuous", rc_bare == 0, f"exit {rc_bare} with the guard removed")

        # 2. Absent and non-executable also refuse.
        d = root / "absent"
        rc, out = run(make_hooks_dir(d, omit=("guard_existing_code.sh",)), make_settings(d))
        check("absent hook refuses", rc == 1, f"exit {rc}")
        check("absent hook names it", "guard_existing_code.sh" in out)

        d = root / "nonexec"
        rc, out = run(make_hooks_dir(d, nonexec=("nudge_synthetic_data.py",)), make_settings(d))
        check("non-executable refuses", rc == 1, f"exit {rc}")
        check("non-executable names it", "not executable" in out)

        # 3. Degraded settings stub is refused (rules/dotfiles-settings.md).
        d = root / "stub"
        rc, out = run(make_hooks_dir(d), make_settings(d, drop_key="statusLine"))
        check("degraded stub refuses", rc == 1, f"exit {rc}")
        check("stub mentions statusLine", "statusLine" in out)

        # 4. Healthy dry run: reports changes, writes NOTHING.
        d = root / "dry"
        hooks, settings = make_hooks_dir(d), make_settings(d)
        before = settings.read_text()
        rc, out = run(hooks, settings)
        check("dry run succeeds", rc == 0, f"exit {rc}")
        check("dry run says dry run", "Dry run" in out)
        check("dry run does not write", settings.read_text() == before)

        # 5. Apply: every wiring lands, ordering respected, matcher repointed.
        d = root / "apply"
        hooks, settings = make_hooks_dir(d), make_settings(d)
        rc, out = run(hooks, settings, apply=True)
        check("apply succeeds", rc == 0, f"exit {rc}: {out[:200]}")
        data = json.loads(settings.read_text())
        pre = data["hooks"]["PreToolUse"]

        bash = next(b for b in pre if b.get("matcher") == "Bash")
        cmds = [h["command"] for h in bash["hooks"]]
        blocker = next(i for i, c in enumerate(cmds) if "block_unsafe_install.py" in c)
        anchor = next(i for i, c in enumerate(cmds) if "warn_dep_install.sh" in c)
        check("blocker inserted before its anchor", blocker < anchor, f"{blocker} vs {anchor}")

        matchers = [b.get("matcher") for b in pre]
        check("dead matcher repointed", wh.DEAD_MATCHER not in matchers)
        check("live matcher present", wh.LIVE_MATCHER in matchers)
        for tool in wh.MCP_DELETE_TOOLS:
            block = next((b for b in pre if b.get("matcher") == tool), None)
            check(f"MCP matcher wired: {tool.rsplit('__', 1)[-1]}", block is not None)
            if block:
                check(
                    f"MCP matcher points at the hook: {tool.rsplit('__', 1)[-1]}",
                    any("block_gws_delete.sh" in h["command"] for h in block["hooks"]),
                )
        stop = data["hooks"]["Stop"][0]
        check("Stop hook wired", any("nudge_number_provenance.py" in h["command"] for h in stop["hooks"]))
        check("backup written", any(p.name.startswith("settings.json.bak.") for p in d.iterdir()))

        # 6. Idempotent: a second apply is a no-op, not a duplicate.
        rc, out = run(hooks, settings, apply=True)
        check("re-apply is a no-op", rc == 0 and "Already wired" in out, out[:120])
        again = json.loads(settings.read_text())
        bash2 = next(b for b in again["hooks"]["PreToolUse"] if b.get("matcher") == "Bash")
        check(
            "no duplicate entries",
            sum("block_unsafe_install.py" in h["command"] for h in bash2["hooks"]) == 1,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\nResults: {PASS} passed, {FAIL} failed (total {PASS + FAIL})")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
