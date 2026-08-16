#!/usr/bin/env python3
"""Install, update, inspect and remove Senpi (code-yeongyu/senpi) locally.

Why this exists rather than the upstream instructions:

Upstream's own install documentation is a maintainer release runbook aimed at
named machines, and its general-audience path is npm (`@code-yeongyu/senpi`).
Neither is reusable here: npm needs node >= 24, writes into a global prefix, and
has no architecture selection. The GitHub release tarballs are the only channel
that is simultaneously user-local, dependency-free, and covered by
upstream-published verification material (a per-release SHA256SUMS file).

Design notes worth knowing before editing:

* The release payload is a *directory*, not a single binary. `pi/pi` ships next
  to `photon_rs_bg.wasm`, `theme/` and `examples/`, which it loads at runtime.
  So the whole tree is installed under a versioned root and reached through a
  tiny `sh` wrapper. A bare symlink would work only if the executable resolved
  its siblings via realpath(argv[0]) rather than dirname(argv[0]); we cannot
  determine which without running the binary, and running an unaudited 120MB
  executable is out of scope for an installer. The wrapper is correct either way.
* The installed version is read from the bundled `pi/package.json`. Nothing here
  ever executes the downloaded artifact -- not to get a version, not to verify a
  successful install.
* `latest` is resolved to a concrete tag exactly once, and both the asset URL and
  the SHA256SUMS URL are built from that one tag. Fetching the artifact from a
  pinned tag and the checksums from `/releases/latest/` would verify against the
  wrong file if a release landed mid-run.
* Tags are opaque strings. Upstream uses CalVer with occasional `-N` suffixes
  (`v2026.8.12-4`), which does not sort naively, so "latest" always comes from
  the GitHub releases/latest endpoint and never from sorting.

Idempotent: re-running after a partial apply adds only what is missing.
Dry-run by default; pass --apply to write.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

REPO = "code-yeongyu/senpi"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_BASE = f"https://github.com/{REPO}/releases/download"
SUMS_NAME = "SHA256SUMS"

# Upstream publishes pi-<os>-<arch>.tar.gz for the full cross-product of these
# two maps; windows ships .zip and is deliberately absent. Anything not spelled
# out here is refused, so widening support means adding a key -- there is no
# separate allowlist to keep in sync, because a second list stating the same
# cross-product could only ever agree with these maps or silently contradict them.
OS_MAP = {"linux": "linux", "darwin": "darwin"}
ARCH_MAP = {
    "x86_64": "x64",
    "amd64": "x64",
    "x64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

DEFAULT_ROOT = Path.home() / ".local" / "share" / "senpi"
DEFAULT_BIN_DIR = Path.home() / ".local" / "bin"
LAUNCHER_NAME = "senpi"
# Paths inside an extracted release tarball, relative to a version directory.
PAYLOAD_ROOT = "pi"
PAYLOAD_EXE = Path(PAYLOAD_ROOT) / "pi"
PAYLOAD_MANIFEST = Path(PAYLOAD_ROOT) / "package.json"

USER_AGENT = "dotfiles-senpi-installer"
HTTP_TIMEOUT = 60
# Refuse to extract implausibly large archives rather than filling the disk.
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024


class SenpiError(RuntimeError):
    """A condition the user must resolve; reported without a traceback."""


# --------------------------------------------------------------------------
# Pure helpers (all unit-tested without network or filesystem writes)
# --------------------------------------------------------------------------


def normalize_platform(system: str, machine: str) -> tuple[str, str]:
    """Map uname-ish strings onto upstream's asset naming.

    Upstream spells x86_64 as "x64", which is easy to invert by accident, so the
    mapping is explicit in both directions rather than derived.
    """
    os_name = OS_MAP.get(system.strip().lower(), "")
    arch = ARCH_MAP.get(machine.strip().lower(), "")
    if not os_name or not arch:
        raise SenpiError(
            f"unsupported platform {system}/{machine}; upstream publishes tarballs "
            "only for linux/darwin on x64/arm64 (windows ships .zip, not handled here)"
        )
    return os_name, arch


def asset_name(os_name: str, arch: str) -> str:
    return f"pi-{os_name}-{arch}.tar.gz"


def normalize_tag(version: str) -> str:
    """Accept `2026.8.14` or `v2026.8.14`; emit the tag upstream actually uses."""
    v = version.strip()
    if not v:
        raise SenpiError("empty version")
    if any(c in v for c in "/ \t\\?#"):
        raise SenpiError(f"refusing suspicious version string: {version!r}")
    return v if v.startswith("v") else f"v{v}"


def release_urls(tag: str, asset: str) -> tuple[str, str]:
    """Both URLs are built from one resolved tag -- see the module docstring."""
    return (
        f"{DOWNLOAD_BASE}/{tag}/{asset}",
        f"{DOWNLOAD_BASE}/{tag}/{SUMS_NAME}",
    )


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a coreutils-style SHA256SUMS body into {filename: digest}.

    Keyed by filename, never by position: upstream's file is not sorted, and the
    ordering has changed between releases.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        digest = digest.lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            continue
        out[name.lstrip("*")] = digest
    return out


def member_is_safe(name: str) -> bool:
    """Reject anything that would escape the extraction directory.

    Requiring the first path component to be exactly `pi/` is what does the work,
    and it subsumes the separate absolute-path and separator checks this used to
    carry: an absolute name splits with "/" as its first component, and a
    Windows-style "pi\\evil" does not split at all, so neither can ever equal
    PAYLOAD_ROOT. Those checks were unreachable, which is worse than absent --
    nothing can test them, so nothing can notice when they rot.

    If the anchor is ever relaxed to accept more than one root, the absolute and
    separator checks have to come back with it; they are not redundant in general,
    only against this anchor.
    """
    if not name:
        return False
    parts = Path(name).parts
    if ".." in parts:
        return False
    return parts[0] == PAYLOAD_ROOT


def is_safe_removal(root: Path, target: Path) -> bool:
    """True only when `target` is a real path strictly inside `root`.

    Guards uninstall against every broad target we can name: `/`, `$HOME`, the
    root itself, and anything reached by following a link out of the tree.
    """
    try:
        root_r = root.resolve()
        target_r = target.resolve()
    except OSError:
        return False
    forbidden = {Path("/"), Path.home().resolve(), Path.home().parent.resolve()}
    if root_r in forbidden or target_r in forbidden:
        return False
    if target_r == root_r:
        return False
    return root_r in target_r.parents


def launcher_body(root: Path) -> str:
    """The wrapper installed on PATH. Kept POSIX sh and shellcheck-clean."""
    return (
        "#!/bin/sh\n"
        "# Managed by dotfiles scripts/setup/senpi.py -- regenerated on install\n"
        "# and update. Local edits will be overwritten.\n"
        f'SENPI_ROOT="{root}"\n'
        'exec "$SENPI_ROOT/current/pi/pi" "$@"\n'
    )


def launcher_is_current(path: Path, root: Path) -> bool:
    try:
        return path.read_text() == launcher_body(root)
    except (OSError, UnicodeDecodeError):
        return False


def launcher_is_managed(path: Path) -> bool:
    """True only for a regular launcher previously written by this installer."""
    if not path.is_file() or path.is_symlink():
        return False
    try:
        return path.read_text().startswith(
            "#!/bin/sh\n# Managed by dotfiles scripts/setup/senpi.py"
        )
    except (OSError, UnicodeDecodeError):
        return False


def read_payload_version(version_dir: Path) -> str | None:
    """Version from the bundled package.json -- never by executing the binary."""
    manifest = version_dir / PAYLOAD_MANIFEST
    try:
        data = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return None
    v = data.get("version")
    return v if isinstance(v, str) else None


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _urlopen(url: str) -> Iterator[Any]:
    """Open `url`, translating urllib's exceptions into SenpiError.

    Both callers need identical headers, timeout and error wording, so they share
    one implementation rather than two copies that can drift apart.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            yield resp
    except urllib.error.HTTPError as exc:
        raise SenpiError(f"HTTP {exc.code} fetching {url}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SenpiError(f"network error fetching {url}: {exc}") from exc


def _get(url: str) -> bytes:
    with _urlopen(url) as resp:
        return resp.read()


def resolve_latest_tag() -> str:
    """Latest tag from GitHub, validated exactly as a user-supplied one is.

    The tag becomes both a path segment and a URL segment, so it must not carry
    separators or traversal -- and that has to hold for the *default* path too,
    not only when --version was passed. A response from the API is still remote
    data; validating only the flag would make the guarantee "validated if the
    user typed it" rather than "validated before touching disk".
    """
    data = json.loads(_get(API_LATEST).decode("utf-8"))
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise SenpiError("GitHub releases/latest returned no tag_name")
    return normalize_tag(tag)


def download_to(url: str, dest: Path) -> str:
    """Stream `url` into `dest`, returning the hex sha256 of what landed."""
    digest = hashlib.sha256()
    total = 0
    with _urlopen(url) as resp, dest.open("wb") as fh:
        while chunk := resp.read(1 << 20):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise SenpiError(f"{url} exceeds {MAX_ARCHIVE_BYTES} bytes; refusing")
            digest.update(chunk)
            fh.write(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def _check_member(member: tarfile.TarInfo) -> None:
    """Raise unless `member` is safe to write under the extraction root."""
    if not member_is_safe(member.name):
        raise SenpiError(f"refusing archive: unsafe member {member.name!r}")
    if member.issym() or member.islnk():
        target = member.linkname
        if os.path.isabs(target) or ".." in Path(target).parts:
            raise SenpiError(f"refusing archive: link {member.name!r} escapes to {target!r}")
    elif not (member.isfile() or member.isdir()):
        raise SenpiError(f"refusing archive: non-regular member {member.name!r}")


def extract_release(archive: Path, dest: Path) -> None:
    """Extract a release tarball to `dest`, refusing unsafe members.

    Two properties are load-bearing and pull against each other, so note why the
    shape is what it is:

    * One decompression pass. `getmembers()` on an `r:gz` stream scans to EOF,
      and the `extractall()` that follows seeks backwards -- which gzip serves by
      rewinding and inflating from byte 0 again. On a 120MB archive that is the
      whole payload decompressed twice. Walking with `next()` and extracting as
      we go keeps it to one forward pass.
    * A refused archive writes nothing *at dest*. Validating each member just
      before extracting it means safe members ahead of a malicious one would
      already be on disk, so the pass lands in a staging directory beside `dest`
      and is renamed into place only once the whole archive has validated. An
      unsafe member is still never written, in either arrangement.

    tarfile's data filter (3.12+) blocks traversal and device nodes too; the
    explicit check runs regardless so behaviour does not depend on the
    interpreter version, and so a refusal carries a message a user can act on.

    Precondition: `dest` is caller-owned and disposable. `os.replace` cannot
    overwrite a non-empty directory, so an existing `dest` is removed outright --
    without the `is_safe_removal` guard `uninstall` applies, because the only
    caller is `_fetch_and_install`, which passes a path inside its own private
    staging tree. A caller that ever passes a user-supplied path has to add that
    guard here first.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.unpack-", dir=dest.parent))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            while (member := tar.next()) is not None:
                _check_member(member)
                if sys.version_info >= (3, 12):
                    tar.extract(member, staging, filter="data")
                else:
                    tar.extract(member, staging)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


class Layout:
    def __init__(self, root: Path, bin_dir: Path) -> None:
        self.root = root
        self.bin_dir = bin_dir

    @property
    def versions(self) -> Path:
        return self.root / "versions"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def launcher(self) -> Path:
        return self.bin_dir / LAUNCHER_NAME

    def version_dir(self, tag: str) -> Path:
        return self.versions / tag

    def installed_tags(self) -> list[str]:
        try:
            return sorted(p.name for p in self.versions.iterdir() if p.is_dir())
        except OSError:
            return []

    def current_tag(self) -> str | None:
        try:
            return Path(os.readlink(self.current)).name
        except OSError:
            return None


def _payload_suffix(version_dir: Path) -> str:
    """` (package.json X)` when the manifest is readable, else nothing."""
    payload = read_payload_version(version_dir)
    return f" (package.json {payload})" if payload else ""


def is_complete(version_dir: Path) -> bool:
    return (version_dir / PAYLOAD_EXE).is_file() and (version_dir / PAYLOAD_MANIFEST).is_file()


def _symlink_atomic(target: Path, link: Path) -> None:
    tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------


class Plan:
    """An ordered list of (description, action) steps.

    Dry run prints the descriptions; --apply runs the actions. They are the same
    list, so a step cannot be executed without having been announced, or
    announced without being executed -- which is exactly the promise dry run
    makes and the one thing two hand-synced code paths could not guarantee.

    Steps whose action is None are announcements of something deliberately *not*
    done, e.g. leaving a launcher this script did not write.
    """

    def __init__(self) -> None:
        self.steps: list[tuple[str, Callable[[], None] | None]] = []

    def add(self, description: str, action: Callable[[], None] | None = None) -> None:
        self.steps.append((description, action))

    def __bool__(self) -> bool:
        return bool(self.steps)

    def show(self) -> None:
        print("\nplanned:")
        for description, _ in self.steps:
            print(f"  - {description}")

    def run(self) -> None:
        for description, action in self.steps:
            if action is not None:
                action()
                print(f"ok: {description}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_status(layout: Layout, args: argparse.Namespace) -> int:
    del args
    print(f"root        {layout.root}")
    print(f"bin dir     {layout.bin_dir}")
    tags = layout.installed_tags()
    print(f"installed   {', '.join(tags) if tags else '(none)'}")
    cur = layout.current_tag()
    if cur:
        print(f"current     {cur}{_payload_suffix(layout.version_dir(cur))}")
    else:
        print("current     (none)")
    launcher = layout.launcher
    if launcher.exists() or launcher.is_symlink():
        state = "up to date" if launcher_is_current(launcher, layout.root) else "STALE or foreign"
        print(f"launcher    {launcher} [{state}]")
    else:
        print(f"launcher    {launcher} (missing)")
    _warn_path(layout.bin_dir)
    return 0


def _warn_path(bin_dir: Path) -> None:
    entries = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if bin_dir not in entries:
        print(
            f"\nnote: {bin_dir} is not on PATH. Add it yourself -- this script never\n"
            "      edits shell startup files."
        )


def cmd_install(layout: Layout, args: argparse.Namespace) -> int:
    os_name, arch = normalize_platform(platform.system(), platform.machine())
    asset = asset_name(os_name, arch)

    if args.version:
        tag = normalize_tag(args.version)
        print(f"version     {tag} (pinned)")
    else:
        tag = resolve_latest_tag()
        print(f"version     {tag} (resolved from releases/latest)")

    asset_url, sums_url = release_urls(tag, asset)
    print(f"platform    {os_name}/{arch} -> {asset}")
    print(f"asset       {asset_url}")
    print(f"checksums   {sums_url}")

    version_dir = layout.version_dir(tag)
    already = is_complete(version_dir)
    if already:
        print(f"payload     already present at {version_dir} (skipping download)")

    needs_current = layout.current_tag() != tag
    needs_launcher = not launcher_is_current(layout.launcher, layout.root)

    if (
        needs_launcher
        and (layout.launcher.exists() or layout.launcher.is_symlink())
        and not launcher_is_managed(layout.launcher)
    ):
        raise SenpiError(
            f"refusing to overwrite foreign launcher {layout.launcher}; "
            "move it aside or choose another --bin-dir"
        )

    if already and not needs_current and not needs_launcher:
        print("\nnothing to do -- already installed and wired up.")
        return 0

    plan = Plan()
    if not already:
        plan.add(
            f"download + verify {asset} and extract to {version_dir}",
            lambda: _fetch_and_install(
                layout, tag, asset, asset_url, sums_url, args.allow_unverified
            ),
        )
    if needs_current:
        plan.add(
            f"point {layout.current} at versions/{tag}",
            lambda: _symlink_atomic(Path("versions") / tag, layout.current),
        )
    if needs_launcher:
        plan.add(f"write launcher {layout.launcher}", lambda: _write_launcher(layout))
    plan.show()

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return 0

    layout.versions.mkdir(parents=True, exist_ok=True)
    plan.run()

    print(f"\ninstalled {tag}{_payload_suffix(layout.version_dir(tag))}")
    print(
        "next: run `senpi --version` yourself to confirm the executable starts.\n"
        "      This installer never executes the downloaded binary."
    )
    _warn_path(layout.bin_dir)
    return 0


def _write_launcher(layout: Layout) -> None:
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    tmp = layout.launcher.with_name(f".{layout.launcher.name}.tmp-{os.getpid()}")
    tmp.write_text(launcher_body(layout.root))
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, layout.launcher)


def _fetch_and_install(
    layout: Layout,
    tag: str,
    asset: str,
    asset_url: str,
    sums_url: str,
    allow_unverified: bool,
) -> None:
    """Download, verify, extract and atomically publish one version directory.

    The staging directory lives under `versions/` rather than $TMPDIR so the
    final os.replace is a same-filesystem rename.
    """
    sums = parse_sha256sums(_get(sums_url).decode("utf-8", "replace"))
    expected = sums.get(asset)
    if expected is None:
        msg = (
            f"upstream {SUMS_NAME} for {tag} has no entry for {asset}; "
            "authenticity cannot be verified"
        )
        if not allow_unverified:
            raise SenpiError(msg + " -- refusing (pass --allow-unverified to override)")
        print(f"WARNING: {msg}; continuing because --allow-unverified was given")

    staging = Path(tempfile.mkdtemp(prefix=f".incoming-{tag}-", dir=layout.versions))
    try:
        archive = staging / asset
        print(f"downloading {asset} ...")
        actual = download_to(asset_url, archive)
        if expected is not None:
            if actual != expected:
                raise SenpiError(
                    f"checksum mismatch for {asset}\n  expected {expected}\n  actual   {actual}"
                )
            print(f"ok: sha256 matches upstream {SUMS_NAME} ({actual})")

        payload = staging / "payload"
        extract_release(archive, payload)
        if not is_complete(payload):
            raise SenpiError(f"extracted archive is missing {PAYLOAD_EXE} or {PAYLOAD_MANIFEST}")
        (payload / PAYLOAD_EXE).chmod(0o755)
        archive.unlink()

        dest = layout.version_dir(tag)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(payload, dest)
        print(f"ok: installed payload at {dest}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def cmd_update(layout: Layout, args: argparse.Namespace) -> int:
    """Thin path over install: resolve latest, then install if it differs."""
    tag = resolve_latest_tag()
    cur = layout.current_tag()
    print(f"current     {cur or '(none)'}")
    print(f"latest      {tag}")
    if cur == tag and is_complete(layout.version_dir(tag)):
        print("\nalready on the latest release.")
        return 0
    args.version = tag
    return cmd_install(layout, args)


def cmd_uninstall(layout: Layout, args: argparse.Namespace) -> int:
    targets: list[Path] = []
    if args.version:
        tag = normalize_tag(args.version)
        d = layout.version_dir(tag)
        if not d.exists():
            print(f"nothing to do -- {d} does not exist.")
            return 0
        targets.append(d)
    else:
        targets.extend(layout.version_dir(t) for t in layout.installed_tags())

    unsafe = [t for t in targets if not is_safe_removal(layout.root, t)]
    if unsafe:
        raise SenpiError(
            "refusing to remove paths outside the senpi root: " + ", ".join(str(u) for u in unsafe)
        )

    remove_all = not args.version
    launcher_ours = launcher_is_current(layout.launcher, layout.root)

    plan = Plan()
    for t in targets:
        plan.add(f"removed {t}", lambda t=t: shutil.rmtree(t))
    if remove_all:
        if layout.current.is_symlink():
            plan.add(f"removed {layout.current}", layout.current.unlink)
        if launcher_ours:
            plan.add(f"removed {layout.launcher}", layout.launcher.unlink)
        elif layout.launcher.exists():
            plan.add(f"LEAVE {layout.launcher} (not written by this script)")

    if not plan:
        print("nothing to do -- no senpi payloads or launcher found.")
        return 0

    plan.show()
    print("\nnote: user data under ~/.senpi is never touched by this script.")
    if not args.apply:
        print("\nDRY RUN -- nothing removed. Re-run with --apply.")
        return 0

    plan.run()
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="senpi.py",
        description="Install Senpi from upstream GitHub release tarballs, user-locally.",
    )
    p.add_argument(
        "--install-dir",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"root for versioned payloads (default: {DEFAULT_ROOT})",
    )
    p.add_argument(
        "--bin-dir",
        type=Path,
        default=DEFAULT_BIN_DIR,
        help=f"directory for the `senpi` launcher (default: {DEFAULT_BIN_DIR})",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="report what is installed (never writes)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("install", help="install a release (dry-run unless --apply)")
    s.add_argument("--version", help="pin a release, e.g. v2026.8.14 (default: latest)")
    s.add_argument("--apply", action="store_true", help="actually write")
    s.add_argument(
        "--allow-unverified",
        action="store_true",
        help="proceed when upstream publishes no checksum for the asset (loud warning)",
    )
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("update", help="install the latest release if newer")
    s.add_argument("--apply", action="store_true", help="actually write")
    s.add_argument("--allow-unverified", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_update, version=None)

    s = sub.add_parser("uninstall", help="remove installed payloads (dry-run unless --apply)")
    s.add_argument("--version", help="remove only this release (default: all)")
    s.add_argument("--apply", action="store_true", help="actually remove")
    s.set_defaults(func=cmd_uninstall)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    layout = Layout(args.install_dir.expanduser(), args.bin_dir.expanduser())
    try:
        return args.func(layout, args)
    except SenpiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
