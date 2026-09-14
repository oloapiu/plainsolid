"""Drawings: views of a part or assembly on a sheet, with hidden lines, section
views with hatching, dimensions, notes and a title block, exported to SVG,
DXF and PDF.

A drawing document names its model in `meta(kind="drawing", of="bracket.py",
sheet="A4")`. Each `view("front", direction=FRONT, at=(60, 150))` projects the
model with build123d's hidden-line algorithm into a frame we reproduce here
(x = up × look_from, y = look_from × x, origin at the model's centre), so a
dimension's references, resolved on the model with the same selectors as
everywhere else, project into the same 2D coordinates. `at` is where the
view's centre lands on the sheet, in mm from the bottom-left corner; `scale`
multiplies. A section view splits the model with a standard plane or one of
the model's plane features, looks at the cut from the removed side, hatches
the cap faces and gets a letter; the other views that see the plane edge-on
show its trace with arrows.

Dimensions take one or two references through the view handle
(`front.edges.of("body").from_sketch("outer_wall")`, `top.faces.of("hole_cut")
.from_sketch("hole1")`) and a placement relative to the view's centre:
distance (points, parallel lines, a point and a line, circle centres),
diameter, radius and angle. Notes are text on the sheet or beside a view.

Everything after projection is one 2D scene in sheet millimetres (`layout`),
which the client draws and the three exporters render; the visible segments
of a plain view carry the selector of the model edge they came from, so a
click on the sheet can write a dimension.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
from build123d import Compound, Edge, Face, GeomType, Keep, Plane, Shape, Vector, Vertex, split
from build123d.exporters import Drawing as _HiddenLines
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType

from . import assembly as pasm
from .dependencies import file_dependencies
from .selectors import Identity, Selector, SelectorError, resolve
from .sketchgeom import STANDARD_PLANES
from .stepimport import file_key

SHEETS = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "letter": (279.4, 215.9)}  # landscape, mm
# look_from (toward the viewer) and up, in model coordinates
DIRECTIONS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "front": ((0, -1, 0), (0, 0, 1)), "back": ((0, 1, 0), (0, 0, 1)),
    "right": ((1, 0, 0), (0, 0, 1)), "left": ((-1, 0, 0), (0, 0, 1)),
    "top": ((0, 0, 1), (0, 1, 0)), "bottom": ((0, 0, -1), (0, 1, 0)),
    "iso": ((1, -1, 1), (0, 0, 1)),
}
DIMENSION_KINDS = ("distance", "diameter", "radius", "angle")
DRAWING_KINDS = {"view", "dimension", "note"}
TEXT = 3.5          # text height, mm
ARROW = 2.5         # arrowhead length, mm
GAP = 1.0           # extension line gap from the geometry
OVERSHOOT = 1.5     # extension line past the dimension line
HATCH_SPACING = 2.5
MARGIN = 8.0        # sheet border inset
TITLE_W, TITLE_H = 110.0, 24.0
MAX_PICKABLE_EDGES = 5000
_MODELS: dict[tuple[str, int, int], Model] = {}


class DrawingError(ValueError):
    pass


def _num(v: float, digits: int = 2) -> str:
    text = f"{v:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _r(v: float) -> float:
    return round(float(v), 3)


def _np(v) -> np.ndarray:
    return np.array([float(v.X), float(v.Y), float(v.Z)]) if hasattr(v, "X") else np.array([float(c) for c in v])


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


# --- the model -------------------------------------------------------------------

@dataclass
class Model:
    """The part or assembly a drawing shows, with the labels its selectors use."""

    path: Path
    kind: str  # part | assembly | step
    name: str
    meta: dict[str, Any]
    shape: Shape
    identity: Identity | None  # owner={} (filled per view when resolving), tags, groups = owner features
    edge_labels: list[str]
    edge_tags: list[tuple[str, ...]]
    planes: dict[str, Plane]
    warnings: list[str]
    key: str
    center: np.ndarray
    dependencies: list[str]
    dep_keys: list[tuple[str, int, int]] = field(default_factory=list)
    input_keys: tuple[tuple[str, int, int, int], ...] = ()

    def current(self) -> bool:
        return self.input_keys == file_dependencies([self.path])


def load_model(path: Path) -> Model:
    """The model at `path` (a part file, an assembly file or a STEP file), cached
    until it or, for an assembly, any of its parts changes on disk."""
    if not path.exists():
        raise DrawingError(f"no such file: {path}")
    key = file_key(path)
    m = _MODELS.get(key)
    if m is None or not m.current():
        while True:
            stamps = file_dependencies([path])
            m = _load_model(path, key)
            if stamps == file_dependencies([path]):
                break
        m.input_keys = stamps
        m.dependencies = [stamp[0] for stamp in stamps]
        m.key += repr(stamps)
        if len(_MODELS) >= 64:
            _MODELS.pop(next(iter(_MODELS)))
        _MODELS[key] = m
    return m


def _load_model(path: Path, key: tuple[str, int, int]) -> Model:
    from .evaluate import evaluate
    from .parse import parse_file

    keytext = f"{key[0]}:{key[1]}:{key[2]}"
    if path.suffix.lower() in pasm.STEP_SUFFIXES:
        try:
            rec = pasm.load_part(path)
        except pasm.AssemblyError as exc:
            raise DrawingError(str(exc)) from None
        n = len(rec.shape.edges())
        return Model(path, "step", rec.name, {"name": rec.name}, rec.shape, None, [rec.name] * n, [()] * n, {},
                     list(rec.warnings), keytext, rec.center, [str(path)], [key])
    if path.suffix != ".py":
        raise DrawingError(f"a drawing shows a .py model file or a STEP file, not {path.name}")
    doc = parse_file(str(path))
    if doc.errors:
        raise DrawingError(f"{path.name} does not parse: {doc.errors[0].message}")
    if doc.kind == "drawing":
        raise DrawingError(f"{path.name} is a drawing; a drawing shows a part or an assembly")
    ev = evaluate(doc)
    warnings = [r.error.message for r in ev.results if r.error]
    name = str(doc.meta.get("name") or path.stem)
    if doc.kind == "assembly":
        items = ev.items()
        if not items:
            raise DrawingError(f"{path.name} has no instances" + (f": {warnings[0]}" if warnings else ""))
        shape = Compound([it.shape for it in items])
        tags: dict[int, tuple[str, ...]] = {}
        groups: dict[int, str] = {}
        for it in items:
            inst = it.path.split(".")[0]
            for f, t in zip(it.shape.faces(), it.face_tags or [], strict=False):
                tags[hash(f.wrapped)] = t
            for e, t in zip(it.shape.edges(), it.edge_tags or [], strict=False):
                tags[hash(e.wrapped)] = t
            for s in (*it.shape.faces(), *it.shape.edges()):
                groups[hash(s.wrapped)] = inst
        edges = shape.edges()
        deps = [str(path), *sorted(ev.dependencies)]
        return Model(path, "assembly", name, dict(doc.meta), shape, Identity(owner={}, tags=tags, groups=groups),
                     [groups.get(hash(e.wrapped), "") for e in edges], [tags.get(hash(e.wrapped), ()) for e in edges],
                     dict(ev.planes), warnings, keytext, pasm._measure(shape)[0], deps,
                     [file_key(Path(p)) for p in deps])
    if ev.body is None:
        raise DrawingError(f"{path.name} has no body" + (f": {warnings[0]}" if warnings else ""))
    ident = ev.identity()
    return Model(path, "part", name, dict(doc.meta), ev.body, Identity(owner={}, tags=ident.tags, groups=ident.owner),
                 ev.edge_labels(), ev.edge_tag_list(), dict(ev.planes), warnings, keytext,
                 pasm._measure(ev.body)[0], [str(path)], [key])


def model_path(doc) -> Path:
    """Where the drawing's `meta(of=...)` points, relative to the drawing file."""
    rel = doc.meta.get("of")
    if not rel or not isinstance(rel, str):
        raise DrawingError('a drawing names its model: meta(kind="drawing", of="part.py")')
    p = Path(rel)
    if not p.is_absolute():
        base = Path(doc.path).parent if doc.path else Path.cwd()
        p = base / p
    return p.resolve()


