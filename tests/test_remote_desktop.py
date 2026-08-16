#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for scripts/setup/remote-desktop.py.

Hermetic: no packages are installed, no service is started, no network call is
made, and nothing outside a TemporaryDirectory is written. The planner takes an
injected ``Probe``, so idempotency is exercised against synthetic host state.

Assertion style follows this repo's scars (CLAUDE.md § Learnings): literals on
both sides, ordered-pair assertions for every flag that takes an operand, and
any extracted set pinned non-empty before a subset check runs over it.

    tests/test_remote_desktop.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/setup/remote-desktop.py"

_spec = importlib.util.spec_from_file_location("remote_desktop", SCRIPT)
assert _spec and _spec.loader
rd = importlib.util.module_from_spec(_spec)
sys.modules["remote_desktop"] = rd  # dataclasses resolves annotations via sys.modules
_spec.loader.exec_module(rd)

PASS = 0
FAIL = 0


def check(label: str, cond: bool, expected: object = "", got: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}")
        if expected != "" or got != "":
            print(f"       expected: {expected!r}")
            print(f"       got:      {got!r}")


def eq(label: str, got: object, expected: object) -> None:
    check(label, got == expected, expected, got)


def pair(label: str, argv: list[str], flag: str, value: str) -> None:
    """Assert `flag` is present AND immediately followed by exactly `value`.

    A presence-only `flag in argv` check passes for any operand, including a
    harmful one — that is the exact bug recorded in CLAUDE.md.
    """
    if flag not in argv:
        check(label, False, f"{flag} {value}", argv)
        return
    i = argv.index(flag)
    got = argv[i + 1] if i + 1 < len(argv) else "<missing operand>"
    check(label, got == value, f"{flag} {value}", f"{flag} {got}")


def raises(label: str, fn, needle: str) -> None:
    try:
        fn()
    except rd.ConfigError as exc:
        check(label, needle in str(exc), f"ConfigError containing {needle!r}", str(exc))
    except Exception as exc:  # any other exception type is itself a failure
        check(label, False, f"ConfigError containing {needle!r}", f"{type(exc).__name__}: {exc}")
    else:
        check(label, False, f"ConfigError containing {needle!r}", "no exception")


def errors_matching(cfg: rd.Config, needle: str) -> list[str]:
    return [e for e in rd.validate(cfg) if needle in e]


ME = rd.resolve_config().user  # the invoking user, guaranteed to exist


def base(**kw) -> rd.Config:
    return replace(rd.Config(user=ME), **kw)


# ---------------------------------------------------------------- TOML parsing


def test_toml_parsing(td: Path) -> None:
    print("\n== TOML parsing ==")

    good = td / "good.toml"
    good.write_text(
        '[general]\nbackend = "kasmvnc"\ndisplay = 3\n'
        '[novnc]\nenabled = false\n'
        '[security]\nallow_public_exposure = true\n'
    )
    over = rd.config_from_toml(good)
    eq("backend override", over["backend"], "kasmvnc")
    eq("display override", over["display"], 3)
    eq("novnc_enabled override", over["novnc_enabled"], False)
    eq("allow_public_exposure override", over["allow_public_exposure"], True)
    eq("only the four set keys are returned", sorted(over), ["allow_public_exposure", "backend", "display", "novnc_enabled"])

    bad_key = td / "unknown.toml"
    bad_key.write_text('[general]\nbaknd = "tigervnc"\n')
    raises("unknown key rejected", lambda: rd.config_from_toml(bad_key), "unknown config key 'general.baknd'")

    bad_type = td / "type.toml"
    bad_type.write_text("[general]\ndisplay = \"one\"\n")
    raises("string where int expected rejected", lambda: rd.config_from_toml(bad_type), "must be an integer")

    bool_as_int = td / "boolint.toml"
    bool_as_int.write_text("[vnc]\nport = true\n")
    raises("bool where int expected rejected", lambda: rd.config_from_toml(bool_as_int), "must be an integer")

    int_as_bool = td / "intbool.toml"
    int_as_bool.write_text("[novnc]\nenabled = 1\n")
    raises("int where bool expected rejected", lambda: rd.config_from_toml(int_as_bool), "must be a boolean")

    for leaf in ("password", "secret", "token", "passphrase"):
        f = td / f"secret_{leaf}.toml"
        f.write_text(f'[vnc]\n{leaf} = "hunter2"\n')
        raises(f"inline secret key '{leaf}' rejected", lambda f=f: rd.config_from_toml(f), "looks like an inline secret")

    broken = td / "broken.toml"
    broken.write_text("[general\nbackend = ")
    raises("malformed TOML rejected", lambda: rd.config_from_toml(broken), "invalid TOML")

    raises("missing file rejected", lambda: rd.config_from_toml(td / "nope.toml"), "config file not found")

    # The shipped example must parse and must not trip the secret guard.
    example = REPO / "config/remote-desktop.toml.example"
    parsed = rd.config_from_toml(example)
    check("shipped example parses", isinstance(parsed, dict), "dict", type(parsed).__name__)
    eq("example is default-equivalent", rd.resolve_config(parsed, {}), rd.resolve_config({}, {}))


# ------------------------------------------------------------------ precedence


