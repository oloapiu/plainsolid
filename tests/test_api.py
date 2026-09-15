"""L5: the HTTP and WebSocket API through FastAPI's test client."""
import pytest
from fastapi.testclient import TestClient

from plainsolid import mesh as pmesh
from plainsolid.server import create_app

pytestmark = pytest.mark.api


@pytest.fixture
def client(project):
    app = create_app(project, serve_client=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def opened(client):
    tree = client.post("/api/documents/open", json={"path": "bracket.py"}).json()
    return client, tree["id"], tree["hash"]


def test_open_and_tree(opened):
    c, did, h = opened
    tree = c.get(f"/api/documents/{did}/tree").json()
    assert tree["hash"] == h
    assert [f["name"] for f in tree["features"]] == ["profile", "body", "inner", "outer", "holes", "hole_cut", "slots", "slot_cut"]
    assert all(f["result"]["ok"] for f in tree["features"])
    assert tree["params"][0]["name"] == "thickness"
    assert c.get("/api/documents").json()[0]["id"] == did
    assert c.post("/api/documents/open", json={"path": "missing.py"}).status_code == 404
    assert c.get("/api/documents/zzzz/tree").status_code == 404


def test_edit_stale_undo_redo_and_events(opened):
    c, did, h = opened
    with c.websocket_connect(f"/api/documents/{did}/events") as ws:
        assert ws.receive_json()["event"] == "hello"
        r = c.post(f"/api/documents/{did}/edit", json={"op": "set_parameter", "name": "thickness", "value": 5, "hash": h})
        assert r.status_code == 200 and r.json()["changed"]
        event = ws.receive_json()
        assert event == {"event": "changed", "doc": did, "hash": r.json()["hash"],
                         "revision": c.get(f"/api/documents/{did}/tree").json()["revision"]}
        h2 = r.json()["hash"]
        stale = c.post(f"/api/documents/{did}/edit", json={"op": "set_parameter", "name": "thickness", "value": 6, "hash": h})
        assert stale.status_code == 409 and stale.json()["hash"] == h2
        bad = c.post(f"/api/documents/{did}/edit", json={"op": "delete_feature", "feature": "nope", "hash": h2})
        assert bad.status_code == 400 and "nope" in bad.json()["error"]
        assert c.post(f"/api/documents/{did}/undo").json()["hash"] == h
        assert ws.receive_json()["event"] == "changed"
        assert c.post(f"/api/documents/{did}/redo").json()["hash"] == h2
        assert ws.receive_json()["event"] == "changed"
        tree = c.get(f"/api/documents/{did}/tree").json()
        assert tree["params"][0]["value"] == 5.0


def test_source_put_and_error_reporting(opened):
    c, did, _ = opened
    src = c.get(f"/api/documents/{did}/source").json()
    broken = src["source"].replace('extrude("body", profile, base_depth)', 'extrude("body", profile, -1)')
    r = c.put(f"/api/documents/{did}/source", json={"source": broken, "hash": src["hash"]})
    assert r.status_code == 200
    tree = c.get(f"/api/documents/{did}/tree").json()
    body = next(f for f in tree["features"] if f["name"] == "body")
    body_line = next(i for i, line in enumerate(src["source"].splitlines(), 1) if line.startswith("body = extrude("))
    assert not body["result"]["ok"] and body["result"]["error"]["line"] == body_line
    assert c.put(f"/api/documents/{did}/source", json={"source": "x", "hash": "stale"}).status_code == 409


def test_mesh_query_render_export(opened, project):
    c, did, h = opened
    m = c.get(f"/api/documents/{did}/mesh")
    assert m.status_code == 200 and m.headers["content-type"] == "application/octet-stream"
    header, arrays = pmesh.decode(m.content)
    assert header["hash"] == h and len(header["face_ranges"]) == 20
    assert set(header["face_labels"].values()) <= {"body", "inner", "outer", "hole_cut", "slot_cut"}
    assert header["face_tags"] and any(t == [":top"] for t in header["face_tags"].values())
    assert arrays["positions"].size == arrays["normals"].size
    upto = pmesh.decode_header(c.get(f"/api/documents/{did}/mesh", params={"upto": "body"}).content)
    assert len(upto["face_ranges"]) == 8

    assert c.get(f"/api/documents/{did}/query/volume").json()["volume"] == pytest.approx(16203.097396)
    assert c.get(f"/api/documents/{did}/query/mass", params={"density": 1}).json()["mass"] == pytest.approx(16.203097)
    assert c.get(f"/api/documents/{did}/query/nope").status_code == 400
    assert c.get(f"/api/documents/{did}/query/volume", params={"upto": "body"}).json()["volume"] == pytest.approx(16960)

    r = c.post(f"/api/documents/{did}/export", json={"format": "stl", "path": "out.stl"})
    assert r.status_code == 200 and (project / "out.stl").stat().st_size > 1000
    assert c.post(f"/api/documents/{did}/export", json={"format": "obj", "path": "x"}).status_code == 400


@pytest.mark.render
def test_render_endpoint(opened):
    c, did, _ = opened
    r = c.post(f"/api/documents/{did}/render", json={"view": "front", "width": 320, "height": 240, "highlight": [1]})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert c.post(f"/api/documents/{did}/render", json={"view": "sideways"}).status_code == 400


def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


def test_open_step_makes_a_wrapper_and_assembly_tree(client, project):
    (project / "vendor").mkdir(exist_ok=True)
    import shutil

    from conftest import ZOO
    shutil.copy(ZOO / "vendor" / "node_stub.step", project / "vendor" / "node_stub.step")
    tree = client.post("/api/documents/open", json={"path": "vendor/node_stub.step"}).json()
    assert tree["kind"] == "assembly" and (project / "vendor" / "node_stub.py").exists()
    roots = tree["evaluation"]["instances"]
    assert roots[0]["name"] == "node_stub" and [c["name"] for c in roots[0]["children"]] == ["box", "bracket", "glands"]
    did = tree["id"]
    header = pmesh.decode_header(client.get(f"/api/documents/{did}/mesh").content)
    assert [it["name"] for it in header["items"]][:2] == ["node_stub.box", "node_stub.bracket"]
    assert header["items"][0]["color"][:3] == pytest.approx([0.75, 0.75, 0.78], abs=0.01)
    assert header["vertices"] == 24
    # a section through the mesh endpoint, and a measurement on its cap
    sec = pmesh.decode_header(client.get(f"/api/documents/{did}/mesh", params={"plane": "XY", "offset": 5}).content)
    assert sec["section_faces"] and sec["section"]["offset"] == 5
    cap = client.post(f"/api/documents/{did}/measure", json={"a": {"face": sec["section_faces"][0]},
                                                            "section": {"plane": "XY", "offset": 5}}).json()
    assert cap["a"]["label"] == "section"
    assert client.get(f"/api/documents/{did}/mesh", params={"plane": "QQ"}).status_code == 400
    assert client.get(f"/api/documents/{did}/query/counts").json()["instances"] == 4


def test_measure_and_views_endpoints(opened, project):
    c, did, _ = opened
    r = c.post(f"/api/documents/{did}/measure", json={"a": {"point": [0, 0, 0]}, "b": {"point": [0, 3, 4]}})
    assert r.status_code == 200 and r.json()["distance"] == pytest.approx(5.0)
    r = c.post(f"/api/documents/{did}/measure", json={"a": {"face": 0}})
    assert r.status_code == 200 and r.json()["a"]["kind"] == "face"
    assert c.post(f"/api/documents/{did}/measure", json={"a": {"face": 9999}}).status_code == 400
    assert c.get(f"/api/documents/{did}/views").json()["named"] == {}
    saved = c.put(f"/api/documents/{did}/views", json={"views": {"named": {"iso": {"camera": {"pos": [1, 2, 3]}}}}}).json()
    assert saved["named"]["iso"]["camera"]["pos"] == [1, 2, 3]
    assert (project / "bracket.views.json").exists()
    assert c.get(f"/api/documents/{did}/views").json()["named"]["iso"]["camera"]["pos"] == [1, 2, 3]


def test_new_document(client, project):
    tree = client.post("/api/documents/new", json={"path": "mount.py"}).json()
    assert tree["kind"] == "part" and tree["features"] == [] and tree["meta"]["name"] == "mount"
    assert (project / "mount.py").read_text().startswith("from plainsolid import *")
    assert client.post("/api/documents/new", json={"path": "mount.py"}).status_code == 409
    assert client.post("/api/documents/new", json={"path": "x.py", "kind": "drawing"}).status_code == 400
    asm = client.post("/api/documents/new", json={"path": "asm.py", "kind": "assembly", "name": "Node S"}).json()
    assert asm["kind"] == "assembly" and asm["meta"]["name"] == "Node S"
    assert {d["name"] for d in client.get("/api/documents").json()} >= {"mount", "Node S"}


def test_preview_mesh_shows_a_candidate_feature_without_writing(opened):
    c, did, h = opened
    op = {"op": "add_feature", "kind": "extrude", "name": "boss", "args": {"sketch": "holes", "depth": 10}, "after": "hole_cut"}
    r = c.post(f"/api/documents/{did}/preview-mesh", json={"op": op})
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    header = _header(r.content)
    assert header["ok"] is True and header["faces"] > 0 and header["hash"] == h
    assert c.get(f"/api/documents/{did}/tree").json()["hash"] == h  # nothing written
    bad = {"op": "add_feature", "kind": "fillet", "name": "f", "args": {"edges": {"expr": "body.edges.nowhere"}, "radius": 1}}
    r = c.post(f"/api/documents/{did}/preview-mesh", json={"op": bad})
    assert r.status_code == 200 and _header(r.content)["ok"] is False and "nowhere" in _header(r.content)["error"]
    assert c.post(f"/api/documents/{did}/preview-mesh", json={"op": {"op": "nonsense"}}).status_code == 422


def _header(data: bytes) -> dict:
    import json
    import struct

    (n,) = struct.unpack_from("<I", data, 0)
    return json.loads(data[4:4 + n].decode("utf-8").rstrip("\0 "))


def test_selectors_in_measure_render_and_compare(opened, project):
    c, did, _ = opened
    m = c.post(f"/api/documents/{did}/measure", json={"a": "body.faces.top", "b": "body.faces.bottom"})
    assert m.status_code == 200 and m.json()["distance"] == pytest.approx(40.0)
    m = c.post(f"/api/documents/{did}/measure", json={"a": {"selector": 'hole_cut.faces.from_sketch("hole1")'}})
    assert m.status_code == 200 and m.json()["a"]["diameter"] == pytest.approx(5.0)
    bad = c.post(f"/api/documents/{did}/measure", json={"a": "hole_cut"})
    assert bad.status_code == 400 and "picks" in bad.json()["error"]
    bad = c.post(f"/api/documents/{did}/measure", json={"a": "nothing.faces"})
    assert bad.status_code == 400 and "not a feature" in bad.json()["error"]
    r = c.post(f"/api/documents/{did}/render", json={"view": "top", "width": 200, "height": 150,
                                                     "highlight": ["hole_cut", "body.faces.top", 3]})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    bad = c.post(f"/api/documents/{did}/render", json={"highlight": ["body.faces[0]"]})
    assert bad.status_code == 400 and "indexing" in bad.json()["error"]
    report = c.post(f"/api/documents/{did}/compare", json={"other": "lid.py"})
    assert report.status_code == 200 and not report.json()["same"] and report.json()["added"]["count"] >= 1
    bad = c.post(f"/api/documents/{did}/compare", json={"other": "missing.py"})
    assert bad.status_code == 400 and "no such file" in bad.json()["error"]
    bad = c.post(f"/api/documents/{did}/compare", json={"rev": "HEAD"})
    assert bad.status_code == 400 and "git" in bad.json()["error"]
    overlay = c.post(f"/api/documents/{did}/render", json={"overlay": "lid.py", "width": 200, "height": 150})
    assert overlay.status_code == 200 and overlay.content[:4] == b"\x89PNG"
    mesh = c.get(f"/api/documents/{did}/mesh", params={"overlay": "lid.py"})
    assert mesh.status_code == 200
    header = _header(mesh.content)
    assert [it["name"] for it in header["items"]] == ["common", "added", "removed"]
    assert header["compare"]["base"]["volume"] == pytest.approx(16203.097396, rel=1e-6)
    assert all(it["color"] for it in header["items"])


def test_locate_endpoint(opened):
    """The browser lights what a reference in the panel picks: selectors, feature names, ids."""
    c, did, _ = opened
    r = c.post(f"/api/documents/{did}/locate", json={"refs": ["body.faces.top", "inner"]})
    assert r.status_code == 200
    ids = r.json()
    assert set(ids) == {"faces", "edges", "vertices"} and len(ids["faces"]) >= 2
    faces_only = c.post(f"/api/documents/{did}/locate", json={"refs": ["body.faces.top"]}).json()
    assert faces_only["faces"] and not faces_only["edges"]
    # the chamfer's edge no longer exists on the finished body: the browser then lights the chamfer itself
    gone = c.post(f"/api/documents/{did}/locate", json={"refs": ['body.edges.from_sketch("bottom").from_sketch("outer_wall")']})
    assert gone.status_code == 200 and not gone.json()["edges"] and "matches nothing" in gone.json()["error"]
    assert c.post(f"/api/documents/{did}/locate", json={"refs": ["outer"]}).json()["faces"]
    bad = c.post(f"/api/documents/{did}/locate", json={"refs": ["nothing.faces"]})
    assert bad.status_code == 200 and not bad.json()["faces"] and "not a feature" in bad.json()["error"]


def test_kernel_work_is_serialized_across_documents(client, monkeypatch):
    """OCCT shapes are shared between documents through the part cache, so the server runs all
    kernel work on one worker: two documents evaluating at once never overlap."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from plainsolid import workspace as workspace_module

    ids = [client.post("/api/documents/open", json={"path": p}).json()["id"] for p in ("bracket.py", "lid.py")]
    ws = client.app.state.workspace
    for i in ids:
        ws.get(i).evaluation = None
    depth, peak, lock = 0, 0, threading.Lock()
    real = workspace_module.evaluate

    def counted(*args, **kwargs):
        nonlocal depth, peak
        with lock:
            depth += 1
            peak = max(peak, depth)
        try:
            time.sleep(0.05)
            return real(*args, **kwargs)
        finally:
            with lock:
                depth -= 1

    monkeypatch.setattr(workspace_module, "evaluate", counted)
    with ThreadPoolExecutor(2) as pool:
        codes = list(pool.map(lambda i: client.get(f"/api/documents/{i}/tree").status_code, ids))
    assert codes == [200, 200]
    assert peak == 1


def test_open_a_sub_assembly_of_a_step_file(client):
    tree = client.post("/api/documents/open", json={"path": "vendor/node_stub.step#node.glands"}).json()
    assert tree["kind"] == "assembly" and [r["node"] for r in tree["evaluation"]["instances"]] == ["node.glands"]
    docs = client.get("/api/documents").json()
    assert [d["name"] for d in docs] == ["glands"] and docs[0]["path"].endswith("vendor/node_stub.glands.py")
    r = client.post("/api/documents/open", json={"path": "vendor/node_stub.step#lid"})
    assert r.status_code == 400 and "no node 'lid'" in r.text
