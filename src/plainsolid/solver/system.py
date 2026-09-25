"""The numerical core: variables, references, constraints, solve, drag, analysis.

Every constraint expresses its residual through point references (2D points
that may be free variables, constants, or functions of other variables such
as a rectangle corner) and scalar references (a radius). Jacobians are
chained through those references, so a rigid macro and a free line share
the same constraint code.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np
from scipy.optimize import least_squares

Row = dict[int, float]  # sparse Jacobian row: variable index -> coefficient


class SketchError(ValueError):
    pass


# --- references ---------------------------------------------------------------

class PointRef:
    def pos(self, x: np.ndarray) -> tuple[float, float]:
        raise NotImplementedError

    def jac(self, x: np.ndarray) -> tuple[Row, Row]:
        """d(px)/d(vars), d(py)/d(vars)."""
        raise NotImplementedError

    @property
    def is_free(self) -> bool:
        return False


@dataclass
class FreePoint(PointRef):
    ix: int
    iy: int

    def pos(self, x):
        return (float(x[self.ix]), float(x[self.iy]))

    def jac(self, x):
        return ({self.ix: 1.0}, {self.iy: 1.0})

    @property
    def is_free(self):
        return True


@dataclass
class ConstPoint(PointRef):
    px: float
    py: float

    def pos(self, x):
        return (self.px, self.py)

    def jac(self, x):
        return ({}, {})


@dataclass
class AffinePoint(PointRef):
    """base + sum(var * offset): rectangle corners and edge midpoints."""

    base: PointRef
    terms: list[tuple[int, float, float]] = field(default_factory=list)  # (var, dx, dy)
    const: tuple[float, float] = (0.0, 0.0)

    def pos(self, x):
        bx, by = self.base.pos(x)
        px, py = bx + self.const[0], by + self.const[1]
        for var, dx, dy in self.terms:
            px += x[var] * dx
            py += x[var] * dy
        return (float(px), float(py))

    def jac(self, x):
        jx, jy = self.base.jac(x)
        jx, jy = dict(jx), dict(jy)
        for var, dx, dy in self.terms:
            if dx:
                jx[var] = jx.get(var, 0.0) + dx
            if dy:
                jy[var] = jy.get(var, 0.0) + dy
        return (jx, jy)


@dataclass
class PolarPoint(PointRef):
    """base + sign * radius(x) * (cos t, sin t) with t a variable: slot arc centres."""

    base: PointRef
    radius: ScalarRef
    it: int
    sign: float = 1.0

    def pos(self, x):
        bx, by = self.base.pos(x)
        r = self.radius.value(x) * self.sign
        t = x[self.it]
        return (float(bx + r * math.cos(t)), float(by + r * math.sin(t)))

    def jac(self, x):
        jx, jy = self.base.jac(x)
        jx, jy = dict(jx), dict(jy)
        r = self.radius.value(x) * self.sign
        t = x[self.it]
        c, s = math.cos(t), math.sin(t)
        for var, dr in self.radius.jac(x).items():
            jx[var] = jx.get(var, 0.0) + self.sign * dr * c
            jy[var] = jy.get(var, 0.0) + self.sign * dr * s
        jx[self.it] = jx.get(self.it, 0.0) - r * s
        jy[self.it] = jy.get(self.it, 0.0) + r * c
        return (jx, jy)


@dataclass
class RigidPoint(PointRef):
    """base + R(t) (lx, ly) with t a variable: a point of an imported DXF outline, which
    moves and turns as one piece about the file's origin."""

    base: PointRef
    it: int
    lx: float
    ly: float

    def pos(self, x):
        bx, by = self.base.pos(x)
        c, s = math.cos(x[self.it]), math.sin(x[self.it])
        return (float(bx + c * self.lx - s * self.ly), float(by + s * self.lx + c * self.ly))

    def jac(self, x):
        jx, jy = self.base.jac(x)
        jx, jy = dict(jx), dict(jy)
        c, s = math.cos(x[self.it]), math.sin(x[self.it])
        jx[self.it] = jx.get(self.it, 0.0) - s * self.lx - c * self.ly
        jy[self.it] = jy.get(self.it, 0.0) + c * self.lx - s * self.ly
        return (jx, jy)


