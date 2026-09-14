"""Assemblies: instances of parts and vendor STEP files, mates, the pose
solver, the bill of materials, interference, STEP export.

Layers: unit (DSL, parsing, composing, write-back, selectors), solver (each
mate kind, degrees of freedom, redundancy, conflicts, perturbed starts,
timing), io (export round trip, generator agreement), roundtrip (edit ops
keep diffs minimal), api (queries, pose write-back, dependency refresh)."""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from plainsolid import assembly as pasm
from plainsolid import edit, query
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document, parse_file
from plainsolid.selectors import Query, Selector, SelectorError
from plainsolid.stepimport import read_step

HEAD = "from plainsolid import *\n"

BASE = HEAD + '''meta(name="base", material="al6061", color="#c0c0c0")
s = sketch("s", on=XY)
s.rect("r", 40, 20)
body = extrude("body", s, 10)
h = sketch("h", on=body.faces.top)
h.circle("hole", 6, at=(10, 0))
hole = cut("hole", h, through=True)
'''
PEG = HEAD + '''meta(name="peg", material="steel")
s = sketch("s", on=XY)
s.circle("c", 6)
body = extrude("body", s, 15)
'''
PLATE = HEAD + '''meta(name="plate", material="abs")
s = sketch("s", on=XY)
s.rect("r", 30, 30)
body = extrude("body", s, 4)
'''


def make_project(root: Path) -> Path:
    (root / "base.py").write_text(BASE)
    (root / "peg.py").write_text(PEG)
    (root / "plate.py").write_text(PLATE)
    return root


def asm(root: Path, body: str, name: str = "asm.py"):
    path = root / name
    path.write_text(HEAD + 'meta(kind="assembly", name="asm")\n' + body)
    doc = parse_file(str(path))
    assert not doc.errors, [e.message for e in doc.errors]
    return evaluate(doc)


def ok(ev) -> None:
    bad = [(r.name, r.error.message) for r in ev.results if r.error]
    assert not bad, bad


# --- unit ---------------------------------------------------------------------------

@pytest.mark.unit
def test_dsl_records_instances_and_mates(tmp_path):
    src = HEAD + '''meta(kind="assembly", name="a")
box = instance("box", "box.py", at=(1, 2, 3), rotate=(0, 90, 0), color="#ff8800", material="steel")
lid = instance("lid", "lid.py", density=1.2)
fixed("anchor", box)
coincident("m1", lid.faces.of("plate").bottom, box.faces.top, flip=True)
distance("m2", lid.planes.YZ, box.axes.Z, 2.5)
angle("m3", lid.planes.XY, box.planes.XY, 30)
'''
    doc = parse_document(src)
    assert not doc.errors and doc.kind == "assembly"
    kinds = [(f.name, f.kind) for f in doc.features]
    assert kinds == [("box", "instance"), ("lid", "instance"), ("anchor", "fixed"), ("m1", "coincident"), ("m2", "distance"), ("m3", "angle")]
    box = doc.feature("box")
    assert box.args["at"] == [1.0, 2.0, 3.0] and box.args["rotate"] == [0.0, 90.0, 0.0]
    assert box.args["color"] == "#ff8800" and box.args["material"] == "steel" and box.variable == "box"
    assert doc.feature("lid").args["density"] == 1.2
    m1 = doc.feature("m1")
    assert m1.args["flip"] is True and Selector.from_json(m1.args["a"]["selector"]).ref_name == "lid.faces.of('plate').bottom"
    assert Selector.from_json(doc.feature("m2").args["b"]["selector"]).ref_name == "box.axes.Z"
    assert doc.feature("m2").args["value"] == 2.5 and doc.feature("m3").arg_texts["value"] == "30"
    assert doc.feature("m1").variable is None
    # instances and mates in a part are errors on their line
    ev = evaluate(parse_document(HEAD + 'p = instance("p", "x.py")\n'))
    assert "belong in an assembly" in ev.result("p").error.message
    # bad references are rejected while parsing
    bad = parse_document(HEAD + 'meta(kind="assembly")\na = instance("a", "a.py")\ncoincident("m", "a.faces.top", a.faces.top)\n')
    assert bad.errors and "reference on an instance" in bad.errors[0].message
    bad = parse_document(HEAD + 'meta(kind="assembly")\na = instance("a", "a.py", color=3)\n')
    assert bad.errors and "color" in bad.errors[0].message


@pytest.mark.unit
def test_instance_selectors():
    q = Query(Selector("box", "faces")).of("body").top
    assert q.ref_name == "box.faces.of('body').top" and Selector.from_json(q.selector.to_json()) == q.selector
    assert Query(Selector("box", "planes")).XZ.ref_name == "box.planes.XZ"
    assert Query(Selector("box", "axes")).Y.ref_name == "box.axes.Y"
    with pytest.raises(SelectorError, match="XY, XZ or YZ"):
        _ = Query(Selector("box", "planes")).top
    with pytest.raises(SelectorError, match="of\\(\\) applies to faces"):
        Query(Selector("box", "planes")).of("x")
    with pytest.raises(SelectorError, match="mate reference"):
        from build123d import Box

        from plainsolid.selectors import resolve
        resolve(Query(Selector("box", "axes")).Z.selector, Box(1, 1, 1))


