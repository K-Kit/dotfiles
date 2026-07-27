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

Allows: `--help`, `--dry-run`, local paths, editable installs, a LOCAL
`-r requirements.txt`, and index/registry/mirror flags (a custom index is a real
vector but is NOT one of the four named gates — keeping the gate precise avoids
blocking `pip install -i <mirror> pkg`).

Each block names the approval path, because all four are overridable by the user
saying so explicitly — the hook stops the *unilateral* action, not the action.

DESIGN: fail CLOSED on ambiguity. Every earlier version of this hook keyed gates
1 and 2 on tokens[0] and tried to skip wrapper flags to find it. That is
unwinnable: `sudo -n` looks exactly like `nice -n`, but sudo's -n is a boolean
and nice's takes a value, so any single skip-table silently permits one of them.
Instead we test EVERY plausible command start. Over-blocking prints a message
naming the approval path; under-blocking is silent permission.

Exit 0 = allow, exit 2 = block.
"""

import json
import re
import shlex
import sys

# Flags whose value is a URL *by design* (mirrors, registries, proxies). Their
# value is skipped so a legitimate mirror is not read as a package URL.
URL_BY_DESIGN_FLAGS = {
    "-i", "--index-url", "--extra-index-url", "--find-links", "-f",
    "--trusted-host", "--proxy", "--registry", "--cert", "--client-cert",
}
# Flags taking a path value that MUST still be checked: a remote requirements or
# constraints file is precisely the arbitrary-URL-install vector.
CHECKED_VALUE_FLAGS = {"-r", "--requirement", "-c", "--constraint"}
# Flags taking a local path value, not a package source.
PATH_VALUE_FLAGS = {
    "--config-settings", "--python", "-p", "--prefix", "--target", "-t", "--cache-dir",
}

REMOTE_PKG_RE = re.compile(
    r"^(git\+|github:|gitlab:|bitbucket:|https?://|git://|ssh://|file://)", re.IGNORECASE
)
ARCHIVE_URL_RE = re.compile(r"^https?://.*\.(tgz|tar\.gz|whl|zip)$", re.IGNORECASE)
# owner/repo/formula — a tapped formula reference
TAP_FORMULA_RE = re.compile(r"^[\w.-]+/[\w.-]+/[\w.@+-]+$")

SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[;\n|]")

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
# Shells whose `-c STRING` argument is a whole nested command. shlex keeps that
# string as ONE token, so without re-entering it `bash -c "pip install git+..."`
# is invisible to every token-level gate.
SHELL_CMDS = {"bash", "sh", "zsh", "dash", "ksh", "ash"}
MAX_NEST_DEPTH = 4


def block(title: str, detail: str, approval: str) -> None:
    print(f"BLOCKED: {title}", file=sys.stderr)
    print(detail, file=sys.stderr)
    print(f"To proceed: {approval}", file=sys.stderr)
    sys.exit(2)


def basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def candidate_starts(tokens: list[str]) -> list[list[str]]:
    """Every suffix that could plausibly be the real command.

    Keying on tokens[0] requires correctly modelling each wrapper's flag
    arity; getting that wrong fails OPEN. Testing all suffixes needs no such
    model, so `sudo -n pip install URL`, `nice -n 10 pip install URL`, and
    `env A=1 doas -u x pip install URL` are all covered by construction.
    """
    out = [tokens]
    for i in range(1, len(tokens)):
        # A command name is never a flag or an env assignment.
        if tokens[i].startswith("-") or ENV_ASSIGN_RE.match(tokens[i]):
            continue
        out.append(tokens[i:])
    return out


def installer_of(rest: list[str]) -> str | None:
    """Return a normalised installer name if this token run installs packages."""
    if not rest:
        return None
    head, args = basename(rest[0]), rest[1:]

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


def check_install_args(rest: list[str]) -> None:
    """Gate 2 over one installer invocation's arguments."""
    skip_next = False
    check_next = False
    for tok in rest[1:]:
        if skip_next:
            skip_next = False
            continue
        if check_next:
            check_next = False
            # fall through to the remote check below for this value
        elif tok in URL_BY_DESIGN_FLAGS or tok in PATH_VALUE_FLAGS:
            skip_next = True
            continue
        elif tok in CHECKED_VALUE_FLAGS:
            check_next = True
            continue
        elif tok.startswith("-"):
            # --requirement=URL / --index-url=URL inline forms
            name, sep, value = tok.partition("=")
            if sep and name in CHECKED_VALUE_FLAGS and (
                REMOTE_PKG_RE.match(value) or ARCHIVE_URL_RE.match(value)
            ):
                block(
                    f"install from a remote requirements/constraints file (`{value}`).",
                    "A remote requirements file installs unreviewed pinned packages, "
                    "bypassing the registry, quarantine, and OSV checks.",
                    "download and review it locally first, or get explicit user approval.",
                )
            continue
        if REMOTE_PKG_RE.match(tok) or ARCHIVE_URL_RE.match(tok):
            block(
                f"install from an arbitrary URL or git repo (`{tok}`).",
                "Packages from URLs/git bypass the registry, the 7-day "
                "min-release-age quarantine, and OSV malware checks.",
                "get explicit user approval, or install the published "
                "registry package instead.",
            )


def check_segment(tokens: list[str], depth: int) -> None:
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

    # Gates 3 and 4 scan every token, so no wrapper can hide a flag from them.
    # Gates 1 and 2 key on a command NAME, so they run over every candidate
    # start rather than trusting a wrapper-flag skip table (see module docstring).
    for rest in candidate_starts(tokens):
        head = basename(rest[0])

        # --- Gate 1: third-party Homebrew taps ---
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
                # `positional` already excludes flags, so --cask needs no special
                # case. Skipping a leading element would silently exempt the
                # first package from the tap check — check every positional.
                for t in positional:
                    if TAP_FORMULA_RE.match(t):
                        block(
                            f"install from a third-party tap (`{t}`).",
                            "An owner/repo/formula reference installs from an unreviewed tap.",
                            "get explicit user approval, or use the official core formula.",
                        )

        # --- Gate 2: arbitrary URL / git installs ---
        if installer_of(rest):
            check_install_args(rest)

    # --- Nested shells: `bash -c "<whole command>"` ---
    if depth < MAX_NEST_DEPTH:
        for idx, tok in enumerate(tokens):
            if basename(tok) not in SHELL_CMDS:
                continue
            for j in range(idx + 1, len(tokens) - 1):
                if tokens[j] == "-c":
                    check_command(tokens[j + 1], depth + 1)
                    break


def check_command(command: str, depth: int = 0) -> None:
    for segment in SEGMENT_SPLIT_RE.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        check_segment(tokens, depth)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if not isinstance(data, dict):
        sys.exit(0)
    inp = data.get("tool_input", data)
    if not isinstance(inp, dict):
        sys.exit(0)
    command = inp.get("command", "") or ""
    if not command.strip():
        sys.exit(0)

    check_command(command)
    sys.exit(0)


if __name__ == "__main__":
    main()