@dataclass
class DerivedPoint(PointRef):
    """A point computed from some variables by any function, differentiated numerically:
    the centre and tangent points of a polygon corner's fillet, which depend on three
    corners and the radius through a bisector."""

    deps: list[int]
    fn: Any  # (x) -> (px, py)

    def pos(self, x):
        px, py = self.fn(x)
        return (float(px), float(py))

    def jac(self, x):
        jx: Row = {}
        jy: Row = {}
        h = 1e-6
        xx = np.array(x, float)
        for i in self.deps:
            xx[i] = x[i] + h
            px1, py1 = self.fn(xx)
            xx[i] = x[i] - h
            px0, py0 = self.fn(xx)
            xx[i] = x[i]
            jx[i] = (px1 - px0) / (2 * h)
            jy[i] = (py1 - py0) / (2 * h)
        return (jx, jy)


@dataclass
class MidPoint(PointRef):
    a: PointRef
    b: PointRef

    def pos(self, x):
        ax, ay = self.a.pos(x)
        bx, by = self.b.pos(x)
        return ((ax + bx) / 2, (ay + by) / 2)

    def jac(self, x):
        ax, ay = self.a.jac(x)
        bx, by = self.b.jac(x)
        return (_add(_scale(ax, 0.5), _scale(bx, 0.5)), _add(_scale(ay, 0.5), _scale(by, 0.5)))


class ScalarRef:
    def value(self, x: np.ndarray) -> float:
        raise NotImplementedError

    def jac(self, x: np.ndarray) -> Row:
        raise NotImplementedError


@dataclass
class FreeScalar(ScalarRef):
    i: int
    scale: float = 1.0

    def value(self, x):
        return float(x[self.i] * self.scale)

    def jac(self, x):
        return {self.i: self.scale}


@dataclass
class ConstScalar(ScalarRef):
    v: float

    def value(self, x):
        return self.v

    def jac(self, x):
        return {}


@dataclass
class DistanceScalar(ScalarRef):
    """|b - a|: the radius of an arc given by centre and start point."""

    a: PointRef
    b: PointRef

    def value(self, x):
        return _norm(_vsub(self.b.pos(x), self.a.pos(x)))

    def jac(self, x):
        return _len_grad(self.a, self.b, x)[1]


@dataclass
class LinearScalar(ScalarRef):
    """sum(coeff * var) + const: a slot's half centre distance (L - W) / 2."""

    terms: list[tuple[int, float]]
    const: float = 0.0

    def value(self, x):
        return float(self.const + sum(x[i] * k for i, k in self.terms))

    def jac(self, x):
        out: Row = {}
        for i, k in self.terms:
            out[i] = out.get(i, 0.0) + k
        return out


@dataclass
class LineRef:
    a: PointRef
    b: PointRef


@dataclass
class CircleRef:
    center: PointRef
    radius: ScalarRef


def _add(a: Row, b: Row) -> Row:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def _scale(a: Row, s: float) -> Row:
    return {k: v * s for k, v in a.items()}


def _chain(p: PointRef, x: np.ndarray, dx: float, dy: float) -> Row:
    """Row contribution of d(residual)/d(point) = (dx, dy) through the point's Jacobian."""
    jx, jy = p.jac(x)
    out: Row = {}
    if dx:
        for k, v in jx.items():
            out[k] = out.get(k, 0.0) + dx * v
    if dy:
        for k, v in jy.items():
            out[k] = out.get(k, 0.0) + dy * v
    return out


def _vsub(p, q):
    return (p[0] - q[0], p[1] - q[1])


def _norm(v):
    return math.hypot(v[0], v[1])


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


# --- constraints -----------------------------------------------------------------