@pytest.mark.unit
def test_colours_and_materials():
    assert pasm.parse_color("#ff8800") == (1.0, 0.5333, 0.0, 1.0)
    assert pasm.parse_color("#abc") == pasm.parse_color("#aabbcc")
    assert pasm.parse_color((255, 0, 0)) == (1.0, 0.0, 0.0, 1.0) and pasm.parse_color([0.5, 0.5, 0.5, 0.5]) == (0.5, 0.5, 0.5, 0.5)
    assert pasm.parse_color(None) is None and pasm.color_hex((1.0, 0.5333, 0.0, 1.0)) == "#ff8800"
    with pytest.raises(pasm.AssemblyError):
        pasm.parse_color("#12345")
    with pytest.raises(pasm.AssemblyError):
        pasm.parse_color("red")


@pytest.mark.unit
def test_compose_instances_and_mates():
    assert edit.compose_feature("instance", "g1", {"path": "vendor/g.step", "material": "nylon", "at": [0, 0, 0]}) == \
        'g1 = instance("g1", "vendor/g.step", material="nylon")'
    assert edit.compose_feature("instance", "g2", {"path": "g.step", "at": [1.5, 0, 0], "rotate": [0, 90.0, 0], "color": "#ff8800", "density": 1.2}) == \
        'g2 = instance("g2", "g.step", at=(1.5, 0, 0), rotate=(0, 90, 0), color="#ff8800", density=1.2)'
    assert edit.compose_feature("fixed", "anchor", {"instance": {"expr": "box"}}) == 'fixed("anchor", box)'
    assert edit.compose_feature("coincident", "m1", {"a": {"expr": "lid.faces.top"}, "b": {"expr": "box.faces.top"}, "flip": True}) == \
        'coincident("m1", lid.faces.top, box.faces.top, flip=True)'
    assert edit.compose_feature("distance", "m2", {"a": {"expr": "a.planes.YZ"}, "b": {"expr": "b.planes.YZ"}, "value": 2.5, "suppressed": True}) == \
        'distance("m2", a.planes.YZ, b.planes.YZ, 2.5, suppressed=True)'
    assert edit.compose_feature("angle", "m3", {"a": {"expr": "a.planes.XY"}, "b": {"expr": "b.planes.XY"}, "value": {"expr": "tilt"}}) == \
        'angle("m3", a.planes.XY, b.planes.XY, tilt)'
    with pytest.raises(edit.EditError, match="needs a value"):
        edit.compose_feature("distance", "m", {"a": {"expr": "a"}, "b": {"expr": "b"}})


@pytest.mark.unit
def test_write_poses_touches_only_literals():
    src = HEAD + 'meta(kind="assembly")\nbox = instance("box", "box.py")  # anchored\nlid = instance("lid", "lid.py", at=(0, 0, 40), color="#ddd")\npeg = instance("peg", "peg.py", at=(x, 0, 0))\n'
    new = edit.write_poses(src, {"box": {"at": (0, 0, 0), "rotate": (0, 0, 0)}, "lid": {"at": (0.00001, 1.23456, 40), "rotate": (0, 0, 180)},
                                 "peg": {"at": (5, 5, 5), "rotate": (0, 0, 0)}})
    assert 'box = instance("box", "box.py")  # anchored\n' in new  # identity stays unwritten
    assert 'lid = instance("lid", "lid.py", at=(0, 1.2346, 40), color="#ddd", rotate=(0, 0, 180))\n' in new
    assert 'peg = instance("peg", "peg.py", at=(x, 5, 5))\n' in new  # an expression is the user's, literals follow the solve
    again = edit.write_poses(new, {"lid": {"at": (0, 1.23456, 40), "rotate": (0, 0, 180)}})
    assert again == new  # unchanged within the precision: no rewrite
    op = edit.apply(new, {"op": "write_poses", "poses": {"box": {"at": (3, 0, 0), "rotate": (0, 0, 0)}}})
    assert 'box = instance("box", "box.py", at=(3, 0, 0))  # anchored' in op
    # setting a pose back to the default drops the keyword
    assert 'lid = instance("lid", "lid.py", color="#ddd", rotate=(0, 0, 180))' in edit.set_argument(new, "lid", "at", "(0, 0, 0)")


@pytest.mark.unit
def test_deleting_an_instance_cascades_to_its_mates():
    src = HEAD + 'meta(kind="assembly")\na = instance("a", "a.py")\nb = instance("b", "b.py")\nfixed("fa", a)\ncoincident("m", a.faces.top, b.faces.top)\nc = instance("c", "c.py")\n'
    assert edit.dependents(src, "b")["features"] == ["m"]
    assert edit.dependents(src, "a")["features"] == ["fa", "m"]
    out = edit.delete_feature(src, "b")
    assert "instance(\"b\"" not in out and "coincident" not in out and 'c = instance("c", "c.py")' in out


