#!/usr/bin/env python3
"""User-local installer for oh-my-openagent (https://github.com/code-yeongyu/oh-my-openagent).

Why this exists rather than following upstream's own instructions
-----------------------------------------------------------------
Upstream documents `bunx oh-my-openagent`, `npx oh-my-openagent` and
`npm i -g oh-my-openagent`. None of those are usable here:

  * `npm i -g` writes to a shared prefix, cannot be pinned or rolled back, and
    leaves nothing to uninstall cleanly.
  * `bunx`/`npx` re-resolve "latest" on every invocation, so the code that runs
    tomorrow is not the code that was reviewed today.
  * None of them verify anything before executing the payload.

So this installer does the download itself, verifies it, and only then hands a
*local file* to npm. Every version lives in its own directory under the install
root and `current` is a symlink, so update and rollback are a symlink swap.

Distribution channel
--------------------
npm is the ONLY channel. All GitHub releases of this project publish zero
assets, and the platform packages (oh-my-opencode-linux-x64 and friends) are
~4 KB stubs, not binaries. There is therefore no upstream SHA256SUMS file and
no author-signed artifact; the verification material is what the npm registry
publishes. See docs/oh-my-openagent-installer.md for the trust chain and its
limits -- matching `dist.integrity` proves transport integrity, not authorship.

Scope
-----
This installs the CLI and nothing else. It deliberately does NOT run upstream's
own `oh-my-openagent install` step, which rewrites ~/.codex/config.toml,
symlinks a dozen component CLIs into ~/.local/bin and edits opencode.json.
User data -- ~/.omo, ~/.codex, opencode.json -- is never read, written, or
removed, including by `uninstall`.

Policy
------
Lifecycle scripts stay off (--ignore-scripts, matching the global ~/.npmrc), and
the min-release-age quarantine is enforced locally too. A package blocked by a
guard is reported, never bypassed.

Idempotent: re-running after a partial apply adds only what is missing.
Dry-run by default; pass --apply to write.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

PACKAGE = "oh-my-openagent"
LAUNCHER_NAME = "oh-my-openagent"
REGISTRY = "https://registry.npmjs.org"
KEYS_URL = f"{REGISTRY}/-/npm/v1/keys"
ATTESTATIONS_URL = f"{REGISTRY}/-/npm/v1/attestations"

DEFAULT_ROOT = Path.home() / ".local" / "share" / "oh-my-openagent"
DEFAULT_BIN_DIR = Path.home() / ".local" / "bin"
DEFAULT_TAG = "latest"
DEFAULT_MIN_RELEASE_AGE_DAYS = 7

MAX_METADATA_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
HTTP_TIMEOUT = 60

# Supported (system, machine) pairs. oh-my-openagent is a Node program, so the
# payload itself is portable; this gate exists to refuse platforms where the
# optional native dependencies have no build and the CLI would fail at runtime.
OS_MAP = {"linux": "linux", "darwin": "darwin"}
ARCH_MAP = {
    "x86_64": "x64",
    "amd64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
}

# Keys whose presence in the TOML config is an error: this file is committed and
# shared, so a credential must never live in it.
SECRET_KEY_HINTS = ("password", "secret", "token", "passphrase", "credential")


class OhMyOpenagentError(RuntimeError):
    """A condition the user must resolve; reported without a traceback."""


# --------------------------------------------------------------------------
# Pure helpers. Everything below is unit-tested with no network and no writes.
# --------------------------------------------------------------------------


def normalize_platform(system: str, machine: str) -> tuple[str, str]:
    """Map platform.system()/platform.machine() onto npm's os/cpu names."""
    os_name = OS_MAP.get(system.strip().lower())
    arch = ARCH_MAP.get(machine.strip().lower())
    if os_name is None:
        raise OhMyOpenagentError(
            f"unsupported operating system {system!r}; supported: {', '.join(sorted(set(OS_MAP.values())))}"
        )
    if arch is None:
        raise OhMyOpenagentError(
            f"unsupported architecture {machine!r}; supported: {', '.join(sorted(set(ARCH_MAP.values())))}"
        )
    return os_name, arch


def normalize_version_spec(spec: str) -> str:
    """Validate a dist-tag or exact version before it reaches a URL.

    Anything that could traverse a path or smuggle a second URL component is
    rejected outright; npm range syntax is rejected too, because a range would
    reintroduce the "resolves differently tomorrow" problem this installer is
    meant to remove.
    """
    value = spec.strip()
    if not value:
        raise OhMyOpenagentError("version spec is empty")
    bad = set('/ \t\\?#%@"\'')
    if bad & set(value):
        raise OhMyOpenagentError(f"invalid version spec {spec!r}: contains a forbidden character")
    if value.startswith(("^", "~", ">", "<", "=", "*")):
        raise OhMyOpenagentError(
            f"invalid version spec {spec!r}: ranges are not supported, pin an exact version or use a dist-tag"
        )
    if value.startswith("-") or value.startswith("."):
        raise OhMyOpenagentError(f"invalid version spec {spec!r}")
    return value


