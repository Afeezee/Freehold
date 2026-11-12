"""What kind of link are we on, and may we use it?

Detection is best effort and platform-specific, so it is deliberately isolated
here behind one function returning one value. Everything that matters —
"should this backup run?" — is pure logic in NetworkPolicy and fully testable
without a network.

The honest limitation: there is no portable way to ask "is this connection
metered?", and a tethered phone over USB or a captive-portal Wi-Fi can look
exactly like cheap home Wi-Fi. `probe_command` exists so a user whose machine
reports the wrong thing can tell Freehold the truth instead of arguing with
it.
"""
from __future__ import annotations

import platform
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

WIFI = "wifi"
ETHERNET = "ethernet"
CELLULAR = "cellular"
METERED = "metered"
OFFLINE = "offline"
UNKNOWN = "unknown"

PROBE_TIMEOUT = 5


@dataclass(frozen=True)
class Link:
    kind: str
    detail: str = ""

    def __str__(self) -> str:
        return self.kind + (" (" + self.detail + ")" if self.detail else "")


def _run(cmd, shell: bool = False) -> str:
    try:
        out = subprocess.run(cmd, shell=shell, capture_output=True,
                             text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "").strip()


# Windows InterfaceType values (IANA ifType)
_WIN_TYPES = {"6": ETHERNET, "71": WIFI, "243": CELLULAR, "244": CELLULAR}


def _detect_windows() -> Link:
    # The active connection profile, mapped to its adapter's interface type.
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-NetConnectionProfile | ForEach-Object { $a = "
                "Get-NetAdapter -InterfaceIndex $_.InterfaceIndex "
                "-ErrorAction SilentlyContinue; if ($a) { "
                "\"$($a.InterfaceType)|$($a.Name)\" } }"])
    if not out:
        return Link(UNKNOWN, "no active connection profile")
    kinds = []
    for line in out.splitlines():
        itype, _, name = line.partition("|")
        kinds.append((_WIN_TYPES.get(itype.strip(), UNKNOWN), name.strip()))
    for wanted in (CELLULAR, WIFI, ETHERNET):
        for kind, name in kinds:
            if kind == wanted:
                return Link(kind, name)
    return Link(UNKNOWN, kinds[0][1] if kinds else "")


def _detect_linux() -> Link:
    route = _run(["ip", "-o", "route", "get", "1.1.1.1"])
    iface = ""
    parts = route.split()
    if "dev" in parts:
        iface = parts[parts.index("dev") + 1]
    if not iface:
        return Link(UNKNOWN, "no default route")
    import os
    base = "/sys/class/net/" + iface
    if os.path.isdir(base + "/wireless") or os.path.exists(base + "/phy80211"):
        return Link(WIFI, iface)
    if iface.startswith(("wwan", "ppp", "rmnet")):
        return Link(CELLULAR, iface)
    # NetworkManager knows about metered state and modems when it is present
    nm = _run(["nmcli", "-t", "-f", "TYPE,STATE", "device"])
    for line in nm.splitlines():
        kind, _, state = line.partition(":")
        if state.strip() == "connected" and kind.strip() == "gsm":
            return Link(CELLULAR, "gsm")
    return Link(ETHERNET, iface)


def _detect_macos() -> Link:
    route = _run(["route", "-n", "get", "default"])
    iface = ""
    for line in route.splitlines():
        if "interface:" in line:
            iface = line.split(":", 1)[1].strip()
    if not iface:
        return Link(UNKNOWN, "no default route")
    ports = _run(["networksetup", "-listallhardwareports"])
    kind = UNKNOWN
    current = ""
    for line in ports.splitlines():
        if line.startswith("Hardware Port:"):
            current = line.split(":", 1)[1].strip().lower()
        elif line.startswith("Device:") and line.split(":", 1)[1].strip() == iface:
            if "wi-fi" in current or "airport" in current:
                kind = WIFI
            elif "ethernet" in current or "lan" in current:
                kind = ETHERNET
            elif "iphone" in current or "modem" in current:
                kind = CELLULAR
            break
    return Link(kind, iface)


def detect(probe_command: Optional[str] = None) -> Link:
    """Best-effort classification of the link currently carrying traffic."""
    if probe_command:
        out = _run(shlex.split(probe_command) if not sys.platform.startswith(
            "win") else probe_command, shell=sys.platform.startswith("win"))
        word = out.split()[0].lower() if out else ""
        if word in (WIFI, ETHERNET, CELLULAR, METERED, OFFLINE, UNKNOWN):
            return Link(word, "probe_command")
        return Link(UNKNOWN, "probe_command said " + repr(out[:40]))
    system = platform.system()
    try:
        if system == "Windows":
            return _detect_windows()
        if system == "Linux":
            return _detect_linux()
        if system == "Darwin":
            return _detect_macos()
    except Exception as exc:          # detection must never crash the agent
        return Link(UNKNOWN, type(exc).__name__)
    return Link(UNKNOWN, system)


class NetworkPolicy:
    """Pure decision logic. No I/O, so the interesting cases are testable."""

    def __init__(self, rules, probe: Optional[Callable[[], Link]] = None,
                 clock: Callable[[], float] = time.time):
        self.rules = rules
        self._probe = probe or (lambda: detect(rules.probe_command))
        self._clock = clock
        self._cached: Optional[Link] = None
        self._checked_at = 0.0

    def link(self, force: bool = False) -> Link:
        now = self._clock()
        if (force or self._cached is None
                or now - self._checked_at >= self.rules.recheck_seconds):
            self._cached = self._probe()
            self._checked_at = now
        return self._cached

    def verdict(self, link: Optional[Link] = None) -> Tuple[bool, str]:
        link = link if link is not None else self.link()
        if link.kind == OFFLINE:
            return False, "offline"
        if link.kind == CELLULAR:
            if self.rules.allow_cellular:
                return True, str(link)
            return False, "on cellular (" + str(link) + ") and " \
                          "allow_cellular is false"
        if link.kind == METERED:
            if self.rules.allow_metered or self.rules.allow_cellular:
                return True, str(link)
            return False, "on a metered link (" + str(link) + ") and " \
                          "allow_metered is false"
        if link.kind in (WIFI, ETHERNET):
            return True, str(link)
        if self.rules.on_unknown == "block":
            return False, "link type unknown (" + str(link) + ") and " \
                          "on_unknown is block"
        return True, "link type unknown (" + str(link) + "), allowed"

    def allowed(self, force: bool = False) -> bool:
        return self.verdict(self.link(force=force))[0]
