"""Tessellate Items into one binary buffer with face, edge and vertex ids.

Layout: uint32 header_len | JSON header (utf-8, padded to 4 bytes) | data.
The header lists sections {offset, count, dtype}, per-face triangle ranges,
per-edge and per-vertex position ranges, id -> label maps, per-item ranges
and colours, and the bounding box. OCCT triangulates each face on its own,
so a per-vertex face id is exact.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import struct
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np
from build123d import Shape
from OCP.BRep import BRep_Tool
from OCP.BRepLib import BRepLib_ToolTriangulatedShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopAbs import TopAbs_Orientation
from OCP.TopLoc import TopLoc_Location

from .evaluate import Item

MESH_VERSION = 2
SECTIONS = ("positions", "normals", "indices", "triangle_face_ids", "edge_positions", "vertex_positions",
            "face_centers", "edge_centers")


def tessellate(shape: Shape, tolerance: float = 0.05, angular: float = 0.3) -> dict[str, Any]:
    """Arrays for one shape, with face, edge and vertex ranges local to it."""
    BRepMesh_IncrementalMesh(shape.wrapped, tolerance, False, angular, True)
    positions, normals, indices, tri_face_ids, face_ranges = [], [], [], [], []
    vertex_base = 0
    for fid, face in enumerate(shape.faces()):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face.wrapped, loc)
        if tri is None:
            face_ranges.append([len(indices) // 3, 0])
            continue
        BRepLib_ToolTriangulatedShape.ComputeNormals_s(face.wrapped, tri)
        trsf = loc.Transformation()
        identity = loc.IsIdentity()
        reversed_ = face.wrapped.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        n_nodes = tri.NbNodes()
        node, normal = tri.Node, tri.Normal
        if identity:
            positions.extend(node(i).Coord() for i in range(1, n_nodes + 1))
            nrm = [normal(i).Coord() for i in range(1, n_nodes + 1)]
        else:
            positions.extend(node(i).Transformed(trsf).Coord() for i in range(1, n_nodes + 1))
            nrm = [normal(i).Transformed(trsf).Coord() for i in range(1, n_nodes + 1)]
        normals.extend(((-x, -y, -z) for x, y, z in nrm) if reversed_ else nrm)
        tri_start = len(indices) // 3
        n_tri = tri.NbTriangles()
        triangle = tri.Triangle
        base = vertex_base - 1
        if reversed_:
            for i in range(1, n_tri + 1):
                a, b, c = triangle(i).Get()
                indices.extend((base + a, base + c, base + b))
        else:
            for i in range(1, n_tri + 1):
                a, b, c = triangle(i).Get()
                indices.extend((base + a, base + b, base + c))
        tri_face_ids.extend([fid] * n_tri)
        face_ranges.append([tri_start, n_tri])
        vertex_base += n_nodes

    edge_positions, edge_ranges = [], []
    for edge in shape.edges():
        try:
            pts = edge.positions(deflection=tolerance)
        except Exception:  # noqa: BLE001 - degenerate edges
            pts = [edge.start_point(), edge.end_point()]
        start = len(edge_positions)
        edge_positions.extend((p.X, p.Y, p.Z) for p in pts)
        edge_ranges.append([start, len(pts)])
    vertex_positions = [(v.X, v.Y, v.Z) for v in shape.vertices()]
    # the reference point of each face and edge, the same one `nearest()` measures from on the
    # server (a face's centre, an edge's half-way point), so a click can name what it hit
    face_centers, edge_centers = [], []
    for face in shape.faces():
        try:
            c = face.center()
        except Exception:  # noqa: BLE001 - a face the kernel cannot measure
            c = face.bounding_box().center()
        face_centers.append((c.X, c.Y, c.Z))
    for edge in shape.edges():
        try:
            c = edge.position_at(0.5)
        except Exception:  # noqa: BLE001
            c = edge.start_point()
        edge_centers.append((c.X, c.Y, c.Z))

    pos = np.asarray(positions, np.float32).reshape(-1, 3)
    return {
        "face_centers": np.asarray(face_centers, np.float32).reshape(-1, 3),
        "edge_centers": np.asarray(edge_centers, np.float32).reshape(-1, 3),
        "positions": pos,
        "normals": np.asarray(normals, np.float32).reshape(-1, 3),
        "indices": np.asarray(indices, np.uint32),
        "triangle_face_ids": np.asarray(tri_face_ids, np.uint32),
        "edge_positions": np.asarray(edge_positions, np.float32).reshape(-1, 3),
        "vertex_positions": np.asarray(vertex_positions, np.float32).reshape(-1, 3),
        "face_ranges": face_ranges,
        "edge_ranges": edge_ranges,
        "bbox": [pos.min(axis=0).tolist(), pos.max(axis=0).tolist()] if len(pos) else [[0, 0, 0], [0, 0, 0]],
    }


# --- disk cache for imported geometry -----------------------------------------

def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / "mesh" / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".npz")


# tessellations of products kept in memory, most recently used last: an assembly's instances share
# them, and a preview or a pose change re-reads nothing from disk
_MEMO: OrderedDict[str, dict[str, Any]] = OrderedDict()
_MEMO_BYTES = 0
MEMO_LIMIT = 256 << 20


def _nbytes(m: dict[str, Any]) -> int:
    return sum(int(np.asarray(m[k]).nbytes) for k in SECTIONS)


def _remember(key: str, m: dict[str, Any]) -> None:
    global _MEMO_BYTES
    _MEMO[key] = m
    _MEMO_BYTES += _nbytes(m)
    while _MEMO_BYTES > MEMO_LIMIT and len(_MEMO) > 1:
        _, old = _MEMO.popitem(last=False)
        _MEMO_BYTES -= _nbytes(old)


def memo_size() -> tuple[int, int]:
    """How many product tessellations are held in memory, and their bytes."""
    return len(_MEMO), _MEMO_BYTES


def clear_memo() -> None:
    global _MEMO_BYTES
    _MEMO.clear()
    _MEMO_BYTES = 0


def _cached_tessellate(shape: Shape, tolerance: float, cache_key: str | None, cache_dir: Path | None) -> dict[str, Any]:
    if cache_key is None:
        return tessellate(shape, tolerance)
    m = _MEMO.get(cache_key)
    if m is not None:
        _MEMO.move_to_end(cache_key)
        return m
    path = _cache_path(cache_dir, cache_key) if cache_dir is not None else None
    if path is not None and path.exists():
        with contextlib.suppress(Exception), np.load(path, allow_pickle=False) as z:  # a bad file is a miss
            loaded = {k: z[k] for k in SECTIONS}
            loaded["face_ranges"] = z["face_ranges"].tolist()
            loaded["edge_ranges"] = z["edge_ranges"].tolist()
            loaded["bbox"] = z["bbox"].tolist()
            m = loaded
    if m is None:
        m = tessellate(shape, tolerance)
        if path is not None:
            with contextlib.suppress(OSError):  # a read-only cache directory just means no caching
                path.parent.mkdir(parents=True, exist_ok=True)
                np.savez(path, **{k: m[k] for k in SECTIONS},
                         face_ranges=np.asarray(m["face_ranges"], np.int64).reshape(-1, 2),
                         edge_ranges=np.asarray(m["edge_ranges"], np.int64).reshape(-1, 2),
                         bbox=np.asarray(m["bbox"], np.float64))
    _remember(cache_key, m)
    return m


def _placed(m: dict[str, Any], transform: Any) -> dict[str, Any]:
    """A product's tessellation moved to an instance's pose (a rigid 4x4 transform)."""
    t = np.asarray(transform, np.float64)
    if t.shape == (4, 4) and np.allclose(t, np.eye(4)):
        return m
    rot, shift = t[:3, :3], t[:3, 3]
    out = dict(m)
    for k in ("positions", "edge_positions", "vertex_positions", "face_centers", "edge_centers"):
        a = np.asarray(m[k], np.float64).reshape(-1, 3)
        out[k] = (a @ rot.T + shift).astype(np.float32)
    n = np.asarray(m["normals"], np.float64).reshape(-1, 3)
    out["normals"] = (n @ rot.T).astype(np.float32)
    pos = out["positions"]
    out["bbox"] = [pos.min(axis=0).tolist(), pos.max(axis=0).tolist()] if len(pos) else [[0, 0, 0], [0, 0, 0]]
    return out


# --- building one mesh from many items ------------------------------------------

def build(items: list[Item], cache_dir: Path | None = None, tolerance: float | None = None) -> dict[str, Any]:
    """Concatenate item meshes into one, with global face, edge and vertex ids."""
    parts = []
    shared: dict[str, dict[str, Any]] = {}  # products tessellated during this build, by key
    for it in items:
        if tolerance is not None:
            it = Item(**{**it.__dict__, "tolerance": tolerance, "cache_key": None})
        if it.local_shape is not None and it.transform is not None:
            # an instanced product: tessellate it once in its own coordinates, place every instance
            key = it.cache_key or f"{id(it.local_shape)}|{it.tolerance}"
            m = shared.get(key)
            if m is None:
                m = shared[key] = _cached_tessellate(it.local_shape, it.tolerance, it.cache_key, cache_dir)
            parts.append((it, _placed(m, it.transform)))
        else:
            parts.append((it, _cached_tessellate(it.shape, it.tolerance, it.cache_key, cache_dir)))

    arrays: dict[str, list[np.ndarray]] = {k: [] for k in SECTIONS}
    face_ranges, edge_ranges, face_labels, edge_labels, item_meta = [], [], [], [], []
    face_tags: list[list[str]] = []
    edge_tags: list[list[str]] = []
    section_faces: list[int] = []
    v_off = tri_off = e_off = 0
    f_base = ed_base = vx_base = 0
    for it, m in parts:
        n_faces, n_edges, n_vx = len(m["face_ranges"]), len(m["edge_ranges"]), len(m["vertex_positions"])
        arrays["positions"].append(m["positions"])
        arrays["normals"].append(m["normals"])
        arrays["indices"].append(m["indices"] + v_off)
        arrays["triangle_face_ids"].append(m["triangle_face_ids"] + f_base)
        arrays["edge_positions"].append(m["edge_positions"])
        arrays["vertex_positions"].append(m["vertex_positions"])
        arrays["face_centers"].append(m["face_centers"])
        arrays["edge_centers"].append(m["edge_centers"])
        face_ranges.extend([s + tri_off, c] for s, c in m["face_ranges"])
        edge_ranges.extend([s + e_off, c] for s, c in m["edge_ranges"])
        labels = it.labels()
        face_labels.extend(labels[i] if i < len(labels) else it.name for i in range(n_faces))
        elabels = it.edge_labels or []
        edge_labels.extend(elabels[i] if i < len(elabels) else it.name for i in range(n_edges))
        ftags, etags = it.face_tags or [], it.edge_tags or []
        face_tags.extend(list(ftags[i]) if i < len(ftags) else [] for i in range(n_faces))
        edge_tags.extend(list(etags[i]) if i < len(etags) else [] for i in range(n_edges))
        section_faces.extend(f_base + i for i in it.section_faces)
        item_meta.append({
            "name": it.name, "path": it.path,
            "color": list(it.color) if it.color else None,
            "faces": [f_base, n_faces], "edges": [ed_base, n_edges], "vertices": [vx_base, n_vx],
            "triangles": [tri_off, len(m["indices"]) // 3],
        })
        v_off += len(m["positions"])
        tri_off += len(m["indices"]) // 3
        e_off += len(m["edge_positions"])
        f_base += n_faces
        ed_base += n_edges
        vx_base += n_vx

    out: dict[str, Any] = {}
    for k in SECTIONS:
        dt = np.uint32 if k in ("indices", "triangle_face_ids") else np.float32
        out[k] = np.concatenate(arrays[k]) if arrays[k] else np.zeros((0, 3) if dt is np.float32 else 0, dt)
    pos = out["positions"].reshape(-1, 3)
    out.update({
        "face_ranges": face_ranges, "edge_ranges": edge_ranges,
        "face_labels": face_labels, "edge_labels": edge_labels,
        "face_tags": face_tags, "edge_tags": edge_tags,
        "items": item_meta, "section_faces": section_faces,
        "bbox": [pos.min(axis=0).tolist(), pos.max(axis=0).tolist()] if len(pos) else [[0, 0, 0], [0, 0, 0]],
    })
    return out


def encode(mesh: dict[str, Any], face_labels: list[str] | None = None,
           edge_labels: list[str] | None = None, extra: dict[str, Any] | None = None) -> bytes:
    sections = {}
    blobs = []
    offset = 0
    for name in SECTIONS:
        arr = np.ascontiguousarray(mesh.get(name, np.zeros(0, np.float32)))
        raw = arr.tobytes()
        sections[name] = {"offset": offset, "count": int(arr.size), "dtype": str(arr.dtype)}
        blobs.append(raw)
        offset += len(raw)
    face_labels = face_labels if face_labels is not None else mesh.get("face_labels")
    edge_labels = edge_labels if edge_labels is not None else mesh.get("edge_labels")
    n_faces, n_edges = len(mesh["face_ranges"]), len(mesh["edge_ranges"])
    header = {
        "version": MESH_VERSION,
        "sections": sections,
        "face_ranges": mesh["face_ranges"],
        "edge_ranges": mesh["edge_ranges"],
        "face_labels": {str(i): (face_labels[i] if face_labels and i < len(face_labels) else "")
                        for i in range(n_faces)},
        "edge_labels": {str(i): (edge_labels[i] if edge_labels and i < len(edge_labels) else "")
                        for i in range(n_edges)},
        "face_tags": {str(i): list(t) for i, t in enumerate(mesh.get("face_tags", [])) if t},
        "edge_tags": {str(i): list(t) for i, t in enumerate(mesh.get("edge_tags", [])) if t},
        "items": mesh.get("items", []),
        "section_faces": mesh.get("section_faces", []),
        "vertices": int(len(mesh.get("vertex_positions", [])) // 3) if np.ndim(mesh.get("vertex_positions", [])) == 1
        else len(mesh.get("vertex_positions", [])),
        "bbox": mesh["bbox"],
        "triangles": int(len(mesh["indices"]) // 3),
    }
    if extra:
        header.update(extra)
    hjson = json.dumps(header).encode()
    pad = (-len(hjson)) % 4
    return b"".join([struct.pack("<I", len(hjson) + pad), hjson, b" " * pad, *blobs])


def decode_header(buf: bytes) -> dict[str, Any]:
    (n,) = struct.unpack_from("<I", buf, 0)
    return json.loads(buf[4:4 + n].decode().rstrip())


def decode(buf: bytes) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    header = decode_header(buf)
    (n,) = struct.unpack_from("<I", buf, 0)
    data = memoryview(buf)[4 + n:]
    arrays = {}
    for name, sec in header["sections"].items():
        dt = np.dtype(sec["dtype"])
        arrays[name] = np.frombuffer(data, dt, count=sec["count"], offset=sec["offset"])
    return header, arrays


def empty() -> dict[str, Any]:
    z = np.zeros((0, 3), np.float32)
    return {"positions": z, "normals": z, "indices": np.zeros(0, np.uint32),
            "triangle_face_ids": np.zeros(0, np.uint32), "edge_positions": z, "vertex_positions": z,
            "face_ranges": [], "edge_ranges": [], "face_labels": [], "edge_labels": [], "items": [],
            "section_faces": [], "bbox": [[0, 0, 0], [0, 0, 0]]}