class Constraint:
    rows: ClassVar[int] = 1
    implicit: ClassVar[bool] = False  # internal consistency, never reported as redundant
    name: str

    def residual(self, x: np.ndarray) -> list[float]:
        raise NotImplementedError

    def jacobian(self, x: np.ndarray) -> list[Row]:
        raise NotImplementedError


@dataclass
class Coincident(Constraint):
    name: str
    p: PointRef
    q: PointRef
    rows = 2

    def residual(self, x):
        a, b = self.p.pos(x), self.q.pos(x)
        return [a[0] - b[0], a[1] - b[1]]

    def jacobian(self, x):
        return [_add(_chain(self.p, x, 1, 0), _chain(self.q, x, -1, 0)),
                _add(_chain(self.p, x, 0, 1), _chain(self.q, x, 0, -1))]


@dataclass
class Horizontal(Constraint):
    name: str
    p: PointRef
    q: PointRef

    def residual(self, x):
        return [self.p.pos(x)[1] - self.q.pos(x)[1]]

    def jacobian(self, x):
        return [_add(_chain(self.p, x, 0, 1), _chain(self.q, x, 0, -1))]


@dataclass
class Vertical(Constraint):
    name: str
    p: PointRef
    q: PointRef

    def residual(self, x):
        return [self.p.pos(x)[0] - self.q.pos(x)[0]]

    def jacobian(self, x):
        return [_add(_chain(self.p, x, 1, 0), _chain(self.q, x, -1, 0))]


@dataclass
class Parallel(Constraint):
    name: str
    l1: LineRef
    l2: LineRef

    def residual(self, x):
        d1 = _vsub(self.l1.b.pos(x), self.l1.a.pos(x))
        d2 = _vsub(self.l2.b.pos(x), self.l2.a.pos(x))
        return [_cross(d1, d2)]

    def jacobian(self, x):
        d1 = _vsub(self.l1.b.pos(x), self.l1.a.pos(x))
        d2 = _vsub(self.l2.b.pos(x), self.l2.a.pos(x))
        # cross(d1, d2) = d1x*d2y - d1y*d2x
        row = _chain(self.l1.b, x, d2[1], -d2[0])
        row = _add(row, _chain(self.l1.a, x, -d2[1], d2[0]))
        row = _add(row, _chain(self.l2.b, x, -d1[1], d1[0]))
        row = _add(row, _chain(self.l2.a, x, d1[1], -d1[0]))
        return [row]


@dataclass
class Perpendicular(Constraint):
    name: str
    l1: LineRef
    l2: LineRef

    def residual(self, x):
        d1 = _vsub(self.l1.b.pos(x), self.l1.a.pos(x))
        d2 = _vsub(self.l2.b.pos(x), self.l2.a.pos(x))
        return [_dot(d1, d2)]

    def jacobian(self, x):
        d1 = _vsub(self.l1.b.pos(x), self.l1.a.pos(x))
        d2 = _vsub(self.l2.b.pos(x), self.l2.a.pos(x))
        row = _chain(self.l1.b, x, d2[0], d2[1])
        row = _add(row, _chain(self.l1.a, x, -d2[0], -d2[1]))
        row = _add(row, _chain(self.l2.b, x, d1[0], d1[1]))
        row = _add(row, _chain(self.l2.a, x, -d1[0], -d1[1]))
        return [row]


def _len_grad(a: PointRef, b: PointRef, x) -> tuple[float, Row]:
    """|b - a| and its row."""
    d = _vsub(b.pos(x), a.pos(x))
    n = _norm(d)
    if n < 1e-12:
        return 0.0, {}
    ux, uy = d[0] / n, d[1] / n
    return n, _add(_chain(b, x, ux, uy), _chain(a, x, -ux, -uy))


@dataclass
class EqualLength(Constraint):
    name: str
    l1: LineRef
    l2: LineRef

    def residual(self, x):
        return [_len_grad(self.l1.a, self.l1.b, x)[0] - _len_grad(self.l2.a, self.l2.b, x)[0]]

    def jacobian(self, x):
        return [_add(_len_grad(self.l1.a, self.l1.b, x)[1], _scale(_len_grad(self.l2.a, self.l2.b, x)[1], -1))]


