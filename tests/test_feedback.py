"""L0: what an agent building a part needs from the engine: sizes of macros as dimensions,
the free variables of a sketch, walls sketched upright, parameters as operations, the
caption a render carries."""
from __future__ import annotations

import pytest
from conftest import ZOO

from plainsolid import edit as pedit
from plainsolid import mesh as pmesh
from plainsolid import refs
from plainsolid.evaluate import evaluate
from plainsolid.model import Constraint, Entity, Feature
from plainsolid.parse import parse_document, parse_file
from plainsolid.solver.build import solve_sketch
from plainsolid.workspace import Workspace

pytestmark = pytest.mark.unit


def _sketch(entities, constraints):
    f = Feature(name="s", kind="sketch", args={"on": "XY"})
    f.entities = [Entity(name=n, kind=k, args=a) for n, k, a in entities]
    f.constraints = [Constraint(name=n, kind=k, refs=r, value=v, options=o or {}) for n, k, r, v, o in constraints]
    return f


def test_slot_and_rect_sizes_are_dimensions():
    f = _sketch([("s1", "slot", {"at": (0.0, 0.0), "length": 10.0, "width": 4.0, "angle": 0.0}),
                 ("r1", "rect", {"at": (30.0, 0.0), "width": 8.0, "height": 8.0})],
                [("len", "length", ["s1.length"], 12.0, None), ("w", "length", ["s1.width"], 6.0, None),
                 ("rw", "length", ["r1.width"], 20.0, None), ("rh", "length", ["r1.height"], 5.0, None)])
    sol = solve_sketch(f)
    assert sol.coords["s1"]["length"] == pytest.approx(12.0) and sol.coords["s1"]["width"] == pytest.approx(6.0)
    assert sol.coords["r1"]["width"] == pytest.approx(20.0) and sol.coords["r1"]["height"] == pytest.approx(5.0)
    assert sol.dof == 3 + 2 and sol.free_variables == {"s1": ["center.x", "center.y", "angle"], "r1": ["center.x", "center.y"]}


def test_free_variables_name_what_is_loose():
    f = _sketch([("l1", "line", {"start": (0.0, 0.0), "end": (10.0, 0.0)}), ("c1", "circle", {"at": (5.0, 5.0), "diameter": 4.0})],
                [("fx", "fix", ["l1.start"], None, None), ("h", "horizontal", ["l1"], None, None),
                 ("d", "diameter", ["c1"], 4.0, None)])
    sol = solve_sketch(f)
    assert sol.free_variables == {"l1": ["end.x"], "c1": ["center.x", "center.y"]}
    assert sol.free_entities == ["l1", "c1"] and sol.dof == 3
    ev = evaluate(parse_file(str(ZOO / "enclosure.py")))
    glands = ev.result("glands").sketch
    assert glands.free_variables == {"gland1": ["radius"], "gland2": ["radius"]}


def test_sketches_on_walls_read_upright():
    """On every vertical face y is up (+Z) and x is the viewer's right; horizontal faces keep x = X."""
    src = (ZOO / "bracket.py").read_text() + "\n"
    for name, selector, x_expected in (("w1", 'body.faces.from_sketch("outer_wall")', (0, -1, 0)),  # normal -X
                                       ("w2", 'body.faces.from_sketch("right")', (0, 1, 0)),  # normal +X
                                       ("w3", "body.faces.bottom", (-1, 0, 0)),  # the y=0 end, normal +Y
                                       ("w4", "body.faces.top", (1, 0, 0)),  # the y=-40 end, normal -Y
                                       ("w5", 'body.faces.from_sketch("top")', (1, 0, 0))):  # horizontal, normal +Z
        doc = parse_document(src + f'{name} = sketch("{name}", on={selector})\n', str(ZOO / "bracket.py"))
        ev = evaluate(doc)
        plane = ev.result(name).plane
        assert ev.result(name).ok, ev.result(name).error
        assert plane["x_dir"] == pytest.approx(x_expected, abs=1e-9), name
        if name != "w5":
            assert plane["y_dir"] == pytest.approx((0, 0, 1), abs=1e-9), name


