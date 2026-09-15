"""Evaluate a Document into geometry, feature by feature.

A failed feature is recorded and skipped; downstream features continue on
the previous body. Parts produce one body; assemblies produce instance
trees. Both expose the same list of Items, which the mesh, section and
measurement code consume in one consistent face/edge/vertex numbering.

Identity map: every face and edge of the body carries the feature that
created it (owner) and semantic tags. An extrusion's end faces are ":top"
and ":bottom" (labels start with a colon so they never collide with a
sketch entity name) and its side faces carry the sketch entity they came from
("rect1.top", "hole1"); a revolve's faces carry their profile entity and a
partial revolve's caps are "start" and "end"; fillet and chamfer faces take
the tags of the edge they replaced; shell walls add "inner"; pattern and
mirror copies repeat the tags of their source. A face that a later feature
splits or trims keeps its owner and tags (matched by surface), and an
edge's tags are the union of its faces' tags. Selectors such as
`body.faces.top` or `boss.edges.from_sketch("rect1.top")` resolve through
this map.

Caching: an evaluation records a signature per feature (a hash chained
over every feature before it) and a snapshot of the state after it. A
later evaluation with `cache=` restores the longest unchanged prefix.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from build123d import (
    Axis,
    Compound,
    Edge,
    Face,
    GeomType,
    Location,
    Part,
    Plane,
    Shape,
    Sketch,
    Vector,
)
from build123d import chamfer as b3d_chamfer
from build123d import extrude as b3d_extrude
from build123d import fillet as b3d_fillet
from build123d import mirror as b3d_mirror
from build123d import offset as b3d_offset
from build123d import revolve as b3d_revolve
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.TopoDS import TopoDS_Compound

from . import assembly as pasm
from . import drawing as pdrawing
from .model import Document, DocumentError, Feature
from .offset import compute_offsets, references_offsets
from .parse import DRAWING_KINDS, MATE_KINDS, TOOL_KINDS
from .projection import ProjectionError, project
from .selectors import Identity, Selector, SelectorError, resolve
from .sketchgeom import (
    STANDARD_PLANES,
    SketchError,
    finish_plane,
    open_chains,
    profile_edges,
    sketch_faces,
)
from .solver.build import Projected, SketchSolution, solve_sketch
from .solver.system import SketchError as SolverError
from .stepimport import (
    ImportError_,
    Instance,
    file_key,
    import_step_tree,
    relocated,
    select_node,
    single_solid,
    split_fragment,
)

DEFAULT_TOLERANCE = 0.05
AXIS_DIRS = {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1), "-X": (-1, 0, 0), "-Y": (0, -1, 0), "-Z": (0, 0, -1)}
TOL = 1e-4


@dataclass
class FeatureResult:
    name: str
    kind: str
    ok: bool = True
    skipped: bool = False
    error: DocumentError | None = None
    warnings: list[str] = field(default_factory=list)
    shape: Any = None
    faces_created: int = 0
    seconds: float = 0.0
    sketch: SketchSolution | None = None
    plane: dict[str, Any] | None = None  # resolved plane of a sketch or plane feature

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "ok": self.ok, "skipped": self.skipped,
            "error": self.error.to_json() if self.error else None,
            "warnings": list(self.warnings), "faces_created": self.faces_created,
            "seconds": round(self.seconds, 4),
            "sketch": self.sketch.to_json() if self.sketch else None,
            "plane": self.plane,
        }


@dataclass
class Item:
    """One shape to display and measure: the part body, or a leaf instance."""

    name: str
    path: str  # instance path, "" for a part body
    shape: Shape
    color: tuple[float, float, float, float] | None = None
    face_labels: list[str] | None = None  # per face; None means `name` for every face
    tolerance: float = DEFAULT_TOLERANCE
    cache_key: str | None = None  # stable key for the mesh cache, None to skip caching
    section_faces: list[int] = field(default_factory=list)  # local face ids created by a section cut
    edge_labels: list[str] | None = None  # per edge; None means `name` for every edge
    face_tags: list[tuple[str, ...]] | None = None  # semantic tags per face (parts only)
    edge_tags: list[tuple[str, ...]] | None = None
    # an instanced product: its own geometry, tessellated once per product and placed by the
    # 4x4 rigid `transform` (`shape` is the same geometry already placed, for measuring)
    local_shape: Shape | None = None
    transform: Any = None

    def labels(self) -> list[str]:
        if self.face_labels is not None:
            return self.face_labels
        return [self.name] * len(self.shape.faces())


@dataclass
class Tool:
    """What a body feature added or removed: what patterns and mirrors repeat."""

    shape: Part
    op: str  # add | cut
    tags: list[tuple[str, ...]]  # per face of shape, in faces() order


@dataclass
class SketchGeom:
    """A solved sketch as geometry: its profile faces on the plane, and the
    labelled 2D profile edges the identity map matches side faces against."""

    faces: Sketch | None
    plane: Plane
    profile: list[tuple[Edge, str]]
    coords: dict[str, dict[str, Any]]
    projected: dict[str, Projected]


@dataclass
class _Snapshot:
    body: Part | None
    planes: dict[str, Plane]
    sketches: dict[str, SketchGeom]
    tools: dict[str, Tool]
    face_owner: dict[int, str]
    edge_owner: dict[int, str]
    face_tags: dict[int, tuple[str, ...]]
    edge_tags: dict[int, tuple[str, ...]]
    instances: list[Instance]
    import_tolerances: dict[str, float]
    import_keys: dict[str, str]
    suppressed: set[str]
    assembly: pasm.AssemblyState
    dependencies: set[str]
    drawing: pdrawing.DrawingState


@dataclass
class Evaluation:
    document: Document
    results: list[FeatureResult] = field(default_factory=list)
    body: Part | None = None
    instances: list[Instance] = field(default_factory=list)  # assembly roots
    planes: dict[str, Plane] = field(default_factory=dict)  # plane features by name
    sketches: dict[str, SketchGeom] = field(default_factory=dict)
    tools: dict[str, Tool] = field(default_factory=dict)
    face_owner: dict[int, str] = field(default_factory=dict)
    edge_owner: dict[int, str] = field(default_factory=dict)
    face_tags: dict[int, tuple[str, ...]] = field(default_factory=dict)
    edge_tags: dict[int, tuple[str, ...]] = field(default_factory=dict)
    suppressed: set[str] = field(default_factory=set)
    import_tolerances: dict[str, float] = field(default_factory=dict)  # instance root name -> tolerance
    import_keys: dict[str, str] = field(default_factory=dict)  # instance root name -> file key
    signatures: list[str] = field(default_factory=list)
    snapshots: list[_Snapshot] = field(default_factory=list)
    cached: int = 0  # features restored from the cache instead of evaluated
    seconds: float = 0.0
    assembly: pasm.AssemblyState = field(default_factory=pasm.AssemblyState)  # instance features and mates
    posed: list[Instance] = field(default_factory=list)  # instance features at their solved poses
    solution: pasm.AssemblySolution | None = None
    dependencies: set[str] = field(default_factory=set)  # files instanced by this document, or drawn
    drawing: pdrawing.DrawingState = field(default_factory=pdrawing.DrawingState)  # drawings: the model, views, dimensions, notes
    scene_cache: dict[str, Any] | None = None

    def result(self, name: str) -> FeatureResult | None:
        for r in self.results:
            if r.name == name:
                return r
        return None

    @property
    def errors(self) -> list[DocumentError]:
        return [r.error for r in self.results if r.error]

    @property
    def roots(self) -> list[Instance]:
        """Every instance tree: review imports, then posed instance features."""
        return [*self.instances, *self.posed]

    @property
    def has_geometry(self) -> bool:
        return self.body is not None or any(leaf.shape is not None for root in self.roots for leaf in root.leaves())

    def identity(self) -> Identity:
        return Identity(owner={**self.face_owner, **self.edge_owner}, tags={**self.face_tags, **self.edge_tags})

    def face_labels(self) -> list[str]:
        if self.body is None:
            return []
        return [self.face_owner.get(_key(f), "") for f in self.body.faces()]

    def edge_labels(self) -> list[str]:
        if self.body is None:
            return []
        return [self.edge_owner.get(_key(e), "") for e in self.body.edges()]

    def face_tag_list(self) -> list[tuple[str, ...]]:
        if self.body is None:
            return []
        return [self.face_tags.get(_key(f), ()) for f in self.body.faces()]

    def edge_tag_list(self) -> list[tuple[str, ...]]:
        if self.body is None:
            return []
        return [self.edge_tags.get(_key(e), ()) for e in self.body.edges()]

    def items(self) -> list[Item]:
        if self.document.kind == "assembly":
            out: list[Item] = []
            for root in self.instances:
                tol = self.import_tolerances.get(root.name, DEFAULT_TOLERANCE)
                fkey = self.import_keys.get(root.name, "")
                for leaf in root.leaves():
                    if leaf.shape is None:
                        continue
                    out.append(Item(name=leaf.path, path=leaf.path, shape=leaf.shape, color=leaf.color,
                                    tolerance=tol, cache_key=f"{fkey}|{leaf.path}|{tol}" if fkey else None))
            if self.posed and self.solution is not None:
                out.extend(pasm.items(self.assembly, self.solution, self.posed))
            return out
        if self.body is None:
            return []
        name = self.document.meta.get("name") or "body"
        return [Item(name=str(name), path="", shape=self.body, face_labels=self.face_labels(),
                     edge_labels=self.edge_labels(), face_tags=self.face_tag_list(), edge_tags=self.edge_tag_list())]

    def instances_json(self) -> list[dict[str, Any]]:
        return [root.to_json() for root in self.roots]

    def result_json(self, name: str) -> dict[str, Any] | None:
        """A feature's result, with what the assembly solve found out about a mate."""
        r = self.result(name)
        if r is None:
            return None
        out = r.to_json()
        if self.solution is not None and r.kind in MATE_KINDS:
            extra = []
            if name in self.solution.conflicting:
                extra.append("not satisfied: it conflicts with other mates")
            if name in self.solution.redundant:
                extra.append("redundant: the other mates already fix this")
            out["warnings"] = [*out["warnings"], *extra]
        return out

    def scene(self) -> dict[str, Any] | None:
        """A drawing's sheet as one 2D scene (see drawing.layout), computed once."""
        if self.document.kind != "drawing":
            return None
        if self.scene_cache is None:
            self.scene_cache = pdrawing.layout(self)
        return self.scene_cache

    def to_json(self) -> dict[str, Any]:
        return {
            "results": [self.result_json(r.name) for r in self.results],
            "has_body": self.has_geometry,
            "kind": self.document.kind,
            "instances": self.instances_json(),
            "assembly": self.solution.to_json() if self.solution is not None else None,
            "drawing": self.scene(),
            "seconds": round(self.seconds, 4),
            "cached": self.cached,
        }


