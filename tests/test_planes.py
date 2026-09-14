"""Unit and roundtrip: reference planes, sketches on faces and planes, cascade delete."""
import pytest

from plainsolid import edit
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document
from plainsolid.workspace import Workspace

HEAD = '''from plainsolid import *
s = sketch("base", on=XY)
s.rect("r", 40, 20)
body = extrude("body", s, 10)
'''


def ev(src: str):
    doc = parse_document(HEAD + src)
    assert not doc.errors, [e.message for e in doc.errors]
    return evaluate(doc)


@pytest.mark.unit
def test_sketch_on_a_face_and_on_a_plane_feature():
    e = ev('top = sketch("top", on=body.faces.where(normal="+Z").largest())\ntop.circle("c", 6, at=(10, 0))\nextrude("boss", top, 5)\n')
    assert e.result("top").ok and e.result("top").plane["origin"] == [0.0, 0.0, 10.0]
    assert e.body.bounding_box().max.Z == pytest.approx(15)
    e = ev('side = sketch("side", on=body.faces.nearest((20, 0, 5)), flip=True)\nside.circle("h", 4)\ncut("hole", side, 8)\n')
    assert e.result("hole").ok and e.body.volume == pytest.approx(8000 - 3.14159265 * 4 * 8, rel=1e-6)
    assert e.result("side").plane["z_dir"] == [-1.0, 0.0, 0.0] and e.result("side").plane["x_dir"] == [0.0, 1.0, 0.0]
    e = ev('p = plane("p", XY, offset=10)\nk = sketch("k", on=p)\nk.rect("q", 10, 10)\nextrude("lid", k, 2)\n')
    assert e.result("lid").ok and e.body.bounding_box().max.Z == pytest.approx(12)
    e = ev('k = sketch("k", on=body.faces.nearest((0, 0, 10)), offset=5)\nk.rect("q", 10, 10)\n')
    assert e.result("k").plane["origin"][2] == 15.0


@pytest.mark.unit
def test_plane_forms():
    e = ev('p2 = plane("p2", body.faces.where(normal="+Z").largest(), offset=3)\n'
           'p3 = plane("p3", XY, angle=30, about=body.edges.where(parallel_to="+Y").nearest((-20, 0, 10)))\n'
           'p4 = plane("p4", between=(body.faces.where(normal="+Y").largest(), body.faces.where(normal="-Y").largest()))\n'
           'p5 = plane("p5", through=(body.vertices.nearest((-20, -10, 0)), body.vertices.nearest((20, -10, 0)), (0, 0, 40)))\n'
           'p6 = plane("p6", XY, offset=4, flip=True)\n')
    r = {x.name: x for x in e.results}
    assert r["p2"].plane["origin"][2] == 13.0
    assert r["p3"].plane["z_dir"] == pytest.approx([-0.5, 0.0, 0.866025], abs=1e-5)
    assert r["p4"].plane["origin"] == pytest.approx([0, 0, 5], abs=1e-6) and abs(r["p4"].plane["z_dir"][1]) == 1.0
    assert r["p5"].plane["origin"] == [-20.0, -10.0, 0.0] and r["p5"].plane["x_dir"] == [1.0, 0.0, 0.0]
    assert r["p6"].plane["z_dir"] == [0.0, 0.0, -1.0] and r["p6"].plane["origin"][2] == 4.0


@pytest.mark.unit
def test_plane_and_face_errors():
    e = ev('bad = plane("bad", between=(XY, XZ))\n')
    assert "parallel" in e.result("bad").error.message and e.result("bad").error.message.count("plane 'bad'") == 1
    e = ev('k = sketch("k", on=body.faces.where(kind="cylinder").largest())\n')
    assert "matches nothing" in e.result("k").error.message
    e = ev('p = plane("p", XY, angle=20, about=body.edges.where(parallel_to="+Z").nearest((20, 10, 5)))\n')
    assert "lie on the base plane" in e.result("p").error.message
    doc = parse_document('from plainsolid import *\nk = sketch("k", on=XY)\nk.rect("q", 1, 1)\nlater = sketch("z", on=body_missing)\n')
    assert doc.errors
    doc = parse_document('from plainsolid import *\np = plane("p", XY, offset=1, between=(XY, XY))\n')
    assert "exactly one" in doc.errors[0].message
    doc = parse_document(HEAD + 'first = sketch("first", on=body.faces.largest())\nfirst.rect("a", 1, 1)\np = plane("later", first)\n')
    assert doc.errors and "is a sketch, not a plane" in doc.errors[0].message