# --- solver -----------------------------------------------------------------------

@pytest.fixture
def parts(tmp_path):
    return make_project(tmp_path)


PEG_IN_HOLE = '''base = instance("base", "base.py")
peg = instance("peg", "peg.py", at=(50, 50, 50), rotate=(30, 20, 10))
fixed("anchor", base)
concentric("axis", peg.faces.of("body").from_sketch("c"), base.faces.of("hole").from_sketch("hole"))
coincident("seat", peg.faces.of("body").bottom, base.faces.of("body").top)
'''


@pytest.mark.solver
def test_concentric_and_seat_place_the_peg(parts):
    ev = asm(parts, PEG_IN_HOLE)
    ok(ev)
    s = ev.solution
    assert s.residual < 1e-9 and s.conflicting == [] and s.redundant == []
    assert s.dof == 1 and s.free == ["peg"] and s.rank == 5 and s.variables == 6
    peg = s.poses["peg"]
    assert peg.at() == pytest.approx((10, 0, 10), abs=1e-6)
    assert abs(peg.R[2, 2] - 1) < 1e-9  # the peg's axis is upright
    assert s.ms < 150
    assert pasm.check_jacobian(ev.assembly) < 1e-6
    # the base is fixed at its file pose, the posed instances carry world shapes
    assert s.poses["base"].at() == (0.0, 0.0, 0.0)
    names = [r.name for r in ev.posed]
    assert names == ["base", "peg"] and ev.posed[1].kind == "part" and ev.posed[1].file.endswith("peg.py")
    bb = ev.posed[1].shape.bounding_box()
    assert (bb.min.Z, bb.max.Z) == pytest.approx((10, 25), abs=1e-6)
    assert ev.result("peg").faces_created == 3


@pytest.mark.solver
def test_plane_mates_and_flip(parts):
    body = '''a = instance("a", "plate.py")
b = instance("b", "plate.py", at=(7, 8, 9), rotate=(10, 20, 30))
fixed("fa", a)
coincident("m1", b.faces.of("body").bottom, a.faces.of("body").top)
coincident("m2", b.planes.YZ, a.planes.YZ)
coincident("m3", b.planes.XZ, a.planes.XZ)
'''
    ev = asm(parts, body)
    ok(ev)
    s = ev.solution
    assert s.dof == 0 and s.free == [] and s.residual < 1e-9
    assert s.poses["b"].at() == pytest.approx((0, 0, 4), abs=1e-6)  # stacked on top, faces touching
    assert np.allclose(s.poses["b"].R, np.eye(3), atol=1e-9)
    # flip: the faces align instead of facing each other, so b turns over (half a turn about X,
    # which its XZ mate must follow with a flip too) and its bottom lies in a's top plane
    flipped = body.replace('a.faces.of("body").top)', 'a.faces.of("body").top, flip=True)').replace('a.planes.XZ)', 'a.planes.XZ, flip=True)')
    ev = asm(parts, flipped)
    ok(ev)
    assert ev.solution.conflicting == [] and ev.solution.residual < 1e-9
    assert ev.solution.poses["b"].at() == pytest.approx((0, 0, 4), abs=1e-6)
    assert ev.solution.poses["b"].R[2, 2] == pytest.approx(-1, abs=1e-9)
    # without the second flip the three mates ask for a reflection: reported, not silently bent
    ev = asm(parts, body.replace('a.faces.of("body").top)', 'a.faces.of("body").top, flip=True)'))
    ok(ev)
    assert ev.solution.conflicting and ev.solution.residual > 1e-3
    # a distance moves b along a's normal by the value; a wholly redundant mate is named
    ev = asm(parts, body.replace('coincident("m1", b.faces.of("body").bottom, a.faces.of("body").top)',
                                 'distance("m1", b.faces.of("body").bottom, a.faces.of("body").top, 2.5)') +
             'parallel("m4", b.planes.XY, a.planes.XY)\n')
    ok(ev)
    assert ev.solution.poses["b"].at()[2] == pytest.approx(6.5, abs=1e-6)
    assert ev.solution.redundant == ["m4"]
    assert "redundant" in ev.result_json("m4")["warnings"][0]


