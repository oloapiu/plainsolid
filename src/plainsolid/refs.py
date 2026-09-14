"""Selectors written as text (`body.edges.from_sketch("bottom")`) and where they
land in the geometry a client sees: the mesh ids the renderer highlights and the
measure tool addresses.

An agent names geometry the way the file does, so the same selector that goes
into a fillet or a mate can be checked in a picture first. On an assembly the
selector resolves on the instance's part in its own coordinates, as mates do."""
from __future__ import annotations

from typing import Any

import numpy as np
from build123d import Compound, Shape

from .dsl import FeatureHandle, InstanceHandle, Query
from .evaluate import Evaluation, Item
from .model import Document
from .selectors import Identity, Selector, SelectorError, resolve

KINDS = ("faces", "edges", "vertices")
MATCH_TOL = 1e-4


def parse_selector(text: str, doc: Document) -> Selector | str:
    """A selector from its source text, against the document's features. The text
    is the same expression the file would hold: `body.faces.top`,
    `lid.faces.of("plate").bottom.largest()`, `hole_cut.edges.nearest((0, 0, 4))`.
    A bare feature name is returned as a string and means everything the feature
    made (the faces and edges it owns; an instance whole)."""
    if not isinstance(text, str) or not text.strip():
        raise SelectorError("a selector is text such as body.faces.top")
    namespace: dict[str, Any] = {}
    for f in doc.features:
        handle = InstanceHandle(f) if f.kind == "instance" else FeatureHandle(f)
        namespace[f.name] = handle
        if f.variable:
            namespace[f.variable] = handle
    try:
        code = compile(text.strip(), "<selector>", "eval")
    except SyntaxError as exc:
        raise SelectorError(f"bad selector {text!r}: {exc.msg}") from None
    try:
        value = eval(code, {"__builtins__": {}}, namespace)  # no builtins: feature handles only
    except NameError as exc:
        known = ", ".join(sorted({f.name for f in doc.features})) or "none"
        raise SelectorError(f"{text!r}: {exc.name!r} is not a feature of the document (features: {known})") from None
    except SelectorError:
        raise
    except Exception as exc:  # noqa: BLE001 - anything else the expression did wrong
        raise SelectorError(f"{text!r}: {exc}") from None
    if isinstance(value, Query):
        return value.selector
    if isinstance(value, FeatureHandle):
        return value.name
    raise SelectorError(f"{text!r} is not a selector; write feature.faces..., feature.edges... or feature.vertices...")


def ref_name(sel: Selector | str) -> str:
    return sel if isinstance(sel, str) else sel.ref_name


def selector_of(ref: Any, doc: Document) -> Selector | str:
    """A Selector (or a bare feature name) from text, from a stored selector dict, or from a Selector."""
    if isinstance(ref, Selector):
        return ref
    if isinstance(ref, dict):
        if "selector" in ref and isinstance(ref["selector"], dict):
            return Selector.from_json(ref["selector"])
        if "feature" in ref and "kind" in ref:
            return Selector.from_json(ref)
        if "selector" in ref:
            return parse_selector(str(ref["selector"]), doc)
    return parse_selector(str(ref), doc)


