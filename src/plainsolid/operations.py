"""Edit request contracts shared by HTTP, CLI/MCP and generated client types.

Operation envelopes are strict. DSL-specific argument semantics still belong to
composition and evaluation, which report model errors in the usual way.
"""

from __future__ import annotations

import math
from functools import reduce
from operator import or_
from typing import Annotated, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    create_model,
    model_validator,
)

type JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | list["JsonValue"]
    | tuple["JsonValue", ...]
    | dict[str, "JsonValue"]
)
Vector3 = Annotated[list[float] | tuple[float, float, float], Field(min_length=3, max_length=3)]


def _check_expressions(value):
    if isinstance(value, dict):
        if "expr" in value and (
            set(value) != {"expr"}
            or not isinstance(value["expr"], str)
            or not value["expr"].strip()
        ):
            raise ValueError(
                "an expression must be an object containing only a non-empty expr string"
            )
        for child in value.values():
            _check_expressions(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _check_expressions(child)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("numbers must be finite")


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def expressions(self):
        payload = self.model_dump()
        if "value" in payload:
            _check_expressions(payload["value"])
        for field in ("args", "options"):
            for value in (payload.get(field) or {}).values():
                _check_expressions(value)
        return self


class SetArgument(Operation):
    op: Literal["set_argument"]
    feature: str
    kwarg: str
    value: JsonValue


class SetParameter(Operation):
    op: Literal["set_parameter"]
    name: str
    value: JsonValue


class AddParameter(Operation):
    op: Literal["add_parameter"]
    name: str
    value: JsonValue
    description: str | None = None


class DeleteParameter(Operation):
    op: Literal["delete_parameter"]
    name: str


class SetMeta(Operation):
    op: Literal["set_meta"]
    key: str
    value: JsonValue


class Composed(Operation):
    statement: str | None = None
    kind: str | None = None
    name: str | None = None
    args: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def composition(self):
        if not self.statement and (not self.kind or not self.name):
            raise ValueError("provide a statement, or both kind and name")
        return self


class AddFeature(Composed):
    op: Literal["add_feature"]
    after: str | None = None


class DeleteFeature(Operation):
    op: Literal["delete_feature"]
    feature: str
    cascade: bool = True


class AddSketchEntity(Composed):
    op: Literal["add_sketch_entity"]
    sketch: str


class DeleteSketchEntity(Operation):
    op: Literal["delete_sketch_entity"]
    sketch: str
    entity: str


class AddConstraint(Composed):
    op: Literal["add_constraint"]
    sketch: str
    refs: list[str] = Field(default_factory=list)
    value: JsonValue = None
    options: dict[str, JsonValue] | None = None


class DeleteConstraint(Operation):
    op: Literal["delete_constraint"]
    sketch: str
    constraint: str


class SetConstraintValue(Operation):
    op: Literal["set_constraint_value"]
    sketch: str
    constraint: str
    value: JsonValue


class SetConstraintArgument(Operation):
    """A keyword on a constraint statement: `at` (the label's place), `along`, `reverse`, `inside`."""
    op: Literal["set_constraint_argument"]
    sketch: str
    constraint: str
    kwarg: str
    value: JsonValue


class FilletCorners(Operation):
    """Round (or with kind chamfer, bevel) corners at one size: a corner is two line ends that
    meet ({"a": "line1.end", "b": "line2.start"}) or a macro's corner ({"entity": "rect1",
    "corner": "tl"}). The first gets a dimension, the others equal it."""
    op: Literal["fillet_corners"]
    sketch: str
    corners: list[dict[str, str]] = Field(min_length=1)
    size: float = Field(gt=0)
    kind: Literal["fillet", "chamfer"] = "fillet"


class Unfillet(Operation):
    """Remove a fillet or chamfer: a line-pair arc or bevel by its name, a macro corner by its
    arc or chamfer reference (`rect1.tl_arc`)."""
    op: Literal["unfillet"]
    sketch: str
    entity: str


class Trim(Operation):
    """Remove the piece of a line, arc or circle under a point, up to the nearest crossings with
    other curves; the cut ends are related to the curves they were cut at."""
    op: Literal["trim"]
    sketch: str
    entity: str
    at: list[float] = Field(min_length=2, max_length=2)


class ConvertDxf(Operation):
    """Turn a DXF import into lines, arcs and circles where it stands, joined by coincidents;
    relations on its curves move to them, relations on the import as a whole go."""
    op: Literal["convert_dxf"]
    sketch: str
    entity: str


class WriteBack(Operation):
    op: Literal["write_back"]
    sketch: str
    coords: dict[str, dict[str, JsonValue]]
    precision: int = Field(default=4, ge=0, le=15)


class Pose(Operation):
    at: Vector3 | None = None
    rotate: Vector3 | None = None


class WritePoses(Operation):
    op: Literal["write_poses"]
    poses: dict[str, Pose]
    precision: int = Field(default=4, ge=0, le=15)


class SetEntityArgument(Operation):
    op: Literal["set_entity_argument"]
    sketch: str
    entity: str
    kwarg: str
    value: JsonValue


class ReplaceSource(Operation):
    op: Literal["replace_source"]
    source: str


class SolveSketch(Operation):
    op: Literal["solve_sketch"]
    sketch: str
    drag: dict[str, Annotated[list[float], Field(min_length=2, max_length=2)]] | None = None


class ExplodeImport(Operation):
    op: Literal["explode_import"]
    feature: str


class MakeEditable(Operation):
    op: Literal["make_editable"]
    instance: str | None = None
    leaf: str | None = None

    @model_validator(mode="after")
    def target(self):
        if bool(self.instance) == bool(self.leaf):
            raise ValueError("provide exactly one of instance or leaf")
        return self


class Batch(Operation):
    op: Literal["batch"]
    ops: list[EditOperation]
    sketch: str | None = None  # compatibility; affected sketches come from sub-operations

    @model_validator(mode="after")
    def source_operations_only(self):
        if any(isinstance(op, (SolveSketch, ExplodeImport, MakeEditable)) for op in self.ops):
            raise ValueError(
                "solve_sketch, explode_import and make_editable must be separate operations"
            )
        return self


EditOperation = Annotated[
    SetArgument
    | SetParameter
    | AddParameter
    | DeleteParameter
    | SetMeta
    | AddFeature
    | DeleteFeature
    | AddSketchEntity
    | DeleteSketchEntity
    | AddConstraint
    | DeleteConstraint
    | SetConstraintValue
    | SetConstraintArgument
    | FilletCorners
    | Unfillet
    | Trim
    | ConvertDxf
    | WriteBack
    | WritePoses
    | SetEntityArgument
    | ReplaceSource
    | SolveSketch
    | ExplodeImport
    | MakeEditable
    | Batch,
    Field(discriminator="op"),
]
Batch.model_rebuild()
EDIT_ADAPTER = TypeAdapter(EditOperation)
# Preserve the flat HTTP payload ({op, ..., hash}) while keeping hashes out of
# engine operations and nested batches.
EditRequest = Annotated[
    reduce(
        or_,
        (
            create_model(f"{model.__name__}Request", __base__=model, hash=(str | None, None))
            for model in get_args(get_args(EditOperation)[0])
        ),
    ),
    Field(discriminator="op"),
]


def validate_operation(value: object) -> dict:
    """Keep the engine's existing dict interface, with one validation boundary."""
    from .edit import EditError

    try:
        return EDIT_ADAPTER.validate_python(value).model_dump(exclude_unset=True)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'operation'}: {e['msg']}"
            for e in exc.errors(include_input=False)
        )
        raise EditError(f"invalid operation: {details}") from None
