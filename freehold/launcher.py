"""The entry point for the packaged application.

Double-clicked with no arguments, it sets itself up if needed, starts the
dashboard and opens a browser. Called with arguments, it is the ordinary
command line. One binary, two audiences.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _home() -> Path:
    env = os.environ.get("FREEHOLD_HOME")
    return Path(env) if env else Path.home() / ".freehold"


def _first_run(home: Path) -> None:
    """Create a key and manifest the first time, and say what that means."""
    from .core import KeyRing, Manifest

    home.mkdir(parents=True, exist_ok=True)
    keyfile = home / "key.json"
    if keyfile.exists():
        return
    print("Setting up Freehold for the first time.")
    KeyRing.generate().save(keyfile)
    Manifest(home / "manifest.db").close()
    print("")
    print("  Your encryption key is at:")
    print("      " + str(keyfile))
    print("")
    print("  COPY THAT FILE SOMEWHERE SAFE AND OFFLINE NOW.")
    print("  It is the only thing that can read your backups. Nobody can")
    print("  recover it for you - not the author, not GitHub, nobody.")
    print("")


def _open_when_ready(url: str) -> None:
    time.sleep(1.2)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def run_app() -> int:
    """No arguments: be an application."""
    from .core import KeyRing, Manifest
    from .dashboard import Dashboard, DashboardAPI

    home = _home()
    _first_run(home)
    manifest = Manifest(home / "manifest.db")
    keyring = KeyRing.load(home / "key.json")
    api = DashboardAPI(home, manifest, keyring)

    port = int(os.environ.get("FREEHOLD_PORT") or 8731)
    board = None
    for candidate in range(port, port + 20):
        try:
            board = Dashboard(api, port=candidate)
            break
        except OSError:
            continue
    if board is None:
        print("Could not find a free port between " + str(port) + " and "
              + str(port + 19) + ".")
        return 1

    print("Freehold is running.")
    print("")
    print("    " + board.url)
    print("")
    print("Opening that in your browser. Keep this window open while you")
    print("use it; closing it stops Freehold. Press Ctrl-C to stop.")
    threading.Thread(target=_open_when_ready, args=(board.url,),
                     daemon=True).start()
    try:
        board.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        board.stop()
        manifest.close()
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        from .cli import main as cli_main
        return cli_main(argv)
    try:
        return run_app()
    except Exception as exc:
        print("")
        print("Freehold could not start: " + str(exc))
        print("")
        # A double-clicked window closes instantly on error otherwise, and
        # the person never sees why.
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to close...")
        return 1


if __name__ == "__main__":
    sys.exit(main())
