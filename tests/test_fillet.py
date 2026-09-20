"""Unit and round-trip: fillets and chamfers at sketch corners, on line pairs and on
rect and polygon corners, and their removal, through the workspace so the solve and
write-back run as the GUI's do."""
import math

import pytest

from plainsolid import edit
from plainsolid.corners import corner_geometry, macro_corners
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document
from plainsolid.workspace import Workspace


def sketch_solution(ev, name):
    return next(r.sketch for r in ev.results if r.name == name)


def tagged(ev, text):
    return any(text in t for tags in ev.face_tags.values() for t in tags)

L = '''from plainsolid import *
w = 40.0
s = sketch("s", on=XY)
s.line("bottom", (0, 0), (40, 0))
s.line("right", (40, 0), (40, 20))
s.line("top", (40, 20), (0, 20))
s.line("left", (0, 20), (0, 0))
s.coincident("c1", "bottom.end", "right.start")
s.coincident("c2", "right.end", "top.start")
s.coincident("c3", "top.end", "left.start")
s.coincident("c4", "left.end", "bottom.start")
s.horizontal("h1", "bottom")
s.horizontal("h2", "top")
s.vertical("v1", "right")
s.vertical("v2", "left")
s.coincident("pin", "bottom.start", "origin")
s.length("width", "bottom", w)
s.length("height", "right", 20)
body = extrude("body", s, 5)
'''


def write(tmp_path, text, name="part.py"):
    p = tmp_path / name
    p.write_text(text)
    return p


@pytest.mark.unit
def test_corner_geometry_places_the_arc_tangent_to_both_sides():
    g = corner_geometry((40, 0), (0, 0), (0, 20), radius=5)  # the bottom-left corner of a plate
    assert g["t1"] == pytest.approx((5, 0)) and g["t2"] == pytest.approx((0, 5)) and g["center"] == pytest.approx((5, 5))
    assert g["start"] == pytest.approx((0, 5)) and g["end"] == pytest.approx((5, 0))  # counter-clockwise about the centre
    c = corner_geometry((40, 0), (0, 0), (0, 20), chamfer=3)
    assert c["t1"] == pytest.approx((3, 0)) and c["t2"] == pytest.approx((0, 3)) and "center" not in c
    with pytest.raises(ValueError, match="does not fit"):
        macro_corners("rect", {"at": (0, 0), "width": 10, "height": 40, "corners": 6}, "rect 'r'")
    with pytest.raises(ValueError, match="both"):
        macro_corners("rect", {"at": (0, 0), "width": 10, "height": 40, "corners": {"tl": 2}, "chamfers": {"tl": 2}}, "rect 'r'")
    cuts = macro_corners("rect", {"at": (0, 0), "width": 40, "height": 20, "corners": {"tl": 4}}, "rect 'r'")
    assert list(cuts) == ["tl"] and cuts["tl"]["center"] == pytest.approx((-16, 6))


@pytest.mark.roundtrip
def test_fillet_two_lines_keeps_the_geometry_and_moves_the_length_to_the_sharp(tmp_path):
    ws = Workspace(tmp_path)
    doc = ws.open(write(tmp_path, L).name)
    volume = doc.ensure_evaluated().body.volume
    r = ws.apply(doc, {"op": "fillet_corners", "sketch": "s", "corners": [{"a": "bottom.end", "b": "right.start"}], "size": 5}, doc.hash)
    src = doc.source
    assert r["changed"] and r["solution"]["dof"] == 0 and not r["solution"]["conflicting"], r["solution"]
    assert 's.line("bottom", (0, 0), (35, 0))' in src and 's.line("right", (40, 5), (40, 20))' in src
    assert 's.arc("fillet1", (35, 5), (35, 0), (40, 5))' in src
    assert 's.coincident("c1", "bottom.end", "right.start")' not in src
    assert 's.coincident("c5", "bottom.end", "fillet1.start")' in src and 's.coincident("c6", "right.start", "fillet1.end")' in src
    assert 's.tangent("t1", "fillet1", "bottom")' in src and 's.tangent("t2", "fillet1", "right")' in src
    assert 's.point("sharp1", (40, 0))' in src and 's.on("on1", "sharp1", "bottom")' in src and 's.on("on2", "sharp1", "right")' in src
    # the lengths of both lines became distances to the sharp, their values untouched
    assert 's.distance("width", "bottom.start", "sharp1", w)' in src and 's.distance("height", "right.end", "sharp1", 20)' in src
    assert 's.radius("rad1", "fillet1", 5, at=' in src
    ev = doc.ensure_evaluated()
    assert not ev.errors
    assert ev.body.volume == pytest.approx(volume - (1 - math.pi / 4) * 25 * 5)  # one rounded corner
    # the corner's face labels: the arc is a face of its own
    assert tagged(ev, "fillet1")
    # undo restores the file exactly
    assert ws.undo(doc)["changed"] and doc.source == L


@pytest.mark.roundtrip
def test_fillet_refuses_what_does_not_fit_or_is_not_a_corner(tmp_path):
    ws = Workspace(tmp_path)
    doc = ws.open(write(tmp_path, L).name)
    with pytest.raises(edit.EditError, match="does not fit"):
        ws.apply(doc, {"op": "fillet_corners", "sketch": "s", "corners": [{"a": "bottom.end", "b": "right.start"}], "size": 25}, doc.hash)
    with pytest.raises(edit.EditError, match="do not meet"):
        ws.apply(doc, {"op": "fillet_corners", "sketch": "s", "corners": [{"a": "bottom.end", "b": "top.start"}], "size": 2}, doc.hash)
    with pytest.raises(edit.EditError, match="also ends"):
        ws.apply(doc, {"op": "batch", "sketch": "s", "ops": [
            {"op": "add_sketch_entity", "sketch": "s", "kind": "line", "name": "spur", "args": {"start": [40, 0], "end": [60, -10]}}]}, doc.hash)
        ws.apply(doc, {"op": "fillet_corners", "sketch": "s", "corners": [{"a": "bottom.end", "b": "right.start"}], "size": 2}, doc.hash)


