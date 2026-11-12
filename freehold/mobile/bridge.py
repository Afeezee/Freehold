"""One entry point for a native layer to call: JSON in, JSON out.

Kotlin (Chaquopy) or Swift does not want to marshal Python objects across the
boundary, and every extra entry point is another thing to keep in step. So
there is exactly one:

    from freehold.mobile.bridge import handle
    reply = handle('{"cmd": "sync", "home": "/data/.../freehold"}')

Every reply is `{"ok": true, ...}` or `{"ok": false, "error": "..."}`. The
bridge never raises into the host.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Union

from ..core.errors import FreeholdError
from .session import Conditions, MobileSession

_sessions: Dict[str, MobileSession] = {}
_lock = threading.Lock()


def _session(payload: Dict[str, Any]) -> MobileSession:
    home = payload.get("home")
    if not home:
        raise FreeholdError("every call needs a 'home' path")
    key = str(Path(home).resolve())
    with _lock:
        session = _sessions.get(key)
        if session is None:
            session = MobileSession.open(Path(home),
                                         payload.get("passphrase"),
                                         payload.get("device"))
            _sessions[key] = session
        return session


def close_all() -> None:
    with _lock:
        for session in _sessions.values():
            session.close()
        _sessions.clear()


def _conditions(payload: Dict[str, Any]) -> Conditions:
    raw = payload.get("conditions") or {}
    return Conditions(
        network=str(raw.get("network", "unknown")),
        charging=bool(raw.get("charging", True)),
        battery_pct=int(raw.get("battery_pct", 100)),
        detail=str(raw.get("detail", "")),
    )


COMMANDS = ("enrol", "status", "add_folder", "remove_folder", "should_sync",
            "sync", "snapshots", "browse", "restore", "close")


def _dispatch(payload: Dict[str, Any]) -> Dict[str, Any]:
    cmd = payload.get("cmd")
    if not cmd:
        raise FreeholdError("no 'cmd' in request")
    # Checked before anything is opened, so a typo reports the typo rather
    # than whatever the session failed on first.
    if cmd not in COMMANDS:
        raise FreeholdError("unknown cmd " + repr(cmd) + "; known: "
                            + ", ".join(COMMANDS))

    if cmd == "enrol":
        session = MobileSession.enrol(Path(payload["home"]),
                                      str(payload["key"]),
                                      str(payload.get("device") or "phone"))
        with _lock:
            _sessions[str(Path(payload["home"]).resolve())] = session
        return session.status()

    session = _session(payload)

    if cmd == "status":
        return session.status()
    if cmd == "add_folder":
        return session.add_folder(str(payload["path"]),
                                  int(payload.get("interval", 3600)))
    if cmd == "remove_folder":
        return {"removed": session.remove_folder(str(payload["path"]))}
    if cmd == "should_sync":
        ok, why = session.should_sync(_conditions(payload))
        return {"should_sync": ok, "reason": why}
    if cmd == "sync":
        return session.sync(
            _conditions(payload),
            budget_seconds=payload.get("budget_seconds"),
            max_bytes=payload.get("max_bytes"),
            force=bool(payload.get("force"))).as_dict()
    if cmd == "snapshots":
        return {"snapshots": session.snapshots(payload.get("device"))}
    if cmd == "browse":
        return session.browse(int(payload["snapshot"]),
                              str(payload.get("prefix", "")))
    if cmd == "restore":
        return session.restore(int(payload["snapshot"]),
                               str(payload["dest"]),
                               payload.get("paths"),
                               bool(payload.get("overwrite")))
    if cmd == "close":
        close_all()
        return {"closed": True}
    raise FreeholdError("unknown cmd " + repr(cmd))


def handle(request: Union[str, bytes, Dict[str, Any]]) -> str:
    """Take a JSON request, return a JSON reply. Never raises."""
    try:
        payload = (request if isinstance(request, dict)
                   else json.loads(request))
        if not isinstance(payload, dict):
            raise FreeholdError("request must be a JSON object")
    except Exception as exc:
        return json.dumps({"ok": False, "error": "bad request: " + str(exc)})
    try:
        result = _dispatch(payload)
        return json.dumps({"ok": True, **result}, default=str)
    except FreeholdError as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    except KeyError as exc:
        return json.dumps({"ok": False,
                           "error": "missing field " + str(exc)})
    except Exception as exc:
        return json.dumps({"ok": False,
                           "error": type(exc).__name__ + ": " + str(exc)})
