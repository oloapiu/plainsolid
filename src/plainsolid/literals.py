"""Python source serialization shared by edits and generated model files."""

from __future__ import annotations

import json
import math
from typing import Any


def python_literal(value: Any) -> str:
    """Serialize DSL values; an explicit {expr: text} represents Python syntax."""
    if isinstance(value, dict) and set(value) == {"expr"} and isinstance(value["expr"], str):
        return value["expr"]
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("numbers written into source must be finite")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False) if '"' not in value else repr(value)
    if isinstance(value, (list, tuple)):
        pair = len(value) == 2 and all(isinstance(v, (int, float)) for v in value)
        content = ", ".join(python_literal(v) for v in value)
        if isinstance(value, tuple) or pair:
            return "(" + content + ("," if len(value) == 1 else "") + ")"
        return "[" + content + "]"
    if value is None:
        return "None"
    if isinstance(value, dict) and "expr" not in value and all(isinstance(k, str) for k in value):  # corners={"tl": 8}
        return "{" + ", ".join(f"{python_literal(k)}: {python_literal(v)}" for k, v in value.items()) + "}"
    raise ValueError(f"cannot write {value!r} into source")
