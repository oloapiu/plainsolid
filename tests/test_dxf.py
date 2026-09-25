"""DXF files: read into a sketch as a rigid outline (L7), solved and converted into lines
(L3, round-trip), shown on a drawing sheet with no model (L7), and opened as a part or a
drawing through the workspace and the API (L5)."""
import math
import time

import pytest

from plainsolid import dxfimport as pdxf
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document, parse_file
from plainsolid.workspace import Workspace

PART = '''from plainsolid import *

meta(name="plate")

thickness = 3.0

profile = sketch("profile", on=XY)
profile.import_dxf("outline", "plate.dxf"{extra})
{constraints}body = extrude("body", profile, thickness)
'''
# 60 x 40 with the top right corner rounded R10 (a bulge) and a 10 mm hole at (20, 20)
PLATE_VOLUME = (60 * 40 - (100 - math.pi * 100 / 4) - math.pi * 25) * 3


def plate_dxf(path, *, units=4, scale=1.0, gap=0.0, layers=False):
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = units
    msp = doc.modelspace()
    doc.layers.add("CUT")
    doc.layers.add("NOTES")
    s = scale
    msp.add_lwpolyline([(0, 0, 0), (60 * s, 0, 0), (60 * s, 30 * s, math.tan(math.radians(22.5))), (50 * s, 40 * s, 0),
                        (gap, 40 * s, 0)], format="xyb", close=not gap, dxfattribs={"layer": "CUT"})
    if gap:
        msp.add_line((0, 40 * s), (0, 0), dxfattribs={"layer": "CUT"})
    msp.add_circle((20 * s, 20 * s), 5 * s, dxfattribs={"layer": "CUT"})
    msp.add_text("PLATE", dxfattribs={"layer": "NOTES", "height": 3}).set_placement((5, 45))
    if layers:
        msp.add_line((-10, -10), (70, -10), dxfattribs={"layer": "NOTES"})
    doc.saveas(path)
    return path


def write_part(tmp_path, extra="", constraints='profile.fix("placed", "outline")\n'):
    plate_dxf(tmp_path / "plate.dxf")
    p = tmp_path / "plate.py"
    p.write_text(PART.format(extra=extra, constraints=constraints))
    return p


# --- reading --------------------------------------------------------------------------------

@pytest.mark.io
def test_profile_reads_curves_in_millimetres_and_leaves_text_out(tmp_path):
    prof = pdxf.read_profile(plate_dxf(tmp_path / "a.dxf"))
    kinds = sorted(it.kind for it in prof.items)
    assert kinds == ["arc", "circle", "line", "line", "line", "line"]
    arc = next(it for it in prof.items if it.kind == "arc")
    assert arc.coords["center"] == pytest.approx((50, 30)) and arc.coords["radius"] == pytest.approx(10)
    assert prof.bbox == pytest.approx((0, 0, 60, 40)) and prof.layers == {"CUT": 2}
    assert prof.warnings == []  # the text is annotation: left out without a word
    # inches become millimetres; a file without units is read as millimetres and says so
    inch = pdxf.read_profile(plate_dxf(tmp_path / "in.dxf", units=1, scale=1 / 25.4))
    assert inch.bbox == pytest.approx((0, 0, 60, 40))
    bare = pdxf.read_profile(plate_dxf(tmp_path / "bare.dxf", units=0))
    assert any("no units" in w for w in bare.warnings)


