"""Unit: part features and the identity map behind semantic selectors."""
import math

import pytest

from plainsolid import edit
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document

pytestmark = pytest.mark.unit

HEAD = "from plainsolid import *\n"
BASE = 's = sketch("s", on=XY)\ns.rect("r", 40, 20)\nbody = extrude("body", s, 10)\n'
PI = math.pi


def ev(src: str, **kw):
    doc = parse_document(HEAD + src)
    assert not doc.errors, [e.message for e in doc.errors]
    return evaluate(doc, **kw)


def owned(e, owner: str) -> list[tuple[str, ...]]:
    return sorted(e.face_tags.get(hash(f.wrapped), ()) for f in e.body.faces() if e.face_owner.get(hash(f.wrapped)) == owner)


def test_extrusion_labels_ends_and_sides():
    e = ev(BASE)
    assert owned(e, "body") == [(":bottom",), (":top",), ("r.bottom",), ("r.left",), ("r.right",), ("r.top",)]
    edge_tags = set(e.edge_tags.values())
    assert (":top", "r.right") in edge_tags and ("r.bottom", "r.left") in edge_tags and len(edge_tags) == 12
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 40, 20)\nbody = extrude("body", s, 10, flip=True)\n')
    top = [f for f in e.body.faces() if e.face_tags[hash(f.wrapped)] == (":top",)]
    assert top[0].center().Z == pytest.approx(-10)  # top follows the direction of extrusion


def test_semantic_selectors_and_split_faces_keep_their_owner():
    e = ev(BASE + 't = sketch("t", on=body.faces.top)\nt.circle("c", 8, at=(10, 0))\nboss = extrude("boss", t, 6)\n'
           'h = sketch("h", on=boss.faces.top)\nh.circle("hole", 4)\nhole = cut("hole", h, upto=body.faces.bottom)\n')
    assert all(r.ok for r in e.results), [r.error.message for r in e.results if r.error]
    assert e.body.bounding_box().max.Z == pytest.approx(16)
    assert e.body.volume == pytest.approx(8000 + PI * 16 * 6 - PI * 4 * 16, rel=1e-6)
    # the body's top face was split by the boss and pierced by the hole: still the body's "top"
    assert (":top",) in owned(e, "body") and owned(e, "boss") == [(":top",), ("c",)]
    assert owned(e, "hole") == [("hole",)]


def test_fillet_and_chamfer_take_the_replaced_edge_tags():
    e = ev(BASE + 'f1 = fillet("f1", body.edges.top.from_sketch("r.right"), 3)\n'
           'c1 = chamfer("c1", body.edges.bottom, 1, distance2=2)\n')
    assert all(r.ok for r in e.results)
    assert owned(e, "f1") == [(":top", "r.right")]
    assert owned(e, "c1") == [(":bottom", "r.bottom"), (":bottom", "r.left"), (":bottom", "r.right"), (":bottom", "r.top")]
    assert e.result("f1").faces_created == 1 and e.result("c1").faces_created == 4
    assert e.body.volume == pytest.approx(8000 - (9 - PI * 9 / 4) * 20 - 4 * 2 * 1 / 2 * 40 - 2 * 2 * 1 / 2 * 20 + 4 * (2 / 3), rel=0.02)
    # a list of selectors rounds the union of what they pick, once each
    e = ev(BASE + 'f2 = fillet("f2", [body.edges.top.from_sketch("r.right"), body.edges.top.from_sketch("r.left"), body.edges.top.from_sketch("r.right")], 2)\n')
    assert e.result("f2").ok and e.result("f2").faces_created == 2 and owned(e, "f2") == [(":top", "r.left"), (":top", "r.right")]
    doc = parse_document(HEAD + BASE + 'f3 = fillet("f3", [], 2)\n')
    assert doc.errors and "empty" in doc.errors[0].message
    bad = ev(BASE + 'f = fillet("f", body.edges.top, 50)\n')
    assert not bad.result("f").ok and "could not fillet" in bad.result("f").error.message
    assert bad.body.volume == pytest.approx(8000)  # skipped, the body passes through


