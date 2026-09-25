"""Trim: the piece of a line, arc or circle under the cursor goes, up to the nearest crossings
with any other curve of the sketch (construction and converted geometry included), as the
ordinary edit operations applied in one batch. A cut end is related to the curve it was cut
at; a line cut in the middle becomes two colinear lines, an arc two coradial arcs; a circle cut
between two crossings becomes an arc of the same name; a curve with no crossing goes entirely.
Relations on what was removed go with it, and a length on a shortened line is dropped."""
from __future__ import annotations

import math
from typing import Any

from .corners import macro_corner_points, macro_corners, side_names
from .filleting import Names
from .model import Feature
from .parse import POSITIONAL
from .solver.build import Projected

Pt = tuple[float, float]
TAU = 2 * math.pi
EPS = 1e-6


class TrimError(ValueError):
    pass


# --- the sketch's curves, as the browser sees them --------------------------------------------

def _arc(ref: str, entity: str, c: Pt, s: Pt, e: Pt) -> dict[str, Any]:
    return {"kind": "arc", "ref": ref, "entity": entity, "c": c, "r": math.hypot(s[0] - c[0], s[1] - c[1]),
            "a0": math.atan2(s[1] - c[1], s[0] - c[0]), "a1": math.atan2(e[1] - c[1], e[0] - c[0])}


def sketch_curves(feature: Feature, coords: dict[str, dict[str, Any]] | None, projected: dict[str, Projected] | None) -> list[dict[str, Any]]:
    """Every curve of the sketch with its reference: lines, circles, arcs, the sides and cuts of
    macros, converted and offset geometry, and the axes."""
    out: list[dict[str, Any]] = []
    L = lambda ref, entity, a, b: out.append({"kind": "line", "ref": ref, "entity": entity, "a": tuple(a), "b": tuple(b)})  # noqa: E731
    reach = 1e4
    L("x_axis", "x_axis", (-reach, 0.0), (reach, 0.0))
    L("y_axis", "y_axis", (0.0, -reach), (0.0, reach))
    for e in feature.entities:
        a = dict(e.args)
        if coords and e.name in coords:
            a.update(coords[e.name])
        n = e.name
        if e.kind == "line":
            L(n, n, a["start"], a["end"])
        elif e.kind == "circle":
            out.append({"kind": "circle", "ref": n, "entity": n, "c": tuple(a["at"]), "r": a["diameter"] / 2})
        elif e.kind == "arc":
            out.append(_arc(n, n, tuple(a["center"]), tuple(a["start"]), tuple(a["end"])))
        elif e.kind in ("rect", "polygon"):
            pts = macro_corner_points(e.kind, a)
            sides = side_names(e.kind, a)
            try:
                cuts = macro_corners(e.kind, a, n)
            except ValueError:
                cuts = {}
            count = len(pts)
            for i, (name, p) in enumerate(pts):
                nxt, q = pts[(i + 1) % count]
                start = cuts[name]["t2"] if name in cuts else p
                end = cuts[nxt]["t1"] if nxt in cuts else q
                L(f"{n}.{sides[i]}", n, start, end)
                cut = cuts.get(nxt)
                if cut and cut["kind"] == "arc":
                    out.append(_arc(f"{n}.{nxt}_arc", n, cut["center"], cut["start"], cut["end"]))
                elif cut:
                    L(f"{n}.{nxt}_chamfer", n, cut["t1"], cut["t2"])
        elif e.kind == "slot":
            cx, cy = a["at"]
            t = math.radians(a["angle"])
            half, r = (a["length"] - a["width"]) / 2, a["width"] / 2
            ux, uy = math.cos(t), math.sin(t)
            vx, vy = -uy, ux
            p, q = (cx - ux * half, cy - uy * half), (cx + ux * half, cy + uy * half)
            L(f"{n}.top", n, (p[0] + vx * r, p[1] + vy * r), (q[0] + vx * r, q[1] + vy * r))
            L(f"{n}.bottom", n, (p[0] - vx * r, p[1] - vy * r), (q[0] - vx * r, q[1] - vy * r))
            out.append(_arc(f"{n}.start_arc", n, p, (p[0] + vx * r, p[1] + vy * r), (p[0] - vx * r, p[1] - vy * r)))
            out.append(_arc(f"{n}.end_arc", n, q, (q[0] - vx * r, q[1] - vy * r), (q[0] + vx * r, q[1] + vy * r)))
        elif e.kind in ("project", "offset", "import_dxf") and projected and n in projected:
            items = projected[n].items
            for i, it in enumerate(items):
                ref = n if len(items) == 1 else f"{n}.e{i}"
                c = it.coords
                if it.kind == "line":
                    L(ref, n, c["start"], c["end"])
                elif it.kind == "arc":
                    out.append(_arc(ref, n, tuple(c["center"]), tuple(c["start"]), tuple(c["end"])))
                elif it.kind == "circle":
                    out.append({"kind": "circle", "ref": ref, "entity": n, "c": tuple(c["center"]), "r": c["radius"]})
    return out