@pytest.mark.io
def test_profile_layers_gaps_blocks_and_splines(tmp_path):
    import ezdxf

    both = plate_dxf(tmp_path / "l.dxf", layers=True)
    assert len(pdxf.read_profile(both).items) == 7
    assert len(pdxf.read_profile(both, "cut").items) == 6  # layer names match without case
    with pytest.raises(pdxf.DxfError, match="no layer 'HOLES'.*CUT"):
        pdxf.read_profile(both, "HOLES")
    # an outline that misses closing by 0.05 mm: reported with where
    gap = pdxf.read_profile(plate_dxf(tmp_path / "g.dxf", gap=0.05))
    assert any("2 open end(s); 1 gap(s)" in w and "0.05 mm at (" in w for w in gap.warnings)
    # ends within the snap distance are joined exactly
    near = pdxf.read_profile(plate_dxf(tmp_path / "n.dxf", gap=0.001))
    assert not any("open end" in w for w in near.warnings)
    # a block reference is exploded; a spline becomes lines within the flattening distance
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    blk = doc.blocks.new("HOLE")
    blk.add_circle((0, 0), 2)
    msp = doc.modelspace()
    msp.add_blockref("HOLE", (10, 10))
    msp.add_blockref("HOLE", (30, 10))
    msp.add_spline([(0, 0), (10, 5), (20, 0), (30, 5)])
    doc.saveas(tmp_path / "b.dxf")
    prof = pdxf.read_profile(tmp_path / "b.dxf")
    circles = sorted(it.coords["center"] for it in prof.items if it.kind == "circle")
    assert circles == [pytest.approx((10, 10)), pytest.approx((30, 10))]
    assert sum(it.kind == "line" for it in prof.items) > 10


# --- a sketch -----------------------------------------------------------------------------------

@pytest.mark.solver
def test_a_fixed_import_extrudes_into_the_plate(tmp_path):
    ev = evaluate(parse_file(str(write_part(tmp_path))))
    sk = next(r for r in ev.results if r.name == "profile")
    assert sk.ok and sk.sketch.dof == 0 and ev.body is not None
    assert ev.body.volume == pytest.approx(PLATE_VOLUME, rel=1e-6)
    assert sk.warnings == []
    # the curves label the faces they sweep, like a projection's
    labels = {t for tags in ev.face_tags.values() for t in tags}
    assert {"outline.e2", "outline.e5"} <= labels


@pytest.mark.solver
def test_the_import_is_rigid_and_placed_by_relations(tmp_path):
    free = evaluate(parse_file(str(write_part(tmp_path, constraints=""))))
    sol = next(r for r in free.results if r.name == "profile").sketch
    assert sol.dof == 3 and sol.free_variables["outline"] == ["at.x", "at.y", "angle"]
    # the hole's centre on the origin, the bottom edge horizontal: nothing is loose, the plate moved
    placed = write_part(tmp_path, constraints='profile.coincident("c", "outline.e5.center", "origin")\n'
                                              'profile.horizontal("h", "outline.e0")\n')
    ev = evaluate(parse_file(str(placed)))
    sol = next(r for r in ev.results if r.name == "profile").sketch
    assert sol.dof == 0 and sol.coords["outline"]["at"] == pytest.approx((-20, -20))
    assert ev.body.volume == pytest.approx(PLATE_VOLUME, rel=1e-6)
    assert ev.body.bounding_box().min.X == pytest.approx(-20)


@pytest.mark.roundtrip
def test_dragging_writes_where_the_file_lands(tmp_path):
    p = write_part(tmp_path, constraints="")
    ws = Workspace(tmp_path)
    doc = ws.open(p.name)
    r = ws.apply(doc, {"op": "solve_sketch", "sketch": "profile", "drag": {"outline.e5.center": [25, 20]}}, doc.hash)
    assert r["changed"]
    assert 'profile.import_dxf("outline", "plate.dxf", at=(5, 0))' in doc.source
    assert parse_document(doc.source).feature("profile").entity("outline").args["at"] == (5.0, 0.0)


