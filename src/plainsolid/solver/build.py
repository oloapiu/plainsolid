"""From a sketch Feature to a solver System and back to solved coordinates."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..model import Constraint, Entity, Feature
from . import system as S
from .system import SketchError


@dataclass
class ProjItem:
    """One piece of projected body geometry in sketch-local 2D."""

    kind: str  # line | circle | arc | point
    coords: dict[str, Any]


@dataclass
class Projected:
    name: str
    items: list[ProjItem]

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "items": [{"kind": i.kind, **i.coords} for i in self.items]}


@dataclass
class SketchSolution:
    coords: dict[str, dict[str, Any]]
    dof: int
    rank: int
    redundant: list[str]
    conflicting: list[str]
    residual: float
    free_entities: list[str]
    projected: dict[str, Projected] = field(default_factory=dict)
    ms: float = 0.0
    free_variables: dict[str, list[str]] = field(default_factory=dict)  # per free entity, which of its variables are loose

    @property
    def fully_constrained(self) -> bool:
        return self.dof == 0

    def to_json(self) -> dict[str, Any]:
        return {
            "coords": self.coords, "dof": self.dof, "rank": self.rank, "redundant": self.redundant,
            "conflicting": self.conflicting, "residual": self.residual, "free_entities": self.free_entities,
            "fully_constrained": self.fully_constrained, "free_variables": self.free_variables,
            "projected": {k: v.to_json() for k, v in self.projected.items()}, "ms": round(self.ms, 2),
        }


class Layout:
    """Where each entity's variables live, and every named reference."""

    def __init__(self) -> None:
        self.system = S.System()
        self.refs: dict[str, Any] = {}
        self.entity_vars: dict[str, list[int]] = {}
        self.extract: dict[str, Any] = {}  # entity -> callable(x) -> solved args
        self.kinds: dict[str, str] = {}
        self.var_names: dict[int, str] = {}  # variable index -> "center.x", "width", "angle", ... for reports

    def name_vars(self, **named: int | S.PointRef) -> None:
        """Record what each variable of an entity means, a point contributing .x and .y."""
        for name, v in named.items():
            if isinstance(v, S.PointRef):
                self.var_names[v.ix], self.var_names[v.iy] = f"{name}.x", f"{name}.y"
            else:
                self.var_names[int(v)] = name

    def _ref(self, name: str, what: str):
        if name in self.refs:
            return self.refs[name]
        raise SketchError(f"{what}: unknown reference {name!r}")

    def point(self, name: str, what: str) -> S.PointRef:
        r = self._ref(name, what)
        if isinstance(r, S.PointRef):
            return r
        if isinstance(r, S.CircleRef):
            return r.center  # a circle's centre stands in for it where a point is needed
        raise SketchError(f"{what}: {name!r} is not a point (it is a {type(r).__name__.replace('Ref', '').lower()})")

    def line(self, name: str, what: str) -> S.LineRef:
        r = self._ref(name, what)
        if isinstance(r, S.LineRef):
            return r
        raise SketchError(f"{what}: {name!r} is not a line")

    def circle(self, name: str, what: str) -> S.CircleRef:
        r = self._ref(name, what)
        if isinstance(r, S.CircleRef):
            return r
        raise SketchError(f"{what}: {name!r} is not a circle or arc")

    def kind_of(self, name: str) -> str:
        r = self.refs.get(name)
        if isinstance(r, S.LineRef):
            return "line"
        if isinstance(r, S.CircleRef):
            return "circle"
        if isinstance(r, S.PointRef):
            return "point"
        if isinstance(r, S.ScalarRef):
            return "scalar"
        return "?"


