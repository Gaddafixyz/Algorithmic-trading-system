"""
Serialization utilities.

Uses orjson for performance (handles datetime, Decimal natively).
Falls back to stdlib json if orjson unavailable.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import orjson


def _default(obj: Any) -> Any:
    """Custom serialiser for types orjson doesn't handle."""
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def dumps(obj: Any, *, indent: bool = False) -> bytes:
    """Serialize object to JSON bytes using orjson."""
    options = orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY
    if indent:
        options |= orjson.OPT_INDENT_2
    return orjson.dumps(obj, default=_default, option=options)


def dumps_str(obj: Any, *, indent: bool = False) -> str:
    """Serialize object to JSON string."""
    return dumps(obj, indent=indent).decode("utf-8")


def loads(data: bytes | str | bytearray) -> Any:
    """Deserialize JSON bytes/string."""
    return orjson.loads(data)


def to_dict(model: Any) -> dict[str, Any]:
    """Convert a Pydantic model to a plain dict."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return dict(model)


def from_dict(model_class: type, data: dict[str, Any]) -> Any:
    """Construct a Pydantic model from a dict."""
    return model_class.model_validate(data)


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal and datetime."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)
