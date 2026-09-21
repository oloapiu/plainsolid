"""Fillets and chamfers at sketch corners, as the ordinary edit operations they expand into.

A corner between two lines gets an arc (or a bevel line) held by coincidences and tangencies,
and a virtual sharp: a construction point kept on both lines' extensions where the corner was,
so every relation that referenced the corner moves to it and a length on either line becomes a
distance to it. Nothing moves. A corner of a rect or polygon becomes an entry of its corners=
(or chamfers=) argument, whose arc is a first-class reference of the macro. Every fillet gets a
radius dimension, or an equal relation to the first one when several are made at once.
The reverse operation takes either apart again."""
from __future__ import annotations

import math
import re
from typing import Any

from .corners import corner_geometry, corner_names, corner_spec, macro_corner_points, macro_corners
from .model import Constraint, Feature
from .parse import POSITIONAL

Pt = tuple[float, float]
_EPS = 1e-6
_PREFIX = {"coincident": "c", "tangent": "t", "on": "on", "radius": "rad", "equal": "eq", "distance": "d", "colinear": "cl", "coradial": "cr",
           "fillet": "fillet", "chamfer": "chamfer", "sharp": "sharp"}


class FilletError(ValueError):
    pass


class Names:
    """Fresh names in the sketch's one namespace, across the operations of one batch."""

    def __init__(self, feature: Feature) -> None:
        self.taken = {e.name for e in feature.entities} | {c.name for c in feature.constraints}

    def next(self, kind: str) -> str:
        prefix = _PREFIX.get(kind, kind)
        i = 1
        while f"{prefix}{i}" in self.taken:
            i += 1
        self.taken.add(f"{prefix}{i}")
        return f"{prefix}{i}"


def _solved(feature: Feature, coords: dict[str, dict[str, Any]] | None, name: str) -> dict[str, Any]:
    e = feature.entity(name)
    if e is None:
        raise FilletError(f"no entity {name!r}")
    a = dict(e.args)
    if coords and name in coords:
        a.update(coords[name])
    return a


def _ends(feature: Feature, coords, name: str) -> tuple[Pt, Pt]:
    a = _solved(feature, coords, name)
    return (tuple(a["start"]), tuple(a["end"]))  # type: ignore[return-value]


def _near(p: Pt, q: Pt) -> bool:
    return math.hypot(p[0] - q[0], p[1] - q[1]) < _EPS


def _num(v: float) -> float | int:
    """A coordinate as the file writes it: four decimals, whole numbers without a fraction."""
    r = round(float(v), 4)
    return int(r) if abs(r - round(r)) < 1e-9 else r


def _round(p: Pt) -> list[float | int]:
    return [_num(p[0]), _num(p[1])]


def _line_end(feature: Feature, ref: str, what: str) -> tuple[str, str]:
    """('line1', 'end') from 'line1.end', checked to be an end of a line."""
    parts = ref.split(".")
    if len(parts) != 2 or parts[1] not in ("start", "end"):
        raise FilletError(f"{what}: {ref!r} is not the start or end of a line")
    e = feature.entity(parts[0])
    if e is None or e.kind != "line":
        raise FilletError(f"{what}: {parts[0]!r} is not a line")
    return parts[0], parts[1]


def _constraint_op(name: str, kind: str, refs: list[str], sketch: str, value: Any = None, options: dict | None = None) -> dict:
    op: dict[str, Any] = {"op": "add_constraint", "sketch": sketch, "kind": kind, "name": name, "refs": refs}
    if value is not None:
        op["value"] = value
    if options:
        op["options"] = options
    return op


def _value_of(c: Constraint) -> Any:
    """A dimension's value as it was written: an expression stays an expression."""
    text = (c.value_text or "").strip()
    if text and not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return {"expr": text}
    return _num(c.value) if isinstance(c.value, (int, float)) else c.value


def _positional_name(c: Constraint, index: int) -> str:
    names = POSITIONAL.get(c.kind, ())
    if index + 1 >= len(names):
        raise FilletError(f"cannot re-point reference {index} of {c.kind} {c.name!r}")
    return names[index + 1]


def _label_beyond(center: Pt, corner: Pt, r: float) -> list[float]:
    """Where a fillet's radius label goes: past the arc, away from the corner."""
    dx, dy = center[0] - corner[0], center[1] - corner[1]
    n = math.hypot(dx, dy) or 1.0
    return _round((center[0] + dx / n * (r + 3), center[1] + dy / n * (r + 3)))


# --- making --------------------------------------------------------------------------------

