"""L2 and unit: constraint edit operations, write-back, and the workspace's
solve-and-write-back flow that the sketch editor relies on."""
import pytest

from plainsolid import edit
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document
from plainsolid.workspace import Workspace

SRC = '''from plainsolid import *
w = 40.0
s = sketch("s", on=XY)
s.line("l1", (0, 0), (39.7, 0.2))   # drawn roughly
s.line("l2", (39.7, 0.2), (40, w / 2))
s.circle("c1", 5, at=(10, w / 4))
s.coincident("c0", "l1.end", "l2.start")
s.length("width", "l1", w)
'''


def changed(a, b):
    return [line for line in edit.unified_diff(a, b).splitlines()[2:] if line[:1] in "+-"]


@pytest.mark.roundtrip
def test_constraint_ops_compose_human_statements():
    n = edit.apply(SRC, {"op": "add_constraint", "sketch": "s", "kind": "horizontal", "name": "h1", "refs": ["l1"]})
    assert changed(SRC, n) == ['+s.horizontal("h1", "l1")']
    n = edit.apply(SRC, {"op": "add_constraint", "sketch": "s", "kind": "distance", "name": "d1",
                         "refs": ["c1.center", "l1"], "value": 12.5, "options": {"along": "y"}})
    assert changed(SRC, n) == ['+s.distance("d1", "c1.center", "l1", 12.5, along="y")']
    n = edit.apply(SRC, {"op": "set_constraint_value", "sketch": "s", "constraint": "width", "value": {"expr": "w + 5"}})
    assert changed(SRC, n) == ['-s.length("width", "l1", w)', '+s.length("width", "l1", w + 5)']
    n = edit.apply(SRC, {"op": "delete_constraint", "sketch": "s", "constraint": "c0"})
    assert changed(SRC, n) == ['-s.coincident("c0", "l1.end", "l2.start")']
    with pytest.raises(edit.EditError):
        edit.apply(SRC, {"op": "add_constraint", "sketch": "s", "kind": "length", "name": "x", "refs": ["l1"]})
    n = edit.apply(SRC, {"op": "batch", "ops": [
        {"op": "add_sketch_entity", "sketch": "s", "kind": "line", "name": "l3", "args": {"start": [40, 20], "end": [0, 20.3]}},
        {"op": "add_constraint", "sketch": "s", "kind": "horizontal", "name": "h3", "refs": ["l3"]},
    ]})
    assert changed(SRC, n) == ['+s.line("l3", (40, 20), (0, 20.3))', '+s.horizontal("h3", "l3")']
    assert not parse_document(n).errors


@pytest.mark.roundtrip
def test_write_back_touches_only_literals():
    coords = {"l1": {"start": (0, 0), "end": (40, 0)}, "l2": {"start": (40, 0), "end": (40, 20)},
              "c1": {"at": (10.00004, 10), "diameter": 5}}
    n = edit.apply(SRC, {"op": "write_back", "sketch": "s", "coords": coords})
    assert changed(SRC, n) == [
        '-s.line("l1", (0, 0), (39.7, 0.2))   # drawn roughly',
        '-s.line("l2", (39.7, 0.2), (40, w / 2))',
        '+s.line("l1", (0, 0), (40, 0))   # drawn roughly',
        '+s.line("l2", (40, 0), (40, w / 2))',
    ]
    assert edit.apply(n, {"op": "write_back", "sketch": "s", "coords": coords}) == n


@pytest.mark.roundtrip
def test_dimension_change_writes_back_in_one_commit(project):
    ws = Workspace(project)
    doc = ws.open("bracket.py")
    r = ws.apply(doc, {"op": "set_constraint_value", "sketch": "profile", "constraint": "h", "value": 60}, doc.hash)
    lines = [line for line in r["diff"].splitlines()[2:] if line[:1] in "+-"]
    assert len(lines) == 8 and r["solution"]["dof"] == 0
    assert 'profile.line("top", (-26, 60), (-30, 60))' in doc.source
    assert doc.ensure_evaluated().body.volume == pytest.approx(16203.097396 + 10 * 4 * 40)
    assert ws.undo(doc)["changed"] and 'profile.length("h", "outer_wall", height)' in doc.source