@pytest.mark.solver
def test_drag_moves_an_instance_only_along_its_free_motions(zoo_dir, parts):
    ev = evaluate(parse_file(str(zoo_dir / "node.py")))
    sol = ev.solution
    # the lid is held by its mates; the gland spins about its own axis (world Y) and nothing else
    r = pasm.drag(ev.assembly, sol, "lid", None, [5, 0, 0], None)
    assert not r["moved"] and "fully constrained" in r["reason"]
    r = pasm.drag(ev.assembly, sol, "gland1", None, [0, 5, 0], None)
    assert not r["moved"] and "no motion that way" in r["reason"]
    r = pasm.drag(ev.assembly, sol, "gland1", None, None, [0.3, 0.3, 0.3])
    assert r["moved"] and r["conflicting"] == []
    g = r["poses"]["gland1"]
    assert g["at"] == pytest.approx(list(sol.poses["gland1"].at()), abs=1e-6)
    assert g["rotate"][2] == pytest.approx(math.degrees(0.3), abs=0.05) and g["rotate"][:2] == pytest.approx([-90, 0], abs=1e-6)
    for name, pose in sol.poses.items():
        if name != "gland1":
            assert r["poses"][name]["at"] == pytest.approx(list(pose.at()), abs=1e-6), name
    with pytest.raises(pasm.AssemblyError):
        pasm.drag(ev.assembly, sol, "nobody", None, [1, 0, 0], None)
    # an instance without mates goes exactly where it is dragged; steps chain from the previous answer
    ev = asm(parts, 'b = instance("b", "plate.py")\n')
    r = pasm.drag(ev.assembly, ev.solution, "b", None, [10, 0, 0], None)
    assert r["moved"] and r["poses"]["b"]["at"] == pytest.approx([10, 0, 0])
    poses = {n: {"at": q["at"], "rotate": q["rotate"]} for n, q in r["poses"].items()}
    r = pasm.drag(ev.assembly, ev.solution, "b", poses, [0, 5, 0], [0, 0, math.pi / 2])
    assert r["poses"]["b"]["at"] == pytest.approx([10, 5, 0], abs=1e-6) and r["poses"]["b"]["rotate"][2] == pytest.approx(90, abs=1e-6)


@pytest.mark.solver
def test_preview_chooses_the_orientation_that_turns_less_and_drag_writes_poses(parts):
    from plainsolid.workspace import Workspace

    path = parts / "asm.py"
    path.write_text(HEAD + 'meta(kind="assembly", name="asm")\n'
                    'a = instance("a", "plate.py")\nb = instance("b", "plate.py", at=(0, 0, 20))\nfixed("fa", a)\n')
    ws = Workspace(parts)
    doc = ws.open(path)
    mate = lambda a, b: {"op": "add_feature", "kind": "coincident", "name": "m", "args": {"a": {"expr": a}, "b": {"expr": b}}}
    # top against top: the convention (faces opposed) would turn b over, so the preview flips
    r = ws.preview(doc, mate('b.faces.of("body").top', 'a.faces.of("body").top'), choose_flip=True)
    assert r["ok"] and r["flip"] is True and r["rotation"] < 1e-6 and r["poses"]["b"]["at"][2] == pytest.approx(0, abs=1e-6)
    # bottom against top: the convention already needs no turn, so it stays
    r = ws.preview(doc, mate('b.faces.of("body").bottom', 'a.faces.of("body").top'), choose_flip=True)
    assert r["ok"] and r["flip"] is False and r["rotation"] < 1e-6 and r["poses"]["b"]["at"][2] == pytest.approx(4, abs=1e-6)
    assert "m" in r["results"] and r["results"]["m"]["ok"] and r["assembly"]["dof"] == 3
    # nothing was written by the previews
    assert "coincident" not in path.read_text()
    # a broken candidate says so
    r = ws.preview(doc, mate('b.faces.of("nothing").top', 'a.faces.of("body").top'))
    assert not r["ok"] and "nothing" in r["results"]["m"]["error"]["message"]
    # a drag of the free plate, then the client writes the poses it ended on
    r = ws.drag(doc, "b", None, [3, 0, 0], None)
    assert r["moved"] and r["poses"]["b"]["at"] == pytest.approx([3, 0, 20])
    res = ws.apply(doc, {"op": "write_poses", "poses": {"b": {"at": r["poses"]["b"]["at"], "rotate": r["poses"]["b"]["rotate"]}}}, doc.hash)
    assert res["changed"] and 'b = instance("b", "plate.py", at=(3, 0, 20))' in path.read_text()


