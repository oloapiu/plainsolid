"""The public API that model files use: `from plainsolid import *`.

Every function records a feature into the document currently being built
(see plainsolid.parse). Nothing here evaluates geometry.
"""
from __future__ import annotations

import contextvars
import sys
from typing import Any

from .model import PLANES, Constraint, Document, Entity, Feature
from .selectors import Query, Selector

__all__ = [
    "BACK",
    "BOTTOM",
    "FRONT",
    "ISO",
    "LEFT",
    "RIGHT",
    "TOP",
    "XY",
    "XZ",
    "YZ",
    "X",
    "Y",
    "Z",
    "angle",
    "chamfer",
    "circular_pattern",
    "coincident",
    "concentric",
    "cut",
    "dimension",
    "distance",
    "extrude",
    "fillet",
    "fixed",
    "import_step",
    "instance",
    "linear_pattern",
    "meta",
    "mirror",
    "note",
    "parallel",
    "param",
    "plane",
    "revolve",
    "shell",
    "sketch",
    "view",
]

DOCUMENT_KINDS = ("part", "assembly", "drawing")


class _Builder:
    """Collects DSL calls for one execution of a model file."""

    def __init__(self, filename: str | None = None):
        self.filename = filename
        self.document = Document()
        self.names: set[str] = set()

    def lineno(self) -> int | None:
        """Line in the model file of the DSL call currently executing."""
        frame = sys._getframe(2)
        while frame is not None:
            if self.filename is None or frame.f_code.co_filename == self.filename:
                return frame.f_lineno
            frame = frame.f_back
        return None

    def add_feature(self, feature: Feature) -> Feature:
        if not isinstance(feature.name, str) or not feature.name.isidentifier():
            raise ValueError(f"feature name must be an identifier, got {feature.name!r}")
        if feature.name in self.names:
            raise ValueError(f"duplicate feature name {feature.name!r}")
        self.names.add(feature.name)
        self.document.features.append(feature)
        return feature


_current: contextvars.ContextVar[_Builder | None] = contextvars.ContextVar("plainsolid_builder", default=None)


def _builder() -> _Builder:
    b = _current.get()
    if b is None:
        b = _Builder()
        _current.set(b)
    return b


class Param(float):
    """A float that carries a description for the GUI."""

    description: str

    def __new__(cls, value: float, description: str = ""):
        obj = super().__new__(cls, value)
        obj.description = description
        return obj


class PlaneRef:
    def __init__(self, name: str):
        self.ref_name = name

    def __repr__(self) -> str:
        return self.ref_name


XY = PlaneRef("XY")
XZ = PlaneRef("XZ")
YZ = PlaneRef("YZ")


class AxisRef:
    """A global axis, for revolves, circular patterns and pattern directions."""

    def __init__(self, name: str):
        self.ref_name = name

    def __neg__(self) -> AxisRef:
        return AxisRef(self.ref_name[1:] if self.ref_name.startswith("-") else "-" + self.ref_name)

    def __repr__(self) -> str:
        return self.ref_name


X = AxisRef("X")
Y = AxisRef("Y")
Z = AxisRef("Z")
GLOBAL_AXES = ("X", "Y", "Z", "-X", "-Y", "-Z")


class ViewDirection:
    """Where a drawing view looks from: FRONT looks along +Y, TOP down -Z, RIGHT along -X,
    ISO from (1, -1, 1), all with Z up (Y up for TOP and BOTTOM)."""

    def __init__(self, name: str):
        self.ref_name = name

    def __repr__(self) -> str:
        return self.ref_name.upper()


FRONT = ViewDirection("front")
BACK = ViewDirection("back")
LEFT = ViewDirection("left")
RIGHT = ViewDirection("right")
TOP = ViewDirection("top")
BOTTOM = ViewDirection("bottom")
ISO = ViewDirection("iso")
VIEW_DIRECTIONS = ("front", "back", "left", "right", "top", "bottom", "iso")


class FeatureHandle:
    """What a feature call returns: a reference usable by later features."""

    def __init__(self, feature: Feature):
        self._feature = feature
        self.ref_name = feature.name

    @property
    def name(self) -> str:
        return self._feature.name

    @property
    def faces(self) -> Query:
        return Query(Selector(self._feature.name, "faces"))

    @property
    def edges(self) -> Query:
        return Query(Selector(self._feature.name, "edges"))

    @property
    def vertices(self) -> Query:
        return Query(Selector(self._feature.name, "vertices"))

    def __repr__(self) -> str:
        return f"<{self._feature.kind} {self._feature.name}>"


def _pt(p) -> tuple[float, float]:
    try:
        x, y = p
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected a 2D point (x, y), got {p!r}") from exc
    return (float(x), float(y))


