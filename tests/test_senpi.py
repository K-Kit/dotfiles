#!/usr/bin/env python3
"""Tests for scripts/setup/senpi.py.

Hermetic and offline: every test synthesizes its own root under a temp dir, and
no test performs network I/O or executes a downloaded artifact. Archives used by
the extraction tests are built in-process by tarfile, including deliberately
malicious ones.

Run directly: python3 tests/test_senpi.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "scripts" / "setup" / "senpi.py"

# importlib rather than sys.path.insert -- the latter is banned repo-wide and
# has crashed Claude Code sessions.
_spec = importlib.util.spec_from_file_location("senpi_installer", TARGET)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)

PASS = 0
FAIL = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {desc}" + (f" ({detail})" if detail else ""))


def raises(desc: str, fn) -> None:
    try:
        fn()
    except sp.SenpiError:
        check(desc, True)
        return
    except Exception as exc:  # noqa: BLE001 - wrong exception type is a failure
        check(desc, False, f"raised {type(exc).__name__}: {exc}")
        return
    check(desc, False, "no SenpiError raised")


def writable_tmp() -> str:
    """$TMPDIR is not reliably writable -- sandboxes pin it to a read-only
    runtime dir and can mount /tmp read-only too."""
    for cand in (os.environ.get("TMPDIR"), "/tmp/claude", "/tmp", "."):
        if not cand:
            continue
        try:
            Path(cand).mkdir(parents=True, exist_ok=True)
            probe = Path(tempfile.mkdtemp(dir=cand))
            shutil.rmtree(probe)
            return cand
        except OSError:
            continue
    raise RuntimeError("no writable temp directory")


TMP = writable_tmp()


def capture(fn, *a, **kw) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


def make_payload(dest: Path, version: str = "2026.8.14") -> None:
    """Write a minimal stand-in for an extracted release tree."""
    (dest / "pi" / "theme").mkdir(parents=True, exist_ok=True)
    (dest / "pi" / "pi").write_text("#!/bin/sh\nexit 0\n")
    (dest / "pi" / "pi").chmod(0o755)
    (dest / "pi" / "package.json").write_text(
        json.dumps({"name": "@code-yeongyu/senpi", "version": version})
    )
    (dest / "pi" / "photon_rs_bg.wasm").write_bytes(b"\0asm")


def make_tarball(path: Path, names: list[str], link: tuple[str, str] | None = None) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name in names:
            data = b"x"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link is not None:
            info = tarfile.TarInfo(link[0])
            info.type = tarfile.SYMTYPE
            info.linkname = link[1]
            tar.addfile(info)


# --------------------------------------------------------------------------
# Platform / architecture normalization and asset selection
# --------------------------------------------------------------------------

for system, machine, want in [
    ("Linux", "x86_64", ("linux", "x64")),
    ("linux", "amd64", ("linux", "x64")),
    ("Linux", "aarch64", ("linux", "arm64")),
    ("Darwin", "arm64", ("darwin", "arm64")),
    ("Darwin", "x86_64", ("darwin", "x64")),
    ("  DARWIN  ", " ARM64 ", ("darwin", "arm64")),
]:
    got = sp.normalize_platform(system, machine)
    check(f"normalize_platform({system!r},{machine!r}) == {want}", got == want, f"got {got}")

raises("windows is refused", lambda: sp.normalize_platform("Windows", "x86_64"))
raises("riscv is refused", lambda: sp.normalize_platform("Linux", "riscv64"))
raises("freebsd is refused", lambda: sp.normalize_platform("FreeBSD", "x86_64"))
raises("empty machine is refused", lambda: sp.normalize_platform("Linux", ""))

check("asset name linux/x64", sp.asset_name("linux", "x64") == "pi-linux-x64.tar.gz")
check("asset name darwin/arm64", sp.asset_name("darwin", "arm64") == "pi-darwin-arm64.tar.gz")
check(
    "x86_64 maps to upstream's x64 spelling, not amd64",
    sp.asset_name(*sp.normalize_platform("Linux", "x86_64")) == "pi-linux-x64.tar.gz",
)

# --------------------------------------------------------------------------
# Tag handling and URL construction
# --------------------------------------------------------------------------

check("normalize_tag adds v", sp.normalize_tag("2026.8.14") == "v2026.8.14")
check("normalize_tag keeps v", sp.normalize_tag("v2026.8.14") == "v2026.8.14")
check("normalize_tag keeps -N suffix", sp.normalize_tag("2026.8.12-4") == "v2026.8.12-4")
raises("empty version refused", lambda: sp.normalize_tag("  "))
raises("path traversal in version refused", lambda: sp.normalize_tag("../../etc"))
raises("query string in version refused", lambda: sp.normalize_tag("v1?x=1"))

asset_url, sums_url = sp.release_urls("v2026.8.14", "pi-linux-x64.tar.gz")
check(
    "asset and checksum URLs are built from the same tag",
    "/v2026.8.14/" in asset_url and "/v2026.8.14/" in sums_url,
    f"{asset_url} | {sums_url}",
)
check("checksum URL is not the latest alias", "latest" not in sums_url, sums_url)
check("asset URL ends with the asset", asset_url.endswith("/pi-linux-x64.tar.gz"))
check("checksum URL ends with SHA256SUMS", sums_url.endswith("/SHA256SUMS"))

# A tag from the network is validated exactly as a --version flag is: the
# default install path must not be the one that skips the check.
_real_get = sp._get
for payload, desc, want_raise in [
    (b'{"tag_name": "v2026.8.14"}', "clean tag passes through", False),
    (b'{"tag_name": "2026.8.14"}', "unprefixed tag from the API gains v", False),
    (b'{"tag_name": "../../etc/passwd"}', "traversal tag from the API refused", True),
    (b'{"tag_name": "v1?x=1"}', "URL-breaking tag from the API refused", True),
    (b'{"tag_name": ""}', "empty tag from the API refused", True),
]:
    sp._get = lambda _url, _p=payload: _p
    try:
        if want_raise:
            raises(f"resolve_latest_tag: {desc}", sp.resolve_latest_tag)
        else:
            check(f"resolve_latest_tag: {desc}", sp.resolve_latest_tag() == "v2026.8.14")
    finally:
        sp._get = _real_get

# --------------------------------------------------------------------------
# SHA256SUMS parsing -- upstream's file is NOT ordered, so keying is by name
# --------------------------------------------------------------------------

REAL_SUMS = """\
164931139e52e1a1447277918bc87e510cf5713981b3b6ae1d0fd70f437a0cf9  pi-darwin-arm64.tar.gz
f24acf9be052277935f2ba10481e425c7feaa8bda3a2c63492ecc3f0f16dd123  pi-darwin-x64.tar.gz
6ae3749757263fd443b4792e8af0b458f881071874fb0890c8625b488eba8d45  pi-linux-x64.tar.gz
df0e9b0c2c2bdc011b35b523860ab0534b2a5fbca64229e574311b5863e5ed68  pi-linux-arm64.tar.gz
e307a34c632ffe8926e8c34f018b4c9a5224481183d16d5c2fea957e9010c1e5  pi-windows-x64.zip
"""
sums = sp.parse_sha256sums(REAL_SUMS)
check(
    "linux-x64 digest keyed by filename, not line order",
    sums["pi-linux-x64.tar.gz"]
    == "6ae3749757263fd443b4792e8af0b458f881071874fb0890c8625b488eba8d45",
)
check(
    "linux-arm64 digest keyed by filename",
    sums["pi-linux-arm64.tar.gz"]
    == "df0e9b0c2c2bdc011b35b523860ab0534b2a5fbca64229e574311b5863e5ed68",
)
check("all five entries parsed", len(sums) == 5, str(len(sums)))
messy = sp.parse_sha256sums(
    "# comment\n\nnot-a-digest  foo.tar.gz\ndeadbeef  short.tar.gz\n"
    + ("aa" * 32)
    + "  *binary.tar.gz\n"
)
check("comments and blanks ignored", "foo.tar.gz" not in messy)
check("short digests rejected", "short.tar.gz" not in messy)
check("non-hex digests rejected", len(messy) == 1, str(messy))
check("leading * stripped from binary-mode names", "binary.tar.gz" in messy)

# --------------------------------------------------------------------------
# Archive member safety
# --------------------------------------------------------------------------

for name, ok in [
    ("pi/pi", True),
    ("pi/theme/dark.json", True),
    ("pi/", True),
    ("pi/../../evil", False),
    ("../evil", False),
    ("/etc/passwd", False),
    ("evil.txt", False),
    ("", False),
    # The pi/ anchor is the whole guarantee -- these pin the cases that used to
    # be covered by separate absolute-path and separator checks.
    ("/pi/pi", False),
    ("pi\\evil", False),
    ("./pi/pi", True),
    ("pi/./theme/x.json", True),
    ("pi/a/../b", False),
]:
    check(f"member_is_safe({name!r}) == {ok}", sp.member_is_safe(name) is ok)

work = Path(tempfile.mkdtemp(dir=TMP, prefix="senpi-extract-"))
good = work / "good.tar.gz"
make_tarball(good, ["pi/pi", "pi/package.json"])
sp.extract_release(good, work / "out-good")
check("safe archive extracts", (work / "out-good" / "pi" / "pi").is_file())

evil = work / "evil.tar.gz"
make_tarball(evil, ["pi/pi", "pi/../../evil"])
raises("traversal member refuses the whole archive", lambda: sp.extract_release(evil, work / "o1"))
check("nothing extracted from refused archive", not (work / "o1" / "pi").exists())

absolute = work / "abs.tar.gz"
make_tarball(absolute, ["/etc/passwd"])
raises("absolute member refused", lambda: sp.extract_release(absolute, work / "o2"))

escaping_link = work / "link.tar.gz"
make_tarball(escaping_link, ["pi/pi"], link=("pi/out", "../../../../etc/passwd"))
raises("escaping symlink refused", lambda: sp.extract_release(escaping_link, work / "o3"))

# --------------------------------------------------------------------------
# Deletion safety
# --------------------------------------------------------------------------

root = work / "root"
(root / "versions" / "v1").mkdir(parents=True)
check("version dir under root is removable", sp.is_safe_removal(root, root / "versions" / "v1"))
check("root itself is NOT removable", not sp.is_safe_removal(root, root))
check("/ is NOT removable", not sp.is_safe_removal(root, Path("/")))
check("$HOME is NOT removable", not sp.is_safe_removal(Path.home(), Path.home()))
check(
    "$HOME is NOT removable even when named as a target under a root",
    not sp.is_safe_removal(root, Path.home()),
)
check("sibling outside root is NOT removable", not sp.is_safe_removal(root, work / "elsewhere"))
outside = work / "outside"
outside.mkdir()
link_out = root / "versions" / "sneaky"
os.symlink(outside, link_out)
check("symlink pointing out of root is NOT removable", not sp.is_safe_removal(root, link_out))
link_out.unlink()

# --------------------------------------------------------------------------
# Launcher generation
# --------------------------------------------------------------------------

body = sp.launcher_body(Path("/home/u/.local/share/senpi"))
check("launcher is POSIX sh", body.startswith("#!/bin/sh\n"))
check("launcher execs through current/", 'exec "$SENPI_ROOT/current/pi/pi" "$@"' in body)
check("launcher forwards arguments", '"$@"' in body)
check("launcher touches no startup files", "bashrc" not in body and "zshrc" not in body)

lp = work / "launcher.sh"
lp.write_text(body)
check(
    "launcher_is_current true for matching body",
    sp.launcher_is_current(lp, Path("/home/u/.local/share/senpi")),
)
check("launcher_is_current false for other root", not sp.launcher_is_current(lp, Path("/other")))
lp.write_text("#!/bin/sh\necho hand-written\n")
check(
    "launcher_is_current false for foreign script",
    not sp.launcher_is_current(lp, Path("/home/u/.local/share/senpi")),
)
check("foreign launcher is not classified as managed", not sp.launcher_is_managed(lp))
lp.write_bytes(b"\xff\xfe\x00")
check(
    "binary foreign launcher is stale without crashing",
    not sp.launcher_is_current(lp, Path("/home/u/.local/share/senpi")),
)
check("binary foreign launcher is not managed", not sp.launcher_is_managed(lp))

if shutil.which("shellcheck"):
    sc = work / "senpi-launcher"
    sc.write_text(body)
    res = subprocess.run(
        ["shellcheck", "-s", "sh", str(sc)], capture_output=True, text=True, check=False
    )
    check("generated launcher is shellcheck-clean", res.returncode == 0, res.stdout + res.stderr)
else:
    print("skip: shellcheck not installed")

# --------------------------------------------------------------------------
# Version reading -- never by executing the payload
# --------------------------------------------------------------------------

vd = work / "vdir"
make_payload(vd, version="2026.8.14")
check("version read from bundled package.json", sp.read_payload_version(vd) == "2026.8.14")
check("missing manifest reads as None", sp.read_payload_version(work / "nope") is None)
(work / "bad").mkdir()
(work / "bad" / "pi").mkdir()
(work / "bad" / "pi" / "package.json").write_text("{not json")
check("corrupt manifest reads as None", sp.read_payload_version(work / "bad") is None)
check("is_complete requires exe and manifest", sp.is_complete(vd))
check("is_complete false for empty dir", not sp.is_complete(work / "nope"))

# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

parser = sp.build_parser()
a = parser.parse_args(["status"])
check("default install dir is under ~/.local/share", a.install_dir == sp.DEFAULT_ROOT)
check("default bin dir is ~/.local/bin", a.bin_dir == sp.DEFAULT_BIN_DIR)

a = parser.parse_args(["--install-dir", "/opt/senpi", "--bin-dir", "/opt/bin", "install"])
check("--install-dir override honoured", a.install_dir == Path("/opt/senpi"))
check("--bin-dir override honoured", a.bin_dir == Path("/opt/bin"))
check("install defaults to dry run", a.apply is False)
check("install defaults to latest", a.version is None)
check("install defaults to verified-only", a.allow_unverified is False)

a = parser.parse_args(["install", "--version", "v2026.8.14", "--apply"])
check("--version parsed", a.version == "v2026.8.14")
check("--apply parsed", a.apply is True)

a = parser.parse_args(["uninstall"])
check("uninstall defaults to dry run", a.apply is False)
check("uninstall defaults to all versions", a.version is None)

a = parser.parse_args(["update"])
check("update carries version=None so it resolves latest", a.version is None)
check("update defaults to dry run", a.apply is False)

for bad in ([], ["bogus"], ["install", "--version"]):
    try:
        # argparse writes its usage to stderr before exiting; swallow it so the
        # test output stays readable.
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(bad)
        check(f"argv {bad} rejected", False, "parsed successfully")
    except SystemExit:
        check(f"argv {bad} rejected", True)

# --------------------------------------------------------------------------
# Plan: printed steps and executed steps are one list, so they cannot diverge
# --------------------------------------------------------------------------

ran: list[str] = []
p = sp.Plan()
check("an empty plan is falsey", not p)
p.add("do the thing", lambda: ran.append("thing"))
p.add("do the other thing", lambda: ran.append("other"))
p.add("LEAVE something alone")  # announced, deliberately not executed
check("a populated plan is truthy", bool(p))

_, out = capture(p.show)
check("show prints every step", "do the thing" in out and "do the other thing" in out)
check("show prints announce-only steps", "LEAVE something alone" in out)
check("show executes nothing", ran == [], str(ran))

_, out = capture(p.run)
check("run executes every actionable step in order", ran == ["thing", "other"], str(ran))
check("run reports what it did", "ok: do the thing" in out)
check("run does not claim to have done announce-only steps", "ok: LEAVE" not in out, out)

# --------------------------------------------------------------------------
# Dry-run behaviour: install writes nothing without --apply
# --------------------------------------------------------------------------


def fresh_layout(name: str) -> sp.Layout:
    base = work / name
    return sp.Layout(base / "share", base / "bin")


lay = fresh_layout("dry")
args = parser.parse_args(["install", "--version", "v2026.8.14"])
rc, out = capture(sp.cmd_install, lay, args)
check("dry-run install exits 0", rc == 0, str(rc))
check("dry-run install says DRY RUN", "DRY RUN" in out)
check("dry-run install creates no root", not lay.root.exists())
check("dry-run install creates no launcher", not lay.launcher.exists())
check("dry-run install reports the pinned tag", "v2026.8.14 (pinned)" in out)
check("dry-run install names the checksum source", "SHA256SUMS" in out)

rc, out = capture(sp.cmd_status, lay, parser.parse_args(["status"]))
check("status on empty root exits 0", rc == 0)
check("status reports nothing installed", "(none)" in out)
check("status creates no directories", not lay.root.exists())

# --------------------------------------------------------------------------
# Idempotency: a fully wired install is a no-op
# --------------------------------------------------------------------------

lay = fresh_layout("idem")
lay.versions.mkdir(parents=True)
make_payload(lay.version_dir("v2026.8.14"))
os.symlink(Path("versions") / "v2026.8.14", lay.current)
lay.bin_dir.mkdir(parents=True)
lay.launcher.write_text(sp.launcher_body(lay.root))
lay.launcher.chmod(0o755)

before = sorted(str(p) for p in lay.root.rglob("*"))
rc, out = capture(sp.cmd_install, lay, parser.parse_args(["install", "--version", "2026.8.14"]))
check("re-install of the wired version is a no-op", "nothing to do" in out, out)
check("no-op install exits 0", rc == 0)
check(
    "no-op install changed nothing on disk", sorted(str(p) for p in lay.root.rglob("*")) == before
)
check("unversioned pin normalizes to the installed tag", "v2026.8.14" in out)

rc, out = capture(sp.cmd_status, lay, parser.parse_args(["status"]))
check("status sees the current tag", "v2026.8.14" in out)
check("status reads the payload version without executing it", "package.json 2026.8.14" in out)
check("status reports the launcher as up to date", "up to date" in out)

# A payload present but not wired up: install must plan the wiring, not a download.
lay2 = fresh_layout("partial")
lay2.versions.mkdir(parents=True)
make_payload(lay2.version_dir("v2026.8.14"))
rc, out = capture(sp.cmd_install, lay2, parser.parse_args(["install", "--version", "v2026.8.14"]))
check("partial install skips the download", "skipping download" in out, out)
check("partial install plans the current symlink", "point" in out and "current" in out)
check("partial install plans the launcher", "write launcher" in out)
check("partial install still writes nothing", not lay2.launcher.exists())

# The same layout with --apply. Every other install test is a dry run, so without
# this one nothing asserts that the plan's actions do what their descriptions say.
# The payload is already on disk, so no step here reaches the network.
rc, out = capture(
    sp.cmd_install, lay2, parser.parse_args(["install", "--version", "v2026.8.14", "--apply"])
)
check("apply install exits 0", rc == 0, str(rc))
check("apply install does not print DRY RUN", "DRY RUN" not in out)
check("apply install created the current symlink", lay2.current.is_symlink())
check(
    "current points at the pinned version",
    os.readlink(lay2.current) == str(Path("versions") / "v2026.8.14"),
    os.readlink(lay2.current) if lay2.current.is_symlink() else "(absent)",
)
check("apply install wrote the launcher", lay2.launcher.exists())
check("launcher is executable", lay2.launcher.stat().st_mode & 0o111 == 0o111)
check("launcher is the one status recognises", sp.launcher_is_current(lay2.launcher, lay2.root))
check("apply install reports the installed tag", "installed v2026.8.14" in out, out)
check(
    "apply install re-run is a no-op",
    "nothing to do"
    in capture(sp.cmd_install, lay2, parser.parse_args(["install", "--version", "v2026.8.14"]))[1],
)

# A same-named launcher owned by another installer must never be overwritten.
foreign_install = fresh_layout("foreign-install")
foreign_install.versions.mkdir(parents=True)
make_payload(foreign_install.version_dir("v2026.8.14"))
foreign_install.bin_dir.mkdir(parents=True)
foreign_install.launcher.write_text("#!/bin/sh\necho foreign\n")
raises(
    "install refuses to overwrite a foreign launcher",
    lambda: sp.cmd_install(
        foreign_install,
        parser.parse_args(["install", "--version", "v2026.8.14", "--apply"]),
    ),
)
check("foreign launcher survives refused install", "echo foreign" in foreign_install.launcher.read_text())

# --------------------------------------------------------------------------
# Update / version handling
# --------------------------------------------------------------------------

lay3 = fresh_layout("update")
lay3.versions.mkdir(parents=True)
make_payload(lay3.version_dir("v2026.8.13"), version="2026.8.13")
os.symlink(Path("versions") / "v2026.8.13", lay3.current)
check("current_tag reads the symlink", lay3.current_tag() == "v2026.8.13")
check("installed_tags lists payloads", lay3.installed_tags() == ["v2026.8.13"])

_real_resolve = sp.resolve_latest_tag
sp.resolve_latest_tag = lambda: "v2026.8.14"  # no network in tests
try:
    rc, out = capture(sp.cmd_update, lay3, parser.parse_args(["update"]))
    check("update reports current and latest", "v2026.8.13" in out and "v2026.8.14" in out)
    check("update on a newer release plans an install", "DRY RUN" in out, out)
    check("update writes nothing without --apply", lay3.current_tag() == "v2026.8.13")

    sp.resolve_latest_tag = lambda: "v2026.8.13"
    rc, out = capture(sp.cmd_update, lay3, parser.parse_args(["update"]))
    check("update is a no-op when already latest", "already on the latest release" in out, out)
    check("no-op update exits 0", rc == 0)
finally:
    sp.resolve_latest_tag = _real_resolve

# --------------------------------------------------------------------------
# Uninstall
# --------------------------------------------------------------------------

lay4 = fresh_layout("uninstall")
lay4.versions.mkdir(parents=True)
make_payload(lay4.version_dir("v2026.8.13"), version="2026.8.13")
make_payload(lay4.version_dir("v2026.8.14"))
os.symlink(Path("versions") / "v2026.8.14", lay4.current)
lay4.bin_dir.mkdir(parents=True)
lay4.launcher.write_text(sp.launcher_body(lay4.root))

empty = fresh_layout("uninstall-empty")
rc, out = capture(sp.cmd_uninstall, empty, parser.parse_args(["uninstall"]))
check("uninstall on an empty root reports nothing to do", "nothing to do" in out, out)
check("uninstall on an empty root prints no empty plan", "planned:" not in out, out)
check("uninstall on an empty root exits 0", rc == 0)
check("uninstall on an empty root creates nothing", not empty.root.exists())

rc, out = capture(sp.cmd_uninstall, lay4, parser.parse_args(["uninstall"]))
check("uninstall dry run says DRY RUN", "DRY RUN" in out)
check("uninstall dry run removes nothing", lay4.version_dir("v2026.8.14").exists())
check("uninstall mentions leaving user data alone", "~/.senpi" in out)

rc, out = capture(
    sp.cmd_uninstall, lay4, parser.parse_args(["uninstall", "--version", "v2026.8.13", "--apply"])
)
check("targeted uninstall removes only that version", not lay4.version_dir("v2026.8.13").exists())
check("targeted uninstall keeps the other version", lay4.version_dir("v2026.8.14").exists())
check("targeted uninstall keeps the launcher", lay4.launcher.exists())
check("targeted uninstall keeps the current symlink", lay4.current.is_symlink())

rc, out = capture(
    sp.cmd_uninstall, lay4, parser.parse_args(["uninstall", "--version", "v2026.8.13"])
)
check("uninstalling an absent version is a no-op", "nothing to do" in out, out)

# A launcher this script did not write must survive uninstall.
lay5 = fresh_layout("foreign")
lay5.versions.mkdir(parents=True)
make_payload(lay5.version_dir("v2026.8.14"))
lay5.bin_dir.mkdir(parents=True)
lay5.launcher.write_text('#!/bin/sh\n# someone else\'s senpi\nexec /usr/bin/senpi "$@"\n')
rc, out = capture(sp.cmd_uninstall, lay5, parser.parse_args(["uninstall", "--apply"]))
check("foreign launcher is left in place", lay5.launcher.exists())
check("foreign launcher is reported as left alone", "LEAVE" in out, out)
check("owned payloads are still removed", not lay5.version_dir("v2026.8.14").exists())
check("senpi root itself is never removed", lay5.root.exists())

# --------------------------------------------------------------------------

shutil.rmtree(work, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