def is_exact_version(spec: str) -> bool:
    """True when spec looks like an exact semver rather than a dist-tag."""
    head = spec.split("-", 1)[0].split("+", 1)[0]
    parts = head.split(".")
    return len(parts) == 3 and all(p.isdigit() for p in parts)


def packument_url(package: str = PACKAGE) -> str:
    return f"{REGISTRY}/{package}"


def attestations_url(package: str, version: str) -> str:
    return f"{ATTESTATIONS_URL}/{package}@{version}"


class VersionInfo:
    """The subset of an npm version manifest this installer actually trusts."""

    def __init__(
        self,
        name: str,
        version: str,
        tarball: str,
        integrity: str,
        shasum: str,
        signatures: list[dict[str, str]],
        published: dt.datetime | None,
        bin_map: dict[str, str],
        license_id: str,
    ) -> None:
        self.name = name
        self.version = version
        self.tarball = tarball
        self.integrity = integrity
        self.shasum = shasum
        self.signatures = signatures
        self.published = published
        self.bin_map = bin_map
        self.license_id = license_id


def parse_published(value: str) -> dt.datetime:
    """Parse an npm `time` entry (RFC3339 with a trailing Z) into aware UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def resolve_version(packument: dict[str, Any], spec: str) -> str:
    """Resolve a dist-tag or exact version against the registry document."""
    tags = packument.get("dist-tags") or {}
    if spec in tags:
        return str(tags[spec])
    versions = packument.get("versions") or {}
    if spec in versions:
        return spec
    if is_exact_version(spec):
        raise OhMyOpenagentError(
            f"version {spec!r} is not published for {PACKAGE}; "
            f"known dist-tags: {', '.join(sorted(tags)) or 'none'}"
        )
    raise OhMyOpenagentError(
        f"unknown dist-tag {spec!r}; known dist-tags: {', '.join(sorted(tags)) or 'none'}"
    )


def extract_version_info(packument: dict[str, Any], version: str) -> VersionInfo:
    """Pull the manifest for one version, failing loudly on missing verification material."""
    manifest = (packument.get("versions") or {}).get(version)
    if not isinstance(manifest, dict):
        raise OhMyOpenagentError(f"registry document has no manifest for version {version}")
    dist = manifest.get("dist")
    if not isinstance(dist, dict):
        raise OhMyOpenagentError(f"manifest for {version} has no dist block")
    tarball = dist.get("tarball")
    integrity = dist.get("integrity")
    if not isinstance(tarball, str) or not tarball:
        raise OhMyOpenagentError(f"manifest for {version} has no tarball URL")
    if not isinstance(integrity, str) or not integrity:
        raise OhMyOpenagentError(
            f"manifest for {version} publishes no dist.integrity; refusing to install unverifiable bytes"
        )
    if not tarball.startswith(f"{REGISTRY}/"):
        raise OhMyOpenagentError(f"tarball URL {tarball!r} is not on {REGISTRY}; refusing")
    published_raw = (packument.get("time") or {}).get(version)
    published = parse_published(published_raw) if isinstance(published_raw, str) else None
    raw_bin = manifest.get("bin")
    bin_map = {str(k): str(v) for k, v in raw_bin.items()} if isinstance(raw_bin, dict) else {}
    signatures = [s for s in (dist.get("signatures") or []) if isinstance(s, dict)]
    return VersionInfo(
        name=str(manifest.get("name") or PACKAGE),
        version=version,
        tarball=tarball,
        integrity=integrity,
        shasum=str(dist.get("shasum") or ""),
        signatures=signatures,
        published=published,
        bin_map=bin_map,
        license_id=str(manifest.get("license") or "unknown"),
    )


def integrity_digest(integrity: str) -> tuple[str, bytes]:
    """Split an SRI string into (algorithm, raw digest bytes)."""
    if "-" not in integrity:
        raise OhMyOpenagentError(f"malformed integrity string {integrity!r}")
    algo, _, encoded = integrity.partition("-")
    algo = algo.strip().lower()
    if algo not in ("sha512", "sha384", "sha256"):
        raise OhMyOpenagentError(f"unsupported integrity algorithm {algo!r}")
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise OhMyOpenagentError(f"malformed integrity string {integrity!r}: {exc}") from exc
    return algo, raw


def verify_integrity(data: bytes, integrity: str) -> None:
    """Hard-fail unless the downloaded bytes match dist.integrity."""
    algo, expected = integrity_digest(integrity)
    actual = hashlib.new(algo, data).digest()
    if actual != expected:
        raise OhMyOpenagentError(
            f"{algo} mismatch: the downloaded tarball does not match the registry's "
            f"dist.integrity. Expected {base64.b64encode(expected).decode()}, "
            f"got {base64.b64encode(actual).decode()}. Refusing to install."
        )


def signature_payload(name: str, version: str, integrity: str) -> str:
    """The exact byte string the npm registry signs. Order and separators matter."""
    return f"{name}@{version}:{integrity}"


def select_key(keys_document: dict[str, Any], keyid: str) -> dict[str, Any] | None:
    """Find the registry public key matching a signature's keyid, if it is still valid."""
    for key in keys_document.get("keys") or []:
        if not isinstance(key, dict) or key.get("keyid") != keyid:
            continue
        expires = key.get("expires")
        if isinstance(expires, str) and expires:
            try:
                if parse_published(expires) <= dt.datetime.now(dt.timezone.utc):
                    return None
            except ValueError:
                return None
        return key
    return None