def _key(shape: Shape) -> int:
    return hash(shape.wrapped)


def _as_part(shape: Shape) -> Part:
    """A Part (a compound) around any shape; fuse() may hand back a bare Solid."""
    if isinstance(shape, Part):
        return shape
    if isinstance(shape.wrapped, TopoDS_Compound):
        return Part(shape.wrapped)
    return Part(Compound([shape]).wrapped)


# ---------------------------------------------------------------------------
# the loop, with the prefix cache


def _signature(prev: str, feature: Feature, doc: Document) -> str:
    from .dependencies import file_dependencies
    payload: dict[str, Any] = {
        "kind": feature.kind, "name": feature.name, "args": feature.args, "suppressed": feature.suppressed,
        "entities": [(e.kind, e.name, e.args, e.construction) for e in feature.entities],
        "constraints": [(c.kind, c.name, c.refs, c.value, c.options) for c in feature.constraints],
    }
    if feature.kind in ("import_step", "instance"):
        path = _resolve_path(doc, split_fragment(feature.args.get("path", ""))[0])
        payload["file"] = file_dependencies([path])
    return hashlib.sha1((prev + json.dumps(payload, sort_keys=True, default=str)).encode()).hexdigest()


def _snapshot(ev: Evaluation) -> _Snapshot:
    return _Snapshot(ev.body, dict(ev.planes), dict(ev.sketches), dict(ev.tools), dict(ev.face_owner),
                     dict(ev.edge_owner), dict(ev.face_tags), dict(ev.edge_tags), list(ev.instances),
                     dict(ev.import_tolerances), dict(ev.import_keys), set(ev.suppressed), ev.assembly.copy(),
                     set(ev.dependencies), ev.drawing.copy())


def _restore(ev: Evaluation, s: _Snapshot) -> None:
    ev.body = s.body
    ev.planes, ev.sketches, ev.tools = dict(s.planes), dict(s.sketches), dict(s.tools)
    ev.face_owner, ev.edge_owner = dict(s.face_owner), dict(s.edge_owner)
    ev.face_tags, ev.edge_tags = dict(s.face_tags), dict(s.edge_tags)
    ev.instances = list(s.instances)
    ev.import_tolerances, ev.import_keys = dict(s.import_tolerances), dict(s.import_keys)
    ev.suppressed = set(s.suppressed)
    ev.assembly = s.assembly.copy()
    ev.dependencies = set(s.dependencies)
    ev.drawing = s.drawing.copy()


def evaluate(doc: Document, upto: str | None = None, cache: Evaluation | None = None) -> Evaluation:
    """Evaluate features in order. `upto` stops after the named feature (rollback).
    `cache` is a previous evaluation of the same file; features whose inputs and
    predecessors are unchanged are restored from it instead of re-evaluated."""
    t0 = time.perf_counter()
    ev = Evaluation(document=doc)
    prev = f"{doc.kind}|{doc.path}"
    if doc.kind == "drawing":
        prev += "|" + pdrawing.model_signature(doc) + "|" + json.dumps({k: doc.meta.get(k) for k in ("of", "sheet", "scale")}, default=str)
        _load_drawing_model(ev)
    for i, feature in enumerate(doc.features):
        sig = _signature(prev, feature, doc)
        prev = sig
        if (cache is not None and i < len(cache.signatures) and cache.signatures[i] == sig
                and i < len(cache.snapshots) and i < len(cache.results)):
            snap = cache.snapshots[i]
            ev.results.append(cache.results[i])
            _restore(ev, snap)
            ev.cached += 1
        else:
            t = time.perf_counter()
            r = FeatureResult(feature.name, feature.kind)
            ev.results.append(r)
            if feature.suppressed:
                r.skipped = True
                ev.suppressed.add(feature.name)
            else:
                try:
                    _evaluate_one(feature, ev, r)
                except Exception as exc:  # noqa: BLE001 - every kernel failure becomes a feature error
                    line = feature.span[0] if feature.span else None
                    r.ok = False
                    prefix = f"{feature.kind} {feature.name!r}: "
                    msg = str(exc)
                    r.error = DocumentError(msg if msg.startswith(prefix) else prefix + msg, line)
            r.seconds = time.perf_counter() - t
            snap = _snapshot(ev)
        ev.signatures.append(sig)
        ev.snapshots.append(snap)
        if upto is not None and feature.name == upto:
            break
    if doc.kind == "assembly":
        _finish_assembly(ev)
    ev.seconds = time.perf_counter() - t0
    return ev


