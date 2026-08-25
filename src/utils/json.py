"""JSON helpers wrapping orjson for performance."""

from __future__ import annotations

from src.core.serialization import dumps, dumps_str, loads

__all__ = ["dumps", "dumps_str", "loads"]
