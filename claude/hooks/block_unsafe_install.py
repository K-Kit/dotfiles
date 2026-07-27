#!/usr/bin/env python3
"""Global PreToolUse(Bash) hook: BLOCKS the four hard supply-chain gates.

These are the prohibitions from rules/supply-chain-security.md that were
previously prose-only. Prose asks the model to refrain; this refuses.

Blocks:
  1. Third-party Homebrew taps    — `brew tap owner/repo`, `brew install owner/repo/formula`
  2. Arbitrary URL / git installs — pip/uv/npm/pnpm/bun/yarn installing from
                                    git+, github:, an http(s) package URL, or
                                    any of npm's scheme-less git spellings
  3. `--ignore-scripts=false`     — re-enabling npm lifecycle scripts, in any
                                    of its flag, negation, and env spellings
  4. `--no-quarantine`            — disabling Gatekeeper on a cask

Allows: `--help`, `--dry-run`, local paths, editable installs, a LOCAL
`-r requirements.txt`, official `homebrew/*` taps and formulae, and
index/registry/mirror flags (a custom index is a real vector but is NOT one of
the four named gates — keeping the gate precise avoids blocking
`pip install -i <mirror> pkg`).

Each block names the approval path, because all four are overridable by the user
saying so explicitly — the hook stops the *unilateral* action, not the action.

DESIGN: fail CLOSED on ambiguity, and never trust a *representation* of the
command in place of the command. Four separate fail-open bugs all came from that
one mistake, so each is now structural rather than patched:

  * Wrapper flags — `sudo -n` is boolean, `nice -n` takes a value, and the two
    are token-identical. No skip table can tell them apart, so we do not try:
    every plausible command start is tested (`candidate_starts`).
  * Safe flags — `bash -c '<payload>' --help` gives `--help` to the wrapper as
    `$0`; bash still executes the payload. So nested strings are extracted and
    checked BEFORE any --help/--dry-run exemption can apply, and the exemption
    only ever covers the segment that literally carries it.
  * Shell options — `bash -lc`, `sh -ec` execute their string exactly as
    `bash -c` does. Any single-dash cluster containing `c` is treated as
    introducing a nested command.
  * Flag arity — `-f`/`-p` take values for pip and are booleans for npm, so one
    shared table silently skips over an npm package. Tables are per-installer.

Quoting is likewise never matched against raw text: the command is lexed with
shlex (punctuation-aware) so `de""lete`, `+se""nd`, and a `;` inside a quoted
URL are all resolved the way the shell would resolve them.

Over-blocking prints a message naming the approval path; under-blocking is
silent permission. When in doubt, block.

Exit 0 = allow, exit 2 = block.
"""

import json
import re
import shlex
import sys

# --- Gate 2 tables -----------------------------------------------------------
# Per-installer, because arity is installer-specific. npm's -f is --force and
# its -p is --parseable (both boolean); pip's -f is --find-links and its -p is
# --python (both take a value). A shared table skips the token after npm's -f,
# which is exactly where the remote package sits.
PIP_URL_BY_DESIGN = {
    "-i", "--index-url", "--extra-index-url", "--find-links", "-f",
    "--trusted-host", "--proxy", "--cert", "--client-cert",
}
PIP_PATH_VALUE = {
    "--config-settings", "--python", "-p", "--prefix", "--target", "-t",
    "--cache-dir", "--build-dir", "--src",
}
NODE_URL_BY_DESIGN = {
    "--registry", "--proxy", "--https-proxy", "--cert", "--cafile", "--ca",
}
NODE_PATH_VALUE = {"--prefix", "--cache", "--cwd", "-C", "--userconfig", "--globalconfig"}

FLAG_TABLES = {
    "pip": (PIP_URL_BY_DESIGN, PIP_PATH_VALUE),
    "node": (NODE_URL_BY_DESIGN, NODE_PATH_VALUE),
}

# Flags taking a path value that MUST still be checked: a remote requirements or
# constraints file is precisely the arbitrary-URL-install vector.
CHECKED_VALUE_FLAGS = {"-r", "--requirement", "-c", "--constraint"}