def _load_drawing_model(ev: Evaluation) -> None:
    """The part or assembly a drawing shows; a failure is reported by every view."""
    try:
        model = pdrawing.load_model(pdrawing.model_path(ev.document))
    except pdrawing.DrawingError as exc:
        ev.drawing.error = str(exc)
        return
    ev.drawing.model = model
    ev.dependencies.update(model.dependencies)


def _resolve_path(doc: Document, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    base = Path(doc.path).parent if doc.path else Path.cwd()
    return (base / p).resolve()


def _evaluate_one(feature: Feature, ev: Evaluation, r: FeatureResult) -> None:
    kind = feature.kind
    if kind in DRAWING_KINDS:
        if ev.document.kind != "drawing":
            raise ValueError('views, dimensions and notes belong in a drawing document: meta(kind="drawing", of="part.py")')
        _evaluate_drawing(feature, ev, r)
        return
    if ev.document.kind == "drawing":
        raise ValueError("only views, dimensions and notes belong in a drawing; the model lives in its own file")

    if kind == "plane":
        plane = evaluate_plane(feature, ev)
        ev.planes[feature.name] = plane
        r.plane = plane_json(plane, ev.body)
        r.shape = plane
        return

    if kind == "sketch":
        _evaluate_sketch(feature, ev, r)
        return

    if kind == "import_step":
        _evaluate_import(feature, ev, r)
        return

    if kind == "instance" or kind in MATE_KINDS:
        if ev.document.kind != "assembly":
            raise ValueError("instances and mates belong in an assembly document: meta(kind=\"assembly\")")
        if kind == "instance":
            _evaluate_instance(feature, ev, r)
        else:
            _evaluate_mate(feature, ev, r)
        return

    if ev.document.kind == "assembly":
        raise ValueError("body features are not allowed in an assembly document")

    if kind in ("extrude", "cut"):
        sg = _sketch_geom(feature, ev)
        op = "cut" if kind == "cut" else feature.args.get("op", "add")
        tool, tags = _extrude_tool(feature, ev, sg, op)
        _merge_body(feature, ev, r, [tool], op, tags)
        ev.tools[feature.name] = Tool(tool, op, tags)
        return

    if kind == "revolve":
        sg = _sketch_geom(feature, ev)
        op = feature.args.get("op", "add")
        tool, tags = _revolve_tool(feature, ev, sg)
        _merge_body(feature, ev, r, [tool], op, tags)
        ev.tools[feature.name] = Tool(tool, op, tags)
        return

    if kind in ("fillet", "chamfer"):
        _fillet_or_chamfer(feature, ev, r)
        return

    if kind == "shell":
        _shell(feature, ev, r)
        return

    if kind in ("linear_pattern", "circular_pattern", "mirror"):
        _repeat(feature, ev, r)
        return

    raise ValueError(f"unknown feature kind {kind!r}")


# ---------------------------------------------------------------------------
# sketches, planes, imports


def _evaluate_sketch(feature: Feature, ev: Evaluation, r: FeatureResult) -> None:
    plane = sketch_plane(feature, ev)
    r.plane = plane_json(plane, ev.body)
    solution = solve_feature_sketch(feature, ev.body, plane=plane, identity=ev.identity())
    r.sketch = solution
    if solution.conflicting:
        r.warnings.append("conflicting constraints: " + ", ".join(solution.conflicting))
    if solution.redundant:
        r.warnings.append("redundant constraints: " + ", ".join(solution.redundant))
    try:
        faces = sketch_faces(feature, solution.coords, solution.projected, plane)
    except SketchError as exc:
        if "no closed profile" not in str(exc):
            raise ValueError(str(exc)) from None
        # an empty or construction-only sketch is fine on its own; a feature that
        # needs a profile reports the problem instead
        r.warnings.append("no closed profile")
        faces = None
    n_open = open_chains(feature, solution.coords, solution.projected)
    if n_open:
        r.warnings.append(f"{n_open} open line chain(s) ignored")
    ev.sketches[feature.name] = SketchGeom(faces, plane, profile_edges(feature, solution.coords, solution.projected),
                                           solution.coords, solution.projected)
    r.shape = faces


def _evaluate_import(feature: Feature, ev: Evaluation, r: FeatureResult) -> None:
    file, fragment = split_fragment(feature.args["path"])
    path = _resolve_path(ev.document, file)
    try:
        tree = import_step_tree(path)
        if fragment:
            tree = relocated(select_node(tree, fragment))  # one node, in its own coordinates
    except ImportError_ as exc:
        raise ValueError(str(exc)) from None
    tol = feature.args.get("tolerance") or _import_tolerance(tree)
    if ev.document.kind == "assembly":
        tree = _document_tree(tree, feature.name, str(path))
        ev.instances.append(tree)
        ev.import_tolerances[tree.name] = float(tol)
        k = file_key(path)
        ev.import_keys[tree.name] = f"{k[0]}:{k[1]}:{k[2]}" + (f"#{fragment}" if fragment else "")
        r.faces_created = sum(len(leaf.shape.faces()) for leaf in tree.leaves() if leaf.shape is not None)
        r.shape = tree
        return
    try:
        solid = single_solid(tree)
    except ImportError_ as exc:
        raise ValueError(str(exc)) from None
    _merge_body(feature, ev, r, [_as_part(solid)], "add", None)


def _evaluate_instance(feature: Feature, ev: Evaluation, r: FeatureResult) -> None:
    a = feature.args
    file, fragment = split_fragment(a["path"])
    path = _resolve_path(ev.document, file)
    if ev.document.path and path == Path(ev.document.path).resolve():
        raise ValueError("an assembly cannot instance itself")
    try:
        part = pasm.load_part(path, fragment)
    except pasm.AssemblyError as exc:
        raise ValueError(str(exc)) from None
    ev.dependencies.add(str(path))
    try:
        pose = pasm.Pose.from_args(a.get("at") or (0.0, 0.0, 0.0), a.get("rotate") or (0.0, 0.0, 0.0))
        color = pasm.parse_color(a.get("color"))
        inst = pasm.InstanceState(feature.name, part, pose, color, a.get("material"), a.get("density"), a.get("tolerance"))
        inst.density_value()  # a wrong material name is an error on this line
    except pasm.AssemblyError as exc:
        raise ValueError(str(exc)) from None
    ev.assembly.instances[feature.name] = inst
    ev.assembly.order.append(feature.name)
    r.warnings.extend(f"{part.path.name}: {w}" for w in part.warnings)
    r.faces_created = len(part.shape.faces())
    r.shape = part.shape


def _mate_reference(ref: Any, ev: Evaluation, what: str) -> tuple[pasm.Ref, str]:
    if not isinstance(ref, dict) or "selector" not in ref:
        raise ValueError(f"{what}: bad reference {ref!r}")
    sel = Selector.from_json(ref["selector"])
    inst = ev.assembly.instances.get(sel.feature)
    if inst is None:
        raise ValueError(f"{what}: instance {sel.feature!r} is missing, failed, suppressed, or comes later in the file")
    try:
        return pasm.resolve_reference(sel, inst.part, what), sel.feature
    except pasm.AssemblyError as exc:
        raise ValueError(str(exc)) from None


def _evaluate_mate(feature: Feature, ev: Evaluation, r: FeatureResult) -> None:
    a = feature.args
    what = f"{feature.kind} {feature.name!r}"
    if feature.kind == "fixed":
        name = str(a["instance"])
        inst = ev.assembly.instances.get(name)
        if inst is None:
            raise ValueError(f"{what}: instance {name!r} is missing, failed, suppressed, or comes later in the file")
        ev.assembly.instances[name] = dataclasses.replace(inst, fixed=True)
        return
    ref_a, inst_a = _mate_reference(a["a"], ev, what)
    ref_b, inst_b = _mate_reference(a["b"], ev, what)
    if inst_a == inst_b:
        raise ValueError(f"{what}: both references are on {inst_a!r}; a mate relates two instances")
    try:
        rows = pasm.mate_rows(feature.kind, ref_a, ref_b, a.get("value"), bool(a.get("flip")), what)
    except pasm.AssemblyError as exc:
        raise ValueError(str(exc)) from None
    ev.assembly.mates.append(pasm.MateState(feature.name, feature.kind, inst_a, inst_b, ref_a, ref_b, rows,
                                            a.get("value"), bool(a.get("flip"))))


def _evaluate_drawing(feature: Feature, ev: Evaluation, r: FeatureResult) -> None:
    st = ev.drawing
    if st.model is None:
        raise ValueError(f"the model could not be loaded: {st.error}")
    try:
        if feature.kind == "view":
            v = pdrawing.build_view(st.model, feature.name, feature.args, pdrawing.sheet_scale(ev.document.meta))
            pdrawing.attach_refs(v, st.model)
            st.views[feature.name] = v
            r.faces_created = len(v.visible)
            r.warnings.extend(v.warnings)
        elif feature.kind == "dimension":
            st.dimensions.append(pdrawing.build_dimension(st.model, feature.name, feature.args, st.views))
        else:
            st.notes.append(pdrawing.build_note(feature.name, feature.args, st.views))
    except pdrawing.DrawingError as exc:
        raise ValueError(str(exc)) from None


def _finish_assembly(ev: Evaluation) -> None:
    """Solve the poses of the instance features evaluated so far and place them."""
    if not ev.assembly.instances:
        ev.solution, ev.posed = None, []
        return
    ev.solution = pasm.solve(ev.assembly)
    ev.posed = pasm.roots(ev.assembly, ev.solution)


def _sketch_geom(feature: Feature, ev: Evaluation) -> SketchGeom:
    sk_name = feature.args["sketch"]
    if sk_name in ev.suppressed:
        raise ValueError(f"sketch {sk_name!r} is suppressed")
    if sk_name not in ev.sketches:
        raise ValueError(f"sketch {sk_name!r} is missing or failed")
    sg = ev.sketches[sk_name]
    if sg.faces is None:
        raise ValueError(f"sketch {sk_name!r} has no closed profile")
    return sg


def _import_tolerance(tree: Instance) -> float:
    """Default tessellation tolerance for an import: absolute for small parts,
    relative for big assemblies so a 500 mm enclosure does not mesh at 0.05 mm."""
    leaves = [leaf.shape for leaf in tree.leaves() if leaf.shape is not None]
    if not leaves:
        return DEFAULT_TOLERANCE
    boxes = [s.bounding_box(optimal=False) for s in leaves]  # a heuristic: the plain box is enough and fast
    lo = min(min(b.min.X, b.min.Y, b.min.Z) for b in boxes)
    hi = max(max(b.max.X, b.max.Y, b.max.Z) for b in boxes)
    return max(DEFAULT_TOLERANCE, round((hi - lo) / 2000.0, 3))


def _v(vec) -> list[float]:
    return [round(float(c), 6) for c in vec]


def plane_json(plane: Plane, body: Part | None) -> dict[str, Any]:
    size = 60.0
    if body is not None:
        size = max(20.0, round(body.bounding_box().diagonal * 0.6, 1))
    return {"origin": _v(plane.origin), "x_dir": _v(plane.x_dir), "y_dir": _v(plane.y_dir),
            "z_dir": _v(plane.z_dir), "size": size}


def _face_plane(face: Face, what: str) -> Plane:
    """The plane of a planar face, oriented as a person looking at the face sees it:
    on a wall or an inclined face y is global +Z projected onto the face (up) and x
    points to the viewer's right; on a horizontal face x is global X."""
    if face.geom_type.name != "PLANE":
        raise ValueError(f"{what}: the face is {face.geom_type.name.lower()}, not planar")
    n = face.normal_at()
    up = Vector(0, 0, 1)
    y = up - n * n.dot(up)
    if y.length > 1e-6:
        x = y.normalized().cross(n)
    else:
        x = Vector(1, 0, 0) - n * n.dot(Vector(1, 0, 0))
    return Plane(origin=face.center(), x_dir=x.normalized(), z_dir=n)


def _select(ref: dict[str, Any], ev: Evaluation, what: str, many: bool = False) -> tuple[Selector, list[Shape]]:
    """Resolve a stored selector against the current body."""
    if ev.body is None:
        raise ValueError(f"{what}: there is no body yet to select from")
    sel = Selector.from_json(ref["selector"])
    try:
        return sel, resolve(sel, ev.body, ev.identity(), many=many)
    except SelectorError as exc:
        raise ValueError(f"{what}: {exc}") from None


def _select_many(refs: Any, ev: Evaluation, what: str) -> tuple[str, list[Shape]]:
    """Resolve one selector or a list of them; the shapes of all, without repeats."""
    items = refs if isinstance(refs, list) else [refs]
    names, out, seen = [], [], set()
    for ref in items:
        sel, shapes = _select(ref, ev, what, many=True)
        names.append(sel.ref_name)
        for s in shapes:
            if _key(s) not in seen:
                seen.add(_key(s))
                out.append(s)
    return ", ".join(names), out


def resolve_plane_ref(ref: Any, ev: Evaluation, what: str) -> Plane:
    """A standard plane name, {"plane": feature} or {"selector": face selector} to a Plane."""
    if isinstance(ref, str):
        if ref not in STANDARD_PLANES:
            raise ValueError(f"{what}: unknown plane {ref!r}")
        return STANDARD_PLANES[ref]
    if isinstance(ref, dict) and "plane" in ref:
        name = ref["plane"]
        if name not in ev.planes:
            raise ValueError(f"{what}: plane {name!r} is missing, failed, or comes later in the tree")
        return ev.planes[name]
    if isinstance(ref, dict) and "selector" in ref:
        sel, faces = _select(ref, ev, what)
        if len(faces) != 1 or not isinstance(faces[0], Face):
            raise ValueError(f"{what}: {sel.ref_name} must pick exactly one face")
        return _face_plane(faces[0], what)
    raise ValueError(f"{what}: bad plane reference {ref!r}")


def sketch_plane(feature: Feature, ev: Evaluation) -> Plane:
    base = resolve_plane_ref(feature.args["on"], ev, f"sketch {feature.name!r}")
    return finish_plane(base, feature)


def _point_ref(ref: Any, ev: Evaluation, what: str) -> Vector:
    if isinstance(ref, dict) and "selector" in ref:
        sel, vs = _select(ref, ev, what)
        if len(vs) != 1:
            raise ValueError(f"{what}: {sel.ref_name} must pick exactly one vertex")
        return Vector(*[float(c) for c in vs[0]])
    return Vector(*[float(c) for c in ref])


def evaluate_plane(feature: Feature, ev: Evaluation) -> Plane:
    a = feature.args
    what = f"plane {feature.name!r}"
    if "base" in a:
        base = resolve_plane_ref(a["base"], ev, what)
        angle = float(a.get("angle", 0.0))
        if angle:
            _, edges = _select({"selector": a["about"]}, ev, what)
            edge = edges[0]
            if edge.geom_type.name != "LINE":
                raise ValueError(f"{what}: about= needs a straight edge")
            d = (edge.end_point() - edge.start_point()).normalized()
            n0 = base.z_dir
            if abs(d.dot(n0)) > 1e-6:
                raise ValueError(f"{what}: the edge must lie on the base plane")
            rad = math.radians(angle)
            n = n0 * math.cos(rad) + d.cross(n0) * math.sin(rad)  # Rodrigues about d, d.n0 == 0
            plane = Plane(origin=edge.start_point(), x_dir=d, z_dir=n)
        else:
            plane = base
        offset = float(a.get("offset", 0.0))
        if offset:
            plane = plane.offset(offset)
    elif "between" in a:
        pa, pb = (resolve_plane_ref(r, ev, what) for r in a["between"])
        if abs(abs(pa.z_dir.dot(pb.z_dir)) - 1.0) > 1e-6:
            raise ValueError(f"{what}: between= needs two parallel planes or faces")
        dist = (pb.origin - pa.origin).dot(pa.z_dir)
        plane = Plane(origin=pa.origin + pa.z_dir * (dist / 2), x_dir=pa.x_dir, z_dir=pa.z_dir)
    elif "through" in a:
        p1, p2, p3 = (_point_ref(r, ev, what) for r in a["through"])
        u, v = p2 - p1, p3 - p1
        n = u.cross(v)
        if n.length < 1e-9:
            raise ValueError(f"{what}: the three points are collinear")
        plane = Plane(origin=p1, x_dir=u.normalized(), z_dir=n.normalized())
    else:
        raise ValueError(f"{what}: no definition")
    if a.get("flip"):
        plane = Plane(origin=plane.origin, x_dir=plane.x_dir, z_dir=-plane.z_dir)
    return plane


def sketch_context(doc: Document, feature: Feature, cache: Evaluation | None = None) -> tuple[Part | None, Plane, Identity]:
    """The body, plane and identity map a sketch sees: everything before it in the tree."""
    idx = doc.features.index(feature)
    ev = evaluate(doc, upto=doc.features[idx - 1].name, cache=cache) if idx > 0 else Evaluation(document=doc)
    return ev.body, sketch_plane(feature, ev), ev.identity()


def resolve_projections(feature: Feature, body: Part | None, plane: Plane,
                        identity: Identity | None = None) -> dict[str, Projected]:
    """Projected entities of a sketch, in the sketch's 2D frame."""
    out: dict[str, Projected] = {}
    for e in feature.entities:
        if e.kind != "project":
            continue
        if body is None:
            raise ValueError(f"project {e.name!r}: there is no body yet to project from")
        sel = Selector.from_json(e.args["selector"])
        try:
            pr = project(sel, body, plane, identity)
        except ProjectionError as exc:
            raise ValueError(f"project {e.name!r}: {exc}") from None
        pr.name = e.name
        out[e.name] = pr
    return out


def solve_feature_sketch(feature: Feature, body: Part | None,
                         drag: dict[str, tuple[float, float]] | None = None,
                         plane: Plane | None = None, identity: Identity | None = None) -> SketchSolution:
    if plane is None:
        on = feature.args["on"]
        plane = finish_plane(STANDARD_PLANES[on], feature) if isinstance(on, str) and on in STANDARD_PLANES else Plane.XY
    projected = resolve_projections(feature, body, plane, identity)
    try:
        return _solve_with_offsets(feature, projected, drag)
    except (SolverError, SketchError) as exc:
        raise ValueError(str(exc)) from None


def _solve_with_offsets(feature: Feature, projected: dict[str, Projected], drag) -> SketchSolution:
    """Solve, derive the offset entities from the solved coordinates, and when
    constraints reference an offset, solve again with the new offset until it
    settles (a few passes at most: the sources are usually pinned by then)."""
    has_offsets = any(e.kind == "offset" for e in feature.entities)
    if not has_offsets:
        return solve_sketch(feature, projected, drag)
    derived = compute_offsets(feature, None, projected)
    solution = solve_sketch(feature, {**projected, **derived}, drag)
    for _ in range(4):
        fresh = compute_offsets(feature, solution.coords, projected)
        settled = all(_same_items(fresh[k], derived.get(k)) for k in fresh)
        derived = fresh
        if settled or not references_offsets(feature):
            break
        solution = solve_sketch(feature, {**projected, **derived}, drag)
    solution.projected = {**projected, **derived}
    return solution


def _same_items(a: Projected, b: Projected | None, tol: float = 1e-6) -> bool:
    if b is None or len(a.items) != len(b.items):
        return False
    for x, y in zip(a.items, b.items, strict=True):
        if x.kind != y.kind or x.coords.keys() != y.coords.keys():
            return False
        for key, v in x.coords.items():
            w = y.coords[key]
            if isinstance(v, (int, float)):
                if abs(float(v) - float(w)) > tol:
                    return False
            elif any(abs(float(p) - float(q)) > tol for p, q in zip(v, w, strict=True)):
                return False
    return True


def _document_tree(tree: Instance, name: str, file: str) -> Instance:
    """An imported tree as the document shows it: the root named after the feature,
    every node knowing its file (`node` keeps its path inside the file)."""
    old_prefix = tree.path

    def fix(inst: Instance) -> Instance:
        c = copy.copy(inst)
        c.index = None
        c.path = name + c.path[len(old_prefix):]
        c.file = file
        c.children = [fix(ch) for ch in inst.children]
        return c

    new = fix(tree)
    new.name = name
    return new


# ---------------------------------------------------------------------------
# the identity map


@dataclass
class _FaceInfo:
    face: Face
    owner: str
    tags: tuple[str, ...]
    kind: GeomType
    normal: Vector | None
    center: Vector


def _face_info(face: Face, owner: str, tags: tuple[str, ...]) -> _FaceInfo:
    kind = face.geom_type
    normal = None
    if kind == GeomType.PLANE:
        try:
            normal = face.normal_at()
        except Exception:  # noqa: BLE001
            normal = None
    return _FaceInfo(face, owner, tags, kind, normal, face.center())


def _same_axis(a, b) -> bool:
    da, db = Vector(*a.Direction().Coord()), Vector(*b.Direction().Coord())
    if abs(abs(da.dot(db)) - 1.0) > 1e-6:
        return False
    d = Vector(*b.Location().Coord()) - Vector(*a.Location().Coord())
    return (d - da * d.dot(da)).length < TOL


def _same_surface(f: _FaceInfo, g: _FaceInfo) -> bool:
    """Do two faces lie on the same surface (one is a split or trimmed piece of the other)?"""
    if f.kind != g.kind:
        return False
    if f.kind == GeomType.PLANE:
        if f.normal is None or g.normal is None or f.normal.dot(g.normal) < 1 - 1e-6:
            return False
        return abs((g.center - f.center).dot(f.normal)) < TOL
    try:
        sa, sb = BRepAdaptor_Surface(f.face.wrapped), BRepAdaptor_Surface(g.face.wrapped)
        if f.kind == GeomType.CYLINDER:
            ca, cb = sa.Cylinder(), sb.Cylinder()
            return abs(ca.Radius() - cb.Radius()) < TOL and _same_axis(ca.Axis(), cb.Axis())
        if f.kind == GeomType.CONE:
            ca, cb = sa.Cone(), sb.Cone()
            return (abs(ca.SemiAngle() - cb.SemiAngle()) < 1e-6 and _same_axis(ca.Axis(), cb.Axis())
                    and (Vector(*ca.Apex().Coord()) - Vector(*cb.Apex().Coord())).length < TOL)
        if f.kind == GeomType.SPHERE:
            ca, cb = sa.Sphere(), sb.Sphere()
            return (abs(ca.Radius() - cb.Radius()) < TOL
                    and (Vector(*ca.Location().Coord()) - Vector(*cb.Location().Coord())).length < TOL)
        if f.kind == GeomType.TORUS:
            ca, cb = sa.Torus(), sb.Torus()
            return (abs(ca.MajorRadius() - cb.MajorRadius()) < TOL and abs(ca.MinorRadius() - cb.MinorRadius()) < TOL
                    and _same_axis(ca.Axis(), cb.Axis()))
    except Exception:  # noqa: BLE001 - an exotic surface: no inheritance
        return False
    return False


def _inherit(face: _FaceInfo, candidates: list[_FaceInfo]) -> tuple[str, tuple[str, ...]] | None:
    """The candidate this face is a piece of: on the same surface and containing
    the midpoints of its edges (two coplanar faces at different places, such as a
    pattern's copies, must not be confused). Falls back to the candidate that
    contains most of them, for faces a clean() merged out of several."""
    on_surface = [c for c in candidates if _same_surface(face, c)]
    if not on_surface:
        return None
    if len(on_surface) == 1:
        return on_surface[0].owner, on_surface[0].tags
    try:
        probes = [e.position_at(0.5) for e in face.face.edges()]
    except Exception:  # noqa: BLE001
        probes = []
    best, best_score = on_surface[0], -1
    for c in on_surface:
        score = _contained(c.face, probes)
        if score > best_score:
            best, best_score = c, score
    return best.owner, best.tags


def _contained(face: Face, points: list[Vector]) -> int:
    """How many of the points lie inside the face (-1 when the classifier fails)."""
    try:
        return sum(1 for pt in points if face.is_inside(pt, TOL))
    except Exception:  # noqa: BLE001
        return -1


def _merge_body(feature: Feature, ev: Evaluation, r: FeatureResult, tools: list[Part], op: str,
                tool_tags: list[tuple[str, ...]] | None, relabel=None) -> None:
    """Combine tools with the body ("add", "cut", or "replace" when a tool is
    the finished new body) and update the identity map. New faces keep the
    owner and tags of the body or tool face whose surface they lie on; faces
    with no such source are the feature's own, labelled by `relabel` if given."""
    old = [_face_info(f, ev.face_owner.get(_key(f), ""), ev.face_tags.get(_key(f), ())) for f in ev.body.faces()] \
        if ev.body is not None else []
    before_faces = {_key(fi.face) for fi in old}
    before_edges = {_key(e) for e in ev.body.edges()} if ev.body is not None else set()
    tool_map: dict[int, tuple[str, ...]] = {}
    tool_faces: list[_FaceInfo] = []
    if op != "replace":
        tags = tool_tags or []
        i = 0
        for t in tools:
            for f in t.faces():
                tg = tags[i] if i < len(tags) else ()
                tool_map[_key(f)] = tg
                tool_faces.append(_face_info(f, feature.name, tg))
                i += 1

    if op == "replace":
        body = tools[0]
    elif op == "add":
        if ev.body is None:
            body = tools[0] if len(tools) == 1 else tools[0].fuse(*tools[1:]).clean()
        else:
            body = ev.body + tools[0] if len(tools) == 1 else ev.body.fuse(*tools).clean()
    else:
        if ev.body is None:
            raise ValueError("nothing to cut yet: add material first")
        body = ev.body - tools[0] if len(tools) == 1 else ev.body.cut(*tools).clean()
    solids = body.solids()
    if len(solids) == 0:
        raise ValueError("the result is empty")
    if len(solids) > 1:
        raise ValueError(f"the result is {len(solids)} separate solids; a part is one solid body")
    body = _as_part(body)

    created = 0
    for f in body.faces():
        k = _key(f)
        if k in before_faces:
            continue
        if k in tool_map:
            ev.face_owner[k], ev.face_tags[k] = feature.name, tool_map[k]
            created += 1
            continue
        info = _face_info(f, feature.name, ())
        src = _inherit(info, old + tool_faces)
        if src is None and relabel is not None:
            src = relabel(info, old)
        if src is None:
            src = (feature.name, ())
        ev.face_owner[k], ev.face_tags[k] = src
        if src[0] == feature.name:
            created += 1

    adjacent: dict[int, list[int]] = {}
    for f in body.faces():
        fk = _key(f)
        for e in f.edges():
            adjacent.setdefault(_key(e), []).append(fk)
    for e in body.edges():
        k = _key(e)
        if k in before_edges:
            continue
        fks = adjacent.get(k, [])
        owners = {ev.face_owner.get(fk, feature.name) for fk in fks}
        ev.edge_owner[k] = owners.pop() if len(owners) == 1 else feature.name
        ev.edge_tags[k] = tuple(sorted({t for fk in fks for t in ev.face_tags.get(fk, ())}))
    r.faces_created = created
    r.shape = body
    ev.body = body


# ---------------------------------------------------------------------------
# tools from sketches


def _nearest_profile_label(sg: SketchGeom, point: Vector) -> tuple[str, ...]:
    if not sg.profile:
        return ()
    local = sg.plane.to_local_coords(point)
    q = Vector(local.X, local.Y, 0)
    best = min(sg.profile, key=lambda pe: pe[0].distance_to(q))
    return (best[1],)


def _label_extrusion(tool: Part, sg: SketchGeom, direction: Vector) -> list[tuple[str, ...]]:
    """"top" for the end face in the direction of extrusion, "bottom" for the
    start face, the profile entity for the sides."""
    faces = tool.faces()
    ends: list[tuple[int, float]] = []
    for i, f in enumerate(faces):
        if f.geom_type == GeomType.PLANE and abs(f.normal_at().dot(direction)) > 1 - 1e-6:
            ends.append((i, (f.center() - sg.plane.origin).dot(direction)))
    tags: list[tuple[str, ...]] = [() for _ in faces]
    if ends:
        ends.sort(key=lambda t: t[1])
        if len(ends) == 1:
            tags[ends[0][0]] = (":top",) if ends[0][1] > TOL else (":bottom",)
        else:
            for i, _ in ends[:-1]:
                tags[i] = (":bottom",)
            tags[ends[-1][0]] = (":top",)
    for i, f in enumerate(faces):
        if not tags[i]:
            tags[i] = _nearest_profile_label(sg, f.center())
    return tags


def _extrude_tool(feature: Feature, ev: Evaluation, sg: SketchGeom, op: str) -> tuple[Part, list[tuple[str, ...]]]:
    a = feature.args
    what = f"{feature.kind} {feature.name!r}"
    depth = a.get("depth")
    through = bool(a.get("through", False))
    symmetric = bool(a.get("symmetric", False))
    flip = bool(a.get("flip", False))
    draft = float(a.get("draft", 0.0))
    direction = sg.plane.z_dir * (-1.0 if flip else 1.0)
    if a.get("upto"):
        sel, faces = _select(a["upto"], ev, what)
        face = faces[0]
        if not isinstance(face, Face) or face.geom_type != GeomType.PLANE:
            raise ValueError(f"upto= needs a planar face; {sel.ref_name} is not")
        fn = face.normal_at()
        den = direction.dot(fn)
        if abs(den) < 1e-6:
            raise ValueError(f"upto= face {sel.ref_name} is parallel to the extrusion")
        depth = (face.center() - sg.plane.origin).dot(fn) / den
        if depth < 0:  # the face is on the other side: go that way
            depth, direction, flip = -depth, -direction, not flip
        if depth <= TOL:
            raise ValueError(f"upto= face {sel.ref_name} lies on the sketch plane")
    elif through:
        if ev.body is None:
            raise ValueError("through=True needs an existing body")
        bb = ev.body.bounding_box()
        depth = 2.0 * (bb.diagonal + 1.0)
    if depth is None or depth <= 0:
        raise ValueError("depth must be positive")
    if op == "cut" and ev.body is not None and not symmetric and not a.get("upto"):
        # a cut aims at the material: drawn on a face it goes into the body, not away from it.
        # Probe just ahead of and behind the profile; when only one side has material, go that
        # way. With material on both sides, or none nearby (a cut drawn off the body), fall back
        # to the body as a whole: flip only when all of it lies behind the sketch plane.
        solids = ev.body.solids()
        centres = [f.center() for f in sg.faces] or [sg.plane.origin]
        ahead = any(s.is_inside(c + direction * 0.05) for c in centres for s in solids)
        behind = any(s.is_inside(c - direction * 0.05) for c in centres for s in solids)
        if behind and not ahead:
            direction, flip = -direction, not flip
        elif ahead == behind:
            bb = ev.body.bounding_box()
            corners = [Vector(x, y, z) for x in (bb.min.X, bb.max.X) for y in (bb.min.Y, bb.max.Y) for z in (bb.min.Z, bb.max.Z)]
            along = [(c - sg.plane.origin).dot(direction) for c in corners]
            if max(along) <= TOL and min(along) < -TOL:
                direction, flip = -direction, not flip
    amount = depth / 2 if symmetric else depth
    if flip:
        amount = -amount
    tool = b3d_extrude(sg.faces, amount=amount, both=symmetric, taper=draft)
    if through and not symmetric:
        # extend a hair behind the sketch plane so coplanar faces are cut cleanly
        back = b3d_extrude(sg.faces, amount=-0.01 if amount > 0 else 0.01)
        tool = tool + back
    tool = _as_part(tool)
    if not tool.solids():
        raise ValueError("the extrusion is empty")
    return tool, _label_extrusion(tool, sg, direction)


def _revolve_axis(feature: Feature, ev: Evaluation, sg: SketchGeom) -> Axis:
    ref = feature.args["axis"]
    if isinstance(ref, dict) and "selector" in ref:
        return _axis_from_selector(ref, ev, f"revolve {feature.name!r}")
    if ref in AXIS_DIRS:
        return Axis((0, 0, 0), AXIS_DIRS[ref])
    sk = feature.args["sketch"]
    doc_sketch = ev.document.feature(sk)
    ent = doc_sketch.entity(ref) if doc_sketch else None
    if ent is None:
        raise ValueError(f"axis {ref!r} is not a line of sketch {sk!r} nor X, Y or Z")
    if ent.kind == "line":
        c = sg.coords.get(ent.name, ent.args)
        p, q = c["start"], c["end"]
    elif ent.kind == "project":
        pr = sg.projected.get(ent.name)
        if pr is None or len(pr.items) != 1 or pr.items[0].kind != "line":
            raise ValueError(f"axis {ref!r} must project to a single straight edge")
        p, q = pr.items[0].coords["start"], pr.items[0].coords["end"]
    else:
        raise ValueError(f"axis {ref!r} is a {ent.kind}; use a line")
    p3 = sg.plane.from_local_coords(Vector(p[0], p[1], 0))
    q3 = sg.plane.from_local_coords(Vector(q[0], q[1], 0))
    if (q3 - p3).length < 1e-9:
        raise ValueError(f"axis {ref!r} has zero length")
    return Axis(p3, q3 - p3)


def _axis_from_selector(ref: dict[str, Any], ev: Evaluation, what: str) -> Axis:
    sel, shapes = _select(ref, ev, what)
    s = shapes[0]
    if isinstance(s, Edge):
        if s.geom_type != GeomType.LINE:
            raise ValueError(f"{what}: axis {sel.ref_name} is not a straight edge")
        return Axis(s.start_point(), s.end_point() - s.start_point())
    if isinstance(s, Face) and s.geom_type == GeomType.CYLINDER:
        ax = BRepAdaptor_Surface(s.wrapped).Cylinder().Axis()
        return Axis(Vector(*ax.Location().Coord()), Vector(*ax.Direction().Coord()))
    raise ValueError(f"{what}: axis {sel.ref_name} must be a straight edge or a cylindrical face")


def _label_revolution(tool: Part, sg: SketchGeom, axis: Axis, angle: float) -> list[tuple[str, ...]]:
    n, d, o = sg.plane.z_dir, axis.direction.normalized(), axis.position
    perp = n.cross(d)
    side = sum((f.center() - o).dot(perp) for f in sg.faces.faces())
    if side < 0:
        perp = -perp
    tags: list[tuple[str, ...]] = []
    for f in tool.faces():
        if angle < 360 and f.geom_type == GeomType.PLANE and abs(f.normal_at().dot(d)) < 1e-6:
            verts = [Vector(*tuple(v)) for v in f.vertices()]
            if verts and all(abs((v - sg.plane.origin).dot(n)) < TOL for v in verts):
                tags.append((":start",))
            else:
                tags.append((":end",))
            continue
        try:
            c = f.position_at(0.5, 0.5)
        except Exception:  # noqa: BLE001
            c = f.center()
        rel = c - o
        h = rel.dot(d)
        radial = rel - d * h
        tags.append(_nearest_profile_label(sg, o + d * h + perp * radial.length))
    return tags


def _revolve_tool(feature: Feature, ev: Evaluation, sg: SketchGeom) -> tuple[Part, list[tuple[str, ...]]]:
    axis = _revolve_axis(feature, ev, sg)
    n = sg.plane.z_dir
    d = axis.direction.normalized()
    if abs(d.dot(n)) > 1e-6 or abs((axis.position - sg.plane.origin).dot(n)) > TOL:
        raise ValueError("the axis must lie in the sketch plane")
    signs = set()
    for f in sg.faces.faces():
        for v in f.vertices():
            s = (Vector(*tuple(v)) - axis.position).cross(d).dot(n)
            if abs(s) > TOL:
                signs.add(s > 0)
    if len(signs) == 2:
        raise ValueError("the profile crosses the axis; keep it on one side")
    if not signs:
        raise ValueError("the profile lies on the axis")
    angle = float(feature.args.get("angle", 360.0))
    tool = b3d_revolve(sg.faces, axis=axis, revolution_arc=angle)
    tool = _as_part(tool)
    if not tool.solids() or tool.volume < 1e-9:
        raise ValueError("the revolve produced no volume")
    return tool, _label_revolution(tool, sg, axis, angle)


# ---------------------------------------------------------------------------
# fillet, chamfer, shell


def _fillet_or_chamfer(feature: Feature, ev: Evaluation, r: FeatureResult) -> None:
    a = feature.args
    what = f"{feature.kind} {feature.name!r}"
    ref_name, edges = _select_many(a["edges"], ev, what)
    edges = [e for e in edges if isinstance(e, Edge)]
    if not edges:
        raise ValueError(f"{ref_name} picked no edges")
    edge_tags = [(e, ev.edge_tags.get(_key(e), ())) for e in edges]
    try:
        if feature.kind == "fillet":
            new = b3d_fillet(edges, radius=float(a["radius"]))
        else:
            new = b3d_chamfer(edges, length=float(a["distance"]), length2=a.get("distance2"))
    except Exception as exc:  # noqa: BLE001 - OCCT's message is rarely useful on its own
        size = a.get("radius", a.get("distance"))
        raise ValueError(f"the kernel could not {feature.kind} {len(edges)} edge(s) of {ref_name} at {size}: "
                         f"{str(exc).strip() or 'the edges may be too short or the radius too large'}") from None
    new = _as_part(new)

    def relabel(info: _FaceInfo, _old: list[_FaceInfo]) -> tuple[str, tuple[str, ...]]:
        nearest = min(edge_tags, key=lambda et: et[0].distance_to(info.center))
        return feature.name, nearest[1]

    _merge_body(feature, ev, r, [new], "replace", None, relabel)


def _shell(feature: Feature, ev: Evaluation, r: FeatureResult) -> None:
    a = feature.args
    what = f"shell {feature.name!r}"
    if ev.body is None:
        raise ValueError("there is no body yet to shell")
    thickness = float(a["thickness"])
    amount = thickness if a.get("outward") else -thickness
    openings: list[Face] = []
    if a.get("faces"):
        _, faces = _select_many(a["faces"], ev, what)
        openings = [f for f in faces if isinstance(f, Face)]
    try:
        if openings:
            new = b3d_offset(ev.body, amount=amount, openings=openings)
        else:
            inner = b3d_offset(ev.body, amount=amount)
            new = (ev.body - inner) if amount < 0 else (inner - ev.body)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"the kernel could not shell the body at {thickness}: {str(exc).strip() or 'try a thinner wall'}") from None
    new = _as_part(new)

    def relabel(info: _FaceInfo, old: list[_FaceInfo]) -> tuple[str, tuple[str, ...]]:
        if info.kind == GeomType.PLANE and info.normal is not None:
            for c in old:
                if c.kind == GeomType.PLANE and c.normal is not None and abs(abs(c.normal.dot(info.normal)) - 1) < 1e-6 \
                        and abs(abs((info.center - c.center).dot(c.normal)) - thickness) < TOL:
                    return feature.name, (*c.tags, ":inner")
        return feature.name, (":inner",)

    _merge_body(feature, ev, r, [new], "replace", None, relabel)


