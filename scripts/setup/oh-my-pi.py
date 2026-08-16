#!/usr/bin/env python3
"""Install, update, inspect and remove oh-my-pi (the `omp` CLI) from GitHub releases.

Upstream is https://github.com/can1357/oh-my-pi (MIT). The project's own
installer is `curl -fsSL https://omp.sh/install | sh`, which this repo forbids
and which -- checked against the served script on 2026-08-15 -- performs no
checksum verification at all. This script instead resolves a release tag,
downloads the matching asset and the `SHA256SUMS.txt` published in that same
tag, and refuses to install on a digest mismatch.

Design notes worth knowing before editing:

* Nothing is executed. The downloaded artifact is never run, not even for a
  `--version` smoke test, so "is it installed and which build" is answered from
  a `metadata.json` sidecar written next to the binary rather than by asking
  the binary.
* Every subcommand is a dry run until `--apply`. The plan is built from the
  same (description, action) pairs that execution runs, so the two cannot
  drift.
* Shell startup files are never touched. PATH and completion setup are printed
  as instructions for the user to apply.
* `~/.omp` (upstream's config and credential directory) is never read, written
  or removed.

Usage:
    scripts/setup/oh-my-pi.py status
    scripts/setup/oh-my-pi.py install                 # dry run
    scripts/setup/oh-my-pi.py install --apply
    scripts/setup/oh-my-pi.py install --version v17.3.4 --apply
    scripts/setup/oh-my-pi.py update --apply
    scripts/setup/oh-my-pi.py uninstall --apply
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomllib

REPO = "can1357/oh-my-pi"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_BASE = f"https://github.com/{REPO}/releases/download"
BINARY_NAME = "omp"
SUMS_NAME = "SHA256SUMS.txt"
USER_AGENT = "dotfiles-oh-my-pi-installer"
HTTP_TIMEOUT = 60
# The release binaries are Bun single-file executables and run ~180 MB. The cap
# is a runaway-download guard, not a size estimate.
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
METADATA_NAME = "metadata.json"
METADATA_SCHEMA = 1

DEFAULT_INSTALL_DIR = Path("~/.local/share/oh-my-pi")
DEFAULT_BIN_DIR = Path("~/.local/bin")

# uname -> upstream asset vocabulary. Windows assets exist upstream and are
# deliberately unsupported here: this repo targets macOS, Linux and RunPod.
OS_MAP = {"linux": "linux", "darwin": "darwin"}
ARCH_MAP = {
    "x86_64": "x64",
    "amd64": "x64",
    "x64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
LIBC_CHOICES = ("auto", "gnu", "musl")


class OhMyPiError(RuntimeError):
    """A condition the user must resolve; reported without a traceback."""


class ConfigError(OhMyPiError):
    """Bad TOML config or bad CLI/TOML combination."""


# --------------------------------------------------------------------------
# platform detection
# --------------------------------------------------------------------------


def normalize_os(system: str) -> str:
    key = system.strip().lower()
    if key not in OS_MAP:
        raise OhMyPiError(
            f"unsupported operating system {system!r}; "
            f"oh-my-pi is installed here for: {', '.join(sorted(OS_MAP))}"
        )
    return OS_MAP[key]


def normalize_arch(machine: str) -> str:
    key = machine.strip().lower()
    if key not in ARCH_MAP:
        raise OhMyPiError(
            f"unsupported CPU architecture {machine!r}; "
            f"oh-my-pi publishes x64 and arm64 builds only"
        )
    return ARCH_MAP[key]


def _read_sysctl(name: str) -> str:
    """Read a macOS sysctl value, or "" when it cannot be read.

    Runs a *system* tool. The downloaded artifact is never executed.
    """
    for exe in ("sysctl", "/usr/sbin/sysctl"):
        try:
            out = subprocess.run(
                [exe, "-in", name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            return out.stdout.strip()
    return ""


def detect_arch(
    system: str,
    machine: str,
    sysctl: Callable[[str], str] = _read_sysctl,
) -> str:
    """Normalized arch, correcting for Rosetta on macOS.

    Under Rosetta 2 `uname -m` reports the translated `x86_64` on an arm64
    Mac, so an x64 build would be installed and run translated forever.
    Upstream's own installer disambiguates with `sysctl hw.optional.arm64`;
    this mirrors that.
    """
    if normalize_os(system) == "darwin" and sysctl("hw.optional.arm64") == "1":
        return "arm64"
    return normalize_arch(machine)


def detect_libc(root: Path = Path("/")) -> str:
    """Return "musl" or "gnu" by inspecting the filesystem only.

    `platform.libc_ver()` reads the running Python's ELF and comes back empty
    on musl often enough to be useless, so this checks the same signals
    upstream's shell installer checks: an Alpine marker file, or a musl
    dynamic loader.
    """
    if (root / "etc/alpine-release").exists():
        return "musl"
    lib = root / "lib"
    with contextlib.suppress(OSError):
        for entry in lib.iterdir():
            name = entry.name
            if name.startswith("ld-musl-") and name.endswith(".so.1"):
                return "musl"
    return "gnu"


def asset_name(os_name: str, arch: str, libc: str) -> str:
    """Release asset filename for a target.

    musl is a Linux-only distinction; a musl request on macOS is a config
    error rather than a silently ignored field.
    """
    if libc not in ("gnu", "musl"):
        raise OhMyPiError(f"libc must be resolved to gnu or musl, got {libc!r}")
    if libc == "musl":
        if os_name != "linux":
            raise OhMyPiError(f"musl builds exist for linux only, not {os_name}")
        return f"{BINARY_NAME}-linux-musl-{arch}"
    return f"{BINARY_NAME}-{os_name}-{arch}"


# --------------------------------------------------------------------------
# release metadata
# --------------------------------------------------------------------------


def normalize_tag(version: str) -> str:
    """Normalize a user-supplied version into a release tag.

    Rejects anything that could escape the URL path or the versions directory.
    """
    tag = version.strip()
    if not tag:
        raise OhMyPiError("version must not be empty")
    if tag == "latest":
        raise OhMyPiError("'latest' is resolved from the API, not treated as a tag")
    bad = set('/\\ \t?#%"\'')
    if any(ch in bad for ch in tag) or ".." in tag:
        raise OhMyPiError(f"refusing suspicious version string {version!r}")
    if not tag[0].isdigit() and not tag.startswith("v"):
        raise OhMyPiError(f"version {version!r} does not look like a release tag")
    return tag if tag.startswith("v") else f"v{tag}"


def release_urls(tag: str, asset: str) -> tuple[str, str]:
    """(asset_url, sums_url) for one tag.

    Both come from the same tag by construction: verifying a v17.3.4 binary
    against a v17.3.0 checksum file is exactly the mistake this prevents.
    """
    return (f"{DOWNLOAD_BASE}/{tag}/{asset}", f"{DOWNLOAD_BASE}/{tag}/{SUMS_NAME}")


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse coreutils `sha256sum` output into {filename: digest}."""
    sums: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise OhMyPiError(f"{SUMS_NAME} line {lineno} is malformed: {raw!r}")
        digest, name = parts[0].lower(), parts[1].strip()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise OhMyPiError(f"{SUMS_NAME} line {lineno} has a bad digest: {raw!r}")
        name = name.lstrip("*")  # binary-mode marker
        if not name:
            raise OhMyPiError(f"{SUMS_NAME} line {lineno} has no filename: {raw!r}")
        sums[name] = digest
    if not sums:
        raise OhMyPiError(f"{SUMS_NAME} contained no usable entries")
    return sums


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _urlopen(url: str) -> Iterator[Any]:
    if not url.startswith("https://"):
        raise OhMyPiError(f"refusing non-https URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            yield response
    except urllib.error.HTTPError as exc:
        raise OhMyPiError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise OhMyPiError(f"network error fetching {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise OhMyPiError(f"timed out after {HTTP_TIMEOUT}s fetching {url}") from exc


def fetch_text(url: str) -> str:
    with _urlopen(url) as response:
        raw = response.read(MAX_DOWNLOAD_BYTES)
    return raw.decode("utf-8", errors="replace")


def resolve_latest_tag() -> str:
    payload = json.loads(fetch_text(API_LATEST))
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise OhMyPiError(f"{API_LATEST} returned no tag_name")
    return normalize_tag(tag)


def download_to(url: str, dest: Path) -> int:
    """Stream a URL to a file, capped. Returns bytes written."""
    written = 0
    with _urlopen(url) as response, dest.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_DOWNLOAD_BYTES:
                raise OhMyPiError(
                    f"{url} exceeded the {MAX_DOWNLOAD_BYTES} byte download cap"
                )
            out.write(chunk)
    if written == 0:
        raise OhMyPiError(f"{url} returned an empty body")
    return written


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Config:
    install_dir: Path = DEFAULT_INSTALL_DIR
    bin_dir: Path = DEFAULT_BIN_DIR
    version: str = "latest"
    libc: str = "auto"
    os_name: str | None = None
    arch: str | None = None
    allow_unverified: bool = False


TOML_MAP: dict[str, str] = {
    "install.install_dir": "install_dir",
    "install.bin_dir": "bin_dir",
    "install.version": "version",
    "platform.libc": "libc",
    "platform.os": "os_name",
    "platform.arch": "arch",
    "security.allow_unverified": "allow_unverified",
}

FIELD_TYPES: dict[str, type] = {
    "install_dir": str,
    "bin_dir": str,
    "version": str,
    "libc": str,
    "os_name": str,
    "arch": str,
    "allow_unverified": bool,
}

# A config file that *can* hold a secret eventually will. oh-my-pi's own
# credentials live in ~/.omp/agent/*.yml, which this script never touches, so
# any secret-shaped key here is a mistake worth failing on.
# Same list as scripts/setup/remote-desktop.py. Deliberately no bare "key": it
# is a substring of too many legitimate names to use as a substring match.
SECRET_KEY_NAMES = ("password", "passwd", "secret", "token", "passphrase")


def flatten_toml(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten_toml(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def config_from_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file into {field: value}, rejecting anything unexpected."""
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    overrides: dict[str, Any] = {}
    for dotted, value in flatten_toml(data).items():
        leaf = dotted.rsplit(".", 1)[-1].lower()
        if any(marker in leaf for marker in SECRET_KEY_NAMES):
            raise ConfigError(
                f"{path}: key {dotted!r} looks like a secret; this installer "
                "never handles credentials -- remove it"
            )
        if dotted not in TOML_MAP:
            raise ConfigError(
                f"{path}: unknown key {dotted!r} "
                f"(known: {', '.join(sorted(TOML_MAP))})"
            )
        field = TOML_MAP[dotted]
        want = FIELD_TYPES[field]
        # bool first: bool is a subclass of int, and str checks must not
        # accept a bare true/false.
        if want is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{path}: {dotted} must be a boolean")
        elif isinstance(value, bool) or not isinstance(value, want):
            raise ConfigError(f"{path}: {dotted} must be a {want.__name__}")
        overrides[field] = value
    return overrides


def _validate(config: Config) -> Config:
    if config.libc not in LIBC_CHOICES:
        raise ConfigError(
            f"libc must be one of {', '.join(LIBC_CHOICES)}, got {config.libc!r}"
        )
    if config.os_name is not None and config.os_name not in set(OS_MAP.values()):
        raise ConfigError(
            f"os must be one of {', '.join(sorted(set(OS_MAP.values())))}, "
            f"got {config.os_name!r}"
        )
    if config.arch is not None and config.arch not in set(ARCH_MAP.values()):
        raise ConfigError(
            f"arch must be one of {', '.join(sorted(set(ARCH_MAP.values())))}, "
            f"got {config.arch!r}"
        )
    if config.version != "latest":
        normalize_tag(config.version)  # raises on anything unsafe
    return config


def resolve_config(
    toml_overrides: dict[str, Any] | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> Config:
    """Layer defaults < TOML < CLI. Only non-None CLI values override."""
    values: dict[str, Any] = {}
    values.update(toml_overrides or {})
    for field, value in (cli_overrides or {}).items():
        if value is not None:
            values[field] = value

    base = Config()
    kwargs: dict[str, Any] = {}
    for field in (f.name for f in dataclasses.fields(Config)):
        if field not in values:
            kwargs[field] = getattr(base, field)
            continue
        raw = values[field]
        if field in ("install_dir", "bin_dir"):
            kwargs[field] = Path(str(raw)).expanduser()
        else:
            kwargs[field] = raw
    return _validate(Config(**kwargs))


# --------------------------------------------------------------------------
# on-disk layout
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Layout:
    root: Path
    bin_dir: Path

    @property
    def versions(self) -> Path:
        return self.root / "versions"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def launcher(self) -> Path:
        return self.bin_dir / BINARY_NAME

    def version_dir(self, tag: str) -> Path:
        return self.versions / tag

    def binary(self, tag: str) -> Path:
        return self.version_dir(tag) / BINARY_NAME

    def metadata_path(self, tag: str) -> Path:
        return self.version_dir(tag) / METADATA_NAME

    def installed_tags(self) -> list[str]:
        if not self.versions.is_dir():
            return []
        return sorted(p.name for p in self.versions.iterdir() if p.is_dir())

    def current_tag(self) -> str | None:
        if not self.current.is_symlink():
            return None
        target = os.readlink(self.current)
        name = Path(target).name
        return name or None


def read_metadata(layout: Layout, tag: str) -> dict[str, Any] | None:
    path = layout.metadata_path(tag)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_installed(layout: Layout, tag: str, expected_sha: str | None = None) -> bool:
    """True when this tag is present, recorded, and (if known) digest-matched.

    "A file with the right name exists" is not enough: without the sidecar we
    could not tell a complete install from a truncated download, and we are
    not allowed to run the binary to ask.
    """
    binary = layout.binary(tag)
    if not binary.is_file() or binary.is_symlink():
        return False
    meta = read_metadata(layout, tag)
    if meta is None or meta.get("tag") != tag:
        return False
    recorded = meta.get("sha256")
    if (
        not isinstance(recorded, str)
        or len(recorded) != 64
        or any(ch not in "0123456789abcdef" for ch in recorded.lower())
    ):
        return False
    if expected_sha is not None and recorded != expected_sha:
        return False
    return sha256_file(binary) == recorded


def launcher_state(layout: Layout) -> tuple[str, str]:
    """Classify bin_dir/omp as absent / ours / foreign, with a description.

    This matters because upstream's own `curl | sh` installer writes a real
    180 MB binary to `$HOME/.local/bin/omp` -- the exact path this script
    wants for its symlink. Clobbering or deleting that would destroy someone
    else's install.
    """
    path = layout.launcher
    if not path.exists() and not path.is_symlink():
        return ("absent", f"{path} does not exist")
    if path.is_symlink():
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            return ("foreign", f"{path} is an unreadable symlink")
        root = layout.root.resolve(strict=False)
        if resolved == root or root in resolved.parents:
            return ("ours", f"{path} -> {resolved}")
        return ("foreign", f"{path} is a symlink to {resolved}, outside {root}")
    return ("foreign", f"{path} is a regular file not managed by this script")


def is_safe_removal(root: Path, target: Path) -> bool:
    """Whether `target` may be deleted as part of uninstalling from `root`.

    Two separate hazards. The `parents` test rejects anything that is not the
    install root or inside it. The explicit blocklist covers the case that
    test would wave through: a misconfigured `--install-dir` of `/` or `$HOME`
    makes the root itself the thing being removed.
    """
    root_r = root.resolve(strict=False)
    target_r = target.resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if target_r in (Path(target_r.anchor), home, home.parent):
        return False
    if target_r == root_r:
        return True
    return root_r in target_r.parents


# --------------------------------------------------------------------------
# plan / execution
# --------------------------------------------------------------------------


class Plan:
    """A list of (description, action) pairs.

    `show()` and `run()` walk the same list, so a dry run cannot describe
    something different from what `--apply` does.
    """

    def __init__(self) -> None:
        self.steps: list[tuple[str, Callable[[], None] | None]] = []

    def add(self, description: str, action: Callable[[], None] | None = None) -> None:
        self.steps.append((description, action))

    def show(self) -> None:
        if not self.steps:
            print("  (nothing to do)")
            return
        for description, action in self.steps:
            marker = " " if action else "-"
            print(f"  {marker} {description}")

    def run(self) -> None:
        for description, action in self.steps:
            print(f"  * {description}")
            if action is not None:
                action()


def _symlink_atomic(target: Path, link: Path) -> None:
    tmp = link.with_name(f"{link.name}.tmp-{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# resolution shared by install/update
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Target:
    os_name: str
    arch: str
    libc: str
    asset: str


def resolve_target(config: Config) -> Target:
    os_name = config.os_name or normalize_os(platform.system())
    arch = config.arch or detect_arch(platform.system(), platform.machine())
    libc = config.libc
    if libc == "auto":
        libc = detect_libc() if os_name == "linux" else "gnu"
    return Target(os_name, arch, libc, asset_name(os_name, arch, libc))


@dataclasses.dataclass(frozen=True)
class Resolution:
    tag: str
    target: Target
    expected_sha: str | None
    verified: bool


def resolve_release(config: Config, target: Target) -> Resolution:
    """Resolve the tag and the published digest for our asset.

    The asset name is checked against the keys of the release's own
    SHA256SUMS.txt rather than against a hardcoded list of valid targets, so
    an upstream naming change surfaces as one clear error here instead of
    drifting silently against a second copy of the platform table.
    """
    tag = resolve_latest_tag() if config.version == "latest" else normalize_tag(config.version)
    _, sums_url = release_urls(tag, target.asset)
    try:
        sums = parse_sha256sums(fetch_text(sums_url))
    except OhMyPiError as exc:
        if not config.allow_unverified:
            raise OhMyPiError(
                f"{exc}\nNo checksum material for {tag}. Re-run with "
                "--allow-unverified only if you accept an unverified download."
            ) from exc
        print(f"  ! {exc}")
        print("  ! proceeding unverified because --allow-unverified was given")
        return Resolution(tag, target, None, False)

    if target.asset not in sums:
        raise OhMyPiError(
            f"release {tag} publishes no asset named {target.asset!r}.\n"
            f"Assets listed in {SUMS_NAME}: {', '.join(sorted(sums))}"
        )
    return Resolution(tag, target, sums[target.asset], True)


def _fetch_and_verify(resolution: Resolution, layout: Layout) -> None:
    """Download into a staging dir, verify, then publish atomically."""
    tag = resolution.tag
    asset_url, _ = release_urls(tag, resolution.target.asset)
    layout.versions.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{tag}-", dir=layout.versions))
    try:
        binary = staging / BINARY_NAME
        size = download_to(asset_url, binary)
        actual = sha256_file(binary)
        if resolution.expected_sha is not None and actual != resolution.expected_sha:
            raise OhMyPiError(
                "checksum mismatch -- refusing to install\n"
                f"  asset:    {resolution.target.asset}\n"
                f"  expected: {resolution.expected_sha}\n"
                f"  actual:   {actual}"
            )
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        metadata = {
            "schema": METADATA_SCHEMA,
            "tag": tag,
            "asset": resolution.target.asset,
            "os": resolution.target.os_name,
            "arch": resolution.target.arch,
            "libc": resolution.target.libc,
            "sha256": actual,
            "verified": resolution.verified,
            "bytes": size,
            "source": asset_url,
            "installed_at": _utc_now(),
            "installer": "dotfiles scripts/setup/oh-my-pi.py",
        }
        (staging / METADATA_NAME).write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        destination = layout.version_dir(tag)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------
# guidance printed rather than applied
# --------------------------------------------------------------------------


def _warn_path(bin_dir: Path) -> None:
    entries = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if str(bin_dir) in entries:
        return
    print()
    print(f"NOTE: {bin_dir} is not on your PATH. Add it yourself --")
    print("      this script never edits shell startup files.")
    print(f'      export PATH="{bin_dir}:$PATH"')


def _post_install_notes(target: Target, layout: Layout) -> None:
    print()
    print("Next steps (run these yourself; nothing below was executed):")
    print(f"  {layout.launcher} --version      # confirm the binary runs")
    if target.libc == "musl":
        print("  # musl builds link libstdc++/libgcc dynamically:")
        print("  apk add libstdc++ libgcc")
    print()
    print("Shell completions -- add to your own rc file if you want them:")
    print('  eval "$(omp completions zsh)"        # or bash')
    print("  omp completions fish > ~/.config/fish/completions/omp.fish")
    _warn_path(layout.bin_dir)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    layout = Layout(config.install_dir, config.bin_dir)
    print(f"install root : {layout.root}")
    print(f"bin dir      : {layout.bin_dir}")

    try:
        target = resolve_target(config)
        print(
            f"target       : {target.os_name}/{target.arch} "
            f"({target.libc}) -> {target.asset}"
        )
    except OhMyPiError as exc:
        print(f"target       : unavailable ({exc})")

    tags = layout.installed_tags()
    print(f"installed    : {', '.join(tags) if tags else '(none)'}")
    current = layout.current_tag()
    print(f"current      : {current or '(none)'}")
    if current:
        meta = read_metadata(layout, current)
        if meta is None:
            print("  metadata   : missing or unreadable")
        else:
            verified = "yes" if meta.get("verified") else "NO"
            print(f"  asset      : {meta.get('asset')}")
            print(f"  sha256     : {meta.get('sha256')}")
            print(f"  verified   : {verified}")
            print(f"  installed  : {meta.get('installed_at')}")
    state, detail = launcher_state(layout)
    print(f"launcher     : {state} -- {detail}")

    if getattr(args, "check_latest", False):
        latest = resolve_latest_tag()
        print(f"latest tag   : {latest}")
        if current == latest:
            print("               up to date")
        elif current:
            print(f"               update available ({current} -> {latest})")
    return 0


def _build_install_plan(
    config: Config, layout: Layout, resolution: Resolution, force: bool
) -> Plan:
    plan = Plan()
    tag = resolution.tag
    already = is_installed(layout, tag, resolution.expected_sha)

    if already:
        plan.add(f"{tag} already installed and digest-matched; skipping download")
    else:
        verified = "verified against SHA256SUMS.txt" if resolution.verified else "UNVERIFIED"
        plan.add(
            f"download {resolution.target.asset} from {tag} ({verified}) "
            f"into {layout.version_dir(tag)}",
            lambda: _fetch_and_verify(resolution, layout),
        )

    if layout.current_tag() == tag and layout.current.is_symlink():
        plan.add(f"current already points at {tag}")
    else:
        plan.add(
            f"point {layout.current} at versions/{tag}",
            lambda: _symlink_atomic(Path("versions") / tag, layout.current),
        )

    state, detail = launcher_state(layout)
    launcher_target = layout.current / BINARY_NAME
    if state == "foreign":
        if not force:
            plan.add(
                f"LEAVE {layout.launcher} alone -- {detail}. "
                "Re-run with --force to move it aside, or pick another --bin-dir"
            )
        else:
            backup = layout.launcher.with_name(
                f"{BINARY_NAME}.bak-{_utc_now().replace(':', '')}"
            )
            plan.add(
                f"move the existing {layout.launcher} aside to {backup} "
                "(moved, never deleted)",
                lambda: layout.launcher.rename(backup),
            )
            plan.add(
                f"link {layout.launcher} -> {launcher_target}",
                lambda: _symlink_atomic(launcher_target, layout.launcher),
            )
    elif state == "ours" and os.readlink(layout.launcher) == str(launcher_target):
        plan.add(f"{layout.launcher} already links to {launcher_target}")
    else:
        plan.add(
            f"create {layout.bin_dir} if missing",
            lambda: layout.bin_dir.mkdir(parents=True, exist_ok=True),
        )
        plan.add(
            f"link {layout.launcher} -> {launcher_target}",
            lambda: _symlink_atomic(launcher_target, layout.launcher),
        )
    return plan


def cmd_install(config: Config, args: argparse.Namespace) -> int:
    layout = Layout(config.install_dir, config.bin_dir)
    target = resolve_target(config)
    print(f"target: {target.os_name}/{target.arch} ({target.libc}) -> {target.asset}")
    resolution = resolve_release(config, target)
    print(f"release: {resolution.tag}")
    if resolution.expected_sha:
        print(f"expected sha256: {resolution.expected_sha}")
    else:
        print("expected sha256: (none -- unverified install)")

    plan = _build_install_plan(config, layout, resolution, force=args.force)
    if not args.apply:
        print("\nDry run -- nothing will be changed. Re-run with --apply to execute:")
        plan.show()
        return 0
    print("\nApplying:")
    plan.run()
    print(f"\noh-my-pi {resolution.tag} installed.")
    _post_install_notes(target, layout)
    return 0


def cmd_update(config: Config, args: argparse.Namespace) -> int:
    layout = Layout(config.install_dir, config.bin_dir)
    if not layout.installed_tags():
        raise OhMyPiError(
            f"nothing installed under {layout.root}; run `install` first"
        )
    return cmd_install(config, args)


def _build_uninstall_plan(layout: Layout, tag: str | None) -> Plan:
    plan = Plan()
    state, detail = launcher_state(layout)

    if tag is not None:
        version_dir = layout.version_dir(tag)
        if not version_dir.is_dir():
            raise OhMyPiError(f"{tag} is not installed under {layout.versions}")
        if not is_safe_removal(layout.root, version_dir):
            raise OhMyPiError(f"refusing to remove {version_dir}: outside {layout.root}")
        if layout.current_tag() == tag:
            plan.add(
                f"remove the {layout.current} symlink (it points at {tag})",
                lambda: layout.current.unlink(missing_ok=True),
            )
            if state == "ours":
                plan.add(
                    f"remove our launcher {layout.launcher} ({detail})",
                    lambda: layout.launcher.unlink(missing_ok=True),
                )
        plan.add(
            f"remove {version_dir}", lambda: shutil.rmtree(version_dir, ignore_errors=False)
        )
        return plan

    if state == "ours":
        plan.add(
            f"remove our launcher {layout.launcher} ({detail})",
            lambda: layout.launcher.unlink(missing_ok=True),
        )
    elif state == "foreign":
        plan.add(f"LEAVE {layout.launcher} alone -- {detail}")

    if not layout.root.exists():
        plan.add(f"{layout.root} does not exist; nothing to remove")
        return plan
    if not is_safe_removal(layout.root, layout.root):
        raise OhMyPiError(f"refusing to remove {layout.root}: unsafe target")
    plan.add(
        f"remove the whole install root {layout.root}",
        lambda: shutil.rmtree(layout.root, ignore_errors=False),
    )
    plan.add("LEAVE ~/.omp (oh-my-pi's own config and credentials) untouched")
    return plan


def cmd_uninstall(config: Config, args: argparse.Namespace) -> int:
    layout = Layout(config.install_dir, config.bin_dir)
    tag = normalize_tag(args.version) if args.version else None
    plan = _build_uninstall_plan(layout, tag)
    if not args.apply:
        print("Dry run -- nothing will be changed. Re-run with --apply to execute:")
        plan.show()
        return 0
    print("Applying:")
    plan.run()
    print("\nDone. oh-my-pi's own data in ~/.omp was not touched.")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oh-my-pi.py",
        description=(
            "Install oh-my-pi (the `omp` CLI) from GitHub releases with SHA-256 "
            "verification. Dry run by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="TOML config file (see config/oh-my-pi.toml.example)",
    )
    parser.add_argument("--install-dir", type=Path, help="versioned install root")
    parser.add_argument("--bin-dir", type=Path, help="directory for the `omp` launcher")
    parser.add_argument("--os", dest="os_name", choices=sorted(set(OS_MAP.values())))
    parser.add_argument("--arch", choices=sorted(set(ARCH_MAP.values())))
    parser.add_argument("--libc", choices=list(LIBC_CHOICES))

    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="report what is installed (no writes)")
    p_status.add_argument(
        "--check-latest", action="store_true", help="query GitHub for the latest tag"
    )
    p_status.set_defaults(func=cmd_status)

    def add_install_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--version", help="release tag to install, or 'latest'")
        p.add_argument("--apply", action="store_true", help="actually make changes")
        p.add_argument(
            "--allow-unverified",
            action="store_true",
            help="proceed when the release publishes no checksum file",
        )
        p.add_argument(
            "--force",
            action="store_true",
            help="move an unmanaged omp in --bin-dir aside instead of refusing",
        )

    p_install = sub.add_parser("install", help="install a release")
    add_install_args(p_install)
    p_install.set_defaults(func=cmd_install)

    p_update = sub.add_parser("update", help="install the latest release over an existing one")
    add_install_args(p_update)
    p_update.set_defaults(func=cmd_update)

    p_uninstall = sub.add_parser("uninstall", help="remove installs made by this script")
    p_uninstall.add_argument("--version", help="remove only this tag")
    p_uninstall.add_argument("--apply", action="store_true", help="actually make changes")
    p_uninstall.set_defaults(func=cmd_uninstall)

    return parser


def cli_overrides_from(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "install_dir": args.install_dir,
        "bin_dir": args.bin_dir,
        "os_name": args.os_name,
        "arch": args.arch,
        "libc": args.libc,
        "version": getattr(args, "version", None),
    }
    if getattr(args, "allow_unverified", False):
        overrides["allow_unverified"] = True
    return overrides


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        toml_overrides = config_from_toml(args.config) if args.config else {}
        config = resolve_config(toml_overrides, cli_overrides_from(args))
        return int(args.func(config, args))
    except OhMyPiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