REMOTE_PKG_RE = re.compile(
    r"^(git\+|github:|gitlab:|bitbucket:|gist:|https?://|git://|ssh://|file://)",
    re.IGNORECASE,
)
ARCHIVE_URL_RE = re.compile(r"^https?://.*\.(tgz|tar\.gz|whl|zip)$", re.IGNORECASE)

# npm accepts git dependencies in spellings that carry no URL scheme at all.
# `npm i foo/bar` is documented GitHub shorthand; `git@host:path` is an scp-style
# git URL; `alias@github:owner/repo` embeds the host after the alias separator.
NPM_HOST_ALIAS_RE = re.compile(r"(^|@)(github|gitlab|bitbucket|gist):", re.IGNORECASE)
NPM_SCP_GIT_RE = re.compile(r"^[\w.-]+@[\w.-]+:[^/].*$")
NPM_SHORTHAND_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

# owner/repo/formula — a tapped formula reference
TAP_FORMULA_RE = re.compile(r"^[\w.-]+/[\w.-]+/[\w.@+-]+$")
# Taps under the homebrew org are official sources, explicitly allowed by policy.
OFFICIAL_TAP_RE = re.compile(r"^homebrew/", re.IGNORECASE)

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
SAFE_FLAGS = {"--help", "-h", "--dry-run"}
OPERATOR_CHARS = set(";&|\n()")

# Shells whose `-c STRING` argument is a whole nested command. shlex keeps that
# string as ONE token, so without re-entering it `bash -c "pip install git+..."`
# is invisible to every token-level gate.
SHELL_CMDS = {"bash", "sh", "zsh", "dash", "ksh", "ash"}
MAX_NEST_DEPTH = 4

NODE_INSTALLERS = {"npm", "pnpm", "yarn", "bun"}
INSTALL_SUBCMDS = {"install", "i", "add"}


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


def nested_command_strings(tokens: list[str]) -> list[str]:
    """Strings a shell wrapper will execute as a whole command.

    Recognises option clusters, not just a standalone `-c`: `bash -lc CMD` and
    `sh -ec CMD` run CMD exactly as `bash -c CMD` does, and matching only the
    exact token `-c` let both through.
    """
    out: list[str] = []
    for idx, tok in enumerate(tokens):
        if basename(tok) not in SHELL_CMDS:
            continue
        for j in range(idx + 1, len(tokens)):
            t = tokens[j]
            if t == "--" or t.startswith("--"):
                continue
            if t.startswith("-") and len(t) > 1:
                if "c" in t[1:] and j + 1 < len(tokens):
                    out.append(tokens[j + 1])
                    break
                continue
            break  # a positional before any -c: this is not a -c invocation
    return out


def installer_of(rest: list[str]) -> str | None:
    """Return a normalised installer name if this token run installs packages.

    Locates the subcommand by SEARCHING the arguments rather than reading
    args[0]. Installer-global options legitimately precede the subcommand
    (`npm --prefix /tmp install`, `pip --isolated install`, `uv --quiet add`),
    and `uv tool install` puts a noun there. Searching needs no arity model and
    over-approximates toward scanning, which is the safe direction.
    """
    if not rest:
        return None
    head, args = basename(rest[0]), rest[1:]

    if head in ("pip", "pip3"):
        return "pip" if "install" in args else None
    if head.startswith("python") and "-m" in args:
        k = args.index("-m")
        if args[k + 1 : k + 2] == ["pip"] and "install" in args[k + 2 :]:
            return "pip"
        return None
    if head == "uv":
        # `uv pip install`, `uv add`, and `uv tool install` all fetch packages.
        return "pip" if ("install" in args or "add" in args) else None
    if head in NODE_INSTALLERS:
        return "node" if any(a in INSTALL_SUBCMDS for a in args) else None
    return None


def is_remote_pkg(tok: str, installer: str) -> bool:
    if REMOTE_PKG_RE.match(tok) or ARCHIVE_URL_RE.match(tok):
        return True
    if installer != "node":
        return False
    # A local path is not a git spec; npm distinguishes them by the leading char.
    if tok.startswith((".", "/", "~", "@")):
        return False
    return bool(
        NPM_HOST_ALIAS_RE.search(tok)
        or NPM_SCP_GIT_RE.match(tok)
        or NPM_SHORTHAND_RE.match(tok)
    )