@pytest.mark.roundtrip
def test_drag_diff_is_confined_to_the_dragged_entities(project):
    ws = Workspace(project)
    doc = ws.open("bracket.py")
    r = ws.apply(doc, {"op": "solve_sketch", "sketch": "profile", "drag": {"top.start": [-20, 70]}}, doc.hash)
    assert not r["changed"], "a fully constrained sketch must not move"
    ws.apply(doc, {"op": "delete_constraint", "sketch": "profile", "constraint": "h"}, doc.hash)
    r = ws.apply(doc, {"op": "solve_sketch", "sketch": "profile", "drag": {"top.start": [-20, 70]}}, doc.hash)
    lines = [line for line in r["diff"].splitlines()[2:] if line[:1] in "+-"]
    assert len(lines) == 6 and all(name in "".join(lines) for name in ("inner_wall", "top", "outer_wall"))
    assert "bottom" not in "".join(lines) and "hole" not in "".join(lines)
    assert r["solution"]["free_entities"] == ["inner_wall", "top", "outer_wall"]


@pytest.mark.unit
def test_projection_and_dimension_names():
    src = '''from plainsolid import *
s = sketch("s", on=XY)
s.rect("r", 40, 20)
e = extrude("e", s, 10)
h = sketch("h", on=XY, offset=10)
h.project("left", e.edges.where(parallel_to="+Y").nearest((-20, 0, 10)))
h.project("top_face", e.faces.where(normal="+Z"), construction=False)
h.project("front", e.edges.where(parallel_to="+X").nearest((0, -10, 10)))
h.circle("c", 6, at=(-8, -3))
h.distance("dx", "c.center", "left", 12)
h.distance("dy", "c.center", "front", 7)
c2 = extrude("boss", h, h.dy)
'''
    doc = parse_document(src)
    assert not doc.errors and doc.feature("boss").args["depth"] == 7.0
    ev = evaluate(doc)
    assert all(r.ok for r in ev.results), [r.error.message for r in ev.results if r.error]
    sol = ev.result("h").sketch
    assert sol.projected["left"].items[0].kind == "line"
    assert len(sol.projected["top_face"].items) == 4
    assert sol.coords["c"]["at"] == pytest.approx((-20 + 12, -10 + 7), abs=1e-6)
    bad = parse_document(src.replace('nearest((-20, 0, 10))', 'nearest((-20, 0, 10))[0]'))
    assert bad.errors and "indexing a selector" in bad.errors[0].message


@pytest.mark.roundtrip
def test_setting_a_flag_back_to_its_default_drops_the_keyword():
    on = edit.apply(SRC, {"op": "set_entity_argument", "sketch": "s", "entity": "l1", "kwarg": "construction", "value": True})
    assert 's.line("l1", (0, 0), (39.7, 0.2), construction=True)   # drawn roughly' in on
    off = edit.apply(on, {"op": "set_entity_argument", "sketch": "s", "entity": "l1", "kwarg": "construction", "value": False})
    assert off == SRC
    assert edit.apply(SRC, {"op": "set_entity_argument", "sketch": "s", "entity": "l1", "kwarg": "construction", "value": False}) == SRC
    src = 'from plainsolid import *\ns = sketch("s", on=XY)\ns.rect("r", 1, 1)\ne = extrude("e", s, 5, flip=True)\n'
    assert edit.apply(src, {"op": "set_argument", "feature": "e", "kwarg": "flip", "value": False}).endswith('e = extrude("e", s, 5)\n')
    assert edit.apply(src, {"op": "set_argument", "feature": "e", "kwarg": "draft", "value": 0}) == src
