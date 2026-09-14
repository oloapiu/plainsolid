"""Bring body geometry into a sketch: resolve a selector on the body and
express the result in the sketch plane's 2D coordinates."""
from __future__ import annotations

from build123d import Edge, Face, GeomType, Plane, Shape, Vector, Vertex

from .selectors import Identity, Selector, SelectorError, resolve
from .solver.build import Projected, ProjItem


class ProjectionError(ValueError):
    pass


def _xy(v) -> tuple[float, float]:
    return (round(float(v.X), 9), round(float(v.Y), 9))


def _local(plane: Plane, point) -> tuple[float, float]:
    """A 3D point in the plane's 2D frame. Always through a Vector: a Vertex handed
    to Plane.to_local_coords comes back with its global coordinates untouched."""
    return _xy(plane.to_local_coords(Vector(*[float(c) for c in point])))


def _ccw_passes(center, start, end, mid) -> bool:
    """Does the counter-clockwise sweep from start to end about center pass through mid?"""
    import math

    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    am = math.atan2(mid[1] - center[1], mid[0] - center[0])
    sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
    return (am - a0) % (2 * math.pi) <= sweep + 1e-9


def edge_item(edge: Edge, plane: Plane) -> ProjItem:
    """An edge as a sketch-local line, circle or arc item. Arcs are stored counter-
    clockwise in the sketch frame, so start and end swap when the plane looks at the
    edge from the other side."""
    gt = edge.geom_type
    start, end = _local(plane, edge.start_point()), _local(plane, edge.end_point())
    if gt == GeomType.LINE:
        return ProjItem("line", {"start": start, "end": end})
    if gt == GeomType.CIRCLE:
        c = _local(plane, edge.arc_center)
        r = float(edge.radius)
        if edge.is_closed:
            return ProjItem("circle", {"center": c, "radius": r})
        if not _ccw_passes(c, start, end, _local(plane, edge.position_at(0.5))):
            start, end = end, start
        return ProjItem("arc", {"center": c, "radius": r, "start": start, "end": end})
    # anything else becomes a straight construction segment through its ends
    return ProjItem("line", {"start": start, "end": end})


def project(selector: Selector, body: Shape, plane: Plane, identity: Identity | None = None) -> Projected:
    try:
        shapes = resolve(selector, body, identity, many=True)
    except SelectorError as exc:
        raise ProjectionError(str(exc)) from None
    items: list[ProjItem] = []
    for s in shapes:
        if isinstance(s, Vertex):
            items.append(ProjItem("point", {"at": _local(plane, (s.X, s.Y, s.Z))}))
        elif isinstance(s, Edge):
            items.append(edge_item(s, plane))
        elif isinstance(s, Face):
            for e in s.outer_wire().edges():
                items.append(edge_item(e, plane))
        else:
            raise ProjectionError(f"cannot project a {type(s).__name__.lower()}")
    if not items:
        raise ProjectionError(f"{selector.ref_name} projected to nothing")
    return Projected(selector.ref_name, items)