def test_shell_open_closed_and_outward():
    e = ev(BASE + 'sh = shell("sh", body.faces.top, 2)\n')
    assert e.result("sh").ok and e.body.volume == pytest.approx(8000 - 36 * 16 * 8)
    assert (":bottom", ":inner") in owned(e, "sh") and ("r.left", ":inner") in owned(e, "sh")
    e = ev(BASE + 'sh = shell("sh", None, 2)\n')
    assert e.body.volume == pytest.approx(8000 - 36 * 16 * 6) and len(e.body.solids()) == 1
    e = ev(BASE + 'sh = shell("sh", body.faces.top, 2, outward=True)\n')
    assert e.body.volume > 4000 and e.body.bounding_box().min.Z == pytest.approx(-2)


def test_revolve_about_a_sketch_line_and_a_global_axis():
    e = ev('p = sketch("p", on=XZ)\np.rect("r", 10, 30, at=(15, 15))\np.line("axis", (0, -5), (0, 40), construction=True)\n'
           'rv = revolve("rv", p, "axis")\n')
    assert e.result("rv").ok and e.body.volume == pytest.approx(PI * (20 ** 2 - 10 ** 2) * 30, rel=1e-6)
    assert owned(e, "rv") == [("r.bottom",), ("r.left",), ("r.right",), ("r.top",)]
    e = ev('q = sketch("q", on=XZ)\nq.circle("c", 6, at=(15, 15))\nrv2 = revolve("rv2", q, Z, angle=90)\n')
    assert e.body.volume == pytest.approx(PI * 9 * 2 * PI * 15 / 4, rel=1e-6)
    assert owned(e, "rv2") == [(":end",), (":start",), ("c",)]


def test_revolve_validation():
    e = ev('p = sketch("p", on=XY)\np.rect("r", 10, 30, at=(15, 15))\nrevolve("rv", p, Z)\n')
    assert "axis must lie in the sketch plane" in e.result("rv").error.message
    e = ev('p = sketch("p", on=XZ)\np.rect("r", 10, 30, at=(0, 15))\nrevolve("rv", p, Z)\n')
    assert "crosses the axis" in e.result("rv").error.message
    e = ev('p = sketch("p", on=XZ)\np.rect("r", 10, 30, at=(15, 15))\nrevolve("rv", p, "nope")\n')
    assert "not a line" in e.result("rv").error.message
    doc = parse_document(HEAD + 'p = sketch("p", on=XZ)\np.rect("r", 1, 1)\nrevolve("rv", p, Z, angle=400)\n')
    assert doc.errors and "between 0 and 360" in doc.errors[0].message


def test_linear_and_circular_patterns_and_mirror():
    e = ev(BASE + 't = sketch("t", on=body.faces.top)\nt.circle("c", 3, at=(-17, -5))\nboss = extrude("boss", t, 5)\n'
           'lp = linear_pattern("lp", boss, 3, spacing=10, direction=X, count2=2, spacing2=10, direction2=Y)\n'
           'm = mirror("m", [boss, lp], about=YZ)\n')
    assert all(r.ok for r in e.results), [r.error.message for r in e.results if r.error]
    assert e.body.volume == pytest.approx(8000 + 12 * PI * 2.25 * 5, rel=1e-6)
    assert owned(e, "lp") == [(":top",)] * 5 + [("c",)] * 5 and len(owned(e, "m")) == 12
    e = ev('s = sketch("s", on=XY)\ns.rect("r", 20, 20, at=(10, 0))\ns.circle("hole", 3, at=(15, 0))\nbody = extrude("body", s, 5)\n'
           'cp = circular_pattern("cp", body, 6, axis=Z)\nmb = mirror("mb", about=XY)\n')
    assert all(r.ok for r in e.results) and len(e.body.solids()) == 1
    assert e.body.bounding_box().min.Z == pytest.approx(-5) and e.body.bounding_box().max.Z == pytest.approx(5)
    assert owned(e, "cp").count(("hole",)) == 5
    e = ev(BASE + 'h = sketch("h", on=body.faces.top)\nh.circle("hh", 2, at=(-15, 0))\nhole = cut("hole", h, through=True)\n'
           'holes = circular_pattern("holes", hole, 4, axis=body.faces.top.nearest((0, 0, 10)).where(kind="plane"), angle=90)\n')
    assert "cylindrical face" in e.result("holes").error.message
    e = ev(BASE + 'one = linear_pattern("one", body, 1, spacing=5)\n')
    assert e.result("one").ok and e.result("one").warnings == ["count 1 repeats nothing"]


