import pytest

from plainsolid.parse import parse_document, parse_file

pytestmark = pytest.mark.unit

MINI = '''from plainsolid import *
meta(name="mini")
wall = 3.0   # thickness
w = param(10, "width")
s = sketch("s1", on=XY)
s.rect("r", w, 5)
e = extrude("e1", s, wall * 2)
'''


def test_parse_bracket_features_and_params(zoo_dir):
    doc = parse_file(str(zoo_dir / "bracket.py"))
    assert not doc.errors
    assert [f.name for f in doc.features] == ["profile", "body", "inner", "outer", "holes", "hole_cut", "slots", "slot_cut"]
    assert [f.kind for f in doc.features] == ["sketch", "extrude", "fillet", "chamfer", "sketch", "cut", "sketch", "cut"]
    assert doc.meta["name"] == "bracket"
    assert doc.param("hole_d").description == "clearance hole for M5"
    assert doc.param("width").value == 60.0
    body_line = next(i for i, line in enumerate(doc.source.splitlines(), 1) if line.startswith("body = extrude("))
    assert doc.feature("body").span == (body_line, body_line)
    assert doc.feature("body").arg_texts["depth"] == "base_depth"
    profile = doc.feature("profile")
    assert [c.kind for c in profile.constraints][:3] == ["coincident", "coincident", "coincident"]
    assert profile.constraint("w").value == 60.0 and profile.constraint("w").value_text == "width"
    assert doc.feature("holes").entity("back_edge").kind == "project"
    assert doc.feature("hole_cut").args["through"] is True
    assert [e.name for e in doc.feature("slots").entities] == ["wall_top", "wall_end", "slot1", "slot2"]
    assert doc.feature("slots").constraint("pitch").options == {"along": "x"}


def test_positional_and_expression_args():
    doc = parse_document(MINI)
    assert not doc.errors
    e = doc.feature("e1")
    assert e.args["depth"] == 6.0
    assert e.arg_texts == {"name": '"e1"', "sketch": "s", "depth": "wall * 2"}
    assert e.variable == "e"
    assert doc.param("wall").expression == "3.0"
    assert doc.param("w").expression == "10"
    assert doc.param("w").description == "width"


def test_loop_features_are_read_only():
    src = MINI + '''
for i in range(2):
    sk = sketch(f"loop{i}", on=XY)
    sk.circle("c", 2, at=(i * 5, 0))
    extrude(f"le{i}", sk, 1)
'''
    doc = parse_document(src)
    assert not doc.errors
    assert doc.feature("loop0").read_only and doc.feature("le1").read_only
    assert not doc.feature("e1").read_only


def test_duplicate_name_is_an_error():
    doc = parse_document(MINI + 'extrude("e1", s, 1)\n')
    assert doc.errors and "duplicate feature name 'e1'" in doc.errors[0].message
    assert doc.errors[0].line == 8
    assert [f.name for f in doc.features] == ["s1", "e1"]  # features before the failure survive


def test_syntax_error_reports_line():
    doc = parse_document("from plainsolid import *\nsketch('a', on=XY\n")
    assert doc.errors and doc.errors[0].line == 2


def test_runtime_error_reports_line():
    doc = parse_document(MINI + "x = 1 / 0\n")
    assert doc.errors[0].line == 8
    assert "ZeroDivisionError" in doc.errors[0].message


def test_imports_are_restricted():
    doc = parse_document("import os\n")
    assert "not allowed" in doc.errors[0].message
    doc = parse_document("import math\nfrom plainsolid import *\nx = math.pi\n")
    assert not doc.errors
    assert doc.param("x").value == pytest.approx(3.14159, abs=1e-4)


def test_unknown_plane_and_bad_point():
    doc = parse_document('from plainsolid import *\nsketch("a", on="QQ")\n')
    assert "unknown plane" in doc.errors[0].message
    doc = parse_document('from plainsolid import *\ns = sketch("a", on=XY)\ns.line("l", (0,0), 5)\n')
    assert "expected a 2D point" in doc.errors[0].message


def test_document_json_roundtrip_shape():
    out = parse_document(MINI).to_json()
    assert out["hash"] and out["features"][1]["args"]["sketch"] == "s1"
    assert out["params"][0]["name"] == "wall"
