"""L5: the MCP server's tools in-process, and one real stdio session."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from plainsolid.mcpserver import GUIDE, build_server

pytestmark = pytest.mark.api

TOOLS = ["list_files", "new_document", "get_tree", "get_feature", "get_source", "edit", "set_source", "undo", "redo",
         "query", "measure", "render", "export", "compare"]


@pytest.fixture
def server(project):
    return build_server(project)


def call(server, tool, **args):
    return asyncio.run(server.call_tool(tool, args))


def data(server, tool, **args):
    return json.loads(call(server, tool, **args).content[0].text)


def test_tools_and_guide(server):
    tools = asyncio.run(server.list_tools())
    assert [t.name for t in tools] == TOOLS
    assert all(t.description for t in tools)
    assert GUIDE.exists()
    guide = asyncio.run(server.read_resource("plainsolid://guide"))
    text = guide[0].content if hasattr(guide[0], "content") else str(guide)
    assert "add_sketch_entity" in text and "from_sketch" in text
    files = data(server, "list_files")
    assert any(f["path"].endswith("bracket.py") for f in files["files"])


def test_edit_reports_errors_and_hashes(server, project):
    tree = data(server, "get_tree", path="bracket.py")
    assert tree["features"][0]["name"] == "profile"
    src = data(server, "get_source", path="bracket.py")
    out = data(server, "edit", path="bracket.py", op={"op": "set_parameter", "name": "width", "value": 70}, hash=src["hash"])
    assert out["ok"] and out["changed"] and out["hash"] != src["hash"] and out["errors"] == []
    assert "width = 70" in (project / "bracket.py").read_text()
    with pytest.raises(ToolError, match="expected hash"):
        call(server, "edit", path="bracket.py", op={"op": "set_parameter", "name": "width", "value": 71}, hash=src["hash"])
    with pytest.raises(ToolError, match="nope"):
        call(server, "edit", path="bracket.py", op={"op": "delete_feature", "feature": "nope"})
    broken = data(server, "edit", path="bracket.py", op={"op": "set_argument", "feature": "body", "kwarg": "depth", "value": -1})
    assert broken["changed"] and not broken["ok"] and broken["errors"][0]["line"]
    assert data(server, "undo", path="bracket.py")["ok"]  # back to the widened, healthy bracket
    assert data(server, "redo", path="bracket.py")["ok"] is False  # the broken depth again
    assert data(server, "undo", path="bracket.py")["ok"]
    assert "width = 70" in (project / "bracket.py").read_text()
    assert data(server, "undo", path="bracket.py")["ok"]
    assert "width = 60" in (project / "bracket.py").read_text()


def test_tools_follow_an_external_edit(server, project):
    """The GUI (or a person) writes the file; the next tool call sees it."""
    before = data(server, "query", path="bracket.py", kind="volume")["volume"]
    text = (project / "bracket.py").read_text().replace("width = 60.0", "width = 80.0")
    (project / "bracket.py").write_text(text)
    after = data(server, "query", path="bracket.py", kind="volume")["volume"]
    assert after > before


def test_query_measure_render_compare_export(server, project):
    summary = data(server, "query", path="bracket.py")
    assert summary["counts"]["faces"] == 20
    m = data(server, "measure", path="bracket.py", a="body.faces.top", b="body.faces.bottom")
    assert m["distance"] == pytest.approx(40.0)
    with pytest.raises(ToolError, match="picks"):
        call(server, "measure", path="bracket.py", a="hole_cut")
    with pytest.raises(ToolError, match="not a feature"):
        call(server, "measure", path="bracket.py", a="nothing.faces")
    picture = call(server, "render", path="bracket.py", highlight=["hole_cut", "body.faces.top"], width=200, height=150,
                   output="pics/check.png")
    assert picture.content[0].type == "image" and (project / "pics" / "check.png").read_bytes()[:4] == b"\x89PNG"
    with pytest.raises(ToolError, match="unknown view"):
        call(server, "render", path="bracket.py", view="sideways")
    report = data(server, "compare", path="bracket.py", other="lid.py")
    assert report["base"]["volume"] == pytest.approx(summary["volume"]) and not report["same"]
    overlay = call(server, "render", path="bracket.py", overlay="lid.py", width=200, height=150)
    assert overlay.content[0].type == "image"
    out = data(server, "export", path="bracket.py", output="out/bracket.step")
    assert out["format"] == "step" and (project / "out" / "bracket.step").stat().st_size > 1000
    with pytest.raises(ToolError, match="no such file"):
        call(server, "query", path="missing.py")


def test_new_document_and_set_source(server, project):
    tree = data(server, "new_document", path="fresh.py", kind="part", name="fresh")
    assert tree["meta"]["name"] == "fresh" and not tree["features"]
    with pytest.raises(ToolError):
        call(server, "new_document", path="fresh.py")
    out = data(server, "set_source", path="fresh.py", source='from plainsolid import *\nmeta(name="fresh")\ns = sketch("s", on=XY)\ns.rect("r", 10, 10)\nb = extrude("b", s, 5)\n')
    assert out["ok"] and data(server, "query", path="fresh.py", kind="volume")["volume"] == pytest.approx(500)


@pytest.mark.timeout(120)
def test_stdio_session(project):
    """`plainsolid mcp DIR` speaks the protocol over stdio."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "plainsolid.cli", "mcp", str(project)])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            info = await session.initialize()
            assert "plainsolid" in (info.instructions or "")
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == TOOLS
            result = await session.call_tool("query", {"path": "bracket.py", "kind": "counts"})
            assert not result.is_error
            assert json.loads(result.content[0].text)["faces"] == 20
            bad = await session.call_tool("query", {"path": "bracket.py", "kind": "nope"})
            assert bad.is_error and "unknown query" in bad.content[0].text
            guide = await session.read_resource("plainsolid://guide")
            assert "add_feature" in guide.contents[0].text

    asyncio.run(go())