def model_signature(doc) -> str:
    """What the prefix cache keys the views on: the model's file keys."""
    try:
        m = load_model(model_path(doc))
    except DrawingError as exc:
        return f"missing:{exc}"
    return repr(m.input_keys)


def sheet_size(meta: dict[str, Any]) -> tuple[str, float, float]:
    name = str(meta.get("sheet") or "A4")
    if name not in SHEETS:
        raise DrawingError(f"unknown sheet {name!r}; use one of {', '.join(SHEETS)}")
    return name, *SHEETS[name]


def sheet_scale(meta: dict[str, Any]) -> float:
    try:
        s = float(meta.get("scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        raise DrawingError(f"meta(scale=...) must be a number, got {meta.get('scale')!r}") from None
    if s <= 0:
        raise DrawingError("meta(scale=...) must be positive")
    return s


# --- 2D primitives -----------------------------------------------------------------

@dataclass
class Frame:
    """A view's projection: x = up × look_from, y = look_from × x, origin at the model centre."""

    origin: np.ndarray
    x: np.ndarray
    y: np.ndarray
    dir: np.ndarray  # toward the viewer

    def to2d(self, p) -> tuple[float, float]:
        d = _np(p) - self.origin
        return float(d @ self.x), float(d @ self.y)

    def depth(self, p) -> float:
        return float((_np(p) - self.origin) @ self.dir)

    def dir2d(self, v) -> tuple[float, float]:
        v = _np(v)
        return float(v @ self.x), float(v @ self.y)


def frame_for(look_from, look_up, origin) -> Frame:
    d = _unit(_np(look_from))
    up = _unit(_np(look_up))
    x = np.cross(up, d)
    if np.linalg.norm(x) < 1e-9:
        up = np.array([0.0, 1.0, 0.0]) if abs(d[1]) < 0.9 else np.array([0.0, 0.0, 1.0])
        x = np.cross(up, d)
    x = _unit(x)
    return Frame(_np(origin), x, np.cross(d, x), d)


@dataclass
class Seg:
    """A line, circle, arc or polyline; angles in degrees, counter-clockwise."""

    kind: str
    pts: list[tuple[float, float]] = field(default_factory=list)
    center: tuple[float, float] | None = None
    radius: float = 0.0
    a0: float = 0.0
    a1: float = 0.0
    ref: str | None = None

    def transformed(self, scale: float, dx: float, dy: float) -> Seg:
        def t(p):
            return (p[0] * scale + dx, p[1] * scale + dy)

        return Seg(self.kind, [t(p) for p in self.pts], t(self.center) if self.center else None,
                   self.radius * scale, self.a0, self.a1, self.ref)

    def bbox(self) -> tuple[float, float, float, float]:
        if self.kind == "circle":
            cx, cy = self.center
            return cx - self.radius, cy - self.radius, cx + self.radius, cy + self.radius
        pts = list(self.pts)
        if self.kind == "arc":
            pts = self.samples()
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def samples(self, step: float = 1.0) -> list[tuple[float, float]]:
        """Points along the segment, for clipping and matching."""
        if self.kind in ("line", "poly"):
            return list(self.pts)
        cx, cy = self.center
        if self.kind == "circle":
            a0, sweep = 0.0, 360.0
        else:
            a0, sweep = self.a0, (self.a1 - self.a0) % 360.0 or 360.0
        n = max(8, min(96, int(self.radius * math.radians(sweep) / step) + 2))
        return [(cx + self.radius * math.cos(math.radians(a0 + sweep * i / n)),
                 cy + self.radius * math.sin(math.radians(a0 + sweep * i / n))) for i in range(n + 1)]

    def to_json(self) -> dict[str, Any]:
        if self.kind == "circle":
            out: dict[str, Any] = {"k": "c", "c": [_r(self.center[0]), _r(self.center[1])], "r": _r(self.radius)}
        elif self.kind == "arc":
            out = {"k": "a", "c": [_r(self.center[0]), _r(self.center[1])], "r": _r(self.radius), "a": [_r(self.a0), _r(self.a1)]}
        else:
            out = {"k": "l" if self.kind == "line" else "p", "p": [_r(c) for p in self.pts for c in p]}
        if self.ref:
            out["ref"] = self.ref
        return out


def _angle(c, p) -> float:
    return math.degrees(math.atan2(p[1] - c[1], p[0] - c[0])) % 360.0


def _ccw_passes(c, a0: float, a1: float, mid) -> bool:
    sweep = (a1 - a0) % 360.0 or 360.0
    return (_angle(c, mid) - a0) % 360.0 <= sweep + 1e-6


def _arc_seg(c, r: float, start, end, mid, ref: str | None = None) -> Seg:
    a0, a1 = _angle(c, start), _angle(c, end)
    if not _ccw_passes(c, a0, a1, mid):
        a0, a1 = a1, a0
    return Seg("arc", [start, end], c, r, a0, a1, ref)


def _poly_seg(points: list[tuple[float, float]], ref: str | None = None) -> Seg:
    return Seg("poly", points, ref=ref)


def _samples(edge: Edge, n: int) -> list:
    return [edge.position_at(i / n) for i in range(n + 1)]


def _sample_count(length: float) -> int:
    return max(6, min(96, int(length / 1.5) + 3))


def edge2d_to_seg(edge: Edge) -> Seg:
    """A hidden-line edge (already in the view plane, z = 0) as a segment."""
    gt = edge.geom_type
    start = (float(edge.start_point().X), float(edge.start_point().Y))
    end = (float(edge.end_point().X), float(edge.end_point().Y))
    if gt == GeomType.LINE:
        return Seg("line", [start, end])
    if gt == GeomType.CIRCLE:
        c = (float(edge.arc_center.X), float(edge.arc_center.Y))
        r = float(edge.radius)
        if edge.is_closed:
            return Seg("circle", [], c, r)
        m = edge.position_at(0.5)
        return _arc_seg(c, r, start, end, (float(m.X), float(m.Y)))
    pts = [(float(p.X), float(p.Y)) for p in _samples(edge, _sample_count(float(edge.length)))]
    return _poly_seg(pts)


def project_edge(edge: Edge, frame: Frame) -> Seg | None:
    """A model edge projected into the view: a line, a circle or arc when its
    axis is along the view, else a sampled polyline. None for an edge seen end-on."""
    gt = edge.geom_type
    if gt == GeomType.LINE:
        a, b = frame.to2d(edge.start_point()), frame.to2d(edge.end_point())
        if math.dist(a, b) < 1e-6:
            return None
        return Seg("line", [a, b])
    if gt == GeomType.CIRCLE:
        axis = BRepAdaptor_Curve(edge.wrapped).Circle().Axis().Direction()
        ax = np.array([axis.X(), axis.Y(), axis.Z()])
        if abs(float(ax @ frame.dir)) > 0.9999:
            c = frame.to2d(edge.arc_center)
            r = float(edge.radius)
            if edge.is_closed:
                return Seg("circle", [], c, r)
            return _arc_seg(c, r, frame.to2d(edge.start_point()), frame.to2d(edge.end_point()), frame.to2d(edge.position_at(0.5)))
    pts = [frame.to2d(p) for p in _samples(edge, _sample_count(float(edge.length)))]
    if max(math.dist(pts[0], p) for p in pts) < 1e-6:
        return None
    return _poly_seg(pts)


def _shape_segs(compound) -> list[Seg]:
    try:
        edges = compound.edges()
    except Exception:  # noqa: BLE001 - an empty compound
        return []
    segs = [_seg_or_none(e) for e in edges]
    return [s for s in segs if s is not None]


def _seg_or_none(edge: Edge) -> Seg | None:
    try:
        return edge2d_to_seg(edge)
    except Exception:  # noqa: BLE001 - a degenerate edge from the projector
        return None


# --- hatching --------------------------------------------------------------------------

def _wire_polygon(wire, frame: Frame) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for e in wire.edges():
        seg = project_edge(e, frame)
        if seg is None:
            continue
        s = seg.samples()
        if pts and math.dist(pts[-1], s[0]) > math.dist(pts[-1], s[-1]):
            s = s[::-1]
        pts.extend(s if not pts else s[1:])
    return pts


def hatch_polygons(polygons: list[list[tuple[float, float]]], spacing: float, angle: float = 45.0) -> list[Seg]:
    """Lines at `angle` clipped to the region the polygons bound (even-odd rule)."""
    if not polygons:
        return []
    d = np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle))])
    n = np.array([-d[1], d[0]])
    pts = np.array([p for poly in polygons for p in poly])
    offs = pts @ n
    lo, hi = float(offs.min()), float(offs.max())
    edges = []
    for poly in polygons:
        if len(poly) < 3:
            continue
        closed = poly if math.dist(poly[0], poly[-1]) < 1e-9 else [*poly, poly[0]]
        edges.extend(pairwise(closed))
    out: list[Seg] = []
    k = math.floor(lo / spacing) + 1
    while k * spacing < hi:
        o = k * spacing + spacing * 0.013  # off the vertices
        k += 1
        ts: list[float] = []
        for p, q in edges:
            p, q = np.array(p), np.array(q)
            fp, fq = float(p @ n) - o, float(q @ n) - o
            if (fp < 0) == (fq < 0):
                continue
            s = fp / (fp - fq)
            x = p + (q - p) * s
            ts.append(float(x @ d))
        ts.sort()
        for i in range(0, len(ts) - 1, 2):
            a, b = ts[i], ts[i + 1]
            if b - a < 1e-6:
                continue
            pa, pb = n * o + d * a, n * o + d * b
            out.append(Seg("line", [(float(pa[0]), float(pa[1])), (float(pb[0]), float(pb[1]))]))
    return out


