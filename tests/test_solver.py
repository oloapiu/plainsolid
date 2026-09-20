"""L3: the sketch solver. Fixtures with known solutions, degrees of freedom,
redundancy and conflict detection, drag, perturbed starts, timing."""
import random
import time

import numpy as np
import pytest

from plainsolid.parse import parse_document
from plainsolid.solver import system as S
from plainsolid.solver.build import build, solve_sketch

pytestmark = pytest.mark.solver

HEAD = "from plainsolid import *\n"
RECT = '''s = sketch("s", on=XY)
s.line("b", (0.3, -0.2), (41, 0.5))
s.line("r", (41, 0.5), (39, 19))
s.line("t", (39, 19), (-1, 21))
s.line("l", (-1, 21), (0.3, -0.2))
s.coincident("c1", "b.end", "r.start")
s.coincident("c2", "r.end", "t.start")
s.coincident("c3", "t.end", "l.start")
s.coincident("c4", "l.end", "b.start")
s.horizontal("h1", "b")
s.horizontal("h2", "t")
s.vertical("v1", "r")
s.vertical("v2", "l")
s.fix("o", "b.start")
s.length("w", "b", 40)
s.length("h", "r", 20)
s.circle("c", 4.6, at=(9, 11))
s.diameter("d", "c", 5)
s.distance("dx", "c.center", "l", 10)
s.distance("dy", "c.center", "b", 10)
'''


def feature(src: str, name: str = "s"):
    doc = parse_document(HEAD + src)
    assert not doc.errors, [e.message for e in doc.errors]
    return doc.feature(name)


def corners(sol):
    return [tuple(round(v, 6) for v in sol.coords[n]["start"]) for n in ("b", "r", "t", "l")]


def test_rectangle_and_circle_fully_constrained():
    sol = solve_sketch(feature(RECT))
    assert sol.dof == 0 and sol.residual < 1e-9 and not sol.redundant and not sol.conflicting
    assert corners(sol) == [(0.3, -0.2), (40.3, -0.2), (40.3, 19.8), (0.3, 19.8)]
    assert sol.coords["c"]["at"] == pytest.approx((10.3, 9.8)) and sol.coords["c"]["diameter"] == pytest.approx(5)
    assert sol.free_entities == []


def test_jacobians_match_finite_differences():
    lay = build(feature(RECT))
    assert S.check_jacobians(lay.system, lay.system.initial()) < 1e-6
    extra = RECT + 's.arc("a", (20, 30), (25, 30), (20, 35))\ns.tangent("tg", "t", "c")\ns.angle("an", "b", "r", 90)\ns.symmetric("sy", "c.center", "a.center", "r")\ns.midpoint("mp", "a.start", "t")\n'
    lay = build(feature(extra))
    assert S.check_jacobians(lay.system, lay.system.initial()) < 1e-6


def test_degrees_of_freedom_and_free_entities():
    sol = solve_sketch(feature(RECT.replace('s.distance("dy", "c.center", "b", 10)\n', "")))
    assert sol.dof == 1 and sol.free_entities == ["c"]
    sol = solve_sketch(feature(RECT.replace('s.length("h", "r", 20)\n', "")))
    assert sol.dof == 1 and set(sol.free_entities) == {"r", "t", "l"}  # the circle is placed from b and l


def test_redundant_and_conflicting_are_named():
    sol = solve_sketch(feature(RECT + 's.horizontal("h_again", "b")\n'))
    assert sol.redundant == ["h_again"] and not sol.conflicting
    sol = solve_sketch(feature(RECT + 's.equal("implied", "b", "t")\n'))
    assert sol.redundant == ["implied"]
    sol = solve_sketch(feature(RECT + 's.length("w2", "b", 45)\n'))
    assert "w2" in sol.conflicting or "w" in sol.conflicting
    assert sol.residual > 1e-3


def test_drag_moves_only_the_free_part():
    f = feature(RECT.replace('s.length("h", "r", 20)\n', ""))
    sol = solve_sketch(f, drag={"t.start": (60, 35)})
    assert sol.residual < 1e-9
    b, r, t, l = corners(sol)
    assert b == (0.3, -0.2) and r == (40.3, -0.2)           # fixed corner and width untouched
    assert t == (40.3, 35.0) and l == (0.3, 35.0)            # height followed the drag
    sol = solve_sketch(feature(RECT), drag={"t.start": (60, 35)})
    assert corners(sol) == [(0.3, -0.2), (40.3, -0.2), (40.3, 19.8), (0.3, 19.8)]  # fully constrained: no move


