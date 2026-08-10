#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Tests for the Coder workspace templates in scripts/coder/.

There is no terraform or tofu on this machine, so `terraform validate` is not
available. The closest substitute, and what this does, is to render each
`.tftpl` the way `templatefile()` would, parse the result as cloud-init YAML,
and shellcheck the scripts embedded in it. That is what caught the `$$var`
escaping bug the first time round (CLAUDE.md Learnings, 2026-08-08).

Hermetic: no provider is contacted, no VM is created, nothing is pushed.

Assertions compare rendered output against literals rather than checking that a
key is merely "present". A presence check passes when a key keeps its name and
silently changes its value -- the failure this repo already hit with
`--tools ""` -> `--tools default` (CLAUDE.md Learnings, 2026-08-06).
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CODER = REPO / "scripts" / "coder"

# Values substituted for the templatefile() map. Deliberately distinctive so an
# assertion can tell "the template emitted this" from "the template emitted a
# literal that happens to look similar".
RENDER_VARS = {
    "username": "testowner",
    "home_device": "/dev/disk/by-id/scsi-0HC_Volume_99999999",
    "home_volume_label": "coder-home",
    "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAADUMMY owner@example.invalid",
    "init_script": base64.b64encode(b"#!/bin/sh\nexec coder agent\n").decode(),
    "coder_agent_token": "dummy-agent-token-3f9c1a5e",
}

PASS = 0
FAIL = 0


