"""Unit: offset entities follow their sources, pick a side, and round or sharpen corners."""
import math

import pytest

from plainsolid import edit
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document

pytestmark = pytest.mark.unit

HEAD = "from plainsolid import *\n"
PI = math.pi


def ev(src: str):
    doc = parse_document(HEAD + src)
    assert not doc.errors, [e.message for e in doc.errors]
    return evaluate(doc)


def test_closed_loop_outside_and_inside_sharp_and_round():
    # a rect drawn as construction, its outside offset extruded: a bigger box
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 40, 20, construction=True)\ns.offset("o", "r", 2)\nbody = extrude("body", s, 5)\n')
    assert all(r.ok for r in e.results), [r.error.message for r in e.results if r.error]
    assert e.body.volume == pytest.approx(44 * 24 * 5)
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 40, 20, construction=True)\ns.offset("o", ["r"], 2, side="inside")\nbody = extrude("body", s, 5)\n')
    assert e.body.volume == pytest.approx(36 * 16 * 5)
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 40, 20, construction=True)\ns.offset("o", "r", 2, corners="round")\nbody = extrude("body", s, 5)\n')
    assert e.body.volume == pytest.approx((44 * 24 - (16 - 4 * PI)) * 5, rel=1e-6)
    # a real rect plus its outside offset: a ring
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 40, 20)\ns.offset("o", "r", 2)\nbody = extrude("body", s, 5)\n')
    assert e.body.volume == pytest.approx((44 * 24 - 40 * 20) * 5)
    assert sorted(t for tags in e.face_tags.values() for t in tags if t.startswith("o")) == ["o.e0", "o.e1", "o.e2", "o.e3"]


def test_open_chain_left_and_right_and_circle():
    src = ('s = sketch("s", on=XY)\ns.line("a", (0, 0), (40, 0))\ns.line("b", (40, 0), (40, 20))\n'
           's.offset("o", ["a", "b"], 3, side="{side}")\n')
    left = ev(src.format(side="left"))
    assert left.result("s").ok
    items = left.result("s").sketch.projected["o"].items
    assert len(items) == 2 and all(i.kind == "line" for i in items)
    ys = sorted(round(c, 6) for i in items for c in (i.coords["start"][1], i.coords["end"][1]))
    assert ys[0] == 3.0  # left of a chain going +x then +y lies above the first line
    right = ev(src.format(side="right"))
    ys = sorted(round(c, 6) for i in right.result("s").sketch.projected["o"].items for c in (i.coords["start"][1], i.coords["end"][1]))
    assert ys[0] == -3.0
    bad = ev(src.format(side="outside"))
    assert "open chain offsets left or right" in bad.result("s").error.message
    e = ev('s = sketch("s", on=XY)\ns.circle("c", 10, construction=True)\ns.offset("o", "c", 2)\nbody = extrude("body", s, 1)\n')
    assert e.body.volume == pytest.approx(PI * 49, rel=1e-6)
    e = ev('s = sketch("s", on=XY)\ns.circle("c", 10)\ns.offset("o", "c", 6, side="inside")\n')
    assert "more than the circle" in e.result("s").error.message


def test_offset_follows_dimensions_and_projected_edges():
    # the source rect is driven by a parameter: the offset moves with it, nothing is written back
    src = HEAD + 'w = 30.0\ns = sketch("s", on=XY)\ns.rect("r", w, 10, construction=True)\ns.offset("o", "r", 1)\nbody = extrude("body", s, 2)\n'
    doc = parse_document(src)
    assert evaluate(doc).body.volume == pytest.approx(32 * 12 * 2)
    doc2 = parse_document(edit.set_parameter(src, "w", "50"))
    assert evaluate(doc2).body.volume == pytest.approx(52 * 12 * 2)
    new = edit.write_back(src, "s", evaluate(doc).result("s").sketch.coords)
    assert new == src
    # an offset of a converted body edge, used as a profile with two more lines
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 40, 20)\nbody = extrude("body", s, 10)\n'
           't = sketch("t", on=body.faces.top)\nt.project("rim", body.edges.top, construction=False)\n'
           't.offset("o", "rim", 3, side="inside")\nlip = cut("lip", t, 2)\n')
    assert all(r.ok for r in e.results), [r.error.message for r in e.results if r.error]
    assert e.body.volume == pytest.approx(8000 - (40 * 20 - 34 * 14) * 2, rel=1e-6)


def test_constraints_may_lean_on_an_offset():
    # a line's end is coincident with an offset corner: solved in a second pass
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 40, 20, construction=True)\ns.offset("o", "r", 2, construction=True)\n'
           's.line("l", (0, 0), (10, 5))\ns.coincident("c1", "l.end", "o.e1.end")\ns.fix("f", "l.start")\n')
    r = e.result("s")
    assert r.ok, r.error and r.error.message
    end = r.sketch.coords["l"]["end"]
    corner = r.sketch.projected["o"].items[1].coords["end"]
    assert end[0] == pytest.approx(corner[0], abs=1e-6) and end[1] == pytest.approx(corner[1], abs=1e-6)


def test_offset_errors_and_editing():
    e = ev('s = sketch("s", on=XY)\ns.line("a", (0, 0), (10, 0))\ns.line("b", (20, 0), (30, 0))\ns.offset("o", ["a", "b"], 1, side="left")\n')
    assert "one connected chain" in e.result("s").error.message
    e = ev('s = sketch("s", on=XY)\ns.line("a", (0, 0), (10, 0))\ns.offset("o", "nope", 1, side="left")\n')
    assert "not a curve" in e.result("s").error.message
    doc = parse_document(HEAD + 's = sketch("s", on=XY)\ns.rect("r", 4, 4)\ns.offset("o", "r", -1)\n')
    assert doc.errors and "positive" in doc.errors[0].message
    doc = parse_document(HEAD + 's = sketch("s", on=XY)\ns.rect("r", 4, 4)\ns.offset("o", "r", 1, side="up")\n')
    assert doc.errors and "side must be" in doc.errors[0].message
    src = HEAD + 's = sketch("s", on=XY)\ns.rect("r", 40, 20)\n'
    new = edit.apply(src, {"op": "add_sketch_entity", "sketch": "s", "kind": "offset", "name": "o1",
                           "args": {"of": ["r"], "distance": 2, "side": "outside", "corners": "sharp", "construction": False}})
    assert new.endswith('s.offset("o1", ["r"], 2)\n')
    new = edit.apply(src, {"op": "add_sketch_entity", "sketch": "s", "kind": "offset", "name": "o1",
                           "args": {"of": ["r.top", "r.right"], "distance": 2, "side": "left", "corners": "round", "construction": True}})
    assert new.endswith('s.offset("o1", ["r.top", "r.right"], 2, side="left", corners="round", construction=True)\n')
    new = edit.apply(new, {"op": "set_entity_argument", "sketch": "s", "entity": "o1", "kwarg": "side", "value": "right"})
    assert 'side="right"' in new
    # converted geometry: construction=True is its default and is dropped, False is written
    new = edit.apply(src, {"op": "add_sketch_entity", "sketch": "s", "kind": "project", "name": "e1",
                           "args": {"selector": {"expr": "body.edges.top"}, "construction": False}})
    assert new.endswith('s.project("e1", body.edges.top, construction=False)\n')
    new = edit.apply(src, {"op": "add_sketch_entity", "sketch": "s", "kind": "project", "name": "e1",
                           "args": {"selector": {"expr": "body.edges.top"}, "construction": True}})
    assert new.endswith('s.project("e1", body.edges.top)\n')