def spki_to_pem(key_b64: str) -> str:
    """Wrap a base64 SPKI DER key from the registry into a PEM document."""
    body = "".join(key_b64.split())
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


def release_age_days(published: dt.datetime, now: dt.datetime) -> float:
    return (now - published).total_seconds() / 86400.0


def check_release_age(info: VersionInfo, min_days: int, now: dt.datetime) -> None:
    """Mirror the repo-wide min-release-age quarantine for the top-level package."""
    if min_days <= 0:
        return
    if info.published is None:
        raise OhMyOpenagentError(
            f"registry publishes no timestamp for {info.name}@{info.version}; "
            f"cannot enforce the {min_days}-day min-release-age quarantine. "
            f"Pass --min-release-age 0 only if you have reviewed the release yourself."
        )
    age = release_age_days(info.published, now)
    if age < min_days:
        raise OhMyOpenagentError(
            f"{info.name}@{info.version} was published {age:.1f} days ago, under the "
            f"{min_days}-day min-release-age quarantine (published "
            f"{info.published.isoformat()}). This guard is working as intended. "
            f"Pin an older version with --version, or wait."
        )


def npm_install_argv(prefix: Path, tarball: Path) -> list[str]:
    """The exact argv handed to npm.

    -g places the package at <prefix>/lib/node_modules with shims in
    <prefix>/bin; a non-global install would put it at <prefix>/node_modules
    with shims in <prefix>/node_modules/.bin, which is a different launcher
    target. The argument is a local verified file, never a registry spec, so
    npm re-resolves nothing.
    """
    return [
        "npm",
        "install",
        "-g",
        "--prefix",
        str(prefix),
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--no-progress",
        str(tarball),
    ]


def launch_target(version_dir: Path, name: str = LAUNCHER_NAME) -> Path:
    """Where npm's shim for our launcher name lands under a version prefix."""
    return version_dir / "bin" / name


def is_complete(version_dir: Path, name: str = LAUNCHER_NAME) -> bool:
    """A version directory only counts as installed if npm actually produced the shim."""
    target = launch_target(version_dir, name)
    return target.exists() and (version_dir / "lib" / "node_modules").is_dir()


def launcher_body(root: Path, name: str = LAUNCHER_NAME) -> str:
    """POSIX-sh launcher pointing at the `current` symlink. Kept shellcheck-clean."""
    return (
        "#!/bin/sh\n"
        "# Managed by dotfiles scripts/setup/oh-my-openagent.py -- regenerated on\n"
        "# install and update. Local edits will be overwritten.\n"
        f'OMOA_ROOT="{root}"\n'
        f'exec "$OMOA_ROOT/current/bin/{name}" "$@"\n'
    )


def launcher_is_current(path: Path, root: Path, name: str = LAUNCHER_NAME) -> bool:
    if not path.is_file():
        return False
    try:
        return path.read_text() == launcher_body(root, name)
    except (OSError, UnicodeDecodeError):
        return False


def is_safe_removal(root: Path, target: Path) -> bool:
    """True only when target is strictly inside root and neither is a sentinel path.

    Guards uninstall against an unresolved or over-broad path: a symlinked or
    misconfigured root must never let us walk out and delete $HOME.
    """
    try:
        root_r = root.resolve()
        target_r = target.resolve()
    except (OSError, RuntimeError):
        return False
    forbidden = {Path("/"), Path.home().resolve(), Path.home().parent.resolve()}
    if root_r in forbidden or target_r in forbidden:
        return False
    if target_r == root_r:
        return False
    return root_r in target_r.parents


