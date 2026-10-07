"""Allow python -m Energyhub wherever the package is installed."""
from .cli import main

raise SystemExit(main())