def test_dragging_a_circle_by_its_rim_changes_the_radius():
    # a free circle: the rim follows, the centre stays (the drag does not split the motion)
    free = HEAD + 's = sketch("s", on=XY)\ns.circle("c", 10, at=(5, 5))\n'
    sol = solve_sketch(feature(free), drag={"c.rim": (5, 13)})
    c = sol.coords["c"]
    assert [round(v, 6) for v in c["at"]] == [5, 5] and float(c.get("diameter", 2 * c.get("radius", 0))) == pytest.approx(16, abs=1e-6)
    src = free + 's.fix("f", "c.center")\n'
    sol = solve_sketch(feature(src), drag={"c.rim": (5, 13)})
    assert sol.residual < 1e-9
    c = sol.coords["c"]
    assert list(c["at"]) == pytest.approx([5, 5], abs=1e-9) and float(c.get("diameter", 2 * c.get("radius", 0))) == pytest.approx(16, abs=1e-6)
    # with the diameter dimensioned the rim does not move; the centre never does
    sol = solve_sketch(feature(src + 's.diameter("d", "c", 10)\n'), drag={"c.rim": (5, 13)})
    c = sol.coords["c"]
    assert list(c["at"]) == pytest.approx([5, 5], abs=1e-9) and float(c.get("diameter", 2 * c.get("radius", 0))) == pytest.approx(10, abs=1e-6)
    with pytest.raises(Exception, match="not a circle"):
        solve_sketch(feature(HEAD + 's = sketch("s", on=XY)\ns.line("l", (0, 0), (10, 0))\n'), drag={"l.rim": (1, 1)})


def test_converges_from_perturbed_starts():
    import re

    rng = random.Random(7)
    base = solve_sketch(feature(RECT))
    for _ in range(20):
        lines = []
        for line in RECT.splitlines():
            if ".line(" in line or ".circle(" in line:
                head, tail = line.split('",', 1)  # keep the name, jitter the coordinates
                tail = re.sub(r"-?\d+(?:\.\d+)?",
                              lambda m: str(round(float(m.group()) * (1 + rng.uniform(-0.2, 0.2)) + rng.uniform(-2, 2), 3)),
                              tail)
                line = head + '",' + tail
            lines.append(line)
        sol = solve_sketch(feature("\n".join(lines) + "\n"))
        assert sol.residual < 1e-8 and sol.dof == 0
        # fix() pins the corner where it was drawn, so compare shapes relative to that corner
        ox, oy = sol.coords["b"]["start"]
        bx, by = base.coords["b"]["start"]
        rel = [(round(x - ox, 6), round(y - oy, 6)) for x, y in corners(sol)]
        assert rel == [(round(x - bx, 6), round(y - by, 6)) for x, y in corners(base)]
        cx, cy = sol.coords["c"]["at"]
        assert (cx - ox, cy - oy) == pytest.approx((base.coords["c"]["at"][0] - bx, base.coords["c"]["at"][1] - by), abs=1e-6)


def test_solve_is_fast():
    f = feature(RECT)
    t = time.perf_counter()
    for _ in range(10):
        solve_sketch(f)
    assert (time.perf_counter() - t) / 10 < 0.05


def test_rigid_macros_and_slot():
    src = '''s = sketch("s", on=XY)
s.rect("r", 30, 10, at=(1, 1))
s.circle("c", 5, at=(-5, 2))
s.fix("f", "r")
s.distance("hx", "c.center", "r.left", 10)
s.distance("hy", "c.center", "r.bottom", 5)
s.slot("sl", 16, 5, at=(40, 40), angle=80)
s.parallel("up", "sl.axis", "r.left")
s.distance("pitch", "sl.center", "r.center", 50)
'''
    sol = solve_sketch(feature(src))
    assert sol.coords["c"]["at"] == pytest.approx((-4, 1), abs=1e-6)
    assert sol.coords["sl"]["angle"] == pytest.approx(90, abs=1e-6) or sol.coords["sl"]["angle"] == pytest.approx(-90, abs=1e-6)
    assert np.hypot(*(np.subtract(sol.coords["sl"]["at"], (1, 1)))) == pytest.approx(50, abs=1e-6)


def test_reference_errors_are_clear():
    with pytest.raises(S.SketchError, match="unknown reference 'nope'"):
        solve_sketch(feature(RECT + 's.horizontal("bad", "nope")\n'))
    with pytest.raises(S.SketchError, match="not a line"):
        solve_sketch(feature(RECT + 's.parallel("bad", "c", "b")\n'))
    with pytest.raises(S.SketchError, match="two lines or two circles"):
        solve_sketch(feature(RECT + 's.equal("bad", "c", "b")\n'))