def resolve_local(sel: Selector | str, ev: Evaluation) -> tuple[str, Shape, list[Shape]]:
    """The shapes a selector picks, in the coordinates the file uses: the body of a
    part, the part of an instance in its own frame, the imported bodies of a review
    import as the file placed them. A bare feature name picks the faces and edges
    the feature owns on a part, or a whole instance. Returns (instance name or "",
    the shape resolved against, the picks)."""
    if ev.document.kind == "drawing":
        raise SelectorError(f"a drawing has no geometry to select; select on its model {ev.document.meta.get('of') or ''}".rstrip())
    feature = sel if isinstance(sel, str) else sel.feature
    text = ref_name(sel)
    inst = ev.assembly.instances.get(feature) if ev.assembly else None
    if inst is not None:
        part = inst.part
        if isinstance(sel, str):
            return feature, part.shape, list(part.shape.faces())
        identity = None
        if part.identity is not None:
            identity = Identity(owner=dict.fromkeys(part.identity.groups, feature), tags=part.identity.tags,
                                groups=part.identity.groups)
        return feature, part.shape, resolve(sel, part.shape, identity, many=True)
    root = next((r for r in ev.instances if r.name == feature), None)
    if root is not None:
        leaves = [leaf.shape for leaf in root.leaves() if leaf.shape is not None]
        if not leaves:
            raise SelectorError(f"{text}: {feature!r} has no geometry")
        shape = leaves[0] if len(leaves) == 1 else Compound(leaves)
        if isinstance(sel, str):
            return feature, shape, list(shape.faces())
        return feature, shape, resolve(sel, shape, None, many=True)
    if ev.document.kind == "assembly":
        raise SelectorError(f"{text}: no instance {feature!r} in the assembly")
    if ev.body is None:
        raise SelectorError(f"{text}: the document has no body")
    if isinstance(sel, str):
        owned = [f for f in ev.body.faces() if ev.face_owner.get(hash(f.wrapped)) == feature]
        owned += [e for e in ev.body.edges() if ev.edge_owner.get(hash(e.wrapped)) == feature]
        if not owned:
            known = sorted({o for o in ev.face_owner.values()})
            raise SelectorError(f"{feature!r} owns no faces or edges of the body (features that do: {', '.join(known)})")
        return "", ev.body, owned
    return "", ev.body, resolve(sel, ev.body, ev.identity(), many=True)


def _sub(shape: Shape, kind: str) -> list[Shape]:
    return list(shape.faces() if kind == "faces" else shape.edges() if kind == "edges" else shape.vertices())


def _kind_of(shape: Shape) -> str:
    from build123d import Edge, Face, Vertex

    if isinstance(shape, Face):
        return "faces"
    if isinstance(shape, Edge):
        return "edges"
    if isinstance(shape, Vertex):
        return "vertices"
    return "faces"


def _center(shape: Shape, kind: str) -> np.ndarray:
    from build123d import Vertex

    if kind == "vertices":
        v = shape if isinstance(shape, Vertex) else shape.center()
        return np.array([float(v.X), float(v.Y), float(v.Z)])
    c = shape.position_at(0.5) if kind == "edges" else shape.center()
    return np.array([float(c.X), float(c.Y), float(c.Z)])


def locate(ev: Evaluation, refs: list[Any], items: list[Item]) -> dict[str, list[int]]:
    """Global mesh ids (as `mesh.build` numbers the items' faces, edges and vertices)
    of what the selectors pick. Ids map exactly while the items show the geometry
    the selectors resolved on; a sectioned item matches its pieces by centre."""
    out: dict[str, list[int]] = {k: [] for k in KINDS}
    counts = [{k: len(_sub(it.shape, k)) for k in KINDS} for it in items]
    bases: list[dict[str, int]] = []
    acc = dict.fromkeys(KINDS, 0)
    for c in counts:
        bases.append(dict(acc))
        for k in KINDS:
            acc[k] += c[k]
    poses = ev.solution.poses if ev.solution is not None else {}
    for ref in refs:
        sel = selector_of(ref, ev.document)
        inst, source, picks = resolve_local(sel, ev)
        cand = [i for i, it in enumerate(items) if it.path == inst or (inst and it.path.startswith(inst + "/"))]
        if not cand:
            raise SelectorError(f"{ref_name(sel)}: {inst or 'the body'} is not shown")
        pose = poses.get(inst) if inst else None
        by_kind: dict[str, list[Shape]] = {k: [] for k in KINDS}
        for s in picks:
            by_kind[_kind_of(s)].append(s)
        for kind, shapes in by_kind.items():
            if not shapes:
                continue
            local = _sub(source, kind)
            concat = [(i, j) for i in cand for j in range(counts[i][kind])]
            exact = len(concat) == len(local)
            if exact:
                index = {hash(s.wrapped): n for n, s in enumerate(local)}
                for s in shapes:
                    n = index.get(hash(s.wrapped))
                    if n is None:
                        exact = False
                        break
                    i, j = concat[n]
                    out[kind].append(bases[i][kind] + j)
            if not exact:
                out[kind].extend(_match_by_center(shapes, kind, cand, items, bases, pose))
    return {k: sorted(set(v)) for k, v in out.items()}