def test_precedence() -> None:
    print("\n== precedence: CLI > TOML > defaults ==")

    eq("default backend", rd.resolve_config().backend, "tigervnc")
    eq("default display", rd.resolve_config().display, 1)
    eq("default vnc_bind", rd.resolve_config().vnc_bind, "127.0.0.1")
    eq("default novnc_port", rd.resolve_config().novnc_port, 6080)
    eq("default allow_public_exposure", rd.resolve_config().allow_public_exposure, False)

    eq("TOML beats default", rd.resolve_config({"display": 5}, {}).display, 5)
    eq("CLI beats TOML", rd.resolve_config({"display": 5}, {"display": 9}).display, 9)
    eq("CLI beats default", rd.resolve_config({}, {"display": 7}).display, 7)
    eq("unset CLI (None) does not override TOML", rd.resolve_config({"display": 5}, {"display": None}).display, 5)
    eq("unset CLI (None) does not override default", rd.resolve_config({}, {"display": None}).display, 1)
    eq("CLI False overrides TOML True", rd.resolve_config({"novnc_enabled": True}, {"novnc_enabled": False}).novnc_enabled, False)
    eq("CLI 0 overrides TOML 6080", rd.resolve_config({"novnc_port": 6080}, {"novnc_port": 1}).novnc_port, 1)

    eq("empty user resolves to invoking user", rd.resolve_config().user, ME)
    eq("explicit user is kept", rd.resolve_config({"user": "root"}, {}).user, "root")

    # The argparse surface must actually carry every field the mapper reads.
    parser = rd.build_parser()
    ns = parser.parse_args(["plan", "--display", "4", "--no-novnc", "--vnc-bind", "10.0.0.1"])
    over = rd.cli_overrides(ns)
    eq("cli_overrides display", over["display"], 4)
    eq("cli_overrides novnc_enabled", over["novnc_enabled"], False)
    eq("cli_overrides vnc_bind", over["vnc_bind"], "10.0.0.1")
    eq("cli_overrides untouched field is None", over["geometry"], None)
    ns2 = parser.parse_args(["plan"])
    eq("no flags -> every override is None", set(rd.cli_overrides(ns2).values()), {None})

    eq("resolved port from display", base(display=2).resolved_vnc_port, 5902)
    eq("explicit port wins over display", base(display=2, vnc_port=5999).resolved_vnc_port, 5999)


# ------------------------------------------------------------------ validation


def test_validation() -> None:
    print("\n== validation ==")

    eq("default config is valid", rd.validate(base()), [])

    eq("bad backend", len(errors_matching(base(backend="rdp"), "backend: 'rdp'")), 1)
    eq("bad session", len(errors_matching(base(session="kde"), "session: 'kde'")), 1)
    eq("display 100 rejected", len(errors_matching(base(display=100), "outside 0-99")), 1)
    eq("display -1 rejected", len(errors_matching(base(display=-1), "outside 0-99")), 1)
    eq("display 0 accepted", errors_matching(base(display=0), "outside 0-99"), [])
    eq("depth 8 rejected", len(errors_matching(base(depth=8), "depth: 8")), 1)

    for geom in ("1920", "1920x", "axb", "50x50", "20000x1080", "1920x1080x24"):
        eq(f"geometry {geom!r} rejected", len(errors_matching(base(geometry=geom), "geometry:")), 1)
    eq("geometry 1280x720 accepted", errors_matching(base(geometry="1280x720"), "geometry:"), [])

    eq("nonexistent user rejected", len(errors_matching(base(user="nosuchuser_zzz"), "does not exist")), 1)

    eq("port 70000 rejected", len(errors_matching(base(vnc_port=70000), "outside 1-65535")), 1)
    eq("privileged port rejected", len(errors_matching(base(vnc_port=443), "privileged")), 1)
    eq("hostname as bind rejected", len(errors_matching(base(vnc_bind="localhost"), "not a valid IP address")), 1)
    eq("port collision rejected", len(errors_matching(base(vnc_port=6080), "collide on 6080")), 1)
    # Under kasmvnc novnc_enabled is false but novnc.port is still the -websocketPort,
    # so the collision check must live OUTSIDE the `if novnc_enabled` block.
    eq("port collision rejected under kasmvnc too",
       len(errors_matching(base(backend="kasmvnc", novnc_enabled=False, novnc_port=5901), "collide on 5901")), 1)
    eq("out-of-range websocket port rejected under kasmvnc",
       len(errors_matching(base(backend="kasmvnc", novnc_enabled=False, novnc_port=70000), "outside 1-65535")), 1)

    eq("bad env var name rejected", len(errors_matching(base(vnc_password_env="2BAD-NAME"), "environment variable name")), 1)

    # noVNC is a frontend for tigervnc, never a peer of kasmvnc.
    eq("novnc+kasmvnc is an error", len(errors_matching(base(backend="kasmvnc", novnc_enabled=True), "cannot be stacked")), 1)
    eq("kasmvnc without novnc is fine", rd.validate(base(backend="kasmvnc", novnc_enabled=False)), [])

    eq("half-set TLS rejected", len(errors_matching(base(novnc_tls_cert="/a.pem"), "set together")), 1)
    # Both paths are relative, so both must be named — a single error would mean
    # the loop stopped at the first offender.
    eq("relative TLS paths are both rejected",
       errors_matching(base(novnc_tls_cert="a.pem", novnc_tls_key="b.pem"), "absolute path"),
       ["novnc.tls_cert: 'a.pem' must be an absolute path",
        "novnc.tls_key: 'b.pem' must be an absolute path"])
    eq("relative install_dir rejected", len(errors_matching(base(novnc_dir="novnc"), "must be absolute")), 1)


