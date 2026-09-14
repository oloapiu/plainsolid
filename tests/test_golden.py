"""L1: every zoo document against its .expected.json."""
import json

import pytest
from conftest import load_expected

from plainsolid import mesh as pmesh
from plainsolid import query
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_file

pytestmark = pytest.mark.golden

TOL = {"volume": 1e-6, "area": 1e-6, "mass": 1e-6}


def zoo_documents(zoo_dir):
    return sorted(p for p in zoo_dir.glob("*.py"))


def test_zoo_documents_match_golden(zoo_dir, update_golden):
    docs = zoo_documents(zoo_dir)
    assert docs, "the zoo is empty"
    for path in docs:
        doc = parse_file(str(path))
        assert not doc.errors, (path.name, [e.message for e in doc.errors])
        ev = evaluate(doc)
        assert all(r.ok for r in ev.results), (path.name, [r.error.message for r in ev.results if r.error])
        actual = query.summary(ev)
        actual["features"] = {r.name: {"faces_created": r.faces_created} for r in ev.results}
        expected_path = path.with_suffix(".expected.json")
        if update_golden:
            expected_path.write_text(json.dumps(actual, indent=2) + "\n")
            continue
        expected = load_expected(expected_path)
        assert expected, f"{expected_path.name} missing; run pytest --update-golden and review it"
        if doc.kind == "drawing":
            # a drawing's golden numbers: segment counts and extents per view, dimension values
            assert actual["sheet"] == expected["sheet"], (path.name, "sheet")
            assert set(actual["views"]) == set(expected["views"]), (path.name, "views")
            for name, v in expected["views"].items():
                a = actual["views"][name]
                assert (a["visible"], a["hidden"], a["hatch"], a["refs"]) == (v["visible"], v["hidden"], v["hatch"], v["refs"]), (path.name, name)
                assert a["bbox"] == pytest.approx(v["bbox"], abs=1e-3), (path.name, name, "bbox")
            assert set(actual["dimensions"]) == set(expected["dimensions"]), (path.name, "dimensions")
            for name, d in expected["dimensions"].items():
                assert actual["dimensions"][name]["kind"] == d["kind"] and actual["dimensions"][name]["text"] == d["text"], (path.name, name)
                assert actual["dimensions"][name]["value"] == pytest.approx(d["value"], abs=1e-4), (path.name, name)
            assert actual["notes"] == expected["notes"] and actual["features"] == expected["features"], (path.name, "notes")
            continue
        for key in ("volume", "area", "mass"):
            assert actual[key] == pytest.approx(expected[key], rel=TOL[key]), (path.name, key)
        for key in ("min", "max"):
            assert actual["bbox"][key] == pytest.approx(expected["bbox"][key], abs=1e-4), (path.name, key)
        assert actual["center_of_mass"] == pytest.approx(expected["center_of_mass"], abs=1e-4)
        assert actual["counts"] == expected["counts"], (path.name, "counts")
        assert actual["features"] == expected["features"], (path.name, "features")


def test_evaluation_is_deterministic(zoo_dir):
    path = zoo_dir / "bracket.py"
    a = evaluate(parse_file(str(path)))
    b = evaluate(parse_file(str(path)))
    assert a.body.volume == b.body.volume
    ma = pmesh.encode(pmesh.tessellate(a.body), a.face_labels(), a.edge_labels())
    mb = pmesh.encode(pmesh.tessellate(b.body), b.face_labels(), b.edge_labels())
    assert ma == mb