@pytest.mark.solver
def test_points_axes_and_angles(parts):
    body = '''a = instance("a", "base.py")
b = instance("b", "peg.py", at=(3, 4, 5))
fixed("fa", a)
coincident("v", b.vertices.nearest((3, 0, 0)), a.vertices.nearest((20, 10, 10)))
'''
    ev = asm(parts, body)
    ok(ev)
    assert ev.solution.dof == 3  # a point on a point leaves every rotation
    corner = ev.posed[1].shape.vertices()
    assert min(((v.X - 20) ** 2 + (v.Y - 10) ** 2 + (v.Z - 10) ** 2) for v in corner) < 1e-12
    body = '''a = instance("a", "base.py")
b = instance("b", "plate.py", at=(0, 0, 30), rotate=(5, 0, 0))
fixed("fa", a)
angle("tilt", b.faces.of("body").bottom, a.faces.of("body").top, 30)
coincident("hinge", b.edges.where(parallel_to="+X").nearest((0, -15, 0)), a.edges.where(parallel_to="+X").nearest((0, -10, 10)))
distance("mid", b.vertices.nearest((15, -15, 0)), a.planes.YZ, 15)
'''
    ev = asm(parts, body)
    ok(ev)
    s = ev.solution
    assert s.dof == 0 and s.conflicting == []
    n = s.poses["b"].R @ np.array([0, 0, -1.0])
    assert math.degrees(math.acos(-n[2])) == pytest.approx(30, abs=1e-6)  # the plate's underside tilts 30 degrees from the top
    hinge = s.poses["b"].apply(np.array([0, -15, 0.0]))
    assert hinge == pytest.approx((0, -10, 10), abs=1e-6)
    assert s.poses["b"].apply(np.array([15, -15, 0.0]))[0] == pytest.approx(15, abs=1e-6)
    # an axis a set distance from a plane, and a point on an axis
    body = '''a = instance("a", "base.py")
b = instance("b", "peg.py", at=(0, 0, 20))
fixed("fa", a)
parallel("up", b.axes.Z, a.axes.Z)
distance("dx", b.axes.Z, a.planes.YZ, 12)
coincident("on", b.vertices.nearest((3, 0, 15)), a.axes.X)
'''
    ev = asm(parts, body)
    ok(ev)
    assert ev.solution.conflicting == []
    top = ev.posed[1].shape.bounding_box()
    assert top.center().X == pytest.approx(12, abs=1e-6)


@pytest.mark.solver
def test_conflicts_are_reported_and_under_constrained_stays_put(parts):
    body = '''a = instance("a", "plate.py")
b = instance("b", "plate.py", at=(0, 0, 9))
fixed("fa", a)
distance("d1", b.faces.of("body").bottom, a.faces.of("body").top, 3)
distance("d2", b.faces.of("body").bottom, a.faces.of("body").top, 5)
'''
    ev = asm(parts, body)
    ok(ev)
    s = ev.solution
    assert set(s.conflicting) == {"d1", "d2"} and s.residual > 1e-3
    assert "conflicts" in ev.result_json("d2")["warnings"][0]
    # an instance with nothing holding it keeps its file pose; a mate that leaves freedom moves it least
    body = '''a = instance("a", "plate.py")
b = instance("b", "plate.py", at=(50, 0, 9), rotate=(0, 0, 45))
c = instance("c", "peg.py", at=(0, 0, 30))
fixed("fa", a)
coincident("m", b.faces.of("body").bottom, a.faces.of("body").top)
'''
    ev = asm(parts, body)
    ok(ev)
    s = ev.solution
    assert s.poses["c"].at() == (0.0, 0.0, 30.0) and "c" in s.free and s.dof == 6 + 3
    assert s.poses["b"].at()[:2] == pytest.approx((50, 0), abs=1e-6) and s.poses["b"].at()[2] == pytest.approx(4, abs=1e-6)
    assert s.poses["b"].rotate()[2] == pytest.approx(45, abs=1e-4)  # the free spin is kept
    # nothing fixed: a warning, and the parts still come together
    ev = asm(parts, body.replace('fixed("fa", a)\n', ''))
    ok(ev)
    assert any("no fixed instance" in w for w in ev.solution.warnings)
    assert ev.solution.residual < 1e-9


@pytest.mark.solver
def test_perturbed_starts_reach_the_same_pose(parts):
    rng = np.random.default_rng(7)
    ok(asm(parts, PEG_IN_HOLE))
    for _ in range(12):
        at = tuple(round(float(v), 3) for v in rng.uniform(-40, 40, 3))
        rot = tuple(round(float(v), 3) for v in rng.uniform(-170, 170, 3))
        body = PEG_IN_HOLE.replace("at=(50, 50, 50), rotate=(30, 20, 10)", f"at={at}, rotate={rot}")
        ev = asm(parts, body)
        ok(ev)
        s = ev.solution
        assert s.residual < 1e-9, (at, rot)
        assert s.poses["peg"].at() == pytest.approx((10, 0, 10), abs=1e-6), (at, rot)
        assert abs(abs(s.poses["peg"].R[2, 2]) - 1) < 1e-9