def test_exposure_gate() -> None:
    print("\n== public-exposure refusal ==")

    eq("loopback needs no opt-in", rd.validate_exposure(base()), [])
    eq("::1 counts as loopback", rd.validate_exposure(base(vnc_bind="::1", novnc_bind="::1")), [])

    for addr in ("0.0.0.0", "::", "10.0.0.5", "192.168.1.20"):
        errs = rd.validate_exposure(base(novnc_bind=addr))
        eq(f"{addr} refused without opt-in", len(errs), 1)
        check(f"{addr} refusal names the opt-in", "allow_public_exposure" in errs[0], True, errs[0])
        check(f"{addr} refusal offers the SSH tunnel", "ssh -N -L" in errs[0], True, errs[0])

    # Opt-in alone is not enough — TLS must be real.
    optin = base(novnc_bind="0.0.0.0", allow_public_exposure=True)
    eq("opt-in without TLS still refused", len(errors_matching(optin, "unencrypted")), 1)

    tls = replace(optin, novnc_tls_cert="/etc/ssl/c.pem", novnc_tls_key="/etc/ssl/k.pem")
    eq("opt-in + TLS + loopback RFB accepted", rd.validate_exposure(tls), [])

    both = replace(tls, vnc_bind="0.0.0.0")
    eq("exposing the raw RFB port is refused even with noVNC TLS", len(errors_matching(both, "has no TLS")), 1)

    kasm = base(backend="kasmvnc", novnc_enabled=False, vnc_bind="0.0.0.0", allow_public_exposure=True)
    eq("kasmvnc opt-in + tls + basic auth accepted", rd.validate_exposure(kasm), [])
    eq("kasmvnc tls=false refused", len(errors_matching(replace(kasm, kasm_tls=False), "kasmvnc.tls must be true")), 1)
    eq("kasmvnc auth=none refused", len(errors_matching(replace(kasm, kasm_auth="none"), "must not be 'none'")), 1)

    # A CLI flag must never be able to open this door: there is no such flag.
    flags = {a for action in rd.build_parser()._actions for a in action.option_strings}
    check("parser has options at all", len(flags) > 5, ">5 options", len(flags))
    check("no CLI flag grants public exposure", not any("public" in f or "expose" in f for f in flags), True, sorted(flags))


def test_tailnet_bind_class() -> None:
    print("\n== tailnet bind class ==")

    # Boundaries, not just a representative address: 100.119.9.66 alone passes
    # for /8, /10 and /16 alike, so the prefix length would be untested.
    for addr in ("100.64.0.0", "100.119.9.66", "100.127.255.255"):
        eq(f"{addr} is tailnet", rd.bind_class(addr), "tailnet")
    for addr in ("100.63.255.255", "100.128.0.0", "10.0.0.5", "0.0.0.0"):
        eq(f"{addr} is public", rd.bind_class(addr), "public")
    eq("fd7a:115c:a1e0:: prefix is tailnet", rd.bind_class("fd7a:115c:a1e0::8e32:943"), "tailnet")
    eq("neighbouring ULA prefix is public", rd.bind_class("fd7a:115c:a1e1::1"), "public")
    eq("loopback outranks everything", rd.bind_class("127.0.0.1"), "loopback")
    eq("a hostname is not a bind class", rd.bind_class("example.com"), "public")

    # The whole point: a tailnet bind needs neither the opt-in nor a TLS cert.
    tailnet = base(novnc_bind="100.119.9.66")
    eq("tailnet noVNC accepted with no opt-in and no TLS", rd.validate_exposure(tailnet), [])
    eq("allow_public_exposure is still false here", tailnet.allow_public_exposure, False)
    eq("tailnet RFB bind accepted too", rd.validate_exposure(base(vnc_bind="100.119.9.66")), [])

    # But it must not become a blanket amnesty for the public bind alongside it.
    mixed = base(vnc_bind="0.0.0.0", novnc_bind="100.119.9.66")
    check("a public bind beside a tailnet one is still refused",
          len(rd.validate_exposure(mixed)) >= 1, True, rd.validate_exposure(mixed))
    check("the refusal names only the public bind",
          "100.119.9.66" not in rd.validate_exposure(mixed)[0], True, rd.validate_exposure(mixed)[0])

    notes = rd.exposure_notes(tailnet)
    eq("a tailnet bind emits exactly one advisory", len(notes), 1)
    check("the advisory states the CGNAT caveat", "100.64.0.0/10" in notes[0], True, notes[0])
    eq("loopback emits no advisory", rd.exposure_notes(base()), [])

    # The firewall note keys off the bind class, not allow_public_exposure —
    # neither of the other two branches is correct for a tailnet bind.
    def firewall(cfg: rd.Config) -> str:
        return next(s.label for s in rd.plan_configure(cfg, rd.Probe()) if "firewall" in s.label)

    tn = firewall(tailnet)
    check("tailnet firewall note allows the interface", "ufw allow in on tailscale0" in tn, True, tn)
    check("tailnet firewall note does not open a port", "/tcp" not in tn, True, tn)
    lb = firewall(base())
    check("loopback firewall note still offers the SSH tunnel", "ssh -N -L" in lb, True, lb)

    eq("status renders the tailnet class", rd._bind_label("100.119.9.66"), "tailnet")
    eq("status still shouts about a public bind", rd._bind_label("0.0.0.0"), "EXPOSED")