@pytest.mark.roundtrip
def test_convert_turns_the_import_into_lines_where_it_stands(tmp_path):
    p = write_part(tmp_path, extra=", at=(5, 0)", constraints='profile.fix("placed", "outline")\n'
                                                               'profile.diameter("hole", "outline.e5", 10)\n')
    ws = Workspace(tmp_path)
    doc = ws.open(p.name)
    r = ws.apply(doc, {"op": "convert_dxf", "sketch": "profile", "entity": "outline"}, doc.hash)
    assert r["changed"] and not r["solution"]["conflicting"]
    src = doc.source
    assert "import_dxf" not in src and 'profile.fix("placed"' not in src
    assert 'profile.line("outline_0", (5, 0), (65, 0))' in src
    assert 'profile.arc("outline_2", (55, 30), (65, 30), (55, 40))' in src
    assert 'profile.circle("outline_5", 10, at=(25, 20))' in src
    assert 'profile.diameter("hole", "outline_5", 10)' in src  # the relation moved to the new circle
    assert src.count("profile.coincident(") == 5  # the closed outline's five corners
    ev = evaluate(parse_document(src, str(p)))
    assert ev.body.volume == pytest.approx(PLATE_VOLUME, rel=1e-6)
    # the curves stay where they were; the relations now define the sketch
    assert next(x for x in ev.results if x.name == "profile").sketch.dof > 0
    with pytest.raises(Exception, match="not a DXF import"):
        ws.apply(doc, {"op": "convert_dxf", "sketch": "profile", "entity": "outline_0"}, doc.hash)


@pytest.mark.solver
def test_a_changed_dxf_rebuilds_the_part(tmp_path):
    p = write_part(tmp_path)
    doc = parse_file(str(p))
    first = evaluate(doc)
    time.sleep(0.01)
    plate_dxf(tmp_path / "plate.dxf", scale=2.0)
    second = evaluate(parse_file(str(p)), cache=first)
    assert second.body.volume == pytest.approx(PLATE_VOLUME * 4, rel=1e-6)
    from plainsolid.dependencies import references

    assert (tmp_path / "plate.dxf").resolve() in references(doc)


@pytest.mark.solver
def test_a_missing_file_is_an_error_on_the_sketch(tmp_path):
    p = tmp_path / "plate.py"
    p.write_text(PART.format(extra="", constraints=""))
    ev = evaluate(parse_file(str(p)))
    sk = next(r for r in ev.results if r.name == "profile")
    assert not sk.ok and "import_dxf 'outline': no such file" in sk.error.message


# --- a drawing ---------------------------------------------------------------------------------

DRAWING = '''from plainsolid import *

meta(kind="drawing", name="legacy", sheet="A4")

sheet = view("sheet", dxf="plate.dxf", at=(148, 120))
note("rev", "Superseded", at=(0, -40), view="sheet")
'''


@pytest.mark.io
def test_a_dxf_drawing_needs_no_model_and_exports(tmp_path):
    import ezdxf

    from plainsolid import io as pio
    from plainsolid.drawing import layout

    src = tmp_path / "plate.dxf"
    plate_dxf(src)
    d = ezdxf.readfile(src)
    d.modelspace().add_linear_dim(base=(0, -10), p1=(0, 0), p2=(60, 0)).render()
    d.saveas(src)
    p = tmp_path / "legacy.py"
    p.write_text(DRAWING)
    ev = evaluate(parse_file(str(p)))
    assert all(r.ok for r in ev.results), [r.error for r in ev.results]
    scene = layout(ev)
    v = scene["views"][0]
    assert scene["model"]["error"] is None and v["dxf"] == "plate.dxf" and v["direction"] == "dxf"
    assert len(v["visible"]) == 6 and v["annotation"] and v["fills"]  # the dimension's lines and arrowheads
    assert {t["text"] for t in v["texts"]} >= {"PLATE", "60"}
    # the extents centre on at=
    x0, _y0, x1, _y1 = v["bbox"]
    assert (x0 + x1) / 2 == pytest.approx(148) and x1 - x0 == pytest.approx(60)
    assert any(t["text"] == "plate.dxf" for t in scene["frame"]["texts"])  # the title block's PART
    for fmt in ("svg", "dxf", "pdf"):
        out = pio.export(ev, fmt, tmp_path / f"out.{fmt}")
        assert out.stat().st_size > 1000
    back = ezdxf.readfile(tmp_path / "out.dxf")
    assert {e.dxf.text for e in back.modelspace().query("TEXT")} >= {"PLATE", "Superseded"}


