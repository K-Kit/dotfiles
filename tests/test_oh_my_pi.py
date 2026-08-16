#!/usr/bin/env python3
"""Tests for scripts/setup/oh-my-pi.py.

Hermetic and offline: every test synthesizes its own root under a temp dir, no
test performs network I/O, and no test executes a downloaded artifact. The
network layer is poisoned at import time so an accidental fetch fails loudly
rather than reaching GitHub.

Run: python3 tests/test_oh_my_pi.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "scripts" / "setup" / "oh-my-pi.py"

# importlib rather than sys.path.insert -- the latter is banned repo-wide and
# has crashed Claude Code sessions.
_spec = importlib.util.spec_from_file_location("oh_my_pi_installer", TARGET)
omp = importlib.util.module_from_spec(_spec)
# dataclasses resolves string annotations through sys.modules, so the module
# must be registered before exec_module or every @dataclass in it blows up.
sys.modules[_spec.name] = omp
_spec.loader.exec_module(omp)


def _no_network(url: str):
    raise AssertionError(f"test attempted network I/O: {url}")


_REAL_URLOPEN = omp._urlopen
omp._urlopen = _no_network

PASS = 0
FAIL = 0


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}{(' -- ' + detail) if detail else ''}")


def raises(desc: str, fn, exc: type = omp.OhMyPiError) -> None:
    try:
        fn()
    except exc:
        check(desc, True)
    except Exception as other:  # noqa: BLE001 - reporting wrong exception type
        check(desc, False, f"raised {type(other).__name__}: {other}")
    else:
        check(desc, False, "did not raise")


def writable_tmp() -> str:
    for candidate in (os.environ.get("TMPDIR"), "/tmp/claude", "/tmp", "."):
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_dir() and os.access(path, os.W_OK):
            return str(path)
    return "."


def capture(fn, *a, **kw) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*a, **kw)
    return rc, buf.getvalue()


# Real SHA256SUMS.txt from oh-my-pi v17.3.4, fetched 2026-08-15.
REAL_SUMS = """\
e2eba38151a6284e72b4626592422a2203682397fd124dfb9332d8a95ed60680  omp-browser-relay-extension.zip
76a6c22f8ba4ba319e3d528adcd921949e0338f2b13042721e64b990f6fffe16  omp-darwin-arm64
cc295533c32d1e5dc1febe8c5972c1235e41b9614318bec57441d03c475f70c1  omp-darwin-x64
8e27e7bfe49fc0f33f6cb0b50128ab85fe5403330d1dfb5bb34cf1f7422cdce8  omp-linux-arm64
612eb3af82c3d49e6cfc31289083483f8f37d7312805451cbed9cedcdf7cbd17  omp-linux-musl-arm64
13f908b56315835f01c3f0cec0dd7f0761d2cb7492970b54b28cd70e2693a33c  omp-linux-musl-x64
3fce4b25628064b0cd7bfbc6245ecdada331750ed4b341aca6bd29ba4478aab5  omp-linux-x64
10b985678aeed0609d5a66257fb77b55abad7f7c6c18ce671033bac13a72bc02  omp-windows-x64.exe
"""

LINUX_X64_SHA = "3fce4b25628064b0cd7bfbc6245ecdada331750ed4b341aca6bd29ba4478aab5"


# --------------------------------------------------------------------------
print("\n== platform normalization ==")

check("normalize_os linux", omp.normalize_os("Linux") == "linux")
check("normalize_os darwin", omp.normalize_os("Darwin") == "darwin")
raises("normalize_os rejects Windows", lambda: omp.normalize_os("Windows"))
raises("normalize_os rejects FreeBSD", lambda: omp.normalize_os("FreeBSD"))

check("normalize_arch x86_64", omp.normalize_arch("x86_64") == "x64")
check("normalize_arch amd64", omp.normalize_arch("amd64") == "x64")
check("normalize_arch aarch64", omp.normalize_arch("aarch64") == "arm64")
check("normalize_arch arm64", omp.normalize_arch("arm64") == "arm64")
raises("normalize_arch rejects i686", lambda: omp.normalize_arch("i686"))
raises("normalize_arch rejects riscv64", lambda: omp.normalize_arch("riscv64"))

check(
    "detect_arch: darwin + x86_64 + rosetta sysctl -> arm64",
    omp.detect_arch("Darwin", "x86_64", lambda _n: "1") == "arm64",
)
check(
    "detect_arch: darwin + x86_64 + no rosetta -> x64",
    omp.detect_arch("Darwin", "x86_64", lambda _n: "") == "x64",
)
check(
    "detect_arch: darwin native arm64 -> arm64",
    omp.detect_arch("Darwin", "arm64", lambda _n: "1") == "arm64",
)
check(
    "detect_arch: linux ignores the sysctl probe",
    omp.detect_arch("Linux", "x86_64", lambda _n: "1") == "x64",
)


# --------------------------------------------------------------------------
print("\n== libc detection (filesystem only, nothing executed) ==")

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    root = Path(td)
    (root / "lib").mkdir()
    check("detect_libc: bare root -> gnu", omp.detect_libc(root) == "gnu")

    (root / "etc").mkdir()
    (root / "etc/alpine-release").write_text("3.20.0\n", encoding="utf-8")
    check("detect_libc: alpine-release -> musl", omp.detect_libc(root) == "musl")

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    root = Path(td)
    (root / "lib").mkdir()
    (root / "lib/ld-musl-x86_64.so.1").write_text("", encoding="utf-8")
    check("detect_libc: musl loader -> musl", omp.detect_libc(root) == "musl")

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    root = Path(td)
    (root / "lib").mkdir()
    (root / "lib/ld-linux-x86-64.so.2").write_text("", encoding="utf-8")
    check("detect_libc: glibc loader -> gnu", omp.detect_libc(root) == "gnu")

check("detect_libc: missing /lib does not raise", omp.detect_libc(Path("/nonexistent-xyz")) == "gnu")


# --------------------------------------------------------------------------
print("\n== asset names ==")

check("asset linux/x64/gnu", omp.asset_name("linux", "x64", "gnu") == "omp-linux-x64")
check("asset linux/arm64/gnu", omp.asset_name("linux", "arm64", "gnu") == "omp-linux-arm64")
check("asset linux/x64/musl", omp.asset_name("linux", "x64", "musl") == "omp-linux-musl-x64")
check(
    "asset linux/arm64/musl",
    omp.asset_name("linux", "arm64", "musl") == "omp-linux-musl-arm64",
)
check("asset darwin/x64", omp.asset_name("darwin", "x64", "gnu") == "omp-darwin-x64")
check("asset darwin/arm64", omp.asset_name("darwin", "arm64", "gnu") == "omp-darwin-arm64")
raises("asset rejects musl on darwin", lambda: omp.asset_name("darwin", "arm64", "musl"))
raises("asset rejects unresolved libc", lambda: omp.asset_name("linux", "x64", "auto"))

# Every name this script can generate must exist in the real release. This is
# the self-check that replaces a second hardcoded platform table.
_real = omp.parse_sha256sums(REAL_SUMS)
_generated = [
    omp.asset_name(o, a, libc)
    for o in ("linux", "darwin")
    for a in ("x64", "arm64")
    for libc in ("gnu", "musl")
    if not (o == "darwin" and libc == "musl")
]
check("generated asset set has 6 entries", len(_generated) == 6, str(_generated))
check(
    "every generated asset name exists in the real v17.3.4 SHA256SUMS.txt",
    all(name in _real for name in _generated),
    str([n for n in _generated if n not in _real]),
)


# --------------------------------------------------------------------------
print("\n== tags and URLs ==")

check("normalize_tag adds v", omp.normalize_tag("17.3.4") == "v17.3.4")
check("normalize_tag keeps v", omp.normalize_tag("v17.3.4") == "v17.3.4")
check("normalize_tag strips whitespace", omp.normalize_tag("  v17.3.4  ") == "v17.3.4")
raises("normalize_tag rejects empty", lambda: omp.normalize_tag("  "))
raises("normalize_tag rejects 'latest'", lambda: omp.normalize_tag("latest"))
raises("normalize_tag rejects path traversal", lambda: omp.normalize_tag("../../etc"))
raises("normalize_tag rejects slashes", lambda: omp.normalize_tag("v1/../../x"))
raises("normalize_tag rejects query chars", lambda: omp.normalize_tag("v1?x=1"))
raises("normalize_tag rejects fragments", lambda: omp.normalize_tag("v1#frag"))
raises("normalize_tag rejects quotes", lambda: omp.normalize_tag('v1"x'))

_asset_url, _sums_url = omp.release_urls("v17.3.4", "omp-linux-x64")
check(
    "asset URL",
    _asset_url == "https://github.com/can1357/oh-my-pi/releases/download/v17.3.4/omp-linux-x64",
    _asset_url,
)
check(
    "sums URL comes from the same tag",
    _sums_url
    == "https://github.com/can1357/oh-my-pi/releases/download/v17.3.4/SHA256SUMS.txt",
    _sums_url,
)


# --------------------------------------------------------------------------
print("\n== SHA256SUMS parsing ==")

check("parses all 8 real entries", len(_real) == 8, str(sorted(_real)))
check("linux-x64 digest", _real["omp-linux-x64"] == LINUX_X64_SHA)
check(
    "darwin-arm64 digest",
    _real["omp-darwin-arm64"]
    == "76a6c22f8ba4ba319e3d528adcd921949e0338f2b13042721e64b990f6fffe16",
)
check(
    "binary-mode asterisk is stripped",
    omp.parse_sha256sums(f"{LINUX_X64_SHA} *omp-linux-x64")["omp-linux-x64"] == LINUX_X64_SHA,
)
check(
    "uppercase digests are normalized",
    omp.parse_sha256sums(f"{LINUX_X64_SHA.upper()}  omp-linux-x64")["omp-linux-x64"]
    == LINUX_X64_SHA,
)
check(
    "comments and blank lines are skipped",
    omp.parse_sha256sums(f"# header\n\n{LINUX_X64_SHA}  omp-linux-x64\n") == {
        "omp-linux-x64": LINUX_X64_SHA
    },
)
raises("rejects a short digest", lambda: omp.parse_sha256sums("abc123  omp-linux-x64"))
raises(
    "rejects a non-hex digest",
    lambda: omp.parse_sha256sums("z" * 64 + "  omp-linux-x64"),
)
raises("rejects a line with no filename", lambda: omp.parse_sha256sums(LINUX_X64_SHA))
raises("rejects an empty sums file", lambda: omp.parse_sha256sums("\n\n# nothing\n"))

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    blob = Path(td) / "blob"
    blob.write_bytes(b"oh-my-pi")
    check(
        "sha256_file digest is exact",
        omp.sha256_file(blob)
        == "e6de263005031ce86dbfe2cf3e754df2103bf6c378b7f23724c3675cb35c6bf0",
        omp.sha256_file(blob),
    )


# --------------------------------------------------------------------------
print("\n== config: TOML, layering, validation ==")

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    cfg = Path(td) / "omp.toml"

    cfg.write_text(
        '[install]\ninstall_dir = "/opt/omp"\nversion = "v17.3.4"\n'
        '[platform]\nlibc = "musl"\n[security]\nallow_unverified = true\n',
        encoding="utf-8",
    )
    parsed = omp.config_from_toml(cfg)
    check(
        "toml maps install.install_dir -> install_dir",
        parsed["install_dir"] == "/opt/omp",
        str(parsed),
    )
    check("toml maps install.version -> version", parsed["version"] == "v17.3.4")
    check("toml maps platform.libc -> libc", parsed["libc"] == "musl")
    check("toml maps security.allow_unverified", parsed["allow_unverified"] is True)

    cfg.write_text('[install]\nnope = "x"\n', encoding="utf-8")
    raises("unknown key is an error", lambda: omp.config_from_toml(cfg), omp.ConfigError)

    cfg.write_text("[install]\nversion = 17\n", encoding="utf-8")
    raises("int for a str field is an error", lambda: omp.config_from_toml(cfg), omp.ConfigError)

    cfg.write_text("[install]\nversion = true\n", encoding="utf-8")
    raises(
        "bool for a str field is an error (bool subclasses int)",
        lambda: omp.config_from_toml(cfg),
        omp.ConfigError,
    )

    cfg.write_text('[security]\nallow_unverified = "yes"\n', encoding="utf-8")
    raises("str for a bool field is an error", lambda: omp.config_from_toml(cfg), omp.ConfigError)

    cfg.write_text('[install]\ngithub_token = "abc"\n', encoding="utf-8")
    raises(
        "a secret-looking key is refused outright",
        lambda: omp.config_from_toml(cfg),
        omp.ConfigError,
    )

    cfg.write_text('[security]\napi_password = "hunter2"\n', encoding="utf-8")
    raises(
        "another secret-looking key is refused",
        lambda: omp.config_from_toml(cfg),
        omp.ConfigError,
    )

    cfg.write_text("[install\n", encoding="utf-8")
    raises("invalid TOML is an error", lambda: omp.config_from_toml(cfg), omp.ConfigError)

    raises(
        "missing config file is an error",
        lambda: omp.config_from_toml(Path(td) / "absent.toml"),
        omp.ConfigError,
    )

_default = omp.resolve_config()
check(
    "default install_dir",
    _default.install_dir == Path("~/.local/share/oh-my-pi"),
    str(_default.install_dir),
)
check("default bin_dir", _default.bin_dir == Path("~/.local/bin"), str(_default.bin_dir))
check("default version", _default.version == "latest")
check("default libc", _default.libc == "auto")
check("default allow_unverified", _default.allow_unverified is False)

_layered = omp.resolve_config({"version": "v1.0.0"}, {"version": None})
check("None CLI value does not override TOML", _layered.version == "v1.0.0")

_layered = omp.resolve_config({"version": "v1.0.0"}, {"version": "v2.0.0"})
check("CLI beats TOML", _layered.version == "v2.0.0")

_layered = omp.resolve_config({}, {"install_dir": Path("/opt/x")})
check("CLI beats defaults", _layered.install_dir == Path("/opt/x"))

_layered = omp.resolve_config({"install_dir": "~/elsewhere"}, {})
check(
    "TOML paths are expanded",
    _layered.install_dir == Path("~/elsewhere").expanduser(),
    str(_layered.install_dir),
)

raises(
    "invalid libc is rejected",
    lambda: omp.resolve_config({}, {"libc": "uclibc"}),
    omp.ConfigError,
)
raises(
    "invalid os is rejected",
    lambda: omp.resolve_config({}, {"os_name": "windows"}),
    omp.ConfigError,
)
raises(
    "invalid arch is rejected",
    lambda: omp.resolve_config({}, {"arch": "i686"}),
    omp.ConfigError,
)
raises(
    "unsafe version is rejected during config validation",
    lambda: omp.resolve_config({}, {"version": "../evil"}),
    omp.OhMyPiError,
)
check(
    "a legal-but-different libc value is accepted",
    omp.resolve_config({}, {"libc": "musl"}).libc == "musl",
)
check(
    "a legal-but-different arch value is accepted",
    omp.resolve_config({}, {"arch": "arm64"}).arch == "arm64",
)

check(
    "the shipped example config parses and only sets known keys",
    set(omp.config_from_toml(HERE.parent / "config" / "oh-my-pi.toml.example"))
    <= set(omp.TOML_MAP.values()),
)
check(
    "the shipped example config is non-empty",
    len(omp.config_from_toml(HERE.parent / "config" / "oh-my-pi.toml.example")) >= 4,
)


# --------------------------------------------------------------------------
print("\n== layout, metadata, launcher ownership ==")


def make_tree(base: Path, tag: str = "v17.3.4", *, sha: str | None = None) -> omp.Layout:
    """Synthesize an installed tree. The 'binary' is a text file, never run."""
    layout = omp.Layout(base / "root", base / "bin")
    vdir = layout.version_dir(tag)
    vdir.mkdir(parents=True)
    binary = vdir / "omp"
    binary.write_text("not a real binary\n", encoding="utf-8")
    recorded_sha = sha or omp.sha256_file(binary)
    (vdir / "metadata.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "tag": tag,
                "asset": "omp-linux-x64",
                "sha256": recorded_sha,
                "verified": True,
                "installed_at": "2026-08-15T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    os.symlink(Path("versions") / tag, layout.current)
    layout.bin_dir.mkdir(parents=True)
    os.symlink(layout.current / "omp", layout.launcher)
    return layout


with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    layout = make_tree(base)
    installed_sha = omp.sha256_file(layout.binary("v17.3.4"))
    check("installed_tags", layout.installed_tags() == ["v17.3.4"], str(layout.installed_tags()))
    check("current_tag", layout.current_tag() == "v17.3.4", str(layout.current_tag()))
    check("is_installed with matching digest", omp.is_installed(layout, "v17.3.4", installed_sha))
    check("is_installed without a digest argument", omp.is_installed(layout, "v17.3.4"))
    check(
        "is_installed is False on a digest mismatch",
        omp.is_installed(layout, "v17.3.4", "0" * 64) is False,
    )
    check("is_installed is False for an absent tag", omp.is_installed(layout, "v9.9.9") is False)
    layout.binary("v17.3.4").write_text("tampered\n", encoding="utf-8")
    check(
        "is_installed rehashes the payload instead of trusting metadata",
        omp.is_installed(layout, "v17.3.4") is False,
    )
    layout.binary("v17.3.4").write_text("not a real binary\n", encoding="utf-8")

    state, detail = omp.launcher_state(layout)
    check("launcher_state: ours", state == "ours", f"{state}: {detail}")

    # Metadata removed -> not installed, even though the file is still there.
    layout.metadata_path("v17.3.4").unlink()
    check(
        "is_installed is False when the metadata sidecar is missing",
        omp.is_installed(layout, "v17.3.4") is False,
    )

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    layout = omp.Layout(base / "root", base / "bin")
    check("installed_tags on an empty root", layout.installed_tags() == [])
    check("current_tag on an empty root", layout.current_tag() is None)
    state, _ = omp.launcher_state(layout)
    check("launcher_state: absent", state == "absent", state)

    # Upstream's curl|sh writes a REAL binary to ~/.local/bin/omp.
    layout.bin_dir.mkdir(parents=True)
    layout.launcher.write_text("#!/bin/sh\necho upstream\n", encoding="utf-8")
    state, detail = omp.launcher_state(layout)
    check("launcher_state: regular file is foreign", state == "foreign", f"{state}: {detail}")

    layout.launcher.unlink()
    other = base / "somewhere-else"
    other.write_text("x", encoding="utf-8")
    os.symlink(other, layout.launcher)
    state, detail = omp.launcher_state(layout)
    check(
        "launcher_state: symlink outside our root is foreign",
        state == "foreign",
        f"{state}: {detail}",
    )


# --------------------------------------------------------------------------
print("\n== removal safety ==")

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    root = Path(td) / "root"
    root.mkdir()
    check("root itself is removable", omp.is_safe_removal(root, root) is True)
    check(
        "a child of root is removable",
        omp.is_safe_removal(root, root / "versions" / "v1") is True,
    )
    check("/ is not removable", omp.is_safe_removal(root, Path("/")) is False)
    check("$HOME is not removable", omp.is_safe_removal(root, Path.home()) is False)
    check(
        "$HOME's parent is not removable",
        omp.is_safe_removal(root, Path.home().parent) is False,
    )
    check(
        "root's parent is not removable",
        omp.is_safe_removal(root, root.parent) is False,
    )
    check(
        "a sibling of root is not removable",
        omp.is_safe_removal(root, root.parent / "other") is False,
    )

# The blocklist only bites when the *root itself* is a dangerous path -- a
# misconfigured --install-dir. Testing it with a temp root proves nothing,
# because a temp root is already outside every blocked path.
check(
    "an install root of $HOME cannot be removed",
    omp.is_safe_removal(Path.home(), Path.home()) is False,
)
check(
    "an install root of / cannot be removed",
    omp.is_safe_removal(Path("/"), Path("/")) is False,
)
check(
    "an install root of $HOME's parent cannot be removed",
    omp.is_safe_removal(Path.home().parent, Path.home().parent) is False,
)
check(
    "a normal root is still removable",
    omp.is_safe_removal(Path.home() / ".local/share/oh-my-pi", Path.home() / ".local/share/oh-my-pi")
    is True,
)


# --------------------------------------------------------------------------
print("\n== plan objects ==")

_plan = omp.Plan()
_ran: list[str] = []
_plan.add("inert note")
_plan.add("real step", lambda: _ran.append("did it"))
_rc, _out = capture(_plan.show)
check("show lists both steps", _out.count("\n") == 2, repr(_out))
check("show marks the inert step", "- inert note" in _out, repr(_out))
check("show does not execute", _ran == [])
capture(_plan.run)
check("run executes the action", _ran == ["did it"], str(_ran))

_empty = omp.Plan()
_rc, _out = capture(_empty.show)
check("empty plan says so", "(nothing to do)" in _out, repr(_out))

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    (base / "a").mkdir()
    (base / "b").mkdir()
    link = base / "link"
    omp._symlink_atomic(base / "a", link)
    check("symlink created", os.readlink(link) == str(base / "a"))
    omp._symlink_atomic(base / "b", link)
    check("symlink replaced atomically", os.readlink(link) == str(base / "b"))
    check(
        "no temp symlink left behind",
        [p.name for p in base.iterdir() if ".tmp-" in p.name] == [],
    )


# --------------------------------------------------------------------------
print("\n== install plan (no network, no writes) ==")

TARGET_LINUX = omp.Target("linux", "x64", "gnu", "omp-linux-x64")


def resolution(tag: str = "v17.3.4", sha: str = LINUX_X64_SHA) -> omp.Resolution:
    return omp.Resolution(tag, TARGET_LINUX, sha, True)


with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    config = omp.resolve_config({}, {"install_dir": base / "root", "bin_dir": base / "bin"})
    layout = omp.Layout(config.install_dir, config.bin_dir)

    plan = omp._build_install_plan(config, layout, resolution(), force=False)
    _rc, out = capture(plan.show)
    check("fresh install plans a download", "download omp-linux-x64" in out, out)
    check("fresh install mentions verification", "verified against SHA256SUMS.txt" in out, out)
    check("fresh install plans the current symlink", "at versions/v17.3.4" in out, out)
    check("fresh install plans the launcher link", "link " in out and "-> " in out, out)
    check(
        "building and showing a plan creates nothing on disk",
        not (base / "root").exists() and not (base / "bin").exists(),
    )

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    layout = make_tree(base)
    config = omp.resolve_config({}, {"install_dir": layout.root, "bin_dir": layout.bin_dir})
    plan = omp._build_install_plan(
        config,
        layout,
        resolution(sha=omp.sha256_file(layout.binary("v17.3.4"))),
        force=False,
    )
    _rc, out = capture(plan.show)
    check("idempotent: no download planned", "download omp-linux-x64" not in out, out)
    check("idempotent: already installed", "already installed" in out, out)
    check("idempotent: current already correct", "current already points at" in out, out)
    check("idempotent: launcher already correct", "already links to" in out, out)
    check(
        "idempotent plan has no executable actions",
        all(action is None for _d, action in plan.steps),
    )

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    layout = make_tree(base)
    config = omp.resolve_config({}, {"install_dir": layout.root, "bin_dir": layout.bin_dir})
    # A digest change for the same tag must re-download, not be skipped.
    plan = omp._build_install_plan(config, layout, resolution(sha="a" * 64), force=False)
    _rc, out = capture(plan.show)
    check("digest mismatch on an installed tag re-downloads", "download omp-linux-x64" in out, out)

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    layout = omp.Layout(base / "root", base / "bin")
    layout.bin_dir.mkdir(parents=True)
    layout.launcher.write_text("real upstream binary\n", encoding="utf-8")
    config = omp.resolve_config({}, {"install_dir": layout.root, "bin_dir": layout.bin_dir})

    plan = omp._build_install_plan(config, layout, resolution(), force=False)
    _rc, out = capture(plan.show)
    check("foreign launcher: refuses to touch it", "LEAVE" in out, out)
    check("foreign launcher: suggests --force", "--force" in out, out)
    check("foreign launcher: no link step planned", "link " not in out, out)

    plan = omp._build_install_plan(config, layout, resolution(), force=True)
    _rc, out = capture(plan.show)
    check("foreign launcher + --force: moves aside", "aside" in out, out)
    check("foreign launcher + --force: never deletes", "moved, never deleted" in out, out)
    check("foreign launcher + --force: then links", "link " in out, out)
    check(
        "the foreign binary is still untouched after planning",
        layout.launcher.read_text(encoding="utf-8") == "real upstream binary\n",
    )


# --------------------------------------------------------------------------
print("\n== uninstall plan ==")

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    layout = make_tree(base)
    plan = omp._build_uninstall_plan(layout, None)
    _rc, out = capture(plan.show)
    check("full uninstall removes our launcher", "remove our launcher" in out, out)
    check("full uninstall removes the root", "remove the whole install root" in out, out)
    check("full uninstall spares ~/.omp", "LEAVE ~/.omp" in out, out)
    check("planning removed nothing", layout.version_dir("v17.3.4").is_dir())

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    layout = make_tree(base)
    plan = omp._build_uninstall_plan(layout, "v17.3.4")
    _rc, out = capture(plan.show)
    check("tag uninstall removes just that version dir", "versions/v17.3.4" in out, out)
    check("tag uninstall drops the current symlink too", "symlink" in out, out)
    raises(
        "uninstalling an absent tag is an error",
        lambda: omp._build_uninstall_plan(layout, "v9.9.9"),
    )

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    layout = omp.Layout(base / "root", base / "bin")
    layout.bin_dir.mkdir(parents=True)
    layout.launcher.write_text("real upstream binary\n", encoding="utf-8")
    plan = omp._build_uninstall_plan(layout, None)
    _rc, out = capture(plan.show)
    check("uninstall leaves a foreign launcher alone", "LEAVE" in out, out)
    capture(plan.run)
    check(
        "and running the plan really does leave it",
        layout.launcher.read_text(encoding="utf-8") == "real upstream binary\n",
    )


# --------------------------------------------------------------------------
print("\n== status is read-only ==")

with tempfile.TemporaryDirectory(dir=writable_tmp()) as td:
    base = Path(td)
    layout = make_tree(base)
    config = omp.resolve_config(
        {}, {"install_dir": layout.root, "bin_dir": layout.bin_dir, "os_name": "linux", "arch": "x64", "libc": "gnu"}
    )

    class _Args:
        check_latest = False

    rc, out = capture(omp.cmd_status, config, _Args())
    check("status exits 0", rc == 0)
    check("status reports the installed tag", "v17.3.4" in out, out)
    check("status reports the digest", omp.sha256_file(layout.binary("v17.3.4")) in out, out)
    check("status reports verified", "verified   : yes" in out, out)
    check("status reports launcher ownership", "launcher     : ours" in out, out)
    check(
        "status wrote nothing new",
        sorted(p.name for p in layout.version_dir("v17.3.4").iterdir())
        == ["metadata.json", "omp"],
    )


# --------------------------------------------------------------------------
print("\n== CLI wiring ==")

parser = omp.build_parser()

args = parser.parse_args(["status"])
check("status subcommand dispatches to cmd_status", args.func is omp.cmd_status)
check("status defaults check_latest off", args.check_latest is False)

args = parser.parse_args(["--install-dir", "/opt/x", "--bin-dir", "/opt/bin", "install"])
check("--install-dir operand", args.install_dir == Path("/opt/x"), str(args.install_dir))
check("--bin-dir operand", args.bin_dir == Path("/opt/bin"), str(args.bin_dir))
check("install dispatches to cmd_install", args.func is omp.cmd_install)
check("install is a dry run by default", args.apply is False)
check("install does not force by default", args.force is False)
check("install does not allow unverified by default", args.allow_unverified is False)
check("install version defaults to None (so config wins)", args.version is None)

args = parser.parse_args(["--libc", "musl", "--arch", "arm64", "--os", "linux", "install", "--version", "v1.2.3", "--apply"])
check("--libc operand", args.libc == "musl", str(args.libc))
check("--arch operand", args.arch == "arm64", str(args.arch))
check("--os operand", args.os_name == "linux", str(args.os_name))
check("--version operand", args.version == "v1.2.3", str(args.version))
check("--apply flips apply", args.apply is True)

args = parser.parse_args(["-c", "/etc/omp.toml", "update"])
check("-c operand", args.config == Path("/etc/omp.toml"), str(args.config))
check("update dispatches to cmd_update", args.func is omp.cmd_update)

args = parser.parse_args(["uninstall", "--version", "v17.3.4"])
check("uninstall dispatches to cmd_uninstall", args.func is omp.cmd_uninstall)
check("uninstall --version operand", args.version == "v17.3.4", str(args.version))
check("uninstall is a dry run by default", args.apply is False)

_overrides = omp.cli_overrides_from(parser.parse_args(["install"]))
check(
    "unset CLI flags come through as None so TOML can win",
    all(_overrides[k] is None for k in ("install_dir", "bin_dir", "os_name", "arch", "libc", "version")),
    str(_overrides),
)
check(
    "allow_unverified is omitted unless the flag is passed",
    "allow_unverified" not in _overrides,
    str(_overrides),
)
_overrides = omp.cli_overrides_from(parser.parse_args(["install", "--allow-unverified"]))
check("allow_unverified appears when passed", _overrides["allow_unverified"] is True)

with contextlib.suppress(SystemExit):
    parser.parse_args(["--os", "windows", "status"])
    check("--os windows is rejected by argparse", False, "parse succeeded")


# --------------------------------------------------------------------------
print("\n== resolve_release ==")

# Drive the real resolve_release with fetch_text stubbed, so the escape hatch is
# exercised end to end rather than only through the layers either side of it.
# _urlopen stays poisoned throughout: nothing here touches the network.
_REAL_FETCH_TEXT = omp.fetch_text


def _with_fetch(text_or_exc):
    def stub(url):
        if isinstance(text_or_exc, Exception):
            raise text_or_exc
        return text_or_exc

    return stub


# os/arch/libc are pinned explicitly so the assertions hold on any host.
_PIN = {"version": "v17.3.4", "os_name": "linux", "arch": "x64", "libc": "gnu"}
_pinned = omp.resolve_config({}, _PIN)
_target = omp.resolve_target(_pinned)
check("the pinned target is the linux x64 asset", _target.asset == "omp-linux-x64", _target.asset)

try:
    omp.fetch_text = _with_fetch(REAL_SUMS)
    _res = omp.resolve_release(_pinned, _target)
    check("resolve_release returns the published digest", _res.expected_sha == LINUX_X64_SHA, _res.expected_sha)
    check("resolve_release marks a checked release verified", _res.verified is True, _res.verified)
    check("resolve_release keeps the pinned tag", _res.tag == "v17.3.4", _res.tag)

    # An asset name absent from the release's own sums file is fatal even when
    # the sums file itself fetched fine.
    _bsd = omp.Target("linux", "riscv", "gnu", "omp-linux-riscv")
    raises("an asset missing from SHA256SUMS is fatal", lambda: omp.resolve_release(_pinned, _bsd))

    # Missing checksum material: fatal by default, degraded by the escape hatch.
    omp.fetch_text = _with_fetch(omp.OhMyPiError("HTTP 404 for SHA256SUMS.txt"))
    raises(
        "a missing sums file is fatal without --allow-unverified",
        lambda: omp.resolve_release(_pinned, _target),
    )

    _lax = omp.resolve_config({}, {**_PIN, "allow_unverified": True})
    _rc, _out = capture(lambda: omp.resolve_release(_lax, _target))
    check("--allow-unverified degrades a missing sums file", _rc.expected_sha is None, repr(_rc.expected_sha))
    check("--allow-unverified marks the resolution unverified", _rc.verified is False, _rc.verified)
    check("--allow-unverified says so on stdout", "proceeding unverified" in _out, _out.strip())
    check("--allow-unverified still resolves the tag", _rc.tag == "v17.3.4", _rc.tag)
finally:
    omp.fetch_text = _REAL_FETCH_TEXT


# --------------------------------------------------------------------------
print("\n== network guards ==")

# The scheme guard lives inside the real _urlopen, which the poison above
# replaces -- so exercise the saved original. It refuses before opening
# anything, so this still performs no network I/O.
raises("non-https URLs are refused", lambda: _REAL_URLOPEN("http://example.com/x").__enter__())
raises("file:// URLs are refused", lambda: _REAL_URLOPEN("file:///etc/passwd").__enter__())
raises("ftp:// URLs are refused", lambda: _REAL_URLOPEN("ftp://example.com/x").__enter__())
check(
    "the API endpoint is the releases/latest endpoint for the right repo",
    omp.API_LATEST == "https://api.github.com/repos/can1357/oh-my-pi/releases/latest",
    omp.API_LATEST,
)
check("the sums filename carries its extension", omp.SUMS_NAME == "SHA256SUMS.txt", omp.SUMS_NAME)
check("the launcher name is omp", omp.BINARY_NAME == "omp", omp.BINARY_NAME)


# --------------------------------------------------------------------------
print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