# ------------------------------------------------------------ rendered output


def test_rendered_artifacts(td: Path) -> None:
    print("\n== rendered artifacts ==")

    x = rd.render_xstartup(base())
    check("xstartup is /bin/sh", x.startswith("#!/bin/sh\n"), "#!/bin/sh", x.splitlines()[0])
    check("xstartup marked managed", rd.MANAGED_HEADER in x, True, False)
    check("gnome session named explicitly (Wayland default would break Xvnc)",
          "exec dbus-launch --exit-with-session gnome-session --session=gnome\n" in x, True, x)
    check("software GL forced for GNOME Shell", "LIBGL_ALWAYS_SOFTWARE=1" in x, True, x)
    check("XDG_SESSION_TYPE is x11", "XDG_SESSION_TYPE=x11" in x, True, x)
    check("XDG_CURRENT_DESKTOP=GNOME", "XDG_CURRENT_DESKTOP=GNOME" in x, True, x)

    xorg = rd.render_xstartup(base(session="gnome-xorg"))
    check("gnome-xorg session mode", "gnome-session --session=gnome-xorg\n" in xorg, True, xorg)

    xf = rd.render_xstartup(base(session="xfce"))
    check("xfce launches startxfce4", "exec dbus-launch --exit-with-session startxfce4\n" in xf, True, xf)
    check("xfce sets XDG_CURRENT_DESKTOP=XFCE", "XDG_CURRENT_DESKTOP=XFCE" in xf, True, xf)
    check("xfce does not launch gnome-session", "gnome-session" not in xf, True, xf)

    if shutil_which("shellcheck"):
        for name, content in (("gnome", x), ("xorg", xorg), ("xfce", xf)):
            p = td / f"xstartup_{name}.sh"
            p.write_text(content)
            proc = subprocess.run(["shellcheck", "-s", "sh", str(p)], capture_output=True, text=True, check=False)
            check(f"shellcheck clean: xstartup ({name})", proc.returncode == 0, "no findings", proc.stdout.strip())
    else:
        print("  skip shellcheck (not installed)")

    cfgfile = rd.render_vnc_config(base())
    check("vnc config geometry", "geometry=1920x1080" in cfgfile, True, cfgfile)
    check("vnc config localhost=yes on loopback", "localhost=yes" in cfgfile, True, cfgfile)
    check("vnc config localhost=no when bound wide",
          "localhost=no" in rd.render_vnc_config(base(vnc_bind="0.0.0.0")), True, False)
    check("vnc config sets a security type", "SecurityTypes=VncAuth" in cfgfile, True, cfgfile)


def test_generated_commands() -> None:
    print("\n== generated commands ==")

    argv = rd.vnc_server_argv(base())
    eq("vncserver binary", argv[0], "/usr/bin/vncserver")
    check("foreground so systemd Type=simple is honest", "-fg" in argv, True, argv)
    check("display positional present", ":1" in argv, True, argv)
    pair("vncserver -geometry", argv, "-geometry", "1920x1080")
    pair("vncserver -depth", argv, "-depth", "24")
    pair("vncserver -rfbport", argv, "-rfbport", "5901")
    pair("vncserver -localhost yes on loopback", argv, "-localhost", "yes")
    check("no -interface on loopback", "-interface" not in argv, True, argv)

    a2 = rd.vnc_server_argv(base(display=7, geometry="1280x720", depth=16))
    check("display :7", ":7" in a2, True, a2)
    pair("port follows display", a2, "-rfbport", "5907")
    pair("geometry override", a2, "-geometry", "1280x720")
    pair("depth override", a2, "-depth", "16")

    wide = rd.vnc_server_argv(base(vnc_bind="0.0.0.0"))
    pair("-localhost no when wide", wide, "-localhost", "no")
    check("0.0.0.0 gets no -interface", "-interface" not in wide, True, wide)

    iface = rd.vnc_server_argv(base(vnc_bind="10.0.0.5"))
    pair("-localhost no on specific iface", iface, "-localhost", "no")
    pair("-interface carries the address", iface, "-interface", "10.0.0.5")

    w = rd.websockify_argv(base())
    eq("websockify binary", w[0], "/usr/bin/websockify")
    eq("websockify web root", w[1], f"--web={base().novnc_path}")
    eq("websockify listen address is the last-but-one arg", w[-2], "127.0.0.1:6080")
    eq("websockify targets the local RFB port", w[-1], "127.0.0.1:5901")
    check("no TLS flags without a cert", not any(a.startswith("--cert") for a in w), True, w)

    wt = rd.websockify_argv(base(novnc_tls_cert="/etc/ssl/c.pem", novnc_tls_key="/etc/ssl/k.pem"))
    check("websockify --cert", "--cert=/etc/ssl/c.pem" in wt, True, wt)
    check("websockify --key", "--key=/etc/ssl/k.pem" in wt, True, wt)
    check("websockify --ssl-only (no plaintext downgrade)", "--ssl-only" in wt, True, wt)

    eq("apt install argv", rd.install_argv("apt", ["a", "b"]),
       ["sudo", "apt-get", "install", "-y", "--no-install-recommends", "a", "b"])
    eq("dnf install argv", rd.install_argv("dnf", ["a"]), ["sudo", "dnf", "install", "-y", "a"])
    eq("pacman install argv", rd.install_argv("pacman", ["a"]),
       ["sudo", "pacman", "-S", "--needed", "--noconfirm", "a"])
    raises("unknown package manager rejected", lambda: rd.install_argv("zypper", ["a"]), "unsupported package manager")


