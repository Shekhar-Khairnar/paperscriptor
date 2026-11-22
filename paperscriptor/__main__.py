"""`python -m paperscriptor` starts the web app; `python -m paperscriptor <command>` runs a CLI command."""

from .cli import main

raise SystemExit(main())