def _add_entity(lay: Layout, e: Entity, projected: dict[str, Projected]) -> None:
    sy = lay.system
    a = e.args
    n = e.name
    if e.kind == "point":
        p = sy.point(*a["at"])
        lay.refs[n] = p
        lay.entity_vars[n] = [p.ix, p.iy]
        lay.var_names[p.ix], lay.var_names[p.iy] = "x", "y"
        lay.extract[n] = lambda x, p=p: {"at": p.pos(x)}
    elif e.kind == "line":
        p, q = sy.point(*a["start"]), sy.point(*a["end"])
        lay.refs[n] = S.LineRef(p, q)
        lay.refs[f"{n}.start"], lay.refs[f"{n}.end"], lay.refs[f"{n}.mid"] = p, q, S.MidPoint(p, q)
        lay.entity_vars[n] = [p.ix, p.iy, q.ix, q.iy]
        lay.name_vars(start=p, end=q)
        lay.extract[n] = lambda x, p=p, q=q: {"start": p.pos(x), "end": q.pos(x)}
    elif e.kind == "circle":
        c = sy.point(*a["at"])
        r = S.FreeScalar(sy.var(a["diameter"] / 2))
        lay.refs[n] = S.CircleRef(c, r)
        lay.refs[f"{n}.center"] = c
        lay.entity_vars[n] = [c.ix, c.iy, r.i]
        lay.name_vars(center=c, radius=r.i)
        lay.extract[n] = lambda x, c=c, r=r: {"at": c.pos(x), "diameter": 2 * r.value(x)}
    elif e.kind == "arc":
        c, p, q = sy.point(*a["center"]), sy.point(*a["start"]), sy.point(*a["end"])
        lay.refs[n] = S.CircleRef(c, S.DistanceScalar(c, p))
        lay.refs[f"{n}.center"], lay.refs[f"{n}.start"], lay.refs[f"{n}.end"] = c, p, q
        lay.entity_vars[n] = [c.ix, c.iy, p.ix, p.iy, q.ix, q.iy]
        lay.name_vars(center=c, start=p, end=q)
        sy.add(S.EqualDistance(f"{n}.__arc", c, p, q))
        lay.extract[n] = lambda x, c=c, p=p, q=q: {"center": c.pos(x), "start": p.pos(x), "end": q.pos(x)}
    elif e.kind == "rect":
        c = sy.point(*a["at"])
        w, h = sy.var(a["width"]), sy.var(a["height"])
        corner = lambda sx, sy_: S.AffinePoint(c, [(w, 0.5 * sx, 0.0), (h, 0.0, 0.5 * sy_)])
        tl, tr, bl, br = corner(-1, 1), corner(1, 1), corner(-1, -1), corner(1, -1)
        lay.refs.update({n: S.LineRef(bl, br), f"{n}.center": c, f"{n}.tl": tl, f"{n}.tr": tr, f"{n}.bl": bl,
                         f"{n}.br": br, f"{n}.top": S.LineRef(tl, tr), f"{n}.bottom": S.LineRef(bl, br),
                         f"{n}.left": S.LineRef(bl, tl), f"{n}.right": S.LineRef(br, tr),
                         f"{n}.width": S.FreeScalar(w), f"{n}.height": S.FreeScalar(h)})
        lay.entity_vars[n] = [c.ix, c.iy, w, h]
        lay.name_vars(center=c, width=w, height=h)
        lay.extract[n] = lambda x, c=c, w=w, h=h: {"at": c.pos(x), "width": float(x[w]), "height": float(x[h])}
    elif e.kind == "slot":
        c = sy.point(*a["at"])
        L, W = sy.var(a["length"]), sy.var(a["width"])
        t = sy.var(math.radians(a["angle"]))
        half = S.LinearScalar([(L, 0.5), (W, -0.5)])
        p, q = S.PolarPoint(c, half, t, -1.0), S.PolarPoint(c, half, t, 1.0)
        rad = S.FreeScalar(W, 0.5)
        lay.refs.update({n: S.LineRef(p, q), f"{n}.center": c, f"{n}.start": p, f"{n}.end": q,
                         f"{n}.axis": S.LineRef(p, q), f"{n}.start_arc": S.CircleRef(p, rad),
                         f"{n}.end_arc": S.CircleRef(q, rad), f"{n}.width": S.FreeScalar(W),
                         f"{n}.length": S.FreeScalar(L)})
        lay.entity_vars[n] = [c.ix, c.iy, L, W, t]
        lay.name_vars(center=c, length=L, width=W, angle=t)
        lay.extract[n] = lambda x, c=c, L=L, W=W, t=t: {"at": c.pos(x), "length": float(x[L]), "width": float(x[W]),
                                                        "angle": math.degrees(float(x[t]))}
    elif e.kind == "polygon":
        pts = [sy.point(*p) for p in a["points"]]
        for i, p in enumerate(pts):
            lay.refs[f"{n}.p{i}"] = p
            lay.refs[f"{n}.e{i}"] = S.LineRef(p, pts[(i + 1) % len(pts)])
            lay.name_vars(**{f"p{i}": p})
        lay.refs[n] = S.LineRef(pts[0], pts[1])
        lay.entity_vars[n] = [v for p in pts for v in (p.ix, p.iy)]
        lay.extract[n] = lambda x, pts=pts: {"points": [p.pos(x) for p in pts]}
    elif e.kind in ("project", "offset"):
        pr = projected.get(n)
        if pr is None:
            raise SketchError(f"{e.kind} {n!r} was not resolved")
        for i, item in enumerate(pr.items):
            names = [n] if len(pr.items) == 1 else [f"{n}.e{i}"]
            if len(pr.items) > 1 and i == 0:
                names.append(n)
            for nm in names:
                _add_const_item(lay, nm, item)
        lay.entity_vars[n] = []
        lay.extract[n] = lambda x: {}
    else:
        raise SketchError(f"unknown entity kind {e.kind!r}")
    lay.kinds[n] = e.kind


