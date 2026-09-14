"""L0: selectors from text and where they land in a mesh; the volumetric compare."""
from __future__ import annotations

import subprocess

import pytest
from conftest import ZOO

from plainsolid import compare as pcompare
from plainsolid import edit as pedit
from plainsolid import refs
from plainsolid import section as psection
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document, parse_file
from plainsolid.selectors import SelectorError

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def bracket():
    ev = evaluate(parse_file(str(ZOO / "bracket.py")))
    return ev, ev.items()


@pytest.fixture(scope="module")
def node():
    ev = evaluate(parse_file(str(ZOO / "node.py")))
    return ev, ev.items()


def test_selector_text_parses_like_the_file(bracket):
    ev, _ = bracket
    sel = refs.parse_selector('body.edges.from_sketch("inner_bottom").from_sketch("inner_wall")', ev.document)
    assert sel.feature == "body" and sel.kind == "edges" and [op for op, _ in sel.ops] == ["tag", "tag"]
    assert refs.parse_selector("hole_cut.faces.nearest((0, -20, 4))", ev.document).ops == (("nearest", (0.0, -20.0, 4.0)),)
    assert refs.parse_selector("inner", ev.document) == "inner"
    assert refs.parse_selector(" body.faces.top ", ev.document).ref_name == "body.faces.top"
    for bad, message in [("nothing.faces", "not a feature"), ("body.faces[0]", "indexing"), ("1 +", "bad selector"),
                         ("body.faces.sideways", "sideways"), ("", "selector is text")]:
        with pytest.raises(SelectorError, match=message):
            refs.parse_selector(bad, ev.document)


def test_ids_of_selectors_on_a_part(bracket):
    ev, items = bracket
    top = refs.locate(ev, ["body.faces.top"], items)
    assert len(top["faces"]) == 1 and not top["edges"] and not top["vertices"]
    face = items[0].shape.faces()[top["faces"][0]]
    assert face.center().Y == pytest.approx(-40.0)  # the extrusion's far end
    holes = refs.locate(ev, ["hole_cut"], items)
    assert len(holes["faces"]) == 2 and len(holes["edges"]) == 6  # two cylinders with their seams and rims
    fillet = refs.locate(ev, ["inner"], items)
    assert len(fillet["faces"]) == 1
    vertex = refs.locate(ev, ["body.vertices.nearest((30, 0, 0))"], items)
    assert len(vertex["vertices"]) == 1
    with pytest.raises(SelectorError, match="owns no faces"):
        refs.locate(ev, ["profile"], items)
    # the chamfer consumed its edge on the final body; it exists up to the feature before
    with pytest.raises(SelectorError, match="matches nothing"):
        refs.locate(ev, ['body.edges.from_sketch("bottom").from_sketch("outer_wall")'], items)
    upto = evaluate(parse_file(str(ZOO / "bracket.py")), upto="inner")
    assert len(refs.locate(upto, ['body.edges.from_sketch("bottom").from_sketch("outer_wall")'], upto.items())["edges"]) == 1


def test_ids_survive_a_section_by_matching_centres(bracket):
    ev, items = bracket
    cut = psection.apply(items, psection.parse_spec("XY", 2.0, False))
    found = refs.locate(ev, ["hole_cut", "body.faces.top"], cut)
    assert len(found["faces"]) == 3  # two half cylinders and the far end face, all below z=2
    shape = cut[0].shape
    for i in found["faces"]:
        assert shape.faces()[i].center().Z < 2.0


def test_ids_of_selectors_on_an_assembly(node):
    ev, items = node
    gland = refs.locate(ev, ['gland1.faces.where(kind="cylinder").largest()'], items)
    assert len(gland["faces"]) == 1
    lid = refs.locate(ev, ['lid.faces.of("plate").bottom.largest()'], items)
    assert len(lid["faces"]) == 1
    whole = refs.locate(ev, ["gland1"], items)
    assert len(whole["faces"]) == len(next(it for it in items if it.path == "gland1").shape.faces())

    # the ids address the posed geometry: the lid's underside sits on the box's top, as the mate says
    def face_of(found):
        base = 0
        for it in items:
            n = len(it.shape.faces())
            if found < base + n:
                return it.shape.faces()[found - base]
            base += n
        raise AssertionError(found)

    box_top = refs.locate(ev, ['box.faces.of("body").top'], items)["faces"][0]
    assert face_of(lid["faces"][0]).center().Z == pytest.approx(face_of(box_top).center().Z, abs=1e-6)
    with pytest.raises(SelectorError, match="mate reference"):
        refs.locate(ev, ["box.planes.XZ"], items)
    with pytest.raises(SelectorError, match="no instance"):
        refs.locate(ev, ["lid_down.faces"], items)
    with pytest.raises(SelectorError, match="not a feature"):
        refs.locate(ev, ["nothing.faces"], items)