def test_units() -> None:
    print("\n== systemd user units ==")

    u = rd.render_vnc_unit(base())
    check("unit marked managed", u.startswith(rd.MANAGED_HEADER), True, u.splitlines()[0])
    check("Type=simple", "\nType=simple\n" in u, True, u)
    eq("ExecStart is the exact vncserver argv",
       [ln for ln in u.splitlines() if ln.startswith("ExecStart=")],
       ["ExecStart=" + " ".join(rd.vnc_server_argv(base()))])
    check("ExecStop kills the display", "ExecStop=/usr/bin/vncserver -kill :1" in u, True, u)
    check("installed into the user target", "WantedBy=default.target" in u, True, u)
    check("no System target (this is a user unit)", "multi-user.target" not in u, True, u)

    n = rd.render_novnc_unit(base())
    eq("noVNC ExecStart is the exact websockify argv",
       [ln for ln in n.splitlines() if ln.startswith("ExecStart=")],
       ["ExecStart=" + " ".join(rd.websockify_argv(base()))])
    check("noVNC unit is bound to the VNC unit",
          "BindsTo=remote-desktop-vnc@1.service" in n, True, n)
    unit_section, service_section = n.split("[Service]", 1)
    check("noVNC dependency is in [Unit]", "BindsTo=" in unit_section, True, n)
    check("noVNC dependency is not misplaced in [Service]", "BindsTo=" not in service_section, True, n)

    k = rd.render_kasm_unit(base(backend="kasmvnc", novnc_enabled=False))
    check("kasm unit passes -websocketPort", "-websocketPort 6080" in k, True, k)
    eq("unit names are display-scoped",
       [rd.vnc_unit_name(base(display=3)), rd.novnc_unit_name(base(display=3)), rd.kasm_unit_name(base(display=3))],
       ["remote-desktop-vnc@3.service", "remote-desktop-novnc@3.service", "remote-desktop-kasmvnc@3.service"])

    y = rd.render_kasm_yaml(base(backend="kasmvnc", novnc_enabled=False))
    check("kasm yaml require_ssl true by default", "require_ssl: true" in y, True, y)
    check("kasm yaml require_ssl false when disabled",
          "require_ssl: false" in rd.render_kasm_yaml(base(backend="kasmvnc", novnc_enabled=False, kasm_tls=False)),
          True, False)
    check("kasm yaml carries the bind interface", "interface: 127.0.0.1" in y, True, y)
    check("kasm yaml width from geometry", "width: 1920" in y, True, y)
    check("kasm yaml height from geometry", "height: 1080" in y, True, y)
    # An empty pem_certificate is a PATH KasmVNC would try to open; the key must
    # be absent, not blank. Assert on the key name so `pem_certificate: ""` fails.
    check("kasm yaml omits pem keys when no cert configured", "pem_" not in y, True, y)
    y_tls = rd.render_kasm_yaml(base(backend="kasmvnc", novnc_enabled=False,
                                     novnc_tls_cert="/etc/ssl/c.pem", novnc_tls_key="/etc/ssl/k.pem"))
    eq("kasm yaml emits both pem keys when a cert pair is configured",
       [ln.strip() for ln in y_tls.splitlines() if ln.strip().startswith("pem_")],
       ["pem_certificate: /etc/ssl/c.pem", "pem_key: /etc/ssl/k.pem"])


# ------------------------------------------------------------------- pinning


def test_pinning() -> None:
    print("\n== pinned downloads ==")

    eq("noVNC tag", rd.NOVNC_TAG, "v1.7.0")
    eq("noVNC commit", rd.NOVNC_COMMIT, "63107bd06d9e1f6136ff21aeda8cd62cbf0d433e")
    eq("noVNC commit is a full 40-hex sha", len(rd.NOVNC_COMMIT), 40)
    eq("noVNC repo is upstream over https", rd.NOVNC_REPO, "https://github.com/novnc/noVNC.git")
    eq("KasmVNC version", rd.KASMVNC_VERSION, "1.5.0")

    check("asset table is non-empty", len(rd.KASMVNC_ASSETS) >= 10, ">=10", len(rd.KASMVNC_ASSETS))
    unpinned = [t.format(v=rd.KASMVNC_VERSION) for t in rd.KASMVNC_ASSETS.values()
                if t.format(v=rd.KASMVNC_VERSION) not in rd.KASMVNC_SHA256]
    eq("every listed asset has a pinned sha256", unpinned, [])
    bad = [(a, s) for a, s in rd.KASMVNC_SHA256.items() if len(s) != 64 or not all(c in "0123456789abcdef" for c in s)]
    eq("every sha256 is 64 lowercase hex chars", bad, [])

    noble = rd.Probe(distro_id="ubuntu", distro_codename="noble", arch="amd64")
    eq("noble amd64 asset+sha", rd.kasm_asset(noble),
       ("kasmvncserver_noble_1.5.0_amd64.deb",
        "f599fe02e2175b9817b6165f74a5d2bebdc73118dde9181ba3410963bed7ae1e"))
    fedora = rd.Probe(distro_id="fedora", distro_codename="42", arch="x86_64")
    eq("fedora 42 asset+sha", rd.kasm_asset(fedora),
       ("kasmvncserver_fedora_42_1.5.0_x86_64.rpm",
        "69711c5a769ad9c53b556e702b8ea0097a29638beb5b97b305bee33a072a64bd"))

    raises("unknown distro refuses rather than guessing",
           lambda: rd.kasm_asset(rd.Probe(distro_id="gentoo", distro_codename="rolling", arch="amd64")),
           "no pinned release asset")

    url = rd.KASMVNC_RELEASE_URL.format(version=rd.KASMVNC_VERSION, asset="X.deb")
    eq("release URL points at the pinned tag on github.com",
       url, "https://github.com/kasmtech/KasmVNC/releases/download/v1.5.0/X.deb")


