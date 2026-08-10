#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Mutation harness for tests/test_coder_templates.py.

A green suite proves nothing until you have watched it go red. Each mutation
below breaks one property the suite claims to protect, and the harness fails if
the suite stays green.

Every mutation is drawn from the *legal* domain -- a value the tool would
genuinely accept -- because an illegal value trips a membership check that a
legal-but-harmful one sails straight through (CLAUDE.md Learnings, 2026-08-06).
`cx23` is a real Hetzner type, `blocking` is a real startup_script_behavior,
`0644` is a real mode, `cp -r` is a real invocation. That is the point.

The working tree is never touched: scripts/coder and the test are copied into a
temp tree and mutated there, so a crash mid-run cannot leave a mutation behind.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITE = "tests/test_coder_templates.py"

HZ_TF = "scripts/coder/hetzner-linux/main.tf"
HZ_TPL = "scripts/coder/hetzner-linux/cloud-config.yaml.tftpl"
DO_TF = "scripts/coder/digitalocean-linux/main.tf"
DO_TPL = "scripts/coder/digitalocean-linux/cloud-config.yaml.tftpl"


@dataclass
class Mutation:
    description: str
    relpath: str
    old: str
    new: str


MUTATIONS = [
    Mutation(
        "default server type back to EU-only cx23",
        HZ_TF,
        'default = "cpx21"',
        'default = "cx23"',
    ),
    Mutation(
        "offer an ARM cax11 type while arch stays amd64",
        HZ_TF,
        'value = "cpx11"',
        'value = "cax11"',
    ),
    Mutation(
        "startup script blocks login instead of warning",
        HZ_TF,
        'startup_script_behavior = "non-blocking"',
        'startup_script_behavior = "blocking"',
    ),
    Mutation(
        "neuter the mountpoint guard in the startup script",
        HZ_TF,
        'if ! mountpoint -q "$home"; then',
        "if false; then",
    ),
    Mutation(
        "drop a variable from the templatefile map",
        HZ_TF,
        "    ssh_public_key    = data.coder_workspace_owner.me.ssh_public_key\n",
        "",
    ),
    Mutation(
        "put the agent token back inline in the unit file",
        HZ_TPL,
        "EnvironmentFile=/etc/coder-agent.env",
        "Environment=CODER_AGENT_TOKEN=${coder_agent_token}",
    ),
    Mutation(
        "loosen the agent env file to world-readable 0644",
        HZ_TPL,
        'permissions: "0600"',
        'permissions: "0644"',
    ),
    Mutation(
        "let the skel copy clobber an existing home",
        HZ_TPL,
        "cp -rn /etc/skel",
        "cp -r /etc/skel",
    ),
    Mutation(
        "reintroduce the $$var pseudo-escape footgun",
        HZ_TPL,
        'mountpoint -q "$home"',
        'mountpoint -q "$$home"',
    ),
    # The suite asserts the same properties for both templates, so mutating only
    # the Hetzner half would leave every DigitalOcean assertion unproven -- and an
    # unproven assertion is exactly where a vacuous pass hides. Same four
    # properties, same legal-domain rule.
    Mutation(
        "DO: drift the default droplet size away from the documented one",
        DO_TF,
        'default      = "s-1vcpu-2gb"',
        'default      = "s-2vcpu-4gb"',
    ),
    Mutation(
        "DO: drop a variable from the templatefile map",
        DO_TF,
        "    ssh_public_key    = data.coder_workspace_owner.me.ssh_public_key\n",
        "",
    ),
    Mutation(
        "DO: neuter the mountpoint guard in the startup script",
        DO_TF,
        'if ! mountpoint -q "$home"; then',
        "if false; then",
    ),
    Mutation(
        "DO: put the agent token back inline in the unit file",
        DO_TPL,
        "EnvironmentFile=/etc/coder-agent.env",
        "Environment=CODER_AGENT_TOKEN=${coder_agent_token}",
    ),
    Mutation(
        "DO: loosen the agent env file to world-readable 0644",
        DO_TPL,
        'permissions: "0600"',
        'permissions: "0644"',
    ),
    Mutation(
        "DO: let the skel copy clobber an existing home",
        DO_TPL,
        "cp -rn /etc/skel",
        "cp -r /etc/skel",
    ),
]


def stage(dest: Path) -> None:
    """Copy just enough of the repo for the suite to run against."""
    shutil.copytree(REPO / "scripts" / "coder", dest / "scripts" / "coder")
    (dest / "tests").mkdir(parents=True)
    shutil.copy2(REPO / SUITE, dest / SUITE)


def suite_passes(root: Path) -> bool:
    proc = subprocess.run(
        ["uv", "run", str(root / SUITE)],
        capture_output=True,
        text=True,
        cwd=root,
    )
    return proc.returncode == 0


def main() -> int:
    print("Mutation testing tests/test_coder_templates.py")

    with tempfile.TemporaryDirectory(prefix="coder-mutate-") as td:
        baseline = Path(td) / "baseline"
        stage(baseline)
        if not suite_passes(baseline):
            print("  ERROR  the unmutated suite already fails -- fix that first")
            return 1
        print("  ok     baseline suite passes on the staged copy")

        caught = 0
        missed = 0
        for m in MUTATIONS:
            root = Path(td) / f"m{caught + missed}"
            stage(root)
            target = root / m.relpath
            text = target.read_text()
            if m.old not in text:
                print(f"  ERROR  {m.description} (search text absent -- mutation is stale)")
                missed += 1
                continue
            target.write_text(text.replace(m.old, m.new, 1))

            if suite_passes(root):
                print(f"  MISSED {m.description} -- suite still passed")
                missed += 1
            else:
                print(f"  caught {m.description}")
                caught += 1

    print(f"\n{caught} caught, {missed} missed")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