@pytest.mark.io
def test_dimensions_measure_model_views_only(tmp_path, zoo_dir):
    import shutil

    plate_dxf(tmp_path / "plate.dxf")
    shutil.copy(zoo_dir / "bracket.py", tmp_path / "bracket.py")
    p = tmp_path / "mixed.py"
    p.write_text('''from plainsolid import *

meta(kind="drawing", name="mixed", of="bracket.py", sheet="A3")

front = view("front", direction=FRONT, at=(100, 200))
old = view("old", dxf="plate.dxf", at=(300, 200))
dimension("w", old.edges.all(), at=(0, -30))
''')
    ev = evaluate(parse_file(str(p)))
    by = {r.name: r for r in ev.results}
    assert by["front"].ok and by["old"].ok
    assert not by["w"].ok and "shows plate.dxf; dimensions measure model views" in by["w"].error.message
    # without a model, a model view says what is missing
    p.write_text(DRAWING + 'front = view("front", direction=FRONT, at=(60, 60))\n')
    ev = evaluate(parse_file(str(p)))
    by = {r.name: r for r in ev.results}
    assert by["sheet"].ok and not by["front"].ok and "of=" in by["front"].error.message


@pytest.mark.unit
def test_sheet_fit_picks_the_smallest_sheet_and_a_standard_scale():
    assert pdxf.sheet_fit(60, 40)[:2] == ("A4", 1.0)
    assert pdxf.sheet_fit(350, 200)[:2] == ("A3", 1.0)
    assert pdxf.sheet_fit(900, 500)[:2] == ("A4", 0.2)  # 1:5 fits the small sheet already
    assert pdxf.sheet_fit(700, 300)[:2] == ("A3", 0.5)


# --- opening -----------------------------------------------------------------------------------

@pytest.mark.api
def test_a_dxf_opens_as_a_part_or_a_drawing(tmp_path, tmp_path_factory):
    from fastapi.testclient import TestClient

    from plainsolid.server import create_app

    elsewhere = tmp_path_factory.mktemp("downloads")
    src = plate_dxf(elsewhere / "Plate Rev B.dxf")
    with TestClient(create_app(tmp_path, serve_client=False)) as c:
        assert c.post("/api/open-request", json={"path": str(src)}).json() == {
            "action": "import", "source": str(src), "name": "Plate Rev B", "suffix": ".dxf", "delivered": 0}
        r = c.post("/api/documents/import", json={"source": str(src), "folder": "proposals", "name": "plate"})
        assert r.status_code == 400 and "part or as a drawing" in r.text and not (tmp_path / "proposals").exists()
        tree = c.post("/api/documents/import", json={"source": str(src), "folder": "proposals", "name": "plate", "mode": "part"}).json()
        assert tree["kind"] == "part" and tree["path"] == str(tmp_path / "proposals" / "plate.py") and tree["created"]
        # only the sketch: what to make of the curves is the person's choice
        assert [f["kind"] for f in tree["features"]] == ["sketch"] and not tree["evaluation"]["has_body"] and not tree["errors"]
        assert tree["features"][0]["result"]["ok"] and tree["features"][0]["result"]["sketch"]["dof"] == 0
        part_src = (tmp_path / "proposals" / "plate.py").read_text()
        assert 'profile.import_dxf("outline", "plate.dxf")' in part_src and "extrude" not in part_src
        # opened again, the file is not written again and nothing says it was
        again = c.post("/api/documents/open", json={"path": "proposals/plate.dxf", "mode": "part"}).json()
        assert again["id"] == tree["id"] and "created" not in again
        # the same file, already inside, as a drawing: a wrapper of its own, the part untouched
        assert c.post("/api/documents/open", json={"path": "proposals/plate.dxf"}).status_code == 400
        dwg = c.post("/api/documents/open", json={"path": "proposals/plate.dxf", "mode": "drawing"}).json()
        assert dwg["kind"] == "drawing" and dwg["path"] == str(tmp_path / "proposals" / "plate_dwg.py")
        assert dwg["evaluation"]["drawing"]["views"][0]["dxf"] == "plate.dxf"
        files = c.get("/api/files").json()["files"]
        assert {"path": "proposals/plate.dxf", "kind": "dxf", "part": "proposals/plate.py",
                "drawing": "proposals/plate_dwg.py"} in files
        r = c.put("/api/documents/upload?folder=vendor&name=cut&suffix=.dxf&mode=drawing", content=src.read_bytes())
        assert r.status_code == 200 and (tmp_path / "vendor" / "cut_dwg.py").exists()