def _is_cap(face: Face, plane: Plane) -> bool:
    try:
        if face.geom_type != GeomType.PLANE:
            return False
        if abs(abs(float(face.normal_at().dot(plane.z_dir))) - 1.0) > 1e-4:
            return False
        return abs(float((face.center() - plane.origin).dot(plane.z_dir))) < 1e-4
    except Exception:  # noqa: BLE001
        return False


# --- views ------------------------------------------------------------------------------

@dataclass
class ViewResult:
    name: str
    direction: str
    at: tuple[float, float]
    scale: float
    hidden_lines: bool
    section: str | None
    offset: float
    flip: bool
    frame: Frame
    shape: Shape
    visible: list[Seg]
    hidden: list[Seg]
    hatch: list[Seg]
    bbox: tuple[float, float, float, float]  # view coordinates, model units
    plane: Plane | None = None
    look_from: np.ndarray | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2)

    def to_sheet(self, p: tuple[float, float]) -> tuple[float, float]:
        cx, cy = self.center
        return (self.at[0] + (p[0] - cx) * self.scale, self.at[1] + (p[1] - cy) * self.scale)

    def sheet_bbox(self) -> tuple[float, float, float, float]:
        x0, y0 = self.to_sheet((self.bbox[0], self.bbox[1]))
        x1, y1 = self.to_sheet((self.bbox[2], self.bbox[3]))
        return x0, y0, x1, y1

    def seg_to_sheet(self, seg: Seg) -> Seg:
        cx, cy = self.center
        return seg.transformed(self.scale, self.at[0] - cx * self.scale, self.at[1] - cy * self.scale)


def _section_plane(model: Model, section: Any, offset: float, what: str) -> Plane:
    if isinstance(section, str) and section in STANDARD_PLANES:
        base = STANDARD_PLANES[section]
    elif isinstance(section, str) and section in model.planes:
        base = model.planes[section]
    else:
        known = ", ".join(("XY", "XZ", "YZ", *model.planes))
        raise DrawingError(f"{what}: section={section!r} is not a standard plane or a plane of {model.path.name}; known: {known}")
    return base.offset(float(offset)) if offset else base


def _xy(value: Any, what: str) -> tuple[float, float]:
    try:
        x, y = (float(c) for c in value)
    except (TypeError, ValueError):
        raise DrawingError(f"{what}: at= must be (x, y) in sheet mm, got {value!r}") from None
    return x, y


def build_view(model: Model, name: str, args: dict[str, Any], sheet_scale_: float) -> ViewResult:
    what = f"view {name!r}"
    at = _xy(args.get("at", (0.0, 0.0)), what)
    scale = float(args["scale"]) if args.get("scale") is not None else sheet_scale_
    if scale <= 0:
        raise DrawingError(f"{what}: scale must be positive")
    section = args.get("section")
    offset = float(args.get("offset") or 0.0)
    flip = bool(args.get("flip"))
    hidden_arg = args.get("hidden")
    plane = None
    look_from_v: np.ndarray
    if section is not None:
        plane = _section_plane(model, section, offset, what)
        n = _np(plane.z_dir)
        look_from_v = -n if flip else n
        up = _np(plane.y_dir)
        keep = Keep.TOP if flip else Keep.BOTTOM
        try:
            shape = split(model.shape, bisect_by=plane, keep=keep)
        except Exception as exc:  # noqa: BLE001
            raise DrawingError(f"{what}: the section failed: {exc}") from None
        if shape is None or not shape.faces():
            raise DrawingError(f"{what}: the section plane leaves nothing to show")
        caps = [f for f in shape.faces() if _is_cap(f, plane)]
        direction = "section"
        with_hidden = bool(hidden_arg) if hidden_arg is not None else False
    else:
        direction = str(args.get("direction") or "front")
        if direction not in DIRECTIONS:
            raise DrawingError(f"{what}: direction must be one of {', '.join(d.upper() for d in DIRECTIONS)}, got {direction!r}")
        look_from_v, up = (_np(v) for v in DIRECTIONS[direction])
        shape = model.shape
        caps = []
        with_hidden = bool(hidden_arg) if hidden_arg is not None else True
    frame = frame_for(look_from_v, up, model.center)
    try:
        hlr = _HiddenLines(shape, look_at=Vector(*model.center), look_from=tuple(look_from_v), look_up=tuple(up),
                           with_hidden=with_hidden)
    except Exception as exc:  # noqa: BLE001
        raise DrawingError(f"{what}: the projection failed: {exc}") from None
    visible = _shape_segs(hlr.visible_lines)
    hidden = _shape_segs(hlr.hidden_lines) if with_hidden else []
    if not visible:
        raise DrawingError(f"{what}: nothing projects into this view")
    hatch: list[Seg] = []
    if caps:
        polys = []
        for f in caps:
            polys.append(_wire_polygon(f.outer_wire(), frame))
            polys.extend(_wire_polygon(w, frame) for w in f.inner_wires())
        hatch = hatch_polygons([p for p in polys if len(p) >= 3], HATCH_SPACING / scale)
    boxes = [s.bbox() for s in (*visible, *hidden)]
    bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    warnings = ["the section plane does not cut the model"] if section is not None and not caps else []
    return ViewResult(name, direction, at, scale, with_hidden, str(section) if section is not None else None, offset, flip,
                      frame, shape, visible, hidden, hatch, bbox, plane, look_from_v, warnings)


# --- which model edge a projected segment came from, for picking -----------------------

def _on_line(p, a, b, tol: float) -> bool:
    ab = (b[0] - a[0], b[1] - a[1])
    L2 = ab[0] ** 2 + ab[1] ** 2
    if L2 < 1e-18:
        return math.dist(p, a) < tol
    t = ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / L2
    if t < -tol / math.sqrt(L2) or t > 1 + tol / math.sqrt(L2):
        return False
    foot = (a[0] + ab[0] * t, a[1] + ab[1] * t)
    return math.dist(p, foot) < tol


def _near_polyline(p, pts, tol: float) -> bool:
    return any(_on_line(p, a, b, tol) for a, b in pairwise(pts))