def _ref(value: Any, what: str) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, SketchHandle):
        raise TypeError(f"{what}: name the entity as a string, for example \"line1\" or \"line1.start\"")
    raise TypeError(f"{what}: expected an entity reference string, got {value!r}")


class SketchHandle(FeatureHandle):
    """Returned by sketch(); entity and constraint methods append to the sketch.
    Dimension values are readable as attributes: `s.width` after `s.length("width", ...)`."""

    # --- entities -----------------------------------------------------------

    def _add(self, kind: str, name: str, args: dict[str, Any], construction: bool = False) -> SketchHandle:
        b = _builder()
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError(f"entity name must be an identifier, got {name!r}")
        if self._feature.entity(name) or self._feature.constraint(name):
            raise ValueError(f"duplicate name {name!r} in sketch {self._feature.name!r}")
        self._feature.entities.append(
            Entity(name=name, kind=kind, args=args, construction=construction, line=b.lineno())
        )
        return self

    def point(self, name: str, at) -> SketchHandle:
        return self._add("point", name, {"at": _pt(at)}, True)

    def line(self, name: str, start, end, *, construction: bool = False) -> SketchHandle:
        return self._add("line", name, {"start": _pt(start), "end": _pt(end)}, construction)

    def circle(self, name: str, diameter: float, *, at=(0.0, 0.0), construction: bool = False) -> SketchHandle:
        return self._add("circle", name, {"diameter": float(diameter), "at": _pt(at)}, construction)

    def arc(self, name: str, center, start, end, *, construction: bool = False) -> SketchHandle:
        """Counter-clockwise arc from start to end about center."""
        return self._add("arc", name, {"center": _pt(center), "start": _pt(start), "end": _pt(end)}, construction)

    def rect(self, name: str, width: float, height: float, *, at=(0.0, 0.0), construction: bool = False) -> SketchHandle:
        return self._add("rect", name, {"width": float(width), "height": float(height), "at": _pt(at)}, construction)

    def slot(self, name: str, length: float, width: float, *, at=(0.0, 0.0), angle: float = 0.0,
             construction: bool = False) -> SketchHandle:
        return self._add("slot", name, {"length": float(length), "width": float(width), "at": _pt(at),
                                        "angle": float(angle)}, construction)

    def polygon(self, name: str, points, *, construction: bool = False) -> SketchHandle:
        pts = [_pt(p) for p in points]
        if len(pts) < 3:
            raise ValueError("polygon needs at least three points")
        return self._add("polygon", name, {"points": pts}, construction)

    def offset(self, name: str, of, distance: float, *, side: str = "outside", corners: str = "sharp",
               construction: bool = False) -> SketchHandle:
        """Curves at a distance from other curves of this sketch (lines, arcs, circles,
        the sides of a rect or polygon, converted body edges, earlier offsets), named
        as strings. A closed loop offsets "outside" or "inside"; an open chain "left"
        or "right" of its direction. corners="round" joins segments with arcs. The
        result follows its sources and is profile geometry unless construction."""
        refs = [of] if isinstance(of, str) else list(of)
        what = f"offset {name!r}"
        if not refs or not all(isinstance(r, str) and r for r in refs):
            raise ValueError(f"{what}: name the curves to offset as strings, for example [\"line1\", \"arc1\"]")
        if float(distance) <= 0:
            raise ValueError(f"{what}: distance must be positive; choose the side instead")
        if side not in ("outside", "inside", "left", "right"):
            raise ValueError(f"{what}: side must be outside, inside, left or right, got {side!r}")
        if corners not in ("sharp", "round"):
            raise ValueError(f"{what}: corners must be sharp or round, got {corners!r}")
        return self._add("offset", name, {"of": refs, "distance": float(distance), "side": side, "corners": corners}, construction)

    def project(self, name: str, selector: Query, *, construction: bool = True) -> SketchHandle:
        """Body geometry brought into the sketch as a fixed entity (an edge, a vertex
        or a face outline). Construction by default; construction=False makes it
        part of the profile."""
        if not isinstance(selector, Query):
            raise TypeError(f"project {name!r}: expected a selector such as body.edges.nearest((x, y, z)), got {selector!r}")
        return self._add("project", name, {"selector": selector.selector.to_json()}, construction)

    # --- constraints --------------------------------------------------------

    def _constrain(self, kind: str, name: str, refs: list[Any], value: float | None = None,
                   **options: Any) -> SketchHandle:
        b = _builder()
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError(f"constraint name must be an identifier, got {name!r}")
        if self._feature.entity(name) or self._feature.constraint(name):
            raise ValueError(f"duplicate name {name!r} in sketch {self._feature.name!r}")
        clean = [_ref(r, f"{kind} {name!r}") for r in refs if r is not None]
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{kind} {name!r}: value must be a number, got {value!r}") from None
        self._feature.constraints.append(
            Constraint(name=name, kind=kind, refs=clean, value=value, options=options, line=b.lineno())
        )
        return self

    def coincident(self, name: str, a, b) -> SketchHandle:
        return self._constrain("coincident", name, [a, b])

    def horizontal(self, name: str, a, b=None) -> SketchHandle:
        return self._constrain("horizontal", name, [a, b])

    def vertical(self, name: str, a, b=None) -> SketchHandle:
        return self._constrain("vertical", name, [a, b])

    def parallel(self, name: str, a, b) -> SketchHandle:
        return self._constrain("parallel", name, [a, b])

    def perpendicular(self, name: str, a, b) -> SketchHandle:
        return self._constrain("perpendicular", name, [a, b])

    def equal(self, name: str, a, b) -> SketchHandle:
        return self._constrain("equal", name, [a, b])

    def tangent(self, name: str, a, b, *, inside: bool = False) -> SketchHandle:
        return self._constrain("tangent", name, [a, b], inside=inside) if inside else self._constrain("tangent", name, [a, b])

    def concentric(self, name: str, a, b) -> SketchHandle:
        return self._constrain("concentric", name, [a, b])

    def coradial(self, name: str, a, b) -> SketchHandle:
        return self._constrain("coradial", name, [a, b])

    def colinear(self, name: str, a, b) -> SketchHandle:
        return self._constrain("colinear", name, [a, b])

    def symmetric(self, name: str, a, b, about) -> SketchHandle:
        return self._constrain("symmetric", name, [a, b, about])

    def midpoint(self, name: str, point, line) -> SketchHandle:
        return self._constrain("midpoint", name, [point, line])

    def on(self, name: str, point, curve) -> SketchHandle:
        return self._constrain("on", name, [point, curve])

    def fix(self, name: str, target) -> SketchHandle:
        return self._constrain("fix", name, [target])

    # --- dimensions ---------------------------------------------------------

    def distance(self, name: str, a, b, value: float, *, along: str | None = None) -> SketchHandle:
        if along not in (None, "x", "y"):
            raise ValueError(f"distance {name!r}: along must be 'x' or 'y'")
        return self._constrain("distance", name, [a, b], value, **({"along": along} if along else {}))

    def length(self, name: str, line, value: float) -> SketchHandle:
        return self._constrain("length", name, [line], value)

    def diameter(self, name: str, circle, value: float) -> SketchHandle:
        return self._constrain("diameter", name, [circle], value)

    def radius(self, name: str, circle, value: float) -> SketchHandle:
        return self._constrain("radius", name, [circle], value)

    def angle(self, name: str, a, b, value: float) -> SketchHandle:
        return self._constrain("angle", name, [a, b], value)

    def __getattr__(self, item: str) -> float:
        # dimension values by name: s.width
        if item.startswith("_"):
            raise AttributeError(item)
        feature = self.__dict__.get("_feature")
        if feature is not None:
            c = feature.constraint(item)
            if c is not None and c.value is not None:
                return c.value
        raise AttributeError(f"sketch {feature.name if feature else '?'!r} has no dimension {item!r}")


