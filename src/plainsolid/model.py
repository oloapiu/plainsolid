"""The feature tree: plain data produced by parsing a model file.

Nothing in here touches the geometry kernel. A Document is what the parser
returns, what the server sends to clients (as JSON), and what the evaluator
consumes.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

PLANES = ("XY", "XZ", "YZ")


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


@dataclass
class DocumentError:
    """A problem found while parsing or executing the file."""

    message: str
    line: int | None = None
    kind: str = "error"  # error | warning

    def to_json(self) -> dict[str, Any]:
        return {"message": self.message, "line": self.line, "kind": self.kind}


@dataclass
class Param:
    name: str
    value: float
    description: str = ""
    line: int | None = None
    expression: str | None = None  # source text of the value, e.g. "wall * 2"
    read_only: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "description": self.description,
            "line": self.line,
            "expression": self.expression,
            "read_only": self.read_only,
        }


@dataclass
class Entity:
    """A sketch entity: line, circle, rect, slot, polygon."""

    name: str
    kind: str
    args: dict[str, Any]
    construction: bool = False
    line: int | None = None
    arg_texts: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "args": _jsonable(self.args),
            "arg_texts": dict(self.arg_texts),
            "construction": self.construction,
            "line": self.line,
        }


@dataclass
class Constraint:
    """A sketch constraint or dimension. refs name entities or their parts
    ("line1", "line1.start", "rect1.top"); value is the dimension value."""

    name: str
    kind: str
    refs: list[str]
    value: float | None = None
    options: dict[str, Any] = field(default_factory=dict)
    line: int | None = None
    value_text: str | None = None  # source text of the value, e.g. "width / 2"

    @property
    def is_dimension(self) -> bool:
        return self.value is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "refs": list(self.refs), "value": self.value,
            "options": _jsonable(self.options), "line": self.line, "value_text": self.value_text,
            "dimension": self.is_dimension,
        }


@dataclass
class Feature:
    name: str
    kind: str  # sketch | extrude | cut
    args: dict[str, Any]  # evaluated argument values
    arg_texts: dict[str, str] = field(default_factory=dict)  # kwarg -> source text
    span: tuple[int, int] | None = None  # (start_line, end_line), 1-based inclusive
    read_only: bool = False
    variable: str | None = None
    entities: list[Entity] = field(default_factory=list)  # sketches only
    constraints: list[Constraint] = field(default_factory=list)  # sketches only
    suppressed: bool = False

    def entity(self, name: str) -> Entity | None:
        for e in self.entities:
            if e.name == name:
                return e
        return None

    def constraint(self, name: str) -> Constraint | None:
        for c in self.constraints:
            if c.name == name:
                return c
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "args": _jsonable(self.args),
            "arg_texts": dict(self.arg_texts),
            "span": list(self.span) if self.span else None,
            "read_only": self.read_only,
            "variable": self.variable,
            "entities": [e.to_json() for e in self.entities],
            "constraints": [c.to_json() for c in self.constraints],
            "suppressed": self.suppressed,
        }


@dataclass
class Document:
    kind: str = "part"
    meta: dict[str, Any] = field(default_factory=dict)
    params: list[Param] = field(default_factory=list)
    features: list[Feature] = field(default_factory=list)
    errors: list[DocumentError] = field(default_factory=list)
    source: str = ""
    path: str | None = None

    @property
    def hash(self) -> str:
        return source_hash(self.source)

    def feature(self, name: str) -> Feature | None:
        for f in self.features:
            if f.name == name:
                return f
        return None

    def param(self, name: str) -> Param | None:
        for p in self.params:
            if p.name == name:
                return p
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "meta": _jsonable(self.meta),
            "params": [p.to_json() for p in self.params],
            "features": [f.to_json() for f in self.features],
            "errors": [e.to_json() for e in self.errors],
            "hash": self.hash,
            "path": self.path,
        }


def _jsonable(value: Any) -> Any:
    """Convert argument values to JSON-safe primitives."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return float(value) if isinstance(value, float) else value
    if hasattr(value, "ref_name"):  # handles to sketches, planes
        return value.ref_name
    return str(value)
