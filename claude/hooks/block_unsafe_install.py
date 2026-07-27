#!/usr/bin/env python3
"""Global PreToolUse(Bash) hook: BLOCKS the four hard supply-chain gates.

These are the prohibitions from rules/supply-chain-security.md that were
previously prose-only. Prose asks the model to refrain; this refuses.

Blocks:
  1. Third-party Homebrew taps    — `brew tap owner/repo`, `brew install owner/repo/formula`
  2. Arbitrary URL / git installs — pip/uv/npm/pnpm/bun/yarn installing from
                                    git+, github:, or an http(s) package URL
  3. `--ignore-scripts=false`     — re-enabling npm lifecycle scripts
  4. `--no-quarantine`            — disabling Gatekeeper on a cask

Allows: `--help`, `--dry-run`, local paths, editable installs, `-r requirements.txt`,
and index/registry flags (a custom index is a real vector but is NOT one of the four
named gates — keeping the gate precise avoids blocking `pip install -i <mirror> pkg`).

Each block names the approval path, because all four are overridable by the user
saying so explicitly — the hook stops the *unilateral* action, not the action.

Exit 0 = allow, exit 2 = block.
"""

import json
import re
import shlex
import sys

# Flags whose *value* is a URL by design — skip the following token when scanning
# for package arguments, so a mirror/index URL is not mistaken for a package URL.
VALUE_TAKING_FLAGS = {
    "-i", "--index-url", "--extra-index-url", "--find-links", "-f",
    "--trusted-host", "--proxy", "--registry", "--cert", "--client-cert",
    "-r", "--requirement", "-c", "--constraint", "--config-settings",
    "--python", "-p", "--prefix", "--target", "-t", "--cache-dir",
}

REMOTE_PKG_RE = re.compile(
    r"^(git\+|github:|gitlab:|bitbucket:|https?://|git://|ssh://|file://)", re.IGNORECASE
)
ARCHIVE_URL_RE = re.compile(r"^https?://.*\.(tgz|tar\.gz|whl|zip)$", re.IGNORECASE)
# owner/repo/formula — a tapped formula reference
TAP_FORMULA_RE = re.compile(r"^[\w.-]+/[\w.-]+/[\w.@+-]+$")

SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[;\n|]")

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
# Wrappers that prefix a real command without changing what it does. Any gate
# keyed on tokens[0] fails OPEN without stripping these: `sudo brew tap o/r` and
# `HOMEBREW_NO_AUTO_UPDATE=1 brew install o/r/f` both walk straight past a check
# that stops the bare form.
PREFIX_CMDS = {"sudo", "doas", "env", "command", "nohup", "nice", "stdbuf", "time"}
# Wrapper flags that consume the NEXT token (sudo -u USER, env -u NAME).
PREFIX_VALUE_FLAGS = {"-u", "--user", "-g", "--group", "-n", "--nice"}


def block(title: str, detail: str, approval: str) -> None:
    print(f"BLOCKED: {title}", file=sys.stderr)
    print(detail, file=sys.stderr)
    print(f"To proceed: {approval}", file=sys.stderr)
    sys.exit(2)


def strip_prefix(tokens: list[str]) -> list[str]:
    """Drop leading env assignments and wrapper commands, returning the real
    command. Loops, so `sudo env VAR=1 brew ...` reduces to `brew ...`."""
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        if tok.rsplit("/", 1)[-1] in PREFIX_CMDS:
            i += 1
            while i < n and tokens[i].startswith("-"):  # the wrapper's own flags
                if tokens[i] in PREFIX_VALUE_FLAGS and i + 1 < n:
                    i += 1
                i += 1
            continue
        break
    return tokens[i:]


