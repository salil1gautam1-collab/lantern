"""Entry point so `python -m lantern <subcommand>` works."""

from __future__ import annotations

import sys

from lantern.cli import cli


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cli()


if __name__ == "__main__":
    main()