@pytest.mark.solver
def test_angle_is_unsigned_and_reverse_measures_the_supplementary_sector():
    """L3: an angle dimension holds the angle between the lines' directions (0 to 180), and
    reverse=True the angle against the second line's opposite direction, so a dimension placed
    in either sector of a V keeps the V where it is; the Jacobians agree with finite differences."""
    v = 's = sketch("s", on=XY)\ns.line("l1", (0, 0), (10, 0))\ns.line("l2", (8.6603, 5), (0, 0))\ns.coincident("c", "l1.start", "l2.end")\ns.fix("f", "l1")\n'
    # l2 runs back into the vertex: the directed angle from l1 to l2 is -150, the unsigned one 150
    sol = solve_sketch(feature(v + 's.angle("a", "l1", "l2", 150)\n'))
    assert sol.residual < 1e-9 and not sol.conflicting and sol.coords["l2"]["start"] == pytest.approx((8.6603, 5), abs=1e-3)
    # the supplementary sector: 30 against l2's opposite direction
    sol = solve_sketch(feature(v + 's.angle("a", "l1", "l2", 30, reverse=True)\n'))
    assert sol.residual < 1e-9 and not sol.conflicting and sol.coords["l2"]["start"] == pytest.approx((8.6603, 5), abs=1e-3)
    # a different value turns l2 about the vertex (its length is free, so the nearest solution shortens it)
    sol = solve_sketch(feature(v + 's.angle("a", "l1", "l2", 90, reverse=True)\n'))
    assert sol.residual < 1e-9 and sol.coords["l2"]["start"][0] == pytest.approx(0, abs=1e-4) and sol.coords["l2"]["start"][1] > 1
    for tail in ('s.angle("a", "l1", "l2", 150)\n', 's.angle("a", "l1", "l2", 30, reverse=True)\n'):
        lay = build(feature(v + tail))
        assert S.check_jacobians(lay.system, lay.system.initial()) < 1e-6


@pytest.mark.solver
def test_every_sketch_has_a_fixed_origin_and_axes():
    """L3: `origin`, `x_axis` and `y_axis` are references every sketch has without declaring them;
    they take the relations a fixed point or line takes, cannot be fixed again, and their names
    are reserved."""
    src = ('s = sketch("s", on=XY)\ns.line("l1", (1, 0.5), (30, 2))\ns.line("l2", (0, 1), (3, 25))\ns.point("p", (5, 1))\n'
           's.coincident("c", "l1.start", "origin")\ns.on("o", "p", "x_axis")\ns.symmetric("sy", "l1.end", "l2.end", "y_axis")\n'
           's.angle("a", "l2", "x_axis", 90, reverse=True)\ns.distance("d", "p", "origin", 5)\ns.length("w", "l1", 30)\n')
    sol = solve_sketch(feature(src))
    assert sol.residual < 1e-9 and not sol.conflicting, (sol.conflicting, sol.residual)
    assert sol.coords["l1"]["start"] == pytest.approx((0, 0), abs=1e-6)
    assert sol.coords["p"]["at"] == pytest.approx((5, 0), abs=1e-6)
    assert sol.coords["l2"]["end"][0] == pytest.approx(-sol.coords["l1"]["end"][0], abs=1e-6)
    assert sol.coords["l2"]["end"][1] == pytest.approx(sol.coords["l1"]["end"][1], abs=1e-6)
    assert sol.coords["l2"]["start"][0] == pytest.approx(sol.coords["l2"]["end"][0], abs=1e-6)  # vertical
    assert "origin" not in sol.free_entities and "x_axis" not in sol.free_entities
    lay = build(feature(src))
    assert S.check_jacobians(lay.system, lay.system.initial()) < 1e-6
    with pytest.raises(Exception, match="fixed already"):
        solve_sketch(feature(src + 's.fix("f", "origin")\n'))
    doc = parse_document(HEAD + 's = sketch("s", on=XY)\ns.line("origin", (0, 0), (1, 1))\n')
    assert doc.errors and "built-in" in doc.errors[0].message
    # a constraint may carry the name: constraints are never referenced (the zoo's bracket fixes its corner with one)
    doc = parse_document(HEAD + 's = sketch("s", on=XY)\ns.line("l", (0, 0), (1, 1))\ns.fix("origin", "l.start")\n')
    assert not doc.errors