@pytest.mark.solver
def test_errors_name_the_mate_and_the_reference(parts):
    body = '''a = instance("a", "base.py")
b = instance("b", "peg.py")
fixed("fa", a)
coincident("both", a.faces.of("body").top, a.faces.of("body").bottom)
concentric("kinds", b.planes.XY, a.planes.XY)
distance("missing", b.faces.of("nope").top, a.planes.XY, 2)
coincident("later", b.faces.of("body").top, c.faces.top)
c = instance("c", "peg.py")
'''
    doc = parse_document(HEAD + 'meta(kind="assembly")\n' + body, str(parts / "asm.py"))
    assert doc.errors and "'c' is not defined" in doc.errors[0].message  # a reference before its instance is a Python error
    body = body.replace('coincident("later", b.faces.of("body").top, c.faces.top)\n', "")
    ev = asm(parts, body)
    msgs = {r.name: r.error.message for r in ev.results if r.error}
    assert "both references are on 'a'" in msgs["both"]
    assert "concentric needs cylindrical faces" in msgs["kinds"]
    assert "come from 'nope'" in msgs["missing"]
    assert ev.solution is not None and ev.solution.dof == 12  # b and c float, nothing valid holds them
    # a vendor part has no labels
    (parts / "vendor").mkdir()
    import shutil
    shutil.copy(Path(__file__).parent.parent / "zoo" / "vendor" / "antenna.step", parts / "vendor" / "antenna.step")
    ev = asm(parts, 'a = instance("a", "base.py")\nv = instance("v", "vendor/antenna.step")\nfixed("fa", a)\ncoincident("m", v.faces.top, a.faces.of("body").top)\n')
    assert "labels are not available" in ev.result("m").error.message
    ev = asm(parts, 'a = instance("a", "base.py")\nv = instance("v", "vendor/antenna.step", material="abs")\nfixed("fa", a)\n'
                    'coincident("m", v.faces.where(normal="-Z").largest(), a.faces.of("body").top)\n')
    ok(ev)
    assert ev.result("v").faces_created == 10 and ev.posed[1].kind == "step"
    assert query.mass(ev)["instances"]["v"] == pytest.approx(4.9626, abs=1e-3)


@pytest.mark.solver
def test_reference_assembly_solves_as_designed(zoo_dir):
    ev = evaluate(parse_file(str(zoo_dir / "node.py")))
    ok(ev)
    s = ev.solution
    assert s.conflicting == [] and s.redundant == [] and s.residual < 1e-9
    assert s.dof == 3 and s.free == ["gland1", "gland2", "antenna"]  # the round parts may spin
    assert s.poses["lid"].at() == pytest.approx((0, 0, 40), abs=1e-6)
    assert s.poses["bracket"].at() == pytest.approx((-90, 0, 0), abs=1e-6)
    assert s.poses["antenna"].at() == pytest.approx((60, 0, 20), abs=1e-6)
    assert s.poses["gland1"].at() == pytest.approx((-30, 40, 20), abs=1e-6)
    assert s.poses["panel"].at()[2] == pytest.approx(68, abs=1e-6) and s.poses["panel"].rotate()[0] == pytest.approx(30, abs=1e-6)
    assert query.interference(ev)["count"] == 0
    bom = query.bom(ev)
    assert {r["name"]: r["count"] for r in bom["rows"]} == {"enclosure": 1, "lid": 1, "board": 1, "bracket": 1, "gland_m12": 2, "antenna": 1, "panel": 1}
    assert bom["mass"] == pytest.approx(query.mass(ev)["mass"]) and bom["unknown_mass"] == []
    assert s.ms < 400
    # a part that interferes is found: push the antenna into the wall
    src = (zoo_dir / "node.py").read_text().replace('coincident("antenna_seat"', 'distance("antenna_seat"').replace(
        'box.faces.of("body").from_sketch("box.right"))', 'box.faces.of("body").from_sketch("box.right"), -3)')
    ev2 = evaluate(parse_document(src, str(zoo_dir / "node.py")))
    ok(ev2)
    pairs = query.interference(ev2)["pairs"]
    assert [(p["a"], p["b"]) for p in pairs] == [("box", "antenna")] and pairs[0]["volume"] > 100


# --- io ------------------------------------------------------------------------------

@pytest.mark.io
def test_assembly_step_export_round_trips_names_colours_and_poses(zoo_dir, tmp_path):
    from plainsolid import io as pio

    ev = evaluate(parse_file(str(zoo_dir / "node.py")))
    path = pio.export(ev, "step", tmp_path / "node.step")
    back = read_step(path)
    assert back.name == "node"
    leaves = {leaf.name: leaf for leaf in back.leaves()}
    assert set(leaves) == {"box", "lid", "board", "bracket", "gland1", "gland2", "antenna", "panel"}
    assert leaves["lid"].color[:3] == pytest.approx((0.847, 0.847, 0.816), abs=0.01)  # the instance override
    assert leaves["panel"].color[:3] == pytest.approx((0.12, 0.2, 0.45), abs=0.01)  # the vendor file's colour
    assert leaves["box"].color is None
    for root in ev.posed:
        a, b = root.shape.bounding_box(), leaves[root.name].shape.bounding_box()
        assert tuple(a.min) == pytest.approx(tuple(b.min), abs=1e-3) and tuple(a.max) == pytest.approx(tuple(b.max), abs=1e-3)
        assert leaves[root.name].shape.volume == pytest.approx(root.shape.volume, rel=1e-6)
    stl = pio.export(ev, "stl", tmp_path / "node.stl")
    assert stl.stat().st_size > 10000