def check_install_args(rest: list[str], installer: str) -> None:
    """Gate 2 over one installer invocation's arguments."""
    url_by_design, path_value = FLAG_TABLES[installer]
    skip_next = False
    check_next = False
    for tok in rest[1:]:
        if skip_next:
            skip_next = False
            continue
        if check_next:
            check_next = False
            # fall through to the remote check below for this value
        elif tok in url_by_design or tok in path_value:
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
        if is_remote_pkg(tok, installer):
            block(
                f"install from an arbitrary URL or git repo (`{tok}`).",
                "Packages from URLs/git bypass the registry, the 7-day "
                "min-release-age quarantine, and OSV malware checks.",
                "get explicit user approval, or install the published "
                "registry package instead.",
            )


def check_ignore_scripts(tokens: list[str]) -> None:
    """Gate 3, over every spelling npm honours.

    `--ignore-scripts=false`, `--ignore-scripts false`, the boolean-negation
    `--no-ignore-scripts`, and the `npm_config_*` environment form are all the
    same instruction to npm; accepting only the first two left two live paths.
    """
    for idx, tok in enumerate(tokens):
        low = tok.lower()
        hit = low in ("--ignore-scripts=false", "--no-ignore-scripts") or (
            low == "--ignore-scripts"
            and tokens[idx + 1 : idx + 2] == ["false"]
        )
        if not hit:
            name, sep, value = tok.partition("=")
            hit = (
                sep
                and name.lower() == "npm_config_ignore_scripts"
                and value.lower() in ("false", "0", "no", "")
            )
        if hit:
            block(
                "re-enabling package lifecycle scripts.",
                "Global ~/.npmrc sets ignore-scripts=true as a supply-chain defense; "
                "postinstall scripts are the main npm attack vector.",
                "get explicit user confirmation for this specific package.",
            )


def check_segment(tokens: list[str], depth: int) -> None:
    if not tokens:
        return

    # Nested shells FIRST. `bash -c '<payload>' --help` hands --help to the
    # wrapper as $0 and still executes the payload, so no outer flag may exempt
    # a nested command from inspection.
    if depth < MAX_NEST_DEPTH:
        for nested in nested_command_strings(tokens):
            check_command(nested, depth + 1)

    if SAFE_FLAGS & set(tokens):
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
    check_ignore_scripts(tokens)

    # Gates 3 and 4 scan every token, so no wrapper can hide a flag from them.
    # Gates 1 and 2 key on a command NAME, so they run over every candidate
    # start rather than trusting a wrapper-flag skip table (see module docstring).
    for rest in candidate_starts(tokens):
        head = basename(rest[0])

        # --- Gate 1: third-party Homebrew taps ---
        if head == "brew" and len(rest) > 1:
            sub = rest[1]
            positional = [t for t in rest[2:] if not t.startswith("-")]
            if sub == "tap" and positional and not OFFICIAL_TAP_RE.match(positional[0]):
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
                    if TAP_FORMULA_RE.match(t) and not OFFICIAL_TAP_RE.match(t):
                        block(
                            f"install from a third-party tap (`{t}`).",
                            "An owner/repo/formula reference installs from an unreviewed tap.",
                            "get explicit user approval, or use the official core formula.",
                        )

        # --- Gate 2: arbitrary URL / git installs ---
        installer = installer_of(rest)
        if installer:
            check_install_args(rest, installer)


def split_segments(command: str) -> list[list[str]]:
    """Lex once, then split on operators.

    Splitting the RAW string on `;`/`&&`/`|` first treats those characters as
    operators even inside quotes, so `pip install 'https://host/p.whl;param'`
    was torn into unbalanced fragments and its URL never matched. shlex with
    punctuation_chars emits operators as their own tokens while respecting
    quoting, so the URL survives intact as one token.
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        # Unbalanced quotes: fall back to a naive split rather than skipping the
        # command entirely, since skipping would be a silent permit.
        return [command.split()]

    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok and all(ch in OPERATOR_CHARS for ch in tok):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


def check_command(command: str, depth: int = 0) -> None:
    for tokens in split_segments(command):
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
