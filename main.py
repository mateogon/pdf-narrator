"""Backward-compatible launcher for the desktop app and CLI."""

from cli import main

if __name__ == "__main__":
    raise SystemExit(main())
