#!/usr/bin/env python3
"""Tests for scripts/setup/oh-my-openagent.py

Hermetic and offline: every test synthesizes its own root under a temp dir, and
no test performs network I/O or executes a downloaded artifact. The npm install
path is exercised with a fake `npm` on PATH; the signature path is exercised
with a real EC keypair generated in-test by openssl, so the crypto is actually
run rather than mocked away.

Run directly: python3 tests/test_oh_my_openagent.py
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "scripts" / "setup" / "oh-my-openagent.py"

# importlib rather than sys.path.insert -- the latter is banned repo-wide and
# has crashed Claude Code sessions.
_spec = importlib.util.spec_from_file_location("oh_my_openagent_installer", TARGET)
omoa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(omoa)

PASS = 0
FAIL = 0
UTC = dt.timezone.utc


def check(desc: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {desc}")
    else:
        FAIL += 1
        print(f"  FAIL {desc}" + (f"\n         {detail}" if detail else ""))


def raises(desc: str, fn) -> None:
    try:
        fn()
    except omoa.OhMyOpenagentError:
        check(desc, True)
    except Exception as exc:  # noqa: BLE001 - a wrong exception type is a real failure
        check(desc, False, f"raised {type(exc).__name__}: {exc}")
    else:
        check(desc, False, "no exception raised")


def writable_tmp() -> str:
    """$TMPDIR is not reliably writable -- sandboxes pin it to a read-only path."""
    for candidate in (os.environ.get("TMPDIR"), "/tmp", str(HERE.parent / "tmp")):
        if not candidate:
            continue
        try:
            Path(candidate).mkdir(parents=True, exist_ok=True)
            probe = Path(candidate) / ".omoa-write-probe"
            probe.write_text("x")
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise RuntimeError("no writable temp directory found")


TMP = writable_tmp()


def section(name: str) -> None:
    print(f"\n{name}")


# ---------------------------------------------------------------- platform
section("platform normalization")
check("linux/x86_64", omoa.normalize_platform("Linux", "x86_64") == ("linux", "x64"))
check("darwin/arm64", omoa.normalize_platform("Darwin", "arm64") == ("darwin", "arm64"))
check("aarch64 aliases arm64", omoa.normalize_platform("Linux", "aarch64") == ("linux", "arm64"))
check("case and whitespace tolerated", omoa.normalize_platform(" linux ", " AMD64 ") == ("linux", "x64"))
raises("windows refused", lambda: omoa.normalize_platform("Windows", "x86_64"))
raises("i686 refused", lambda: omoa.normalize_platform("Linux", "i686"))

# ---------------------------------------------------------------- version specs
section("version spec validation")
check("exact version accepted", omoa.normalize_version_spec("4.19.4") == "4.19.4")
check("dist-tag accepted", omoa.normalize_version_spec("latest") == "latest")
check("prerelease accepted", omoa.normalize_version_spec("5.0.0-beta.7") == "5.0.0-beta.7")
for bad in ("../etc", "a/b", "1.0.0 2.0.0", "v1#frag", "^4.0.0", "~4.0.0", ">=4", "*", "", "  "):
    raises(f"rejected {bad!r}", lambda b=bad: omoa.normalize_version_spec(b))
check("exact detection: 4.19.4", omoa.is_exact_version("4.19.4") is True)
check("exact detection: 5.0.0-beta.7", omoa.is_exact_version("5.0.0-beta.7") is True)
check("exact detection: latest", omoa.is_exact_version("latest") is False)

# ---------------------------------------------------------------- packument parsing
section("registry document parsing")
PAYLOAD = b"pretend tarball bytes"
INTEGRITY = "sha512-" + base64.b64encode(hashlib.sha512(PAYLOAD).digest()).decode()
PACKUMENT = {
    "dist-tags": {"latest": "4.19.4", "beta": "5.0.0-beta.7"},
    "time": {
        "4.19.4": "2026-08-01T09:47:40.813Z",
        "5.0.0-beta.7": "2026-08-12T00:00:00.000Z",
    },
    "versions": {
        "4.19.4": {
            "name": "oh-my-openagent",
            "version": "4.19.4",
            "license": "SUL-1.0",
            "bin": {"oh-my-openagent": "bin/oh-my-opencode.js", "omo": "bin/oh-my-opencode.js"},
            "dist": {
                "tarball": "https://registry.npmjs.org/oh-my-openagent/-/oh-my-openagent-4.19.4.tgz",
                "integrity": INTEGRITY,
                "shasum": "deadbeef",
                "signatures": [{"keyid": "SHA256:test", "sig": base64.b64encode(b"sig").decode()}],
            },
        },
        "5.0.0-beta.7": {
            "name": "oh-my-openagent",
            "version": "5.0.0-beta.7",
            "bin": {"oh-my-openagent": "bin/oh-my-opencode.js"},
            "dist": {
                "tarball": "https://registry.npmjs.org/oh-my-openagent/-/oh-my-openagent-5.0.0-beta.7.tgz",
                "integrity": INTEGRITY,
            },
        },
    },
}

check("dist-tag resolves", omoa.resolve_version(PACKUMENT, "latest") == "4.19.4")
check("beta tag resolves", omoa.resolve_version(PACKUMENT, "beta") == "5.0.0-beta.7")
check("exact version passes through", omoa.resolve_version(PACKUMENT, "4.19.4") == "4.19.4")
raises("unpublished exact version refused", lambda: omoa.resolve_version(PACKUMENT, "9.9.9"))
raises("unknown dist-tag refused", lambda: omoa.resolve_version(PACKUMENT, "nightly"))

INFO = omoa.extract_version_info(PACKUMENT, "4.19.4")
check("name from manifest", INFO.name == "oh-my-openagent")
check("license captured", INFO.license_id == "SUL-1.0")
check("integrity captured", INFO.integrity == INTEGRITY)
check("bin map captured", "oh-my-openagent" in INFO.bin_map)
check("published parsed to UTC", INFO.published == dt.datetime(2026, 8, 1, 9, 47, 40, 813000, tzinfo=UTC))

raises(
    "manifest without dist.integrity refused",
    lambda: omoa.extract_version_info(
        {"versions": {"1.0.0": {"dist": {"tarball": "https://registry.npmjs.org/x/-/x-1.0.0.tgz"}}}}, "1.0.0"
    ),
)
raises(
    "off-registry tarball URL refused",
    lambda: omoa.extract_version_info(
        {"versions": {"1.0.0": {"dist": {"tarball": "https://evil.example/x.tgz", "integrity": INTEGRITY}}}},
        "1.0.0",
    ),
)
raises("missing manifest refused", lambda: omoa.extract_version_info(PACKUMENT, "0.0.1"))

# ---------------------------------------------------------------- integrity
section("integrity verification")
omoa.verify_integrity(PAYLOAD, INTEGRITY)
check("matching payload accepted", True)
raises("flipped byte refused", lambda: omoa.verify_integrity(PAYLOAD + b"!", INTEGRITY))
raises("truncated payload refused", lambda: omoa.verify_integrity(PAYLOAD[:-1], INTEGRITY))
raises("malformed SRI refused", lambda: omoa.verify_integrity(PAYLOAD, "notanintegrity"))
raises("unsupported algorithm refused", lambda: omoa.verify_integrity(PAYLOAD, "md5-abc"))
raises("non-base64 digest refused", lambda: omoa.verify_integrity(PAYLOAD, "sha512-!!!!"))
check(
    "sha256 SRI also supported",
    omoa.integrity_digest("sha256-" + base64.b64encode(hashlib.sha256(PAYLOAD).digest()).decode())[0] == "sha256",
)

# ---------------------------------------------------------------- release age
section("min-release-age quarantine")
NOW = dt.datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
check("age computed in days", round(omoa.release_age_days(dt.datetime(2026, 8, 8, 12, 0, tzinfo=UTC), NOW), 3) == 7.0)
omoa.check_release_age(INFO, 7, NOW)
check("14-day-old release passes the 7-day gate", True)
BETA = omoa.extract_version_info(PACKUMENT, "5.0.0-beta.7")
raises("3-day-old release blocked by the 7-day gate", lambda: omoa.check_release_age(BETA, 7, NOW))
omoa.check_release_age(BETA, 0, NOW)
check("gate disabled with 0", True)


class _NoTime:
    published = None
    name = "x"
    version = "1.0.0"


raises("missing publish time blocks the gate", lambda: omoa.check_release_age(_NoTime(), 7, NOW))

# ---------------------------------------------------------------- npm argv
section("npm invocation")
ARGV = omoa.npm_install_argv(Path("/root/versions/.staging-4.19.4"), Path("/work/package.tgz"))
check(
    "full ordered argv is exactly as expected",
    ARGV
    == [
        "npm",
        "install",
        "-g",
        "--prefix",
        "/root/versions/.staging-4.19.4",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--no-progress",
        "/work/package.tgz",
    ],
    f"got {ARGV}",
)
check("--prefix operand is the staging dir, not a bare flag", ARGV[ARGV.index("--prefix") + 1] == "/root/versions/.staging-4.19.4")
check("the install target is a local file, never a registry spec", ARGV[-1] == "/work/package.tgz")
check("global flag present so bins land in <prefix>/bin", "-g" in ARGV)

# ---------------------------------------------------------------- launcher
section("launcher")
ROOT = Path("/home/example/.local/share/oh-my-openagent")
BODY = omoa.launcher_body(ROOT)
check("starts with a POSIX sh shebang", BODY.startswith("#!/bin/sh\n"))
check("execs through the current symlink", 'exec "$OMOA_ROOT/current/bin/oh-my-openagent" "$@"' in BODY)
check("root is quoted", f'OMOA_ROOT="{ROOT}"' in BODY)
check("declares itself managed", "regenerated on" in BODY)
check("does not reference ~/.omo or ~/.codex", ".omo" not in BODY and ".codex" not in BODY)

with tempfile.TemporaryDirectory(dir=TMP) as td:
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        script = Path(td) / "launcher.sh"
        script.write_text(BODY)
        result = subprocess.run([shellcheck, "-s", "sh", str(script)], capture_output=True, text=True)
        check("generated launcher is shellcheck-clean", result.returncode == 0, result.stdout.strip())
    else:
        print("  skip shellcheck not on PATH")

    lp = Path(td) / "oh-my-openagent"
    check("missing launcher is not current", omoa.launcher_is_current(lp, ROOT) is False)
    lp.write_text(BODY)
    check("matching launcher is current", omoa.launcher_is_current(lp, ROOT) is True)
    lp.write_text(BODY + "# tampered\n")
    check("modified launcher is stale", omoa.launcher_is_current(lp, ROOT) is False)
    lp.write_text(omoa.launcher_body(Path("/somewhere/else")))
    check("launcher for a different root is stale", omoa.launcher_is_current(lp, ROOT) is False)
    lp.unlink()
    lp.write_bytes(b"\x9e\xa66\xf5")
    check("undecodable launcher is stale, not a crash", omoa.launcher_is_current(lp, ROOT) is False)

# ---------------------------------------------------------------- path safety
section("removal safety")
with tempfile.TemporaryDirectory(dir=TMP) as td:
    root = Path(td) / "root"
    (root / "versions" / "4.19.4").mkdir(parents=True)
    check("inside root is safe", omoa.is_safe_removal(root, root / "versions" / "4.19.4") is True)
    check("root itself is not safe", omoa.is_safe_removal(root, root) is False)
    check("parent of root is not safe", omoa.is_safe_removal(root, root.parent) is False)
    check("unrelated path is not safe", omoa.is_safe_removal(root, Path(td) / "elsewhere") is False)
    check("$HOME is never safe", omoa.is_safe_removal(Path.home(), Path.home()) is False)
    check("/ is never safe", omoa.is_safe_removal(Path("/"), Path("/etc")) is False)
    check("home as target is never safe", omoa.is_safe_removal(root, Path.home()) is False)

    escape = root / "versions" / "escape"
    escape.symlink_to(Path(td) / "outside")
    (Path(td) / "outside").mkdir()
    check("symlink escaping root is not safe", omoa.is_safe_removal(root, escape) is False)

# ---------------------------------------------------------------- tar members
section("archive member safety")
for good in ("package/package.json", "package/bin/oh-my-opencode.js", "package/a/b/c.txt"):
    check(f"accepted {good}", omoa.member_is_safe(good) is True)
for bad in ("/etc/passwd", "package/../../etc/passwd", "../package/x", "other/package.json", "", "package\\x"):
    check(f"rejected {bad!r}", omoa.member_is_safe(bad) is False)


def _make_tarball(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


with tempfile.TemporaryDirectory(dir=TMP) as td:
    good = Path(td) / "good.tgz"
    _make_tarball(good, {"package/package.json": json.dumps({"name": "oh-my-openagent", "version": "4.19.4"}).encode()})
    check("manifest name and version read from the tarball", omoa.read_manifest_name(good) == ("oh-my-openagent", "4.19.4"))

    evil = Path(td) / "evil.tgz"
    _make_tarball(evil, {"../../etc/passwd": b"x"})
    raises("traversal member refused", lambda: omoa.read_manifest_name(evil))

    nomanifest = Path(td) / "nomanifest.tgz"
    _make_tarball(nomanifest, {"package/readme.md": b"hi"})
    raises("tarball without package.json refused", lambda: omoa.read_manifest_name(nomanifest))

    badjson = Path(td) / "badjson.tgz"
    _make_tarball(badjson, {"package/package.json": b"{not json"})
    raises("invalid package.json refused", lambda: omoa.read_manifest_name(badjson))

# ---------------------------------------------------------------- signature
section("npm registry signature verification")
check(
    "signed payload has the exact registry format",
    omoa.signature_payload("oh-my-openagent", "4.19.4", "sha512-abc") == "oh-my-openagent@4.19.4:sha512-abc",
)

KEYS = {
    "keys": [
        {"keyid": "SHA256:live", "keytype": "ecdsa-sha2-nistp256", "key": "AAAA", "expires": None},
        {"keyid": "SHA256:dead", "keytype": "ecdsa-sha2-nistp256", "key": "BBBB", "expires": "2020-01-01T00:00:00.000Z"},
    ]
}
check("live key selected", (omoa.select_key(KEYS, "SHA256:live") or {}).get("key") == "AAAA")
check("expired key rejected", omoa.select_key(KEYS, "SHA256:dead") is None)
check("unknown keyid returns None", omoa.select_key(KEYS, "SHA256:nope") is None)

PEM = omoa.spki_to_pem("AAAABBBB" * 20)
check("PEM header and footer", PEM.startswith("-----BEGIN PUBLIC KEY-----\n") and PEM.rstrip().endswith("-----END PUBLIC KEY-----"))
check("PEM wrapped at 64 columns", all(len(line) <= 64 for line in PEM.splitlines()[1:-1]))

openssl = shutil.which("openssl")
if openssl:
    with tempfile.TemporaryDirectory(dir=TMP) as td:
        work = Path(td)
        priv, pub = work / "priv.pem", work / "pub.pem"
        subprocess.run([openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(priv)], check=True, capture_output=True)
        subprocess.run([openssl, "ec", "-in", str(priv), "-pubout", "-out", str(pub)], check=True, capture_output=True)

        # Strip the PEM back to base64 SPKI, exactly the shape the registry publishes.
        spki_b64 = "".join(l for l in pub.read_text().splitlines() if not l.startswith("-----"))

        name, version, integrity = "oh-my-openagent", "4.19.4", INTEGRITY
        payload = omoa.signature_payload(name, version, integrity)
        msg = work / "msg.txt"
        msg.write_text(payload)
        sigfile = work / "sig.der"
        subprocess.run([openssl, "dgst", "-sha256", "-sign", str(priv), "-out", str(sigfile), str(msg)], check=True, capture_output=True)
        real_sig = base64.b64encode(sigfile.read_bytes()).decode()

        keys_doc = {"keys": [{"keyid": "SHA256:test", "keytype": "ecdsa-sha2-nistp256", "key": spki_b64, "expires": None}]}

        def make_info(sig: str, keyid: str = "SHA256:test"):
            doc = json.loads(json.dumps(PACKUMENT))
            doc["versions"]["4.19.4"]["dist"]["signatures"] = [{"keyid": keyid, "sig": sig}]
            return omoa.extract_version_info(doc, "4.19.4")

        state, detail = omoa.verify_signature(make_info(real_sig), keys_doc, work)
        check("a real signature over the real payload verifies", state == "verified", f"{state}: {detail}")

        # Flip one byte of the DER signature: must fail, not merely warn.
        raw = bytearray(sigfile.read_bytes())
        raw[-1] ^= 0xFF
        state, detail = omoa.verify_signature(make_info(base64.b64encode(bytes(raw)).decode()), keys_doc, work)
        check("a tampered signature FAILS", state == "failed", f"{state}: {detail}")

        # Signature over a different payload (wrong version) must also fail.
        other = work / "other.txt"
        other.write_text(omoa.signature_payload(name, "9.9.9", integrity))
        othersig = work / "other.der"
        subprocess.run([openssl, "dgst", "-sha256", "-sign", str(priv), "-out", str(othersig), str(other)], check=True, capture_output=True)
        state, _ = omoa.verify_signature(make_info(base64.b64encode(othersig.read_bytes()).decode()), keys_doc, work)
        check("a signature bound to another version FAILS", state == "failed")

        state, detail = omoa.verify_signature(make_info(real_sig, keyid="SHA256:unknown"), keys_doc, work)
        check("an unpublished keyid is unavailable, not verified", state == "unavailable", detail)

        state, _ = omoa.verify_signature(make_info("!!!not base64!!!"), keys_doc, work)
        check("a non-base64 signature FAILS", state == "failed")

        nosig = omoa.extract_version_info(
            {
                "versions": {
                    "1.0.0": {
                        "name": "x",
                        "version": "1.0.0",
                        "dist": {"tarball": "https://registry.npmjs.org/x/-/x-1.0.0.tgz", "integrity": INTEGRITY},
                    }
                }
            },
            "1.0.0",
        )
        state, _ = omoa.verify_signature(nosig, keys_doc, work)
        check("no published signature is unavailable, not verified", state == "unavailable")
else:
    print("  skip openssl not on PATH")

# ---------------------------------------------------------------- config
section("config precedence and validation")


def write_cfg(td: Path, text: str) -> Path:
    p = td / "cfg.toml"
    p.write_text(text)
    return p


with tempfile.TemporaryDirectory(dir=TMP) as td:
    tdp = Path(td)
    parser = omoa.build_parser()

    cfg = omoa.build_config(parser.parse_args(["status"]))
    check("defaults: root", cfg.root == omoa.DEFAULT_ROOT)
    check("defaults: version", cfg.version == "latest")
    check("defaults: min_release_age is 7", cfg.min_release_age == 7)
    check("defaults: signature verification on", cfg.verify_signature is True)
    check("defaults: allow_unverified off", cfg.allow_unverified is False)

    c = write_cfg(tdp, 'version = "4.19.4"\nmin_release_age = 3\nroot = "~/omoa"\n')
    cfg = omoa.build_config(parser.parse_args(["-c", str(c), "status"]))
    check("TOML overrides defaults", (cfg.version, cfg.min_release_age) == ("4.19.4", 3))
    check("TOML paths expand ~", cfg.root == Path.home() / "omoa")

    cfg = omoa.build_config(parser.parse_args(["-c", str(c), "--version", "beta", "status"]))
    check("CLI beats TOML", cfg.version == "beta")
    check("unset CLI flags do not clobber TOML", cfg.min_release_age == 3)

    cfg = omoa.build_config(parser.parse_args(["-c", str(c), "--min-release-age", "0", "status"]))
    check("CLI zero is honoured, not treated as unset", cfg.min_release_age == 0)

    nested = write_cfg(tdp, '[oh_my_openagent]\nversion = "4.19.4"\n')
    check("[oh_my_openagent] table accepted", omoa.build_config(parser.parse_args(["-c", str(nested), "status"])).version == "4.19.4")

    empty = write_cfg(tdp, "# nothing here\n")
    check("empty config equals defaults", omoa.build_config(parser.parse_args(["-c", str(empty), "status"])).version == "latest")

    for bad, why in (
        ('token = "abc123"', "secret-shaped key"),
        ('password = "hunter2"', "password key"),
        ('api_secret = "x"', "secret substring"),
        ('unknown_key = "x"', "unknown key"),
        ("min_release_age = -1", "negative age"),
        ('min_release_age = "7"', "age as string"),
        ('verify_signature = "yes"', "bool as string"),
        ('root = ""', "empty path"),
        ("version = 4", "version as int"),
        ("this is not toml", "invalid TOML"),
    ):
        p = write_cfg(tdp, bad + "\n")
        raises(f"rejected: {why}", lambda pp=p: omoa.build_config(parser.parse_args(["-c", str(pp), "status"])))

    raises("missing config file refused", lambda: omoa.build_config(parser.parse_args(["-c", str(tdp / "nope.toml"), "status"])))

# ---------------------------------------------------------------- layout & plan
section("layout and plan")
with tempfile.TemporaryDirectory(dir=TMP) as td:
    root = Path(td) / "root"
    layout = omoa.Layout(root, Path(td) / "bin")
    check("no versions on an empty root", layout.installed_versions() == [])
    check("no current on an empty root", layout.current_version() is None)

    vd = layout.version_dir("4.19.4")
    (vd / "lib" / "node_modules").mkdir(parents=True)
    check("incomplete without the bin shim", omoa.is_complete(vd) is False)
    (vd / "bin").mkdir()
    (vd / "bin" / "oh-my-openagent").write_text("#!/bin/sh\n")
    check("complete once the shim exists", omoa.is_complete(vd) is True)

    other = layout.version_dir("4.18.0")
    (other / "bin").mkdir(parents=True)
    (other / "bin" / "oh-my-openagent").write_text("x")
    check("bin shim alone is not complete (no lib/node_modules)", omoa.is_complete(other) is False)

    check("installed versions listed sorted", layout.installed_versions() == ["4.18.0", "4.19.4"])
    omoa._atomic_symlink(layout.current, Path("versions") / "4.19.4")
    check("current reports the linked version", layout.current_version() == "4.19.4")
    omoa._atomic_symlink(layout.current, Path("versions") / "4.18.0")
    check("symlink swap is idempotent and repoints", layout.current_version() == "4.18.0")

plan = omoa.Plan()
check("empty plan is falsy", bool(plan) is False)
plan.add("announce only")
check("announcement-only plan is still falsy", bool(plan) is False)
done = []
plan.add("do a thing", lambda: done.append(1))
check("plan with an action is truthy", bool(plan) is True)
plan.run()
check("run executes actions once", done == [1])

# ---------------------------------------------------------------- install e2e (fake npm)
section("install end to end with a fake npm")
with tempfile.TemporaryDirectory(dir=TMP) as td:
    tdp = Path(td)
    root = tdp / "root"
    bindir = tdp / "bin"
    root.mkdir()

    fakebin = tdp / "fakebin"
    fakebin.mkdir()
    npm = fakebin / "npm"
    # A fake npm that reproduces the real -g --prefix layout: package under
    # <prefix>/lib/node_modules/<name>, shims in <prefix>/bin.
    npm.write_text(
        "#!/bin/sh\n"
        'prefix=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "--prefix" ]; then prefix="$2"; shift; fi\n'
        "  shift\n"
        "done\n"
        'echo "$@" > "$prefix/../fake-npm-was-called" 2>/dev/null || true\n'
        'mkdir -p "$prefix/lib/node_modules/oh-my-openagent" "$prefix/bin"\n'
        'printf \'#!/bin/sh\\necho fake\\n\' > "$prefix/bin/oh-my-openagent"\n'
        'chmod +x "$prefix/bin/oh-my-openagent"\n'
    )
    npm.chmod(0o755)

    payload_tar = tdp / "payload.tgz"
    _make_tarball(payload_tar, {"package/package.json": json.dumps({"name": "oh-my-openagent", "version": "4.19.4"}).encode()})
    payload_bytes = payload_tar.read_bytes()
    real_integrity = "sha512-" + base64.b64encode(hashlib.sha512(payload_bytes).digest()).decode()

    doc = json.loads(json.dumps(PACKUMENT))
    doc["versions"]["4.19.4"]["dist"]["integrity"] = real_integrity
    info = omoa.extract_version_info(doc, "4.19.4")

    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{fakebin}{os.pathsep}{old_path}"
    orig_download = omoa.download_bytes
    omoa.download_bytes = lambda url: payload_bytes  # noqa: ARG005 - offline by construction
    try:
        cfg = omoa.Config(root=root, bin_dir=bindir, verify_signature=False)
        layout = omoa.Layout(root, bindir)

        # Dry run must write nothing.
        plan = omoa.Plan()
        omoa._install_plan(cfg, layout, "4.19.4", info, plan)
        check("dry-run plan proposes real work", bool(plan) is True)
        check("dry run created no version dir", not layout.version_dir("4.19.4").exists())
        check("dry run created no launcher", not layout.launcher.exists())

        plan.run()
        check("version dir is complete after apply", omoa.is_complete(layout.version_dir("4.19.4")) is True)
        check("current points at the installed version", layout.current_version() == "4.19.4")
        check("launcher written and current", omoa.launcher_is_current(layout.launcher, root) is True)
        check("launcher is executable", os.access(layout.launcher, os.X_OK))
        check("no staging dir left behind", not list(layout.versions.glob(".staging-*")))
        check("no temp dir left behind", not list(root.glob("oh-my-openagent-*")))

        # Idempotence: a second identical run must propose no mutations.
        plan2 = omoa.Plan()
        omoa._install_plan(cfg, layout, "4.19.4", info, plan2)
        check("re-running proposes no changes (idempotent)", bool(plan2) is False)

        # --- update, against a stubbed registry (no network) ---
        orig_get_json_u = omoa._get_json
        try:
            omoa._get_json = lambda url: doc  # noqa: ARG005 - latest is 4.19.4, already installed
            uargs = omoa.build_parser().parse_args(["update"])
            check("update is a no-op when already on the resolved version", omoa.cmd_update(cfg, uargs) == 0)
            check("update installed nothing extra", layout.installed_versions() == ["4.19.4"])

            # A newer latest must be proposed, but a dry run must still write nothing.
            newer = json.loads(json.dumps(doc))
            newer["dist-tags"]["latest"] = "4.20.0"
            newer["time"]["4.20.0"] = "2026-08-01T09:47:40.813Z"
            newer["versions"]["4.20.0"] = json.loads(json.dumps(doc["versions"]["4.19.4"]))
            newer["versions"]["4.20.0"]["version"] = "4.20.0"
            omoa._get_json = lambda url: newer  # noqa: ARG005
            check("update to a newer version returns 0 as a dry run", omoa.cmd_update(cfg, uargs) == 0)
            check("update dry run installed nothing", layout.installed_versions() == ["4.19.4"])
            check("update dry run left current alone", layout.current_version() == "4.19.4")

            empty_layout_cfg = omoa.Config(root=tdp / "no-such-root", bin_dir=bindir)
            raises("update on an empty root refuses", lambda: omoa.cmd_update(empty_layout_cfg, uargs))
        finally:
            omoa._get_json = orig_get_json_u

        # Repair: a removed launcher is the only thing re-proposed.
        layout.launcher.unlink()
        plan3 = omoa.Plan()
        omoa._install_plan(cfg, layout, "4.19.4", info, plan3)
        check("a missing launcher is re-proposed", bool(plan3) is True)
        plan3.run()
        check("launcher repaired", omoa.launcher_is_current(layout.launcher, root) is True)

        # Integrity failure must abort before npm ever runs.
        omoa.download_bytes = lambda url: payload_bytes + b"tampered"  # noqa: ARG005
        shutil.rmtree(layout.version_dir("4.19.4"))
        raises("tampered download aborts the install", lambda: omoa._fetch_and_install(cfg, layout, info))
        check("nothing installed after a failed integrity check", not layout.version_dir("4.19.4").exists())
        check("no staging dir survives the failure", not list(layout.versions.glob(".staging-*")))

        # An npm that produces no shim must not yield a linked launcher.
        omoa.download_bytes = lambda url: payload_bytes  # noqa: ARG005
        npm.write_text('#!/bin/sh\nexit 0\n')
        npm.chmod(0o755)
        raises("install refuses when npm produces no shim", lambda: omoa._fetch_and_install(cfg, layout, info))
        check("no version dir after a shimless install", not layout.version_dir("4.19.4").exists())

        # A quarantine refusal from npm is reported, not swallowed. Each marker is
        # tested on its own, or one matcher can carry a broken sibling.
        for marker, label in (
            ("minimumReleaseAge", "minimumReleaseAge marker"),
            ("min-release-age", "min-release-age marker"),
            ("E403", "E403 marker"),
        ):
            npm.write_text(f'#!/bin/sh\necho "npm error {marker} blah" >&2\nexit 1\n')
            npm.chmod(0o755)
            try:
                omoa._fetch_and_install(cfg, layout, info)
                check(f"npm refusal surfaces via {label}", False, "no exception")
            except omoa.OhMyOpenagentError as exc:
                text = str(exc)
                check(f"npm refusal names the guard via {label}", marker in text, text)
                check(f"npm refusal says it was not bypassed via {label}", "NOT been bypassed" in text, text)

        # An unrelated npm failure must NOT be mislabelled as a quarantine block.
        npm.write_text('#!/bin/sh\necho "npm error ENOSPC no space left" >&2\nexit 1\n')
        npm.chmod(0o755)
        try:
            omoa._fetch_and_install(cfg, layout, info)
            check("an unrelated npm failure still raises", False, "no exception")
        except omoa.OhMyOpenagentError as exc:
            text = str(exc)
            check("an unrelated npm failure is not called a quarantine", "NOT been bypassed" not in text, text)
            check("an unrelated npm failure reports npm's own stderr", "ENOSPC" in text, text)

        # --- signature enforcement inside the install path, not just in the helper ---
        npm.write_text(
            "#!/bin/sh\n"
            'prefix=""\n'
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--prefix" ]; then prefix="$2"; shift; fi\n'
            "  shift\n"
            "done\n"
            'mkdir -p "$prefix/lib/node_modules/oh-my-openagent" "$prefix/bin"\n'
            'printf \'#!/bin/sh\\necho fake\\n\' > "$prefix/bin/oh-my-openagent"\n'
            'chmod +x "$prefix/bin/oh-my-openagent"\n'
        )
        npm.chmod(0o755)
        shutil.rmtree(layout.version_dir("4.19.4"), ignore_errors=True)

        orig_get_json = omoa._get_json
        try:
            # No signature published at all -> "unavailable".
            omoa._get_json = lambda url: {"keys": []}  # noqa: ARG005
            unsigned_doc = json.loads(json.dumps(doc))
            unsigned_doc["versions"]["4.19.4"]["dist"].pop("signatures", None)
            unsigned = omoa.extract_version_info(unsigned_doc, "4.19.4")

            strict = omoa.Config(root=root, bin_dir=bindir, verify_signature=True)
            raises(
                "an unverifiable signature blocks the install by default",
                lambda: omoa._fetch_and_install(strict, layout, unsigned),
            )
            check("nothing installed when the signature is unverifiable", not layout.version_dir("4.19.4").exists())

            lenient = omoa.Config(root=root, bin_dir=bindir, verify_signature=True, allow_unverified=True)
            omoa._fetch_and_install(lenient, layout, unsigned)
            check("--allow-unverified permits an unverifiable signature", omoa.is_complete(layout.version_dir("4.19.4")))

            if openssl:
                # A signature that is checked and FAILS must be refused even with
                # --allow-unverified: unverifiable and invalid are not the same thing.
                shutil.rmtree(layout.version_dir("4.19.4"), ignore_errors=True)
                keydir = tdp / "sigkeys"
                keydir.mkdir(exist_ok=True)
                priv2, pub2 = keydir / "p.pem", keydir / "P.pem"
                subprocess.run([openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(priv2)], check=True, capture_output=True)
                subprocess.run([openssl, "ec", "-in", str(priv2), "-pubout", "-out", str(pub2)], check=True, capture_output=True)
                spki2 = "".join(l for l in pub2.read_text().splitlines() if not l.startswith("-----"))
                omoa._get_json = lambda url: {  # noqa: ARG005
                    "keys": [{"keyid": "SHA256:test", "keytype": "ecdsa-sha2-nistp256", "key": spki2, "expires": None}]
                }
                bogus_doc = json.loads(json.dumps(doc))
                bogus_doc["versions"]["4.19.4"]["dist"]["signatures"] = [
                    {"keyid": "SHA256:test", "sig": base64.b64encode(b"0E\x02\x01\x00" + b"\x00" * 60).decode()}
                ]
                bogus = omoa.extract_version_info(bogus_doc, "4.19.4")
                raises(
                    "a FAILING signature blocks the install",
                    lambda: omoa._fetch_and_install(strict, layout, bogus),
                )
                raises(
                    "--allow-unverified does NOT excuse a failing signature",
                    lambda: omoa._fetch_and_install(lenient, layout, bogus),
                )
                check("nothing installed after a failing signature", not layout.version_dir("4.19.4").exists())
        finally:
            omoa._get_json = orig_get_json
    finally:
        omoa.download_bytes = orig_download
        os.environ["PATH"] = old_path

# ---------------------------------------------------------------- uninstall
section("uninstall")
with tempfile.TemporaryDirectory(dir=TMP) as td:
    tdp = Path(td)
    root, bindir = tdp / "root", tdp / "bin"
    layout = omoa.Layout(root, bindir)
    vd = layout.version_dir("4.19.4")
    (vd / "lib" / "node_modules").mkdir(parents=True)
    (vd / "bin").mkdir()
    (vd / "bin" / "oh-my-openagent").write_text("x")
    omoa._atomic_symlink(layout.current, Path("versions") / "4.19.4")
    bindir.mkdir()
    layout.launcher.write_text(omoa.launcher_body(root))

    # A launcher this installer did not write must be left alone.
    foreign = tdp / "bin2"
    foreign.mkdir()
    (foreign / "oh-my-openagent").write_text("#!/bin/sh\n# someone else's\n")

    args = omoa.build_parser().parse_args(["uninstall"])
    cfg = omoa.Config(root=root, bin_dir=bindir)
    rc = omoa.cmd_uninstall(cfg, args)
    check("dry-run uninstall returns 0", rc == 0)
    check("dry run removed nothing", vd.exists() and layout.launcher.exists())

    args.apply = True
    omoa.cmd_uninstall(cfg, args)
    check("version dir removed", not vd.exists())
    check("current symlink removed", not layout.current.is_symlink())
    check("launcher removed", not layout.launcher.exists())

    cfg2 = omoa.Config(root=root, bin_dir=foreign)
    layout2 = omoa.Layout(root, foreign)
    check("a foreign launcher is not ours", omoa.launcher_is_current(layout2.launcher, root) is False)
    args2 = omoa.build_parser().parse_args(["uninstall"])
    args2.apply = True
    omoa.cmd_uninstall(cfg2, args2)
    check("a foreign launcher survives uninstall", (foreign / "oh-my-openagent").exists())

    check("uninstall is idempotent on an empty root", omoa.cmd_uninstall(cfg, args) == 0)

# ---------------------------------------------------------------- CLI surface
section("CLI surface")
parser = omoa.build_parser()
for cmd in ("status", "install", "update", "uninstall"):
    ns = parser.parse_args([cmd])
    check(f"{cmd} parses", ns.command == cmd)
    if cmd == "status":
        check("status has --remote and defaults off", ns.remote is False)
    else:
        check(f"{cmd} defaults to a dry run", ns.apply is False)
check("install --apply parses", parser.parse_args(["install", "--apply"]).apply is True)

# Unset shared options must leave NO attribute behind, or a subparser default
# would silently overwrite a value parsed at the top level.
for attr in ("verify_signature", "allow_unverified", "version", "root", "bin_dir", "min_release_age", "config"):
    check(f"unset --{attr.replace('_', '-')} leaves no attribute", not hasattr(parser.parse_args(["install"]), attr))

check("--no-verify-signature sets False", parser.parse_args(["install", "--no-verify-signature"]).verify_signature is False)
check("--allow-unverified sets True", parser.parse_args(["install", "--allow-unverified"]).allow_unverified is True)

# Shared options must work on either side of the subcommand.
check("option after the subcommand", parser.parse_args(["install", "--version", "4.19.4"]).version == "4.19.4")
check("option before the subcommand", parser.parse_args(["--version", "4.19.4", "install"]).version == "4.19.4")
check("--apply survives a trailing shared option", parser.parse_args(["install", "--apply", "--version", "beta"]).apply is True)
check(
    "a top-level option is not clobbered by the subparser",
    omoa.build_config(parser.parse_args(["--min-release-age", "0", "install"])).min_release_age == 0,
)
check(
    "config flag works after the subcommand too",
    parser.parse_args(["install", "-c", "/x.toml"]).config == Path("/x.toml"),
)
check("package name pinned", omoa.PACKAGE == "oh-my-openagent")
check("launcher name pinned to the documented bin entry", omoa.LAUNCHER_NAME == "oh-my-openagent")
check("registry pinned to npmjs.org", omoa.REGISTRY == "https://registry.npmjs.org")
check("default min release age matches repo policy", omoa.DEFAULT_MIN_RELEASE_AGE_DAYS == 7)
check("packument URL", omoa.packument_url() == "https://registry.npmjs.org/oh-my-openagent")
check(
    "attestations URL",
    omoa.attestations_url("oh-my-openagent", "4.19.4")
    == "https://registry.npmjs.org/-/npm/v1/attestations/oh-my-openagent@4.19.4",
)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