def _same_curve(seg: Seg, model_seg: Seg, tol: float) -> bool:
    if seg.kind == "line":
        if model_seg.kind == "line":
            a, b = model_seg.pts
            return _on_line(seg.pts[0], a, b, tol) and _on_line(seg.pts[1], a, b, tol)
        if model_seg.kind == "poly":
            return _near_polyline(seg.pts[0], model_seg.pts, tol) and _near_polyline(seg.pts[1], model_seg.pts, tol)
        return False
    if seg.kind in ("circle", "arc"):
        if model_seg.kind not in ("circle", "arc"):
            return False
        return math.dist(seg.center, model_seg.center) < tol and abs(seg.radius - model_seg.radius) < tol
    if model_seg.kind == "poly":
        pts = seg.samples()
        probe = pts[:: max(1, len(pts) // 6)] + [pts[-1]]
        return all(_near_polyline(p, model_seg.pts, tol) for p in probe)
    if model_seg.kind in ("circle", "arc"):
        pts = seg.samples()
        probe = pts[:: max(1, len(pts) // 6)] + [pts[-1]]
        return all(abs(math.dist(p, model_seg.center) - model_seg.radius) < tol for p in probe)
    return False


def _unique_tags(index: int, model: Model) -> list[str] | None:
    """The smallest set of tags that picks this edge alone among its owner's, or None."""
    owner = model.edge_labels[index]
    mine = list(model.edge_tags[index])
    if not owner or not mine:
        return None
    peers = [t for o, t in zip(model.edge_labels, model.edge_tags, strict=True) if o == owner]

    def unique(subset: list[str]) -> bool:
        return sum(1 for tags in peers if all(t in tags for t in subset)) == 1

    for t in mine:
        if unique([t]):
            return [t]
    for i in range(len(mine)):
        for j in range(i + 1, len(mine)):
            if unique([mine[i], mine[j]]):
                return [mine[i], mine[j]]
    return mine if unique(mine) else None


def _fmt_coord(v: float) -> str:
    return _num(v, 3)


def edge_selector(view: str, index: int, model: Model, edges: list[Edge]) -> str:
    """A selector for model edge `index` through the view: semantic when the labels
    make it unique, else by the nearest point in model coordinates."""
    owner = model.edge_labels[index] if index < len(model.edge_labels) else ""
    tags = _unique_tags(index, model) if owner else None
    if tags is not None:
        ops = "".join(f".{t[1:]}" if t.startswith(":") else f".from_sketch({json.dumps(t)})" for t in tags)
        return f"{view}.edges.of({json.dumps(owner)}){ops}"
    c = edges[index].center()
    return f"{view}.edges.nearest(({_fmt_coord(c.X)}, {_fmt_coord(c.Y)}, {_fmt_coord(c.Z)}))"


def _project_or_none(edge: Edge, frame: Frame) -> Seg | None:
    try:
        return project_edge(edge, frame)
    except Exception:  # noqa: BLE001 - a degenerate model edge
        return None


def attach_refs(view: ViewResult, model: Model) -> None:
    """Give each visible segment of a plain view the selector of the model edge it shows."""
    if view.section is not None:
        return
    edges = model.shape.edges()
    if len(edges) > MAX_PICKABLE_EDGES:
        return
    projected: list[tuple[int, Seg, float]] = []
    for i, e in enumerate(edges):
        seg = _project_or_none(e, view.frame)
        if seg is not None:
            projected.append((i, seg, view.frame.depth(e.center())))
    tol = 1e-3 * max(1.0, max(view.bbox[2] - view.bbox[0], view.bbox[3] - view.bbox[1]))
    texts: dict[int, str] = {}
    for seg in view.visible:
        best: tuple[int, float] | None = None
        for i, pseg, depth in projected:
            if (best is None or depth > best[1]) and _same_curve(seg, pseg, tol):
                best = (i, depth)
        if best is not None:
            if best[0] not in texts:
                texts[best[0]] = edge_selector(view.name, best[0], model, edges)
            seg.ref = texts[best[0]]


# --- dimensions ---------------------------------------------------------------------------

@dataclass
class Ref2D:
    kind: str  # point | line | circle
    p: tuple[float, float]
    q: tuple[float, float] | None = None  # line end
    r: float = 0.0
    a0: float = 0.0
    a1: float = 360.0
    text: str = ""

    def scaled(self, view: ViewResult) -> Ref2D:
        return Ref2D(self.kind, view.to_sheet(self.p), view.to_sheet(self.q) if self.q else None,
                     self.r * view.scale, self.a0, self.a1, self.text)


def _extreme_points(points: list[tuple[float, float]]) -> tuple[tuple[float, float], tuple[float, float]]:
    """The two points furthest apart (a set of collinear points as a line)."""
    best = (points[0], points[0], -1.0)
    for i, a in enumerate(points):
        for b in points[i + 1:]:
            d = math.dist(a, b)
            if d > best[2]:
                best = (a, b, d)
    return best[0], best[1]


def resolve_ref(ref: dict[str, Any], model: Model, view: ViewResult, what: str) -> Ref2D:
    """One face, edge or vertex of the model as a point, line or circle in the view."""
    selector = Selector.from_json(ref["selector"])
    text = selector.ref_name
    identity = None
    if model.identity is not None:
        identity = Identity(owner=dict.fromkeys(model.identity.groups, selector.feature), tags=model.identity.tags,
                            groups=model.identity.groups)
    try:
        shapes = resolve(selector, model.shape, identity, many=False)
    except SelectorError as exc:
        raise DrawingError(f"{what}: {exc}") from None
    if len(shapes) != 1:
        raise DrawingError(f"{what}: {text} must pick one face, edge or vertex, it picks {len(shapes)}")
    s = shapes[0]
    fr = view.frame
    if isinstance(s, Vertex):
        return Ref2D("point", fr.to2d(s), text=text)
    if isinstance(s, Edge):
        if s.geom_type == GeomType.LINE:
            a, b = fr.to2d(s.start_point()), fr.to2d(s.end_point())
            if math.dist(a, b) < 1e-6:
                return Ref2D("point", a, text=text)
            return Ref2D("line", a, b, text=text)
        if s.geom_type == GeomType.CIRCLE:
            axis = BRepAdaptor_Curve(s.wrapped).Circle().Axis().Direction()
            ax = np.array([axis.X(), axis.Y(), axis.Z()])
            along = float(ax @ fr.dir)
            c = fr.to2d(s.arc_center)
            r = float(s.radius)
            if abs(along) > 0.9999:
                if s.is_closed:
                    return Ref2D("circle", c, r=r, text=text)
                seg = _arc_seg(c, r, fr.to2d(s.start_point()), fr.to2d(s.end_point()), fr.to2d(s.position_at(0.5)))
                return Ref2D("circle", c, r=r, a0=seg.a0, a1=seg.a1, text=text)
            if abs(along) < 1e-3:
                u = _unit(np.cross(ax, fr.dir))
                d = fr.dir2d(u)
                return Ref2D("line", (c[0] - d[0] * r, c[1] - d[1] * r), (c[0] + d[0] * r, c[1] + d[1] * r), text=text)
            raise DrawingError(f"{what}: {text} is a circle seen at an angle in view {view.name!r}")
        pts = [fr.to2d(p) for p in _samples(s, 8)]
        a, b = _extreme_points(pts)
        return Ref2D("line", a, b, text=text) if math.dist(a, b) > 1e-6 else Ref2D("point", a, text=text)
    if isinstance(s, Face):
        ad = BRepAdaptor_Surface(s.wrapped)
        st = ad.GetType()
        if st == GeomAbs_SurfaceType.GeomAbs_Plane:
            n = _np(s.normal_at())
            along = float(n @ fr.dir)
            if abs(along) < 1e-3:
                pts = [fr.to2d(v) for v in s.vertices()] or [fr.to2d(p) for e in s.edges() for p in _samples(e, 4)]
                a, b = _extreme_points(pts)
                return Ref2D("line", a, b, text=text)
            if abs(along) > 0.9999:
                return Ref2D("point", fr.to2d(s.center()), text=text)
            raise DrawingError(f"{what}: {text} is a face seen at an angle in view {view.name!r}; dimension an edge instead")
        if st == GeomAbs_SurfaceType.GeomAbs_Cylinder:
            cyl = ad.Cylinder()
            axis = cyl.Axis()
            ax = np.array([axis.Direction().X(), axis.Direction().Y(), axis.Direction().Z()])
            o = np.array([axis.Location().X(), axis.Location().Y(), axis.Location().Z()])
            along = float(ax @ fr.dir)
            if abs(along) > 0.9999:
                return Ref2D("circle", fr.to2d(o), r=float(cyl.Radius()), text=text)
            if abs(along) < 1e-3:
                ts = [float((_np(v) - o) @ ax) for v in s.vertices()]
                if not ts:
                    ts = [-1.0, 1.0]
                return Ref2D("line", fr.to2d(o + ax * min(ts)), fr.to2d(o + ax * max(ts)), text=text)
            raise DrawingError(f"{what}: {text} is a cylinder seen at an angle in view {view.name!r}")
        if st == GeomAbs_SurfaceType.GeomAbs_Sphere:
            sph = ad.Sphere()
            c = sph.Location()
            return Ref2D("circle", fr.to2d((c.X(), c.Y(), c.Z())), r=float(sph.Radius()), text=text)
        return Ref2D("point", fr.to2d(s.center()), text=text)
    raise DrawingError(f"{what}: {text} is not a face, edge or vertex")


@dataclass
class DimResult:
    name: str
    view: str
    kind: str
    value: float
    text: str
    lines: list[tuple[float, float, float, float]] = field(default_factory=list)
    arrows: list[tuple[float, float, float, float]] = field(default_factory=list)  # tip x, y, direction to the tail
    arc: tuple[float, float, float, float, float] | None = None  # cx, cy, r, a0, a1
    label: dict[str, Any] = field(default_factory=dict)  # at, text, angle, anchor
    at: tuple[float, float] = (0.0, 0.0)  # placement, sheet coordinates
    refs: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name, "view": self.view, "kind": self.kind, "value": round(self.value, 4), "text": self.text,
            "lines": [[_r(c) for c in ln] for ln in self.lines], "arrows": [[_r(c) for c in a] for a in self.arrows],
            "arc": [_r(c) for c in self.arc] if self.arc else None,
            "label": {**self.label, "at": [_r(c) for c in self.label["at"]]} if self.label else None,
            "at": [_r(self.at[0]), _r(self.at[1])], "refs": list(self.refs),
        }


def _perp(u: np.ndarray) -> np.ndarray:
    return np.array([-u[1], u[0]])


def _reading(u: np.ndarray) -> np.ndarray:
    """Flip a direction so text along it reads left to right, or bottom to top."""
    if u[0] < -1e-9 or (abs(u[0]) < 1e-9 and u[1] < 0):
        return -u
    return u


def _foot(p: np.ndarray, a: np.ndarray, b: np.ndarray, clamp: bool = True) -> np.ndarray:
    ab = b - a
    L2 = float(ab @ ab)
    t = float((p - a) @ ab) / L2 if L2 > 1e-18 else 0.0
    if clamp:
        t = min(1.0, max(0.0, t))
    return a + ab * t


def _label(at, text: str, angle: float = 0.0, anchor: str = "middle") -> dict[str, Any]:
    return {"at": (float(at[0]), float(at[1])), "text": text, "angle": round(float(angle), 3), "anchor": anchor}


def _linear(A: Ref2D, B: Ref2D, along: str | None, at: np.ndarray, scale: float, what: str) -> tuple[float, DimResult]:
    pa, pb = np.array(A.p), np.array(B.p)
    if A.kind == "line" and B.kind == "line":
        ua, ub = _unit(np.array(A.q) - pa), _unit(np.array(B.q) - pb)
        if abs(float(ua[0] * ub[1] - ua[1] * ub[0])) > 1e-3:
            raise DrawingError(f"{what}: {A.text} and {B.text} are not parallel in this view; use kind=\"angle\"")
        u = _perp(ua)
        pa, pb = _foot(at, pa, np.array(A.q)), _foot(at, pb, np.array(B.q))
    elif A.kind == "line" or B.kind == "line":
        line, other = (A, B) if A.kind == "line" else (B, A)
        la, lb = np.array(line.p), np.array(line.q)
        u = _perp(_unit(lb - la))
        pt = np.array(other.p)
        pl = _foot(at, la, lb)
        pa, pb = (pt, pl) if A.kind != "line" else (pl, pt)
    else:
        u = _unit(pb - pa)
        if float(np.linalg.norm(pb - pa)) < 1e-9:
            raise DrawingError(f"{what}: {A.text} and {B.text} project to the same point in this view")
    if along in ("x", "y"):
        u = np.array([1.0, 0.0]) if along == "x" else np.array([0.0, 1.0])
    elif along is not None:
        raise DrawingError(f"{what}: along= must be \"x\" or \"y\", got {along!r}")
    u = _reading(_unit(u))
    n = _perp(u)
    ea = at + u * float((pa - at) @ u)
    eb = at + u * float((pb - at) @ u)
    if float((eb - ea) @ u) < 0:
        ea, eb, pa, pb = eb, ea, pb, pa
    span = float((eb - ea) @ u)
    value = span / scale
    lines: list[tuple[float, float, float, float]] = []
    arrows: list[tuple[float, float, float, float]] = []
    if span > 3 * ARROW:
        lines.append((*ea, *eb))
        arrows += [(*ea, *u), (*eb, *(-u))]
    else:
        lines.append((*(ea - u * ARROW * 2), *(eb + u * ARROW * 2)))
        arrows += [(*ea, *(-u)), (*eb, *u)]
    for p, e in ((pa, ea), (pb, eb)):
        d = e - p
        L = float(np.linalg.norm(d))
        if L > GAP + 0.2:
            d = d / L
            lines.append((*(p + d * GAP), *(e + d * OVERSHOOT)))
    mid = (ea + eb) / 2 + n * (TEXT * 0.5 + 0.6)
    angle = math.degrees(math.atan2(u[1], u[0]))
    return value, DimResult("", "", "distance", value, "", lines, arrows, None, _label(mid, "", angle), tuple(at))


def _leader(c: np.ndarray, r: float, at: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    d = at - c
    u = _unit(d) if float(np.linalg.norm(d)) > 1e-9 else np.array([1.0, 0.0])
    return u, c + u * r


def _radial(A: Ref2D, kind: str, at: np.ndarray, scale: float, what: str) -> tuple[float, DimResult]:
    if A.kind != "circle":
        raise DrawingError(f"{what}: a {kind} dimension needs a circular edge or a round face, {A.text} is not one in this view")
    c = np.array(A.p)
    u, tip = _leader(c, A.r, at)
    value = (2 * A.r if kind == "diameter" else A.r) / scale
    outside = float(np.linalg.norm(at - c)) > A.r
    start = at if outside else c
    lines = [(*start, *tip)]
    arrows = [(*tip, *(u if outside else -u))]
    anchor = "start" if u[0] >= 0 else "end"
    shoulder = np.array([0.8 if u[0] >= 0 else -0.8, -TEXT * 0.35])
    return value, DimResult("", "", kind, value, "", lines, arrows, None, _label(at + shoulder, "", 0.0, anchor), tuple(at))


def _angular(A: Ref2D, B: Ref2D, at: np.ndarray, what: str) -> tuple[float, DimResult]:
    if A.kind != "line" or B.kind != "line":
        raise DrawingError(f"{what}: an angle dimension needs two straight edges or edge-on faces")
    pa, qa, pb, qb = (np.array(v) for v in (A.p, A.q, B.p, B.q))
    da, db = _unit(qa - pa), _unit(qb - pb)
    cross = float(da[0] * db[1] - da[1] * db[0])
    if abs(cross) < 1e-6:
        raise DrawingError(f"{what}: {A.text} and {B.text} are parallel; use a distance dimension")
    t = float((pb - pa)[0] * db[1] - (pb - pa)[1] * db[0]) / cross
    v = pa + da * t
    r = max(5.0, float(np.linalg.norm(at - v)))
    toward = _unit(at - v)
    best = max(((sa, sb) for sa in (1, -1) for sb in (1, -1)), key=lambda s: float(_unit(s[0] * da + s[1] * db) @ toward))
    d1, d2 = best[0] * da, best[1] * db
    a1, a2 = math.degrees(math.atan2(d1[1], d1[0])) % 360, math.degrees(math.atan2(d2[1], d2[0])) % 360
    if (a2 - a1) % 360 > 180:
        a1, a2, d1, d2 = a2, a1, d2, d1
    value = math.degrees(math.acos(max(-1.0, min(1.0, float(d1 @ d2)))))
    lines: list[tuple[float, float, float, float]] = []
    for d, p, q in ((d1, pa, qa), (d2, pb, qb)):
        e = v + d * r
        seg = q - p
        L2 = float(seg @ seg)
        tt = float((e - p) @ seg) / L2 if L2 > 1e-18 else 0.0
        if tt < 0 or tt > 1:
            near = p if tt < 0 else q
            dd = _unit(e - near)
            lines.append((*(near + dd * GAP), *(e + dd * OVERSHOOT)))
    t1 = np.array([-math.sin(math.radians(a1)), math.cos(math.radians(a1))])
    t2 = np.array([math.sin(math.radians(a2)), -math.cos(math.radians(a2))])
    arrows = [(*(v + d1 * r), *t1), (*(v + d2 * r), *t2)]
    bis = _unit(d1 + d2)
    text_at = v + bis * (r + TEXT * 0.9) + np.array([0.0, -TEXT * 0.35])
    return value, DimResult("", "", "angle", value, "", lines, arrows, (float(v[0]), float(v[1]), r, a1, a2),
                            _label(text_at, ""), tuple(at))


def build_dimension(model: Model, name: str, args: dict[str, Any], views: dict[str, ViewResult]) -> DimResult:
    what = f"dimension {name!r}"
    a, b = args.get("a"), args.get("b")
    if not a:
        raise DrawingError(f"{what}: needs a reference through a view, such as front.edges.of(\"body\").top")
    sel_a = Selector.from_json(a["selector"])
    view = views.get(sel_a.feature)
    if view is None:
        raise DrawingError(f"{what}: {sel_a.ref_name} refers to {sel_a.feature!r}, which is not a view above it that evaluated")
    if b is not None and Selector.from_json(b["selector"]).feature != view.name:
        raise DrawingError(f"{what}: both references must be in view {view.name!r}")
    kind = args.get("kind") or ("distance" if b is not None else "diameter")
    if kind not in DIMENSION_KINDS:
        raise DrawingError(f"{what}: kind must be one of {', '.join(DIMENSION_KINDS)}, got {kind!r}")
    if kind in ("distance", "angle") and b is None:
        raise DrawingError(f"{what}: a {kind} dimension needs two references")
    if kind in ("diameter", "radius") and b is not None:
        raise DrawingError(f"{what}: a {kind} dimension takes one reference")
    rel = _xy(args.get("at", (0.0, 0.0)), what)
    at = np.array([view.at[0] + rel[0], view.at[1] + rel[1]])
    A = resolve_ref(a, model, view, what).scaled(view)
    B = resolve_ref(b, model, view, what).scaled(view) if b is not None else None
    if kind == "distance":
        value, dim = _linear(A, B, args.get("along"), at, view.scale, what)
        text = _num(value)
    elif kind == "angle":
        value, dim = _angular(A, B, at, what)
        text = _num(value, 1) + "°"
    else:
        value, dim = _radial(A, kind, at, view.scale, what)
        text = ("Ø" if kind == "diameter" else "R") + _num(value)
    if args.get("text"):
        text = str(args["text"])
    dim.name, dim.view, dim.text = name, view.name, text
    dim.label["text"] = text
    dim.refs = [A.text] + ([B.text] if B else [])
    return dim


# --- notes, the state, the layout -----------------------------------------------------------

@dataclass
class NoteResult:
    name: str
    text: str
    at: tuple[float, float]
    size: float
    view: str | None

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "text": self.text, "at": [_r(self.at[0]), _r(self.at[1])], "size": self.size, "view": self.view}


def build_note(name: str, args: dict[str, Any], views: dict[str, ViewResult]) -> NoteResult:
    what = f"note {name!r}"
    text = str(args.get("text") or "")
    if not text.strip():
        raise DrawingError(f"{what}: the note is empty")
    at = _xy(args.get("at", (0.0, 0.0)), what)
    size = float(args.get("size") or TEXT)
    if size <= 0:
        raise DrawingError(f"{what}: size must be positive")
    view = args.get("view")
    if view is not None:
        v = views.get(str(view))
        if v is None:
            raise DrawingError(f"{what}: view {view!r} is not defined above it")
        at = (v.at[0] + at[0], v.at[1] + at[1])
    return NoteResult(name, text, at, size, str(view) if view is not None else None)


@dataclass
class DrawingState:
    model: Model | None = None
    error: str | None = None
    views: dict[str, ViewResult] = field(default_factory=dict)
    dimensions: list[DimResult] = field(default_factory=list)
    notes: list[NoteResult] = field(default_factory=list)

    def copy(self) -> DrawingState:
        return DrawingState(self.model, self.error, dict(self.views), list(self.dimensions), list(self.notes))


def _scale_text(scale: float) -> str:
    """1:2, 2:1, 3:4: the ratio a title block shows."""
    from fractions import Fraction

    f = Fraction(scale).limit_denominator(50)
    if f.numerator > 0 and abs(float(f) - scale) < 1e-6:
        return f"{f.numerator}:{f.denominator}"
    return f"{_num(scale)}:1" if scale >= 1 else f"1:{_num(1 / scale)}"


def _title_block(width: float, height: float, meta: dict[str, Any], model: Model | None, scale: float, sheet: str):
    lines: list[tuple[float, float, float, float]] = []
    texts: list[dict[str, Any]] = []
    m = MARGIN
    lines += [(m, m, width - m, m), (width - m, m, width - m, height - m), (width - m, height - m, m, height - m), (m, height - m, m, m)]
    x0, y0 = width - m - TITLE_W, m
    split_x = x0 + TITLE_W * 0.64
    lines += [(x0, y0, x0, y0 + TITLE_H), (x0, y0 + TITLE_H, x0 + TITLE_W, y0 + TITLE_H),
              (x0, y0 + TITLE_H / 3, x0 + TITLE_W, y0 + TITLE_H / 3), (x0, y0 + 2 * TITLE_H / 3, x0 + TITLE_W, y0 + 2 * TITLE_H / 3),
              (split_x, y0, split_x, y0 + TITLE_H)]
    mm = model.meta if model else {}
    title = str(meta.get("title") or (model.name if model else meta.get("name") or ""))
    part = model.name if model else str(meta.get("of") or "")

    def field_(k: str) -> str:
        return str(meta.get(k) or mm.get(k) or "")

    row = TITLE_H / 3
    small = 2.2

    def cell(x, y, label: str, value: str, size: float = TEXT * 0.8):
        texts.append({"at": [x + 1.5, y + row - small - 0.5], "text": label, "size": small, "anchor": "start", "angle": 0, "layer": "frame"})
        texts.append({"at": [x + 1.5, y + 1.4], "text": value, "size": size, "anchor": "start", "angle": 0, "layer": "frame"})

    cell(x0, y0 + 2 * row, "TITLE", title, TEXT)
    cell(split_x, y0 + 2 * row, "REV", field_("revision"))
    cell(x0, y0 + row, "PART", part + (f"  ·  {field_('material')}" if field_("material") else ""))
    cell(split_x, y0 + row, "SCALE", _scale_text(scale))
    cell(x0, y0, "AUTHOR", field_("author") + (f"  ·  {field_('date')}" if field_("date") else ""))
    cell(split_x, y0, "SHEET", f"{sheet} · mm")
    return lines, texts


def _traces(views: dict[str, ViewResult], letters: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """The cutting-plane trace of every section view on each view that sees its plane edge-on."""
    out: dict[str, list[dict[str, Any]]] = {}
    for s in views.values():
        if s.plane is None or s.look_from is None:
            continue
        n = _np(s.plane.z_dir)
        o = _np(s.plane.origin)
        for v in views.values():
            if v is s or v.section is not None or abs(float(n @ v.frame.dir)) > 1e-3:
                continue
            t = _unit(np.cross(n, v.frame.dir))
            t2 = np.array(v.frame.dir2d(t))
            o2 = np.array(v.frame.to2d(o))
            a2 = _unit(np.array(v.frame.dir2d(s.look_from)))
            x0, y0, x1, y1 = v.bbox
            pad = 3.0 / v.scale
            ts = []
            for k in (0, 1):
                if abs(t2[k]) > 1e-9:
                    for bound in ((x0 - pad, x1 + pad) if k == 0 else (y0 - pad, y1 + pad)):
                        ts.append((bound - o2[k]) / t2[k])
            if len(ts) < 2:
                continue
            lo, hi = min(ts), max(ts)
            p0, p1 = o2 + t2 * lo, o2 + t2 * hi
            arrows, texts, lines = [], [], []
            side = _unit(np.array(v.frame.dir2d(t)))
            for p, k in ((p0, -1.0), (p1, 1.0)):
                sp = np.array(v.to_sheet(tuple(p)))
                tip = sp + a2 * 3.5
                lines.append((*sp, *tip))
                arrows.append((*tip, *(-a2)))
                # the letter beside the arrow, away from the view
                texts.append({"at": [float(c) for c in (sp + a2 * 1.5 + side * k * 2.2 + np.array([0.0, -TEXT * 0.35]))],
                              "text": letters[s.name], "size": TEXT, "anchor": "middle", "angle": 0})
            sp0, sp1 = v.to_sheet(tuple(p0)), v.to_sheet(tuple(p1))
            out.setdefault(v.name, []).append({
                "section": s.name, "letter": letters[s.name], "line": [_r(c) for c in (*sp0, *sp1)],
                "lines": [[_r(c) for c in ln] for ln in lines], "arrows": [[_r(c) for c in a] for a in arrows], "texts": texts,
            })
    return out


def layout(ev) -> dict[str, Any]:
    """The whole sheet as one 2D scene in sheet millimetres."""
    doc = ev.document
    st: DrawingState = ev.drawing
    meta = doc.meta
    try:
        sheet, width, height = sheet_size(meta)
        scale = sheet_scale(meta)
    except DrawingError as exc:
        sheet, width, height, scale = "A4", *SHEETS["A4"], 1.0
        st.error = st.error or str(exc)
    model = st.model
    frame_lines, texts = _title_block(width, height, meta, model, scale, sheet)
    letters: dict[str, str] = {}
    for v in st.views.values():
        if v.section is not None:
            letters[v.name] = chr(ord("A") + len(letters))
    traces = _traces(st.views, letters)
    views = []
    for v in st.views.values():
        label = None
        if v.section is not None:
            label = f"SECTION {letters[v.name]}-{letters[v.name]}"
        if abs(v.scale - scale) > 1e-9:
            label = f"{label + '  ' if label else ''}SCALE {_scale_text(v.scale)}"
        bx = v.sheet_bbox()
        views.append({
            "name": v.name, "direction": v.direction, "at": [_r(v.at[0]), _r(v.at[1])], "scale": v.scale,
            "section": v.section, "offset": v.offset, "flip": v.flip, "hidden_lines": v.hidden_lines,
            "bbox": [_r(c) for c in bx], "label": label,
            "label_at": [_r((bx[0] + bx[2]) / 2), _r(bx[1] - TEXT * 1.8)] if label else None,
            "visible": [v.seg_to_sheet(s).to_json() for s in v.visible],
            "hidden": [v.seg_to_sheet(s).to_json() for s in v.hidden],
            "hatch": [v.seg_to_sheet(s).to_json() for s in v.hatch],
            "traces": traces.get(v.name, []),
        })
    return {
        "sheet": {"size": sheet, "width": width, "height": height, "scale": scale},
        "model": {"path": str(meta.get("of") or ""), "name": model.name if model else None, "kind": model.kind if model else None,
                  "error": st.error, "planes": list(model.planes) if model else [],
                  "meta": {k: str(model.meta[k]) for k in ("name", "material", "revision", "author", "date") if model and model.meta.get(k)}},
        "frame": {"lines": [[_r(c) for c in ln] for ln in frame_lines], "texts": texts},
        "views": views,
        "dimensions": [d.to_json() for d in st.dimensions],
        "notes": [n.to_json() for n in st.notes],
    }


def summary(ev) -> dict[str, Any]:
    """Golden numbers of a drawing: segment counts and extents per view, dimension values."""
    scene = layout(ev)
    return {
        "sheet": scene["sheet"],
        "views": {v["name"]: {"visible": len(v["visible"]), "hidden": len(v["hidden"]), "hatch": len(v["hatch"]),
                              "bbox": v["bbox"], "refs": sum(1 for s in v["visible"] if s.get("ref"))} for v in scene["views"]},
        "dimensions": {d["name"]: {"kind": d["kind"], "value": d["value"], "text": d["text"]} for d in scene["dimensions"]},
        "notes": len(scene["notes"]),
    }


# --- export -----------------------------------------------------------------------------------

def _texts(scene: dict[str, Any]):
    """Every text on the sheet: (x, y, text, size, anchor, angle, layer)."""
    for t in scene["frame"]["texts"]:
        yield (*t["at"], t["text"], t["size"], t["anchor"], t.get("angle", 0), "frame")
    for v in scene["views"]:
        if v["label"]:
            yield (*v["label_at"], v["label"], TEXT, "middle", 0, "views")
        for tr in v["traces"]:
            for t in tr["texts"]:
                yield (*t["at"], t["text"], t["size"], t["anchor"], 0, "section")
    for d in scene["dimensions"]:
        if d["label"]:
            yield (*d["label"]["at"], d["label"]["text"], TEXT, d["label"]["anchor"], d["label"]["angle"], "dimensions")
    for n in scene["notes"]:
        for i, line in enumerate(n["text"].split("\n")):
            yield (n["at"][0], n["at"][1] - i * n["size"] * 1.5, line, n["size"], "start", 0, "notes")


def _arrow_triangle(a) -> list[tuple[float, float]]:
    x, y, dx, dy = a
    tail = (x + dx * ARROW, y + dy * ARROW)
    w = ARROW * 0.18
    return [(x, y), (tail[0] - dy * w, tail[1] + dx * w), (tail[0] + dy * w, tail[1] - dx * w)]


def _segments(scene: dict[str, Any]):
    """Every stroked segment: (layer, seg json)."""
    for ln in scene["frame"]["lines"]:
        yield "frame", {"k": "l", "p": ln}
    for v in scene["views"]:
        for s in v["visible"]:
            yield "visible", s
        for s in v["hidden"]:
            yield "hidden", s
        for s in v["hatch"]:
            yield "hatch", s
        for tr in v["traces"]:
            yield "section", {"k": "l", "p": tr["line"]}
            for ln in tr["lines"]:
                yield "section", {"k": "l", "p": ln}
    for d in scene["dimensions"]:
        for ln in d["lines"]:
            yield "dimensions", {"k": "l", "p": ln}
        if d["arc"]:
            cx, cy, r, a0, a1 = d["arc"]
            yield "dimensions", {"k": "a", "c": [cx, cy], "r": r, "a": [a0, a1]}


def _arrows(scene: dict[str, Any]):
    for v in scene["views"]:
        for tr in v["traces"]:
            for a in tr["arrows"]:
                yield "section", a
    for d in scene["dimensions"]:
        for a in d["arrows"]:
            yield "dimensions", a


LAYERS = {
    "frame": {"width": 0.5, "color": "#000000", "dash": None},
    "visible": {"width": 0.5, "color": "#000000", "dash": None},
    "hidden": {"width": 0.25, "color": "#606060", "dash": (2.0, 1.0)},
    "hatch": {"width": 0.18, "color": "#404040", "dash": None},
    "section": {"width": 0.5, "color": "#000000", "dash": (6.0, 1.5, 1.5, 1.5)},
    "dimensions": {"width": 0.25, "color": "#000000", "dash": None},
    "views": {"width": 0.25, "color": "#000000", "dash": None},
    "notes": {"width": 0.25, "color": "#000000", "dash": None},
}
FONT_EM = 1 / 0.72  # font size per cap height for Helvetica


def _arc_path_svg(cx, cy, r, a0, a1) -> str:
    sweep = (a1 - a0) % 360.0 or 360.0
    x0, y0 = cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))
    x1, y1 = cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))
    return f"M {x0:.3f} {y0:.3f} A {r:.3f} {r:.3f} 0 {1 if sweep > 180 else 0} 1 {x1:.3f} {y1:.3f}"