# ---------------------------------------------------------------------------
# public DSL


def meta(**fields: Any) -> None:
    """Document metadata: name, material, revision, author, plus free-form extras.

    `kind="assembly"` declares an assembly document; the default is a part."""
    b = _builder()
    kind = fields.pop("kind", None)
    if kind is not None:
        if kind not in DOCUMENT_KINDS:
            raise ValueError(f"meta(kind=...) must be one of {DOCUMENT_KINDS}, got {kind!r}")
        b.document.kind = kind
    b.document.meta.update(fields)


def param(value: float, description: str = "") -> Param:
    return Param(value, description)


def _plane_ref(value: Any, what: str) -> Any:
    """A plane reference as stored in feature args: a standard plane name, a
    {"plane": name} for a plane feature, or {"selector": ...} for a body face."""
    if isinstance(value, PlaneRef):
        return value.ref_name
    if isinstance(value, str):
        if value in PLANES:
            return value
        raise ValueError(f"{what}: unknown plane {value!r}, expected XY, XZ, YZ, a plane feature or a face selector")
    if isinstance(value, FeatureHandle):
        if value._feature.kind != "plane":
            raise TypeError(f"{what}: {value.name!r} is a {value._feature.kind}, not a plane")
        return {"plane": value.name}
    if isinstance(value, Query):
        if value.selector.kind != "faces":
            raise TypeError(f"{what}: a sketch plane needs a face selector, got {value.ref_name}")
        return {"selector": value.selector.to_json()}
    raise TypeError(f"{what}: expected XY, XZ, YZ, a plane feature or a face selector, got {value!r}")