def _match_by_center(shapes: list[Shape], kind: str, cand: list[int], items: list[Item],
                     bases: list[dict[str, int]], pose) -> list[int]:
    """Pieces of the shown items whose centre lies on one of the picked shapes: what
    a section left of a face, an edge or a vertex."""
    from build123d import Vector

    found: list[int] = []
    for i in cand:
        for j, piece in enumerate(_sub(items[i].shape, kind)):
            c = _center(piece, kind)
            if pose is not None:
                c = pose.inverse().apply(c)
            point = Vector(*c)
            if any(s.distance_to(point) < MATCH_TOL for s in shapes):
                found.append(bases[i][kind] + j)
    return found


def measure_ref(ev: Evaluation, items: list[Item], ref: Any) -> dict[str, Any]:
    """A measure reference: `{"face": id}` and friends pass through; a selector (text
    or `{"selector": ...}`) must pick exactly one face, edge or vertex."""
    if isinstance(ref, dict) and "selector" not in ref:
        return ref
    ids = locate(ev, [ref], items)
    hits = [(k, i) for k in KINDS for i in ids[k]]
    if len(hits) != 1:
        text = ref_name(selector_of(ref, ev.document))
        if not hits:
            raise SelectorError(f"{text} matches nothing in the shown geometry")
        raise SelectorError(f"{text} picks {len(hits)} entities; add nearest(), largest() or smallest() to name one")
    kind, idx = hits[0]
    return {kind[:-1] if kind != "vertices" else "vertex": idx}


def highlight_ids(ev: Evaluation, items: list[Item], highlight: list[Any]) -> dict[str, list[int]]:
    """What a render highlights: integers are face ids as before; anything else is a selector."""
    out: dict[str, list[int]] = {k: [] for k in KINDS}
    refs: list[Any] = []
    for h in highlight or []:
        if isinstance(h, bool):
            continue
        if isinstance(h, int) or (isinstance(h, str) and h.strip().isdigit()):
            out["faces"].append(int(h))
        else:
            refs.append(h)
    if refs:
        found = locate(ev, refs, items)
        for k in KINDS:
            out[k].extend(found[k])
    return {k: sorted(set(v)) for k, v in out.items()}


def _fmt(v: float) -> str:
    return f"{v:.4g}" if abs(v) < 1e4 else f"{v:.0f}"


def _describe_text(info: dict[str, Any]) -> str:
    """One phrase for measure.describe()'s dictionary: `plane 424 mm²`, `circle Ø5`, `line 40 mm`."""
    t = str(info.get("type", "?"))
    if "area" in info:
        text = f"{t} {_fmt(info['area'])} mm²"
        if "diameter" in info:
            text += f", Ø{_fmt(info['diameter'])}"
        elif "normal" in info:
            n = info["normal"]
            axis = next((f"{'+' if c > 0 else '-'}{'XYZ'[i]}" for i, c in enumerate(n) if abs(abs(c) - 1) < 1e-6), None)
            text += f", normal {axis}" if axis else f", normal ({', '.join(_fmt(c) for c in n)})"
        return text
    if "length" in info:
        if "diameter" in info:
            return f"{t} Ø{_fmt(info['diameter'])}, {_fmt(info['length'])} mm long"
        return f"{t} {_fmt(info['length'])} mm"
    if "position" in info:
        return "at (" + ", ".join(_fmt(c) for c in info["position"]) + ")"
    return t


class _Occluder:
    """Ray tests against the rendered triangles: does anything sit between a point and the camera?"""

    def __init__(self, mesh: dict[str, Any]):
        pos = np.asarray(mesh["positions"], dtype=np.float64).reshape(-1, 3)
        idx = np.asarray(mesh["indices"], dtype=np.int64).reshape(-1, 3)
        self.v0 = pos[idx[:, 0]]
        self.e1 = pos[idx[:, 1]] - self.v0
        self.e2 = pos[idx[:, 2]] - self.v0
        lo, hi = np.asarray(mesh["bbox"][0], dtype=np.float64), np.asarray(mesh["bbox"][1], dtype=np.float64)
        self.eps = max(float(np.linalg.norm(hi - lo)) * 1e-4, 1e-6)

    def blocked(self, point: np.ndarray, toward: np.ndarray) -> bool:
        """Möller-Trumbore over every triangle for the ray point + t·toward, t past a hair."""
        if not len(self.v0):
            return False
        pvec = np.cross(toward, self.e2)
        det = (self.e1 * pvec).sum(axis=1)
        ok = np.abs(det) > 1e-12
        if not ok.any():
            return False
        inv = 1.0 / det[ok]
        tvec = point - self.v0[ok]
        u = (tvec * pvec[ok]).sum(axis=1) * inv
        qvec = np.cross(tvec, self.e1[ok])
        v = (toward * qvec).sum(axis=1) * inv
        t = (self.e2[ok] * qvec).sum(axis=1) * inv
        hit = (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > self.eps)
        return bool(hit.any())


