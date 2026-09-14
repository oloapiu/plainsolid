"""Vendor geometry made editable: STEP fragments, single-solid STEP files opening
as parts, review imports turned into instances, part wrappers, and the assembly
following edits of its parts. Layers: unit, io, api."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plainsolid import assembly as pasm
from plainsolid import query
from plainsolid.evaluate import evaluate
from plainsolid.parse import parse_document, parse_file
from plainsolid.stepimport import ImportError_, read_step, select_node, split_fragment
from plainsolid.workspace import Workspace, identifier, step_kind, wrapper_source

HEAD = "from plainsolid import *\n"


@pytest.mark.unit
def test_fragments_and_identifiers():
    assert split_fragment("vendor/node.step#node.glands.gland_1") == ("vendor/node.step", "node.glands.gland_1")
    assert split_fragment("lid.py") == ("lid.py", None)
    assert identifier("gland_1") == "gland_1" and identifier("3D bracket (rev A)") == "p_3D_bracket_rev_A"
    assert identifier("class") == "class_" and identifier("body") == "body_" and identifier("body_", {"body_"}) == "body__2"
    assert identifier("box", {"box"}) == "box_2"
    assert wrapper_source("gland.step", "part") == 'from plainsolid import *\n\nmeta(name="gland")\n\nbody = import_step("body", "gland.step")\n'
    assert 'kind="assembly"' in wrapper_source("node.step")


@pytest.mark.unit
def test_select_node_by_path_suffix_or_name(zoo_dir):
    tree = read_step(zoo_dir / "vendor" / "node_stub.step")
    assert select_node(tree, "node.glands.gland_1").path == "node.glands.gland_1"
    assert select_node(tree, "glands.gland_2").name == "gland_2"
    assert select_node(tree, "bracket").product == "bracket"
    assert select_node(tree, "glands").children and len(select_node(tree, "glands").leaves()) == 2
    with pytest.raises(ImportError_, match="no node 'lid'"):
        select_node(tree, "lid")
    leaf = select_node(tree, "gland_1")
    assert leaf.local_shape is not None and tuple(leaf.local_shape.bounding_box().center()) == pytest.approx((0, 0, 10), abs=1e-6)
    assert tuple(leaf.shape.bounding_box().center()) == pytest.approx((-15, 10, 0), abs=1e-3)


@pytest.mark.io
def test_step_kind_and_a_part_that_is_only_an_import(zoo_dir):
    assert step_kind(zoo_dir / "vendor" / "gland_m12.step") == "part"
    assert step_kind(zoo_dir / "vendor" / "node_stub.step") == "assembly"
    src = HEAD + f'meta(name="g")\nbody = import_step("body", "{zoo_dir / "vendor" / "gland_m12.step"}")\n'
    ev = evaluate(parse_document(src, str(zoo_dir / "g.py")))
    assert ev.result("body").ok and ev.body.volume == pytest.approx(4808.5, abs=1.0)
    # a node of a multi-body file, in its own coordinates
    src = HEAD + f'meta(name="g1")\nbody = import_step("body", "{zoo_dir / "vendor" / "node_stub.step"}#node.glands.gland_1")\n'
    ev = evaluate(parse_document(src, str(zoo_dir / "g1.py")))
    assert ev.result("body").ok and tuple(ev.body.bounding_box().center()) == pytest.approx((0, 0, 10), abs=1e-6)
    src = HEAD + f'meta(name="g")\nbody = import_step("body", "{zoo_dir / "vendor" / "node_stub.step"}#glands")\n'
    ev = evaluate(parse_document(src, str(zoo_dir / "g.py")))
    assert "2 solids" in ev.result("body").error.message


@pytest.mark.io
def test_instances_of_step_nodes_are_posed_by_the_file(zoo_dir, tmp_path):
    shutil.copytree(zoo_dir / "vendor", tmp_path / "vendor")
    src = HEAD + '''meta(kind="assembly", name="a")
g1 = instance("g1", "vendor/node_stub.step#node.glands.gland_1", at=(-15, 20, 0), rotate=(90, 0, 0))
g2 = instance("g2", "vendor/node_stub.step#node.glands.gland_2", at=(15, 20, 0), rotate=(90, 0, 0))
'''
    (tmp_path / "a.py").write_text(src)
    ev = evaluate(parse_file(str(tmp_path / "a.py")))
    assert all(r.ok for r in ev.results), [r.error.message for r in ev.results if r.error]
    whole = read_step(zoo_dir / "vendor" / "node_stub.step")
    for root, leaf in zip(ev.posed, whole.leaves()[2:], strict=True):
        assert tuple(root.shape.bounding_box().center()) == pytest.approx(tuple(leaf.shape.bounding_box().center()), abs=1e-3)
        assert root.color == leaf.color and root.product == leaf.product and root.kind == "step"
    assert ev.solution.warnings == []  # nothing mated, nothing floats
    bad = HEAD + 'meta(kind="assembly")\ng = instance("g", "vendor/node_stub.step#glands")\n'
    (tmp_path / "b.py").write_text(bad)
    ev = evaluate(parse_file(str(tmp_path / "b.py")))
    assert "sub-assembly of 2 parts" in ev.result("g").error.message


@pytest.mark.api
def test_open_single_solid_step_as_a_part(project):
    ws = Workspace(project)
    doc = ws.open(project / "vendor" / "gland_m12.step")
    assert doc.document.kind == "part" and (project / "vendor" / "gland_m12.py").exists()
    assert doc.ensure_evaluated().body.volume == pytest.approx(4808.5, abs=1.0)
    files = ws.files()
    assert {"path": "vendor/gland_m12.py", "kind": "part"} in files and {"path": "node.py", "kind": "assembly"} in files
    step = next(f for f in files if f["path"] == "vendor/gland_m12.step")
    assert step == {"path": "vendor/gland_m12.step", "kind": "step", "wrapper": "vendor/gland_m12.py"}  # opening either is one document
    assert next(f for f in files if f["path"] == "vendor/panel.step") == {"path": "vendor/panel.step", "kind": "step"}
    assert not any(f["path"].endswith("gen_vendor.py") for f in files)


@pytest.mark.api
def test_edit_a_body_of_a_review_import_and_see_the_assembly_follow(project):
    ws = Workspace(project)
    rev = ws.open(project / "node_review.py")
    original = rev.source
    r = ws.apply(rev, {"op": "make_editable", "leaf": "node.glands.gland_1"}, rev.hash)
    assert r["changed"] and r["instance"] == "gland_1" and r["repointed"] == ["gland_1", "gland_2"]
    assert r["instances"] == {"node.box": "box", "node.bracket": "bracket", "node.glands.gland_1": "gland_1", "node.glands.gland_2": "gland_2"}
    part = Path(r["part"])
    assert part == project / "gland_1.py"
    assert part.read_text() == 'from plainsolid import *\n\nmeta(name="gland_1")\n\nbody = import_step("body", "vendor/node_stub.step#node.glands.gland_1")\n'
    lines = [ln for ln in rev.source.splitlines() if "instance(" in ln]
    assert lines == [
        'box = instance("box", "vendor/node_stub.step#node.box")',
        'bracket = instance("bracket", "vendor/node_stub.step#node.bracket", at=(0, -21.5, 10))',
        'gland_1 = instance("gland_1", "gland_1.py", at=(-15, 20, 0), rotate=(90, 0, 0))',
        'gland_2 = instance("gland_2", "gland_1.py", at=(15, 20, 0), rotate=(90, 0, 0))',
    ]
    assert "import_step" not in rev.source and rev.source.startswith(original.split("meta(")[0])  # the comment above survives
    ev = rev.ensure_evaluated()
    whole = read_step(project / "vendor" / "node_stub.step")
    for root, leaf in zip(ev.posed, whole.leaves(), strict=True):
        assert tuple(root.shape.bounding_box().center()) == pytest.approx(tuple(leaf.shape.bounding_box().center()), abs=1e-3)
        assert root.shape.volume == pytest.approx(leaf.shape.volume, rel=1e-6)
    assert query.counts(ev)["instances"] == 4
    # editing the part is seen by the assembly at once
    pdoc = ws.open(part)
    before = ev.posed[2].shape.volume
    ws.set_source(pdoc, pdoc.source + 's = sketch("s", on=body.faces.where(normal="+Z").largest())\ns.circle("h", 3)\ncut("h", s, through=True)\n', pdoc.hash)
    assert rev.evaluation is None
    ev2 = rev.ensure_evaluated()
    assert ev2.posed[2].shape.volume < before - 100 and ev2.posed[3].shape.volume == pytest.approx(ev2.posed[2].shape.volume, rel=1e-6)
    # the sibling already comes from the part: a second make_editable is refused
    with pytest.raises(Exception, match="already a part file"):
        ws.apply(rev, {"op": "make_editable", "instance": "gland_2"}, rev.hash)
    # undo brings the review import back, byte for byte
    while rev.undo:
        ws.undo(rev)
    assert rev.source == original


@pytest.fixture
def client(project):
    from plainsolid.server import create_app

    app = create_app(project, serve_client=False)
    with TestClient(app) as c:
        yield c


@pytest.mark.api
def test_files_close_and_make_editable_through_the_api(client, project):
    files = client.get("/api/files").json()["files"]
    assert {"path": "bracket.py", "kind": "part"} in files and {"path": "vendor/panel.step", "kind": "step"} in files
    tree = client.post("/api/documents/open", json={"path": "vendor/gland_m12.step"}).json()
    assert tree["kind"] == "part" and tree["features"][0]["kind"] == "import_step"
    docs = client.get("/api/documents").json()
    assert docs[0]["kind"] == "part"
    closed = client.post(f"/api/documents/{tree['id']}/close").json()
    assert closed["closed"] == tree["id"] and closed["documents"] == []
    assert client.get(f"/api/documents/{tree['id']}/tree").status_code == 404
    rev = client.post("/api/documents/open", json={"path": "node_review.py"}).json()
    r = client.post(f"/api/documents/{rev['id']}/edit", json={"op": "make_editable", "leaf": "node.glands.gland_2", "hash": rev["hash"]}).json()
    assert r["part"].endswith("gland_2.py") and r["repointed"] == ["gland_1", "gland_2"]
    tree2 = client.get(f"/api/documents/{rev['id']}/tree").json()
    assert [f["kind"] for f in tree2["features"]] == ["instance"] * 4
    assert tree2["evaluation"]["instances"][2]["kind"] == "part" and tree2["evaluation"]["assembly"]["dof"] == 24
    bad = client.post(f"/api/documents/{rev['id']}/edit", json={"op": "make_editable", "leaf": "node.glands", "hash": tree2["hash"]})
    assert bad.status_code == 400 and "no imported node" in bad.json()["error"]


@pytest.mark.unit
def test_load_part_keys_include_the_fragment(zoo_dir):
    path = zoo_dir / "vendor" / "node_stub.step"
    a = pasm.load_part(path, "node.glands.gland_1")
    b = pasm.load_part(path, "node.box")
    whole = pasm.load_part(path)
    assert a is not b and a.name == "gland_2" and b.name == "box" and whole.name == "node_stub"
    assert len(whole.shape.solids()) == 4 and len(a.shape.solids()) == 1
    assert pasm.load_part(path, "node.glands.gland_1") is a


@pytest.mark.api
def test_explode_maps_the_tree_the_client_shows_even_when_the_root_is_renamed(project):
    """The review tree names its root after the feature; the file names it after itself."""
    (project / "review2.py").write_text(HEAD + 'meta(kind="assembly", name="review2")\nn = import_step("n", "vendor/node_stub.step")\n')
    ws = Workspace(project)
    doc = ws.open(project / "review2.py")
    ev = doc.ensure_evaluated()
    assert [leaf.path for leaf in ev.instances[0].leaves()][2] == "n.glands.gland_1"
    r = ws.apply(doc, {"op": "make_editable", "leaf": "n.glands.gland_1"}, doc.hash)
    assert r["instance"] == "gland_1" and r["skipped"] == [] and r["instances"]["n.glands.gland_1"] == "gland_1"
    assert 'box = instance("box", "vendor/node_stub.step#node.box")' in doc.source  # fragments use the file's own names
    assert 'gland_1 = instance("gland_1", "gland_1.py", at=(-15, 20, 0), rotate=(90, 0, 0))' in doc.source
    ev2 = doc.ensure_evaluated()
    assert not [x for x in ev2.results if x.error] and len(ev2.posed) == 4


@pytest.mark.unit
def test_add_features_inserts_many_statements_in_one_pass():
    from plainsolid import edit

    src = HEAD + 'meta(kind="assembly")\nn = import_step("n", "x.step")  # keep\n'
    out = edit.add_features(src, "n", [f'p{i} = instance("p{i}", "x.step#a.b{i}")' for i in range(3)])
    assert out.endswith('n = import_step("n", "x.step")  # keep\np0 = instance("p0", "x.step#a.b0")\np1 = instance("p1", "x.step#a.b1")\np2 = instance("p2", "x.step#a.b2")\n')
    assert edit.delete_feature(out, "n", cascade=False).count("instance(") == 3


@pytest.mark.unit
def test_dependents_all_agrees_with_dependents_on_every_zoo_part(zoo_dir):
    from plainsolid import edit

    for name in ("bracket", "enclosure", "lid", "node"):
        src = (zoo_dir / f"{name}.py").read_text()
        every = edit.dependents_all(src)
        for feature, expected in every.items():
            assert edit.dependents(src, feature) == expected, (name, feature)
    src = (zoo_dir / "enclosure.py").read_text()
    assert edit.dependents_all(src)["boss"]["features"] == ["bosses", "pilot_sk", "pilot", "pilots"]


@pytest.mark.api
def test_a_model_file_cannot_be_emptied_through_the_source_endpoint(client, project):
    tree = client.post("/api/documents/open", json={"path": "bracket.py"}).json()
    r = client.put(f"/api/documents/{tree['id']}/source", json={"source": "   \n", "hash": tree["hash"]})
    assert r.status_code == 400 and "nothing" in r.json()["error"]
    assert (project / "bracket.py").read_text().startswith("from plainsolid")
    ok = client.put(f"/api/documents/{tree['id']}/source", json={"source": tree and (project / "bracket.py").read_text() + "\n# note\n", "hash": tree["hash"]})
    assert ok.status_code == 200 and ok.json()["changed"]


@pytest.mark.io
def test_make_editable_preserves_quoted_step_path(zoo_dir, tmp_path):
    from plainsolid.edit import compose_feature

    step = tmp_path / 'vendor "quoted" \'part.step'
    shutil.copyfile(zoo_dir / 'vendor' / 'gland_m12.step', step)
    source = HEAD + 'meta(kind="assembly")\n' + compose_feature('instance', 'g', {'path': step.name}) + '\n'
    path = tmp_path / 'assembly.py'
    path.write_text(source)
    ws = Workspace(tmp_path)
    doc = ws.open(path)
    result = ws.apply(doc, {'op': 'make_editable', 'instance': 'g'}, doc.hash)
    part = parse_file(result['part'])
    assert not part.errors
    assert part.features[0].args['path'] == step.name
    assert evaluate(part).body.volume > 0
    # Looking up the same product finds the generated wrapper, even with quotes.
    assert ws._part_file_for(doc, 'another', step.name, None) == Path(result['part'])
