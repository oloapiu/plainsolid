"""Turn solved sketch entities into build123d faces located on the sketch plane.

Closed profiles nest by containment: a profile inside another is a hole,
a profile inside a hole is an island again (even-odd). Projected body
geometry joins the profile only when the entity says construction=False.
"""
from __future__ import annotations

import contextlib
import math
from typing import Any

from build123d import (
    Circle,
    Edge,
    Face,
    Location,
    Plane,
    Polygon,
    Rectangle,
    Sketch,
    SlotOverall,
    Wire,
)

from .corners import macro_corner_points, macro_corners, side_names
from .model import Entity, Feature
from .solver.build import Projected


class SketchError(ValueError):
    pass


STANDARD_PLANES = {"XY": Plane.XY, "XZ": Plane.XZ, "YZ": Plane.YZ}


def plane_for(feature: Feature) -> Plane:
    """The plane of a sketch on a standard plane (evaluate.py resolves the others)."""
    on = feature.args["on"]
    if not isinstance(on, str):
        raise SketchError(f"sketch {feature.name!r} is on a body face or plane feature; evaluate it in context")
    return finish_plane(STANDARD_PLANES[on], feature)


def finish_plane(base: Plane, feature: Feature) -> Plane:
    offset = float(feature.args.get("offset", 0.0))
    p = base.offset(offset) if offset else base
    if feature.args.get("flip"):
        p = Plane(origin=p.origin, x_dir=p.x_dir, z_dir=-p.z_dir)
    return p