@dataclass
class EqualRadius(Constraint):
    name: str
    c1: CircleRef
    c2: CircleRef

    def residual(self, x):
        return [self.c1.radius.value(x) - self.c2.radius.value(x)]

    def jacobian(self, x):
        return [_add(self.c1.radius.jac(x), _scale(self.c2.radius.jac(x), -1))]


@dataclass
class Length(Constraint):
    name: str
    line: LineRef
    value: float

    def residual(self, x):
        return [_len_grad(self.line.a, self.line.b, x)[0] - self.value]

    def jacobian(self, x):
        return [_len_grad(self.line.a, self.line.b, x)[1]]


@dataclass
class Distance(Constraint):
    """Point to point distance, optionally along one axis (signed by the initial layout)."""

    name: str
    p: PointRef
    q: PointRef
    value: float
    along: str | None = None  # None | "x" | "y"
    sign: float = 1.0

    def residual(self, x):
        if self.along == "x":
            return [self.sign * (self.q.pos(x)[0] - self.p.pos(x)[0]) - self.value]
        if self.along == "y":
            return [self.sign * (self.q.pos(x)[1] - self.p.pos(x)[1]) - self.value]
        return [_len_grad(self.p, self.q, x)[0] - self.value]

    def jacobian(self, x):
        if self.along == "x":
            return [_add(_chain(self.q, x, self.sign, 0), _chain(self.p, x, -self.sign, 0))]
        if self.along == "y":
            return [_add(_chain(self.q, x, 0, self.sign), _chain(self.p, x, 0, -self.sign))]
        return [_len_grad(self.p, self.q, x)[1]]


def _point_line(p: PointRef, line: LineRef, x) -> tuple[float, Row]:
    """Signed distance of p from the infinite line a-b, and its row."""
    a, b = line.a.pos(x), line.b.pos(x)
    pp = p.pos(x)
    d = _vsub(b, a)
    n = _norm(d)
    if n < 1e-12:
        return 0.0, {}
    w = _vsub(pp, a)
    c = _cross(d, w)  # d x (p - a)
    dist = c / n
    # d dist / d p = (-dy, dx)/n ; d/d b = ((w_y), -(w_x))/n - dist*d/n^2 ; d/d a = -(d/dp) - (d/db)
    dp = (-d[1] / n, d[0] / n)
    db = (w[1] / n - dist * d[0] / (n * n), -w[0] / n - dist * d[1] / (n * n))
    da = (-dp[0] - db[0], -dp[1] - db[1])
    row = _chain(p, x, *dp)
    row = _add(row, _chain(line.b, x, *db))
    row = _add(row, _chain(line.a, x, *da))
    return dist, row


@dataclass
class PointLineDistance(Constraint):
    name: str
    p: PointRef
    line: LineRef
    value: float
    sign: float = 1.0

    def residual(self, x):
        return [self.sign * _point_line(self.p, self.line, x)[0] - self.value]

    def jacobian(self, x):
        return [_scale(_point_line(self.p, self.line, x)[1], self.sign)]


@dataclass
class PointOnLine(Constraint):
    name: str
    p: PointRef
    line: LineRef

    def residual(self, x):
        return [_point_line(self.p, self.line, x)[0]]

    def jacobian(self, x):
        return [_point_line(self.p, self.line, x)[1]]


@dataclass
class PointOnCircle(Constraint):
    name: str
    p: PointRef
    c: CircleRef

    def residual(self, x):
        return [_len_grad(self.c.center, self.p, x)[0] - self.c.radius.value(x)]

    def jacobian(self, x):
        return [_add(_len_grad(self.c.center, self.p, x)[1], _scale(self.c.radius.jac(x), -1))]


