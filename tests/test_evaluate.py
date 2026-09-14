import pytest

from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document

pytestmark = pytest.mark.unit

HEAD = "from plainsolid import *\n"


def ev(src: str):
    doc = parse_document(HEAD + src)
    assert not doc.errors, doc.errors
    return evaluate(doc)


def test_extrude_directions_and_symmetric():
    e = ev('s = sketch("s", on=XZ)\ns.rect("r", 10, 20)\nextrude("e", s, 5)\n')
    bb = e.body.bounding_box()
    assert (round(bb.min.Y, 6), round(bb.max.Y, 6)) == (-5, 0)  # XZ normal is -Y
    e = ev('s = sketch("s", on=XZ)\ns.rect("r", 10, 20)\nextrude("e", s, 5, flip=True)\n')
    bb = e.body.bounding_box()
    assert (round(bb.min.Y, 6), round(bb.max.Y, 6)) == (0, 5)
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 10, 20)\nextrude("e", s, 8, symmetric=True)\n')
    bb = e.body.bounding_box()
    assert (round(bb.min.Z, 6), round(bb.max.Z, 6)) == (-4, 4)


def test_offset_plane_and_draft():
    e = ev('s = sketch("s", on=XY, offset=7)\ns.rect("r", 10, 10)\nextrude("e", s, 10)\n')
    assert round(e.body.bounding_box().min.Z, 6) == 7
    e2 = ev('s = sketch("s", on=XY)\ns.rect("r", 10, 10)\nextrude("e", s, 10, draft=5)\n')
    assert e2.body.volume < 1000


def test_nested_profile_makes_a_hole():
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 40, 20)\ns.circle("c", 10)\nextrude("e", s, 2)\n')
    assert e.body.volume == pytest.approx((800 - 3.14159265 * 25) * 2, rel=1e-6)
    # an island inside a hole is a second, disconnected solid: rejected by the one-body rule
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 40, 20)\ns.circle("c", 10)\ns.circle("i", 4)\nextrude("e", s, 2)\n')
    assert e.body is None and "separate solids" in e.result("e").error.message


def test_lines_close_into_a_profile_and_construction_is_ignored():
    e = ev('s = sketch("s", on=XY)\n'
           's.line("a", (0,0), (10,0))\ns.line("b", (10,0), (10,5))\ns.line("c", (10,5), (0,0))\n'
           's.line("x", (0,0), (5,5), construction=True)\n'
           'extrude("e", s, 2)\n')
    assert e.body.volume == pytest.approx(50, rel=1e-6)


def test_open_chain_warns_and_empty_sketch_fails():
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 4, 4)\ns.line("a", (0,0), (10,0))\nextrude("e", s, 1)\n')
    assert e.result("s").warnings == ["1 open line chain(s) ignored"]
    e = ev('s = sketch("s", on=XY)\ns.line("a", (0,0), (10,0))\nextrude("e", s, 1)\n')
    assert e.result("s").ok and "no closed profile" in e.result("s").warnings  # a sketch may be empty or open
    assert not e.result("e").ok and "no closed profile" in e.result("e").error.message
    assert e.body is None
    e = ev('s = sketch("s", on=XY)\ns.circle("ref", 10, construction=True)\n')
    assert e.result("s").ok and e.result("s").warnings == ["no closed profile"]


def test_failed_feature_is_skipped_and_downstream_continues():
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 10, 10)\nextrude("e", s, 10)\n'
           'c = sketch("c", on=XY)\nc.circle("hole", -1)\ncut("bad", c, through=True)\n'
           'd = sketch("d", on=XY)\nd.circle("h2", 2)\ncut("good", d, through=True)\n')
    assert not e.result("c").ok and not e.result("bad").ok
    assert e.result("good").ok
    assert e.body.volume == pytest.approx(1000 - 3.14159265 * 10, rel=1e-6)
    assert e.result("bad").error.line == 7


def test_split_body_and_cut_without_body_are_errors():
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 30, 10)\nextrude("e", s, 5)\n'
           'c = sketch("c", on=XY)\nc.rect("k", 2, 20)\ncut("split", c, through=True)\n')
    assert "separate solids" in e.result("split").error.message
    assert e.body.volume == pytest.approx(1500)
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 30, 10)\ncut("nothing", s, 5)\n')
    assert "nothing to cut" in e.result("nothing").error.message


def test_rollback_upto_and_suppressed():
    src = 's = sketch("s", on=XY)\ns.rect("r", 10, 10)\nextrude("e", s, 10)\nc = sketch("c", on=XY)\nc.circle("h", 2)\ncut("k", c, through=True)\n'
    doc = parse_document(HEAD + src)
    part = evaluate(doc, upto="e")
    assert part.body.volume == pytest.approx(1000) and len(part.results) == 2
    doc.feature("k").suppressed = True
    full = evaluate(doc)
    assert full.result("k").skipped and full.body.volume == pytest.approx(1000)


def test_face_attribution(bracket_eval):
    labels = bracket_eval.face_labels()
    assert len(labels) == 20 and set(labels) <= {"body", "inner", "outer", "hole_cut", "slot_cut"}
    # the two hole walls are the cut's; the base's top and bottom, split by the holes, stay the body's
    assert bracket_eval.result("hole_cut").faces_created == 2
    assert labels.count("hole_cut") == 2 and labels.count("body") >= 8