def fillet_ops(feature: Feature, coords: dict[str, dict[str, Any]] | None, corners: list[dict[str, str]],
               size: float, kind: str = "fillet") -> list[dict[str, Any]]:
    """The batch that fillets (or chamfers, with a setback) the given corners at one size.
    A corner is {"a": "line1.end", "b": "line2.start"} for two lines meeting there, or
    {"entity": "rect1", "corner": "tl"} for a macro's corner."""
    if kind not in ("fillet", "chamfer"):
        raise FilletError(f"kind must be fillet or chamfer, got {kind!r}")
    if not corners:
        raise FilletError("no corner given")
    if not isinstance(size, (int, float)) or isinstance(size, bool) or size <= 0:
        raise FilletError(f"the {'radius' if kind == 'fillet' else 'setback'} must be a positive number, got {size!r}")
    size = _num(size)
    names = Names(feature)
    ops: list[dict[str, Any]] = []
    first: str | None = None  # the first arc or chamfer line: it carries the dimension, the others equal it
    macro_state: dict[str, dict[str, Any]] = {}  # entity -> its corners/chamfers as they will be after this batch
    for spec in corners:
        if "entity" in spec:
            ref = _macro_corner(feature, coords, spec, size, kind, names, ops, first is None, macro_state)
        else:
            ref = _line_corner(feature, coords, spec, size, kind, names, ops, first is None)
        if first is None:
            first = ref
        else:
            ops.append(_constraint_op(names.next("equal"), "equal", [ref, first], feature.name))
    return ops


def _line_corner(feature: Feature, coords, spec: dict[str, str], size: float, kind: str, names: Names,
                 ops: list[dict[str, Any]], dimension: bool) -> str:
    what = f"{kind} at {spec.get('a')!r} and {spec.get('b')!r}"
    if "a" not in spec or "b" not in spec:
        raise FilletError(f"{what}: a corner names two line ends, a and b")
    la, ea = _line_end(feature, spec["a"], what)
    lb, eb = _line_end(feature, spec["b"], what)
    if la == lb:
        raise FilletError(f"{what}: both ends belong to {la!r}")
    sa, ta = _ends(feature, coords, la)
    sb, tb = _ends(feature, coords, lb)
    p, far_a = (ta, sa) if ea == "end" else (sa, ta)
    pb, far_b = (tb, sb) if eb == "end" else (sb, tb)
    if not _near(p, pb):
        raise FilletError(f"{what}: the ends do not meet ({p[0]:.4g}, {p[1]:.4g}) and ({pb[0]:.4g}, {pb[1]:.4g})")
    # a third curve ending here would be left hanging
    for e in feature.entities:
        if e.kind not in ("line", "arc") or e.name in (la, lb):
            continue
        a = _solved(feature, coords, e.name)
        if _near(tuple(a["start"]), p) or _near(tuple(a["end"]), p):
            raise FilletError(f"{what}: {e.name!r} also ends at this corner; fillet a corner of two lines")
    try:
        g = corner_geometry(far_a, p, far_b, **({"radius": size} if kind == "fillet" else {"chamfer": size}))
    except ValueError as exc:
        raise FilletError(f"{what}: {'radius' if kind == 'fillet' else 'chamfer'} {size:g} does not fit ({exc})") from None
    sketch = feature.name
    sharp_ref = spec["a"]
    both_construction = bool(feature.entity(la).construction and feature.entity(lb).construction)  # type: ignore[union-attr]
    # the lines end at the tangent points now
    ops.append({"op": "set_entity_argument", "sketch": sketch, "entity": la, "kwarg": ea, "value": _round(g["t1"])})
    ops.append({"op": "set_entity_argument", "sketch": sketch, "entity": lb, "kwarg": eb, "value": _round(g["t2"])})
    new = names.next(kind)
    if kind == "fillet":
        args: dict[str, Any] = {"center": _round(g["center"]), "start": _round(g["start"]), "end": _round(g["end"])}
        a_end = "start" if _near(g["start"], g["t1"]) else "end"
    else:
        args = {"start": _round(g["t1"]), "end": _round(g["t2"])}
        a_end = "start"
    if both_construction:
        args["construction"] = True
    ops.append({"op": "add_sketch_entity", "sketch": sketch, "kind": "arc" if kind == "fillet" else "line", "name": new, "args": args})
    b_end = "end" if a_end == "start" else "start"
    # the corner's own coincidence goes; the new curve takes its place
    corner_coincidence = next((c for c in feature.constraints if c.kind == "coincident" and set(c.refs) == {spec["a"], spec["b"]}), None)
    if corner_coincidence is not None:
        ops.append({"op": "delete_constraint", "sketch": sketch, "constraint": corner_coincidence.name})
    ops.append(_constraint_op(names.next("coincident"), "coincident", [spec["a"], f"{new}.{a_end}"], sketch))
    ops.append(_constraint_op(names.next("coincident"), "coincident", [spec["b"], f"{new}.{b_end}"], sketch))
    if kind == "fillet":
        ops.append(_constraint_op(names.next("tangent"), "tangent", [new, la], sketch))
        ops.append(_constraint_op(names.next("tangent"), "tangent", [new, lb], sketch))
    # the virtual sharp: where the corner was, on both lines' extensions
    sharp = names.next("sharp")
    ops.append({"op": "add_sketch_entity", "sketch": sketch, "kind": "point", "name": sharp, "args": {"at": _round(p)}})
    ops.append(_constraint_op(names.next("on"), "on", [sharp, la], sketch))
    ops.append(_constraint_op(names.next("on"), "on", [sharp, lb], sketch))
    # what referenced the corner now references the sharp; a length of either line becomes a distance to it
    for c in feature.constraints:
        if c is corner_coincidence:
            continue
        if c.kind == "length" and c.refs == [la] or c.kind == "length" and c.refs == [lb]:
            far = far_a if c.refs == [la] else far_b
            far_ref = f"{c.refs[0]}.{'start' if _near(far, _ends(feature, coords, c.refs[0])[0]) else 'end'}"
            ops.append({"op": "delete_constraint", "sketch": sketch, "constraint": c.name})
            ops.append(_constraint_op(c.name, "distance", [far_ref, sharp], sketch, _value_of(c), dict(c.options) or None))
            continue
        for i, r in enumerate(c.refs):
            if r in (spec["a"], spec["b"]):
                ops.append({"op": "set_constraint_argument", "sketch": sketch, "constraint": c.name, "kwarg": _positional_name(c, i), "value": sharp})
    if dimension:
        if kind == "fillet":
            ops.append(_constraint_op(names.next("radius"), "radius", [new], sketch, size, {"at": _label_beyond(g["center"], p, size)}))
        else:
            ops.append(_constraint_op(names.next("distance"), "distance", [sharp, f"{new}.start"], sketch, size))
            ops.append(_constraint_op(names.next("distance"), "distance", [sharp, f"{new}.end"], sketch, size))
    del sharp_ref
    return new