def to_svg(scene: dict[str, Any]) -> str:
    w, h = scene["sheet"]["width"], scene["sheet"]["height"]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}" font-family="Helvetica, Arial, sans-serif">',
           '<rect width="100%" height="100%" fill="white"/>', f'<g transform="translate(0 {h}) scale(1 -1)" fill="none" stroke-linecap="round">']
    by_layer: dict[str, list[str]] = {}
    for layer, s in _segments(scene):
        if s["k"] == "l":
            x0, y0, x1, y1 = s["p"]
            el = f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}"/>'
        elif s["k"] == "c":
            el = f'<circle cx="{s["c"][0]}" cy="{s["c"][1]}" r="{s["r"]}"/>'
        elif s["k"] == "a":
            el = f'<path d="{_arc_path_svg(*s["c"], s["r"], *s["a"])}"/>'
        else:
            pts = " ".join(f"{s['p'][i]},{s['p'][i + 1]}" for i in range(0, len(s["p"]), 2))
            el = f'<polyline points="{pts}"/>'
        by_layer.setdefault(layer, []).append(el)
    for layer, a in _arrows(scene):
        tri = " ".join(f"{x:.3f},{y:.3f}" for x, y in _arrow_triangle(a))
        by_layer.setdefault(layer, []).append(f'<polygon points="{tri}" fill="{LAYERS[layer]["color"]}"/>')
    for layer, els in by_layer.items():
        style = LAYERS[layer]
        dash = f' stroke-dasharray="{" ".join(str(d) for d in style["dash"])}"' if style["dash"] else ""
        out.append(f'<g id="{layer}" stroke="{style["color"]}" stroke-width="{style["width"]}"{dash}>')
        out.extend(els)
        out.append("</g>")
    out.append("</g>")
    for x, y, text, size, anchor, angle, layer in _texts(scene):
        esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        rot = f" rotate({-angle:.3f})" if angle else ""
        out.append(f'<text transform="translate({x:.3f} {h - y:.3f}){rot}" font-size="{size * FONT_EM:.3f}" text-anchor="{anchor}" fill="#000" class="{layer}">{esc}</text>')
    out.append("</svg>")
    return "\n".join(out)