# --- crossings ---------------------------------------------------------------------------------

def _sweep(a0: float, a1: float) -> float:
    s = (a1 - a0) % TAU
    return s if s > 1e-9 else TAU


def _on_arc(c: dict[str, Any], p: Pt, slack: float = 1e-6) -> bool:
    """Whether a point of the circle lies within an arc's sweep (any point for a full circle)."""
    if c["kind"] == "circle":
        return True
    u = (math.atan2(p[1] - c["c"][1], p[0] - c["c"][0]) - c["a0"]) % TAU
    return u <= _sweep(c["a0"], c["a1"]) + slack or u >= TAU - slack


def _on_segment(c: dict[str, Any], p: Pt, slack: float = 1e-6) -> bool:
    a, b = c["a"], c["b"]
    dx, dy, l2 = b[0] - a[0], b[1] - a[1], (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
    if l2 < 1e-18:
        return False
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2
    return -slack <= t <= 1 + slack


def _line_line(p: dict[str, Any], q: dict[str, Any]) -> list[Pt]:
    (ax, ay), (bx, by) = p["a"], p["b"]
    (cx, cy), (dx, dy) = q["a"], q["b"]
    den = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx)
    if abs(den) < 1e-12:
        return []
    t = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / den
    return [(ax + t * (bx - ax), ay + t * (by - ay))]


def _line_circle(ln: dict[str, Any], ci: dict[str, Any]) -> list[Pt]:
    (ax, ay), (bx, by) = ln["a"], ln["b"]
    (cx, cy), r = ci["c"], ci["r"]
    dx, dy = bx - ax, by - ay
    fx, fy = ax - cx, ay - cy
    A, B, C = dx * dx + dy * dy, 2 * (fx * dx + fy * dy), fx * fx + fy * fy - r * r
    if A < 1e-18:
        return []
    disc = B * B - 4 * A * C
    if disc < -1e-9:
        return []
    disc = max(disc, 0.0)
    roots = {(-B - math.sqrt(disc)) / (2 * A), (-B + math.sqrt(disc)) / (2 * A)}
    return [(ax + t * dx, ay + t * dy) for t in roots]


def _circle_circle(p: dict[str, Any], q: dict[str, Any]) -> list[Pt]:
    (x0, y0), r0 = p["c"], p["r"]
    (x1, y1), r1 = q["c"], q["r"]
    d = math.hypot(x1 - x0, y1 - y0)
    if d < 1e-12 or d > r0 + r1 + 1e-9 or d < abs(r0 - r1) - 1e-9:
        return []
    a = (r0 * r0 - r1 * r1 + d * d) / (2 * d)
    h = math.sqrt(max(r0 * r0 - a * a, 0.0))
    mx, my = x0 + a * (x1 - x0) / d, y0 + a * (y1 - y0) / d
    rx, ry = -(y1 - y0) / d * h, (x1 - x0) / d * h
    pts = {(mx + rx, my + ry), (mx - rx, my - ry)}
    return list(pts)


def crossings(target: dict[str, Any], other: dict[str, Any]) -> list[Pt]:
    """Where two curves meet, within both of them."""
    kinds = (target["kind"] == "line", other["kind"] == "line")
    if kinds == (True, True):
        pts = _line_line(target, other)
    elif kinds == (True, False):
        pts = _line_circle(target, other)
    elif kinds == (False, True):
        pts = _line_circle(other, target)
    else:
        pts = _circle_circle(target, other)
    inside = lambda c, p: _on_segment(c, p) if c["kind"] == "line" else _on_arc(c, p)  # noqa: E731
    return [p for p in pts if inside(target, p) and inside(other, p)]


