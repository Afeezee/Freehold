"""Where a user's backend credentials live when there is no shell.

Environment variables are the right answer for someone working in a
terminal, and the wrong answer for someone who double-clicks an .exe. So
credentials resolve in this order:

    1. the environment            (unchanged; still wins)
    2. credentials.json in FREEHOLD_HOME

This is a plain file with filesystem permissions, the same arrangement as
~/.aws/credentials or ~/.netrc. It is NOT encrypted, and it deliberately does
not pretend to be: the process has to be able to read it unattended, so any
key it was encrypted with would have to sit right next to it.

It is kept out of the manifest on purpose. The manifest gets copied into the
backends so a dead machine can be recovered; credentials must not ride along
to the very service they authenticate to.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Dict, List, Optional

CREDENTIALS_NAME = "credentials.json"


def home_dir() -> Path:
    env = os.environ.get("FREEHOLD_HOME")
    return Path(env) if env else Path.home() / ".freehold"


def path(home: Optional[Path] = None) -> Path:
    return (Path(home) if home else home_dir()) / CREDENTIALS_NAME


def _load(home: Optional[Path] = None) -> Dict[str, str]:
    p = path(home)
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text())
    except (ValueError, OSError):
        return {}
    return {str(k): str(v) for k, v in doc.items()} if isinstance(doc, dict) \
        else {}


def get(name: str, home: Optional[Path] = None) -> Optional[str]:
    """Environment first, then the file. Returns None if neither has it."""
    value = os.environ.get(name)
    if value:
        return value
    return _load(home).get(name)


def set_value(name: str, value: str, home: Optional[Path] = None) -> Path:
    p = path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = _load(home)
    doc[name] = value
    p.write_text(json.dumps(doc, indent=2))
    _lock_down(p)
    return p


def unset(name: str, home: Optional[Path] = None) -> bool:
    doc = _load(home)
    if name not in doc:
        return False
    del doc[name]
    p = path(home)
    p.write_text(json.dumps(doc, indent=2))
    _lock_down(p)
    return True


def names(home: Optional[Path] = None) -> List[str]:
    """The names that are stored. Never the values."""
    return sorted(_load(home))


def sources(name: str, home: Optional[Path] = None) -> str:
    if os.environ.get(name):
        return "environment"
    if name in _load(home):
        return "credentials.json"
    return "not set"


def _lock_down(p: Path) -> None:
    """Owner-only, where the platform understands that."""
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    if os.name == "nt":
        # Windows ignores chmod; remove inherited access and grant the
        # current user only. Best effort: a failure here is not fatal.
        try:
            import subprocess
            user = os.environ.get("USERNAME") or ""
            if user:
                subprocess.run(["icacls", str(p), "/inheritance:r",
                                "/grant:r", user + ":F"],
                               capture_output=True, timeout=10)
        except Exception:
            pass