def test_batch_writes_solved_coordinates_back(project):
    ws = Workspace(project)
    d = ws.open("bracket.py")
    out = ws.apply(d, {"op": "batch", "ops": [
        {"op": "add_feature", "kind": "sketch", "name": "s2", "args": {"on": "XY"}},
        {"op": "add_sketch_entity", "sketch": "s2", "kind": "circle", "name": "c", "args": {"diameter": 6, "at": [0, 5]}},
        {"op": "add_sketch_entity", "sketch": "s2", "kind": "line", "name": "l", "args": {"start": [-10, -20], "end": [10, -20]}},
        {"op": "add_constraint", "sketch": "s2", "kind": "fix", "name": "f", "refs": ["l"]},
        {"op": "add_constraint", "sketch": "s2", "kind": "distance", "name": "d", "refs": ["c.center", "l"], "value": 15},
        {"op": "add_constraint", "sketch": "s2", "kind": "vertical", "name": "v", "refs": ["c.center", "l.mid"]},
        {"op": "add_constraint", "sketch": "s2", "kind": "diameter", "name": "dia", "refs": ["c"], "value": 6},
    ]}, None)
    assert out["changed"] and out["solution"]["dof"] == 0, out["solution"]
    text = (project / "bracket.py").read_text()
    assert 's2.circle("c", 6, at=(0, -5))' in text, text[-400:]
    assert 's2.line("l", (-10, -20), (10, -20))' in text


def test_parameter_operations(bracket_src):
    added = pedit.apply(bracket_src, {"op": "add_parameter", "name": "lip", "value": 2.5, "description": "lip height"})
    lines = added.splitlines()
    i = lines.index('lip = param(2.5, "lip height")')
    assert lines[i - 1] == "slot_spacing = 24.0"  # after the last parameter, before every feature
    plain = pedit.apply(bracket_src, {"op": "add_parameter", "name": "lip", "value": {"expr": "thickness / 2"}})
    assert "lip = thickness / 2" in plain
    with pytest.raises(pedit.EditError, match="already exists"):
        pedit.apply(bracket_src, {"op": "add_parameter", "name": "width", "value": 1})
    with pytest.raises(pedit.EditError, match="not a valid"):
        pedit.apply(bracket_src, {"op": "add_parameter", "name": "class", "value": 1})
    with pytest.raises(pedit.EditError, match="still used: .*extrude"):
        pedit.apply(bracket_src, {"op": "delete_parameter", "name": "base_depth"})
    with pytest.raises(pedit.EditError, match="is a feature"):
        pedit.apply(bracket_src, {"op": "delete_parameter", "name": "body"})
    gone = pedit.apply(added, {"op": "delete_parameter", "name": "lip"})
    assert gone == bracket_src
    fresh = 'from plainsolid import *\n\nmeta(name="x")\n\ns = sketch("s", on=XY)\n'
    assert pedit.apply(fresh, {"op": "add_parameter", "name": "t", "value": 3}).splitlines()[3] == "t = 3"
    # a whole number over a float literal keeps the float style
    assert "thickness = 5.0" in pedit.apply(bracket_src, {"op": "set_parameter", "name": "thickness", "value": 5})
    assert "thickness = 5.5" in pedit.apply(bracket_src, {"op": "set_parameter", "name": "thickness", "value": 5.5})
    assert "hole_d = param(6.0," in pedit.apply(bracket_src, {"op": "set_parameter", "name": "hole_d", "value": 6})


def test_render_caption_says_what_a_view_shows():
    ev = evaluate(parse_file(str(ZOO / "bracket.py")))
    items = ev.items()
    m = pmesh.build(items)
    ids = refs.highlight_ids(ev, items, ["body.faces.top", 'hole_cut.edges.from_sketch("bottom").from_sketch("hole1")', "body.vertices.nearest((30, 0, 0))"])
    front = refs.describe_highlights(ev, items, m, ids, "front")
    back = refs.describe_highlights(ev, items, m, ids, "back")
    faces = {e["id"]: e for e in front["entities"] if e["kind"] == "face"}
    end_face = next(iter(faces.values()))
    assert end_face["visible"] and "normal -Y" in end_face["description"] and end_face["labels"] == ["top"]
    assert not next(e for e in back["entities"] if e["kind"] == "face")["visible"]
    edge = next(e for e in front["entities"] if e["kind"] == "edge")
    assert "circle" in edge["description"] and "Ø5" in edge["description"] and not edge["visible"]  # the rim at z=0 lies under the base
    below = refs.describe_highlights(ev, items, m, ids, "bottom")
    assert next(e for e in below["entities"] if e["kind"] == "edge")["visible"]
    assert "not visible from back" in back["text"] and front["count"] == 3
    assert refs.describe_highlights(ev, items, m, {"faces": [], "edges": [], "vertices": []}, "iso")["text"] == "nothing highlighted"