def _macro_corner(feature: Feature, coords, spec: dict[str, str], size: float, kind: str, names: Names,
                  ops: list[dict[str, Any]], dimension: bool, state: dict[str, dict[str, Any]]) -> str:
    entity, corner = spec.get("entity", ""), spec.get("corner", "")
    what = f"{kind} at {entity}.{corner}"
    e = feature.entity(entity)
    if e is None or e.kind not in ("rect", "polygon"):
        raise FilletError(f"{what}: {entity!r} is not a rect or polygon")
    args = state.get(entity) or _solved(feature, coords, entity)
    valid = corner_names(e.kind, args)
    if corner not in valid:
        raise FilletError(f"{what}: the corners of {entity!r} are {', '.join(valid)}")
    radii = corner_spec(args.get("corners"), valid, what)
    bevels = corner_spec(args.get("chamfers"), valid, what)
    (radii if kind == "fillet" else bevels)[corner] = float(size)
    other = (bevels if kind == "fillet" else radii).pop(corner, None)
    if other is not None:  # the corner changes kind: what referenced its old cut goes with it
        old = f"{entity}.{corner}_{'chamfer' if kind == 'fillet' else 'arc'}"
        for c in feature.constraints:
            if any(r == old or r.startswith(old + ".") for r in c.refs):
                ops.append({"op": "delete_constraint", "sketch": feature.name, "constraint": c.name})
    new_args = {**args, "corners": _form(radii, valid), "chamfers": _form(bevels, valid)}
    try:
        cuts = macro_corners(e.kind, new_args, f"{e.kind} {entity!r}")
    except ValueError as exc:
        raise FilletError(str(exc)) from None
    sketch = feature.name
    for key in ("corners", "chamfers"):
        if new_args[key] != args.get(key):
            ops.append({"op": "set_entity_argument", "sketch": sketch, "entity": entity, "kwarg": key, "value": new_args[key]})
    state[entity] = new_args
    ref = f"{entity}.{corner}_{'arc' if kind == 'fillet' else 'chamfer'}"
    if dimension:
        corner_pt = dict(macro_corner_points(e.kind, new_args))[corner]
        if kind == "fillet":
            ops.append(_constraint_op(names.next("radius"), "radius", [ref], sketch, size, {"at": _label_beyond(cuts[corner]["center"], corner_pt, size)}))
        else:
            ops.append(_constraint_op(names.next("distance"), "distance", [f"{entity}.{corner}", f"{ref}.start"], sketch, size))
    return ref