@dataclass
class Tangent(Constraint):
    """Line tangent to a circle: distance from the centre to the line equals the radius."""

    name: str
    line: LineRef
    c: CircleRef
    sign: float = 1.0

    def residual(self, x):
        return [self.sign * _point_line(self.c.center, self.line, x)[0] - self.c.radius.value(x)]

    def jacobian(self, x):
        return [_add(_scale(_point_line(self.c.center, self.line, x)[1], self.sign),
                     _scale(self.c.radius.jac(x), -1))]


@dataclass
class TangentAt(Constraint):
    """A line tangent to an arc at the arc's end that sits on it (a fillet): the radius vector
    there is perpendicular to the line. The distance form has a vanishing gradient in that
    configuration, a double root, and would count as redundant."""

    name: str
    line: LineRef
    center: PointRef
    at: PointRef

    def _parts(self, x):
        a, b = self.line.a.pos(x), self.line.b.pos(x)
        d = _vsub(b, a)
        n = _norm(d)
        v = _vsub(self.center.pos(x), self.at.pos(x))
        return d, n, v

    def residual(self, x):
        d, n, v = self._parts(x)
        if n < 1e-12:
            return [0.0]
        return [_dot(v, d) / n]

    def jacobian(self, x):
        d, n, v = self._parts(x)
        if n < 1e-12:
            return [{}]
        u = (d[0] / n, d[1] / n)
        f = _dot(v, u)
        w = ((v[0] - f * u[0]) / n, (v[1] - f * u[1]) / n)  # d(v . u)/d(b) with u = d/|d|
        row = _chain(self.center, x, u[0], u[1])
        row = _add(row, _chain(self.at, x, -u[0], -u[1]))
        row = _add(row, _chain(self.line.b, x, w[0], w[1]))
        row = _add(row, _chain(self.line.a, x, -w[0], -w[1]))
        return [row]


@dataclass
class TangentCircles(Constraint):
    """Two circles tangent: centre distance equals r1 + r2 (outside) or |r1 - r2| (inside)."""

    name: str
    c1: CircleRef
    c2: CircleRef
    inside: bool = False

    def residual(self, x):
        d = _len_grad(self.c1.center, self.c2.center, x)[0]
        r1, r2 = self.c1.radius.value(x), self.c2.radius.value(x)
        return [d - (abs(r1 - r2) if self.inside else r1 + r2)]

    def jacobian(self, x):
        row = _len_grad(self.c1.center, self.c2.center, x)[1]
        r1, r2 = self.c1.radius.value(x), self.c2.radius.value(x)
        if self.inside:
            s = 1.0 if r1 >= r2 else -1.0
            row = _add(row, _scale(self.c1.radius.jac(x), -s))
            row = _add(row, _scale(self.c2.radius.jac(x), s))
        else:
            row = _add(row, _scale(self.c1.radius.jac(x), -1))
            row = _add(row, _scale(self.c2.radius.jac(x), -1))
        return [row]


@dataclass
class Angle(Constraint):
    """The unsigned angle between the lines' directions, 0 to 180 degrees; with reverse
    the second line's direction is taken the other way (the supplementary sector), which
    is how a dimension placed in that sector keeps the geometry where it is."""

    name: str
    l1: LineRef
    l2: LineRef
    degrees: float
    reverse: bool = False

    def _parts(self, x):
        d1 = _vsub(self.l1.b.pos(x), self.l1.a.pos(x))
        d2 = _vsub(self.l2.b.pos(x), self.l2.a.pos(x))
        if self.reverse:
            d2 = (-d2[0], -d2[1])
        return d1, d2, _cross(d1, d2), _dot(d1, d2)

    def residual(self, x):
        _, _, c, s = self._parts(x)
        ang = math.degrees(math.atan2(c, s))
        return [abs(ang) - abs(self.degrees)]

    def jacobian(self, x):
        d1, d2, c, s = self._parts(x)
        den = c * c + s * s
        if den < 1e-18:
            return [{}]
        sign = 1.0 if math.atan2(c, s) >= 0 else -1.0  # d|theta| = sign(theta) dtheta
        k = sign * math.degrees(1.0) / den
        # theta = atan2(c, s); dtheta = (s dc - c ds)/den
        # dc/dd1 = (d2y, -d2x); dc/dd2 = (-d1y, d1x); ds/dd1 = d2; ds/dd2 = d1
        g1 = (k * (s * d2[1] - c * d2[0]), k * (-s * d2[0] - c * d2[1]))
        g2 = (k * (-s * d1[1] - c * d1[0]), k * (s * d1[0] - c * d1[1]))
        f = -1.0 if self.reverse else 1.0  # d2 is the line's direction negated
        row = _chain(self.l1.b, x, *g1)
        row = _add(row, _chain(self.l1.a, x, -g1[0], -g1[1]))
        row = _add(row, _chain(self.l2.b, x, f * g2[0], f * g2[1]))
        row = _add(row, _chain(self.l2.a, x, -f * g2[0], -f * g2[1]))
        return [row]


