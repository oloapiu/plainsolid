import pytest

from plainsolid.workspace import StaleHashError, Workspace

pytestmark = pytest.mark.unit


def test_apply_undo_redo_stale_and_reload(project):
    ws = Workspace(project)
    doc = ws.open("bracket.py")
    events = []
    ws.listeners.append(lambda _id, p: events.append(p["event"]))
    h0 = doc.hash
    r = ws.apply(doc, {"op": "set_parameter", "name": "thickness", "value": 5}, h0)
    assert r["changed"] and r["hash"] != h0 and "thickness = 5" in (project / "bracket.py").read_text()
    assert doc.document.param("thickness").value == 5.0
    with pytest.raises(StaleHashError):
        ws.apply(doc, {"op": "set_parameter", "name": "thickness", "value": 6}, h0)
    assert ws.undo(doc)["changed"] and doc.hash == h0
    assert (project / "bracket.py").read_text() == doc.source
    assert ws.redo(doc)["changed"] and doc.hash == r["hash"]
    assert not ws.redo(doc)["changed"]
    (project / "bracket.py").write_text(doc.source + "\n# external\n")
    assert ws.reload_from_disk(doc) is True
    assert ws.reload_from_disk(doc) is False
    assert events == ["changed", "changed", "changed", "external"]
    assert ws.undo(doc)["changed"]  # an external change is undoable too


def test_no_op_edit_does_not_push_undo(project):
    ws = Workspace(project)
    doc = ws.open("bracket.py")
    r = ws.apply(doc, {"op": "set_parameter", "name": "thickness", "value": {"expr": "4.0"}}, None)
    assert not r["changed"] and not doc.undo


def test_open_is_idempotent_and_ids_are_stable(project):
    ws = Workspace(project)
    a = ws.open("bracket.py")
    b = ws.open(project / "bracket.py")
    assert a is b and ws.get(a.id) is a and len(ws.docs) == 1


def test_wrapper_source_sanitises_names():
    from plainsolid.parse import parse_document
    from plainsolid.workspace import wrapper_source

    src = wrapper_source("node-v3 (1).step")
    doc = parse_document(src)
    assert not doc.errors and doc.kind == "assembly" and doc.meta["name"] == "node-v3 (1)"
    assert doc.features[0].name == "node_v3__1_" and doc.features[0].args["path"] == "node-v3 (1).step"
    assert parse_document(wrapper_source("3d.step")).features[0].name == "step_3d"


def test_create_from_template(project):
    from plainsolid.workspace import template_source

    src = template_source("mount", author="Paolo")
    assert src == 'from plainsolid import *\n\nmeta(name="mount", material="al6061", revision="A", author="Paolo")\n'
    assert 'kind="assembly"' in template_source("node", kind="assembly", author="")
    ws = Workspace(project)
    doc = ws.create("parts/mount")  # .py is added, folders are made
    assert doc.path.name == "mount.py" and doc.path.parent.name == "parts"
    assert doc.document.kind == "part" and doc.document.meta["name"] == "mount" and not doc.document.errors
    assert doc.document.features == [] and doc.ensure_evaluated().body is None
    with pytest.raises(FileExistsError):
        ws.create("parts/mount.py")
    r = ws.apply(doc, {"op": "add_feature", "kind": "sketch", "name": "sketch1", "args": {"on": "XY"}}, doc.hash)
    assert r["changed"] and 'sketch1 = sketch("sketch1", on=XY)' in doc.source


def test_open_and_create_stay_inside_the_project(project, tmp_path_factory):
    """A path outside the served directory is refused, so no wrapper, sidecar or new
    document is ever written elsewhere by mistake; folders inside are created."""
    ws = Workspace(project)
    outside = tmp_path_factory.mktemp("elsewhere")
    (outside / "part.py").write_text((project / "bracket.py").read_text())
    with pytest.raises(ValueError, match="outside the project"):
        ws.open(outside / "part.py")
    with pytest.raises(ValueError, match="outside the project"):
        ws.create(f"../{outside.name}/new.py")
    assert not (outside / "new.py").exists()
    assert ws.create("parts/new.py").path == (project / "parts" / "new.py").resolve()
