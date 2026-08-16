#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Reusable Linux remote-desktop setup: TigerVNC + GNOME, optional noVNC, or KasmVNC.

Stdlib only (``tomllib`` is stdlib on 3.11+), so this runs with plain ``python3``
as well as ``uv run``. No third-party TOML parser is pulled in.

Backends and frontends are NOT peers:

  backend = "tigervnc"   conventional VNC server (Xvnc) running a GNOME session.
                         This is what "standard GNOME vncserver" resolves to here.
                         Optional frontend: noVNC (browser client) via websockify.
  backend = "kasmvnc"    KasmVNC, which ships its own web client and TLS. Stacking
                         noVNC on it is incoherent and is a validation *error*.

Why TigerVNC rather than gnome-remote-desktop: g-r-d is coupled to the host GNOME
version, wants a logind seat, and its headless mode moves between releases. Xvnc +
`~/.vnc/xstartup` is version-stable, works on a server with no seat, and is what
every distro's `vncserver` package documents.

Safety model
  - Mutating subcommands are DRY RUN by default; nothing happens without --apply.
  - A bind falls into one of three classes: loopback (always fine), tailnet
    (Tailscale's 100.64.0.0/10 or fd7a:115c:a1e0::/48 — WireGuard-encrypted with
    tailnet membership as authentication, so also fine), and public (everything
    else). Public exposure is refused unless the TOML sets
    security.allow_public_exposure AND TLS is on AND auth is not "none".
    A CLI flag alone can never open the door.
  - Passwords are taken as an env var NAME, never a value; never echoed, never
    placed in argv (/proc is world-readable) — piped to vncpasswd on stdin.
  - Downloads are pinned: noVNC by tag AND commit SHA (verified after clone),
    KasmVNC by release asset SHA256 (verified before install). No checksum on
    file means no install.
  - Firewall rules are never modified. Required commands are printed instead.

Pinned upstream (verified against the GitHub API on 2026-08-15):
  noVNC    v1.7.0   commit 63107bd06d9e1f6136ff21aeda8cd62cbf0d433e
  KasmVNC  v1.5.0   per-distro release assets, SHA256 table below

Usage
  scripts/setup/remote-desktop.py plan                       # resolved config + plan
  scripts/setup/remote-desktop.py install                    # dry run
  scripts/setup/remote-desktop.py install --apply            # actually install
  scripts/setup/remote-desktop.py configure --apply
  scripts/setup/remote-desktop.py status
  scripts/setup/remote-desktop.py print-config --format toml
  scripts/setup/remote-desktop.py plan -c config/remote-desktop.toml --display 2
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pwd
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

# tomllib is stdlib from 3.11 onward; ruff sorts it below without a target-version
# setting, which the repo does not have. No third-party TOML parser is used.
import tomllib

# --------------------------------------------------------------------------
# Pinned upstream artifacts.  Do not edit by hand without re-querying upstream:
#   curl -sS https://api.github.com/repos/novnc/noVNC/releases/latest
#   curl -sS https://api.github.com/repos/kasmtech/KasmVNC/releases/latest
# --------------------------------------------------------------------------

NOVNC_REPO = "https://github.com/novnc/noVNC.git"
NOVNC_TAG = "v1.7.0"
NOVNC_COMMIT = "63107bd06d9e1f6136ff21aeda8cd62cbf0d433e"

KASMVNC_VERSION = "1.5.0"
KASMVNC_RELEASE_URL = "https://github.com/kasmtech/KasmVNC/releases/download/v{version}/{asset}"

# (os-release ID, VERSION_CODENAME or VERSION_ID) -> asset name template.
# Only distro/arch pairs whose SHA256 we actually hold are installable.
KASMVNC_ASSETS: dict[str, str] = {
    "ubuntu:noble:amd64": "kasmvncserver_noble_{v}_amd64.deb",
    "ubuntu:noble:arm64": "kasmvncserver_noble_{v}_arm64.deb",
    "ubuntu:jammy:amd64": "kasmvncserver_jammy_{v}_amd64.deb",
    "ubuntu:jammy:arm64": "kasmvncserver_jammy_{v}_arm64.deb",
    "ubuntu:focal:amd64": "kasmvncserver_focal_{v}_amd64.deb",
    "ubuntu:focal:arm64": "kasmvncserver_focal_{v}_arm64.deb",
    "debian:bookworm:amd64": "kasmvncserver_bookworm_{v}_amd64.deb",
    "debian:bookworm:arm64": "kasmvncserver_bookworm_{v}_arm64.deb",
    "debian:bullseye:amd64": "kasmvncserver_bullseye_{v}_amd64.deb",
    "debian:bullseye:arm64": "kasmvncserver_bullseye_{v}_arm64.deb",
    "debian:trixie:amd64": "kasmvncserver_trixie_{v}_amd64.deb",
    "debian:trixie:arm64": "kasmvncserver_trixie_{v}_arm64.deb",
    "fedora:42:x86_64": "kasmvncserver_fedora_42_{v}_x86_64.rpm",
    "fedora:42:aarch64": "kasmvncserver_fedora_42_{v}_aarch64.rpm",
    "fedora:43:x86_64": "kasmvncserver_fedora_43_{v}_x86_64.rpm",
    "fedora:43:aarch64": "kasmvncserver_fedora_43_{v}_aarch64.rpm",
}