def _samples(mesh: dict[str, Any], kind: str, idx: int) -> list[tuple[np.ndarray, np.ndarray | None]]:
    """Points to test on an entity (with the outward normal for a face's triangles)."""
    if kind == "faces":
        start, count = mesh["face_ranges"][idx]
        if count == 0:
            return []
        pos = np.asarray(mesh["positions"], dtype=np.float64).reshape(-1, 3)
        tris = np.asarray(mesh["indices"], dtype=np.int64).reshape(-1, 3)[start:start + count]
        pick = tris[np.linspace(0, count - 1, min(count, 12)).astype(int)]
        out = []
        for a, b, c in pick:
            n = np.cross(pos[b] - pos[a], pos[c] - pos[a])
            length = np.linalg.norm(n)
            out.append(((pos[a] + pos[b] + pos[c]) / 3, n / length if length > 1e-12 else None))
        return out
    if kind == "edges":
        start, count = mesh["edge_ranges"][idx]
        if count == 0:
            return []
        pts = np.asarray(mesh["edge_positions"], dtype=np.float64).reshape(-1, 3)[start:start + count]
        pick = np.linspace(0, count - 1, min(count, 5)).astype(int)
        return [(pts[i], None) for i in pick]
    pts = np.asarray(mesh["vertex_positions"], dtype=np.float64).reshape(-1, 3)
    return [(pts[idx], None)] if idx < len(pts) else []


def describe_highlights(ev: Evaluation, items: list[Item], mesh: dict[str, Any], ids: dict[str, list[int]],
                        view: str | None = None) -> dict[str, Any]:
    """What a render highlights, in words: per entity its kind and size, its labels, and
    whether the view shows it (a face turned away or hidden behind the body is reported
    so an agent does not mistake an invisible highlight for a selector that picked nothing)."""
    from . import measure as pmeasure

    toward = None
    if view:
        from .drawing import DIRECTIONS

        if view in DIRECTIONS:
            d = np.asarray(DIRECTIONS[view][0], dtype=np.float64)
            toward = d / np.linalg.norm(d)
    occluder = _Occluder(mesh) if toward is not None else None
    entries: list[dict[str, Any]] = []
    for kind in KINDS:
        singular = kind[:-1] if kind != "vertices" else "vertex"
        labels = mesh.get(f"{singular}_labels") or []
        tags = mesh.get(f"{singular}_tags") or []
        for idx in ids.get(kind, []):
            try:
                _, info = pmeasure.resolve(items, {singular: idx})
                text = _describe_text(info)
            except pmeasure.MeasureError:
                info, text = {}, "?"
            entry: dict[str, Any] = {"kind": singular, "id": idx, "description": text}
            if idx < len(labels) and labels[idx]:
                entry["feature"] = labels[idx]
            if idx < len(tags) and tags[idx]:
                entry["labels"] = [t.lstrip(":") for t in tags[idx]]
            if occluder is not None:
                visible = False
                for point, normal in _samples(mesh, kind, idx):
                    if normal is not None and float(normal @ toward) <= 1e-6:
                        continue  # this bit of the face turns away from the camera
                    if not occluder.blocked(point, toward):
                        visible = True
                        break
                entry["visible"] = visible
            entries.append(entry)
    lines = []
    for e in entries:
        where = f" ({e['feature']}{' ' + ' '.join(e['labels']) if e.get('labels') else ''})" if e.get("feature") else ""
        shown = "" if "visible" not in e else (", visible" if e["visible"] else f", not visible from {view}")
        lines.append(f"{e['kind']} {e['id']}: {e['description']}{where}{shown}")
    return {"entities": entries, "text": "highlighted " + "; ".join(lines) if lines else "nothing highlighted",
            "count": len(entries)}


__all__ = ["KINDS", "describe_highlights", "highlight_ids", "locate", "measure_ref", "parse_selector", "ref_name",
           "resolve_local", "selector_of"]
