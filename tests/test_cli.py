"""L6: the CLI, in-process through typer's runner plus one real subprocess."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from plainsolid.cli import app

pytestmark = pytest.mark.cli
runner = CliRunner()


def run(*args):
    result = runner.invoke(app, [str(a) for a in args])
    return result.exit_code, result.stdout


def test_tree_and_query(project):
    code, out = run("tree", project / "bracket.py")
    assert code == 0
    tree = json.loads(out)
    assert [f["name"] for f in tree["features"]][:2] == ["profile", "body"]
    assert tree["evaluation"]["has_body"]
    code, out = run("query", project / "bracket.py", "bbox")
    assert code == 0 and json.loads(out)["size"] == [60.0, 40.0, 50.0]
    code, out = run("query", project / "bracket.py", "volume", "--upto", "body")
    assert json.loads(out)["volume"] == pytest.approx(16960)
    code, _ = run("query", project / "bracket.py", "nope")
    assert code == 1


def test_edit_dry_run_and_real(project):
    f = project / "bracket.py"
    before = f.read_text()
    code, out = run("edit", f, '{"op":"set_parameter","name":"width","value":70}', "--dry-run")
    assert code == 0 and json.loads(out)["changed"] and f.read_text() == before
    code, out = run("edit", f, '{"op":"set_parameter","name":"width","value":70}')
    assert code == 0 and "width = 70" in f.read_text()
    code, _ = run("edit", f, '{"op":"delete_feature","feature":"nope"}')
    assert code == 1
    code, _ = run("edit", f, "not json")
    assert code == 1
    code, _ = run("edit", f, '{"op":"set_parameter","name":"width","value":71,"hash":"stale"}')
    assert code == 3


def test_export_and_mesh(project):
    f = project / "bracket.py"
    code, out = run("export", f, "-o", project / "b.step")
    assert code == 0 and json.loads(out)["format"] == "step" and (project / "b.step").stat().st_size > 1000
    code, out = run("mesh", f, "-o", project / "b.bin")
    assert code == 0 and json.loads(out)["faces"] == 20


@pytest.mark.render
def test_render_command(project):
    f = project / "bracket.py"
    code, out = run("render", f, "-o", project / "r.png", "--view", "top", "--size", "200x150")
    assert code == 0 and json.loads(out)["size"] == [200, 150]
    assert (project / "r.png").read_bytes()[:4] == b"\x89PNG"


def test_console_script_subprocess(project):
    result = subprocess.run([sys.executable, "-m", "plainsolid.cli", "query", str(project / "bracket.py"), "counts"],
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["faces"] == 20


def test_tree_on_step_and_measure(project):
    import shutil

    from conftest import ZOO
    (project / "vendor").mkdir(exist_ok=True)
    shutil.copy(ZOO / "vendor" / "node_stub.step", project / "vendor" / "node_stub.step")
    code, out = run("tree", project / "vendor" / "node_stub.step")
    assert code == 0 and json.loads(out)["kind"] == "assembly" and (project / "vendor" / "node_stub.py").exists()
    code, out = run("measure", project / "vendor" / "node_stub.py", "point:0,0,0", "point:3,4,0")
    assert code == 0 and json.loads(out)["distance"] == pytest.approx(5.0)
    code, out = run("measure", project / "vendor" / "node_stub.py", "face:0", "--plane", "XY", "--offset", "5")
    assert code == 0 and json.loads(out)["a"]["kind"] == "face"
    code, _ = run("measure", project / "vendor" / "node_stub.py", "banana:1")
    assert code == 1
    code, out = run("mesh", project / "vendor" / "node_stub.py", "-o", project / "n.bin", "--plane", "XY", "--offset", "5")
    assert code == 0 and len(json.loads(out)["items"]) == 4


def test_new_command(project):
    code, out = run("new", project / "fresh.py")
    assert code == 0 and json.loads(out)["meta"]["name"] == "fresh" and (project / "fresh.py").exists()
    code, _ = run("new", project / "fresh.py")
    assert code == 2
    code, out = run("new", project / "asm.py", "--kind", "assembly", "--name", "Node")
    assert code == 0 and json.loads(out)["kind"] == "assembly"


def test_measure_and_render_by_selector(project):
    f = project / "bracket.py"
    code, out = run("measure", f, "body.faces.top", "body.faces.bottom")
    assert code == 0 and json.loads(out)["distance"] == pytest.approx(40.0)
    code, out = run("measure", f, 'hole_cut.faces.from_sketch("hole1")')
    assert code == 0 and json.loads(out)["a"]["diameter"] == pytest.approx(5.0)
    code, _ = run("measure", f, "hole_cut")
    assert code == 1
    code, _ = run("measure", f, "point:1,2")
    assert code == 1


@pytest.mark.render
def test_render_highlights_selectors_and_overlays(project):
    f = project / "bracket.py"
    code, out = run("render", f, "-o", project / "h.png", "--size", "200x150", "--highlight", "hole_cut",
                    "--highlight", "body.vertices.nearest((30, 0, 0))", "--highlight", "3,4")
    assert code == 0
    ids = json.loads(out)["highlighted"]
    assert len(ids["faces"]) == 4 and len(ids["edges"]) == 6 and len(ids["vertices"]) == 1
    code, _ = run("render", f, "-o", project / "h.png", "--highlight", "nothing.faces")
    assert code == 1
    code, out = run("render", f, "-o", project / "o.png", "--size", "200x150", "--overlay", "lid.py")
    assert code == 0 and (project / "o.png").read_bytes()[:4] == b"\x89PNG"


def test_compare_command_and_docs(project):
    f = project / "bracket.py"
    code, out = run("compare", f, project / "lid.py")
    assert code == 0
    report = json.loads(out)
    assert report["base"]["volume"] == pytest.approx(16203.097396, rel=1e-6) and not report["same"]
    code, out = run("compare", f, f)
    assert code == 0 and json.loads(out)["same"]
    code, _ = run("compare", f, "--rev", "HEAD")
    assert code == 1
    code, _ = run("compare", f)
    assert code == 1
    code, out = run("docs")
    assert code == 0 and "add_sketch_entity" in out


def test_serve_and_mcp_default_to_the_checkouts_cad_folder(tmp_path, monkeypatch):
    """No argument: cad/ in the plainsolid checkout, created by serve; an argument wins;
    outside a checkout there is no default and the command asks for one."""
    import plainsolid.cli
    import plainsolid.mcpserver
    import plainsolid.server
    assert plainsolid.cli._checkout() == Path(plainsolid.cli.__file__).resolve().parents[2]
    seen = {}
    monkeypatch.setattr(plainsolid.server, "serve", lambda root, **kw: seen.__setitem__("serve", root))
    monkeypatch.setattr(plainsolid.mcpserver, "serve", lambda root: seen.__setitem__("mcp", root))
    monkeypatch.setattr(plainsolid.cli, "_checkout", lambda: tmp_path)
    assert runner.invoke(app, ["serve"]).exit_code == 0
    assert seen["serve"] == tmp_path / "cad" and (tmp_path / "cad").is_dir()
    assert runner.invoke(app, ["mcp"]).exit_code == 0
    assert seen["mcp"] == tmp_path / "cad"
    other = tmp_path / "other"
    other.mkdir()
    assert runner.invoke(app, ["serve", str(other)]).exit_code == 0
    assert seen["serve"] == other.resolve()
    monkeypatch.setattr(plainsolid.cli, "_checkout", lambda: None)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1 and "pass one" in result.output


@pytest.fixture
def live_server(project):
    """The real server on a free port in a thread, for the commands that talk to one."""
    import socket
    import threading
    import time

    import uvicorn

    from plainsolid.server import create_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(project, serve_client=False)
    app.state.port = port
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    app.state.server = server
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    yield port, project, thread
    server.should_exit = True
    thread.join(5)


def test_open_status_and_stop_talk_to_the_running_server(live_server, tmp_path_factory, monkeypatch):
    """status reports the project; open classifies each file and, with no tab connected, raises
    the browser on a URL carrying them; stop makes the server exit."""
    import shutil
    import urllib.parse
    import webbrowser

    port, project, thread = live_server
    code, out = run("status", "--port", port)
    st = json.loads(out)
    assert code == 0 and st["running"] and st["root"] == str(project) and st["documents"] == [] and st["tabs"] == 0
    elsewhere = tmp_path_factory.mktemp("downloads")
    src = elsewhere / "node_v4.step"
    shutil.copy(project / "vendor" / "node_stub.step", src)
    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url: urls.append(url))
    code, out = run("open", project / "lid.py", src, "--port", port)
    r = json.loads(out)
    assert code == 0 and not r["started"] and r["browser"]
    assert [(f["action"], f.get("path") or f["source"]) for f in r["files"]] == [("open", "lid.py"), ("import", str(src))]
    assert urls == [f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode([("open", "lid.py"), ("import", str(src))])]
    assert not (project / "node_v4.step").exists()  # the copy is the dialog's decision, not open's
    code, out = run("open", elsewhere / "missing.step", "--port", port)
    assert code == 1
    code, out = run("stop", "--port", port)
    assert code == 0 and json.loads(out)["stopped"]
    thread.join(5)
    assert not thread.is_alive()
    code, out = run("status", "--port", port)
    assert json.loads(out) == {"running": False, "port": port}
    code, _ = run("stop", "--port", port)
    assert code == 1


def test_launcher_files(monkeypatch, tmp_path):
    """The Finder Quick Action and the desktop entry call the running executable by its full
    path and only touch STEP files."""
    import plistlib

    from plainsolid import cli
    from plainsolid.server import DEFAULT_PORT

    assert cli.DEFAULT_PORT == DEFAULT_PORT
    workflow, info = cli.quick_action(["/Applications/My Tools/plainsolid"])
    for plist in (workflow, info):
        assert plistlib.loads(plistlib.dumps(plist)) == plist
    script = workflow["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"]
    assert "*.step|*.stp|*.STEP|*.STP) '/Applications/My Tools/plainsolid' open \"$f\"" in script
    assert workflow["workflowMetaData"]["serviceApplicationBundleID"] == "com.apple.finder"
    assert info["NSServices"][0]["NSMenuItem"]["default"] == "Open in plainsolid"
    desktop, mime = cli.desktop_entry(["/opt/plainsolid/.venv/bin/plainsolid"])
    assert "Exec=/opt/plainsolid/.venv/bin/plainsolid open %F" in desktop and "MimeType=model/step;" in desktop
    assert '<glob pattern="*.stp"/>' in mime
    exe = tmp_path / "plainsolid"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr("sys.argv", [str(exe), "install-launcher"])
    assert cli._launch_command() == [str(exe)]
    monkeypatch.setattr("sys.argv", ["/x/cli.py"])
    assert cli._launch_command()[1:] == ["-m", "plainsolid.cli"]
