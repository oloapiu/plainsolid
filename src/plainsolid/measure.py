"""Measurements between faces, edges, vertices and points, addressed by the
same global ids the mesh uses."""
from __future__ import annotations

import math
from typing import Any

from build123d import Edge, Face, GeomType, Shape, Vector, Vertex
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType

from .evaluate import Item


class MeasureError(ValueError):
    pass


def _v(vec) -> list[float]:
    return [round(float(c), 6) for c in vec]


def resolve(items: list[Item], ref: dict[str, Any]) -> tuple[Shape, dict[str, Any]]:
    """A shape for a reference like {"face": 3}, {"edge": 5}, {"vertex": 2}, {"point": [x, y, z]}."""
    if not isinstance(ref, dict) or len(ref) != 1:
        raise MeasureError(f"a reference is one of face, edge, vertex or point: {ref!r}")
    (kind, value), = ref.items()
    if kind == "point":
        try:
            x, y, z = (float(c) for c in value)
        except (TypeError, ValueError):
            raise MeasureError(f"point needs [x, y, z], got {value!r}") from None
        return Vertex(x, y, z), {"kind": "point", "position": [x, y, z]}
    if kind not in ("face", "edge", "vertex"):
        raise MeasureError(f"unknown reference kind {kind!r}")
    try:
        idx = int(value)
    except (TypeError, ValueError):
        raise MeasureError(f"{kind} id must be an integer, got {value!r}") from None
    base = 0
    for it in items:
        sub = it.shape.faces() if kind == "face" else it.shape.edges() if kind == "edge" else it.shape.vertices()
        if idx < base + len(sub):
            shape = sub[idx - base]
            info = describe(shape)
            info.update({"kind": kind, "id": idx, "item": it.name, "label": it.labels()[idx - base] if kind == "face" else it.name})
            return shape, info
        base += len(sub)
    raise MeasureError(f"no {kind} with id {idx}")


def describe(shape: Shape) -> dict[str, Any]:
    if isinstance(shape, Face):
        out: dict[str, Any] = {"type": shape.geom_type.name.lower(), "area": round(shape.area, 6),
                               "center": _v(shape.center())}
        ad = BRepAdaptor_Surface(shape.wrapped)
        if ad.GetType() == GeomAbs_SurfaceType.GeomAbs_Plane:
            out["normal"] = _v(shape.normal_at())
        elif ad.GetType() == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            cyl = ad.Cylinder()
            ax = cyl.Axis()
            out["radius"] = round(cyl.Radius(), 6)
            out["diameter"] = round(2 * cyl.Radius(), 6)
            out["axis_point"] = [ax.Location().X(), ax.Location().Y(), ax.Location().Z()]
            out["axis"] = [ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()]
        elif ad.GetType() == GeomAbs_SurfaceType.GeomAbs_Sphere:
            out["radius"] = round(ad.Sphere().Radius(), 6)
        return out
    if isinstance(shape, Edge):
        out = {"type": shape.geom_type.name.lower(), "length": round(shape.length, 6),
               "start": _v(shape.start_point()), "end": _v(shape.end_point()), "center": _v(shape.center())}
        if shape.geom_type == GeomType.CIRCLE:
            out["radius"] = round(shape.radius, 6)
            out["diameter"] = round(2 * shape.radius, 6)
            out["center"] = _v(shape.arc_center)
        elif shape.geom_type == GeomType.LINE:
            d = shape.end_point() - shape.start_point()
            out["direction"] = _v(d.normalized()) if d.length > 1e-12 else None
        return out
    if isinstance(shape, Vertex):
        return {"type": "vertex", "position": _v(shape)}
    return {"type": type(shape).__name__.lower()}


def _direction(info: dict[str, Any]) -> Vector | None:
    for key in ("normal", "axis", "direction"):
        if info.get(key):
            return Vector(*info[key])
    return None


def measure(items: list[Item], a: dict[str, Any], b: dict[str, Any] | None = None) -> dict[str, Any]:
    sa, ia = resolve(items, a)
    if b is None:
        return {"a": ia}
    sb, ib = resolve(items, b)
    dist, pa, pb = sa.distance_to_with_closest_points(sb)
    delta = pb - pa
    out: dict[str, Any] = {
        "a": ia, "b": ib,
        "distance": round(float(dist), 6),
        "closest": [_v(pa), _v(pb)],
        "delta": _v(delta),
    }
    da, db = _direction(ia), _direction(ib)
    if da is not None and db is not None:
        cos = max(-1.0, min(1.0, abs(da.normalized().dot(db.normalized()))))
        out["angle"] = round(math.degrees(math.acos(cos)), 6)
    ca = ia.get("axis_point") if "axis" in ia else ia.get("center") if ia.get("type") == "circle" else None
    cb = ib.get("axis_point") if "axis" in ib else ib.get("center") if ib.get("type") == "circle" else None
    if ca is not None and cb is not None:
        if "axis" in ia and "axis" in ib:
            out["axis_distance"] = round(_axis_distance(Vector(*ca), Vector(*ia["axis"]), Vector(*cb), Vector(*ib["axis"])), 6)
        else:
            out["center_distance"] = round((Vector(*cb) - Vector(*ca)).length, 6)
    return out


def _axis_distance(p1: Vector, d1: Vector, p2: Vector, d2: Vector) -> float:
    """Distance between two axes: perpendicular distance when parallel, else closest approach."""
    n = d1.cross(d2)
    w = p2 - p1
    if n.length < 1e-9:
        return (w - d1 * w.dot(d1)).length
    return abs(w.dot(n)) / n.length
