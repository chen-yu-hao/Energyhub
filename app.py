"""Backward-compatible standalone service: python -m Energyhub.app."""
from __future__ import annotations
import sys


def main(argv=None):
    from .cli import main as cli_main
    return cli_main(['serve', *(sys.argv[1:] if argv is None else argv)])


if __name__ == '__main__':
    raise SystemExit(main())
