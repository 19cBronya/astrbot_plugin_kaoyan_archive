from __future__ import annotations

import dataclasses
import json
import time
from enum import Enum
from pathlib import Path
from typing import Any


def utc_timestamp() -> float:
    return time.time()


def json_safe(value: Any, *, depth: int = 0) -> Any:
    """Convert adapter objects into bounded JSON-compatible data without pickle."""
    if depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return json_safe(dataclasses.asdict(value), depth=depth + 1)
    if isinstance(value, dict):
        return {
            str(key): json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:1000]
        }
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item, depth=depth + 1) for item in list(value)[:1000]]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return json_safe(model_dump(mode="json"), depth=depth + 1)
        except Exception:
            pass
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return json_safe(attrs, depth=depth + 1)
    return {"type": type(value).__name__, "repr": repr(value)[:2000]}


def canonical_json(value: Any) -> str:
    return json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