def param(c: dict[str, Any], p: Pt) -> float:
    """Where along the curve a point is: a line's fraction, an arc's angle from its start, a circle's angle."""
    if c["kind"] == "line":
        a, b = c["a"], c["b"]
        l2 = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 or 1.0
        return ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / l2
    ang = math.atan2(p[1] - c["c"][1], p[0] - c["c"][0])
    return (ang - c["a0"]) % TAU if c["kind"] == "arc" else ang % TAU


def point_at(c: dict[str, Any], u: float) -> Pt:
    if c["kind"] == "line":
        a, b = c["a"], c["b"]
        return (a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1]))
    ang = c["a0"] + u if c["kind"] == "arc" else u
    return (c["c"][0] + c["r"] * math.cos(ang), c["c"][1] + c["r"] * math.sin(ang))


def nearest_param(c: dict[str, Any], p: Pt) -> float:
    if c["kind"] == "line":
        return max(0.0, min(1.0, param(c, p)))
    return param(c, p)


def piece(curves: list[dict[str, Any]], target: dict[str, Any], at: Pt) -> dict[str, Any]:
    """The piece of the target under the cursor: its parameter span, and the curve it is cut at on
    each side (None at the target's own end, or all round for a circle without two crossings)."""
    hits: list[tuple[float, str]] = []
    for other in curves:
        if other["ref"] == target["ref"] or other["entity"] == target["entity"] and other["kind"] == target["kind"] and other["ref"] == target["ref"]:
            continue
        if other["entity"] == target["entity"]:
            continue  # a macro's own sides meet at its corners, that is no crossing
        for p in crossings(target, other):
            u = param(target, p)
            if target["kind"] == "line" and not (EPS < u < 1 - EPS):
                continue
            if target["kind"] == "arc" and not (EPS < u < _sweep(target["a0"], target["a1"]) - EPS):
                continue
            hits.append((u, other["ref"]))
    # several curves crossing at one point: the sketch's own geometry names the boundary, not an axis
    hits.sort(key=lambda h: (h[0], h[1] in ("x_axis", "y_axis"), h[1]))
    merged: list[tuple[float, str]] = []
    for h in hits:
        if merged and abs(h[0] - merged[-1][0]) < 1e-9:
            continue
        merged.append(h)
    hits = merged
    uc = nearest_param(target, at)
    if target["kind"] == "circle":
        if len(hits) < 2:
            return {"whole": True}
        below = max((h for h in hits if h[0] < uc), default=hits[-1])
        above = min((h for h in hits if h[0] > uc), default=hits[0])
        return {"whole": False, "below": below, "above": above}
    below = max((h for h in hits if h[0] < uc), default=None)
    above = min((h for h in hits if h[0] > uc), default=None)
    return {"whole": below is None and above is None, "below": below, "above": above}


# --- the operations ---------------------------------------------------------------------------

def _num(v: float) -> float | int:
    r = round(float(v), 4)
    return int(r) if abs(r - round(r)) < 1e-9 else r


def _pt(p: Pt) -> list[float | int]:
    return [_num(p[0]), _num(p[1])]


def _relation_kind(curves: list[dict[str, Any]], ref: str) -> str:
    c = next((x for x in curves if x["ref"] == ref), None)
    return "coincident" if c is None or c["kind"] == "line" else "on"


def _delete_referencing(feature: Feature, sketch: str, ref: str, ops: list[dict[str, Any]], keep: set[str]) -> None:
    for c in feature.constraints:
        if c.name in keep:
            continue
        if any(r == ref or r.startswith(ref + ".") for r in c.refs):
            ops.append({"op": "delete_constraint", "sketch": sketch, "constraint": c.name})
            keep.add(c.name)


def _repoint(feature: Feature, sketch: str, old: str, new: str, ops: list[dict[str, Any]], skip: set[str]) -> None:
    for c in feature.constraints:
        if c.name in skip:
            continue
        for i, r in enumerate(c.refs):
            if r == old:
                names = POSITIONAL.get(c.kind, ())
                if i + 1 < len(names):
                    ops.append({"op": "set_constraint_argument", "sketch": sketch, "constraint": c.name, "kwarg": names[i + 1], "value": new})


