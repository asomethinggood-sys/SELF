"""Allow ``python -m havoc`` as an alias for the ``havoc`` console script."""

from __future__ import annotations

from .main import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
