"""Alternative start script with env override support."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> None:
    from src.main import main as app_main

    app_main()


if __name__ == "__main__":
    main()