@dataclass
class Symmetric(Constraint):
    """p and q mirror images about a line: midpoint on the line, p - q along its normal."""

    name: str
    p: PointRef
    q: PointRef
    line: LineRef
    rows = 2

    def residual(self, x):
        mid = MidPoint(self.p, self.q)
        d = _vsub(self.line.b.pos(x), self.line.a.pos(x))
        pq = _vsub(self.p.pos(x), self.q.pos(x))
        return [_point_line(mid, self.line, x)[0], _dot(d, pq)]

    def jacobian(self, x):
        mid = MidPoint(self.p, self.q)
        d = _vsub(self.line.b.pos(x), self.line.a.pos(x))
        pq = _vsub(self.p.pos(x), self.q.pos(x))
        r2 = _chain(self.p, x, d[0], d[1])
        r2 = _add(r2, _chain(self.q, x, -d[0], -d[1]))
        r2 = _add(r2, _chain(self.line.b, x, pq[0], pq[1]))
        r2 = _add(r2, _chain(self.line.a, x, -pq[0], -pq[1]))
        return [_point_line(mid, self.line, x)[1], r2]


@dataclass
class Midpoint(Constraint):
    name: str
    p: PointRef
    line: LineRef
    rows = 2

    def residual(self, x):
        m = MidPoint(self.line.a, self.line.b).pos(x)
        p = self.p.pos(x)
        return [p[0] - m[0], p[1] - m[1]]

    def jacobian(self, x):
        m = MidPoint(self.line.a, self.line.b)
        return [_add(_chain(self.p, x, 1, 0), _chain(m, x, -1, 0)),
                _add(_chain(self.p, x, 0, 1), _chain(m, x, 0, -1))]


@dataclass
class FixPoint(Constraint):
    name: str
    p: PointRef
    at: tuple[float, float]
    rows = 2

    def residual(self, x):
        px, py = self.p.pos(x)
        return [px - self.at[0], py - self.at[1]]

    def jacobian(self, x):
        return [_chain(self.p, x, 1, 0), _chain(self.p, x, 0, 1)]


@dataclass
class FixScalar(Constraint):
    name: str
    s: ScalarRef
    at: float

    def residual(self, x):
        return [self.s.value(x) - self.at]

    def jacobian(self, x):
        return [self.s.jac(x)]


class Composite(Constraint):
    """Several primitive constraints under one user-visible name (coradial, colinear, fix)."""

    def __init__(self, name: str, parts: list[Constraint]):
        self.name = name
        self.parts = parts
        self.rows = sum(p.rows for p in parts)

    def residual(self, x):
        out: list[float] = []
        for p in self.parts:
            out.extend(p.residual(x))
        return out

    def jacobian(self, x):
        out: list[Row] = []
        for p in self.parts:
            out.extend(p.jacobian(x))
        return out


@dataclass
class EqualDistance(Constraint):
    """|p - c| == |q - c|: keeps an arc's two endpoints on its circle."""

    name: str
    c: PointRef
    p: PointRef
    q: PointRef
    implicit = True

    def residual(self, x):
        return [_len_grad(self.c, self.p, x)[0] - _len_grad(self.c, self.q, x)[0]]

    def jacobian(self, x):
        return [_add(_len_grad(self.c, self.p, x)[1], _scale(_len_grad(self.c, self.q, x)[1], -1))]


