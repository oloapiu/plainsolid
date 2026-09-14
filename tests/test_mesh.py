import numpy as np
import pytest

from plainsolid import mesh as pmesh

pytestmark = pytest.mark.unit


def test_mesh_encoding_round_trip(bracket_eval):
    m = pmesh.tessellate(bracket_eval.body)
    buf = pmesh.encode(m, bracket_eval.face_labels(), bracket_eval.edge_labels(), {"hash": "abc"})
    header, arrays = pmesh.decode(buf)
    assert header["version"] == pmesh.MESH_VERSION and header["hash"] == "abc"
    assert header["triangles"] == len(m["indices"]) // 3
    assert np.array_equal(arrays["positions"], m["positions"].ravel())
    assert np.array_equal(arrays["indices"], m["indices"])
    assert sum(c for _, c in header["face_ranges"]) == header["triangles"]
    assert arrays["indices"].max() < len(m["positions"])
    # every triangle's face id matches the range it sits in
    fid = arrays["triangle_face_ids"]
    for i, (start, count) in enumerate(header["face_ranges"]):
        assert (fid[start:start + count] == i).all()
    # normals are unit length
    n = arrays["normals"].reshape(-1, 3)
    assert np.allclose(np.linalg.norm(n, axis=1), 1.0, atol=1e-3)


def test_mesh_carries_the_servers_entity_centres(bracket_eval):
    """The client names what it hit through nearest((x, y, z)); the point it sends must be the
    one the server measures from, so the mesh carries it (a full circle's average is its centre,
    nowhere near the curve)."""
    from plainsolid.evaluate import Item

    m = pmesh.build([Item(name="body", path="", shape=bracket_eval.body)])
    fc, ec = np.asarray(m["face_centers"]).reshape(-1, 3), np.asarray(m["edge_centers"]).reshape(-1, 3)
    faces, edges = bracket_eval.body.faces(), bracket_eval.body.edges()
    assert len(fc) == len(faces) and len(ec) == len(edges)
    for f, c in zip(faces, fc, strict=True):
        assert np.allclose(c, [f.center().X, f.center().Y, f.center().Z], atol=1e-3)
    for e, c in zip(edges, ec, strict=True):
        assert np.allclose(c, [e.center().X, e.center().Y, e.center().Z], atol=1e-3)
    ring = next(i for i, e in enumerate(edges) if e.geom_type.name == "CIRCLE" and e.is_closed)
    assert abs(np.linalg.norm(ec[ring] - [edges[ring].arc_center.X, edges[ring].arc_center.Y, edges[ring].arc_center.Z]) - edges[ring].radius) < 1e-3
    header = pmesh.decode_header(pmesh.encode(m))
    assert set(header["sections"]) >= {"face_centers", "edge_centers"}


def test_instances_share_one_tessellation_per_product(project, monkeypatch):
    """An assembly tessellates each product once, in the product's own coordinates, and places
    every instance by transforming the arrays: a pose change or a preview re-tessellates nothing,
    and the placed arrays match tessellating the posed shape directly."""
    from plainsolid.evaluate import evaluate
    from plainsolid.parse import parse_document

    src = ('from plainsolid import *\nmeta(kind="assembly")\n'
           'a = instance("a", "lid.py")\n'
           'b = instance("b", "lid.py", at=(200, 0, 0), rotate=(0, 0, 90))\n'
           'c = instance("c", "lid.py", at=(0, 150, 30), rotate=(45, 0, 0))\n')
    path = project / "grid.py"
    path.write_text(src)
    ev = evaluate(parse_document(src, str(path)))
    items = ev.items()
    assert len(items) == 3 and all(it.local_shape is not None for it in items)
    real = pmesh.tessellate
    calls: list[int] = []

    def counted(shape, tolerance=0.05, angular=0.3):
        calls.append(1)
        return real(shape, tolerance, angular)

    monkeypatch.setattr(pmesh, "tessellate", counted)
    pmesh.clear_memo()
    m = pmesh.build(items, None)
    assert len(calls) == 1
    pos, nrm = m["positions"].reshape(-1, 3), m["normals"].reshape(-1, 3)
    for i, it in enumerate(items):
        direct = real(it.shape, it.tolerance)
        meta = m["items"][i]
        t0, nt = meta["triangles"]
        idx = m["indices"][t0 * 3:(t0 + nt) * 3]
        assert np.allclose(pos[idx], direct["positions"][direct["indices"]], atol=1e-3)
        assert np.allclose(nrm[idx], direct["normals"][direct["indices"]], atol=1e-3)
        f0, nf = meta["faces"]
        assert np.allclose(m["face_centers"].reshape(-1, 3)[f0:f0 + nf], direct["face_centers"], atol=1e-3)
        assert m["face_ranges"][f0:f0 + nf] == [[s + t0, c] for s, c in direct["face_ranges"]]
    # moving an instance re-evaluates the assembly but tessellates nothing new
    moved = parse_document(src.replace("at=(200, 0, 0)", "at=(220, 5, 0)"), str(path))
    m2 = pmesh.build(evaluate(moved, cache=ev).items(), None)
    assert len(calls) == 1
    assert m2["items"][1]["triangles"] == m["items"][1]["triangles"]
    assert not np.allclose(m2["bbox"], m["bbox"])