@pytest.mark.io
def test_vendor_generator_and_step_files_agree(zoo_dir, tmp_path):
    gen = zoo_dir / "vendor" / "gen_vendor.py"
    code = gen.read_text().replace("HERE = Path(__file__).parent", f"HERE = Path({str(tmp_path)!r})")
    (tmp_path / "gen.py").write_text(code)
    subprocess.run([sys.executable, str(tmp_path / "gen.py")], check=True, capture_output=True, timeout=120)
    for name in ("gland_m12.step", "antenna.step", "panel.step"):
        a, b = read_step(tmp_path / name).leaves()[0], read_step(zoo_dir / "vendor" / name).leaves()[0]
        assert a.shape.volume == pytest.approx(b.shape.volume, rel=1e-9) and a.color == b.color and a.product == b.product


# --- roundtrip ------------------------------------------------------------------------

@pytest.mark.roundtrip
def test_adding_instances_and_mates_keeps_the_diff_minimal(project):
    from plainsolid.workspace import Workspace

    ws = Workspace(project)
    doc = ws.create(project / "stack.py", "assembly", "stack")
    original = doc.source
    r = ws.apply(doc, {"op": "add_feature", "kind": "instance", "name": "box", "args": {"path": "enclosure.py"}}, doc.hash)
    r = ws.apply(doc, {"op": "add_feature", "kind": "instance", "name": "lid", "args": {"path": "lid.py", "color": "#dddddd", "at": [0, 0, 60]}}, doc.hash)
    assert r["changed"] and doc.source.endswith('box = instance("box", "enclosure.py")\nlid = instance("lid", "lid.py", at=(0, 0, 60), color="#dddddd")\n')
    r = ws.apply(doc, {"op": "add_feature", "kind": "fixed", "name": "anchor", "args": {"instance": {"expr": "box"}}}, doc.hash)
    added = [ln for ln in r["diff"].splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    assert added == ['+fixed("anchor", box)']
    # a mate solves the poses and writes them back into the instance line, nothing else moves
    r = ws.apply(doc, {"op": "add_feature", "kind": "coincident", "name": "lid_down",
                       "args": {"a": {"expr": 'lid.faces.of("plate").bottom.largest()'}, "b": {"expr": 'box.faces.of("body").top'}}}, doc.hash)
    added = [ln for ln in r["diff"].splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    removed = [ln for ln in r["diff"].splitlines() if ln.startswith("-") and not ln.startswith("---")]
    assert added == ['+lid = instance("lid", "lid.py", at=(0, 0, 40), color="#dddddd")', '+coincident("lid_down", lid.faces.of("plate").bottom.largest(), box.faces.of("body").top)']
    assert removed == ['-lid = instance("lid", "lid.py", at=(0, 0, 60), color="#dddddd")']
    assert original.splitlines()[:3] == doc.source.splitlines()[:3]
    ev = doc.ensure_evaluated()
    assert ev.solution.poses["lid"].at() == pytest.approx((0, 0, 40), abs=1e-6)
    # undo all the way back reproduces the template byte for byte
    for _ in range(4):
        ws.undo(doc)
    assert doc.source == original


# --- api --------------------------------------------------------------------------------

@pytest.fixture
def client(project):
    from plainsolid.server import create_app

    app = create_app(project, serve_client=False)
    with TestClient(app) as c:
        yield c


@pytest.mark.api
def test_assembly_tree_queries_and_dependency_refresh(client, project):
    tree = client.post("/api/documents/open", json={"path": "node.py"}).json()
    did = tree["id"]
    assert tree["kind"] == "assembly" and tree["evaluation"]["assembly"]["dof"] == 3
    kinds = {f["name"]: f["kind"] for f in tree["features"]}
    assert kinds["box"] == "instance" and kinds["panel_tilt"] == "angle" and kinds["anchor"] == "fixed"
    roots = tree["evaluation"]["instances"]
    assert [r["name"] for r in roots] == ["box", "lid", "board", "bracket", "gland1", "gland2", "antenna", "panel"]
    assert roots[1]["kind"] == "part" and roots[1]["color"][:3] == pytest.approx([0.847, 0.847, 0.816], abs=0.01)
    assert roots[1]["transform"][2][3] == pytest.approx(40, abs=1e-6) and roots[4]["kind"] == "step"
    assert tree["evaluation"]["assembly"]["poses"]["antenna"]["at"] == pytest.approx([60, 0, 20], abs=1e-6)
    bom = client.get(f"/api/documents/{did}/query/bom").json()
    assert bom["instances"] == 8 and sum(r["count"] for r in bom["rows"]) == 8
    assert client.get(f"/api/documents/{did}/query/interference").json()["count"] == 0
    assert client.get(f"/api/documents/{did}/query/mass").json()["instances"]["lid"] > 0
    assert client.get(f"/api/documents/{did}/query/summary").json()["dof"] == 3
    mesh = client.get(f"/api/documents/{did}/mesh").content
    from plainsolid import mesh as pmesh
    header = pmesh.decode_header(mesh)
    items = {it["path"]: it for it in header["items"]}
    assert set(items) == {"box", "lid", "board", "bracket", "gland1", "gland2", "antenna", "panel"}
    first_lid_face = items["lid"]["faces"][0]
    assert header["face_labels"][str(first_lid_face)] in ("plate", "corners", "skirt", "rim", "rim_corners", "pocket", "screw", "screws", "edge_break")
    assert header["face_labels"][str(items["gland1"]["faces"][0])] == "gland1"
    # editing a mate value re-solves and writes the new pose back
    src = client.get(f"/api/documents/{did}/source").json()
    r = client.post(f"/api/documents/{did}/edit", json={"op": "set_argument", "feature": "antenna_up", "kwarg": "value", "value": 25, "hash": src["hash"]}).json()
    assert r["changed"]
    text = client.get(f"/api/documents/{did}/source").json()["source"]
    assert 'antenna = instance("antenna", "vendor/antenna.step", material="abs", at=(60, 0, 25), rotate=(0, 90, 0))' in text
    # a change to an instanced part file refreshes the assembly through the dependency list
    ws = client.app.state.workspace
    doc = ws.get(did)
    assert str(project / "lid.py") in doc.ensure_evaluated().dependencies
    assert [d.id for d in ws.dependents_of(project / "lid.py")] == [did]
    with client.websocket_connect(f"/api/documents/{did}/events") as sock:
        assert sock.receive_json()["event"] == "hello"
        lid = project / "lid.py"
        lid.write_text(lid.read_text().replace("thickness = 3.0", "thickness = 6.0"))
        ws.invalidate(doc)
        assert sock.receive_json()["event"] == "dependency"
    tree2 = client.get(f"/api/documents/{did}/tree").json()
    assert tree2["evaluation"]["assembly"]["poses"]["panel"]["at"][2] == pytest.approx(71, abs=1e-6)  # the lid grew by 3, the hinge rose with it


@pytest.mark.api
def test_preview_and_drag_endpoints(client, project):
    tree = client.post("/api/documents/open", json={"path": "node.py"}).json()
    did = tree["id"]
    op = {"op": "add_feature", "kind": "distance", "name": "d", "args": {"a": {"expr": "lid.planes.XY"}, "b": {"expr": "box.planes.XY"}, "value": -40}}
    r = client.post(f"/api/documents/{did}/preview", json={"op": op, "choose_flip": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["flip"] is False and body["rotation"] < 1e-6 and "d" in body["results"]
    assert "redundant" in " ".join(body["results"]["d"]["warnings"]) or "d" in body["assembly"]["redundant"]
    assert client.get(f"/api/documents/{did}/tree").json()["hash"] == tree["hash"]  # nothing written
    r = client.post(f"/api/documents/{did}/drag", json={"instance": "gland1", "rotate": [0, 0.2, 0]})
    assert r.status_code == 200 and r.json()["moved"] and r.json()["poses"]["gland1"]["rotate"][2] == pytest.approx(math.degrees(0.2), abs=0.05)
    r = client.post(f"/api/documents/{did}/drag", json={"instance": "lid", "translate": [1, 0, 0]})
    assert r.status_code == 200 and not r.json()["moved"] and "fully constrained" in r.json()["reason"]
    assert client.post(f"/api/documents/{did}/drag", json={"instance": "nobody", "translate": [1, 0, 0]}).status_code == 400
    part = client.post("/api/documents/open", json={"path": "bracket.py"}).json()
    assert client.post(f"/api/documents/{part['id']}/drag", json={"instance": "x", "translate": [1, 0, 0]}).status_code == 400


@pytest.mark.api
def test_new_assembly_and_export_through_the_api(client, project):
    tree = client.post("/api/documents/new", json={"path": "fresh.py", "kind": "assembly", "name": "fresh"}).json()
    assert tree["kind"] == "assembly" and tree["features"] == [] and tree["evaluation"]["assembly"] is None
    did = tree["id"]
    h = tree["hash"]
    r = client.post(f"/api/documents/{did}/edit", json={"op": "add_feature", "kind": "instance", "name": "b", "args": {"path": "bracket.py"}, "hash": h}).json()
    assert r["changed"]
    out = client.post(f"/api/documents/{did}/export", json={"format": "step", "path": "fresh.step"}).json()
    assert Path(out["path"]).exists() and read_step(out["path"]).leaves()[0].name == "b"
    bad = client.post(f"/api/documents/{did}/edit", json={"op": "add_feature", "kind": "instance", "name": "n", "args": {"path": "node.py"}, "hash": r["hash"]}).json()
    tree = client.get(f"/api/documents/{did}/tree").json()
    assert bad["changed"] and "nested assemblies" in tree["features"][1]["result"]["error"]["message"]


@pytest.mark.cli
def test_cli_queries_and_exports_assemblies(project, tmp_path):
    out = subprocess.run([sys.executable, "-m", "plainsolid.cli", "query", str(project / "node.py"), "bom"], capture_output=True, text=True, timeout=300, check=False)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["instances"] == 8
    out = subprocess.run([sys.executable, "-m", "plainsolid.cli", "export", str(project / "node.py"), "-o", str(tmp_path / "node.step")], capture_output=True, text=True, timeout=300, check=False)
    assert out.returncode == 0 and (tmp_path / "node.step").exists()
