"""Save failures and dependency changes must never silently lose work or show old geometry."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from build123d import Align, Box, export_step

from plainsolid import query
from plainsolid import workspace as workspace_module
from plainsolid.workspace import StaleHashError, Workspace

pytestmark = pytest.mark.unit

SOURCE = '''from plainsolid import *
meta(name="test")
depth = 2.0
s = sketch("s", on=XY)
s.rect("r", 10, 10)
body = extrude("body", s, depth)
'''


@pytest.fixture
def document(tmp_path):
    (tmp_path / "part.py").write_text(SOURCE)
    ws = Workspace(tmp_path)
    return ws, ws.open("part.py")


def test_external_write_is_detected_without_waiting_for_watcher(document):
    ws, doc = document
    original_hash = doc.hash
    external = SOURCE.replace("depth = 2.0", "depth = 3.0")
    doc.path.write_text(external)
    with pytest.raises(StaleHashError):
        ws.apply(doc, {"op": "set_parameter", "name": "depth", "value": 4}, original_hash)
    assert doc.source == doc.path.read_text() == external
    assert doc.undo == [SOURCE]
    ws.apply(doc, {"op": "set_meta", "key": "revision", "value": "B"}, doc.hash)
    assert doc.document.param("depth").value == 3
    assert doc.ensure_evaluated().body.volume == pytest.approx(300)


@pytest.mark.parametrize("action", ["edit", "undo", "redo"])
def test_failed_replace_preserves_file_memory_and_history(document, monkeypatch, action):
    ws, doc = document
    ws.apply(doc, {"op": "set_parameter", "name": "depth", "value": 3}, doc.hash)
    if action == "redo":
        ws.undo(doc)
    ev = doc.ensure_evaluated()
    before = doc.source, doc.hash, list(doc.undo), list(doc.redo)

    def fail(*_args):
        raise PermissionError("replacement refused")

    monkeypatch.setattr(workspace_module.os, "replace", fail)
    with pytest.raises(PermissionError, match="replacement refused"):
        if action == "edit":
            ws.apply(doc, {"op": "set_parameter", "name": "depth", "value": 4}, doc.hash)
        else:
            getattr(ws, action)(doc)
    assert (doc.source, doc.hash, doc.undo, doc.redo) == before
    assert doc.path.read_text() == doc.source
    assert doc.evaluation is ev
    assert not list(doc.path.parent.glob(".*.tmp"))


def test_external_write_during_edit_is_checked_again_before_replace(document, monkeypatch):
    ws, doc = document
    apply = workspace_module.edit_ops.apply
    external = SOURCE + "\n# another editor's work\n"

    def edit_and_write(*args):
        result = apply(*args)
        doc.path.write_text(external)
        return result

    monkeypatch.setattr(workspace_module.edit_ops, "apply", edit_and_write)
    with pytest.raises(StaleHashError):
        ws.apply(doc, {"op": "set_parameter", "name": "depth", "value": 4}, doc.hash)
    assert doc.source == doc.path.read_text() == external
    assert not list(doc.path.parent.glob(".*.tmp"))


def test_atomic_write_preserves_permissions_and_undo(document):
    ws, doc = document
    doc.path.chmod(0o640)
    ws.apply(doc, {"op": "set_parameter", "name": "depth", "value": 4}, doc.hash)
    assert doc.path.stat().st_mode & 0o777 == 0o640
    assert doc.path.read_text() == doc.source
    assert doc.ensure_evaluated().body.volume == pytest.approx(400)
    ws.undo(doc)
    assert doc.path.read_text() == SOURCE


def test_edit_during_evaluation_cannot_publish_old_geometry(document, monkeypatch):
    ws, doc = document
    entered, release, editing = Event(), Event(), Event()
    evaluate = workspace_module.evaluate

    def delayed(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result

    def edit():
        editing.set()
        return ws.apply(doc, {"op": "set_parameter", "name": "depth", "value": 4}, None)

    with monkeypatch.context() as m, ThreadPoolExecutor(max_workers=2) as pool:
        m.setattr(workspace_module, "evaluate", delayed)
        evaluation = pool.submit(doc.ensure_evaluated)
        try:
            assert entered.wait(10)
            update = pool.submit(edit)
            assert editing.wait(5)
        finally:
            release.set()
        evaluation.result(timeout=10)
        assert update.result(timeout=10)["changed"]
    assert doc.document.param("depth").value == 4
    assert doc.ensure_evaluated().body.volume == pytest.approx(400)
    assert doc.mesh()["revision"] == doc.tree_json()["revision"]


def test_dependency_change_refreshes_tree_mesh_queries_and_compare(document):
    ws, part = document
    root = part.path.parent
    (root / "assembly.py").write_text('from plainsolid import *\nmeta(kind="assembly")\ni = instance("i", "part.py")\n')
    (root / "reference.py").write_text(SOURCE)
    assembly = ws.open("assembly.py")
    tree = assembly.tree_json()
    mesh = assembly.mesh()
    assert ws.compare(assembly, "reference.py")[0]["same"]
    ws.apply(part, {"op": "set_parameter", "name": "depth", "value": 4}, part.hash)
    updated = assembly.tree_json()
    assert updated["hash"] == tree["hash"]
    assert updated["revision"] != tree["revision"]
    assert assembly.mesh()["revision"] != mesh["revision"]
    assert assembly.mesh()["bbox"][1][2] == pytest.approx(4)
    assert query.run(assembly.ensure_evaluated(), "bom")["rows"][0]["volume"] == pytest.approx(400)
    assert ws.compare(assembly, "reference.py")[0]["added"]["volume"] == pytest.approx(200)
    # The other side's dependencies also participate in the comparison cache.
    (root / "reference.py").write_text(SOURCE.replace("depth = 2.0", "depth = 4.0"))
    assert ws.compare(assembly, "reference.py")[0]["same"]


def test_wrapped_step_changes_reach_assembly_and_drawing(tmp_path):
    step = tmp_path / "vendor.step"
    export_step(Box(10, 10, 2, align=(Align.CENTER, Align.CENTER, Align.MIN)), step)
    (tmp_path / "part.py").write_text('from plainsolid import *\nmeta(name="part")\nbody = import_step("body", "vendor.step")\n')
    (tmp_path / "assembly.py").write_text('from plainsolid import *\nmeta(kind="assembly")\ni = instance("i", "part.py")\n')
    (tmp_path / "drawing.py").write_text('from plainsolid import *\nmeta(kind="drawing", of="assembly.py")\nfront = view("front", direction=FRONT, at=(50,50))\n')
    ws = Workspace(tmp_path)
    docs = [ws.open(name) for name in ("part.py", "assembly.py", "drawing.py")]
    revisions = [d.tree_json()["revision"] for d in docs]
    export_step(Box(10, 10, 4, align=(Align.CENTER, Align.CENTER, Align.MIN)), step)
    assert {d.id for d in ws.dependents_of(step)} == {d.id for d in docs}
    assert all(d.tree_json()["revision"] != old for d, old in zip(docs, revisions, strict=True))
    assert docs[0].ensure_evaluated().body.volume == pytest.approx(400)
    assert query.run(docs[1].ensure_evaluated(), "bom")["rows"][0]["volume"] == pytest.approx(400)
    assert docs[2].ensure_evaluated().drawing.model.shape.volume == pytest.approx(400)


def test_dependency_changed_during_evaluation_is_retried(document, monkeypatch):
    ws, part = document
    root = part.path.parent
    (root / "assembly.py").write_text('from plainsolid import *\nmeta(kind="assembly")\ni = instance("i", "part.py")\n')
    assembly = ws.open("assembly.py")
    evaluate = workspace_module.evaluate
    calls = []

    def changing(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            part.path.write_text(SOURCE.replace("depth = 2.0", "depth = 4.0"))
        return result

    monkeypatch.setattr(workspace_module, "evaluate", changing)
    ev = assembly.ensure_evaluated()
    assert len(calls) == 2
    assert query.run(ev, "bom")["rows"][0]["volume"] == pytest.approx(400)