# --- the system ------------------------------------------------------------------

@dataclass
class Solution:
    x: np.ndarray
    residual: float
    dof: int
    rank: int
    redundant: list[str]
    conflicting: list[str]
    free_vars: list[int]
    nfev: int
    ms: float


class System:
    def __init__(self) -> None:
        self.x0: list[float] = []
        self.constraints: list[Constraint] = []

    # variables
    def var(self, value: float) -> int:
        self.x0.append(float(value))
        return len(self.x0) - 1

    def point(self, x: float, y: float) -> FreePoint:
        return FreePoint(self.var(x), self.var(y))

    @property
    def nvars(self) -> int:
        return len(self.x0)

    @property
    def nrows(self) -> int:
        return sum(c.rows for c in self.constraints)

    def add(self, c: Constraint) -> None:
        self.constraints.append(c)

    def initial(self) -> np.ndarray:
        return np.asarray(self.x0, float)

    # evaluation
    def residual(self, x: np.ndarray) -> np.ndarray:
        out: list[float] = []
        for c in self.constraints:
            out.extend(c.residual(x))
        return np.asarray(out, float)

    def jacobian(self, x: np.ndarray) -> np.ndarray:
        J = np.zeros((self.nrows, self.nvars))
        r = 0
        for c in self.constraints:
            for row in c.jacobian(x):
                for k, v in row.items():
                    J[r, k] += v
                r += 1
        return J

    # solving
    def project(self, x: np.ndarray, tol: float = 1e-12, maxit: int = 25) -> tuple[np.ndarray, int]:
        """Gauss-Newton least-norm projection onto the constraint manifold."""
        nfev = 0
        if self.nrows == 0:
            return x, 0
        for _ in range(maxit):
            r = self.residual(x)
            nfev += 1
            if np.linalg.norm(r) < tol:
                break
            J = self.jacobian(x)
            s = np.linalg.lstsq(J, -r, rcond=1e-10)[0]
            x = x + s
        return x, nfev

    def solve(self, x_init: np.ndarray | None = None, anchor: float = 1e-4) -> Solution:
        """Levenberg-Marquardt on the constraints plus weak anchors to the current
        positions (minimal movement, well posed when under-constrained), then a
        projection to land on the manifold."""
        t0 = time.perf_counter()
        x_ref = self.initial() if x_init is None else np.asarray(x_init, float)
        n = self.nvars
        if n == 0:
            return self._finish(x_ref, 0, t0)
        if self.nrows == 0:
            return self._finish(x_ref, 0, t0)
        w = np.full(n, anchor)

        def fun(x):
            return np.concatenate([self.residual(x), w * (x - x_ref)])

        def jac(x):
            return np.vstack([self.jacobian(x), np.diag(w)])

        res = least_squares(fun, x_ref, jac=jac, method="lm", xtol=1e-12, ftol=1e-12, gtol=1e-12, max_nfev=2000)
        x, k = self.project(res.x)
        return self._finish(x, res.nfev + k, t0)

    def drag(self, x_init: np.ndarray, targets: list[tuple[PointRef, tuple[float, float]]],
             anchor: float = 1e-4, weight: float = 1.0, maxit: int = 12,
             scalars: list[tuple[Callable[[np.ndarray], float], Callable[[np.ndarray], Row]]] | None = None) -> Solution:
        """Move points toward targets by equality-constrained least squares steps in
        the tangent space of the constraints, projecting back after each step. The
        constraints never fight the pull, so nothing leaks into other entities.
        `scalars` are residuals (value, gradient) to drive to zero the same way: a
        circle's rim dragged to the cursor is `|centre - cursor| - radius`."""
        t0 = time.perf_counter()
        x = np.asarray(x_init, float)
        n = self.nvars
        nfev = 0
        for _ in range(maxit):
            x, k = self.project(x)
            nfev += k
            # objective: weight^2 |Jp s - (t - p)|^2 + anchor^2 |s|^2, subject to J s = 0
            H = np.eye(n) * anchor * anchor
            g = np.zeros(n)
            rows: list[tuple[Row, float]] = []
            for p, (tx, ty) in targets:
                px, py = p.pos(x)
                jx, jy = p.jac(x)
                rows += [(jx, tx - px), (jy, ty - py)]
            for value, grad in scalars or []:
                rows.append((grad(x), -value(x)))
            for row, delta in rows:
                vec = np.zeros(n)
                for kk, v in row.items():
                    vec[kk] = v
                H += weight * weight * np.outer(vec, vec)
                g += weight * weight * vec * delta
            m = self.nrows
            if m:
                J = self.jacobian(x)
                K = np.block([[H, J.T], [J, np.zeros((m, m))]])
                rhs = np.concatenate([g, np.zeros(m)])
                sol = np.linalg.lstsq(K, rhs, rcond=1e-12)[0]
                s = sol[:n]
            else:
                s = np.linalg.solve(H, g)
            if np.linalg.norm(s) < 1e-9:
                break
            x = x + s
        x, k = self.project(x)
        return self._finish(x, nfev + k, t0)

    def _finish(self, x: np.ndarray, nfev: int, t0: float) -> Solution:
        rank, dof, redundant, free = self.analyze(x)
        r = self.residual(x) if self.nrows else np.zeros(0)
        rnorm = float(np.linalg.norm(r)) if r.size else 0.0
        conflicting = self._conflicting(r) if rnorm > 1e-6 else []
        return Solution(x, rnorm, dof, rank, redundant, conflicting, free, nfev, (time.perf_counter() - t0) * 1e3)

    def _conflicting(self, r: np.ndarray) -> list[str]:
        out, i = [], 0
        for c in self.constraints:
            if np.linalg.norm(r[i:i + c.rows]) > 1e-6 and not c.implicit:
                out.append(c.name)
            i += c.rows
        return out

    @staticmethod
    def _rank(J: np.ndarray) -> int:
        if J.size == 0:
            return 0
        s = np.linalg.svd(J, compute_uv=False)
        return int((s > 1e-9 * max(1.0, s[0])).sum())

    def analyze(self, x: np.ndarray) -> tuple[int, int, list[str], list[int]]:
        """Rank, degrees of freedom, redundant constraints (removing them loses no
        rank, newest first) and the free variables (those the null space touches)."""
        n = self.nvars
        if self.nrows == 0:
            return 0, n, [], list(range(n))
        J = self.jacobian(x)
        rank = self._rank(J)
        dof = n - rank
        redundant: list[str] = []
        if J.shape[0] > rank:
            offs, r0 = [], 0
            for c in self.constraints:
                offs.append((c, r0, r0 + c.rows))
                r0 += c.rows
            keep = np.ones(J.shape[0], bool)
            for c, a, b in reversed(offs):
                if c.implicit:
                    continue
                trial = keep.copy()
                trial[a:b] = False
                if self._rank(J[trial]) == rank:
                    redundant.append(c.name)
                    keep = trial
                    if J[keep].shape[0] == rank:
                        break
        free: list[int] = []
        if dof > 0 and n:
            _, _, vt = np.linalg.svd(J, full_matrices=True)
            null = vt[rank:]
            if null.size:
                free = [int(i) for i in np.flatnonzero(np.abs(null).max(axis=0) > 1e-7)]
        return rank, dof, redundant, free


def check_jacobians(system: System, x: np.ndarray, eps: float = 1e-6) -> float:
    """Max abs difference between analytic and central-difference Jacobians (for tests)."""
    J = system.jacobian(x)
    Jn = np.zeros_like(J)
    for i in range(system.nvars):
        xp, xm = x.copy(), x.copy()
        xp[i] += eps
        xm[i] -= eps
        Jn[:, i] = (system.residual(xp) - system.residual(xm)) / (2 * eps)
    return float(np.abs(J - Jn).max()) if J.size else 0.0
