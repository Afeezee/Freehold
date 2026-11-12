"""Print a service definition for the current platform.

Deliberately prints rather than installs: registering a service touches the
system, often needs elevation, and is the user's decision to make. Copy what
this emits, read it, then run it.
"""
from __future__ import annotations

import os
import platform
import shlex
import sys
from pathlib import Path


def _python() -> str:
    return sys.executable or "python"


def _env(home: Path) -> str:
    return str(home)


def windows_task(home: Path) -> str:
    cmd = ('schtasks /Create /TN "Freehold Agent" /SC ONLOGON /RL LIMITED '
           '/TR "\'' + _python() + "' -m freehold.cli agent run\" /F")
    return (
        "# Run at logon via Task Scheduler (no elevation needed for /RL "
        "LIMITED):\n"
        "setx FREEHOLD_HOME \"" + _env(home) + "\"\n"
        + cmd + "\n\n"
        "# then start it now without waiting for a logon:\n"
        'schtasks /Run /TN "Freehold Agent"\n\n'
        "# to remove it:\n"
        'schtasks /Delete /TN "Freehold Agent" /F\n')


def systemd_unit(home: Path) -> str:
    return (
        "# ~/.config/systemd/user/freehold-agent.service\n"
        "[Unit]\n"
        "Description=Freehold backup agent\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "Environment=FREEHOLD_HOME=" + _env(home) + "\n"
        "ExecStart=" + shlex.quote(_python()) + " -m freehold.cli agent run\n"
        "Restart=on-failure\n"
        "RestartSec=30\n\n"
        "[Install]\n"
        "WantedBy=default.target\n\n"
        "# then:\n"
        "#   systemctl --user daemon-reload\n"
        "#   systemctl --user enable --now freehold-agent\n"
        "#   journalctl --user -u freehold-agent -f\n"
        "# and, so it runs while you are logged out:\n"
        "#   loginctl enable-linger $USER\n")


def launchd_plist(home: Path) -> str:
    return (
        "<!-- ~/Library/LaunchAgents/dev.freehold.agent.plist -->\n"
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>dev.freehold.agent</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        "    <string>" + _python() + "</string>\n"
        "    <string>-m</string><string>freehold.cli</string>\n"
        "    <string>agent</string><string>run</string>\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key><dict>\n"
        "    <key>FREEHOLD_HOME</key><string>" + _env(home) + "</string>\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><dict>\n"
        "    <key>SuccessfulExit</key><false/>\n"
        "  </dict>\n"
        "</dict></plist>\n\n"
        "<!-- then: launchctl load -w "
        "~/Library/LaunchAgents/dev.freehold.agent.plist -->\n")


def definition(home: Path, system: str = "") -> str:
    system = system or platform.system()
    if system == "Windows":
        return windows_task(home)
    if system == "Darwin":
        return launchd_plist(home)
    if system == "Linux":
        return systemd_unit(home)
    return ("No service template for " + system + ". Run it yourself:\n"
            "  FREEHOLD_HOME=" + _env(home) + " " + _python()
            + " -m freehold.cli agent run\n")


def note(home: Path) -> str:
    key = Path(home) / "key.json"
    warning = ""
    if key.exists():
        try:
            wrapped = '"wrapped": true' in key.read_text()
        except OSError:
            wrapped = False
        if wrapped:
            warning = (
                "\nNOTE: your key is passphrase-wrapped, so an unattended "
                "agent cannot unlock it. Either run the agent with "
                "--passphrase (which puts the passphrase in the service "
                "definition and in process listings), or keep an unwrapped "
                "key readable only by your user and rely on filesystem "
                "permissions.\n")
    return warning