def _add_const_item(lay: Layout, name: str, item: ProjItem) -> None:
    c = item.coords
    if item.kind == "line":
        p, q = S.ConstPoint(*c["start"]), S.ConstPoint(*c["end"])
        lay.refs[name] = S.LineRef(p, q)
        lay.refs[f"{name}.start"], lay.refs[f"{name}.end"], lay.refs[f"{name}.mid"] = p, q, S.MidPoint(p, q)
    elif item.kind == "circle":
        cp = S.ConstPoint(*c["center"])
        lay.refs[name] = S.CircleRef(cp, S.ConstScalar(c["radius"]))
        lay.refs[f"{name}.center"] = cp
    elif item.kind == "arc":
        cp = S.ConstPoint(*c["center"])
        lay.refs[name] = S.CircleRef(cp, S.ConstScalar(c["radius"]))
        lay.refs[f"{name}.center"] = cp
        lay.refs[f"{name}.start"], lay.refs[f"{name}.end"] = S.ConstPoint(*c["start"]), S.ConstPoint(*c["end"])
    elif item.kind == "point":
        lay.refs[name] = S.ConstPoint(*c["at"])
    else:
        raise SketchError(f"cannot use projected {item.kind!r} in constraints")


def _sign(v: float) -> float:
    return -1.0 if v < 0 else 1.0