def installer_of(tokens: list[str]) -> str | None:
    """Return a normalised installer name if this segment installs packages."""
    rest = strip_prefix(tokens)
    if not rest:
        return None

    head, args = rest[0], rest[1:]
    head = head.rsplit("/", 1)[-1]  # /usr/bin/pip3 -> pip3

    if head in ("pip", "pip3") and args and args[0] == "install":
        return "pip"
    if head.startswith("python") and args[:3] == ["-m", "pip", "install"]:
        return "pip"
    if head == "uv":
        if args[:2] == ["pip", "install"] or (args and args[0] == "add"):
            return "pip"
    if head in ("npm", "pnpm", "yarn", "bun") and args:
        if args[0] in ("install", "i", "add"):
            return "node"
    return None


def check_segment(tokens: list[str]) -> None:
    if not tokens:
        return
    if "--help" in tokens or "-h" in tokens or "--dry-run" in tokens:
        return

    # --- Gate 4: Gatekeeper bypass (applies to any command) ---
    if any(t == "--no-quarantine" or t.startswith("--no-quarantine=") for t in tokens):
        block(
            "`--no-quarantine` disables Gatekeeper.",
            "Notarization + quarantine are the defense against a malicious cask.",
            "ask the user; this flag is never used unattended "
            "(rules/supply-chain-security.md).",
        )

    # --- Gate 3: npm lifecycle scripts re-enabled ---
    for idx, tok in enumerate(tokens):
        if tok == "--ignore-scripts=false" or (
            tok == "--ignore-scripts" and idx + 1 < len(tokens) and tokens[idx + 1] == "false"
        ):
            block(
                "`--ignore-scripts=false` re-enables package lifecycle scripts.",
                "Global ~/.npmrc sets ignore-scripts=true as a supply-chain defense; "
                "postinstall scripts are the main npm attack vector.",
                "get explicit user confirmation for this specific package.",
            )

    # Gates 3 and 4 scan every token, so a wrapper prefix cannot hide a flag
    # from them. Gates 1 and 2 key on the command NAME, so they must look past
    # `sudo` / `VAR=value` / `env` first or they silently permit the wrapped form.
    rest = strip_prefix(tokens)

    # --- Gate 1: third-party Homebrew taps ---
    head = rest[0].rsplit("/", 1)[-1] if rest else ""
    if head == "brew" and len(rest) > 1:
        sub = rest[1]
        positional = [t for t in rest[2:] if not t.startswith("-")]
        if sub == "tap" and positional:
            block(
                f"third-party Homebrew tap `{positional[0]}`.",
                "Only official core formulae/casks and Mac App Store apps are allowed; "
                "a tap is unreviewed third-party code.",
                "get explicit user approval before adding any tap.",
            )
        if sub in ("install", "reinstall", "upgrade"):
            # `positional` already excludes flags, so --cask needs no special case.
            # Skipping a leading element here would silently exempt the first
            # package from the tap check — check every positional.
            for t in positional:
                if TAP_FORMULA_RE.match(t):
                    block(
                        f"install from a third-party tap (`{t}`).",
                        "An owner/repo/formula reference installs from an unreviewed tap.",
                        "get explicit user approval, or use the official core formula.",
                    )

    # --- Gate 2: arbitrary URL / git installs ---
    installer = installer_of(tokens)
    if installer:
        skip_next = False
        for tok in rest[1:]:
            if skip_next:
                skip_next = False
                continue
            if tok in VALUE_TAKING_FLAGS:
                skip_next = True
                continue
            if tok.startswith("-"):
                continue
            if REMOTE_PKG_RE.match(tok) or ARCHIVE_URL_RE.match(tok):
                block(
                    f"install from an arbitrary URL or git repo (`{tok}`).",
                    "Packages from URLs/git bypass the registry, the 7-day "
                    "min-release-age quarantine, and OSV malware checks.",
                    "get explicit user approval, or install the published "
                    "registry package instead.",
                )


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    inp = data.get("tool_input", data) or {}
    command = inp.get("command", "") or ""
    if not command.strip():
        sys.exit(0)

    for segment in SEGMENT_SPLIT_RE.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        check_segment(tokens)

    sys.exit(0)


if __name__ == "__main__":
    main()