def test_cuts_aim_at_the_material():
    # a through cut drawn on the top face goes down into the body without flip=True
    e = ev(BASE + 'h = sketch("h", on=body.faces.top)\nh.circle("hh", 2, at=(-15, 0))\ncut("hole", h, through=True)\n')
    assert e.body.volume == pytest.approx(8000 - PI * 1 * 10, rel=1e-6)
    e = ev(BASE + 'h = sketch("h", on=body.faces.top)\nh.circle("hh", 2, at=(-15, 0))\ncut("pocket", h, 4)\n')
    assert e.body.volume == pytest.approx(8000 - PI * 1 * 4, rel=1e-6)
    # extrude up to a face on either side of the sketch plane
    e = ev(BASE + 'p = plane("p", XY, offset=30)\nk = sketch("k", on=p)\nk.circle("c", 6)\npost = extrude("post", k, upto=body.faces.top)\n')
    assert e.result("post").ok and e.body.volume == pytest.approx(8000 + PI * 9 * 20, rel=1e-6)
    e = ev(BASE + 'k = sketch("k", on=XY, offset=5)\nk.circle("c", 6)\npost = extrude("post", k, upto=body.faces.from_sketch("r.left"))\n')
    assert "parallel" in e.result("post").error.message


def test_suppressed_keyword_and_dependants():
    e = ev(BASE + 'k = sketch("k", on=body.faces.top)\nk.circle("kk", 2, at=(12, 0))\npeg = extrude("peg", k, 3, suppressed=True)\n'
           'cp = circular_pattern("cp", peg, 5, axis=Z)\n')
    assert e.result("peg").skipped and "suppressed" in e.result("cp").error.message
    e = ev('s = sketch("s", on=XY, suppressed=True)\ns.rect("r", 4, 4)\nextrude("e", s, 1)\n')
    assert "is suppressed" in e.result("e").error.message
    src = HEAD + BASE
    new = edit.apply(src, {"op": "set_argument", "feature": "body", "kwarg": "suppressed", "value": True})
    assert new.endswith('body = extrude("body", s, 10, suppressed=True)\n')
    assert edit.apply(new, {"op": "set_argument", "feature": "body", "kwarg": "suppressed", "value": False}) == src


def test_compose_round_trips_every_feature_kind():
    cases = [
        ("revolve", "rv", {"sketch": "p", "axis": "axis", "angle": 180}, 'rv = revolve("rv", p, "axis", angle=180)'),
        ("revolve", "rv", {"sketch": "p", "axis": "Z"}, 'rv = revolve("rv", p, Z)'),
        ("fillet", "f1", {"edges": {"expr": "body.edges.top"}, "radius": 2}, 'f1 = fillet("f1", body.edges.top, 2)'),
        ("chamfer", "c1", {"edges": {"expr": "body.edges.bottom"}, "distance": 1, "distance2": 2},
         'c1 = chamfer("c1", body.edges.bottom, 1, distance2=2)'),
        ("shell", "sh", {"faces": {"expr": "body.faces.top"}, "thickness": 1.5, "outward": True},
         'sh = shell("sh", body.faces.top, 1.5, outward=True)'),
        ("shell", "sh", {"faces": None, "thickness": 2}, 'sh = shell("sh", None, 2)'),
        ("linear_pattern", "lp", {"feature": {"expr": "boss"}, "count": 3, "spacing": 10, "direction": "X", "count2": 2,
                                  "spacing2": 5, "direction2": "-Y"},
         'lp = linear_pattern("lp", boss, 3, spacing=10, direction=X, count2=2, spacing2=5, direction2=-Y)'),
        ("circular_pattern", "cp", {"feature": {"expr": "boss"}, "count": 6, "axis": {"expr": "post.faces.from_sketch('c')"}, "angle": 180},
         "cp = circular_pattern(\"cp\", boss, 6, axis=post.faces.from_sketch('c'), angle=180)"),
        ("mirror", "m", {"feature": {"expr": "boss"}, "about": "XZ"}, 'm = mirror("m", boss, about=XZ)'),
        ("mirror", "m2", {"about": {"expr": "plane1"}, "suppressed": True}, 'm2 = mirror("m2", about=plane1, suppressed=True)'),
        ("extrude", "e2", {"sketch": "t", "upto": {"expr": "body.faces.bottom"}, "op": "cut"},
         'e2 = extrude("e2", t, upto=body.faces.bottom, op="cut")'),
        ("cut", "c2", {"sketch": "t", "through": True}, 'cut("c2", t, through=True)'),
    ]
    for kind, name, args, expected in cases:
        assert edit.compose_feature(kind, name, args) == expected
    with pytest.raises(edit.EditError):
        edit.compose_feature("extrude", "e", {"sketch": "t"})
    # every composed statement parses back to the same kind and name
    src = HEAD + BASE + 't = sketch("t", on=body.faces.top)\nt.circle("c", 4)\nboss = extrude("boss", t, 5)\n'
    for kind, name, args in [("fillet", "f1", {"edges": {"expr": "body.edges.top"}, "radius": 2}),
                             ("linear_pattern", "lp", {"feature": {"expr": "boss"}, "count": 2, "spacing": 12, "direction": "X"}),
                             ("mirror", "m", {"feature": {"expr": "boss"}, "about": "XZ"}),
                             ("shell", "sh", {"faces": {"expr": "body.faces.bottom"}, "thickness": 1})]:
        src = edit.apply(src, {"op": "add_feature", "kind": kind, "name": name, "args": args})
        doc = parse_document(src)
        assert not doc.errors and doc.feature(name).kind == kind, (kind, doc.errors)
    e = evaluate(doc)
    assert all(r.ok for r in e.results), [r.error.message for r in e.results if r.error]


