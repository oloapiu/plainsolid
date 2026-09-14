"""Geometry references written into the file: a chain over the faces, edges
or vertices of a feature.

Two families. Semantic: `.top`, `.bottom`, `.start`, `.end`, `.inner` and
`.from_sketch("rect1.top")` pick by the labels the identity map gave the
faces and edges a feature created (evaluate.py maintains it). Geometric:
`.where(normal=..., kind=..., parallel_to=...)`, `.nearest(point)`,
`.largest()`, `.smallest()`, `.all()` pick by shape. Integer indexing is
never offered, so the file cannot contain a fragile raw index.

On an assembly instance the same chains apply in the part's own coordinates,
`.of("boss")` narrows to what one feature of the part made, and
`inst.planes.XY` / `inst.axes.Z` name the part's standard planes and axes as
mate references."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from build123d import Axis, Shape, Vector

AXES = {"+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0), "-Y": (0, -1, 0), "+Z": (0, 0, 1), "-Z": (0, 0, -1),
        "X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}
# labels the identity map hands out besides sketch entity names; stored as ":top" so
# that a sketch entity called "top" cannot collide with them
LABELS = ("top", "bottom", "start", "end", "inner")
PLANE_NAMES = ("XY", "XZ", "YZ")
AXIS_NAMES = ("X", "Y", "Z")


class SelectorError(ValueError):
    pass


@dataclass
class Identity:
    """Owner feature and semantic tags per face and edge, keyed by shape hash."""

    owner: dict[int, str] = field(default_factory=dict)
    tags: dict[int, tuple[str, ...]] = field(default_factory=dict)
    groups: dict[int, str] = field(default_factory=dict)  # instances: the part feature that made each entity


@dataclass(frozen=True)
class Selector:
    feature: str
    kind: str  # faces | edges | vertices
    ops: tuple[tuple[str, Any], ...] = ()

    @property
    def ref_name(self) -> str:
        text = f"{self.feature}.{self.kind}"
        for op, arg in self.ops:
            if op == "where":
                inner = ", ".join(f"{k}={_fmt(v)}" for k, v in arg.items())
                text += f".where({inner})"
            elif op == "tag":
                text += f".{arg[1:]}" if arg.startswith(":") else f".from_sketch({_fmt(arg)})"
            elif op == "named":
                text += f".{arg}"
            elif arg is None:
                text += f".{op}()"
            else:
                text += f".{op}({_fmt(arg)})"
        return text

    @property
    def semantic(self) -> bool:
        return any(op == "tag" for op, _ in self.ops)

    def to_json(self) -> dict[str, Any]:
        return {"feature": self.feature, "kind": self.kind, "ops": [[op, arg] for op, arg in self.ops]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Selector:
        return cls(data["feature"], data["kind"], tuple((op, arg if not isinstance(arg, list) or op == "where" else tuple(arg)) for op, arg in data.get("ops", [])))

    def __repr__(self) -> str:
        return self.ref_name


def _fmt(v: Any) -> str:
    if isinstance(v, (tuple, list)):
        return "(" + ", ".join(_fmt(c) for c in v) + ")"
    if isinstance(v, str):
        return repr(v)
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return repr(v)


class Query:
    """What `extrude1.edges` returns in a model file; every method returns a new Query."""

    def __init__(self, selector: Selector):
        self.selector = selector

    @property
    def ref_name(self) -> str:
        return self.selector.ref_name

    def _with(self, op: str, arg: Any) -> Query:
        return Query(Selector(self.selector.feature, self.selector.kind, (*self.selector.ops, (op, arg))))

    def nearest(self, point) -> Query:
        try:
            x, y, z = (float(c) for c in point)
        except (TypeError, ValueError):
            raise SelectorError(f"nearest() needs a 3D point, got {point!r}") from None
        return self._with("nearest", (x, y, z))

    def where(self, **kw: Any) -> Query:
        clean: dict[str, Any] = {}
        for k, v in kw.items():
            if k not in ("normal", "kind", "parallel_to"):
                raise SelectorError(f"where() does not know {k!r}; use normal=, kind= or parallel_to=")
            clean[k] = _direction(v) if k in ("normal", "parallel_to") else str(v).lower()
        return self._with("where", clean)

    def largest(self) -> Query:
        return self._with("largest", None)

    def smallest(self) -> Query:
        return self._with("smallest", None)

    def all(self) -> Query:
        return self._with("all", None)

    def of(self, feature: str) -> Query:
        """On an instance: the faces or edges one feature of the part made (`lid.faces.of("plate").top`)."""
        if not isinstance(feature, str) or not feature:
            raise SelectorError(f"of() needs a feature name of the part, got {feature!r}")
        if self.selector.kind in ("planes", "axes"):
            raise SelectorError("of() applies to faces, edges and vertices")
        return self._with("of", feature)

    def from_sketch(self, ref: str) -> Query:
        """Faces or edges that came from a sketch entity: "rect1.top", "hole1", "poly.e2"."""
        if not isinstance(ref, str) or not ref or ref.startswith(":"):
            raise SelectorError(f"from_sketch() needs an entity reference such as \"rect1.top\", got {ref!r}")
        if self.selector.kind == "vertices":
            raise SelectorError("vertices carry no sketch labels; use nearest()")
        return self._with("tag", ref)

    def __getattr__(self, item: str) -> Query:
        if self.selector.kind == "planes":
            if item in PLANE_NAMES:
                return self._with("named", item)
            raise SelectorError(f"planes are XY, XZ or YZ, not {item!r}")
        if self.selector.kind == "axes":
            if item in AXIS_NAMES:
                return self._with("named", item)
            raise SelectorError(f"axes are X, Y or Z, not {item!r}")
        if item in LABELS:
            if self.selector.kind == "vertices":
                raise SelectorError(f"vertices carry no labels; use nearest() instead of .{item}")
            return self._with("tag", ":" + item)
        raise AttributeError(item)

    def __repr__(self) -> str:
        return self.ref_name

    def __getitem__(self, _):
        raise SelectorError("indexing a selector is not allowed: use nearest(), where(), largest() or smallest()")

    def __iter__(self):
        raise SelectorError("a selector cannot be iterated; pass it whole, or use .all()")


def _direction(v: Any):
    if isinstance(v, str):
        key = v.upper()
        if key not in AXES:
            raise SelectorError(f"unknown direction {v!r}; use +X, -X, +Y, -Y, +Z, -Z or a tuple")
        return AXES[key]
    try:
        x, y, z = (float(c) for c in v)
    except (TypeError, ValueError):
        raise SelectorError(f"a direction is +X..-Z or a 3-tuple, got {v!r}") from None
    return (x, y, z)


def _key(shape: Shape) -> int:
    return hash(shape.wrapped)


def resolve(selector: Selector, body: Shape, identity: Identity | None = None, many: bool = False) -> list[Shape]:
    """The shapes a selector picks on a body. Raises SelectorError when nothing
    matches, or when several match and the caller wants one (`many=False`)
    and the selector has no `.all()`. Semantic steps need the identity map."""
    if selector.kind == "faces":
        items: list[Shape] = list(body.faces())
    elif selector.kind == "edges":
        items = list(body.edges())
    elif selector.kind == "vertices":
        items = list(body.vertices())
    elif selector.kind in ("planes", "axes"):
        raise SelectorError(f"{selector.ref_name} is a mate reference, not geometry of a body")
    else:
        raise SelectorError(f"unknown selector kind {selector.kind!r}")
    single = False
    for op, arg in selector.ops:
        if op == "where":
            items = [s for s in items if _matches(s, arg)]
        elif op == "tag":
            if identity is None:
                raise SelectorError(f"{selector.ref_name}: labels are not available here; use a geometric selector")
            items = [s for s in items
                     if identity.owner.get(_key(s)) == selector.feature and arg in identity.tags.get(_key(s), ())]
            if not items:
                raise SelectorError(f"{selector.ref_name} matches nothing: {selector.feature!r} has no {selector.kind} labelled {arg.lstrip(':')!r}")
        elif op == "of":
            if identity is None or not identity.groups:
                raise SelectorError(f"{selector.ref_name}: of() applies to instances of part files")
            items = [s for s in items if identity.groups.get(_key(s)) == arg]
            if not items:
                raise SelectorError(f"{selector.ref_name} matches nothing: no {selector.kind} of {selector.feature!r} come from {arg!r}")
        elif op == "nearest":
            if not items:
                break
            target = Vector(*arg)
            items = [min(items, key=lambda s: (s.center() - target).length)]
            single = True
        elif op in ("largest", "smallest"):
            if not items:
                break
            items = [(max if op == "largest" else min)(items, key=_size)]
            single = True
        elif op == "all":
            single = False
        else:
            raise SelectorError(f"unknown selector operation {op!r}")
    if not items:
        raise SelectorError(f"{selector.ref_name} matches nothing")
    if not many and not single and len(items) > 1 and not any(op == "all" for op, _ in selector.ops):
        raise SelectorError(f"{selector.ref_name} matches {len(items)} items; add nearest(), largest(), smallest() or all()")
    return items


def _size(s: Shape) -> float:
    """What largest() and smallest() compare: a face's area, an edge's length."""
    from build123d import Edge, Face, Wire

    if isinstance(s, (Edge, Wire)):
        return float(s.length)
    if isinstance(s, Face):
        return float(s.area)
    for attr in ("volume", "area", "length"):
        v = getattr(s, attr, None)
        if v:
            return float(v)
    return 0.0


def _matches(s: Shape, cond: dict[str, Any]) -> bool:
    if "kind" in cond:
        gt = getattr(s, "geom_type", None)
        if gt is None or gt.name.lower() != cond["kind"]:
            return False
    if "normal" in cond:
        try:
            n = s.normal_at()
        except Exception:  # noqa: BLE001 - not a face
            return False
        if (n - Vector(*cond["normal"]).normalized()).length > 1e-3:
            return False
    if "parallel_to" in cond:
        d = _edge_direction(s)
        if d is None:
            return False
        if abs(abs(d.dot(Vector(*cond["parallel_to"]).normalized())) - 1.0) > 1e-3:
            return False
    return True


def _edge_direction(s: Shape):
    try:
        if s.geom_type.name != "LINE":
            return None
        d = s.end_point() - s.start_point()
        return d.normalized() if d.length > 1e-9 else None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["AXIS_NAMES", "LABELS", "PLANE_NAMES", "Axis", "Identity", "Query", "Selector", "SelectorError", "resolve"]