# --------------------------------------------------------------- idempotency


def converged_probe(cfg: rd.Config, **kw) -> rd.Probe:
    """A Probe describing a host where `configure` has already fully run."""
    files: dict[str, str] = {}
    home = cfg.home
    units = home / ".config/systemd/user"
    if cfg.backend == "tigervnc":
        files[str(home / ".vnc/xstartup")] = rd.render_xstartup(cfg)
        files[str(home / ".vnc/config")] = rd.render_vnc_config(cfg)
        files[str(units / rd.vnc_unit_name(cfg))] = rd.render_vnc_unit(cfg)
        if cfg.novnc_enabled:
            files[str(units / rd.novnc_unit_name(cfg))] = rd.render_novnc_unit(cfg)
        enabled = {rd.vnc_unit_name(cfg)}
        if cfg.novnc_enabled:
            enabled.add(rd.novnc_unit_name(cfg))
        files[str(home / ".vnc/passwd")] = "<blob>"
    else:
        files[str(home / ".vnc/xstartup")] = rd.render_xstartup(cfg)
        files[str(home / ".vnc/kasmvnc.yaml")] = rd.render_kasm_yaml(cfg)
        files[str(units / rd.kasm_unit_name(cfg))] = rd.render_kasm_unit(cfg)
        enabled = {rd.kasm_unit_name(cfg)}
        files[str(home / ".kasmpasswd")] = "<blob>"
    return rd.Probe(existing_files=files, enabled_units=frozenset(enabled), **kw)


def test_idempotency() -> None:
    print("\n== idempotency ==")

    cfg = base()
    fresh = rd.Probe()
    steps = rd.plan_configure(cfg, fresh)
    actionable = [s for s in steps if s.kind != "note"]
    check("fresh host has work to do", all(not s.satisfied for s in actionable), True,
          [s.label for s in actionable if s.satisfied])

    conv = converged_probe(cfg)
    steps2 = rd.plan_configure(cfg, conv)
    unsatisfied = [s.label for s in steps2 if not s.satisfied and s.kind != "note"]
    eq("converged host is a no-op", unsatisfied, [])

    # The planner is a pure function of (Config, Probe) — it must never stat the
    # host itself. That only holds if probe_system() is asked about the password
    # files, so pin them into the probed set: drop them and every run would
    # re-create a password that already exists.
    probed = [str(p) for p in rd._managed_paths(cfg)]
    for label, path in (("tigervnc passwd", cfg.home / ".vnc/passwd"),
                        ("kasmvnc passwd", cfg.home / ".kasmpasswd")):
        check(f"{label} is probed", str(path) in probed, True, probed)
    kasm = base(backend="kasmvnc", novnc_enabled=False)
    kasm_conv = converged_probe(kasm)
    eq("converged kasmvnc host is a no-op too",
       [s.label for s in rd.plan_configure(kasm, kasm_conv) if not s.satisfied and s.kind != "note"], [])
    no_pw = replace(kasm_conv, existing_files={k: v for k, v in kasm_conv.existing_files.items()
                                               if not k.endswith(".kasmpasswd")})
    check("missing kasmvnc password is planned from the probe alone",
          any(not s.satisfied and s.secret_env for s in rd.plan_configure(kasm, no_pw)), True, False)

    # daemon-reload must fire only when a unit actually changed.
    def reload_step(sts): return next(s for s in sts if s.argv[:3] == ["systemctl", "--user", "daemon-reload"])
    check("no daemon-reload when units are unchanged", reload_step(steps2).satisfied, True, False)
    drifted = dict(conv.existing_files)
    drifted[str(cfg.home / ".config/systemd/user" / rd.vnc_unit_name(cfg))] = "# edited by hand\n"
    conv_drift = replace(conv, existing_files=drifted)
    check("daemon-reload fires when a unit drifted", not reload_step(rd.plan_configure(cfg, conv_drift)).satisfied, True, False)

    # A drifted non-unit file must be rewritten but must NOT trigger daemon-reload.
    drift2 = dict(conv.existing_files)
    drift2[str(cfg.home / ".vnc/xstartup")] = "#!/bin/sh\n# hand edited\n"
    steps3 = rd.plan_configure(cfg, replace(conv, existing_files=drift2))
    xs = next(s for s in steps3 if s.path.endswith(".vnc/xstartup"))
    check("drifted xstartup is rewritten", not xs.satisfied, True, xs.satisfied)
    check("xstartup drift alone does not reload systemd", reload_step(steps3).satisfied, True, False)

    # Changing config changes the rendered unit, so a converged probe for display 1
    # must NOT satisfy a plan for display 2.
    steps4 = rd.plan_configure(base(display=2), conv)
    check("different display is not considered converged",
          any(not s.satisfied for s in steps4 if s.kind == "write"), True, False)

    # Password file: present -> satisfied; --force -> regenerate.
    pw = next(s for s in steps2 if s.secret_env)
    check("existing password file is left alone", pw.satisfied, True, False)
    pw_forced = next(s for s in rd.plan_configure(replace(cfg, force=True), conv) if s.secret_env)
    check("--force regenerates the password file", not pw_forced.satisfied, True, False)
    eq("password step names the env var, not a value", pw.secret_env, "VNC_PASSWORD")
    eq("kasm password step uses the kasm env var",
       next(s for s in rd.plan_configure(base(backend="kasmvnc", novnc_enabled=False),
                                         rd.Probe()) if s.secret_env).secret_env, "KASMVNC_PASSWORD")

    # enable-unit steps
    disabled = replace(conv, enabled_units=frozenset())
    enables = [s for s in rd.plan_configure(cfg, disabled) if s.argv[:3] == ["systemctl", "--user", "enable"]]
    eq("both units are enabled when neither is", [s.argv[3] for s in enables],
       ["remote-desktop-vnc@1.service", "remote-desktop-novnc@1.service"])
    eq("already-enabled units are skipped",
       [s.satisfied for s in steps2 if s.argv[:3] == ["systemctl", "--user", "enable"]], [True, True])