def trim_ops(feature: Feature, coords: dict[str, dict[str, Any]] | None, projected: dict[str, Projected] | None,
             entity: str, at: Pt) -> list[dict[str, Any]]:
    """The batch that removes the piece of `entity` (a line, arc or circle) under `at`."""
    e = feature.entity(entity)
    if e is None:
        raise TrimError(f"no entity {entity!r}")
    if e.kind in ("rect", "polygon", "slot"):
        raise TrimError(f"{entity!r} is a {e.kind}: its sides cannot be trimmed; draw the outline with lines to trim it")
    if e.kind in ("project", "offset"):
        raise TrimError(f"{entity!r} follows other geometry and cannot be trimmed")
    if e.kind == "import_dxf":
        raise TrimError(f"{entity!r} is a DXF import; convert it to lines first to trim it")
    if e.kind not in ("line", "arc", "circle"):
        raise TrimError(f"{entity!r} is a {e.kind}; trim works on lines, arcs and circles")
    curves = sketch_curves(feature, coords, projected)
    target = next(c for c in curves if c["ref"] == entity)
    cut = piece(curves, target, at)
    sketch = feature.name
    ops: list[dict[str, Any]] = []
    done: set[str] = set()
    names = Names(feature)
    if cut["whole"]:
        _delete_referencing(feature, sketch, entity, ops, done)
        ops.append({"op": "delete_sketch_entity", "sketch": sketch, "entity": entity})
        return ops
    below, above = cut["below"], cut["above"]
    relate = lambda point_ref, other_ref: ops.append({"op": "add_constraint", "sketch": sketch, "kind": _relation_kind(curves, other_ref),  # noqa: E731
                                                     "name": names.next(_relation_kind(curves, other_ref)), "refs": [point_ref, other_ref]})
    if e.kind == "circle":
        # the complementary arc, counter-clockwise from the crossing above the cursor round to the one below
        start, end = point_at(target, above[0]), point_at(target, below[0])
        ops.append({"op": "delete_sketch_entity", "sketch": sketch, "entity": entity})
        ops.append({"op": "add_sketch_entity", "sketch": sketch, "kind": "arc", "name": entity,
                    "args": {"center": _pt(target["c"]), "start": _pt(start), "end": _pt(end), **({"construction": True} if e.construction else {})}})
        relate(f"{entity}.start", above[1])
        relate(f"{entity}.end", below[1])
        return ops
    # a line or an arc: its start is at parameter 0, its end at 1 (or at the sweep)
    if below is not None and above is not None:
        # the middle goes: the original keeps the start side, a new piece takes the end side
        old_end = target["b"] if e.kind == "line" else point_at(target, _sweep(target["a0"], target["a1"]))
        new = names.next(e.kind)
        ops.append({"op": "set_entity_argument", "sketch": sketch, "entity": entity, "kwarg": "end", "value": _pt(point_at(target, below[0]))})
        args: dict[str, Any] = {"start": _pt(point_at(target, above[0])), "end": _pt(old_end)}
        if e.kind == "arc":
            args = {"center": _pt(target["c"]), **args}
        if e.construction:
            args["construction"] = True
        ops.append({"op": "add_sketch_entity", "sketch": sketch, "kind": e.kind, "name": new, "args": args})
        _repoint(feature, sketch, f"{entity}.end", f"{new}.end", ops, done)
        relate(f"{entity}.end", below[1])
        relate(f"{new}.start", above[1])
        hold = "colinear" if e.kind == "line" else "coradial"
        ops.append({"op": "add_constraint", "sketch": sketch, "kind": hold, "name": names.next(hold), "refs": [new, entity]})
    elif below is not None:
        ops.append({"op": "set_entity_argument", "sketch": sketch, "entity": entity, "kwarg": "end", "value": _pt(point_at(target, below[0]))})
        _delete_referencing(feature, sketch, f"{entity}.end", ops, done)
        relate(f"{entity}.end", below[1])
    else:
        assert above is not None
        ops.append({"op": "set_entity_argument", "sketch": sketch, "entity": entity, "kwarg": "start", "value": _pt(point_at(target, above[0]))})
        _delete_referencing(feature, sketch, f"{entity}.start", ops, done)
        relate(f"{entity}.start", above[1])
    if e.kind == "line":  # a length on a shortened line means nothing any more
        for c in feature.constraints:
            if c.kind == "length" and c.refs == [entity] and c.name not in done:
                ops.append({"op": "delete_constraint", "sketch": sketch, "constraint": c.name})
                done.add(c.name)
    return ops