def solved_args(entity: Entity, coords: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    a = dict(entity.args)
    if coords and entity.name in coords:
        a.update(coords[entity.name])
    return a


def _arc_edge(center, start, end) -> Edge:
    cx, cy = center
    sx, sy = start
    ex, ey = end
    r = math.hypot(sx - cx, sy - cy)
    if r < 1e-9:
        raise SketchError("arc has zero radius")
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    sweep = (a1 - a0) % (2 * math.pi)
    if sweep < 1e-9:
        sweep = 2 * math.pi
    am = a0 + sweep / 2
    mid = (cx + r * math.cos(am), cy + r * math.sin(am), 0)
    return Edge.make_three_point_arc((sx, sy, 0), mid, (ex, ey, 0))


def _edges_and_faces(entities: list[Entity], coords, projected: dict[str, Projected]) -> tuple[list[Edge], list[Face]]:
    faces: list[Face] = []
    edges: list[Edge] = []
    for e in entities:
        if e.construction or e.kind == "point":
            continue
        a = solved_args(e, coords)
        if e.kind == "line":
            (x1, y1), (x2, y2) = a["start"], a["end"]
            if abs(x1 - x2) < 1e-9 and abs(y1 - y2) < 1e-9:
                raise SketchError(f"line {e.name!r} has zero length")
            edges.append(Edge.make_line((x1, y1, 0), (x2, y2, 0)))
        elif e.kind == "arc":
            edges.append(_arc_edge(a["center"], a["start"], a["end"]))
        elif e.kind == "circle":
            if a["diameter"] <= 0:
                raise SketchError(f"circle {e.name!r} needs a positive diameter")
            faces.extend(_at(Circle(a["diameter"] / 2), a["at"]).faces())
        elif e.kind == "rect":
            if a["width"] <= 0 or a["height"] <= 0:
                raise SketchError(f"rect {e.name!r} needs positive width and height")
            if e.construction_sides:
                edges.extend(_macro_outline(e, a))  # an open outline: its sides close with other curves
            elif a.get("corners") or a.get("chamfers"):
                faces.append(Face(Wire(_macro_outline(e, a))))
            else:
                faces.extend(_at(Rectangle(a["width"], a["height"]), a["at"]).faces())
        elif e.kind == "slot":
            if a["length"] <= a["width"] or a["width"] <= 0:
                raise SketchError(f"slot {e.name!r} needs length greater than width, both positive")
            faces.extend(_at(SlotOverall(a["length"], a["width"], rotation=a["angle"]), a["at"]).faces())
        elif e.kind == "polygon":
            if e.construction_sides:
                edges.extend(_macro_outline(e, a))
            elif a.get("corners") or a.get("chamfers"):
                faces.append(Face(Wire(_macro_outline(e, a))))
            else:
                pts = [(x, y, 0) for x, y in a["points"]]
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                faces.extend(Polygon(*pts).faces())
        elif e.kind in ("project", "offset", "import_dxf"):
            pr = projected.get(e.name)
            if pr is None:
                raise SketchError(f"{e.kind} {e.name!r} was not resolved")
            for item in pr.items:
                c = item.coords
                if item.kind == "line":
                    edges.append(Edge.make_line((*c["start"], 0), (*c["end"], 0)))
                elif item.kind == "arc":
                    edges.append(_arc_edge(c["center"], c["start"], c["end"]))
                elif item.kind == "circle":
                    faces.extend(_at(Circle(c["radius"]), c["center"]).faces())
        else:
            raise SketchError(f"unknown entity kind {e.kind!r}")
    return edges, faces


def _macro_outline(e: Entity, a: dict[str, Any]) -> list[Edge]:
    """The outline of a rect or polygon with rounded or bevelled corners: sides cut back to
    the tangent points, arcs and chamfer lines between them, as (edge, label) pairs' edges."""
    return [edge for edge, _ in _macro_outline_labelled(e, a)]


def _macro_outline_labelled(e: Entity, a: dict[str, Any], include_construction: bool = False) -> list[tuple[Edge, str]]:
    """Sides in walking order with their corner cuts. A side named in construction_sides is left
    out (unless asked for), and so is a cut at a corner one of whose sides is construction."""
    n = e.name
    try:
        cuts = macro_corners(e.kind, a, f"{e.kind} {n!r}")
    except ValueError as exc:
        raise SketchError(str(exc)) from None
    pts = macro_corner_points(e.kind, a)
    count = len(pts)
    sides = side_names(e.kind, a)
    side_label = lambda i: f"{n}.{sides[i]}"  # noqa: E731
    hidden = set(e.construction_sides) if not include_construction else set()
    out: list[tuple[Edge, str]] = []
    for i, (name, p) in enumerate(pts):
        nxt_name, q = pts[(i + 1) % count]
        start = cuts[name]["t2"] if name in cuts else p
        end = cuts[nxt_name]["t1"] if nxt_name in cuts else q
        if sides[i] not in hidden and math.hypot(end[0] - start[0], end[1] - start[1]) > 1e-9:
            out.append((Edge.make_line((*start, 0), (*end, 0)), side_label(i)))
        cut = cuts.get(nxt_name)
        if cut is None or sides[i] in hidden or sides[(i + 1) % count] in hidden:
            continue
        if cut["kind"] == "arc":
            out.append((_arc_edge(cut["center"], cut["start"], cut["end"]), f"{n}.{nxt_name}_arc"))
        else:
            out.append((Edge.make_line((*cut["t1"], 0), (*cut["t2"], 0)), f"{n}.{nxt_name}_chamfer"))
    return out


def _faces_2d(entities: list[Entity], coords=None, projected: dict[str, Projected] | None = None) -> list[Face]:
    edges, faces = _edges_and_faces(entities, coords, projected or {})
    if edges:
        for wire in Wire.combine(edges):
            if wire.is_closed:
                faces.append(Face(wire))
    return faces


def _at(obj: Sketch, at: tuple[float, float]) -> Sketch:
    x, y = at
    return obj.moved(Location((x, y, 0))) if (x or y) else obj


def _nest(faces: list[Face]) -> list[Face]:
    """Even-odd nesting by containment of face centres."""
    faces = sorted(faces, key=lambda f: -f.area)
    boxes = [f.bounding_box() for f in faces]
    depth = [0] * len(faces)
    parent = [-1] * len(faces)
    for i, f in enumerate(faces):
        c = f.center()
        for j in range(i):
            b = boxes[j]  # the point-in-face test is costly: only faces whose box holds the point
            if not (b.min.X - 1e-6 <= c.X <= b.max.X + 1e-6 and b.min.Y - 1e-6 <= c.Y <= b.max.Y + 1e-6):
                continue
            if faces[j].is_inside(c):
                depth[i] += 1
                if parent[i] == -1 or depth[j] > depth[parent[i]]:
                    parent[i] = j
    result: list[Face] = []
    for i, f in enumerate(faces):
        if depth[i] % 2:
            continue
        holes = [faces[k] for k in range(len(faces)) if parent[k] == i and depth[k] == depth[i] + 1]
        result.extend(_with_holes(f, holes))
    return result


def _with_holes(face: Face, holes: list[Face]) -> list[Face]:
    """The face with the holes inside it: built from the wires at once, which is fast; a
    boolean cut per hole when that face is not valid (a hole touching the outline)."""
    if not holes:
        return [face]
    with contextlib.suppress(Exception):
        made = Face(face.outer_wire(), [h.outer_wire() for h in holes])
        if made.is_valid() and abs(made.area - (face.area - sum(h.area for h in holes))) <= 1e-6 * max(face.area, 1.0):
            return [made]
    shape = face
    for h in holes:
        shape = shape - h
    return list(shape.faces())


def sketch_faces(feature: Feature, coords=None, projected: dict[str, Projected] | None = None,
                 plane: Plane | None = None) -> Sketch:
    """All closed profiles of the sketch as faces on its plane."""
    faces = _nest(_faces_2d(feature.entities, coords, projected))
    if not faces:
        raise SketchError(f"sketch {feature.name!r} has no closed profile")
    return (plane or plane_for(feature)) * Sketch(faces)


def _circle_edge(center, radius: float) -> Edge:
    cx, cy = center
    return Edge.make_circle(radius, Plane((cx, cy, 0)))


def profile_edges(feature: Feature, coords=None, projected: dict[str, Projected] | None = None,
                  include_construction: bool = False) -> list[tuple[Edge, str]]:
    """The profile's edges in sketch coordinates (z = 0), each with the label the
    identity map gives the body faces it generates: an entity name, or a side of
    a macro ("rect1.top", "slot1.start_arc", "poly.e2"). Best effort: entities
    that cannot be built are skipped, sketch_faces() reports the real errors.
    With include_construction, construction curves are listed too (offsets may
    follow them)."""
    projected = projected or {}
    out: list[tuple[Edge, str]] = []

    def line(a, b) -> Edge:
        return Edge.make_line((*a, 0), (*b, 0))

    for e in feature.entities:
        if (e.construction and not include_construction) or e.kind == "point":
            continue
        a = solved_args(e, coords)
        n = e.name
        with contextlib.suppress(Exception):  # a degenerate entity: the profile build reports it
            if e.kind == "line":
                out.append((line(a["start"], a["end"]), n))
            elif e.kind == "arc":
                out.append((_arc_edge(a["center"], a["start"], a["end"]), n))
            elif e.kind == "circle":
                out.append((_circle_edge(a["at"], a["diameter"] / 2), n))
            elif e.kind in ("rect", "polygon") and (a.get("corners") or a.get("chamfers") or e.construction_sides):
                out += _macro_outline_labelled(e, a, include_construction)
            elif e.kind == "rect":
                cx, cy = a["at"]
                w, h = a["width"] / 2, a["height"] / 2
                bl, br, tr, tl = (cx - w, cy - h), (cx + w, cy - h), (cx + w, cy + h), (cx - w, cy + h)
                out += [(line(bl, br), f"{n}.bottom"), (line(br, tr), f"{n}.right"),
                        (line(tr, tl), f"{n}.top"), (line(tl, bl), f"{n}.left")]
            elif e.kind == "slot":
                cx, cy = a["at"]
                t = math.radians(a["angle"])
                half, r = (a["length"] - a["width"]) / 2, a["width"] / 2
                ux, uy = math.cos(t), math.sin(t)
                vx, vy = -uy, ux
                px, py = cx - ux * half, cy - uy * half
                qx, qy = cx + ux * half, cy + uy * half
                out += [(line((px + vx * r, py + vy * r), (qx + vx * r, qy + vy * r)), f"{n}.top"),
                        (line((px - vx * r, py - vy * r), (qx - vx * r, qy - vy * r)), f"{n}.bottom"),
                        (_arc_edge((px, py), (px + vx * r, py + vy * r), (px - vx * r, py - vy * r)), f"{n}.start_arc"),
                        (_arc_edge((qx, qy), (qx - vx * r, qy - vy * r), (qx + vx * r, qy + vy * r)), f"{n}.end_arc")]
            elif e.kind == "polygon":
                pts = a["points"]
                for i, pt in enumerate(pts):
                    out.append((line(pt, pts[(i + 1) % len(pts)]), f"{n}.e{i}"))
            elif e.kind in ("project", "offset", "import_dxf"):
                pr = projected.get(n)
                if pr is None:
                    continue
                for i, item in enumerate(pr.items):
                    label = n if len(pr.items) == 1 else f"{n}.e{i}"
                    c = item.coords
                    if item.kind == "line":
                        out.append((line(c["start"], c["end"]), label))
                    elif item.kind == "arc":
                        out.append((_arc_edge(c["center"], c["start"], c["end"]), label))
                    elif item.kind == "circle":
                        out.append((_circle_edge(c["center"], c["radius"]), label))
    return out


def open_chains(feature: Feature, coords=None, projected: dict[str, Projected] | None = None) -> int:
    """Number of edge chains that do not close (for warnings)."""
    edges, _ = _edges_and_faces(feature.entities, coords, projected or {})
    if not edges:
        return 0
    return sum(1 for w in Wire.combine(edges) if not w.is_closed)