def test_install_plan() -> None:
    print("\n== install plan ==")

    cfg = base()
    fresh = rd.Probe(pkg_manager="apt", distro_id="ubuntu", distro_codename="noble", arch="amd64")
    steps = rd.plan_install(cfg, fresh)
    pkg = steps[0]
    check("package step is pending on a bare host", not pkg.satisfied, True, pkg.satisfied)
    eq("apt packages for tigervnc+novnc+gnome", pkg.argv,
       ["sudo", "apt-get", "install", "-y", "--no-install-recommends",
        "tigervnc-standalone-server", "tigervnc-common", "tigervnc-tools", "dbus-x11",
        "websockify", "gnome-session"])

    all_present = replace(fresh, installed_packages=frozenset(
        {"tigervnc-standalone-server", "tigervnc-common", "tigervnc-tools", "dbus-x11",
        "websockify", "gnome-session"}))
    eq("no package step when everything is installed",
       [s.satisfied for s in rd.plan_install(cfg, all_present) if s.kind == "pkg"], [True])

    no_novnc = rd.plan_install(base(novnc_enabled=False), fresh)
    check("websockify is not installed when noVNC is off", "websockify" not in no_novnc[0].argv, True, no_novnc[0].argv)

    xfce = rd.plan_install(base(session="xfce"), fresh)
    check("xfce4 replaces gnome-session", "xfce4" in xfce[0].argv and "gnome-session" not in xfce[0].argv,
          True, xfce[0].argv)

    dnf_probe = rd.Probe(pkg_manager="dnf", distro_id="fedora", distro_codename="42", arch="x86_64")
    dnf_steps = rd.plan_install(cfg, dnf_probe)
    check("Fedora uses tigervnc-server, not the Debian name",
          "tigervnc-server" in dnf_steps[0].argv and "tigervnc-standalone-server" not in dnf_steps[0].argv,
          True, dnf_steps[0].argv)

    raises("unknown package manager refuses to plan",
           lambda: rd.plan_install(cfg, rd.Probe(pkg_manager="unknown")), "unsupported package manager")

    clone = next(s for s in steps if s.label.startswith("clone noVNC"))
    check("noVNC clone pending when absent", not clone.satisfied, True, clone.satisfied)
    pair("clone pins the tag", clone.argv, "--branch", "v1.7.0")
    check("shallow clone", "--depth" in clone.argv, True, clone.argv)
    checkout = next(s for s in steps if "checkout pinned" in s.label)
    eq("clone is followed by an exact commit checkout", checkout.argv,
       ["git", "-C", str(cfg.novnc_path), "checkout", "--detach", rd.NOVNC_COMMIT])
    at_pin = replace(fresh, novnc_exists=True, novnc_commit=rd.NOVNC_COMMIT,
                     novnc_origin=rd.NOVNC_REPO)
    check("noVNC at the pinned commit is a no-op",
          next(s for s in rd.plan_install(cfg, at_pin) if "noVNC" in s.label).satisfied, True, False)
    wrong = replace(fresh, novnc_exists=True, novnc_commit="0" * 40,
                    novnc_origin=rd.NOVNC_REPO)
    check("noVNC at a different commit is NOT converged",
          not next(s for s in rd.plan_install(cfg, wrong) if "noVNC" in s.label).satisfied, True, False)
    wrong_steps = rd.plan_install(cfg, wrong)
    check("an old managed checkout fetches the pinned tag",
          any(s.argv[:4] == ["git", "-C", str(cfg.novnc_path), "fetch"] for s in wrong_steps),
          True, [s.argv for s in wrong_steps])
    raises("a foreign noVNC directory is never overwritten",
           lambda: rd.plan_install(
               cfg,
               replace(fresh, novnc_exists=True, novnc_origin="https://example.com/other.git"),
           ),
           "refusing to overwrite")

    kasm_steps = rd.plan_install(base(backend="kasmvnc", novnc_enabled=False), fresh)
    dl = next(s for s in kasm_steps if s.kind == "download")
    eq("download step carries url and sha", dl.argv,
       ["https://github.com/kasmtech/KasmVNC/releases/download/v1.5.0/kasmvncserver_noble_1.5.0_amd64.deb",
        "f599fe02e2175b9817b6165f74a5d2bebdc73118dde9181ba3410963bed7ae1e"])
    check("no noVNC clone under the kasmvnc backend",
          not any("noVNC" in s.label for s in kasm_steps), True, [s.label for s in kasm_steps])
    already = replace(fresh, installed_packages=frozenset({"kasmvncserver"}))
    check("installed kasmvncserver is a no-op",
          next(s for s in rd.plan_install(base(backend="kasmvnc", novnc_enabled=False), already)
               if s.kind == "download").satisfied, True, False)