def _add_constraint(lay: Layout, c: Constraint, x0: np.ndarray) -> None:
    sy = lay.system
    what = f"{c.kind} {c.name!r}"
    k, r, v = c.kind, c.refs, c.value

    def need(n: int) -> None:
        if len(r) != n:
            raise SketchError(f"{what} needs {n} reference(s), got {len(r)}")

    if k == "coincident":
        need(2)
        ka, kb = lay.kind_of(r[0]), lay.kind_of(r[1])
        if ka == "point" and kb == "point":
            sy.add(S.Coincident(c.name, lay.point(r[0], what), lay.point(r[1], what)))
        elif ka == "point":
            _on(lay, c.name, r[0], r[1], what)
        elif kb == "point":
            _on(lay, c.name, r[1], r[0], what)
        else:
            raise SketchError(f"{what}: needs two points, or a point and a curve")
    elif k in ("horizontal", "vertical"):
        cls = S.Horizontal if k == "horizontal" else S.Vertical
        if len(r) == 1:
            ln = lay.line(r[0], what)
            sy.add(cls(c.name, ln.a, ln.b))
        else:
            need(2)
            sy.add(cls(c.name, lay.point(r[0], what), lay.point(r[1], what)))
    elif k in ("parallel", "perpendicular"):
        need(2)
        cls = S.Parallel if k == "parallel" else S.Perpendicular
        sy.add(cls(c.name, lay.line(r[0], what), lay.line(r[1], what)))
    elif k == "equal":
        need(2)
        ka, kb = lay.kind_of(r[0]), lay.kind_of(r[1])
        if ka == "line" and kb == "line":
            sy.add(S.EqualLength(c.name, lay.line(r[0], what), lay.line(r[1], what)))
        elif ka == "circle" and kb == "circle":
            sy.add(S.EqualRadius(c.name, lay.circle(r[0], what), lay.circle(r[1], what)))
        else:
            raise SketchError(f"{what}: needs two lines or two circles/arcs")
    elif k == "tangent":
        need(2)
        ka, kb = lay.kind_of(r[0]), lay.kind_of(r[1])
        if ka == "circle" and kb == "circle":
            sy.add(S.TangentCircles(c.name, lay.circle(r[0], what), lay.circle(r[1], what), bool(c.options.get("inside"))))
        else:
            line_name, circ_name = (r[0], r[1]) if ka == "line" else (r[1], r[0])
            ln, ci = lay.line(line_name, what), lay.circle(circ_name, what)
            sign = _sign(S._point_line(ci.center, ln, x0)[0])
            sy.add(S.Tangent(c.name, ln, ci, sign))
    elif k == "concentric":
        need(2)
        sy.add(S.Coincident(c.name, lay.circle(r[0], what).center, lay.circle(r[1], what).center))
    elif k == "coradial":
        need(2)
        a, b = lay.circle(r[0], what), lay.circle(r[1], what)
        sy.add(S.Composite(c.name, [S.Coincident(c.name, a.center, b.center), S.EqualRadius(c.name, a, b)]))
    elif k == "colinear":
        need(2)
        a, b = lay.line(r[0], what), lay.line(r[1], what)
        sy.add(S.Composite(c.name, [S.PointOnLine(c.name, a.a, b), S.PointOnLine(c.name, a.b, b)]))
    elif k == "symmetric":
        need(3)
        sy.add(S.Symmetric(c.name, lay.point(r[0], what), lay.point(r[1], what), lay.line(r[2], what)))
    elif k == "midpoint":
        need(2)
        sy.add(S.Midpoint(c.name, lay.point(r[0], what), lay.line(r[1], what)))
    elif k == "on":
        need(2)
        _on(lay, c.name, r[0], r[1], what)
    elif k == "fix":
        need(1)
        sy.add(S.Composite(c.name, _fix_parts(lay, r[0], x0, what)))
    elif k == "distance":
        need(2)
        ka, kb = lay.kind_of(r[0]), lay.kind_of(r[1])
        along = c.options.get("along")
        if ka == "point" and kb == "point":
            p, q = lay.point(r[0], what), lay.point(r[1], what)
            sign = 1.0
            if along:
                i = 0 if along == "x" else 1
                sign = _sign(q.pos(x0)[i] - p.pos(x0)[i])
            sy.add(S.Distance(c.name, p, q, v, along, sign))
        elif "point" in (ka, kb):
            pn, ln_ = (r[0], r[1]) if ka == "point" else (r[1], r[0])
            p, ln = lay.point(pn, what), lay.line(ln_, what)
            sy.add(S.PointLineDistance(c.name, p, ln, v, _sign(S._point_line(p, ln, x0)[0])))
        elif ka == "line" and kb == "line":
            l1, l2 = lay.line(r[0], what), lay.line(r[1], what)
            sy.add(S.PointLineDistance(c.name, l2.a, l1, v, _sign(S._point_line(l2.a, l1, x0)[0])))
        else:
            raise SketchError(f"{what}: needs two points, a point and a line, or two lines")
    elif k == "length":
        need(1)
        ref = lay.refs.get(r[0])
        if isinstance(ref, S.ScalarRef):
            # a size of a macro: slot1.length (tip to tip), slot1.width, rect1.width, rect1.height
            sy.add(S.FixScalar(c.name, ref, v))
        else:
            sy.add(S.Length(c.name, lay.line(r[0], what), v))
    elif k in ("diameter", "radius"):
        need(1)
        ci = lay.circle(r[0], what)
        sy.add(S.FixScalar(c.name, ci.radius, v / 2 if k == "diameter" else v))
    elif k == "angle":
        need(2)
        sy.add(S.Angle(c.name, lay.line(r[0], what), lay.line(r[1], what), v))
    else:
        raise SketchError(f"unknown constraint kind {k!r}")