def to_dxf(scene: dict[str, Any], path: Path) -> None:
    import ezdxf
    from ezdxf.enums import TextEntityAlignment

    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    colors = {"frame": 7, "visible": 7, "hidden": 8, "hatch": 8, "section": 7, "dimensions": 7, "views": 7, "notes": 7}
    for layer, style in LAYERS.items():
        lt = "DASHED" if layer == "hidden" else "DASHDOT" if layer == "section" else "CONTINUOUS"
        doc.layers.add(layer, color=colors[layer], linetype=lt, lineweight=int(style["width"] * 100))
    msp = doc.modelspace()
    for layer, s in _segments(scene):
        attrs = {"layer": layer}
        if s["k"] == "l":
            x0, y0, x1, y1 = s["p"]
            msp.add_line((x0, y0), (x1, y1), dxfattribs=attrs)
        elif s["k"] == "c":
            msp.add_circle(tuple(s["c"]), s["r"], dxfattribs=attrs)
        elif s["k"] == "a":
            msp.add_arc(tuple(s["c"]), s["r"], s["a"][0], s["a"][1], dxfattribs=attrs)
        else:
            msp.add_lwpolyline([(s["p"][i], s["p"][i + 1]) for i in range(0, len(s["p"]), 2)], dxfattribs=attrs)
    for layer, a in _arrows(scene):
        tri = _arrow_triangle(a)
        msp.add_solid([tri[0], tri[1], tri[2]], dxfattribs={"layer": layer})
    align = {"start": TextEntityAlignment.LEFT, "middle": TextEntityAlignment.CENTER, "end": TextEntityAlignment.RIGHT}
    for x, y, text, size, anchor, angle, layer in _texts(scene):
        t = msp.add_text(text, dxfattribs={"layer": layer, "height": size, "rotation": angle})
        t.set_placement((x, y), align=align[anchor])
    doc.saveas(path)


