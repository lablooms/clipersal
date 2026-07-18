"""PyInstaller entry point for clipersal-trigger. See
entry_clipersal.py for why this indirection exists.
"""

from clipersal.trigger import main

if __name__ == "__main__":
    raise SystemExit(main())