# ---------------------------------------------------------------------------
# patterns and mirrors


def _direction(ref: Any, ev: Evaluation, what: str) -> Vector:
    if isinstance(ref, str):
        if ref not in AXIS_DIRS:
            raise ValueError(f"{what}: unknown direction {ref!r}")
        return Vector(*AXIS_DIRS[ref])
    if isinstance(ref, dict) and "selector" in ref:
        sel, shapes = _select(ref, ev, what)
        e = shapes[0]
        if not isinstance(e, Edge) or e.geom_type != GeomType.LINE:
            raise ValueError(f"{what}: direction {sel.ref_name} is not a straight edge")
        return (e.end_point() - e.start_point()).normalized()
    v = Vector(*[float(c) for c in ref])
    if v.length < 1e-9:
        raise ValueError(f"{what}: direction has zero length")
    return v.normalized()


def _source_tools(feature: Feature, ev: Evaluation, what: str) -> list[Tool]:
    tools = []
    for name in feature.args.get("features", []):
        f = ev.document.feature(name)
        if f is None or f.kind not in TOOL_KINDS:
            raise ValueError(f"{what}: {name!r} is not an extrude, cut or revolve")
        if name in ev.suppressed:
            raise ValueError(f"{what}: {name!r} is suppressed")
        if name not in ev.tools:
            raise ValueError(f"{what}: {name!r} failed or comes later in the tree")
        tools.append(ev.tools[name])
    ops = {t.op for t in tools}
    if len(ops) > 1:
        raise ValueError(f"{what}: repeat features that all add or all cut, not a mix")
    return tools


