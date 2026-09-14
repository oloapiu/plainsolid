"""L12: a scripted agent session builds the bracket through the CLI alone (and
through the MCP tools) and the result matches the golden geometry and the file a
person wrote."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from conftest import ZOO, load_expected
from typer.testing import CliRunner

from plainsolid.cli import app

pytestmark = pytest.mark.agent

EDGE_FILLET = 'body.edges.from_sketch("inner_bottom").from_sketch("inner_wall")'
EDGE_CHAMFER = 'body.edges.from_sketch("bottom").from_sketch("outer_wall")'


def entity(sketch, kind, name, **args):
    return {"op": "add_sketch_entity", "sketch": sketch, "kind": kind, "name": name, "args": args}


def constraint(sketch, kind, name, refs, value=None, **options):
    op = {"op": "add_constraint", "sketch": sketch, "kind": kind, "name": name, "refs": refs}
    if value is not None:
        op["value"] = value
    if options:
        op["options"] = options
    return op


def feature(kind, name, **args):
    return {"op": "add_feature", "kind": kind, "name": name, "args": args}


def expr(text):
    return {"expr": text}


# The bracket of the zoo, as the operations an agent issues. Each entry is one edit.
SCRIPT = [
    {"op": "batch", "ops": [{"op": "add_feature", "statement": s} for s in (
        "thickness = 4.0", "width = 60.0", "base_depth = 40.0", "height = 50.0",
        'hole_d = param(5.0, "clearance hole for M5")', "slot_spacing = 24.0")]},
    {"op": "batch", "ops": [
        feature("sketch", "profile", on="XZ"),
        entity("profile", "line", "bottom", start=[-30, 0], end=[30, 0]),
        entity("profile", "line", "right", start=[30, 0], end=[30, 4]),
        entity("profile", "line", "inner_bottom", start=[30, 4], end=[-26, 4]),
        entity("profile", "line", "inner_wall", start=[-26, 4], end=[-26, 50]),
        entity("profile", "line", "top", start=[-26, 50], end=[-30, 50]),
        entity("profile", "line", "outer_wall", start=[-30, 50], end=[-30, 0]),
        constraint("profile", "coincident", "c1", ["bottom.end", "right.start"]),
        constraint("profile", "coincident", "c2", ["right.end", "inner_bottom.start"]),
        constraint("profile", "coincident", "c3", ["inner_bottom.end", "inner_wall.start"]),
        constraint("profile", "coincident", "c4", ["inner_wall.end", "top.start"]),
        constraint("profile", "coincident", "c5", ["top.end", "outer_wall.start"]),
        constraint("profile", "coincident", "c6", ["outer_wall.end", "bottom.start"]),
        constraint("profile", "horizontal", "h1", ["bottom"]),
        constraint("profile", "horizontal", "h2", ["inner_bottom"]),
        constraint("profile", "horizontal", "h3", ["top"]),
        constraint("profile", "vertical", "v1", ["right"]),
        constraint("profile", "vertical", "v2", ["inner_wall"]),
        constraint("profile", "vertical", "v3", ["outer_wall"]),
        constraint("profile", "fix", "origin", ["bottom.start"]),
        constraint("profile", "length", "w", ["bottom"], expr("width")),
        constraint("profile", "length", "t", ["right"], expr("thickness")),
        constraint("profile", "length", "h", ["outer_wall"], expr("height")),
        constraint("profile", "equal", "wall_t", ["top", "right"]),
    ]},
    feature("extrude", "body", sketch="profile", depth=expr("base_depth")),
    feature("fillet", "inner", edges=expr(EDGE_FILLET), radius=3),
    feature("chamfer", "outer", edges=expr(EDGE_CHAMFER), distance=2),
    {"op": "batch", "ops": [
        feature("sketch", "holes", on="XY"),
        entity("holes", "project", "back_edge", selector=expr('body.edges.where(parallel_to="+X").nearest((0, 0, 0))')),
        entity("holes", "project", "side_edge", selector=expr('body.edges.where(parallel_to="+Y").nearest((30, -20, 0))')),
        entity("holes", "circle", "hole1", diameter=expr("hole_d"), at=[-10, -20]),
        entity("holes", "circle", "hole2", diameter=expr("hole_d"), at=[20, -20]),
        constraint("holes", "diameter", "d1", ["hole1"], expr("hole_d")),
        constraint("holes", "equal", "same", ["hole1", "hole2"]),
        constraint("holes", "distance", "y1", ["hole1.center", "back_edge"], expr("base_depth / 2")),
        constraint("holes", "distance", "y2", ["hole2.center", "back_edge"], expr("base_depth / 2")),
        constraint("holes", "distance", "x1", ["hole1.center", "side_edge"], 40),
        constraint("holes", "distance", "x2", ["hole2.center", "side_edge"], 10),
        feature("cut", "hole_cut", sketch="holes", through=True),
    ]},
    {"op": "batch", "ops": [
        feature("sketch", "slots", on="YZ"),
        entity("slots", "project", "wall_top", selector=expr('body.edges.from_sketch("top").from_sketch("outer_wall")')),
        entity("slots", "project", "wall_end", selector=expr('body.edges.top.from_sketch("outer_wall")')),
        entity("slots", "slot", "slot1", length=16, width=5, at=[-32, 30], angle=90),
        entity("slots", "slot", "slot2", length=16, width=5, at=[-8, 30], angle=90),
        constraint("slots", "vertical", "upright1", ["slot1.axis"]),
        constraint("slots", "length", "slot_len", ["slot1.length"], 16),
        constraint("slots", "length", "slot_w", ["slot1.width"], 5),
        constraint("slots", "distance", "down", ["slot1.center", "wall_top"], 20),
        constraint("slots", "distance", "in", ["slot1.center", "wall_end"], 8),
        constraint("slots", "distance", "pitch", ["slot1.center", "slot2.center"], expr("slot_spacing"), along="x"),
        constraint("slots", "distance", "level", ["slot1.center", "slot2.center"], 0, along="y"),
        constraint("slots", "equal", "same_length", ["slot1.axis", "slot2.axis"]),
        constraint("slots", "equal", "same_width", ["slot1.start_arc", "slot2.start_arc"]),
        constraint("slots", "parallel", "upright", ["slot1.axis", "slot2.axis"]),
        feature("cut", "slot_cut", sketch="slots", through=True, flip=True),
    ]},
]


class CliSession:
    """The agent drives the command line: one process per command, JSON in and out."""

    def __init__(self, project: Path):
        self.project = project
        self.runner = CliRunner()

    def run(self, *args):
        result = self.runner.invoke(app, [str(a) for a in args])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def new(self, name):
        return self.run("new", self.project / name, "--name", "bracket")

    def edit(self, name, op):
        return self.run("edit", self.project / name, json.dumps(op))

    def tree(self, name):
        return self.run("tree", self.project / name)

    def query(self, name, kind="summary"):
        return self.run("query", self.project / name, kind)

    def measure(self, name, a, b=None):
        return self.run("measure", self.project / name, a, *([b] if b else []))

    def render(self, name, out, *highlight, upto=None):
        args = ["render", self.project / name, "-o", out, "--size", "240x180"]
        for h in highlight:
            args += ["--highlight", h]
        if upto:
            args += ["--upto", upto]
        return self.run(*args)

    def export(self, name, out):
        return self.run("export", self.project / name, "-o", out)


class McpSession:
    """The agent calls the MCP tools in-process."""

    def __init__(self, project: Path):
        from plainsolid.mcpserver import build_server

        self.project = project
        self.server = build_server(project)

    def call(self, tool, **args):
        result = asyncio.run(self.server.call_tool(tool, args))
        assert not result.is_error, result.content
        return result

    def data(self, tool, **args):
        return json.loads(self.call(tool, **args).content[0].text)

    def new(self, name):
        return self.data("new_document", path=name, name="bracket")

    def edit(self, name, op):
        out = self.data("edit", path=name, op=op)
        assert out["ok"], out["errors"]
        return out

    def tree(self, name):
        return self.data("get_tree", path=name)

    def query(self, name, kind="summary"):
        return self.data("query", path=name, kind=kind)

    def measure(self, name, a, b=None):
        return self.data("measure", path=name, a=a, b=b)

    def render(self, name, out, *highlight, upto=None):
        result = self.call("render", path=name, highlight=list(highlight), upto=upto, width=240, height=180, output=str(out))
        assert result.content[0].type == "image" and result.content[1].type == "text"
        return {"bytes": len(result.content[0].data), "caption": result.content[1].text}

    def export(self, name, out):
        return self.data("export", path=name, output=str(out))


def statements(text: str) -> list[str]:
    """The DSL statements of a file, without comments, blank lines and the meta line."""
    out = []
    for line in text.splitlines():
        code = re.sub(r"\s+#.*$", "", line).strip()
        if code and not code.startswith("#") and not code.startswith("meta(") and not code.startswith("from "):
            out.append(code)
    return out


@pytest.mark.parametrize("transport", [CliSession, McpSession])
def test_agent_builds_the_bracket(tmp_path, transport):
    session = transport(tmp_path)
    tree = session.new("bracket.py")
    assert tree["meta"]["name"] == "bracket" and not tree["features"]
    for op in SCRIPT:
        out = session.edit("bracket.py", op)
        assert out["changed"], op
    tree = session.tree("bracket.py")
    assert [f["name"] for f in tree["features"]] == ["profile", "body", "inner", "outer", "holes", "hole_cut", "slots", "slot_cut"]
    assert all(f["result"]["ok"] for f in tree["features"]), [f["result"]["error"] for f in tree["features"] if not f["result"]["ok"]]
    assert all(f["result"]["sketch"]["dof"] == 0 for f in tree["features"] if f["kind"] == "sketch"), "a sketch is under-constrained"

    # the numbers are the golden ones
    expected = load_expected(ZOO / "bracket.expected.json")
    actual = session.query("bracket.py")
    for key in ("volume", "area", "mass"):
        assert actual[key] == pytest.approx(expected[key], rel=1e-4), key
    for key in ("min", "max"):
        assert actual["bbox"][key] == pytest.approx(expected["bbox"][key], abs=1e-4)
    assert actual["center_of_mass"] == pytest.approx(expected["center_of_mass"], abs=1e-4)
    assert actual["counts"] == expected["counts"]

    # and the file is the one a person wrote: every statement of the zoo's bracket, in order
    written = statements((tmp_path / "bracket.py").read_text())
    human = statements((ZOO / "bracket.py").read_text())
    assert written == human

    # the agent's checks along the way: a highlighted render, a measurement, an export
    picture = tmp_path / "check.png"
    caption = session.render("bracket.py", picture, "hole_cut", "body.faces.top", 'body.vertices.nearest((30, 0, 0))')
    assert picture.read_bytes()[:4] == b"\x89PNG"
    assert "face" in caption["caption"] and "hole_cut" in caption["caption"] and "visible" in caption["caption"]
    session.render("bracket.py", picture, EDGE_CHAMFER, upto="inner")
    m = session.measure("bracket.py", "body.faces.top", "body.faces.bottom")
    assert m["distance"] == pytest.approx(40.0)
    d = session.measure("bracket.py", 'hole_cut.faces.from_sketch("hole1")')
    assert d["a"]["diameter"] == pytest.approx(5.0)
    session.export("bracket.py", tmp_path / "bracket.step")
    assert (tmp_path / "bracket.step").stat().st_size > 1000


def test_agent_sees_a_failed_feature(tmp_path):
    """A wrong selector fails its feature with a line, and the answer says so at once."""
    session = McpSession(tmp_path)
    session.new("bracket.py")
    for op in SCRIPT[:3]:
        session.edit("bracket.py", op)
    out = session.data("edit", path="bracket.py", op=feature("fillet", "bad", edges=expr('body.edges.from_sketch("nowhere")'), radius=3))
    assert out["changed"] and not out["ok"]
    assert "nowhere" in out["errors"][0]["message"] and out["errors"][0]["line"]
    tree = session.tree("bracket.py")
    assert tree["features"][-1]["result"]["ok"] is False and tree["evaluation"]["has_body"]
    undone = session.data("undo", path="bracket.py")
    assert undone["ok"] and "bad" not in (tmp_path / "bracket.py").read_text()
