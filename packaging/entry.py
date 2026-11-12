"""PyInstaller entry point. Kept separate so the spec has a plain script to
analyse rather than a package submodule."""
import multiprocessing
import sys

from freehold.launcher import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