def member_is_safe(name: str) -> bool:
    """Reject absolute paths, traversal, and anything outside npm's `package/` prefix."""
    if not name or name.startswith("/") or "\\" in name:
        return False
    parts = Path(name).parts
    if not parts or parts[0] != "package":
        return False
    return not any(p == ".." for p in parts)


def read_manifest_name(tarball: Path) -> tuple[str, str]:
    """Read (name, version) out of package/package.json inside the tarball.

    The installed directory name comes from the manifest, not from what we
    asked for -- the GitHub repo root manifest says oh-my-opencode while the
    npm name is oh-my-openagent, so assuming either one would be a guess.
    """
    with tarfile.open(tarball, mode="r:gz") as tar:
        while True:
            member = tar.next()
            if member is None:
                break
            if not member_is_safe(member.name):
                raise OhMyOpenagentError(f"tarball contains an unsafe member path: {member.name!r}")
            if member.name == "package/package.json" and member.isfile():
                handle = tar.extractfile(member)
                if handle is None:
                    break
                data = handle.read(MAX_METADATA_BYTES)
                try:
                    manifest = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise OhMyOpenagentError(f"package.json inside the tarball is not valid JSON: {exc}") from exc
                return str(manifest.get("name") or ""), str(manifest.get("version") or "")
    raise OhMyOpenagentError("tarball contains no package/package.json")


# --------------------------------------------------------------------------
# Configuration: CLI > TOML > built-in defaults.
# --------------------------------------------------------------------------


class Config:
    def __init__(
        self,
        root: Path = DEFAULT_ROOT,
        bin_dir: Path = DEFAULT_BIN_DIR,
        version: str = DEFAULT_TAG,
        min_release_age: int = DEFAULT_MIN_RELEASE_AGE_DAYS,
        verify_signature: bool = True,
        allow_unverified: bool = False,
    ) -> None:
        self.root = root
        self.bin_dir = bin_dir
        self.version = version
        self.min_release_age = min_release_age
        self.verify_signature = verify_signature
        self.allow_unverified = allow_unverified


CONFIG_FIELDS = {
    "root": Path,
    "bin_dir": Path,
    "version": str,
    "min_release_age": int,
    "verify_signature": bool,
    "allow_unverified": bool,
}


def _reject_secrets(mapping: dict[str, Any], where: str) -> None:
    for key in mapping:
        lowered = str(key).lower()
        if any(hint in lowered for hint in SECRET_KEY_HINTS):
            raise OhMyOpenagentError(
                f"{where}: key {key!r} looks like a credential. This file must never "
                f"contain a secret; oh-my-openagent reads its own credentials from "
                f"the environment."
            )