@pytest.mark.roundtrip
def test_cascade_delete_keeps_the_file_valid(bracket_src):
    dep = edit.dependents(bracket_src, "profile")
    assert dep["features"] == ["body", "inner", "outer"] and "bottom" in dep["entities"] and "wall_t" in dep["entities"]
    assert edit.dependents(bracket_src, "body")["entities"] == ["back_edge", "side_edge", "y1", "y2", "x1", "x2", "wall_top", "wall_end", "down", "in"]
    for name in ("profile", "body", "holes", "slots"):
        new = edit.apply(bracket_src, {"op": "delete_feature", "feature": name})
        doc = parse_document(new)
        assert not doc.errors, (name, [e.message for e in doc.errors])
        assert doc.feature(name) is None
        assert sum(line.strip().startswith("#") for line in new.splitlines()) == 6  # every comment of the file survives
    new = edit.apply(bracket_src, {"op": "delete_feature", "feature": "profile", "cascade": False})
    assert parse_document(new).errors  # without cascade the file dangles, as asked


@pytest.mark.roundtrip
def test_sketch_on_face_and_plane_ops_write_human_text(project):
    ws = Workspace(project)
    doc = ws.open("bracket.py")
    r = ws.apply(doc, {"op": "add_feature", "kind": "sketch", "name": "top_sk",
                       "args": {"on": {"expr": 'body.faces.where(normal="+Z").nearest((0, -20, 4))'}}, "after": "body"}, doc.hash)
    assert '+top_sk = sketch("top_sk", on=body.faces.where(normal="+Z").nearest((0, -20, 4)))' in r["diff"]
    r = ws.apply(doc, {"op": "add_sketch_entity", "sketch": "top_sk", "kind": "circle", "name": "c1",
                       "args": {"diameter": 3, "at": [5, 2]}}, doc.hash)
    assert r["changed"] and r["solution"]["dof"] == 3
    tree = doc.tree_json()
    top = next(f for f in tree["features"] if f["name"] == "top_sk")
    assert top["result"]["ok"] and top["result"]["plane"]["origin"] == [2.0, -20.0, 4.0]
    assert next(f for f in tree["features"] if f["name"] == "profile")["dependents"]["features"] == ["body", "top_sk", "inner", "outer"]
    r = ws.apply(doc, {"op": "add_feature", "kind": "plane", "name": "p1", "args": {"base": "XY", "offset": 25}, "after": "body"}, doc.hash)
    assert '+p1 = plane("p1", XY, offset=25)' in r["diff"]
    r = ws.apply(doc, {"op": "add_feature", "kind": "plane", "name": "p2",
                       "args": {"base": "XY", "angle": 30, "about": {"expr": "body.edges.nearest((0, 0, 0))"}}}, doc.hash)
    assert '+p2 = plane("p2", XY, angle=30, about=body.edges.nearest((0, 0, 0)))' in r["diff"]
    assert all(f["result"]["ok"] for f in doc.tree_json()["features"])


@pytest.mark.unit
def test_converted_vertices_and_edges_land_in_the_sketch_frame():
    """A sketch on the back wall: its origin is the wall's centre, its y points up the
    wall and its x to the right of someone looking at the wall from outside (+Y), so
    global +X is sketch -x. A converted floor vertex and floor edge must read in that
    frame, not in global coordinates (vertices used to come through untransformed)."""
    src = HEAD + ('w = sketch("w", on=body.faces.from_sketch("r.top"))\n'
                  'w.project("corner", body.vertices.nearest((20, 10, 0)))\n'
                  'w.project("floor", body.edges.bottom.from_sketch("r.top"))\n'
                  'w.circle("c", 3, at=(0, 0))\nw.coincident("cc", "c.center", "corner")\n'
                  'boss = extrude("boss", w, 2)\n')
    doc = parse_document(src)
    assert not doc.errors, [e.message for e in doc.errors]
    e = evaluate(doc)
    r = e.result("w")
    assert r.ok, r.error and r.error.message
    plane = e.sketches["w"].plane
    assert list(plane.origin) == pytest.approx([0, 10, 5]) and list(plane.y_dir) == pytest.approx([0, 0, 1])
    assert list(plane.x_dir) == pytest.approx([-1, 0, 0])
    assert r.sketch.projected["corner"].items[0].coords["at"] == pytest.approx((-20, -5))
    floor = r.sketch.projected["floor"].items[0].coords
    assert sorted([floor["start"][0], floor["end"][0]]) == pytest.approx([-20, 20]) and floor["start"][1] == pytest.approx(-5)
    assert r.sketch.coords["c"]["at"] == pytest.approx((-20, -5))
    # the boss grew on the wall at the corner: its centre sits at global (20, 10, 0)
    assert e.result("boss").ok
    bb = e.body.bounding_box()
    assert bb.max.Y == pytest.approx(12) and bb.min.Z == pytest.approx(-1.5)  # diameter 3