# SHA256 as published by the GitHub release API `digest` field (2026-08-15).
KASMVNC_SHA256: dict[str, str] = {
    "kasmvncserver_noble_1.5.0_amd64.deb": "f599fe02e2175b9817b6165f74a5d2bebdc73118dde9181ba3410963bed7ae1e",
    "kasmvncserver_noble_1.5.0_arm64.deb": "c9199cf4753208bfb69fd016a9780242bebfc43370cc38c97d61e90a3c783e04",
    "kasmvncserver_jammy_1.5.0_amd64.deb": "485c190b1d9269fc40fc7205f850ecf71292d737cdb5482e778f2e52f128cd8a",
    "kasmvncserver_jammy_1.5.0_arm64.deb": "9b21779ee16e514bccd2971dcb3983cd8019a94c8614149dbd717d3702c8dbb5",
    "kasmvncserver_focal_1.5.0_amd64.deb": "a2951daaa48aa52d09c04a0c6438ab43fe2e7d420485b1bfc36253772799c83c",
    "kasmvncserver_focal_1.5.0_arm64.deb": "d0188b4e65706f5143fcb000bb29e97d90a3848e7290c9155f7df20585957558",
    "kasmvncserver_bookworm_1.5.0_amd64.deb": "770fd3df51510beecc89666879d82faf411276e68c6e11df612f736b891b5f71",
    "kasmvncserver_bookworm_1.5.0_arm64.deb": "aa83a1a6c9069d1a02239988b07a3a2a082a433042b4d4ee2b9e9f6b2df9643c",
    "kasmvncserver_bullseye_1.5.0_amd64.deb": "4b33cf0a9442a58fd7c4705bbfbfbf0cec1ce7d58a776341af9d8ce684bf6ed2",
    "kasmvncserver_bullseye_1.5.0_arm64.deb": "250bcff22a470157a7d1109881e991c11016a149888a29f3a280ece59a2e4efb",
    "kasmvncserver_trixie_1.5.0_amd64.deb": "80b241de7dfe53bba2b7e1cc5ac8c5246d72271efa16be2d4f76607f30fab1c4",
    "kasmvncserver_trixie_1.5.0_arm64.deb": "fbb11589958a2acccd2d67f67944be79ac1e8e3a1d6172c0e6db6dc59e55a919",
    "kasmvncserver_fedora_42_1.5.0_x86_64.rpm": "69711c5a769ad9c53b556e702b8ea0097a29638beb5b97b305bee33a072a64bd",
    "kasmvncserver_fedora_42_1.5.0_aarch64.rpm": "c4c421179cd88608eebd6d72177744475cff9b2adf34cf111e4fe0a9a63e64d3",
    "kasmvncserver_fedora_43_1.5.0_x86_64.rpm": "a799979ebcfa4c0d17f4480620c1db7a6f5650b6039283527d5241bdc0283b95",
    "kasmvncserver_fedora_43_1.5.0_aarch64.rpm": "d7e834d5da4545085ada644fc30ecbcf88001e87ce036979485ba3701163852a",
}

# Distro package names.  TigerVNC is `tigervnc-standalone-server` on Debian family
# and `tigervnc-server` on Fedora — a single name would be wrong on one of them.
PACKAGES: dict[str, dict[str, list[str]]] = {
    "apt": {
        # tigervnc-tools is what actually provides vncpasswd on Debian/Ubuntu: it ships
        # /usr/bin/tigervncpasswd and registers the `vncpasswd` alternative. tigervnc-common
        # only ships tigervncconfig, so without -tools the configure step dies on ENOENT.
        "tigervnc": ["tigervnc-standalone-server", "tigervnc-common", "tigervnc-tools", "dbus-x11"],
        "novnc": ["websockify"],
        "gnome": ["gnome-session"],
        "xfce": ["xfce4"],
    },
    "dnf": {
        "tigervnc": ["tigervnc-server", "dbus-x11"],
        "novnc": ["python3-websockify"],
        "gnome": ["gnome-session-xsession"],
        "xfce": ["@xfce-desktop-environment"],
    },
    "pacman": {
        "tigervnc": ["tigervnc", "dbus"],
        "novnc": ["python-websockify"],
        "gnome": ["gnome-session"],
        "xfce": ["xfce4"],
    },
}

BACKENDS = ("tigervnc", "kasmvnc")
SESSIONS = ("gnome", "gnome-xorg", "xfce")
KASM_AUTH = ("basic", "none")

MANAGED_HEADER = "# Managed by scripts/setup/remote-desktop.py — edits are overwritten."


class ConfigError(Exception):
    """Configuration is invalid or unsafe. Message is user-facing."""


# --------------------------------------------------------------------------
# Configuration: defaults < TOML < CLI
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    backend: str = "tigervnc"
    user: str = ""  # empty -> resolved to the invoking user
    display: int = 1
    geometry: str = "1920x1080"
    depth: int = 24
    session: str = "gnome"

    vnc_bind: str = "127.0.0.1"
    vnc_port: int = 0  # 0 -> 5900 + display
    vnc_password_env: str = "VNC_PASSWORD"

    novnc_enabled: bool = True
    novnc_bind: str = "127.0.0.1"
    novnc_port: int = 6080
    novnc_dir: str = "~/.local/share/novnc"
    novnc_tls_cert: str = ""
    novnc_tls_key: str = ""

    kasm_tls: bool = True
    kasm_auth: str = "basic"
    kasm_password_env: str = "KASMVNC_PASSWORD"

    allow_public_exposure: bool = False

    # Runtime knob, kept here so the plan is a pure function of Config.
    force: bool = False

    @property
    def resolved_vnc_port(self) -> int:
        return self.vnc_port or (5900 + self.display)

    @property
    def home(self) -> Path:
        return Path(pwd.getpwnam(self.user).pw_dir)

    @property
    def novnc_path(self) -> Path:
        raw = self.novnc_dir
        if raw.startswith("~/"):
            return self.home / raw[2:]
        return Path(raw)


# TOML table.key -> Config field.  Explicit, so an unknown key is an error rather
# than a silently ignored typo.
TOML_MAP: dict[str, str] = {
    "general.backend": "backend",
    "general.user": "user",
    "general.display": "display",
    "general.geometry": "geometry",
    "general.depth": "depth",
    "general.session": "session",
    "vnc.bind": "vnc_bind",
    "vnc.port": "vnc_port",
    "vnc.password_env": "vnc_password_env",
    "novnc.enabled": "novnc_enabled",
    "novnc.bind": "novnc_bind",
    "novnc.port": "novnc_port",
    "novnc.install_dir": "novnc_dir",
    "novnc.tls_cert": "novnc_tls_cert",
    "novnc.tls_key": "novnc_tls_key",
    "kasmvnc.tls": "kasm_tls",
    "kasmvnc.auth": "kasm_auth",
    "kasmvnc.password_env": "kasm_password_env",
    "security.allow_public_exposure": "allow_public_exposure",
}

# Keys that would mean an inline secret.  Refused outright rather than redacted:
# a config file that *can* hold a password will eventually hold one.
SECRET_KEY_NAMES = ("password", "passwd", "secret", "token", "passphrase")


def flatten_toml(data: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten_toml(value, f"{path}."))
        else:
            out[path] = value
    return out


