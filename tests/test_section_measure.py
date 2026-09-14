"""L7 and unit: sections with capped faces, and measurements by mesh id."""
import pytest

from plainsolid import measure as pmeasure
from plainsolid import mesh as pmesh
from plainsolid import section as psection
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_file


@pytest.fixture(scope="module")
def review(zoo_dir):
    ev = evaluate(parse_file(str(zoo_dir / "node_review.py")))
    items = ev.items()
    return ev, items, pmesh.build(items)


@pytest.mark.unit
def test_section_keeps_one_side_with_caps(review):
    _, items, _ = review
    spec = psection.parse_spec("XY", 5.0, False)
    cut = psection.apply(items, spec)
    ms = pmesh.build(cut)
    assert ms["bbox"][1][2] == pytest.approx(5.0, abs=1e-3)      # material above z=5 removed
    assert ms["bbox"][0][2] == pytest.approx(-15.0, abs=1e-3)
    assert ms["section_faces"], "no cap faces labelled"
    assert all(ms["face_labels"][i] == "section" for i in ms["section_faces"])
    flipped = pmesh.build(psection.apply(items, psection.parse_spec("XY", 5.0, True)))
    assert flipped["bbox"][0][2] == pytest.approx(5.0, abs=1e-3)
    # an item entirely on the removed side disappears: bracket sits at z 10 +- 25, cut at z = -40 keeps nothing
    gone = psection.apply(items, psection.parse_spec("XY", -40.0, False))
    assert gone == []


@pytest.mark.unit
def test_section_spec_validation():
    assert psection.parse_spec(None) is None
    with pytest.raises(psection.SectionError):
        psection.parse_spec("QQ")
    with pytest.raises(psection.SectionError):
        psection.parse_spec("custom")
    spec = psection.parse_spec("custom", 0, False, [0, 0, 0], [1, 1, 0])
    assert spec.plane == "custom" and spec.key


@pytest.mark.unit
def test_measure_entities_and_pairs(review):
    _, items, m = review
    one = pmeasure.measure(items, {"face": 0})["a"]
    assert one["kind"] == "face" and one["item"] == "node.box" and one["type"] == "plane" and "normal" in one
    gland_faces = [i for i, lab in enumerate(m["face_labels"]) if "gland" in lab]
    described = [pmeasure.measure(items, {"face": i})["a"] for i in gland_faces]
    cylinders = [d for d in described if d["type"] == "cylinder"]
    assert cylinders and {round(d["radius"], 3) for d in cylinders} == {6.0, 8.0}
    cyl = next(i for i, d in zip(gland_faces, described, strict=True) if d["type"] == "cylinder")
    pair = pmeasure.measure(items, {"point": [0, 0, 0]}, {"point": [3, 4, 0]})
    assert pair["distance"] == pytest.approx(5.0) and pair["delta"] == pytest.approx([3, 4, 0])
    two = pmeasure.measure(items, {"face": 0}, {"face": cyl})
    assert two["distance"] >= 0 and len(two["closest"]) == 2 and "angle" in two
    v = pmeasure.measure(items, {"vertex": 0})["a"]
    assert v["kind"] == "vertex" and len(v["position"]) == 3
    edge = pmeasure.measure(items, {"edge": 0})["a"]
    assert edge["kind"] == "edge" and edge["length"] > 0


@pytest.mark.unit
def test_measure_errors(review):
    _, items, _ = review
    for bad in ({"face": 9999}, {"nope": 1}, {"point": [1, 2]}, {"face": "x"}, {}):
        with pytest.raises(pmeasure.MeasureError):
            pmeasure.measure(items, bad)


@pytest.mark.unit
def test_measure_ids_match_mesh_ids(review):
    _, items, m = review
    # the mesh's item face ranges and the measurement resolver agree on which item owns a face
    for it in m["items"]:
        start, count = it["faces"]
        assert pmeasure.measure(items, {"face": start})["a"]["item"] == it["name"]
        assert pmeasure.measure(items, {"face": start + count - 1})["a"]["item"] == it["name"]


@pytest.mark.unit
def test_section_classifies_by_bounding_box(review):
    _, items, _ = review
    plane = psection.plane_of(psection.parse_spec("XY", 5.0, False))
    kinds = {it.name: psection.classify(it, plane, False) for it in items}
    assert kinds["node.box"] == "cut" and kinds["node.bracket"] == "cut"          # both straddle z = 5
    far = psection.plane_of(psection.parse_spec("XY", 100.0, False))
    assert {psection.classify(it, far, False) for it in items} == {"keep"}
    assert {psection.classify(it, far, True) for it in items} == {"drop"}
    # untouched items keep their cache key so the disk cache still serves them
    kept = psection.apply(items, psection.parse_spec("YZ", 25.0, False))
    assert any(it.cache_key for it in kept) and all(not it.section_faces for it in kept if it.cache_key)