def test_kind_aware_defaults_drop_keywords():
    src = HEAD + 'p = sketch("p", on=XZ)\np.rect("r", 10, 30, at=(15, 15))\nrv = revolve("rv", p, Z, angle=90)\n'
    new = edit.apply(src, {"op": "set_argument", "feature": "rv", "kwarg": "angle", "value": 360})
    assert 'rv = revolve("rv", p, Z)\n' in new
    src = HEAD + 'p = plane("p", XY, angle=30, about=body.edges.top)\n'
    new = edit.apply(src, {"op": "set_argument", "feature": "p", "kwarg": "angle", "value": 0})
    assert 'plane("p", XY, about=body.edges.top)' in new


def test_prefix_cache_restores_unchanged_features():
    src = HEAD + BASE + 't = sketch("t", on=body.faces.top)\nt.circle("c", 8, at=(10, 0))\nboss = extrude("boss", t, 6)\n' \
        'f1 = fillet("f1", boss.edges.top, 1)\n'
    first = evaluate(parse_document(src))
    second = evaluate(parse_document(src.replace("boss.edges.top, 1", "boss.edges.top, 2")), cache=first)
    assert second.cached == 4 and second.body.volume != pytest.approx(first.body.volume)
    assert [r.name for r in second.results] == ["s", "body", "t", "boss", "f1"]
    third = evaluate(parse_document(src.replace('rect("r", 40, 20)', 'rect("r", 40, 22)')), cache=second)
    assert third.cached == 0 and third.body.volume > second.body.volume
    partial = evaluate(parse_document(src), upto="boss", cache=first)
    assert partial.cached == 4 and len(partial.results) == 4 and partial.body.volume == pytest.approx(8000 + PI * 16 * 6, rel=1e-6)
    # a change in the middle keeps the prefix, drops the rest
    fourth = evaluate(parse_document(src.replace('at=(10, 0)', 'at=(8, 0)')), cache=first)
    assert fourth.cached == 2 and fourth.result("f1").ok


def test_mesh_header_carries_tags(bracket_eval):
    from plainsolid import mesh as pmesh

    m = pmesh.build(bracket_eval.items())
    header = pmesh.decode_header(pmesh.encode(m))
    tags = header["face_tags"]
    assert any(":top" in t for t in tags.values()) and any(":bottom" in t for t in tags.values())
    assert set(header["edge_labels"].values()) <= {"body", "inner", "outer", "hole_cut", "slot_cut"}
    assert any(t == ["inner_bottom", "inner_wall"] for t in tags.values())  # the fillet face