def config_from_toml(path: Path) -> dict[str, object]:
    """Parse a TOML config into Config-field overrides. Raises ConfigError."""
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    flat = flatten_toml(raw)
    overrides: dict[str, object] = {}
    for key, value in flat.items():
        leaf = key.rsplit(".", 1)[-1]
        if leaf in SECRET_KEY_NAMES:
            raise ConfigError(
                f"{path}: key '{key}' looks like an inline secret. "
                "Use *_password_env with the NAME of an environment variable instead."
            )
        if key not in TOML_MAP:
            known = ", ".join(sorted(TOML_MAP))
            raise ConfigError(f"{path}: unknown config key '{key}'. Known keys: {known}")
        field_name = TOML_MAP[key]
        expected = type(getattr(Config(), field_name))
        # bool is a subclass of int; check it first so `port = true` is rejected.
        if expected is bool and not isinstance(value, bool):
            raise ConfigError(f"{path}: '{key}' must be a boolean, got {value!r}")
        if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ConfigError(f"{path}: '{key}' must be an integer, got {value!r}")
        if expected is str and not isinstance(value, str):
            raise ConfigError(f"{path}: '{key}' must be a string, got {value!r}")
        overrides[field_name] = value
    return overrides


def resolve_config(
    toml_overrides: dict[str, object] | None = None,
    cli_overrides: dict[str, object] | None = None,
) -> Config:
    """Layer defaults < TOML < CLI. Only non-None CLI values override."""
    cfg = Config()
    if toml_overrides:
        cfg = replace(cfg, **toml_overrides)  # type: ignore[arg-type]
    if cli_overrides:
        live = {k: v for k, v in cli_overrides.items() if v is not None}
        cfg = replace(cfg, **live)  # type: ignore[arg-type]
    if not cfg.user:
        cfg = replace(cfg, user=os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name)
    return cfg


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


# Tailscale's address space. The IPv4 range is RFC 6598 CGNAT, which Tailscale
# claims by convention but does not own — a carrier-grade NAT'd machine can hold
# a 100.64/10 address with no tailnet involved, hence the advisory in
# exposure_notes(). The IPv6 ULA prefix is Tailscale-specific and unambiguous.
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _is_tailnet(addr: str) -> bool:
    """True for an address inside Tailscale's ranges. Pure — never probes the daemon.

    Keeping this a range test rather than a `tailscale ip` call is deliberate:
    validate() and validate_exposure() are pure functions asserted directly in
    tests, and a subprocess in either would end that.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip in (_TAILNET_V6 if ip.version == 6 else _TAILNET_V4)


def bind_class(addr: str) -> str:
    """'loopback' | 'tailnet' | 'public' — the three exposure classes."""
    if _is_loopback(addr):
        return "loopback"
    if _is_tailnet(addr):
        return "tailnet"
    return "public"


def _check_port(label: str, port: int, errors: list[str]) -> None:
    if not 1 <= port <= 65535:
        errors.append(f"{label}: port {port} is outside 1-65535")
    elif port < 1024:
        errors.append(f"{label}: port {port} is privileged (<1024); this tool runs unprivileged")


def _check_bind(label: str, addr: str, errors: list[str]) -> None:
    try:
        ipaddress.ip_address(addr)
    except ValueError:
        errors.append(f"{label}: '{addr}' is not a valid IP address (hostnames are not accepted)")


def validate(cfg: Config) -> list[str]:
    """Return a list of human-readable errors. Empty list means the config is usable."""
    errors: list[str] = []

    if cfg.backend not in BACKENDS:
        errors.append(f"backend: '{cfg.backend}' is not one of {', '.join(BACKENDS)}")
    if cfg.session not in SESSIONS:
        errors.append(f"session: '{cfg.session}' is not one of {', '.join(SESSIONS)}")
    if cfg.kasm_auth not in KASM_AUTH:
        errors.append(f"kasmvnc.auth: '{cfg.kasm_auth}' is not one of {', '.join(KASM_AUTH)}")

    if not 0 <= cfg.display <= 99:
        errors.append(f"display: :{cfg.display} is outside 0-99")
    if cfg.depth not in (16, 24, 32):
        errors.append(f"depth: {cfg.depth} is not one of 16, 24, 32")

    parts = cfg.geometry.split("x")
    if len(parts) != 2 or not all(p.isdigit() and 100 <= int(p) <= 10000 for p in parts):
        errors.append(f"geometry: '{cfg.geometry}' must be WIDTHxHEIGHT with each 100-10000")

    try:
        pwd.getpwnam(cfg.user)
    except KeyError:
        errors.append(f"user: '{cfg.user}' does not exist on this system")

    for name, envvar in (("vnc.password_env", cfg.vnc_password_env), ("kasmvnc.password_env", cfg.kasm_password_env)):
        if not envvar.replace("_", "").isalnum() or envvar[:1].isdigit():
            errors.append(f"{name}: '{envvar}' is not a valid environment variable name")

    _check_bind("vnc.bind", cfg.vnc_bind, errors)
    _check_port("vnc", cfg.resolved_vnc_port, errors)

    # noVNC is a FRONTEND for tigervnc, not a peer backend. KasmVNC ships its own.
    if cfg.novnc_enabled and cfg.backend == "kasmvnc":
        errors.append(
            "novnc.enabled: KasmVNC serves its own web client; noVNC cannot be stacked on it. "
            "Set novnc.enabled = false, or use backend = 'tigervnc'."
        )
    # Checked for BOTH backends: under kasmvnc this value is the -websocketPort,
    # so a collision with the RFB port is just as fatal there as it is under noVNC.
    _check_port("novnc", cfg.novnc_port, errors)
    if cfg.novnc_port == cfg.resolved_vnc_port:
        errors.append(f"novnc.port and vnc port collide on {cfg.novnc_port}")

    if cfg.novnc_enabled:
        _check_bind("novnc.bind", cfg.novnc_bind, errors)
        if bool(cfg.novnc_tls_cert) != bool(cfg.novnc_tls_key):
            errors.append("novnc: tls_cert and tls_key must be set together or both left empty")
        for label, p in (("novnc.tls_cert", cfg.novnc_tls_cert), ("novnc.tls_key", cfg.novnc_tls_key)):
            if p and not p.startswith("/"):
                errors.append(f"{label}: '{p}' must be an absolute path")

    if not cfg.novnc_dir.startswith(("/", "~/")):
        errors.append(f"novnc.install_dir: '{cfg.novnc_dir}' must be absolute or start with ~/")

    errors.extend(validate_exposure(cfg))
    return errors


def validate_exposure(cfg: Config) -> list[str]:
    """Refuse anything publicly reachable unless opted in AND encrypted AND authenticated.

    A CLI flag is deliberately not enough: the opt-in lives in the TOML file, which
    is reviewable and version-controlled, and it must be paired with real TLS.

    A tailnet bind is not public: the traffic is WireGuard-encrypted end to end and
    tailnet membership is the authentication, so it needs neither the opt-in nor a
    TLS cert. See exposure_notes() for the caveat this carries.
    """
    errors: list[str] = []
    exposed: list[tuple[str, str]] = []
    if bind_class(cfg.vnc_bind) == "public":
        exposed.append(("vnc.bind", cfg.vnc_bind))
    if cfg.novnc_enabled and bind_class(cfg.novnc_bind) == "public":
        exposed.append(("novnc.bind", cfg.novnc_bind))

    if not exposed:
        return errors

    where = ", ".join(f"{k}={v}" for k, v in exposed)
    if not cfg.allow_public_exposure:
        errors.append(
            f"refusing public exposure ({where}): set security.allow_public_exposure = true "
            "in the TOML config to opt in. The default access path is an SSH tunnel: "
            "ssh -N -L 6080:127.0.0.1:6080 <host>"
        )
        return errors

    if cfg.backend == "kasmvnc":
        if not cfg.kasm_tls:
            errors.append(f"refusing public exposure ({where}): kasmvnc.tls must be true")
        if cfg.kasm_auth == "none":
            errors.append(f"refusing public exposure ({where}): kasmvnc.auth must not be 'none'")
    else:
        if not (cfg.novnc_tls_cert and cfg.novnc_tls_key):
            errors.append(
                f"refusing public exposure ({where}): plain VNC/noVNC is unencrypted. "
                "Provide novnc.tls_cert and novnc.tls_key, or keep the bind on loopback."
            )
        if bind_class(cfg.vnc_bind) == "public":
            errors.append(
                "refusing public exposure (vnc.bind): the RFB port itself has no TLS. "
                "Keep vnc.bind = '127.0.0.1' and expose only the TLS noVNC port."
            )
    return errors


def exposure_notes(cfg: Config) -> list[str]:
    """Advisories that do not block. Empty unless something is bound to a tailnet."""
    notes: list[str] = []
    tailnet = [
        (label, addr)
        for label, addr, enabled in (
            ("vnc.bind", cfg.vnc_bind, True),
            ("novnc.bind", cfg.novnc_bind, cfg.novnc_enabled),
        )
        if enabled and bind_class(addr) == "tailnet"
    ]
    if not tailnet:
        return notes
    where = ", ".join(f"{k}={v}" for k, v in tailnet)
    notes.append(
        f"{where} is treated as a Tailscale address, so TLS and "
        "security.allow_public_exposure are not required — the tailnet is encrypted and "
        "membership is the authentication. Verify with 'tailscale ip -4': a 100.64.0.0/10 "
        "address is only ASSUMED to be a tailnet, and carrier-grade NAT uses the same range."
    )
    return notes


# --------------------------------------------------------------------------
# Rendered artifacts (pure functions — every one of these is asserted in tests)
# --------------------------------------------------------------------------


def render_xstartup(cfg: Config) -> str:
    """~/.vnc/xstartup — shellcheck-clean, must be executable."""
    if cfg.session == "xfce":
        launch = "exec dbus-launch --exit-with-session startxfce4"
    else:
        # GNOME defaults to Wayland; Xvnc is an X server, so the X11 session has to
        # be named explicitly or gnome-session picks a mode that never comes up.
        mode = "gnome-xorg" if cfg.session == "gnome-xorg" else "gnome"
        launch = f"exec dbus-launch --exit-with-session gnome-session --session={mode}"
    return f"""#!/bin/sh
{MANAGED_HEADER}
# TigerVNC runs this as the session leader for display :{cfg.display}.

unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS

# GNOME Shell has no GPU under Xvnc; without llvmpipe it fails to start at all.
LIBGL_ALWAYS_SOFTWARE=1
export LIBGL_ALWAYS_SOFTWARE
XDG_SESSION_TYPE=x11
export XDG_SESSION_TYPE
XDG_CURRENT_DESKTOP={"XFCE" if cfg.session == "xfce" else "GNOME"}
export XDG_CURRENT_DESKTOP

[ -r "$HOME/.Xresources" ] && xrdb "$HOME/.Xresources"

{launch}
"""


def render_vnc_config(cfg: Config) -> str:
    """~/.vnc/config — read by the TigerVNC vncserver wrapper.

    For the systemd path the UNIT IS AUTHORITATIVE: vnc_server_argv() passes
    -geometry/-depth/-rfbport/-localhost explicitly and they win over this file.
    The geometry/depth/localhost keys here exist so that a hand-run `vncserver :N`
    behaves the same as the service; SecurityTypes is the only key with no argv
    equivalent. Keep the two in sync, or drop the duplicated keys.
    """
    lines = [
        MANAGED_HEADER,
        f"geometry={cfg.geometry}",
        f"depth={cfg.depth}",
        f"localhost={'yes' if _is_loopback(cfg.vnc_bind) else 'no'}",
        "SecurityTypes=VncAuth",
    ]
    return "\n".join(lines) + "\n"


def vnc_server_argv(cfg: Config) -> list[str]:
    """Argv for the foreground Xvnc session. No secret ever appears here."""
    argv = [
        "/usr/bin/vncserver",
        "-fg",
        f":{cfg.display}",
        "-geometry",
        cfg.geometry,
        "-depth",
        str(cfg.depth),
        "-rfbport",
        str(cfg.resolved_vnc_port),
    ]
    if _is_loopback(cfg.vnc_bind):
        argv += ["-localhost", "yes"]
    else:
        argv += ["-localhost", "no"]
        if cfg.vnc_bind not in ("0.0.0.0", "::"):
            argv += ["-interface", cfg.vnc_bind]
    return argv


def websockify_argv(cfg: Config) -> list[str]:
    argv = ["/usr/bin/websockify", f"--web={cfg.novnc_path}"]
    if cfg.novnc_tls_cert and cfg.novnc_tls_key:
        argv += [f"--cert={cfg.novnc_tls_cert}", f"--key={cfg.novnc_tls_key}", "--ssl-only"]
    argv += [f"{cfg.novnc_bind}:{cfg.novnc_port}", f"127.0.0.1:{cfg.resolved_vnc_port}"]
    return argv


def _unit(
    description: str,
    argv: list[str],
    *,
    unit_extra: list[str] | None = None,
    service_extra: list[str] | None = None,
) -> str:
    body = [
        "[Unit]",
        f"Description={description}",
        "Documentation=man:Xvnc(1)",
        "After=default.target",
    ]
    body += unit_extra or []
    body += [
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={' '.join(argv)}",
    ]
    body += service_extra or []
    body += [
        "Restart=on-failure",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return f"{MANAGED_HEADER}\n" + "\n".join(body)


def render_vnc_unit(cfg: Config) -> str:
    return _unit(
        f"TigerVNC server on :{cfg.display} ({cfg.session} session)",
        vnc_server_argv(cfg),
        service_extra=[f"ExecStop=/usr/bin/vncserver -kill :{cfg.display}"],
    )


def render_novnc_unit(cfg: Config) -> str:
    return _unit(
        f"noVNC {NOVNC_TAG} web client for :{cfg.display}",
        websockify_argv(cfg),
        unit_extra=[f"BindsTo={vnc_unit_name(cfg)}", f"After={vnc_unit_name(cfg)}"],
    )


def render_kasm_yaml(cfg: Config) -> str:
    """~/.vnc/kasmvnc.yaml — KasmVNC reads YAML; emitted by hand, no yaml dep.

    The pem_* keys are OMITTED rather than emitted empty when no cert is
    configured: an empty string is a path KasmVNC would try to open, whereas an
    absent key leaves it on its self-signed default.
    """
    pem = ""
    if cfg.novnc_tls_cert and cfg.novnc_tls_key:
        pem = (f"\n    pem_certificate: {cfg.novnc_tls_cert}"
               f"\n    pem_key: {cfg.novnc_tls_key}")
    return f"""{MANAGED_HEADER}