def load_config_file(path: Path) -> dict[str, Any]:
    """Parse the TOML config, rejecting unknown keys and anything secret-shaped."""
    try:
        raw = tomllib.loads(path.read_text())
    except OSError as exc:
        raise OhMyOpenagentError(f"cannot read config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise OhMyOpenagentError(f"invalid TOML in {path}: {exc}") from exc

    section = raw.get("oh_my_openagent", raw)
    if not isinstance(section, dict):
        raise OhMyOpenagentError(f"{path}: [oh_my_openagent] must be a table")
    _reject_secrets(section, str(path))

    out: dict[str, Any] = {}
    for key, value in section.items():
        if key not in CONFIG_FIELDS:
            raise OhMyOpenagentError(
                f"{path}: unknown key {key!r}; known keys: {', '.join(sorted(CONFIG_FIELDS))}"
            )
        want = CONFIG_FIELDS[key]
        if want is bool:
            if not isinstance(value, bool):
                raise OhMyOpenagentError(f"{path}: {key} must be true or false")
            out[key] = value
        elif want is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise OhMyOpenagentError(f"{path}: {key} must be an integer")
            if value < 0:
                raise OhMyOpenagentError(f"{path}: {key} must not be negative")
            out[key] = value
        elif want is Path:
            if not isinstance(value, str) or not value.strip():
                raise OhMyOpenagentError(f"{path}: {key} must be a non-empty string")
            out[key] = Path(value).expanduser()
        else:
            if not isinstance(value, str) or not value.strip():
                raise OhMyOpenagentError(f"{path}: {key} must be a non-empty string")
            out[key] = value
    return out


def build_config(args: argparse.Namespace) -> Config:
    """Layer CLI over TOML over defaults. A CLI flag left at None does not override."""
    cfg = Config()
    if getattr(args, "config", None):
        for key, value in load_config_file(args.config).items():
            setattr(cfg, key, value)
    for key in CONFIG_FIELDS:
        value = getattr(args, key, None)
        if value is not None:
            setattr(cfg, key, Path(value).expanduser() if CONFIG_FIELDS[key] is Path else value)
    cfg.root = Path(cfg.root).expanduser()
    cfg.bin_dir = Path(cfg.bin_dir).expanduser()
    cfg.version = normalize_version_spec(cfg.version)
    return cfg


# --------------------------------------------------------------------------
# Network. Nothing here executes a downloaded artifact.
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _urlopen(url: str) -> Iterator[Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "dotfiles-oh-my-openagent-installer"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 - https only, validated above
            yield response
    except urllib.error.HTTPError as exc:
        raise OhMyOpenagentError(f"HTTP {exc.code} fetching {url}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OhMyOpenagentError(f"network error fetching {url}: {exc}") from exc


def _get_json(url: str) -> dict[str, Any]:
    with _urlopen(url) as response:
        data = response.read(MAX_METADATA_BYTES)
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise OhMyOpenagentError(f"response from {url} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise OhMyOpenagentError(f"response from {url} is not a JSON object")
    return parsed


def download_bytes(url: str) -> bytes:
    """Stream a tarball into memory under a hard size cap."""
    chunks: list[bytes] = []
    total = 0
    with _urlopen(url) as response:
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise OhMyOpenagentError(
                    f"download from {url} exceeded {MAX_ARCHIVE_BYTES} bytes; refusing"
                )
            chunks.append(chunk)
    if total == 0:
        raise OhMyOpenagentError(f"download from {url} was empty")
    return b"".join(chunks)


def verify_signature(
    info: VersionInfo,
    keys_document: dict[str, Any],
    workdir: Path,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None,
) -> tuple[str, str]:
    """Verify the npm registry's ECDSA signature. Returns (state, detail).

    state is one of "verified", "unavailable", "failed". Tri-state on purpose:
    "unavailable" (no openssl, or the keyid is not in the published key set) is
    a loud warning, "failed" is a hard refusal, and neither is ever silent.
    Python's stdlib cannot do ECDSA, hence the openssl CLI.
    """
    if not info.signatures:
        return "unavailable", "the registry published no signature for this version"
    if runner is None:
        if shutil.which("openssl") is None:
            return "unavailable", "openssl was not found on PATH"
        runner = lambda argv: subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: E731

    signature = info.signatures[0]
    keyid = str(signature.get("keyid") or "")
    sig_b64 = str(signature.get("sig") or "")
    if not keyid or not sig_b64:
        return "unavailable", "the published signature is missing its keyid or sig field"

    key = select_key(keys_document, keyid)
    if key is None:
        return "unavailable", f"keyid {keyid} is not in the registry's published (unexpired) key set"

    payload = signature_payload(info.name, info.version, info.integrity)
    pem_path = workdir / "npm-registry-key.pem"
    sig_path = workdir / "package.sig"
    msg_path = workdir / "package.payload"
    pem_path.write_text(spki_to_pem(str(key.get("key") or "")))
    try:
        sig_path.write_bytes(base64.b64decode(sig_b64, validate=True))
    except (ValueError, TypeError) as exc:
        return "failed", f"the published signature is not valid base64: {exc}"
    msg_path.write_text(payload)

    argv = [
        "openssl",
        "dgst",
        "-sha256",
        "-verify",
        str(pem_path),
        "-signature",
        str(sig_path),
        str(msg_path),
    ]
    result = runner(argv)
    if result.returncode == 0:
        return "verified", f"npm registry key {keyid}"
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return "failed", detail[-1] if detail else f"openssl exited {result.returncode}"


def fetch_attestation_state(package: str, version: str) -> tuple[str, str]:
    """Report whether SLSA provenance exists. We do not verify it -- see the doc."""
    try:
        document = _get_json(attestations_url(package, version))
    except OhMyOpenagentError as exc:
        return "unavailable", str(exc)
    entries = document.get("attestations") or []
    if not entries:
        return "absent", "the registry lists no provenance attestation"
    kinds = sorted({str(e.get("predicateType") or "?") for e in entries if isinstance(e, dict)})
    return "present", ", ".join(kinds)


# --------------------------------------------------------------------------
# Layout and plan
# --------------------------------------------------------------------------


class Layout:
    def __init__(self, root: Path, bin_dir: Path) -> None:
        self.root = root
        self.bin_dir = bin_dir
        self.versions = root / "versions"
        self.current = root / "current"
        self.launcher = bin_dir / LAUNCHER_NAME

    def version_dir(self, version: str) -> Path:
        return self.versions / version

    def installed_versions(self) -> list[str]:
        if not self.versions.is_dir():
            return []
        return sorted(p.name for p in self.versions.iterdir() if p.is_dir())

    def current_version(self) -> str | None:
        try:
            return Path(os.readlink(self.current)).name
        except OSError:
            return None


class Plan:
    """Every mutation is registered here, so --apply and the dry run cannot diverge."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, Callable[[], None] | None]] = []

    def add(self, description: str, action: Callable[[], None] | None = None) -> None:
        self.steps.append((description, action))

    def __bool__(self) -> bool:
        return any(action is not None for _, action in self.steps)

    def show(self, header: str) -> None:
        print(header)
        if not self.steps:
            print("  nothing to do")
            return
        for description, action in self.steps:
            print(f"  {'*' if action is not None else '-'} {description}")

    def run(self) -> None:
        for description, action in self.steps:
            if action is None:
                continue
            print(f"  -> {description}")
            action()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _warn_path(bin_dir: Path) -> None:
    entries = [Path(p).expanduser() for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if bin_dir not in entries:
        print(
            f"note: {bin_dir} is not on your PATH. Add it in your own shell startup file:\n"
            f'      export PATH="{bin_dir}:$PATH"\n'
            f"      This installer never edits shell startup files."
        )


def _atomic_symlink(link: Path, target: Path) -> None:
    tmp = link.with_name(link.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, link)


def _npm_or_die() -> str:
    npm = shutil.which("npm")
    if npm is None:
        raise OhMyOpenagentError(
            "npm was not found on PATH. oh-my-openagent is distributed only via npm; "
            "install Node.js (which ships npm) and re-run."
        )
    return npm


def _run_npm(argv: Sequence[str], cwd: Path) -> None:
    result = subprocess.run(list(argv), cwd=str(cwd), capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").strip()
    blocked = [
        line
        for line in stderr.splitlines()
        if "minimumReleaseAge" in line or "min-release-age" in line or "E403" in line
    ]
    if blocked:
        raise OhMyOpenagentError(
            "npm refused a dependency:\n  "
            + "\n  ".join(blocked[:5])
            + "\nThis is a supply-chain guard working as intended. It has NOT been "
            "bypassed. Wait for the quarantine to lapse or pin an older version."
        )
    tail = "\n  ".join(stderr.splitlines()[-12:]) or "(no stderr)"
    raise OhMyOpenagentError(f"npm install failed (exit {result.returncode}):\n  {tail}")


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    layout = Layout(cfg.root, cfg.bin_dir)
    print(f"root         : {layout.root}")
    print(f"bin dir      : {layout.bin_dir}")
    installed = layout.installed_versions()
    print(f"installed    : {', '.join(installed) if installed else 'none'}")
    for version in installed:
        if not is_complete(layout.version_dir(version)):
            print(f"  ! {version} is incomplete (no bin/{LAUNCHER_NAME}); re-run install to repair")
    current = layout.current_version()
    print(f"current      : {current or 'none'}")
    launcher_state = (
        "current"
        if launcher_is_current(layout.launcher, layout.root)
        else ("stale" if layout.launcher.exists() else "missing")
    )
    print(f"launcher     : {layout.launcher} ({launcher_state})")
    print(f"npm          : {shutil.which('npm') or 'NOT FOUND'}")
    print(f"openssl      : {shutil.which('openssl') or 'NOT FOUND (signature checks unavailable)'}")
    if args.remote:
        packument = _get_json(packument_url())
        version = resolve_version(packument, cfg.version)
        info = extract_version_info(packument, version)
        age = (
            f"{release_age_days(info.published, dt.datetime.now(dt.timezone.utc)):.1f} days old"
            if info.published
            else "publication time unknown"
        )
        print(f"remote       : {cfg.version} -> {version} ({age}, license {info.license_id})")
        state, detail = fetch_attestation_state(info.name, version)
        print(f"provenance   : {state} ({detail})")
    _warn_path(layout.bin_dir)
    return 0


def _install_plan(cfg: Config, layout: Layout, version: str, info: VersionInfo, plan: Plan) -> None:
    version_dir = layout.version_dir(version)
    if is_complete(version_dir):
        plan.add(f"version {version} already installed at {version_dir}")
    else:
        plan.add(
            f"download and verify {info.name}@{version} then npm install into {version_dir}",
            lambda: _fetch_and_install(cfg, layout, info),
        )
    if layout.current_version() == version and layout.current.is_symlink():
        plan.add(f"current already points at {version}")
    else:
        plan.add(
            f"point {layout.current} at versions/{version}",
            lambda: _atomic_symlink(layout.current, Path("versions") / version),
        )
    if launcher_is_current(layout.launcher, layout.root):
        plan.add(f"launcher {layout.launcher} already current")
    else:
        plan.add(f"write launcher {layout.launcher}", lambda: _write_launcher(layout))


def _write_launcher(layout: Layout) -> None:
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    tmp = layout.launcher.with_name(layout.launcher.name + ".tmp")
    tmp.write_text(launcher_body(layout.root))
    tmp.chmod(0o755)
    os.replace(tmp, layout.launcher)


def _fetch_and_install(cfg: Config, layout: Layout, info: VersionInfo) -> None:
    version_dir = layout.version_dir(info.version)
    layout.versions.mkdir(parents=True, exist_ok=True)
    _npm_or_die()
    with tempfile.TemporaryDirectory(prefix="oh-my-openagent-", dir=str(layout.root)) as tmpdir:
        work = Path(tmpdir)
        print(f"     fetching {info.tarball}")
        data = download_bytes(info.tarball)
        verify_integrity(data, info.integrity)
        print(f"     integrity ok ({info.integrity.split('-', 1)[0]} matches dist.integrity)")

        state, detail = "skipped", "disabled with --no-verify-signature"
        if cfg.verify_signature:
            state, detail = verify_signature(info, _get_json(KEYS_URL), work)
        if state == "verified":
            print(f"     signature verified ({detail})")
        elif state == "failed":
            raise OhMyOpenagentError(
                f"npm registry signature verification FAILED for {info.name}@{info.version}: {detail}. "
                f"Refusing to install."
            )
        elif state == "unavailable":
            if not cfg.allow_unverified:
                raise OhMyOpenagentError(
                    f"cannot verify the npm registry signature: {detail}. The sha512 "
                    f"integrity check passed, but authenticity is unconfirmed. Re-run with "
                    f"--allow-unverified to accept that, or install openssl."
                )
            print(f"     WARNING: signature NOT verified ({detail}); continuing due to --allow-unverified")
        else:
            print(f"     signature check {state} ({detail})")

        tarball = work / "package.tgz"
        tarball.write_bytes(data)
        manifest_name, manifest_version = read_manifest_name(tarball)
        if manifest_version and manifest_version != info.version:
            raise OhMyOpenagentError(
                f"tarball contains version {manifest_version} but the registry advertised "
                f"{info.version}; refusing"
            )
        print(f"     tarball manifest: {manifest_name}@{manifest_version}")

        staging = layout.versions / f".staging-{info.version}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            _run_npm(npm_install_argv(staging, tarball), cwd=work)
            if not is_complete(staging):
                produced = sorted(p.name for p in (staging / "bin").glob("*")) if (staging / "bin").is_dir() else []
                raise OhMyOpenagentError(
                    f"npm install produced no bin/{LAUNCHER_NAME} shim under {staging}. "
                    f"Shims present: {', '.join(produced) or 'none'}. Refusing to link a launcher "
                    f"that would not run."
                )
            shutil.rmtree(version_dir, ignore_errors=True)
            os.replace(staging, version_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def cmd_install(cfg: Config, args: argparse.Namespace) -> int:
    layout = Layout(cfg.root, cfg.bin_dir)
    normalize_platform(
        getattr(args, "system", None) or platform.system(),
        getattr(args, "machine", None) or platform.machine(),
    )
    packument = _get_json(packument_url())
    version = resolve_version(packument, cfg.version)
    info = extract_version_info(packument, version)
    check_release_age(info, cfg.min_release_age, dt.datetime.now(dt.timezone.utc))

    print(f"resolved {cfg.version} -> {info.name}@{version} (license {info.license_id})")
    if LAUNCHER_NAME not in info.bin_map:
        raise OhMyOpenagentError(
            f"{info.name}@{version} publishes no {LAUNCHER_NAME!r} entry in its bin map "
            f"(has: {', '.join(sorted(info.bin_map)) or 'none'}); refusing to guess a command name"
        )
    state, detail = fetch_attestation_state(info.name, version)
    print(f"provenance: {state} ({detail}) -- not verified by this installer, see the doc")

    plan = Plan()
    if not cfg.root.exists():
        plan.add(f"create install root {cfg.root}", lambda: cfg.root.mkdir(parents=True, exist_ok=True))
    _install_plan(cfg, layout, version, info, plan)
    plan.show(f"\nplan ({'apply' if args.apply else 'dry run'}):")
    if not args.apply:
        print("\ndry run only; re-run with --apply to write.")
        return 0
    cfg.root.mkdir(parents=True, exist_ok=True)
    plan.run()
    _warn_path(layout.bin_dir)
    print(
        f"\ninstalled. next: run `{LAUNCHER_NAME} --version` yourself to confirm the "
        f"executable starts. This installer never executes the downloaded artifact."
    )
    return 0


def cmd_update(cfg: Config, args: argparse.Namespace) -> int:
    layout = Layout(cfg.root, cfg.bin_dir)
    if not layout.versions.is_dir():
        raise OhMyOpenagentError(f"nothing installed under {cfg.root}; run `install --apply` first")
    current = layout.current_version()
    packument = _get_json(packument_url())
    version = resolve_version(packument, cfg.version)
    if current == version and is_complete(layout.version_dir(version)):
        print(f"already on {version} (from {cfg.version}); nothing to do")
        return 0
    print(f"update: {current or 'none'} -> {version}")
    return cmd_install(cfg, args)


def cmd_uninstall(cfg: Config, args: argparse.Namespace) -> int:
    layout = Layout(cfg.root, cfg.bin_dir)
    plan = Plan()
    for version in layout.installed_versions():
        target = layout.version_dir(version)
        if not is_safe_removal(layout.root, target):
            plan.add(f"REFUSING to remove {target}: outside the install root")
            continue
        plan.add(f"remove {target}", lambda t=target: shutil.rmtree(t, ignore_errors=True))
    if layout.current.is_symlink():
        plan.add(f"remove symlink {layout.current}", layout.current.unlink)
    if launcher_is_current(layout.launcher, layout.root):
        plan.add(f"remove launcher {layout.launcher}", layout.launcher.unlink)
    elif layout.launcher.exists():
        plan.add(f"leaving {layout.launcher} alone: not written by this installer")
    if layout.root.is_dir() and is_safe_removal(layout.root.parent, layout.root):
        plan.add(f"remove empty root {layout.root}", lambda: _rmdir_if_empty(layout.root))
    plan.add("user data in ~/.omo, ~/.codex and opencode.json is never touched")
    plan.show(f"\nplan ({'apply' if args.apply else 'dry run'}):")
    if not args.apply:
        print("\ndry run only; re-run with --apply to remove.")
        return 0
    plan.run()
    return 0


def _rmdir_if_empty(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.rmdir()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # Shared options live in a parent parser attached to BOTH the top level and
    # every subcommand, so `install --version 4.19.4` and `--version 4.19.4
    # install` are equally valid. default=SUPPRESS is what makes that safe: an
    # option the user did not type leaves no attribute behind, so a subparser
    # cannot silently overwrite a value parsed at the top level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c",
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="TOML config file (see config/oh-my-openagent.toml.example)",
    )
    common.add_argument("--root", default=argparse.SUPPRESS, help=f"install root (default {DEFAULT_ROOT})")
    common.add_argument(
        "--bin-dir", dest="bin_dir", default=argparse.SUPPRESS, help=f"launcher directory (default {DEFAULT_BIN_DIR})"
    )
    common.add_argument(
        "--version", default=argparse.SUPPRESS, help=f"exact version or dist-tag (default {DEFAULT_TAG})"
    )
    common.add_argument(
        "--min-release-age",
        dest="min_release_age",
        type=int,
        default=argparse.SUPPRESS,
        help=f"refuse releases newer than N days (default {DEFAULT_MIN_RELEASE_AGE_DAYS}; 0 disables)",
    )
    common.add_argument(
        "--no-verify-signature",
        dest="verify_signature",
        action="store_false",
        default=argparse.SUPPRESS,
        help="skip the npm registry ECDSA signature check (integrity is still enforced)",
    )
    common.add_argument(
        "--allow-unverified",
        dest="allow_unverified",
        action="store_true",
        default=argparse.SUPPRESS,
        help="proceed when the signature cannot be checked at all (never when it fails)",
    )
    common.add_argument("--system", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--machine", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="oh-my-openagent.py",
        description="User-local, verified installer for oh-my-openagent.",
        epilog="Precedence: command-line flags > --config TOML > built-in defaults.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, func, helptext in (
        ("status", cmd_status, "show what is installed"),
        ("install", cmd_install, "install the resolved version"),
        ("update", cmd_update, "install a newer version and repoint current"),
        ("uninstall", cmd_uninstall, "remove everything this installer created"),
    ):
        sub = subparsers.add_parser(name, help=helptext, parents=[common])
        sub.set_defaults(func=func)
        if name == "status":
            sub.add_argument("--remote", action="store_true", help="also query the registry")
        else:
            sub.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "apply"):
        args.apply = False
    if not hasattr(args, "remote"):
        args.remote = False
    try:
        return int(args.func(build_config(args), args))
    except OhMyOpenagentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