@pytest.mark.roundtrip
def test_several_corners_share_one_dimension_and_unfillet_restores_the_corner(tmp_path):
    ws = Workspace(tmp_path)
    doc = ws.open(write(tmp_path, L).name)
    ws.apply(doc, {"op": "fillet_corners", "sketch": "s", "size": 3, "kind": "chamfer",
                   "corners": [{"a": "right.end", "b": "top.start"}, {"a": "top.end", "b": "left.start"}]}, doc.hash)
    src = doc.source
    assert 's.line("chamfer1", (40, 17), (37, 20))' in src and 's.line("chamfer2", (3, 20), (0, 17))' in src
    assert 's.distance("d1", "sharp1", "chamfer1.start", 3)' in src and 's.distance("d2", "sharp1", "chamfer1.end", 3)' in src
    assert 's.equal("eq1", "chamfer2", "chamfer1")' in src and 's.distance("d3"' not in src
    assert not doc.ensure_evaluated().errors
    r = ws.apply(doc, {"op": "unfillet", "sketch": "s", "entity": "chamfer1"}, doc.hash)
    src = doc.source
    assert r["changed"] and "chamfer1" not in src and "sharp1" not in src and 's.line("right", (40, 0), (40, 20))' in src
    assert 's.coincident("c' in src and '"right.end", "top.start")' in src
    assert 's.distance("height", "right.end", "top.end", 20)' not in src  # the height went back to the corner it names
    assert not doc.ensure_evaluated().errors


@pytest.mark.roundtrip
def test_macro_corners_take_an_argument_with_a_dimension_and_write_back_their_form(tmp_path):
    src = ('from plainsolid import *\ns = sketch("s", on=XY)\ns.rect("plate", 160, 94, at=(0, 0))\n'
           's.fix("f", "plate")\nbody = extrude("body", s, 4)\n')
    ws = Workspace(tmp_path)
    doc = ws.open(write(tmp_path, src).name)
    ws.apply(doc, {"op": "fillet_corners", "sketch": "s", "size": 8,
                   "corners": [{"entity": "plate", "corner": c} for c in ("tl", "tr", "br", "bl")]}, doc.hash)
    text = doc.source
    assert 's.rect("plate", 160, 94, at=(0, 0), corners=8)' in text
    assert 's.radius("rad1", "plate.tl_arc", 8, at=' in text and text.count('s.equal(') == 3 and '"plate.bl_arc", "plate.tl_arc")' in text
    ev = doc.ensure_evaluated()
    assert not ev.errors and ev.body.volume == pytest.approx((160 * 94 - (4 - math.pi) * 64) * 4)
    assert tagged(ev, "plate.tl_arc") and tagged(ev, "plate.top")
    # editing the shared dimension changes all four and keeps the number form
    ws.apply(doc, {"op": "set_constraint_value", "sketch": "s", "constraint": "rad1", "value": 5}, doc.hash)
    assert 'corners=5' in doc.source
    # one corner on its own: the dict form; a chamfer replaces a rounding
    ws.apply(doc, {"op": "unfillet", "sketch": "s", "entity": "plate.br_arc"}, doc.hash)
    assert 'corners={"tl": 5, "tr": 5, "bl": 5}' in doc.source and "plate.br_arc" not in doc.source
    ws.apply(doc, {"op": "fillet_corners", "sketch": "s", "size": 3, "kind": "chamfer", "corners": [{"entity": "plate", "corner": "tl"}]}, doc.hash)
    assert 'corners={"tr": 5, "bl": 5}, chamfers={"tl": 3}' in doc.source and 's.distance("d1", "plate.tl", "plate.tl_chamfer.start", 3)' in doc.source
    assert not doc.ensure_evaluated().errors
    with pytest.raises(edit.EditError, match="does not fit"):
        ws.apply(doc, {"op": "fillet_corners", "sketch": "s", "size": 100, "corners": [{"entity": "plate", "corner": "bl"}]}, doc.hash)


@pytest.mark.solver
def test_polygon_corner_arcs_are_references_the_solver_can_relate_to():
    src = ('from plainsolid import *\ns = sketch("s", on=XY)\ns.polygon("tri", [(0, 0), (40, 0), (20, 30)], corners={"p2": 5})\n'
           's.fix("f", "tri")\ns.circle("c", 4, at=(10, 10))\ns.diameter("dc", "c", 4)\ns.concentric("cc", "c", "tri.p2_arc")\ns.radius("r", "tri.p2_arc", 5)\n')
    doc = parse_document(src)
    assert not doc.errors, [e.message for e in doc.errors]
    ev = evaluate(doc)
    sol = sketch_solution(ev, "s")
    assert sol is not None and sol.dof == 0 and not sol.conflicting
    g = macro_corners("polygon", {"points": [(0, 0), (40, 0), (20, 30)], "corners": {"p2": 5}}, "tri")["p2"]
    assert sol.coords["c"]["at"] == pytest.approx(g["center"], abs=1e-6)