def test_measure_reference_from_a_selector(bracket):
    ev, items = bracket
    assert refs.measure_ref(ev, items, "body.faces.top") == {"face": refs.locate(ev, ["body.faces.top"], items)["faces"][0]}
    assert refs.measure_ref(ev, items, {"face": 3}) == {"face": 3}
    assert refs.measure_ref(ev, items, {"selector": "body.vertices.nearest((30, 0, 0))"}) == {"vertex": refs.locate(ev, ["body.vertices.nearest((30, 0, 0))"], items)["vertices"][0]}
    with pytest.raises(SelectorError, match="picks 8 entities"):
        refs.measure_ref(ev, items, "hole_cut")


def test_highlight_mixes_ids_and_selectors(bracket):
    ev, items = bracket
    ids = refs.highlight_ids(ev, items, [3, "4", "body.faces.top", "inner"])
    assert 3 in ids["faces"] and 4 in ids["faces"] and len(ids["faces"]) == 4
    assert refs.highlight_ids(ev, items, []) == {"faces": [], "edges": [], "vertices": []}


def test_compare_reports_added_and_removed_volume(bracket):
    ev, _ = bracket
    src = (ZOO / "bracket.py").read_text()
    wider = evaluate(parse_document(pedit.apply(src, {"op": "set_parameter", "name": "width", "value": 70}), str(ZOO / "bracket.py")))
    report, items = pcompare.compare(wider, ev)  # the widened bracket, against the original: what widening added
    assert report["change"] == pytest.approx(wider.body.volume - ev.body.volume, abs=1e-3)
    assert report["added"]["volume"] - report["removed"]["volume"] == pytest.approx(report["change"], abs=1e-3)
    assert not report["same"] and report["added"]["count"] >= 1
    biggest = report["added"]["regions"][0]
    assert biggest["center"][0] == pytest.approx(35.0) and biggest["size"][0] == pytest.approx(10.0)  # the 10 mm strip on +X
    assert [it.name for it in items] == ["common", "added", "removed"]
    assert sum(it.shape.volume for it in items if it.name != "removed") == pytest.approx(wider.body.volume, rel=1e-6)
    same, only = pcompare.compare(ev, ev)
    assert same["same"] and same["added"]["volume"] == 0 and [it.name for it in only] == ["common"]


def test_compare_against_a_file_and_a_git_revision(project):
    ev = evaluate(parse_file(str(project / "bracket.py")))
    other = pcompare.evaluate_other(project / "bracket.py", other="lid.py")
    assert other.body is not None
    with pytest.raises(pcompare.CompareError, match="no such file"):
        pcompare.evaluate_other(project / "bracket.py", other="missing.py")
    with pytest.raises(pcompare.CompareError, match="needs another file"):
        pcompare.evaluate_other(project / "bracket.py")
    with pytest.raises(pcompare.CompareError, match="not inside a git repository"):
        pcompare.git_source(project / "bracket.py", "HEAD")
    git = ["git", "-C", str(project)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@t", "add", "bracket.py"], check=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"], check=True)
    src = (project / "bracket.py").read_text()
    (project / "bracket.py").write_text(pedit.apply(src, {"op": "set_parameter", "name": "thickness", "value": 6}))
    thicker = evaluate(parse_file(str(project / "bracket.py")))
    report, _ = pcompare.compare(thicker, pcompare.evaluate_other(project / "bracket.py", rev="HEAD"))
    assert report["change"] > 0 and report["removed"]["volume"] == 0  # a thicker wall only adds material
    assert report["other"]["volume"] == pytest.approx(ev.body.volume, rel=1e-6)
    with pytest.raises(pcompare.CompareError, match="git has no"):
        pcompare.git_source(project / "bracket.py", "nope")