def _repeat(feature: Feature, ev: Evaluation, r: FeatureResult) -> None:
    a = feature.args
    what = f"{feature.kind} {feature.name!r}"
    copies: list[Part] = []
    tags: list[tuple[str, ...]] = []
    if feature.kind == "mirror":
        plane = resolve_plane_ref(a["about"], ev, what)
        tools = _source_tools(feature, ev, what)
        if tools:
            op = tools[0].op
            for t in tools:
                copies.append(Part(b3d_mirror(t.shape, about=plane).wrapped))
                tags.extend(t.tags)
        else:
            if ev.body is None:
                raise ValueError(f"{what}: there is no body to mirror")
            op = "add"
            copies.append(Part(b3d_mirror(ev.body, about=plane).wrapped))
            tags.extend(ev.face_tag_list())
    else:
        tools = _source_tools(feature, ev, what)
        if not tools:
            raise ValueError(f"{what}: name the feature to repeat")
        op = tools[0].op
        if feature.kind == "linear_pattern":
            d1 = _direction(a.get("direction", "X"), ev, what)
            n1, s1 = int(a["count"]), float(a["spacing"])
            n2 = int(a["count2"]) if a.get("count2") else 1
            d2 = _direction(a.get("direction2", "Y"), ev, what) if n2 > 1 else Vector(0, 0, 0)
            s2 = float(a.get("spacing2") or 0.0)
            offsets = [d1 * (s1 * i) + d2 * (s2 * j) for j in range(n2) for i in range(n1) if i or j]
            for off in offsets:
                loc = Location((off.X, off.Y, off.Z))
                for t in tools:
                    copies.append(t.shape.moved(loc))
                    tags.extend(t.tags)
        else:
            axis_ref = a.get("axis", "Z")
            axis = _axis_from_selector(axis_ref, ev, what) if isinstance(axis_ref, dict) else Axis((0, 0, 0), AXIS_DIRS[axis_ref])
            count, total = int(a["count"]), float(a.get("angle", 360.0))
            if count > 1:
                step = total / count if abs(total - 360.0) < 1e-9 else total / (count - 1)
                for k in range(1, count):
                    for t in tools:
                        copies.append(t.shape.rotate(axis, step * k))
                        tags.extend(t.tags)
    if not copies:
        r.warnings.append("count 1 repeats nothing")
        r.shape = ev.body
        return
    _merge_body(feature, ev, r, copies, op, tags)
    ev.tools[feature.name] = Tool(copies[0] if len(copies) == 1 else Part(Compound(copies).wrapped), op, tags)