# ----------------------------------------------------------------- no secrets


def test_no_secrets_leak() -> None:
    print("\n== secret handling ==")

    cfg = base()
    rendered = "\n".join([
        rd.render_xstartup(cfg), rd.render_vnc_config(cfg), rd.render_vnc_unit(cfg),
        rd.render_novnc_unit(cfg), rd.render_kasm_yaml(cfg), rd.config_to_toml(cfg),
    ])
    check("rendered artifacts are non-empty", len(rendered) > 500, ">500 chars", len(rendered))
    for needle in ("VNC_PASSWORD=", "password=", "passwd=", "-passwd", "PASSWORD "):
        check(f"no {needle!r} in any rendered artifact", needle not in rendered, True, needle)

    steps = rd.plan_configure(cfg, rd.Probe()) + rd.plan_install(cfg, rd.Probe(pkg_manager="apt"))
    flat = " ".join(a for s in steps for a in s.argv)
    check("argv set is non-empty", len(flat) > 100, ">100 chars", len(flat))
    check("no password ever reaches argv (/proc is world-readable)",
          "VNC_PASSWORD" not in flat and "hunter" not in flat, True, flat[:200])

    pw = next(s for s in steps if s.secret_env)
    eq("vncpasswd reads stdin, not a flag", pw.argv, ["vncpasswd", "-f"])
    check("dry-run line names the env var, not its value",
          "$VNC_PASSWORD (value never printed)" in pw.display, True, pw.display)
    eq("password file mode is 0600", pw.mode, 0o600)

    xstartup_step = next(s for s in steps if s.path.endswith(".vnc/xstartup"))
    eq("xstartup is executable and private", xstartup_step.mode, 0o700)
    unit_step = next(s for s in steps if s.path.endswith(".service"))
    eq("unit files are 0644", unit_step.mode, 0o644)


def test_binary_password_file(td: Path) -> None:
    print("\n== binary password file ==")

    # `vncpasswd -f` emits a DES-obfuscated blob, not text. Real sample: the first byte alone
    # (0x9e) is an invalid UTF-8 start byte, so any read_text() on this path raises.
    blob = bytes.fromhex("9ea636f516387b9b")
    cfg = base()
    (td / ".vnc").mkdir(parents=True, exist_ok=True)
    (td / ".vnc/passwd").write_bytes(blob)

    # Config.home reads the passwd database, so redirect it at the module's `pwd` import
    # rather than at the Config — that keeps the test hermetic on any host.
    class _FakePwd:
        @staticmethod
        def getpwnam(_name):
            return SimpleNamespace(pw_dir=str(td))

    real_pwd = rd.pwd
    rd.pwd = _FakePwd
    try:
        probe = rd.probe_system(cfg)  # must not raise UnicodeDecodeError
        check("probe sees the password file", probe.file_exists(td / ".vnc/passwd"), True,
              probe.file_exists(td / ".vnc/passwd"))
        check("probe never holds the secret bytes",
              all(blob.hex() not in v and "\x9e" not in v for v in probe.existing_files.values()),
              True, list(probe.existing_files))
        steps = rd.plan_configure(cfg, probe)
        check("password step is satisfied once the file exists",
              all(s.satisfied for s in steps if s.secret_env), True,
              [s.display for s in steps if s.secret_env])
    finally:
        rd.pwd = real_pwd


def test_config_roundtrip(td: Path) -> None:
    print("\n== print-config round trip ==")

    cfg = base(backend="kasmvnc", novnc_enabled=False, display=4, geometry="1280x720", kasm_tls=True)
    out = td / "round.toml"
    out.write_text(rd.config_to_toml(cfg))
    reparsed = rd.resolve_config(rd.config_from_toml(out), {})
    eq("emitted config re-parses to the same values", reparsed, replace(cfg, force=False))
    check("emitted config declares every table",
          all(f"[{t}]" in out.read_text() for t in ("general", "vnc", "novnc", "kasmvnc", "security")),
          True, out.read_text())


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def main() -> int:
    print(f"remote-desktop.py test suite  ({SCRIPT})")
    with tempfile.TemporaryDirectory(prefix="rd-test-") as tmp:
        td = Path(tmp)
        test_toml_parsing(td)
        test_precedence()
        test_validation()
        test_exposure_gate()
        test_tailnet_bind_class()
        test_rendered_artifacts(td)
        test_generated_commands()
        test_units()
        test_pinning()
        test_idempotency()
        test_install_plan()
        test_no_secrets_leak()
        test_binary_password_file(td)
        test_config_roundtrip(td)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