def _form(values: dict[str, float], names: tuple[str, ...]) -> Any:
    """corners= as a number when every corner shares one size, a dict otherwise, None when empty."""
    if not values:
        return None
    vs = list(values.values())
    if len(values) == len(names) and all(abs(v - vs[0]) < 1e-9 for v in vs):
        return _num(vs[0])
    return {k: _num(values[k]) for k in names if k in values}


# --- taking apart --------------------------------------------------------------------------

_MACRO_CUT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z0-9_]+)_(arc|chamfer)$")


def unfillet_ops(feature: Feature, coords: dict[str, dict[str, Any]] | None, entity: str) -> list[dict[str, Any]]:
    """The batch that removes a fillet or chamfer: a macro corner loses its entry and the
    relations on its arc; a line-pair arc or bevel goes with its relations, the lines meet
    again at the sharp, what referenced the sharp references the corner, and the sharp goes."""
    sketch = feature.name
    m = _MACRO_CUT.match(entity)
    if m:
        name, corner, which = m.groups()
        e = feature.entity(name)
        if e is None or e.kind not in ("rect", "polygon"):
            raise FilletError(f"{name!r} is not a rect or polygon")
        args = _solved(feature, coords, name)
        valid = corner_names(e.kind, args)
        key = "corners" if which == "arc" else "chamfers"
        sizes = corner_spec(args.get(key), valid, entity)
        if corner not in sizes:
            raise FilletError(f"{entity} is not rounded" if which == "arc" else f"{entity} is not chamfered")
        del sizes[corner]
        ops: list[dict[str, Any]] = [{"op": "set_entity_argument", "sketch": sketch, "entity": name, "kwarg": key, "value": _form(sizes, valid)}]
        for c in feature.constraints:
            if any(r == entity or r.startswith(entity + ".") for r in c.refs):
                ops.append({"op": "delete_constraint", "sketch": sketch, "constraint": c.name})
        return ops
    e = feature.entity(entity)
    if e is None or e.kind not in ("arc", "line"):
        raise FilletError(f"{entity!r} is not a fillet arc or a chamfer line")
    joins = [c for c in feature.constraints if c.kind == "coincident" and any(r in (f"{entity}.start", f"{entity}.end") for r in c.refs)]
    ends: list[tuple[str, str]] = []  # (line, end) the fillet joins, in start/end order of the fillet
    for part in ("start", "end"):
        c = next((j for j in joins if f"{entity}.{part}" in j.refs), None)
        if c is None:
            raise FilletError(f"{entity!r} is not a fillet: its {part} joins nothing")
        other = next(r for r in c.refs if r != f"{entity}.{part}")
        ends.append(_line_end(feature, other, f"remove {entity}"))
    (la, ea), (lb, eb) = ends
    sharp = next((p.name for p in feature.entities if p.kind == "point"
                  and {la, lb} <= {c.refs[1] for c in feature.constraints if c.kind == "on" and c.refs[0] == p.name}), None)
    if sharp is None:
        raise FilletError(f"{entity!r} has no sharp point on both {la!r} and {lb!r}; it was not made by fillet")
    p = tuple(_solved(feature, coords, sharp)["at"])
    ops = []
    # what referenced the sharp references the corner again
    for c in feature.constraints:
        if c.kind == "on" and c.refs[0] == sharp:
            continue
        for i, r in enumerate(c.refs):
            if r == sharp:
                ops.append({"op": "set_constraint_argument", "sketch": sketch, "constraint": c.name, "kwarg": _positional_name(c, i), "value": f"{la}.{ea}"})
    for c in feature.constraints:
        if any(r == entity or r.startswith(entity + ".") for r in c.refs) or (c.kind == "on" and c.refs[0] == sharp):
            ops.append({"op": "delete_constraint", "sketch": sketch, "constraint": c.name})
    ops.append({"op": "delete_sketch_entity", "sketch": sketch, "entity": entity})
    ops.append({"op": "delete_sketch_entity", "sketch": sketch, "entity": sharp})
    ops.append({"op": "set_entity_argument", "sketch": sketch, "entity": la, "kwarg": ea, "value": _round(p)})
    ops.append({"op": "set_entity_argument", "sketch": sketch, "entity": lb, "kwarg": eb, "value": _round(p)})
    ops.append(_constraint_op(Names(feature).next("coincident"), "coincident", [f"{la}.{ea}", f"{lb}.{eb}"], sketch))
    return ops