@pytest.mark.cli
def test_new_drawing_of_a_dxf(tmp_path):
    from typer.testing import CliRunner

    from plainsolid.cli import app

    plate_dxf(tmp_path / "plate.dxf")
    r = CliRunner().invoke(app, ["new", str(tmp_path / "legacy.py"), "--kind", "drawing", "--of", "plate.dxf"])
    assert r.exit_code == 0, r.output
    src = (tmp_path / "legacy.py").read_text()
    assert 'meta(kind="drawing", name="legacy", title="plate", sheet="A4")' in src and 'view("sheet", dxf="plate.dxf"' in src
    r = CliRunner().invoke(app, ["new", str(tmp_path / "x.py"), "--kind", "drawing", "--of", "gone.dxf"])
    assert r.exit_code == 1 and "no such DXF file" in r.output


@pytest.mark.solver
def test_many_holes_nest_and_label_quickly(tmp_path):
    """A plate with a hundred holes: the nesting builds the face from its wires and the side faces
    find their curve through the box index, and each hole's wall is still named by its circle."""
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (300, 0), (300, 300), (0, 300)], close=True)
    for i in range(10):
        for j in range(10):
            msp.add_circle((15 + 30 * i, 15 + 30 * j), 5)
    doc.saveas(tmp_path / "plate.dxf")
    p = tmp_path / "plate.py"
    p.write_text(PART.format(extra="", constraints='profile.fix("placed", "outline")\n'))
    t = time.perf_counter()
    ev = evaluate(parse_file(str(p)))
    assert time.perf_counter() - t < 60
    assert ev.body is not None and ev.body.volume == pytest.approx((300 * 300 - 100 * math.pi * 25) * 3, rel=1e-6)
    items = pdxf.read_profile(tmp_path / "plate.dxf").items
    k = next(i for i, it in enumerate(items) if it.kind == "circle" and it.coords["center"] == pytest.approx((135, 165)))
    wall = next(f for f in ev.body.faces() if f.geom_type.name == "CYLINDER" and abs(f.center().X - 135) < 6 and abs(f.center().Y - 165) < 6)
    assert ev.identity().tags[hash(wall.wrapped)] == (f"outline.e{k}",)


@pytest.mark.solver
def test_separate_outlines_suggest_the_drawing(tmp_path):
    import ezdxf

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    msp.add_lwpolyline([(20, 0), (30, 0), (30, 10), (20, 10)], close=True)
    doc.saveas(tmp_path / "plate.dxf")
    p = tmp_path / "plate.py"
    p.write_text(PART.format(extra="", constraints=""))
    body = next(r for r in evaluate(parse_file(str(p))).results if r.name == "body")
    assert not body.ok and "2 separate solids" in body.error.message and "open the .dxf as a drawing" in body.error.message


@pytest.mark.unit
def test_pdf_text_the_standard_font_cannot_draw_gets_a_unicode_font():
    from plainsolid.drawing import _pdf_font

    assert _pdf_font("Break all edges 0.5 mm ±0.1°") == "Helvetica"
    assert _pdf_font("標籤需要討論放置位置。") != "Helvetica"