def pass_(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  ok   {name}")


def lose(name: str, expected: str = "", got: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL {name}")
    if expected:
        print(f"       expected: {expected}")
    if got:
        print(f"       got:      {got}")


def check(name: str, condition: bool, expected: str = "", got: str = "") -> bool:
    if condition:
        pass_(name)
    else:
        lose(name, expected, got)
    return condition


# --------------------------------------------------------------------------
# Minimal HCL and templatefile support
# --------------------------------------------------------------------------


def balanced(text: str, open_at: int) -> str:
    """Return the contents between the brace at open_at and its partner."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : i]
    raise AssertionError(f"unbalanced braces from offset {open_at}")


def block_body(text: str, header_re: str) -> str:
    """Return the brace-balanced body of the first block matching header_re."""
    m = re.search(header_re, text)
    if not m:
        raise AssertionError(f"no block matching {header_re!r}")
    return balanced(text, text.index("{", m.end() - 1))


def templatefile_keys(main_tf: str) -> set[str]:
    """Keys of the map passed to templatefile() in the VM resource.

    The first argument is a quoted path that itself contains ${path.module}, so
    the map opens at the first brace *after the comma*, not the first brace
    after the paren. Getting that wrong yields an empty key set, which makes the
    "no dead entries" direction of the parity check pass vacuously.
    """
    m = re.search(r"templatefile\(\s*\"[^\"]*\"\s*,\s*(?=\{)", main_tf)
    if not m:
        raise AssertionError("no templatefile(<path>, { ... }) call found")
    body = balanced(main_tf, m.end())
    return set(re.findall(r"^\s*([a-z_][a-z0-9_]*)\s*=", body, re.MULTILINE))


def template_refs(tftpl: str) -> set[str]:
    """Variables the template actually interpolates, ignoring $${ escapes."""
    without_escapes = tftpl.replace("$${", "\0").replace("%%{", "\0")
    return set(re.findall(r"\$\{\s*([a-z_][a-z0-9_]*)\s*\}", without_escapes))


def render(tftpl: str, variables: dict[str, str]) -> str:
    """Approximate templatefile(): ${x} substitutes, $${ and %%{ are escapes."""
    ESC_D, ESC_P = "\x00DOLLAR\x00", "\x00PCT\x00"
    out = tftpl.replace("$${", ESC_D).replace("%%{", ESC_P)

    def sub(m: re.Match[str]) -> str:
        key = m.group(1).strip()
        if key not in variables:
            raise AssertionError(f"template references undefined variable {key!r}")
        return variables[key]

    out = re.sub(r"\$\{([^{}]*)\}", sub, out)
    return out.replace(ESC_D, "${").replace(ESC_P, "%{")


@dataclass
class WrittenFile:
    path: str
    permissions: str
    content: str


def written_files(cloud_config: dict) -> dict[str, WrittenFile]:
    out: dict[str, WrittenFile] = {}
    for entry in cloud_config.get("write_files", []):
        out[entry["path"]] = WrittenFile(
            path=entry["path"],
            permissions=str(entry.get("permissions", "")),
            content=entry.get("content", ""),
        )
    return out


# --------------------------------------------------------------------------
# Per-template checks
# --------------------------------------------------------------------------


@dataclass
class Template:
    slug: str
    machine_param: str  # coder_parameter holding the VM size
    readme_default_row: str  # row label in the template README's parameter table
    mount_helper: str  # the write_files script that owns /home


TEMPLATES = [
    Template("hetzner-linux", "server_type", "Server type", "/usr/local/sbin/coder-home-mount"),
    Template("digitalocean-linux", "droplet_size", "Droplet size", "/usr/local/sbin/coder-home-prepare"),
]


def check_template(t: Template) -> tuple[str, dict[str, WrittenFile]]:
    d = CODER / t.slug
    main_tf = (d / "main.tf").read_text()
    raw = (d / "cloud-config.yaml.tftpl").read_text()

    print(f"\n{t.slug}")

    # -- the documented .tftpl footgun: $$var renders literally, it is not an
    # -- escape. Only $${ is. A $$ followed by a letter is always the bug.
    bad = re.findall(r"\$\$[A-Za-z_]", raw)
    check(
        "no $$var pseudo-escape in the template",
        not bad,
        expected="shell variables written with a single $",
        got=f"found {bad}",
    )

    # -- main.tf <-> template parity. templatefile() errors at apply time on a
    # -- variable the template uses but the map omits; this catches it at test
    # -- time, and catches the reverse (a map entry nothing consumes) too.
    passed_keys = templatefile_keys(main_tf)
    used_keys = template_refs(raw)
    # Both directions below are subset tests, and a subset test against an empty
    # set is a pass that means nothing. Pin the extraction itself first.
    check(
        "templatefile() map parsed out of main.tf",
        len(passed_keys) >= 4,
        expected="at least 4 keys in the templatefile() map",
        got=f"{sorted(passed_keys)}",
    )
    check(
        "every variable the template uses is passed by main.tf",
        used_keys <= passed_keys,
        expected="no unsatisfied references",
        got=f"missing from templatefile(): {sorted(used_keys - passed_keys)}",
    )
    check(
        "every variable main.tf passes is used by the template",
        passed_keys <= used_keys,
        expected="no dead entries in the templatefile() map",
        got=f"passed but unused: {sorted(passed_keys - used_keys)}",
    )

    rendered = render(raw, RENDER_VARS)

    check(
        "nothing left unrendered",
        "${" not in rendered,
        expected="no ${...} in the rendered output",
        got=repr(re.findall(r"\$\{[^}]*\}", rendered)[:3]),
    )

    cfg = yaml.safe_load(rendered)
    check("renders to a YAML mapping", isinstance(cfg, dict), got=type(cfg).__name__)
    files = written_files(cfg)

    # -- agent token hygiene. systemd exposes Environment= values to any local
    # -- user via `systemctl show`, whatever mode the unit file carries, so the
    # -- token has to live in an EnvironmentFile instead.
    token = RENDER_VARS["coder_agent_token"]
    unit = files.get("/etc/systemd/system/coder-agent.service")
    if check("writes the coder-agent unit", unit is not None) and unit:
        check(
            "agent token is not inline in the unit file",
            token not in unit.content,
            expected="no CODER_AGENT_TOKEN= literal in the unit",
            got="token found inline",
        )
        check(
            "unit reads the token from EnvironmentFile",
            "EnvironmentFile=/etc/coder-agent.env" in unit.content,
            expected="EnvironmentFile=/etc/coder-agent.env",
        )
        check(
            "unit runs the agent as the workspace owner",
            f"User={RENDER_VARS['username']}" in unit.content,
            expected=f"User={RENDER_VARS['username']}",
        )

    envfile = files.get("/etc/coder-agent.env")
    if check("writes the agent env file", envfile is not None) and envfile:
        check(
            "agent env file is root-only",
            envfile.permissions == "0600",
            expected="0600",
            got=envfile.permissions,
        )
        check(
            "agent env file carries the token",
            envfile.content.strip() == f"CODER_AGENT_TOKEN={token}",
            expected=f"CODER_AGENT_TOKEN={token}",
            got=envfile.content.strip(),
        )

    # -- the token must not leak into any other world-readable file either
    leaks = [
        f.path
        for f in files.values()
        if token in f.content and f.permissions != "0600"
    ]
    check(
        "token appears in no world-readable file",
        not leaks,
        expected="token confined to the 0600 env file",
        got=f"also in {leaks}",
    )

    # -- the owner's public key is the break-glass path, and is not a secret
    keyfile = files.get("/etc/coder-owner-key.pub")
    if check("writes the owner public key", keyfile is not None) and keyfile:
        check(
            "owner key carries the value Coder holds",
            RENDER_VARS["ssh_public_key"] in keyfile.content,
            expected=RENDER_VARS["ssh_public_key"],
            got=keyfile.content.strip(),
        )

    # -- the agent binary is written from the base64 the provider handed us
    init = files.get("/opt/coder/init")
    if check("writes the agent init script", init is not None) and init:
        check(
            "init script is executable",
            init.permissions == "0755",
            expected="0755",
            got=init.permissions,
        )

    # -- home persistence. The mount is nofail and the helper exits 0 on a
    # -- missing device, both deliberate, so the only thing standing between a
    # -- silently-ephemeral /home and lost work is the startup_script guard.
    agent_body = block_body(main_tf, r'resource\s+"coder_agent"\s+"main"')
    check(
        "agent startup script tests that /home is a mountpoint",
        "mountpoint -q" in agent_body,
        expected="mountpoint -q in coder_agent.startup_script",
    )
    check(
        "agent startup script fails when /home is not persistent",
        "exit 1" in agent_body,
        expected="exit 1 in coder_agent.startup_script",
    )
    check(
        "startup script stays non-blocking so a failure is visible, not locking",
        'startup_script_behavior = "non-blocking"' in agent_body,
        expected='startup_script_behavior = "non-blocking"',
    )

    helper = files.get(t.mount_helper)
    if check(f"writes {t.mount_helper}", helper is not None) and helper:
        check(
            "home helper seeds /etc/skel so a fresh volume has shell rc files",
            "/etc/skel" in helper.content,
            expected="an /etc/skel copy in the home helper",
        )
        check(
            "the skel copy never overwrites an existing home",
            "cp -rn" in helper.content,
            expected="cp -rn (no-clobber)",
        )

    # -- arch parity. coder_agent.arch is fixed, and an ARM machine type behind
    # -- the same parameter builds fine and never connects.
    check(
        "agent arch is amd64",
        'arch = "amd64"' in agent_body,
        expected='arch = "amd64"',
    )
    size_body = block_body(main_tf, rf'data\s+"coder_parameter"\s+"{t.machine_param}"')
    offered = re.findall(r'value\s*=\s*"([^"]+)"', size_body)
    arm = [v for v in offered if v.startswith("cax") or "arm" in v.lower()]
    check(
        "no ARM machine types offered while arch is amd64",
        not arm,
        expected="x86 sizes only",
        got=f"ARM sizes offered: {arm}",
    )

    # -- doc/code parity for the default size. Asserting the literal alone lets
    # -- the docs drift; asserting parity alone passes vacuously once the README
    # -- row stops matching the regex. Both, as with hz (Learnings 2026-08-08).
    m = re.search(r'^\s*default\s*=\s*"([^"]+)"', size_body, re.MULTILINE)
    default_size = m.group(1) if m else ""
    check(
        f"{t.machine_param} has a default",
        bool(default_size),
        expected="a default = ... line",
    )
    check(
        "the default is one of the offered options",
        default_size in offered,
        expected=f"one of {offered}",
        got=default_size,
    )

    readme = (d / "README.md").read_text()
    row = next(
        (ln for ln in readme.splitlines() if ln.startswith(f"| {t.readme_default_row} |")),
        "",
    )
    check(
        f"template README documents a {t.readme_default_row} default",
        bool(row),
        expected=f"a '| {t.readme_default_row} |' table row",
    )
    check(
        "template README default matches main.tf",
        f"`{default_size}`" in row,
        expected=f"`{default_size}` in the README row",
        got=row.strip(),
    )

    index = (CODER / "README.md").read_text()
    index_row = next(
        (ln for ln in index.splitlines() if f"[`{t.slug}/`]" in ln),
        "",
    )
    check(
        "scripts/coder/README.md lists the template",
        bool(index_row),
        expected=f"a row for {t.slug}/",
    )
    check(
        "scripts/coder/README.md default matches main.tf",
        f"`{default_size}`" in index_row,
        expected=f"`{default_size}` in the index row",
        got=index_row.strip(),
    )

    return rendered, files


def shellcheck_embedded(t: Template, files: dict[str, WrittenFile], tmp: Path) -> None:
    if not shutil.which("shellcheck"):
        print("  skip shellcheck (not installed)")
        return
    scripts = {p: f for p, f in files.items() if f.content.lstrip().startswith("#!")}
    check(
        "template embeds at least one shell script to check",
        bool(scripts),
        expected="a write_files entry starting with #!",
    )
    for path, f in scripts.items():
        target = tmp / (t.slug + path.replace("/", "_"))
        target.write_text(f.content)
        proc = subprocess.run(
            ["shellcheck", "--shell=bash", str(target)],
            capture_output=True,
            text=True,
        )
        check(
            f"shellcheck clean: {path}",
            proc.returncode == 0,
            expected="no findings",
            got=proc.stdout.strip() or proc.stderr.strip(),
        )


def main() -> int:
    import tempfile

    print("Coder template rendering")
    with tempfile.TemporaryDirectory(prefix="coder-tmpl-") as td:
        tmp = Path(td)
        for t in TEMPLATES:
            try:
                _, files = check_template(t)
                shellcheck_embedded(t, files, tmp)
            except AssertionError as exc:
                lose(f"{t.slug}: {exc}")
            except yaml.YAMLError as exc:
                lose(f"{t.slug}: rendered cloud-config is not valid YAML", got=str(exc))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
