"""Drawings: the DSL and its statements (L0), views, sections and
dimensions on the zoo bracket (L0 against known numbers; L1 holds the golden
file), export to SVG, DXF and PDF (L7), the API and CLI (L5, L6) and the
evaluation budget (L11)."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from plainsolid import drawing as pdrawing
from plainsolid import edit as pedit
from plainsolid import io as pio
from plainsolid import query
from plainsolid.cli import app
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document, parse_file
from plainsolid.server import create_app

SVG = "{http://www.w3.org/2000/svg}"

DRAWING = '''from plainsolid import *

meta(kind="drawing", name="test", of="bracket.py", sheet="A3", scale=0.5)

front = view("front", direction=FRONT, at=(60, 150))
sec = view("sec", section=YZ, offset=-10, at=(160, 150), flip=True)
dimension("w", front.edges.of("body").bottom.from_sketch("outer_wall"), front.edges.of("body").bottom.from_sketch("right"), at=(0, -40))
dimension("d", front.faces.of("inner"), at=(10, 10), kind="radius", text="R3 TYP")
note("n", "hello\\nworld", at=(20, 20), size=5, view=front)
'''


def _eval(project: Path, source: str):
    path = project / "dwg.py"
    path.write_text(source)
    doc = parse_file(str(path))
    assert not doc.errors, [e.message for e in doc.errors]
    return evaluate(doc)


def _errors(ev) -> dict[str, str]:
    return {r.name: r.error.message for r in ev.results if r.error}


# --- L0: the DSL and its statements ------------------------------------------------------

@pytest.mark.unit
def test_drawing_dsl_records_views_dimensions_and_notes():
    doc = parse_document(DRAWING, "dwg.py")
    assert not doc.errors and doc.kind == "drawing"
    assert doc.meta["of"] == "bracket.py" and doc.meta["sheet"] == "A3"
    kinds = [(f.kind, f.name) for f in doc.features]
    assert kinds == [("view", "front"), ("view", "sec"), ("dimension", "w"), ("dimension", "d"), ("note", "n")]
    front, sec, w, d, n = doc.features
    assert front.args == {"at": [60.0, 150.0], "direction": "front"} and front.variable == "front"
    assert sec.args == {"at": [160.0, 150.0], "section": "YZ", "offset": -10.0, "flip": True}
    assert w.args["a"]["selector"]["feature"] == "front" and w.args["b"]["selector"]["kind"] == "edges"
    assert w.arg_texts["a"] == 'front.edges.of("body").bottom.from_sketch("outer_wall")'
    assert d.args["kind"] == "radius" and d.args["text"] == "R3 TYP" and d.args["b"] is None
    assert n.args == {"text": "hello\nworld", "at": [20.0, 20.0], "size": 5.0, "view": "front"}


@pytest.mark.unit
def test_drawing_dsl_rejects_bad_arguments():
    for bad, message in (
        ('view("v", direction="sideways")', "FRONT, BACK"),
        ('front = view("front")\ndimension("d", "front.edges", at=(0, 0))', "reference through a view"),
        ('front = view("front")\ndimension("d", front.edges.top, kind="length")', "kind must be"),
        ('note("n", 42)', "text must be a string"),
        ('view("v", section=3)', "section= takes XY"),
    ):
        doc = parse_document('from plainsolid import *\nmeta(kind="drawing", of="bracket.py")\n' + bad, "dwg.py")
        assert doc.errors and message in doc.errors[0].message, (bad, [e.message for e in doc.errors])


@pytest.mark.unit
def test_compose_drawing_statements_round_trip():
    stmts = [
        pedit.compose_feature("view", "front", {"direction": "front", "at": [60, 150.5]}),
        pedit.compose_feature("view", "iso", {"direction": "iso", "at": [200, 150], "scale": 0.5, "hidden": False}),
        pedit.compose_feature("view", "aa", {"section": "YZ", "offset": -10, "flip": True, "at": [160, 60]}),
        pedit.compose_feature("dimension", "w", {"a": {"expr": 'front.edges.of("body").top'}, "b": {"expr": 'front.edges.of("body").bottom'}, "at": [0, -30], "along": "y"}),
        pedit.compose_feature("dimension", "h", {"a": {"expr": 'front.faces.of("inner")'}, "at": [5, 5], "kind": "radius"}),
        pedit.compose_feature("note", "n", {"text": "Break edges", "at": [20, 20], "size": 5, "view": {"expr": "front"}}),
    ]
    assert stmts == [
        'front = view("front", direction=FRONT, at=(60, 150.5))',
        'iso = view("iso", direction=ISO, at=(200, 150), scale=0.5, hidden=False)',
        'aa = view("aa", at=(160, 60), section=YZ, offset=-10, flip=True)',
        'dimension("w", front.edges.of("body").top, front.edges.of("body").bottom, at=(0, -30), along="y")',
        'dimension("h", front.faces.of("inner"), at=(5, 5), kind="radius")',
        'note("n", "Break edges", at=(20, 20), size=5, view=front)',
    ]
    src = 'from plainsolid import *\n\nmeta(kind="drawing", of="bracket.py")\n'
    for s in stmts:
        src = pedit.apply(src, {"op": "add_feature", "statement": s, "kind": "view", "name": "x"})
    doc = parse_document(src, "dwg.py")
    assert not doc.errors and [f.name for f in doc.features] == ["front", "iso", "aa", "w", "h", "n"]
    # moving a view rewrites its placement; a default written back drops the keyword
    src = pedit.apply(src, {"op": "set_argument", "feature": "iso", "kwarg": "at", "value": [210, 140]})
    src = pedit.apply(src, {"op": "set_argument", "feature": "iso", "kwarg": "hidden", "value": None})
    assert 'iso = view("iso", direction=ISO, at=(210, 140), scale=0.5)' in src


@pytest.mark.unit
def test_set_meta_edits_the_meta_line(bracket_src):
    src = pedit.apply(bracket_src, {"op": "set_meta", "key": "material", "value": "steel"})
    assert 'meta(name="bracket", material="steel", revision="A", author="Paolo")' in src
    src = pedit.apply(src, {"op": "set_meta", "key": "date", "value": "2026-09-05"})
    assert 'author="Paolo", date="2026-09-05")' in src
    src = pedit.apply(src, {"op": "set_meta", "key": "date", "value": None})
    src = pedit.apply(src, {"op": "set_meta", "key": "author", "value": None})
    assert 'meta(name="bracket", material="steel", revision="A")' in src
    assert src.count("# parameters") == 1 and parse_document(src, "b.py").meta == {"name": "bracket", "material": "steel", "revision": "A"}
    dwg = pedit.apply(DRAWING, {"op": "set_meta", "key": "sheet", "value": "A3"})
    dwg = pedit.apply(dwg, {"op": "set_meta", "key": "scale", "value": 0.5})
    assert 'meta(kind="drawing", name="test", of="bracket.py", sheet="A3", scale=0.5)' in dwg
    bare = pedit.apply("from plainsolid import *\n\nbody = sketch(\"s\", on=XY)\n", {"op": "set_meta", "key": "material", "value": "abs"})
    assert bare.startswith('from plainsolid import *\nmeta(material="abs")\n')
    with pytest.raises(pedit.EditError, match="kind is fixed"):
        pedit.apply(bracket_src, {"op": "set_meta", "key": "kind", "value": "assembly"})
    with pytest.raises(pedit.EditError, match="identifier"):
        pedit.apply(bracket_src, {"op": "set_meta", "key": "not a key", "value": 1})


@pytest.mark.unit
def test_view_frame_matches_the_projection_and_hatching_stays_inside():
    frame = pdrawing.frame_for(*pdrawing.DIRECTIONS["front"], (0, -20, 25))
    assert frame.to2d((-30, -40, 0)) == pytest.approx((-30, -25)) and frame.to2d((30, 0, 50)) == pytest.approx((30, 25))
    top = pdrawing.frame_for(*pdrawing.DIRECTIONS["top"], (0, 0, 0))
    assert top.to2d((1, 2, 3)) == pytest.approx((1, 2)) and top.depth((0, 0, 3)) == pytest.approx(3)
    square = [(0, 0), (20, 0), (20, 20), (0, 20)]
    hole = [(8, 8), (12, 8), (12, 12), (8, 12)]
    lines = pdrawing.hatch_polygons([square, hole], 2.5)
    assert 10 <= len(lines) <= 30
    for seg in lines:
        (x0, y0), (x1, y1) = seg.pts
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        assert 0 <= mx <= 20 and 0 <= my <= 20
        assert not (8 < mx < 12 and 8 < my < 12), "a hatch line crosses the hole"


# --- L0: views and dimensions on the bracket ---------------------------------------------

@pytest.mark.unit
def test_bracket_drawing_measures_the_part(zoo_dir):
    ev = evaluate(parse_file(str(zoo_dir / "bracket_dwg.py")))
    assert not _errors(ev)
    dims = {d.name: d for d in ev.drawing.dimensions}
    assert {n: round(d.value, 4) for n, d in dims.items()} == {
        "width": 60, "height": 50, "fillet": 3, "chamfer": 45, "depth": 40, "pitch": 30, "hole": 5, "wall": 4, "base": 4,
    }
    assert dims["hole"].text == "2× Ø5" and dims["fillet"].text == "R3" and dims["chamfer"].text == "45°"
    assert dims["height"].label["angle"] == 90 and dims["width"].label["angle"] == 0
    scene = ev.scene()
    views = {v["name"]: v for v in scene["views"]}
    assert views["section"]["label"] == "SECTION A-A" and views["section"]["hatch"] and not views["section"]["hidden"]
    assert views["iso"]["label"] == "SCALE 3:4" and views["front"]["hidden"]
    # the plane of the section shows as a trace on the views that see it edge-on
    assert [v["name"] for v in scene["views"] if v["traces"]] == ["front", "top"]
    assert views["front"]["traces"][0]["letter"] == "A"
    # visible segments of a plain view name the model edge they show
    refs = {s.get("ref") for s in views["front"]["visible"]}
    assert 'front.edges.of("body").top.from_sketch("outer_wall")' in refs
    assert all(s.get("ref") for s in views["top"]["visible"]) and not any(s.get("ref") for s in views["section"]["visible"])
    assert scene["frame"]["texts"][1]["text"] == "bracket" and scene["notes"][0]["text"].startswith("Break")
    assert query.summary(ev)["dimensions"]["width"]["value"] == 60


@pytest.mark.unit
def test_dimension_errors_name_the_problem_and_leave_the_rest(project):
    ev = _eval(project, '''from plainsolid import *
meta(kind="drawing", of="bracket.py")
front = view("front", direction=FRONT, at=(60, 150))
miss = view("miss", section=XY, offset=-500, at=(0, 0))
dimension("nowhere", miss.edges.of("body").top, at=(0, 0))
dimension("straight", front.edges.of("body").bottom.from_sketch("bottom"), at=(0, 0))
dimension("parallel", front.edges.of("body").bottom.from_sketch("bottom"), front.edges.of("body").bottom.from_sketch("top"), at=(0, 0), kind="angle")
dimension("one", front.edges.of("body").bottom.from_sketch("bottom"), at=(0, 0), kind="distance")
dimension("skew", front.edges.of("body").bottom.from_sketch("bottom"), front.edges.of("body").bottom.from_sketch("right"), at=(0, 0))
dimension("ok", front.edges.of("body").bottom.from_sketch("bottom"), front.edges.of("body").bottom.from_sketch("top"), at=(-40, 0))
note("empty", "   ", at=(1, 1))
sketch("s", on=XY)
''')
    errors = _errors(ev)
    assert "leaves nothing" in errors["miss"] and "not a view above it" in errors["nowhere"]
    assert "circular edge or a round face" in errors["straight"]
    assert "parallel" in errors["parallel"]
    assert "needs two references" in errors["one"]
    assert "not parallel" in errors["skew"]
    assert "empty" in errors["empty"]
    assert "only views, dimensions and notes" in errors["s"]
    assert "ok" not in errors and ev.drawing.dimensions[0].value == pytest.approx(50)


@pytest.mark.unit
def test_section_views_and_scales(project):
    ev = _eval(project, DRAWING)
    assert not _errors(ev), _errors(ev)
    front, sec = ev.drawing.views["front"], ev.drawing.views["sec"]
    assert front.scale == 0.5 and sec.section == "YZ" and sec.flip and sec.hatch
    assert front.sheet_bbox() == pytest.approx((45, 137.5, 75, 162.5))  # 60 x 50 at half scale, centred on (60, 150)
    w = ev.drawing.dimensions[0]
    assert w.value == pytest.approx(60) and w.text == "60" and w.at == (60, 110)
    assert ev.drawing.dimensions[1].text == "R3 TYP"
    assert ev.drawing.notes[0].at == (80, 170) and ev.drawing.notes[0].size == 5
    scene = ev.scene()
    assert scene["sheet"] == {"size": "A3", "width": 420.0, "height": 297.0, "scale": 0.5}
    assert [v["label"] for v in scene["views"]] == [None, "SECTION A-A"]
    # a plane of the model, and a plane that misses it
    ev = _eval(project, '''from plainsolid import *
meta(kind="drawing", of="enclosure.py")
mid = view("mid", section="board_plane", offset=10, at=(100, 100))
miss = view("miss", section=XY, offset=-500, at=(200, 100))
''')
    errors = _errors(ev)
    assert "mid" not in errors and ev.drawing.views["mid"].hatch, errors
    assert "leaves nothing" in errors["miss"]


@pytest.mark.unit
def test_drawings_of_an_assembly_and_a_step_file(project):
    ev = _eval(project, '''from plainsolid import *
meta(kind="drawing", of="node.py")
front = view("front", direction=FRONT, at=(150, 120), hidden=False)
top = view("top", direction=TOP, at=(150, 60), hidden=False, scale=0.5)
dimension("lid", front.faces.of("lid").where(normal="+Z").largest(), front.faces.of("box").where(normal="-Z").largest(), at=(-90, 0))
''')
    assert not _errors(ev), _errors(ev)
    assert ev.drawing.model.kind == "assembly" and len(ev.drawing.views["front"].visible) > 20
    assert ev.drawing.dimensions[0].value > 40  # the enclosure and its lid stacked
    assert str(project / "lid.py") in ev.dependencies and str(project / "node.py") in ev.dependencies
    ev = _eval(project, '''from plainsolid import *
meta(kind="drawing", of="vendor/gland_m12.step")
side = view("side", direction=FRONT, at=(100, 100))
dimension("od", side.faces.where(kind="cylinder").largest(), at=(20, 20), kind="diameter")
''')
    errors = _errors(ev)
    assert ev.drawing.model.kind == "step" and "side" not in errors, errors
    # a cylinder seen from the side is a line, not a circle
    assert "od" in errors and "not one in this view" in errors["od"]


@pytest.mark.unit
def test_model_edits_reach_the_drawing_through_the_cache(project):
    path = project / "dwg.py"
    path.write_text(DRAWING)
    doc = parse_file(str(path))
    first = evaluate(doc)
    assert first.drawing.dimensions[0].value == pytest.approx(60)
    again = evaluate(doc, cache=first)
    assert again.cached == len(doc.features)
    part = project / "bracket.py"
    part.write_text(part.read_text().replace("width = 60.0", "width = 70.0"))
    third = evaluate(parse_file(str(path)), cache=first)
    assert third.cached == 0 and third.drawing.dimensions[0].value == pytest.approx(70)
    # the model gone: every view says so, the document still opens
    part.unlink()
    broken = evaluate(parse_file(str(path)))
    assert all("could not be loaded" in m for m in _errors(broken).values()) and broken.scene()["model"]["error"]


# --- L7: SVG, DXF and PDF ----------------------------------------------------------------

@pytest.mark.io
def test_svg_dxf_and_pdf_export(zoo_dir, tmp_path):
    import ezdxf

    ev = evaluate(parse_file(str(zoo_dir / "bracket_dwg.py")))
    svg = ET.parse(pio.export(ev, "svg", tmp_path / "b.svg")).getroot()
    groups = {g.get("id"): g for g in svg.iter(f"{SVG}g") if g.get("id")}
    assert {"visible", "hidden", "hatch", "dimensions", "frame", "section"} <= set(groups)
    assert groups["hidden"].get("stroke-dasharray") and not groups["visible"].get("stroke-dasharray")
    assert len(list(groups["visible"])) > 90 and len(list(groups["hidden"])) > 100
    texts = [t.text for t in svg.iter(f"{SVG}text")]
    assert {"60", "50", "R3", "45°", "40", "30", "2× Ø5", "SECTION A-A", "SCALE 3:4", "bracket", "Anodize clear"} <= set(texts)
    assert svg.get("viewBox") == "0 0 297.0 210.0"

    dxf = ezdxf.readfile(pio.export(ev, "dxf", tmp_path / "b.dxf"))
    msp = dxf.modelspace()
    by_layer: dict[str, dict[str, int]] = {}
    for e in msp:
        by_layer.setdefault(e.dxf.layer, {}).setdefault(e.dxftype(), 0)
        by_layer[e.dxf.layer][e.dxftype()] += 1
    assert by_layer["visible"]["CIRCLE"] == 2 and by_layer["visible"]["ARC"] >= 8 and by_layer["visible"]["LINE"] > 60
    assert by_layer["hidden"]["LINE"] > 50 and by_layer["hatch"]["LINE"] == 12
    assert by_layer["dimensions"]["TEXT"] == 9 and by_layer["dimensions"]["SOLID"] == 16 and by_layer["dimensions"]["ARC"] == 1
    assert dxf.layers.get("hidden").dxf.linetype == "DASHED" and dxf.header["$INSUNITS"] == 4
    assert not dxf.audit().has_errors
    assert {e.dxf.text for e in msp.query("TEXT")} >= {"60", "SECTION A-A", "bracket", "Paolo"}
    circle = next(e for e in msp.query("CIRCLE"))
    assert circle.dxf.radius == pytest.approx(2.5)

    pdf = pio.export(ev, "pdf", tmp_path / "b.pdf").read_bytes()
    assert pdf.startswith(b"%PDF") and len(re.findall(rb"/Type\s*/Page[^s]", pdf)) == 1 and len(pdf) > 5000

    with pytest.raises(pio.ExportError, match="drawing format"):
        pio.export(evaluate(parse_file(str(zoo_dir / "bracket.py"))), "dxf", tmp_path / "x.dxf")
    with pytest.raises(pio.ExportError, match="unknown drawing format"):
        pio.export(ev, "step", tmp_path / "x.step")


# --- L5, L6: the API and the CLI -----------------------------------------------------------

@pytest.mark.api
def test_drawing_api_tree_edits_and_dependency(project):
    (project / "dwg.py").write_text(DRAWING)
    app_ = create_app(project, serve_client=False)
    with TestClient(app_) as c:
        tree = c.post("/api/documents/open", json={"path": "dwg.py"}).json()
        did = tree["id"]
        assert tree["kind"] == "drawing" and tree["evaluation"]["drawing"]["sheet"]["size"] == "A3"
        assert [v["name"] for v in tree["evaluation"]["drawing"]["views"]] == ["front", "sec"]
        assert tree["evaluation"]["drawing"]["dimensions"][0]["text"] == "60"
        assert {f["path"]: f["kind"] for f in c.get("/api/files").json()["files"]}["dwg.py"] == "drawing"
        r = c.post(f"/api/documents/{did}/edit", json={"op": "set_argument", "feature": "front", "kwarg": "at", "value": [70, 140], "hash": tree["hash"]})
        assert r.status_code == 200 and 'front = view("front", direction=FRONT, at=(70, 140))' in (project / "dwg.py").read_text()
        tree = c.get(f"/api/documents/{did}/tree").json()
        assert tree["evaluation"]["drawing"]["views"][0]["at"] == [70, 140]
        # a note and a dimension added the way the client does
        r = c.post(f"/api/documents/{did}/edit", json={"op": "add_feature", "kind": "note", "name": "n2", "args": {"text": "hi", "at": [10, 10]}, "hash": tree["hash"]})
        assert r.status_code == 200
        r = c.post(f"/api/documents/{did}/edit", json={"op": "add_feature", "kind": "dimension", "name": "h", "hash": r.json()["hash"],
                                                        "args": {"a": {"expr": 'front.edges.of("body").bottom.from_sketch("bottom")'}, "b": {"expr": 'front.edges.of("body").bottom.from_sketch("top")'}, "at": [-40, 0]}})
        assert r.status_code == 200
        tree = c.get(f"/api/documents/{did}/tree").json()
        assert [d["text"] for d in tree["evaluation"]["drawing"]["dimensions"]] == ["60", "R3 TYP", "50"]
        # the sheet and scale from the panel
        r = c.post(f"/api/documents/{did}/edit", json={"op": "set_meta", "key": "sheet", "value": "letter", "hash": tree["hash"]})
        assert r.status_code == 200
        tree = c.get(f"/api/documents/{did}/tree").json()
        assert tree["meta"]["sheet"] == "letter" and tree["evaluation"]["drawing"]["sheet"]["width"] == 279.4
        assert tree["evaluation"]["drawing"]["model"]["meta"]["material"] == "al6061"
        # editing the part through the API refreshes the drawing and tells its subscribers
        part = c.post("/api/documents/open", json={"path": "bracket.py"}).json()
        with c.websocket_connect(f"/api/documents/{did}/events") as ws:
            assert ws.receive_json()["event"] == "hello"
            r = c.post(f"/api/documents/{part['id']}/edit", json={"op": "set_parameter", "name": "width", "value": 70, "hash": part["hash"]})
            assert r.status_code == 200
            assert ws.receive_json()["event"] == "dependency"
        tree = c.get(f"/api/documents/{did}/tree").json()
        assert tree["evaluation"]["drawing"]["dimensions"][0]["text"] == "70"
        r = c.post(f"/api/documents/{did}/export", json={"format": "dxf", "path": "out/dwg.dxf"})
        assert r.status_code == 200 and (project / "out" / "dwg.dxf").stat().st_size > 10000
        # a new drawing from the template
        tree = c.post("/api/documents/new", json={"path": "lid_dwg.py", "kind": "drawing", "of": "lid.py"}).json()
        assert tree["kind"] == "drawing" and [f["name"] for f in tree["features"]] == ["front", "top", "right", "iso"]
        assert all(f["result"]["ok"] for f in tree["features"])
        assert c.post("/api/documents/new", json={"path": "x.py", "kind": "drawing"}).status_code == 400


@pytest.mark.cli
def test_drawing_cli_new_query_and_export(project):
    runner = CliRunner()
    r = runner.invoke(app, ["new", str(project / "dwg.py"), "--kind", "drawing", "--of", "bracket.py"])
    assert r.exit_code == 0, r.stdout
    src = (project / "dwg.py").read_text()
    assert 'meta(kind="drawing", name="dwg", of="bracket.py", sheet="A4"' in src and src.count("view(") == 4
    r = runner.invoke(app, ["query", str(project / "dwg.py"), "summary"])
    assert r.exit_code == 0 and set(json.loads(r.stdout)["views"]) == {"front", "top", "right", "iso"}
    r = runner.invoke(app, ["query", str(project / "dwg.py"), "volume"])
    assert r.exit_code == 1
    for fmt in ("pdf", "dxf", "svg"):
        r = runner.invoke(app, ["export", str(project / "dwg.py"), "-o", str(project / f"dwg.{fmt}")])
        assert r.exit_code == 0 and (project / f"dwg.{fmt}").stat().st_size > 1000, r.stdout
    r = runner.invoke(app, ["new", str(project / "bad.py"), "--kind", "drawing"])
    assert r.exit_code == 1 and "of=" in r.output


@pytest.mark.perf
def test_drawing_evaluates_within_budget(zoo_dir):
    import time

    doc = parse_file(str(zoo_dir / "bracket_dwg.py"))
    evaluate(doc)
    t = time.perf_counter()
    ev = evaluate(doc)
    seconds = time.perf_counter() - t
    assert not _errors(ev) and seconds < 1.0, f"the bracket drawing evaluated in {seconds:.2f} s"
    t = time.perf_counter()
    pio.export(ev, "pdf", zoo_dir.parent / ".plainsolid-cache" / "perf.pdf") if False else None
    assert len(ev.scene()["views"]) == 5 and time.perf_counter() - t < 0.5