def _on(lay: Layout, name: str, point: str, curve: str, what: str) -> None:
    p = lay.point(point, what)
    kc = lay.kind_of(curve)
    if kc == "line":
        lay.system.add(S.PointOnLine(name, p, lay.line(curve, what)))
    elif kc == "circle":
        lay.system.add(S.PointOnCircle(name, p, lay.circle(curve, what)))
    else:
        raise SketchError(f"{what}: {curve!r} is not a line, circle or arc")


def _fix_parts(lay: Layout, target: str, x0: np.ndarray, what: str) -> list[S.Constraint]:
    ref = lay.refs.get(target)
    kind = lay.kinds.get(target)
    parts: list[S.Constraint] = []
    if kind in ("rect", "slot", "polygon", "arc", "line", "circle", "point") and "." not in target:
        for v in lay.entity_vars[target]:
            parts.append(S.FixScalar(what, S.FreeScalar(v), float(x0[v])))
        return parts
    if isinstance(ref, S.PointRef):
        return [S.FixPoint(what, ref, ref.pos(x0))]
    if isinstance(ref, S.LineRef):
        return [S.FixPoint(what, ref.a, ref.a.pos(x0)), S.FixPoint(what, ref.b, ref.b.pos(x0))]
    if isinstance(ref, S.CircleRef):
        return [S.FixPoint(what, ref.center, ref.center.pos(x0)), S.FixScalar(what, ref.radius, ref.radius.value(x0))]
    raise SketchError(f"{what}: unknown reference {target!r}")


def build(feature: Feature, projected: dict[str, Projected] | None = None) -> Layout:
    lay = Layout()
    projected = projected or {}
    for e in feature.entities:
        _add_entity(lay, e, projected)
    x0 = lay.system.initial()
    for c in feature.constraints:
        _add_constraint(lay, c, x0)
    return lay


def _rim_target(circle: S.CircleRef, tx: float, ty: float):
    """`|centre - cursor| - radius` and its gradient: zero when the rim passes through the cursor."""
    def value(x: np.ndarray) -> float:
        cx, cy = circle.center.pos(x)
        return math.hypot(cx - tx, cy - ty) - circle.radius.value(x)

    def grad(x: np.ndarray) -> S.Row:
        cx, cy = circle.center.pos(x)
        d = math.hypot(cx - tx, cy - ty) or 1.0
        jx, jy = circle.center.jac(x)
        out: dict[int, float] = {}
        for k, v in jx.items():
            out[k] = out.get(k, 0.0) + v * (cx - tx) / d
        for k, v in jy.items():
            out[k] = out.get(k, 0.0) + v * (cy - ty) / d
        for k, v in circle.radius.jac(x).items():
            out[k] = out.get(k, 0.0) - v
        return out
    return value, grad


def solve_sketch(feature: Feature, projected: dict[str, Projected] | None = None,
                 drag: dict[str, tuple[float, float]] | None = None) -> SketchSolution:
    """Solve a sketch from the coordinates in the file; with drag, move named
    points toward targets first. Returns solved coordinates per entity."""
    lay = build(feature, projected)
    sy = lay.system
    if drag:
        base = sy.solve()
        targets = []
        scalars = []
        for name, (tx, ty) in drag.items():
            if name.endswith(".rim"):
                # a circle dragged by its curve: the radius follows the cursor and the centre is held
                # where it is (else the least-norm step would split the motion between the two)
                circle = lay.circle(name[:-4], f"drag {name!r}")
                scalars.append(_rim_target(circle, float(tx), float(ty)))
                targets.append((circle.center, circle.center.pos(base.x)))
                continue
            targets.append((lay.point(name, f"drag {name!r}"), (float(tx), float(ty))))
        sol = sy.drag(base.x, targets, scalars=scalars)
    else:
        sol = sy.solve()
    free_vars = set(sol.free_vars)
    coords = {name: fn(sol.x) for name, fn in lay.extract.items()}
    free_entities = [name for name, vars_ in lay.entity_vars.items() if any(v in free_vars for v in vars_)]
    free_variables = {name: [lay.var_names.get(v, f"v{v}") for v in lay.entity_vars[name] if v in free_vars]
                      for name in free_entities}
    return SketchSolution(coords, sol.dof, sol.rank, sol.redundant, sol.conflicting, sol.residual,
                          free_entities, dict(projected or {}), sol.ms, free_variables)