def to_pdf(scene: dict[str, Any], path: Path) -> None:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    w, h = scene["sheet"]["width"], scene["sheet"]["height"]
    c = canvas.Canvas(str(path), pagesize=(w * mm, h * mm))
    c.setTitle(str(scene["model"].get("name") or "drawing"))
    c.setLineCap(1)
    for layer, s in _segments(scene):
        style = LAYERS[layer]
        c.setLineWidth(style["width"] * mm)
        c.setStrokeColor(HexColor(style["color"]))
        c.setDash([d * mm for d in style["dash"]] if style["dash"] else [])
        if s["k"] == "l":
            x0, y0, x1, y1 = s["p"]
            c.line(x0 * mm, y0 * mm, x1 * mm, y1 * mm)
        elif s["k"] == "c":
            c.circle(s["c"][0] * mm, s["c"][1] * mm, s["r"] * mm, stroke=1, fill=0)
        elif s["k"] == "a":
            cx, cy, r = s["c"][0] * mm, s["c"][1] * mm, s["r"] * mm
            a0, a1 = s["a"]
            p = c.beginPath()
            p.arc(cx - r, cy - r, cx + r, cy + r, a0, ((a1 - a0) % 360.0) or 360.0)
            c.drawPath(p, stroke=1, fill=0)
        else:
            p = c.beginPath()
            p.moveTo(s["p"][0] * mm, s["p"][1] * mm)
            for i in range(2, len(s["p"]), 2):
                p.lineTo(s["p"][i] * mm, s["p"][i + 1] * mm)
            c.drawPath(p, stroke=1, fill=0)
    c.setDash([])
    for layer, a in _arrows(scene):
        c.setFillColor(HexColor(LAYERS[layer]["color"]))
        tri = _arrow_triangle(a)
        p = c.beginPath()
        p.moveTo(tri[0][0] * mm, tri[0][1] * mm)
        p.lineTo(tri[1][0] * mm, tri[1][1] * mm)
        p.lineTo(tri[2][0] * mm, tri[2][1] * mm)
        p.close()
        c.drawPath(p, stroke=0, fill=1)
    c.setFillColor(HexColor("#000000"))
    for x, y, text, size, anchor, angle, _layer in _texts(scene):
        c.saveState()
        c.translate(x * mm, y * mm)
        if angle:
            c.rotate(angle)
        c.setFont("Helvetica", size * FONT_EM * mm)
        if anchor == "middle":
            c.drawCentredString(0, 0, text)
        elif anchor == "end":
            c.drawRightString(0, 0, text)
        else:
            c.drawString(0, 0, text)
        c.restoreState()
    c.showPage()
    c.save()


def export(ev, fmt: str, path: Path) -> Path:
    scene = layout(ev)
    if not scene["views"]:
        raise DrawingError("the drawing has no views to export")
    fmt = fmt.lower().lstrip(".")
    if fmt == "svg":
        path.write_text(to_svg(scene), encoding="utf-8")
    elif fmt == "dxf":
        to_dxf(scene, path)
    elif fmt == "pdf":
        to_pdf(scene, path)
    else:
        raise DrawingError(f"unknown drawing format {fmt!r}; known: svg, dxf, pdf")
    return path