def sketch(name: str, on: Any = XY, *, offset: float = 0.0, flip: bool = False,
           suppressed: bool = False) -> SketchHandle:
    """A sketch on a standard plane, a plane feature or a planar body face
    (`on=body.faces.top`, `on=body.faces.nearest((x, y, z))`), optionally offset along its normal."""
    b = _builder()
    f = Feature(name=name, kind="sketch", args={
        "on": _plane_ref(on, f"sketch {name!r}"), "offset": float(offset), "flip": bool(flip),
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return SketchHandle(f)


def plane(name: str, base: Any = None, *, offset: float = 0.0, angle: float = 0.0, about: Query | None = None,
          between: tuple | None = None, through: tuple | None = None, flip: bool = False,
          suppressed: bool = False) -> FeatureHandle:
    """A reference plane. Forms:
    plane("p", XY, offset=10)                      offset from a plane or face
    plane("p", body.faces.top, offset=5)
    plane("p", XY, angle=30, about=body.edges.nearest(...))   rotated about an edge lying on the base
    plane("p", between=(faceA, faceB))             midplane between parallel faces or planes
    plane("p", through=(v1, v2, v3))               through three vertices or points
    """
    b = _builder()
    what = f"plane {name!r}"
    args: dict[str, Any] = {"offset": float(offset), "angle": float(angle), "flip": bool(flip)}
    forms = sum(x is not None for x in (base, between, through))
    if forms != 1:
        raise ValueError(f"{what}: give exactly one of a base plane/face, between=, or through=")
    if base is not None:
        args["base"] = _plane_ref(base, what)
        if angle:
            if not isinstance(about, Query) or about.selector.kind != "edges":
                raise ValueError(f"{what}: angle= needs about=<edge selector>")
            args["about"] = about.selector.to_json()
    if between is not None:
        if len(between) != 2:
            raise ValueError(f"{what}: between= needs two planes or faces")
        args["between"] = [_plane_ref(v, what) for v in between]
    if through is not None:
        if len(through) != 3:
            raise ValueError(f"{what}: through= needs three vertices or points")
        pts = []
        for v in through:
            if isinstance(v, Query):
                if v.selector.kind != "vertices":
                    raise TypeError(f"{what}: through= takes vertex selectors or (x, y, z) points")
                pts.append({"selector": v.selector.to_json()})
            else:
                try:
                    x, y, z = (float(c) for c in v)
                except (TypeError, ValueError):
                    raise TypeError(f"{what}: through= takes vertex selectors or (x, y, z) points") from None
                pts.append([x, y, z])
        args["through"] = pts
    f = Feature(name=name, kind="plane", args=args, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def _selector_arg(value: Any, kind: str, what: str, optional: bool = False) -> dict[str, Any] | list[dict[str, Any]] | None:
    """A face or edge selector as stored in feature args: {"selector": ...}, or a
    list of them when the file gives several (`[body.edges.top, boss.edges.bottom]`)."""
    if value is None and optional:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError(f"{what}: the list of {kind} is empty")
        return [_selector_arg(v, kind, what) for v in value]
    if not isinstance(value, Query):
        raise TypeError(f"{what}: expected a {kind[:-1]} selector such as body.{kind}.top, got {value!r}")
    if value.selector.kind != kind:
        raise TypeError(f"{what}: expected {kind}, got {value.ref_name}")
    return {"selector": value.selector.to_json()}


def _upto_arg(name: str, kind: str, depth: Any, upto: Any, through: bool, symmetric: bool) -> dict[str, Any]:
    what = f"{kind} {name!r}"
    if upto is not None:
        if depth is not None or through:
            raise ValueError(f"{what}: upto= replaces the depth and through=")
        if symmetric:
            raise ValueError(f"{what}: upto= cannot be symmetric")
        return {"depth": None, "upto": _selector_arg(upto, "faces", what)}
    if depth is None and not through:
        raise ValueError(f"{what}: give a depth, through=True or upto=<face selector>")
    return {"depth": None if depth is None else float(depth)}


def extrude(name: str, sketch: SketchHandle, depth: float | None = None, *, symmetric: bool = False,
            flip: bool = False, draft: float = 0.0, op: str = "add", upto: Query | None = None,
            through: bool = False, suppressed: bool = False) -> FeatureHandle:
    """Extrude a sketch by a depth, up to a planar face (`upto=body.faces.top`) or
    through everything (`through=True`, cuts only). `op="cut"` removes material."""
    b = _builder()
    if op not in ("add", "cut"):
        raise ValueError(f"extrude {name!r}: op must be 'add' or 'cut', got {op!r}")
    if through and op != "cut":
        raise ValueError(f"extrude {name!r}: through=True only makes sense with op=\"cut\"; give a depth or upto=")
    args: dict[str, Any] = {
        "sketch": _sketch_name(name, sketch), "symmetric": bool(symmetric), "flip": bool(flip),
        "draft": float(draft), "op": op, "through": bool(through),
    }
    args.update(_upto_arg(name, "extrude", depth, upto, through, symmetric))
    f = Feature(name=name, kind="extrude", args=args, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def cut(name: str, sketch: SketchHandle, depth: float | None = None, *, through: bool = False,
        symmetric: bool = False, flip: bool = False, upto: Query | None = None,
        suppressed: bool = False) -> FeatureHandle:
    b = _builder()
    args: dict[str, Any] = {
        "sketch": _sketch_name(name, sketch), "through": bool(through), "symmetric": bool(symmetric),
        "flip": bool(flip),
    }
    args.update(_upto_arg(name, "cut", depth, upto, through, symmetric))
    f = Feature(name=name, kind="cut", args=args, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def _axis_arg(value: Any, what: str, sketch_lines: bool) -> Any:
    """An axis as stored in feature args: a global axis name, a sketch line name
    (revolve only) or an edge / cylindrical face selector."""
    if isinstance(value, AxisRef):
        return value.ref_name
    if isinstance(value, str) and value:
        if value in GLOBAL_AXES or sketch_lines:
            return value
        raise ValueError(f"{what}: unknown axis {value!r}; use X, Y, Z or an edge selector")
    if isinstance(value, Query):
        if value.selector.kind not in ("edges", "faces"):
            raise TypeError(f"{what}: an axis is a straight edge or a cylindrical face, got {value.ref_name}")
        return {"selector": value.selector.to_json()}
    hint = "a sketch line name, X, Y, Z or an edge selector" if sketch_lines else "X, Y, Z or an edge selector"
    raise TypeError(f"{what}: axis must be {hint}, got {value!r}")


def _direction_arg(value: Any, what: str) -> Any:
    if isinstance(value, AxisRef):
        return value.ref_name
    if isinstance(value, str):
        key = value.upper().replace("+", "")
        if key in GLOBAL_AXES:
            return key
        raise ValueError(f"{what}: unknown direction {value!r}; use X, Y, Z, -X, -Y, -Z, a tuple or an edge selector")
    if isinstance(value, Query):
        if value.selector.kind != "edges":
            raise TypeError(f"{what}: a direction from the body is a straight edge, got {value.ref_name}")
        return {"selector": value.selector.to_json()}
    try:
        x, y, z = (float(c) for c in value)
    except (TypeError, ValueError):
        raise TypeError(f"{what}: direction must be X, Y, Z, a (dx, dy, dz) tuple or an edge selector, got {value!r}") from None
    return [x, y, z]


def revolve(name: str, sketch: SketchHandle, axis: Any, *, angle: float = 360.0, op: str = "add",
            suppressed: bool = False) -> FeatureHandle:
    """Revolve a sketch about an axis lying in its plane: a construction line of the
    sketch by name (`axis="centerline"`) or a global axis (`axis=Z`)."""
    b = _builder()
    what = f"revolve {name!r}"
    if op not in ("add", "cut"):
        raise ValueError(f"{what}: op must be 'add' or 'cut', got {op!r}")
    angle = float(angle)
    if not 0 < angle <= 360:
        raise ValueError(f"{what}: angle must be between 0 and 360 degrees, got {angle}")
    f = Feature(name=name, kind="revolve", args={
        "sketch": _sketch_name(name, sketch), "axis": _axis_arg(axis, what, sketch_lines=True),
        "angle": angle, "op": op,
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def fillet(name: str, edges: Query, radius: float, *, suppressed: bool = False) -> FeatureHandle:
    """Round the selected edges (`body.edges.top`, `hole.edges.all()`, ...) with one radius."""
    b = _builder()
    what = f"fillet {name!r}"
    if float(radius) <= 0:
        raise ValueError(f"{what}: radius must be positive")
    f = Feature(name=name, kind="fillet", args={
        "edges": _selector_arg(edges, "edges", what), "radius": float(radius),
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def chamfer(name: str, edges: Query, distance: float, *, distance2: float | None = None,
            suppressed: bool = False) -> FeatureHandle:
    """Bevel the selected edges by one distance, or two (distance on the first
    adjacent face, distance2 on the other)."""
    b = _builder()
    what = f"chamfer {name!r}"
    if float(distance) <= 0 or (distance2 is not None and float(distance2) <= 0):
        raise ValueError(f"{what}: distances must be positive")
    f = Feature(name=name, kind="chamfer", args={
        "edges": _selector_arg(edges, "edges", what), "distance": float(distance),
        "distance2": None if distance2 is None else float(distance2),
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def shell(name: str, faces: Query | None, thickness: float, *, outward: bool = False,
          suppressed: bool = False) -> FeatureHandle:
    """Hollow the body to a wall thickness, removing the selected faces (`None`
    keeps it closed). Walls grow inward unless outward=True."""
    b = _builder()
    what = f"shell {name!r}"
    if float(thickness) <= 0:
        raise ValueError(f"{what}: thickness must be positive")
    f = Feature(name=name, kind="shell", args={
        "faces": _selector_arg(faces, "faces", what, optional=True), "thickness": float(thickness),
        "outward": bool(outward),
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def _feature_list(value: Any, what: str, optional: bool = False) -> list[str]:
    if value is None:
        if optional:
            return []
        raise ValueError(f"{what}: name the feature to repeat")
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    names = []
    for v in items:
        if isinstance(v, FeatureHandle):
            names.append(v.name)
        elif isinstance(v, str) and v:
            names.append(v)
        else:
            raise TypeError(f"{what}: expected a feature (extrude, cut or revolve), got {v!r}")
    return names


def linear_pattern(name: str, feature: Any, count: int, *, spacing: float, direction: Any = X,
                   count2: int | None = None, spacing2: float | None = None, direction2: Any = Y,
                   suppressed: bool = False) -> FeatureHandle:
    """Repeat an extrude, cut or revolve `count` times `spacing` apart along a
    direction, optionally in a second direction too."""
    b = _builder()
    what = f"linear_pattern {name!r}"
    if int(count) < 1 or (count2 is not None and int(count2) < 1):
        raise ValueError(f"{what}: counts must be at least 1")
    if count2 is not None and spacing2 is None:
        raise ValueError(f"{what}: count2= needs spacing2=")
    f = Feature(name=name, kind="linear_pattern", args={
        "features": _feature_list(feature, what), "count": int(count), "spacing": float(spacing),
        "direction": _direction_arg(direction, what),
        "count2": None if count2 is None else int(count2), "spacing2": None if spacing2 is None else float(spacing2),
        "direction2": _direction_arg(direction2, what),
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def circular_pattern(name: str, feature: Any, count: int, *, axis: Any = Z, angle: float = 360.0,
                     suppressed: bool = False) -> FeatureHandle:
    """Repeat an extrude, cut or revolve `count` times about an axis, spread over
    `angle` degrees (360 fills the circle evenly)."""
    b = _builder()
    what = f"circular_pattern {name!r}"
    if int(count) < 1:
        raise ValueError(f"{what}: count must be at least 1")
    f = Feature(name=name, kind="circular_pattern", args={
        "features": _feature_list(feature, what), "count": int(count),
        "axis": _axis_arg(axis, what, sketch_lines=False), "angle": float(angle),
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def mirror(name: str, feature: Any = None, *, about: Any = YZ, suppressed: bool = False) -> FeatureHandle:
    """Mirror an extrude, cut or revolve (or the whole body when no feature is
    given) across a standard plane, a plane feature or a planar face."""
    b = _builder()
    what = f"mirror {name!r}"
    f = Feature(name=name, kind="mirror", args={
        "features": _feature_list(feature, what, optional=True), "about": _plane_ref(about, what),
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def import_step(name: str, path: str, *, tolerance: float | None = None, suppressed: bool = False) -> FeatureHandle:
    """A body (part document) or an instance tree (assembly document) read from a STEP file.
    The path is relative to the model file."""
    b = _builder()
    if not isinstance(path, str) or not path:
        raise ValueError(f"import_step {name!r}: path must be a non-empty string")
    f = Feature(name=name, kind="import_step", args={
        "path": path, "tolerance": None if tolerance is None else float(tolerance),
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


# ---------------------------------------------------------------------------
# assemblies


class InstanceHandle(FeatureHandle):
    """What instance() returns: faces, edges and vertices of the part in its own
    coordinates, plus its standard planes and axes as mate references."""

    @property
    def planes(self) -> Query:
        return Query(Selector(self._feature.name, "planes"))

    @property
    def axes(self) -> Query:
        return Query(Selector(self._feature.name, "axes"))


def _xyz(value: Any, what: str) -> list[float]:
    try:
        x, y, z = (float(c) for c in value)
    except (TypeError, ValueError):
        raise TypeError(f"{what}: expected (x, y, z), got {value!r}") from None
    return [x, y, z]


def instance(name: str, path: str, *, at=(0.0, 0.0, 0.0), rotate=(0.0, 0.0, 0.0), color: Any = None,
             material: str | None = None, density: float | None = None, tolerance: float | None = None,
             suppressed: bool = False) -> InstanceHandle:
    """An instance of a part file or a vendor STEP file (path relative to this file)
    at a pose: `at` is the translation and `rotate` the rotation in degrees about
    X, then Y, then Z. The pose is where the solver starts and what it writes
    back. `color` overrides the part's colour (`meta(color="#c0c0c0")`), `material`
    or `density` (g/cm^3) give vendor parts a mass."""
    b = _builder()
    what = f"instance {name!r}"
    if not isinstance(path, str) or not path:
        raise ValueError(f"{what}: path must be a non-empty string")
    if color is not None and not isinstance(color, (str, list, tuple)):
        raise TypeError(f"{what}: color must be \"#rrggbb\" or (r, g, b), got {color!r}")
    if material is not None and not isinstance(material, str):
        raise TypeError(f"{what}: material must be a name such as \"al6061\", got {material!r}")
    f = Feature(name=name, kind="instance", args={
        "path": path, "at": _xyz(at, what), "rotate": _xyz(rotate, what),
        "color": list(color) if isinstance(color, (list, tuple)) else color, "material": material,
        "density": None if density is None else float(density),
        "tolerance": None if tolerance is None else float(tolerance),
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return InstanceHandle(f)


def _mate_ref(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Query):
        raise TypeError(f"{what}: expected a reference on an instance such as box.faces.top, box.planes.XY "
                        f"or gland.axes.Z, got {value!r}")
    return {"selector": value.selector.to_json()}


def _mate(kind: str, name: str, a: Any, b: Any, value: float | None = None, flip: bool = False,
          suppressed: bool = False) -> FeatureHandle:
    bd = _builder()
    what = f"{kind} {name!r}"
    args: dict[str, Any] = {"a": _mate_ref(a, what), "b": _mate_ref(b, what), "flip": bool(flip)}
    if value is not None:
        try:
            args["value"] = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{what}: value must be a number, got {value!r}") from None
    f = Feature(name=name, kind=kind, args=args, span=_span(bd), suppressed=bool(suppressed))
    bd.add_feature(f)
    return FeatureHandle(f)


def fixed(name: str, instance: Any, *, suppressed: bool = False) -> FeatureHandle:
    """Anchor an instance at the pose written in its statement."""
    b = _builder()
    what = f"fixed {name!r}"
    if isinstance(instance, InstanceHandle):
        target = instance.name
    elif isinstance(instance, str) and instance:
        target = instance
    else:
        raise TypeError(f"{what}: expected an instance, got {instance!r}")
    f = Feature(name=name, kind="fixed", args={"instance": target}, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def coincident(name: str, a: Any, b: Any, *, flip: bool = False, suppressed: bool = False) -> FeatureHandle:
    """Two planar faces or planes coplanar (body faces face each other unless flip=True),
    two points together, two axes colinear, a point on a plane or axis, an axis in a plane."""
    return _mate("coincident", name, a, b, None, flip, suppressed)


def concentric(name: str, a: Any, b: Any, *, suppressed: bool = False) -> FeatureHandle:
    """Two cylindrical faces, circular edges or axes on one axis."""
    return _mate("concentric", name, a, b, None, False, suppressed)


def distance(name: str, a: Any, b: Any, value: float, *, flip: bool = False, suppressed: bool = False) -> FeatureHandle:
    """Like coincident, at a distance: `b` lies `value` along `a`'s normal (planes),
    the signed distance of a point or axis from a plane, or the distance between
    two points or two parallel axes."""
    return _mate("distance", name, a, b, value, flip, suppressed)


def parallel(name: str, a: Any, b: Any, *, suppressed: bool = False) -> FeatureHandle:
    """Two planes, two axes, or an axis and a plane, parallel."""
    return _mate("parallel", name, a, b, None, False, suppressed)


def angle(name: str, a: Any, b: Any, value: float, *, flip: bool = False, suppressed: bool = False) -> FeatureHandle:
    """Two planes or axes at an angle in degrees (0 is the coincident orientation)."""
    return _mate("angle", name, a, b, value, flip, suppressed)


# ---------------------------------------------------------------------------
# drawings


class ViewHandle(FeatureHandle):
    """What view() returns: the model's faces, edges and vertices as seen in this view,
    for dimensions (`front.edges.of("body").from_sketch("outer_wall")`)."""


def _direction_name(value: Any, what: str) -> str:
    if isinstance(value, ViewDirection):
        return value.ref_name
    if isinstance(value, str) and value.lower() in VIEW_DIRECTIONS:
        return value.lower()
    raise ValueError(f"{what}: direction must be FRONT, BACK, LEFT, RIGHT, TOP, BOTTOM or ISO, got {value!r}")


def _sheet_xy(value: Any, what: str) -> list[float]:
    try:
        x, y = (float(c) for c in value)
    except (TypeError, ValueError):
        raise TypeError(f"{what}: at= must be (x, y) in sheet mm, got {value!r}") from None
    return [x, y]


def view(name: str, direction: Any = FRONT, *, at=(0.0, 0.0), scale: float | None = None, hidden: bool | None = None,
         section: Any = None, offset: float = 0.0, flip: bool = False, suppressed: bool = False) -> ViewHandle:
    """A view of the drawing's model, its centre at `at` on the sheet (mm from the
    bottom-left corner). `scale` defaults to the sheet's. `hidden` draws hidden lines
    dashed (on by default, off for sections). `section=YZ` (a standard plane) or
    `section="mid"` (a plane feature of the model), with `offset`, cuts the model and
    looks at the cut face from the side that was removed; `flip` keeps the other side."""
    b = _builder()
    what = f"view {name!r}"
    args: dict[str, Any] = {"at": _sheet_xy(at, what)}
    if section is not None:
        if isinstance(section, PlaneRef):
            args["section"] = section.ref_name
        elif isinstance(section, str) and section:
            args["section"] = section
        else:
            raise TypeError(f"{what}: section= takes XY, XZ, YZ or the name of a plane feature of the model, got {section!r}")
        args["offset"] = float(offset)
        args["flip"] = bool(flip)
    else:
        args["direction"] = _direction_name(direction, what)
    if scale is not None:
        args["scale"] = float(scale)
    if hidden is not None:
        args["hidden"] = bool(hidden)
    f = Feature(name=name, kind="view", args=args, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return ViewHandle(f)


def dimension(name: str, a: Query, b: Query | None = None, *, at=(0.0, 0.0), kind: str | None = None,
              along: str | None = None, text: str | None = None, suppressed: bool = False) -> FeatureHandle:
    """A dimension in a view, placed at `at` relative to the view's centre. One reference
    (a circular edge or a round face) gives a diameter, or a radius with kind="radius";
    two give a distance (points, parallel lines, a point and a line, circle centres;
    along="x" or "y" measures along a sheet axis) or, with kind="angle", the angle
    between two lines. `text` replaces the measured value."""
    bd = _builder()
    what = f"dimension {name!r}"
    if not isinstance(a, Query) or a.selector.kind not in ("faces", "edges", "vertices"):
        raise TypeError(f"{what}: expected a reference through a view such as front.edges.of(\"body\").top, got {a!r}")
    if b is not None and (not isinstance(b, Query) or b.selector.kind not in ("faces", "edges", "vertices")):
        raise TypeError(f"{what}: expected a second reference through a view, got {b!r}")
    if kind is not None and kind not in ("distance", "diameter", "radius", "angle"):
        raise ValueError(f"{what}: kind must be distance, diameter, radius or angle, got {kind!r}")
    if along is not None and along not in ("x", "y"):
        raise ValueError(f"{what}: along must be \"x\" or \"y\", got {along!r}")
    f = Feature(name=name, kind="dimension", args={
        "a": {"selector": a.selector.to_json()}, "b": {"selector": b.selector.to_json()} if b is not None else None,
        "at": _sheet_xy(at, what), "kind": kind, "along": along, "text": None if text is None else str(text),
    }, span=_span(bd), suppressed=bool(suppressed))
    bd.add_feature(f)
    return FeatureHandle(f)


def note(name: str, text: str, *, at=(0.0, 0.0), size: float = 3.5, view: Any = None,
         suppressed: bool = False) -> FeatureHandle:
    """Text on the sheet at `at` (mm from the bottom-left corner), or relative to a
    view's centre with `view=front`. Lines break at newlines; `size` is the text height."""
    b = _builder()
    what = f"note {name!r}"
    if not isinstance(text, str):
        raise TypeError(f"{what}: the text must be a string, got {text!r}")
    view_name = None
    if view is not None:
        if isinstance(view, ViewHandle):
            view_name = view.name
        elif isinstance(view, str) and view:
            view_name = view
        else:
            raise TypeError(f"{what}: view= takes a view of this drawing, got {view!r}")
    f = Feature(name=name, kind="note", args={
        "text": text, "at": _sheet_xy(at, what), "size": float(size), "view": view_name,
    }, span=_span(b), suppressed=bool(suppressed))
    b.add_feature(f)
    return FeatureHandle(f)


def _sketch_name(feature: str, sketch: Any) -> str:
    if isinstance(sketch, SketchHandle):
        return sketch.name
    if isinstance(sketch, str):
        return sketch
    raise ValueError(f"{feature!r}: expected a sketch, got {sketch!r}")


def _span(b: _Builder) -> tuple[int, int] | None:
    ln = b.lineno()
    return (ln, ln) if ln is not None else None