network:
  interface: {cfg.vnc_bind}
  websocket_port: {cfg.novnc_port}
  udp:
    public_ip: auto
  ssl:
    require_ssl: {'true' if cfg.kasm_tls else 'false'}{pem}
desktop:
  resolution:
    width: {cfg.geometry.split('x')[0]}
    height: {cfg.geometry.split('x')[1]}
"""


def vnc_unit_name(cfg: Config) -> str:
    return f"remote-desktop-vnc@{cfg.display}.service"


def novnc_unit_name(cfg: Config) -> str:
    return f"remote-desktop-novnc@{cfg.display}.service"


def kasm_unit_name(cfg: Config) -> str:
    return f"remote-desktop-kasmvnc@{cfg.display}.service"


# --------------------------------------------------------------------------
# System probing — injected so the planner is testable without touching the host
# --------------------------------------------------------------------------


@dataclass
class Probe:
    """Everything the planner needs to know about the machine, in one place."""

    pkg_manager: str = "apt"
    distro_id: str = "ubuntu"
    distro_codename: str = "noble"
    arch: str = "amd64"
    installed_packages: frozenset[str] = frozenset()
    existing_files: dict[str, str] = field(default_factory=dict)  # path -> content
    novnc_exists: bool = False
    novnc_commit: str | None = None
    novnc_origin: str | None = None
    enabled_units: frozenset[str] = frozenset()

    def file_matches(self, path: Path, content: str) -> bool:
        return self.existing_files.get(str(path)) == content

    def file_exists(self, path: Path) -> bool:
        return str(path) in self.existing_files


def probe_system(cfg: Config) -> Probe:
    """Read real host state. Never mutates anything."""
    osrel: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                osrel[k] = v.strip('"')
    except OSError:
        pass

    for mgr in ("apt-get", "dnf", "pacman"):
        if shutil.which(mgr):
            pkg_manager = {"apt-get": "apt", "dnf": "dnf", "pacman": "pacman"}[mgr]
            break
    else:
        pkg_manager = "unknown"

    installed: set[str] = set()
    if pkg_manager == "apt":
        out = _capture(["dpkg-query", "-W", "-f=${Package} ${Status}\n"])
        installed = {ln.split()[0] for ln in out.splitlines() if ln.endswith("ok installed")}
    elif pkg_manager == "dnf":
        installed = set(_capture(["rpm", "-qa", "--qf", "%{NAME}\n"]).split())
    elif pkg_manager == "pacman":
        installed = {ln.split()[0] for ln in _capture(["pacman", "-Q"]).splitlines() if ln}

    arch = _capture(["dpkg", "--print-architecture"]).strip() or os.uname().machine

    files: dict[str, str] = {}
    for path in _managed_paths(cfg):
        if path.name in _SECRET_FILE_NAMES:
            # Existence only. These hold the obfuscated password blob: it is binary (so read_text
            # raises UnicodeDecodeError on it) and it is a secret, so it must never reach the
            # Probe, a state dump, or a diff. The planner only ever asks file_exists() for these.
            if path.exists():
                files[str(path)] = ""
            continue
        try:
            files[str(path)] = path.read_text()
        except (OSError, UnicodeDecodeError):
            pass

    novnc_exists = cfg.novnc_path.exists()
    novnc_commit = None
    novnc_origin = None
    if (cfg.novnc_path / ".git").exists():
        novnc_commit = _capture(["git", "-C", str(cfg.novnc_path), "rev-parse", "HEAD"]).strip() or None
        novnc_origin = _capture(
            ["git", "-C", str(cfg.novnc_path), "remote", "get-url", "origin"]
        ).strip() or None

    enabled = set()
    out = _capture(["systemctl", "--user", "list-unit-files", "--no-legend", "--state=enabled"])
    for line in out.splitlines():
        if line.strip():
            enabled.add(line.split()[0])

    return Probe(
        pkg_manager=pkg_manager,
        distro_id=osrel.get("ID", "unknown"),
        distro_codename=osrel.get("VERSION_CODENAME") or osrel.get("VERSION_ID", ""),
        arch=arch,
        installed_packages=frozenset(installed),
        existing_files=files,
        novnc_exists=novnc_exists,
        novnc_commit=novnc_commit,
        novnc_origin=novnc_origin,
        enabled_units=frozenset(enabled),
    )


def _capture(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


# Basenames whose content is a secret and is never read, compared or printed.
_SECRET_FILE_NAMES = frozenset({"passwd", ".kasmpasswd"})


def _managed_paths(cfg: Config) -> list[Path]:
    home = cfg.home
    units = home / ".config/systemd/user"
    return [
        home / ".vnc/xstartup",
        home / ".vnc/config",
        home / ".vnc/kasmvnc.yaml",
        # Password files are probed (existence only, never content) so the planner
        # stays a pure function of Config+Probe rather than stat-ing the host itself.
        home / ".vnc/passwd",
        home / ".kasmpasswd",
        units / vnc_unit_name(cfg),
        units / novnc_unit_name(cfg),
        units / kasm_unit_name(cfg),
    ]


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


@dataclass
class Step:
    label: str
    kind: str  # "pkg" | "write" | "exec" | "download" | "note"
    satisfied: bool
    argv: list[str] = field(default_factory=list)
    path: str = ""
    content: str = ""
    mode: int = 0o644
    secret_env: str = ""  # env var NAME whose value is piped to argv on stdin

    @property
    def display(self) -> str:
        if self.kind == "write":
            return f"write {self.path} (mode {self.mode:04o}, {len(self.content)} bytes)"
        if self.kind == "note":
            return self.label
        if self.kind == "download":
            url, sha = self.argv
            return f"download {url}\n         verify sha256 {sha}, then install with the system package manager"
        if self.secret_env:
            return f"run {' '.join(self.argv)}  <<< ${self.secret_env} (value never printed)"
        return f"run {' '.join(self.argv)}"


def kasm_asset(probe: Probe) -> tuple[str, str]:
    """Return (asset_name, sha256). Raises ConfigError when unpinned."""
    key = f"{probe.distro_id}:{probe.distro_codename}:{probe.arch}"
    template = KASMVNC_ASSETS.get(key)
    if template is None:
        supported = ", ".join(sorted(KASMVNC_ASSETS))
        raise ConfigError(
            f"KasmVNC: no pinned release asset for '{key}'. Supported: {supported}"
        )
    asset = template.format(v=KASMVNC_VERSION)
    sha = KASMVNC_SHA256.get(asset)
    if not sha:
        raise ConfigError(f"KasmVNC: no pinned SHA256 for '{asset}'; refusing to download unverified")
    return asset, sha


def install_argv(pkg_manager: str, packages: list[str]) -> list[str]:
    if pkg_manager == "apt":
        return ["sudo", "apt-get", "install", "-y", "--no-install-recommends", *packages]
    if pkg_manager == "dnf":
        return ["sudo", "dnf", "install", "-y", *packages]
    if pkg_manager == "pacman":
        return ["sudo", "pacman", "-S", "--needed", "--noconfirm", *packages]
    raise ConfigError(f"unsupported package manager: {pkg_manager}")


def plan_install(cfg: Config, probe: Probe) -> list[Step]:
    steps: list[Step] = []
    table = PACKAGES.get(probe.pkg_manager)
    if table is None:
        raise ConfigError(
            f"unsupported package manager '{probe.pkg_manager}'; "
            f"supported: {', '.join(sorted(PACKAGES))}"
        )

    wanted: list[str] = []
    if cfg.backend == "tigervnc":
        wanted += table["tigervnc"]
        if cfg.novnc_enabled:
            wanted += table["novnc"]
    wanted += table["xfce" if cfg.session == "xfce" else "gnome"]

    missing = [p for p in wanted if p not in probe.installed_packages]
    if missing:
        steps.append(
            Step(
                label=f"install {len(missing)} package(s)",
                kind="pkg",
                satisfied=False,
                argv=install_argv(probe.pkg_manager, missing),
            )
        )
    else:
        steps.append(Step(label=f"packages already present: {', '.join(wanted)}", kind="pkg", satisfied=True))

    if cfg.backend == "kasmvnc":
        asset, sha = kasm_asset(probe)
        url = KASMVNC_RELEASE_URL.format(version=KASMVNC_VERSION, asset=asset)
        already = "kasmvncserver" in probe.installed_packages
        steps.append(
            Step(
                label=f"KasmVNC {KASMVNC_VERSION} ({asset}, sha256 verified)",
                kind="download",
                satisfied=already,
                argv=[url, sha],
            )
        )

    if cfg.backend == "tigervnc" and cfg.novnc_enabled:
        at_pin = probe.novnc_commit == NOVNC_COMMIT
        if probe.novnc_exists and probe.novnc_origin != NOVNC_REPO:
            raise ConfigError(
                f"noVNC install path {cfg.novnc_path} already exists but is not a clone of "
                f"{NOVNC_REPO}; refusing to overwrite it"
            )
        if not probe.novnc_exists:
            steps.append(
                Step(
                    label=f"clone noVNC {NOVNC_TAG} into {cfg.novnc_path}",
                    kind="exec",
                    satisfied=False,
                    argv=["git", "clone", "--depth", "1", "--branch", NOVNC_TAG, NOVNC_REPO, str(cfg.novnc_path)],
                )
            )
        elif not at_pin:
            steps.append(
                Step(
                    label=f"fetch noVNC {NOVNC_TAG}",
                    kind="exec",
                    satisfied=False,
                    argv=["git", "-C", str(cfg.novnc_path), "fetch", "--depth", "1", "origin", "tag", NOVNC_TAG],
                )
            )
        steps.append(
            Step(
                label=f"noVNC checkout pinned at {NOVNC_COMMIT}",
                kind="exec",
                satisfied=at_pin,
                argv=["git", "-C", str(cfg.novnc_path), "checkout", "--detach", NOVNC_COMMIT],
            )
        )
    return steps


def plan_configure(cfg: Config, probe: Probe) -> list[Step]:
    steps: list[Step] = []
    home = cfg.home
    units = home / ".config/systemd/user"

    artifacts: list[tuple[Path, str, int]] = []
    if cfg.backend == "tigervnc":
        artifacts.append((home / ".vnc/xstartup", render_xstartup(cfg), 0o700))
        artifacts.append((home / ".vnc/config", render_vnc_config(cfg), 0o600))
        artifacts.append((units / vnc_unit_name(cfg), render_vnc_unit(cfg), 0o644))
        if cfg.novnc_enabled:
            artifacts.append((units / novnc_unit_name(cfg), render_novnc_unit(cfg), 0o644))
    else:
        artifacts.append((home / ".vnc/xstartup", render_xstartup(cfg), 0o700))
        artifacts.append((home / ".vnc/kasmvnc.yaml", render_kasm_yaml(cfg), 0o600))
        artifacts.append((units / kasm_unit_name(cfg), render_kasm_unit(cfg), 0o644))

    changed_unit = False
    for path, content, mode in artifacts:
        satisfied = probe.file_matches(path, content)
        if not satisfied and str(path).endswith(".service"):
            changed_unit = True
        steps.append(
            Step(
                label=f"{'unchanged' if satisfied else 'update'} {path}",
                kind="write",
                satisfied=satisfied,
                path=str(path),
                content=content,
                mode=mode,
            )
        )

    passwd_file = home / (".kasmpasswd" if cfg.backend == "kasmvnc" else ".vnc/passwd")
    env_name = cfg.kasm_password_env if cfg.backend == "kasmvnc" else cfg.vnc_password_env
    have_passwd = probe.file_exists(passwd_file)
    if cfg.backend == "kasmvnc":
        argv = ["kasmvncpasswd", "-u", cfg.user, "-w", "-r", str(passwd_file)]
    else:
        argv = ["vncpasswd", "-f"]
    steps.append(
        Step(
            label=f"VNC password file {passwd_file}",
            kind="exec",
            satisfied=have_passwd and not cfg.force,
            argv=argv,
            path=str(passwd_file),
            mode=0o600,
            secret_env=env_name,
        )
    )

    # daemon-reload only when a unit actually changed — reloading unconditionally
    # would make every run look like it did work.
    steps.append(
        Step(
            label="systemctl --user daemon-reload",
            kind="exec",
            satisfied=not changed_unit,
            argv=["systemctl", "--user", "daemon-reload"],
        )
    )

    unit = kasm_unit_name(cfg) if cfg.backend == "kasmvnc" else vnc_unit_name(cfg)
    to_enable = [unit]
    if cfg.backend == "tigervnc" and cfg.novnc_enabled:
        to_enable.append(novnc_unit_name(cfg))
    for name in to_enable:
        steps.append(
            Step(
                label=f"enable {name}",
                kind="exec",
                satisfied=name in probe.enabled_units,
                argv=["systemctl", "--user", "enable", name],
            )
        )

    # Keyed off the bind CLASS, not allow_public_exposure: a tailnet bind never
    # sets that flag, and neither of the other two branches is right for it —
    # opening the port to the world is wrong, and an SSH tunnel is unnecessary.
    reachable = cfg.novnc_bind if cfg.novnc_enabled else cfg.vnc_bind
    if bind_class(reachable) == "tailnet":
        firewall_note = (
            "firewall NOT modified by this tool. Allow the tailnet interface only: "
            "sudo ufw allow in on tailscale0"
        )
    elif cfg.allow_public_exposure:
        firewall_note = (
            "firewall NOT modified by this tool. If you opted into public exposure, "
            f"open the port yourself: sudo ufw allow {cfg.novnc_port}/tcp"
        )
    else:
        firewall_note = (
            "firewall NOT modified. Access path is an SSH tunnel: "
            f"ssh -N -L {cfg.novnc_port}:127.0.0.1:{cfg.novnc_port} {cfg.user}@<host>"
        )
    steps.append(Step(label=firewall_note, kind="note", satisfied=True))
    return steps


def render_kasm_unit(cfg: Config) -> str:
    argv = [
        "/usr/bin/vncserver",
        "-fg",
        f":{cfg.display}",
        "-geometry",
        cfg.geometry,
        "-depth",
        str(cfg.depth),
        "-websocketPort",
        str(cfg.novnc_port),
        "-interface",
        cfg.vnc_bind,
    ]
    return _unit(f"KasmVNC {KASMVNC_VERSION} on :{cfg.display}", argv,
                 service_extra=[f"ExecStop=/usr/bin/vncserver -kill :{cfg.display}"])


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def execute(steps: list[Step], apply: bool) -> int:
    pending = [s for s in steps if not s.satisfied and s.kind != "note"]
    for step in steps:
        if step.kind == "note":
            print(f"  note   {step.label}")
        elif step.satisfied:
            print(f"  ok     {step.label}")
        elif not apply:
            print(f"  would  {step.display}")
        else:
            print(f"  doing  {step.display}")
            rc = run_step(step)
            if rc != 0:
                print(f"  FAILED ({rc}) {step.label}", file=sys.stderr)
                return rc
    if not apply and pending:
        print(f"\nDry run: {len(pending)} step(s) pending. Re-run with --apply to execute.")
    elif not pending:
        print("\nNothing to do — already converged.")
    return 0


def run_step(step: Step) -> int:
    if step.kind == "write":
        path = Path(step.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o077)
        try:
            path.write_text(step.content)
            path.chmod(step.mode)
        finally:
            os.umask(old)
        return 0

    if step.kind == "download":
        url, sha = step.argv
        return install_verified_package(url, sha)

    if step.secret_env:
        secret = os.environ.get(step.secret_env)
        if not secret:
            print(
                f"  ${step.secret_env} is not set. Export it (or use a password manager) "
                "and re-run; the value is never written to argv or logs.",
                file=sys.stderr,
            )
            return 1
        target = Path(step.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o077)
        try:
            try:
                # Binary throughout: `vncpasswd -f` writes the DES-obfuscated blob to stdout, which
                # is not valid UTF-8, so text=True would raise UnicodeDecodeError inside run().
                proc = subprocess.run(
                    step.argv, input=secret.encode() + b"\n", capture_output=True, check=False
                )
            except FileNotFoundError:
                print(missing_tool_message(step.argv[0]), file=sys.stderr)
                return 127
            if proc.returncode != 0:
                # stderr may echo the prompt but never the value; still, do not relay it.
                print("  password tool failed (output suppressed to avoid leaking input)", file=sys.stderr)
                return proc.returncode
            if step.argv[0] == "vncpasswd":
                target.write_bytes(proc.stdout)
            target.chmod(0o600)
        finally:
            os.umask(old)
        return 0

    try:
        return subprocess.run(step.argv, check=False).returncode
    except FileNotFoundError:
        print(missing_tool_message(step.argv[0]), file=sys.stderr)
        return 127


# Which package to name when a tool is missing.  Keyed by executable, not by backend:
# the user sees the executable name in the error, so that is what they will search for.
TOOL_PACKAGES: dict[str, dict[str, str]] = {
    "vncpasswd": {"apt": "tigervnc-tools", "dnf": "tigervnc-server", "pacman": "tigervnc"},
    "vncserver": {"apt": "tigervnc-standalone-server", "dnf": "tigervnc-server", "pacman": "tigervnc"},
    "kasmvncpasswd": {"apt": "kasmvncserver", "dnf": "kasmvncserver", "pacman": "kasmvncserver"},
    "websockify": {"apt": "websockify", "dnf": "python3-websockify", "pacman": "python-websockify"},
}


def missing_tool_message(tool: str) -> str:
    pkgs = TOOL_PACKAGES.get(tool)
    hint = ""
    if pkgs:
        names = sorted(set(pkgs.values()))
        # Only the apt names are verified against the actual archive; the dnf/pacman ones are
        # best-effort, hence "usually" rather than a flat assertion the user might paste blind.
        hint = f" It usually ships in {' / '.join(names)}, depending on the distro."
    return (
        f"  {tool} is not installed.{hint}\n"
        f"  Run `{sys.argv[0]} install --apply` first, then re-run this command."
    )


def install_verified_package(url: str, expected_sha: str) -> int:
    """Download to a temp file, verify SHA256, then install. Never installs unverified."""
    import tempfile
    import urllib.request

    if not url.startswith("https://"):
        print(f"  refusing to download over a non-https URL: {url}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="remote-desktop-") as td:
        dest = Path(td) / url.rsplit("/", 1)[-1]
        print(f"         downloading {url}")
        urllib.request.urlretrieve(url, dest)
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        if digest != expected_sha:
            print(f"  SHA256 MISMATCH\n    expected {expected_sha}\n    got      {digest}", file=sys.stderr)
            return 1
        print(f"         sha256 ok ({digest[:16]}…)")
        if dest.suffix == ".deb":
            argv = ["sudo", "apt-get", "install", "-y", str(dest)]
        else:
            argv = ["sudo", "dnf", "install", "-y", str(dest)]
        return subprocess.run(argv, check=False).returncode


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def _bind_label(addr: str) -> str:
    """Status-line rendering of the bind class. Only 'public' shouts."""
    return {"loopback": "loopback", "tailnet": "tailnet"}.get(bind_class(addr), "EXPOSED")


def report_status(cfg: Config, probe: Probe) -> int:
    print(f"backend        {cfg.backend}  (session: {cfg.session}, display :{cfg.display})")
    print(f"distro         {probe.distro_id} {probe.distro_codename} {probe.arch} via {probe.pkg_manager}")
    print(f"vnc            {cfg.vnc_bind}:{cfg.resolved_vnc_port} "
          f"({_bind_label(cfg.vnc_bind)})")
    if cfg.novnc_enabled:
        scheme = "https" if (cfg.novnc_tls_cert and cfg.novnc_tls_key) else "http"
        print(f"novnc          {scheme}://{cfg.novnc_bind}:{cfg.novnc_port}/vnc.html "
              f"({_bind_label(cfg.novnc_bind)})")
        print(f"novnc checkout {probe.novnc_commit or '<absent>'} "
              f"({'at pin' if probe.novnc_commit == NOVNC_COMMIT else 'NOT at pin ' + NOVNC_COMMIT[:12]})")

    # TigerVNC and KasmVNC both ship /usr/bin/vncserver and conflict as packages.
    have_tiger = any(p.startswith("tigervnc") for p in probe.installed_packages)
    have_kasm = "kasmvncserver" in probe.installed_packages
    if have_tiger and have_kasm:
        print("conflict       BOTH tigervnc and kasmvncserver are installed; they both own "
              "/usr/bin/vncserver. Remove one.")

    for path in _managed_paths(cfg):
        state = "present" if probe.file_exists(path) else "absent"
        print(f"  {state:8} {path}")
    for name in (vnc_unit_name(cfg), novnc_unit_name(cfg), kasm_unit_name(cfg)):
        print(f"  {'enabled' if name in probe.enabled_units else 'disabled':8} {name}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remote-desktop.py",
        description="Set up a Linux remote desktop: TigerVNC+GNOME, optional noVNC, or KasmVNC.",
        epilog="Mutating subcommands are dry runs unless --apply is given.",
    )
    p.add_argument("command", choices=("plan", "install", "configure", "status", "print-config"))
    p.add_argument("-c", "--config", type=Path, help="TOML config file (see config/remote-desktop.toml.example)")
    p.add_argument("--apply", action="store_true", help="actually perform the steps (default: dry run)")
    p.add_argument("--force", action="store_true", help="regenerate the password file even if it exists")
    p.add_argument("--format", choices=("toml", "json"), default="toml", help="print-config output format")

    g = p.add_argument_group("overrides (take precedence over the TOML file)")
    g.add_argument("--backend", choices=BACKENDS)
    g.add_argument("--user")
    g.add_argument("--display", type=int)
    g.add_argument("--geometry")
    g.add_argument("--depth", type=int)
    g.add_argument("--session", choices=SESSIONS)
    g.add_argument("--vnc-bind")
    g.add_argument("--vnc-port", type=int)
    g.add_argument("--novnc", dest="novnc_enabled", action="store_true", default=None)
    g.add_argument("--no-novnc", dest="novnc_enabled", action="store_false", default=None)
    g.add_argument("--novnc-bind")
    g.add_argument("--novnc-port", type=int)
    return p


CLI_FIELDS = (
    "backend", "user", "display", "geometry", "depth", "session",
    "vnc_bind", "vnc_port", "novnc_enabled", "novnc_bind", "novnc_port",
)


def cli_overrides(ns: argparse.Namespace) -> dict[str, object]:
    return {name: getattr(ns, name, None) for name in CLI_FIELDS}


def config_to_toml(cfg: Config) -> str:
    """Round-trippable TOML for the resolved config. Contains no secrets by construction."""
    def lit(v: object) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        return json.dumps(v)

    out = [MANAGED_HEADER.replace("are overwritten", "reflect the resolved config"), ""]
    for table in ("general", "vnc", "novnc", "kasmvnc", "security"):
        rows = [(k.split(".", 1)[1], f) for k, f in TOML_MAP.items() if k.startswith(f"{table}.")]
        out.append(f"[{table}]")
        for key, fieldname in rows:
            out.append(f"{key} = {lit(getattr(cfg, fieldname))}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)

    try:
        toml_over = config_from_toml(ns.config) if ns.config else {}
        cfg = resolve_config(toml_over, cli_overrides(ns))
        cfg = replace(cfg, force=ns.force)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    errors = validate(cfg)
    if errors:
        print("error: configuration rejected", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    for note in exposure_notes(cfg):
        print(f"note: {note}", file=sys.stderr)

    if ns.command == "print-config":
        print(config_to_toml(cfg) if ns.format == "toml" else json.dumps(asdict(cfg), indent=2, default=str))
        return 0

    probe = probe_system(cfg)

    if ns.command == "status":
        return report_status(cfg, probe)

    try:
        if ns.command == "install":
            steps = plan_install(cfg, probe)
        elif ns.command == "configure":
            steps = plan_configure(cfg, probe)
        else:  # plan
            steps = plan_install(cfg, probe) + plan_configure(cfg, probe)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if ns.command == "plan":
        print(config_to_toml(cfg))
        print("--- plan ---")
        return execute(steps, apply=False)
    return execute(steps, apply=ns.apply)


if __name__ == "__main__":
    sys.exit(main())
