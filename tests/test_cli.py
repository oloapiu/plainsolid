"""L6: the CLI, in-process through typer's runner plus one real subprocess."""
import json
import subprocess
import sys

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
