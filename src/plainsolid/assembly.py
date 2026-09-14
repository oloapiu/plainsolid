"""Assemblies: instances of part files and vendor STEP files, mates between
them, and the pose solver.

An `instance("lid", "lid.py", at=(0, 0, 40), rotate=(0, 0, 90))` loads a
part file (evaluated in its own coordinates, with its identity map) or a
STEP file (a rigid compound of every solid it holds, geometric selectors
only). Its pose is the rigid transform written in the file; the solver moves
it to satisfy the mates and the workspace writes the solved pose back, so
the file always holds the pose that is shown.

Mates take references on instances: body faces (`lid.faces.of("plate").bottom`),
edges and vertices (`panel.edges.where(parallel_to="+X").nearest((0, -50, 0))`),
the part's standard planes (`box.planes.XZ`) and axes (`gland.axes.Z`), all in
the part's own coordinates. A planar face or plane is a plane (origin,
normal); a cylinder, cone, torus, straight edge or axis is an axis; a circular
edge is a circle; a vertex or sphere is a point. Two body faces mate face to
face (normals opposed); every other pairing aligns; `flip=True` reverses.

Solver: six variables per free instance (a rotation vector about the
instance's centre and a translation), residuals per mate, Levenberg-Marquardt
with a weak anchor to the current pose so under-constrained instances stay
where they are, then a projection onto the mates. Degrees of freedom, wholly
redundant mates and conflicting mates come from the Jacobian, as for sketches.
"""
from __future__ import annotations

import contextlib
import dataclasses
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from build123d import Compound, Edge, Face, GeomType, Location, Shape, Vertex
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.gp import gp_Trsf

from .dependencies import file_dependencies
from .materials import MATERIALS
from .selectors import Identity, Selector, SelectorError, resolve
from .sketchgeom import STANDARD_PLANES
from .stepimport import ImportError_, Instance, file_key, import_step_tree, select_node

MATE_KINDS = ("fixed", "coincident", "concentric", "distance", "parallel", "angle")
PART_SUFFIXES = (".py",)
STEP_SUFFIXES = (".step", ".stp")
AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}


class AssemblyError(ValueError):
    pass


# --- colours ------------------------------------------------------------------

def parse_color(value: Any) -> tuple[float, float, float, float] | None:
    """"#rrggbb", "#rrggbbaa", or (r, g, b[, a]) in 0..1 or 0..255, to sRGB floats."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) not in (6, 8):
            raise AssemblyError(f"colour must be #rrggbb, got {value!r}")
        try:
            n = [int(s[i:i + 2], 16) / 255.0 for i in range(0, len(s), 2)]
        except ValueError:
            raise AssemblyError(f"colour must be #rrggbb, got {value!r}") from None
        return (round(n[0], 4), round(n[1], 4), round(n[2], 4), round(n[3], 4) if len(n) == 4 else 1.0)
    try:
        parts = [float(c) for c in value]
    except (TypeError, ValueError):
        raise AssemblyError(f"colour must be #rrggbb or (r, g, b), got {value!r}") from None
    if len(parts) not in (3, 4):
        raise AssemblyError(f"colour must be #rrggbb or (r, g, b), got {value!r}")
    if any(c > 1.0 for c in parts):
        parts = [c / 255.0 for c in parts]
    if len(parts) == 3:
        parts.append(1.0)
    return tuple(round(c, 4) for c in parts)


def color_hex(color: tuple[float, ...] | None) -> str | None:
    if color is None:
        return None
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in color[:3])


# --- loading parts -------------------------------------------------------------

@dataclass
class PartRecord:
    """A part file or a vendor STEP file loaded for instancing, in its own coordinates."""

    path: Path
    kind: str  # part | step
    name: str  # product name: the part's meta name, or the file stem
    shape: Shape  # the body, or a compound of every solid of the STEP file
    identity: Identity | None  # parts only: labels for semantic selectors
    face_labels: list[str] | None
    edge_labels: list[str] | None
    face_tags: list[tuple[str, ...]] | None
    edge_tags: list[tuple[str, ...]] | None
    material: str | None
    color: tuple[float, float, float, float] | None
    leaves: list[Instance]  # step: the imported leaves with their colours
    warnings: list[str]
    key: str
    center: np.ndarray  # bounding-box centre, local
    extent: float = 1.0  # half the bounding-box diagonal: how far a rotation moves the outer points
    tolerance: float = 0.05  # default tessellation tolerance for a STEP part

    @property
    def volume(self) -> float:
        return float(self.shape.volume)


_PARTS: dict[tuple, PartRecord] = {}
_PARTS_MAX = 4096  # every body of a CM proposal, so re-evaluating never reloads them


def _measure(shape: Shape) -> tuple[np.ndarray, float, float]:
    """Centre, half diagonal and a tessellation tolerance from the plain bounding box
    (the tight one costs a surface walk per face, too slow for hundreds of bodies)."""
    from .evaluate import DEFAULT_TOLERANCE

    bb = shape.bounding_box(optimal=False)
    extent = max(bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, bb.max.Z - bb.min.Z)
    tol = max(DEFAULT_TOLERANCE, round(extent / 2000.0, 3))
    return np.array([bb.center().X, bb.center().Y, bb.center().Z]), float(bb.diagonal) / 2.0, tol


def load_part(path: Path, fragment: str | None = None) -> PartRecord:
    """Load (cached by path, mtime, size and fragment) a part file, a STEP file, or
    one node of a STEP file (`fragment` is its path inside the file) for instancing."""
    if not path.exists():
        raise AssemblyError(f"no such file: {path}")
    key = (*file_key(path), fragment or "", file_dependencies([path]))
    rec = _PARTS.get(key)
    if rec is None:
        rec = _load_part(path, key[:3], fragment)
        if len(_PARTS) >= _PARTS_MAX:
            _PARTS.pop(next(iter(_PARTS)))
        _PARTS[key] = rec
    return rec


def _load_part(path: Path, key: tuple[str, int, int], fragment: str | None) -> PartRecord:
    from .evaluate import evaluate
    from .parse import parse_file

    suffix = path.suffix.lower()
    keytext = f"{key[0]}:{key[1]}:{key[2]}" + (f"#{fragment}" if fragment else "")
    if suffix in STEP_SUFFIXES:
        try:
            tree = import_step_tree(path)
            node = select_node(tree, fragment) if fragment else tree
        except ImportError_ as exc:
            raise AssemblyError(str(exc)) from None
        if fragment:
            if node.children:
                raise AssemblyError(f"{path.name}#{fragment} is a sub-assembly of {len(node.leaves())} parts; "
                                    "instance its parts, or the whole file")
            leaves = [node]
            solids = list(node.local_shape.solids()) if node.local_shape is not None else []
            name = node.product or node.name
        else:
            leaves = [leaf for leaf in tree.leaves() if leaf.shape is not None]
            solids = [s for leaf in leaves for s in leaf.shape.solids()]
            name = path.stem
        if not solids:
            raise AssemblyError(f"{path.name}{'#' + fragment if fragment else ''} holds no solids")
        shape = Compound(solids) if len(solids) > 1 else solids[0]
        color = leaves[0].color if len(leaves) == 1 else None
        center, extent, tol = _measure(shape)
        return PartRecord(path, "step", name, shape, None, None, None, None, None, None, color, leaves, [],
                          keytext, center, extent, tol)
    if fragment:
        raise AssemblyError(f"only STEP files take a #fragment, not {path.name}")
    if suffix not in PART_SUFFIXES:
        raise AssemblyError(f"an instance is a .py part file or a .step file, not {path.name}")
    doc = parse_file(str(path))
    if doc.errors:
        raise AssemblyError(f"{path.name} does not parse: {doc.errors[0].message}")
    if doc.kind == "assembly":
        raise AssemblyError(f"{path.name} is an assembly; nested assemblies are not supported")
    ev = evaluate(doc)
    if ev.body is None:
        first = next((r.error.message for r in ev.results if r.error), None)
        raise AssemblyError(f"{path.name} has no body" + (f": {first}" if first else ""))
    warnings = [r.error.message for r in ev.results if r.error]
    meta = doc.meta
    center, extent, _tol = _measure(ev.body)
    ident = ev.identity()
    # on an instance every entity belongs to the instance; the part's owners become groups for .of()
    identity = Identity(owner={}, tags=ident.tags, groups=ident.owner)
    return PartRecord(
        path, "part", str(meta.get("name") or path.stem), ev.body, identity,
        ev.face_labels(), ev.edge_labels(), ev.face_tag_list(), ev.edge_tag_list(),
        str(meta["material"]) if meta.get("material") else None, parse_color(meta.get("color")),
        [], warnings, keytext, center, extent,
    )


# --- poses ------------------------------------------------------------------------

def _skew(w: np.ndarray) -> np.ndarray:
    return np.array([[0.0, -w[2], w[1]], [w[2], 0.0, -w[0]], [-w[1], w[0], 0.0]])


def _log(rot: np.ndarray) -> np.ndarray:
    """Rotation vector of a rotation matrix."""
    cos = max(-1.0, min(1.0, (float(np.trace(rot)) - 1.0) / 2.0))
    th = math.acos(cos)
    if th < 1e-9:
        return np.zeros(3)
    if abs(th - math.pi) < 1e-6:
        # near a half turn the axis comes from the symmetric part
        a = np.sqrt(np.maximum(np.diag(rot) + 1.0, 0.0) / 2.0)
        k = int(np.argmax(a))
        axis = rot[:, k] / (2.0 * a[k]) if a[k] > 1e-9 else np.array([1.0, 0.0, 0.0])
        axis[k] = a[k]
        return axis / np.linalg.norm(axis) * th
    v = np.array([rot[2, 1] - rot[1, 2], rot[0, 2] - rot[2, 0], rot[1, 0] - rot[0, 1]])
    return v * (th / (2.0 * math.sin(th)))


def _exp(w: np.ndarray) -> np.ndarray:
    """Rotation matrix of a rotation vector (Rodrigues)."""
    th = float(np.linalg.norm(w))
    if th < 1e-12:
        return np.eye(3) + _skew(w)
    k = w / th
    k_ = _skew(k)
    return np.eye(3) + math.sin(th) * k_ + (1.0 - math.cos(th)) * (k_ @ k_)


@dataclass
class Pose:
    """A rigid transform: p_world = R @ p_local + t."""

    R: np.ndarray
    t: np.ndarray

    @classmethod
    def identity(cls) -> Pose:
        return cls(np.eye(3), np.zeros(3))

    @classmethod
    def from_args(cls, at: Any, rotate: Any) -> Pose:
        """From the file's `at=(x, y, z)` and `rotate=(rx, ry, rz)` (degrees, build123d's
        Location convention: intrinsic rotations about X, then Y, then Z)."""
        return cls.from_location(Location(tuple(float(c) for c in at), tuple(float(c) for c in rotate)))

    @classmethod
    def from_location(cls, loc: Location) -> Pose:
        tr = loc.wrapped.Transformation()
        m = np.array([[tr.Value(r, c) for c in range(1, 5)] for r in range(1, 4)])
        return cls(m[:, :3].copy(), m[:, 3].copy())

    def location(self) -> Location:
        tr = gp_Trsf()
        tr.SetValues(*[float(v) for v in np.hstack([self.R, self.t[:, None]]).flatten()])
        return Location(tr)

    def at(self) -> tuple[float, float, float]:
        return (float(self.t[0]), float(self.t[1]), float(self.t[2]))

    def rotate(self) -> tuple[float, float, float]:
        o = self.location().orientation
        return (float(o.X), float(o.Y), float(o.Z))

    def matrix(self) -> list[list[float]]:
        m = np.vstack([np.hstack([self.R, self.t[:, None]]), [0.0, 0.0, 0.0, 1.0]])
        return [[float(v) for v in row] for row in m]

    def apply(self, p: np.ndarray) -> np.ndarray:
        return self.R @ p + self.t

    def inverse(self) -> Pose:
        return Pose(self.R.T, -(self.R.T @ self.t))


# --- references --------------------------------------------------------------------

@dataclass
class Ref:
    """What a mate reference resolves to, in the instance's local coordinates."""

    kind: str  # plane | axis | circle | point
    origin: np.ndarray
    direction: np.ndarray | None
    face: bool  # a body face: two of these mate face to face (normals opposed)
    text: str
    radius: float = 0.0

    def posed(self, pose: Pose) -> Ref:
        return Ref(self.kind, pose.apply(self.origin), None if self.direction is None else pose.R @ self.direction,
                   self.face, self.text, self.radius)


def _np(v) -> np.ndarray:
    return np.array([float(v.X), float(v.Y), float(v.Z)]) if hasattr(v, "X") else np.array([float(c) for c in v])


def resolve_reference(selector: Selector, part: PartRecord, what: str) -> Ref:
    """A mate reference: a standard plane or axis of the part, or one face, edge or vertex."""
    text = selector.ref_name
    if selector.kind in ("planes", "axes"):
        names = [arg for op, arg in selector.ops if op == "named"]
        if len(names) != 1 or len(selector.ops) != 1:
            raise AssemblyError(f"{what}: {text} must name one of XY, XZ, YZ (planes) or X, Y, Z (axes)")
        if selector.kind == "planes":
            plane = STANDARD_PLANES[names[0]]
            return Ref("plane", np.zeros(3), _np(plane.z_dir), False, text)
        return Ref("axis", np.zeros(3), np.array(AXES[names[0]]), False, text)
    identity = None
    if part.identity is not None:
        identity = Identity(owner=dict.fromkeys(part.identity.groups, selector.feature), tags=part.identity.tags,
                            groups=part.identity.groups)
    try:
        shapes = resolve(selector, part.shape, identity, many=False)
    except SelectorError as exc:
        raise AssemblyError(f"{what}: {exc}") from None
    if len(shapes) != 1:
        raise AssemblyError(f"{what}: {text} must pick exactly one face, edge or vertex, it picks {len(shapes)}")
    s = shapes[0]
    if isinstance(s, Face):
        ad = BRepAdaptor_Surface(s.wrapped)
        st = ad.GetType()
        if st == GeomAbs_SurfaceType.GeomAbs_Plane:
            return Ref("plane", _np(s.center()), _np(s.normal_at()), True, text)
        if st in (GeomAbs_SurfaceType.GeomAbs_Cylinder, GeomAbs_SurfaceType.GeomAbs_Cone, GeomAbs_SurfaceType.GeomAbs_Torus):
            ax = ad.Cylinder().Axis() if st == GeomAbs_SurfaceType.GeomAbs_Cylinder else \
                ad.Cone().Axis() if st == GeomAbs_SurfaceType.GeomAbs_Cone else ad.Torus().Axis()
            o, d = ax.Location(), ax.Direction()
            r = float(ad.Cylinder().Radius()) if st == GeomAbs_SurfaceType.GeomAbs_Cylinder else 0.0
            return Ref("axis", np.array([o.X(), o.Y(), o.Z()]), np.array([d.X(), d.Y(), d.Z()]), True, text, r)
        if st == GeomAbs_SurfaceType.GeomAbs_Sphere:
            c = ad.Sphere().Location()
            return Ref("point", np.array([c.X(), c.Y(), c.Z()]), None, True, text, float(ad.Sphere().Radius()))
        raise AssemblyError(f"{what}: {text} is a {s.geom_type.name.lower()} face; mates take planar, cylindrical, "
                            "conical, spherical or toroidal faces")
    if isinstance(s, Edge):
        if s.geom_type == GeomType.LINE:
            d = s.end_point() - s.start_point()
            if d.length < 1e-9:
                raise AssemblyError(f"{what}: {text} is a degenerate edge")
            return Ref("axis", _np(s.start_point()), _np(d.normalized()), False, text)
        if s.geom_type == GeomType.CIRCLE:
            circ = BRepAdaptor_Curve(s.wrapped).Circle()
            d = circ.Axis().Direction()
            return Ref("circle", _np(s.arc_center), np.array([d.X(), d.Y(), d.Z()]), False, text, float(s.radius))
        raise AssemblyError(f"{what}: {text} is a {s.geom_type.name.lower()} edge; mates take straight or circular edges")
    if isinstance(s, Vertex):
        return Ref("point", _np(s), None, False, text)
    raise AssemblyError(f"{what}: {text} is not a face, edge or vertex")


# --- mates ------------------------------------------------------------------------
#
# A mate contributes rows r(A, B) of the two posed references and their derivatives
# with respect to the references' origins and directions, as (k, 6) blocks
# [d r / d origin | d r / d direction]. The solver chains them with the pose
# derivatives, so no numerical differentiation is needed anywhere.

AXISLIKE = ("axis", "circle")
I3 = np.eye(3)
Z3 = np.zeros((3, 3))


def _sign(a: Ref, b: Ref, flip: bool) -> float:
    """+1 when the two references align, -1 when they oppose: body faces oppose."""
    s = -1.0 if (a.face and b.face) else 1.0
    return -s if flip else s


def _blk(o: np.ndarray, d: np.ndarray) -> np.ndarray:
    """A (k, 6) Jacobian block from its origin and direction parts (each (k, 3))."""
    return np.hstack([np.atleast_2d(o), np.atleast_2d(d)])


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else np.zeros(3)


def _sgn(v: float) -> float:
    return -1.0 if v < 0 else 1.0


Rows = Any  # callable(A: Ref, B: Ref) -> (r (k,), Ja (k, 6), Jb (k, 6))


def _dir_aligned(s: float) -> Rows:
    def rows(A, B):
        return B.direction - s * A.direction, _blk(Z3, -s * I3), _blk(Z3, I3)
    return rows


def _dir_cross() -> Rows:
    def rows(A, B):
        return np.cross(A.direction, B.direction), _blk(Z3, -_skew(B.direction)), _blk(Z3, _skew(A.direction))
    return rows


def _plane_offset(v: float) -> Rows:
    def rows(A, B):
        u = B.origin - A.origin
        return np.array([float(u @ A.direction) - v]), _blk(-A.direction, u), _blk(A.direction, np.zeros(3))
    return rows


def _dot_signed(s: float, c: float) -> Rows:
    def rows(A, B):
        return np.array([s * float(A.direction @ B.direction) - c]), _blk(np.zeros(3), s * B.direction), _blk(np.zeros(3), s * A.direction)
    return rows


def _dot_abs(c: float) -> Rows:
    def rows(A, B):
        dot = float(A.direction @ B.direction)
        sg = _sgn(dot)
        return np.array([abs(dot) - c]), _blk(np.zeros(3), sg * B.direction), _blk(np.zeros(3), sg * A.direction)
    return rows


def _line_cross() -> Rows:
    """(oB - oA) x dA = 0: B's origin lies on A's axis."""
    def rows(A, B):
        u = B.origin - A.origin
        return np.cross(u, A.direction), _blk(_skew(A.direction), _skew(u)), _blk(-_skew(A.direction), Z3)
    return rows


def _line_dist(v: float) -> Rows:
    def rows(A, B):
        u = B.origin - A.origin
        w = np.cross(u, A.direction)
        g = _unit(w)
        return np.array([float(np.linalg.norm(w)) - v]), _blk(g @ _skew(A.direction), g @ _skew(u)), _blk(g @ -_skew(A.direction), np.zeros(3))
    return rows


def _points_diff() -> Rows:
    def rows(A, B):
        return A.origin - B.origin, _blk(I3, Z3), _blk(-I3, Z3)
    return rows


def _points_dist(v: float) -> Rows:
    def rows(A, B):
        u = A.origin - B.origin
        g = _unit(u)
        return np.array([float(np.linalg.norm(u)) - v]), _blk(g, np.zeros(3)), _blk(-g, np.zeros(3))
    return rows


def _point_plane(point_first: bool, v: float) -> Rows:
    """Signed distance of a point from a plane along the plane's normal."""
    def rows(A, B):
        p, pl = (A, B) if point_first else (B, A)
        u = p.origin - pl.origin
        r = np.array([float(u @ pl.direction) - v])
        jp, jpl = _blk(pl.direction, np.zeros(3)), _blk(-pl.direction, u)
        return (r, jp, jpl) if point_first else (r, jpl, jp)
    return rows


def _axis_plane(kind: str, axis_first: bool, value: float) -> Rows:
    v = 0.0 if kind == "coincident" else value

    def rows(A, B):
        ax, pl = (A, B) if axis_first else (B, A)
        d, n = ax.direction, pl.direction
        if kind == "parallel":
            r, jax, jpl = np.array([float(d @ n)]), _blk(np.zeros(3), n), _blk(np.zeros(3), d)
        elif kind == "angle":
            sg = _sgn(float(d @ n))
            r = np.array([abs(float(d @ n)) - math.sin(math.radians(value))])
            jax, jpl = _blk(np.zeros(3), sg * n), _blk(np.zeros(3), sg * d)
        else:
            u = ax.origin - pl.origin
            r = np.array([float(d @ n), float(u @ n) - v])
            jax = np.vstack([_blk(np.zeros(3), n), _blk(n, np.zeros(3))])
            jpl = np.vstack([_blk(np.zeros(3), d), _blk(-n, u)])
        return (r, jax, jpl) if axis_first else (r, jpl, jax)
    return rows


def _point_axis(kind: str, point_first: bool, value: float) -> Rows:
    def rows(A, B):
        p, ax = (A, B) if point_first else (B, A)
        u = p.origin - ax.origin
        w = np.cross(u, ax.direction)
        if kind == "distance":
            g = _unit(w)
            r = np.array([float(np.linalg.norm(w)) - value])
            jp, jax = _blk(g @ -_skew(ax.direction), np.zeros(3)), _blk(g @ _skew(ax.direction), g @ _skew(u))
        else:
            r = w
            jp, jax = _blk(-_skew(ax.direction), Z3), _blk(_skew(ax.direction), _skew(u))
        return (r, jp, jax) if point_first else (r, jax, jp)
    return rows


def _stack(*parts: Rows) -> Rows:
    def rows(A, B):
        out = [p(A, B) for p in parts]
        return (np.concatenate([o[0] for o in out]), np.vstack([o[1] for o in out]), np.vstack([o[2] for o in out]))
    return rows


def _plane_pair(kind: str, a: Ref, b: Ref, value: float, flip: bool) -> Rows:
    s = _sign(a, b, flip)
    if kind == "parallel":
        return _dir_cross()
    if kind == "angle":
        return _dot_signed(s, math.cos(math.radians(value)))
    return _stack(_dir_aligned(s), _plane_offset(0.0 if kind == "coincident" else value))


def _axis_pair(kind: str, a: Ref, b: Ref, value: float) -> Rows:
    if kind == "parallel":
        return _dir_cross()
    if kind == "angle":
        return _dot_abs(math.cos(math.radians(value)))
    if kind == "distance" and value != 0.0:
        return _stack(_dir_cross(), _line_dist(value))
    if kind == "coincident" and a.kind == "circle" and b.kind == "circle":
        return _stack(_dir_cross(), _points_diff())
    return _stack(_dir_cross(), _line_cross())


def mate_rows(kind: str, a: Ref, b: Ref, value: float | None, flip: bool, what: str) -> Rows:
    """The residual rows of a mate for its pair of reference kinds."""
    ka, kb = a.kind, b.kind
    val = 0.0 if value is None else float(value)
    pair = f"{ka} and {kb}"
    if kind == "concentric":
        if ka in AXISLIKE and kb in AXISLIKE:
            return _axis_pair("concentric", a, b, val)
        if ka == "point" and kb in AXISLIKE:
            return _point_axis("coincident", True, val)
        if ka in AXISLIKE and kb == "point":
            return _point_axis("coincident", False, val)
        raise AssemblyError(f"{what}: concentric needs cylindrical faces, circular edges or axes, not {pair}")
    if ka == "plane" and kb == "plane":
        return _plane_pair(kind, a, b, val, flip)
    if ka in AXISLIKE and kb in AXISLIKE:
        return _axis_pair(kind, a, b, val)
    if ka == "point" and kb == "point":
        if kind == "distance":
            return _points_dist(val)
        if kind == "coincident":
            return _points_diff()
        raise AssemblyError(f"{what}: {kind} does not apply to two points")
    if {ka, kb} == {"point", "plane"}:
        if kind in ("coincident", "distance"):
            return _point_plane(ka == "point", val)
        raise AssemblyError(f"{what}: {kind} does not apply to {pair}")
    if (ka in AXISLIKE and kb == "plane") or (ka == "plane" and kb in AXISLIKE):
        return _axis_plane(kind, ka in AXISLIKE, val)
    if (ka == "point" and kb in AXISLIKE) or (ka in AXISLIKE and kb == "point"):
        if kind in ("coincident", "distance"):
            return _point_axis(kind, ka == "point", val)
        raise AssemblyError(f"{what}: {kind} does not apply to {pair}")
    raise AssemblyError(f"{what}: {kind} does not apply to {pair}")


# --- state --------------------------------------------------------------------------

@dataclass
class InstanceState:
    name: str
    part: PartRecord
    pose: Pose  # from the file
    color: tuple[float, float, float, float] | None  # override
    material: str | None
    density: float | None
    tolerance: float | None
    fixed: bool = False

    def effective_color(self) -> tuple[float, float, float, float] | None:
        return self.color or self.part.color

    def density_value(self) -> float | None:
        if self.density is not None:
            return float(self.density)
        name = self.material or self.part.material
        if name is None:
            return None
        key = str(name).lower().replace(" ", "").replace("-", "")
        if key not in MATERIALS:
            raise AssemblyError(f"instance {self.name!r}: unknown material {name!r}; known: {sorted(MATERIALS)}")
        return MATERIALS[key]


@dataclass
class MateState:
    name: str
    kind: str
    a_inst: str
    b_inst: str
    a: Ref
    b: Ref
    rows: Rows
    value: float | None
    flip: bool


@dataclass
class AssemblyState:
    instances: dict[str, InstanceState] = field(default_factory=dict)
    mates: list[MateState] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # instance names in feature order

    def copy(self) -> AssemblyState:
        return AssemblyState(dict(self.instances), list(self.mates), list(self.order))


@dataclass
class AssemblySolution:
    poses: dict[str, Pose]
    dof: int = 0
    rank: int = 0
    variables: int = 0
    redundant: list[str] = field(default_factory=list)
    conflicting: list[str] = field(default_factory=list)
    free: list[str] = field(default_factory=list)  # under-constrained instances
    residual: float = 0.0
    iterations: int = 0
    ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "dof": self.dof, "rank": self.rank, "variables": self.variables,
            "redundant": list(self.redundant), "conflicting": list(self.conflicting), "free": list(self.free),
            "residual": self.residual, "iterations": self.iterations, "ms": round(self.ms, 2),
            "warnings": list(self.warnings),
            "poses": {name: {"at": [round(c, 6) for c in p.at()], "rotate": [round(c, 6) for c in p.rotate()],
                             "transform": p.matrix()} for name, p in self.poses.items()},
        }


# --- the solver ----------------------------------------------------------------------
#
# Variables: per free instance a small rotation about its centre and a translation,
# re-linearised at every iteration. The rotation is measured in millimetres at the
# instance's extent (a rotation costs what it moves), so rotating a part is never
# the cheap way to shift one of its faces. Damped Gauss-Newton with least-norm
# steps and a weak anchor: the step never moves along the mates' null space, so an
# under-constrained instance keeps whatever the mates leave free (it stays nearest
# its current pose) and a fully constrained one converges quadratically.

class _Problem:
    def __init__(self, state: AssemblyState):
        self.names = [n for n in state.order if n in state.instances]
        self.free = [n for n in self.names if not state.instances[n].fixed]
        self.index = {n: i for i, n in enumerate(self.free)}
        self.n = 6 * len(self.free)
        self.poses = {name: state.instances[name].pose for name in self.names}
        self.local_center = {name: state.instances[name].part.center for name in self.free}
        self.scale = {name: max(1.0, state.instances[name].part.extent) for name in self.free}
        self.mates = [m for m in state.mates if m.a_inst in self.poses and m.b_inst in self.poses]
        self.start = dict(self.poses)
        self.start_centers = self.centers()

    def centers(self) -> dict[str, np.ndarray]:
        return {name: self.poses[name].apply(self.local_center[name]) for name in self.free}

    def displacement(self, poses: dict[str, Pose] | None = None) -> np.ndarray:
        """How far each free instance has come from where it started: a rotation
        vector and the motion of its centre, in the order of the variables."""
        poses = self.poses if poses is None else poses
        out = np.zeros(self.n)
        for name in self.free:
            i = self.index[name]
            rd = poses[name].R @ self.start[name].R.T
            out[6 * i:6 * i + 3] = _log(rd) * self.scale[name]
            out[6 * i + 3:6 * i + 6] = poses[name].apply(self.local_center[name]) - self.start_centers[name]
        return out

    def settle_free(self, jac: np.ndarray, rank: int) -> None:
        """Along the mates' null space, move back as close to the start as the
        mates allow: a free spin the iterations picked up goes away."""
        if rank >= self.n:
            return
        _, _, vt = np.linalg.svd(jac, full_matrices=True)
        null = vt[rank:]
        k = null.shape[0]
        c = np.zeros(k)
        h = 1e-4
        for _ in range(8):
            d0 = self.displacement(self.stepped(null.T @ c))
            if float(np.linalg.norm(d0)) < 1e-10:
                break
            jd = np.zeros((self.n, k))
            for j in range(k):
                e = np.zeros(k)
                e[j] = h
                jd[:, j] = (self.displacement(self.stepped(null.T @ (c + e)))
                            - self.displacement(self.stepped(null.T @ (c - e)))) / (2 * h)
            step = np.linalg.lstsq(jd, -d0, rcond=1e-10)[0]
            c = c + step
            if float(np.linalg.norm(step)) < 1e-10:
                break
        self.poses = self.stepped(null.T @ c)

    def linearize(self) -> tuple[np.ndarray, np.ndarray]:
        """Residual and Jacobian with respect to the local steps of the free instances."""
        centers = self.centers()
        rs, js = [], []
        for m in self.mates:
            A, B = m.a.posed(self.poses[m.a_inst]), m.b.posed(self.poses[m.b_inst])
            r, ja, jb = m.rows(A, B)
            k = r.size
            row = np.zeros((k, self.n))
            for ref, jr, inst in ((A, ja, m.a_inst), (B, jb, m.b_inst)):
                i = self.index.get(inst)
                if i is None:
                    continue
                # d origin / d(dw, dt) = [-[o - c]x | I], d direction / d(dw, dt) = [-[d]x | 0],
                # with dw in millimetres at the instance's extent
                pose_j = np.zeros((6, 6))
                pose_j[:3, :3] = -_skew(ref.origin - centers[inst]) / self.scale[inst]
                pose_j[:3, 3:] = I3
                if ref.direction is not None:
                    pose_j[3:, :3] = -_skew(ref.direction) / self.scale[inst]
                row[:, 6 * i:6 * i + 6] = jr @ pose_j
            rs.append(r)
            js.append(row)
        if not rs:
            return np.zeros(0), np.zeros((0, self.n))
        return np.concatenate(rs), np.vstack(js)

    def residual_at(self, poses: dict[str, Pose]) -> np.ndarray:
        parts = [m.rows(m.a.posed(poses[m.a_inst]), m.b.posed(poses[m.b_inst]))[0] for m in self.mates]
        return np.concatenate(parts) if parts else np.zeros(0)

    def stepped(self, delta: np.ndarray) -> dict[str, Pose]:
        centers = self.centers()
        out = dict(self.poses)
        for name in self.free:
            i = self.index[name]
            w, tau = delta[6 * i:6 * i + 3] / self.scale[name], delta[6 * i + 3:6 * i + 6]
            rd = _exp(w)
            p, c = self.poses[name], centers[name]
            out[name] = Pose(rd @ p.R, rd @ (p.t - c) + c + tau)
        return out


ANCHOR = 1e-3  # weight of the pull back toward the starting pose (mates weigh 1)


def solve(state: AssemblyState, max_iterations: int = 60, tol: float = 1e-10,
          trace: list[tuple[int, float, float, float]] | None = None) -> AssemblySolution:
    """Poses satisfying the mates, nearest the current ones. `trace` collects
    (iteration, residual norm, damping, largest rotation step) for diagnosis."""
    t0 = time.perf_counter()
    prob = _Problem(state)
    warnings: list[str] = []
    if prob.mates and not any(state.instances[n].fixed for n in prob.names):
        warnings.append("no fixed instance: the assembly floats, add fixed(...) to anchor it")
    sol = AssemblySolution(poses=dict(prob.poses), variables=prob.n, warnings=warnings)
    if prob.n == 0 or not prob.mates:
        sol.dof = prob.n
        sol.free = list(prob.free)
        sol.ms = (time.perf_counter() - t0) * 1e3
        return sol

    lam = 1e-3
    r, jac = prob.linearize()
    norm = float(np.linalg.norm(r))
    iterations = 0
    for _ in range(max_iterations):
        if norm < tol:
            break
        iterations += 1
        improved = False
        disp = prob.displacement()
        for _try in range(8):
            # mates, a weak anchor toward the start, and the damping
            aug = np.vstack([jac, ANCHOR * np.eye(prob.n), math.sqrt(lam) * np.eye(prob.n)])
            rhs = np.concatenate([-r, -ANCHOR * disp, np.zeros(prob.n)])
            delta = np.linalg.lstsq(aug, rhs, rcond=None)[0]
            # keep every rotation step inside the range where the linearisation holds
            biggest = max((float(np.linalg.norm(delta[6 * i:6 * i + 3])) / prob.scale[name]
                           for name, i in prob.index.items()), default=0.0)
            if biggest > 0.5:
                delta *= 0.5 / biggest
            # take the step, or a fraction of it when the full one overshoots
            for alpha in (1.0, 0.5, 0.25, 0.1):
                trial = prob.stepped(alpha * delta)
                new_norm = float(np.linalg.norm(prob.residual_at(trial)))
                if trace is not None:
                    trace.append((iterations, new_norm, lam, alpha * biggest))
                if new_norm < norm or new_norm < tol:
                    prob.poses = trial
                    improved = True
                    break
            if improved:
                lam = max(lam / 3.0, 1e-12) if alpha == 1.0 else lam
                break
            lam = min(lam * 10.0, 1e6)
        if not improved:
            break
        r, jac = prob.linearize()
        norm = float(np.linalg.norm(r))
    def polish() -> None:
        nonlocal r, jac, norm
        for _ in range(6):
            if norm < 1e-13:
                break
            delta = np.linalg.lstsq(jac, -r, rcond=1e-10)[0]
            trial = prob.stepped(delta)
            new_norm = float(np.linalg.norm(prob.residual_at(trial)))
            if new_norm >= norm:
                break
            prob.poses = trial
            r, jac = prob.linearize()
            norm = float(np.linalg.norm(r))

    # undamped least-norm steps land exactly on the mates; then what the mates leave
    # free went wherever the iterations dragged it: move it back toward the start
    # along the null space, and land on the mates again
    polish()
    rank = _rank(jac)
    if norm < 1e-6 and rank < prob.n:
        before = dict(prob.poses)
        prob.settle_free(jac, rank)
        r, jac = prob.linearize()
        norm = float(np.linalg.norm(r))
        polish()
        if norm > 1e-6:  # the null space was too curved to follow: keep what converged
            prob.poses = before
            r, jac = prob.linearize()
            norm = float(np.linalg.norm(r))
    rank = _rank(jac)
    sol.poses = dict(prob.poses)
    sol.rank, sol.dof, sol.iterations, sol.residual = rank, prob.n - rank, iterations, norm
    if norm > 1e-6:
        for m in prob.mates:
            rows = m.rows(m.a.posed(prob.poses[m.a_inst]), m.b.posed(prob.poses[m.b_inst]))[0]
            if np.linalg.norm(rows) > 1e-6:
                sol.conflicting.append(m.name)
    if jac.shape[0] > rank:
        offs, r0 = [], 0
        for m in prob.mates:
            size = m.rows(m.a.posed(prob.poses[m.a_inst]), m.b.posed(prob.poses[m.b_inst]))[0].size
            offs.append((m, r0, r0 + size))
            r0 += size
        keep = np.ones(jac.shape[0], bool)
        for m, a_, b_ in reversed(offs):
            trial_keep = keep.copy()
            trial_keep[a_:b_] = False
            if _rank(jac[trial_keep]) == rank:
                sol.redundant.append(m.name)
                keep = trial_keep
                if jac[keep].shape[0] == rank:
                    break
    if sol.dof > 0:
        _, _, vt = np.linalg.svd(jac, full_matrices=True)
        null = vt[rank:]
        touched = np.abs(null).max(axis=0) > 1e-7 if null.size else np.zeros(prob.n, bool)
        sol.free = [name for name in prob.free if touched[6 * prob.index[name]:6 * prob.index[name] + 6].any()]
    sol.ms = (time.perf_counter() - t0) * 1e3
    return sol


def poses_from_json(state: AssemblyState, poses: dict[str, Any] | None, fallback: AssemblySolution) -> AssemblyState:
    """A copy of the state whose instances sit at `poses` ({name: {at, rotate}}), or at the solution's."""
    out = state.copy()
    for name, inst in state.instances.items():
        src = (poses or {}).get(name)
        pose = Pose.from_args(src["at"], src["rotate"]) if src else fallback.poses.get(name, inst.pose)
        out.instances[name] = dataclasses.replace(inst, pose=pose)
    return out


def drag(state: AssemblyState, solution: AssemblySolution, name: str, poses: dict[str, Any] | None,
         translate: Any = None, rotate: Any = None) -> dict[str, Any]:
    """Move one instance by a requested translation (mm) and rotation (a rotation vector,
    radians, about its centre), as far as the mates allow: the request is projected onto the
    null space of the mates, applied, and the assembly re-solved from there. `poses` is where
    the instances are when the drag step starts (the previous step's answer), so a drag is a
    chain of small steps and the linearisation always holds. Nothing is written."""
    if name not in state.instances:
        raise AssemblyError(f"no instance {name!r}")
    cur = poses_from_json(state, poses, solution)
    prob = _Problem(cur)
    if name not in prob.index:
        return {"moved": False, "reason": f"{name} is fixed", "poses": solution.to_json()["poses"] if poses is None else poses}
    i = prob.index[name]
    d = np.zeros(prob.n)
    if rotate is not None:
        w = np.array([float(c) for c in rotate])
        d[6 * i:6 * i + 3] = w * prob.scale[name]
    if translate is not None:
        d[6 * i + 3:6 * i + 6] = np.array([float(c) for c in translate])
    if float(np.linalg.norm(d)) < 1e-12:
        return {"moved": False, "reason": "nothing to move", "poses": _poses_json(prob.poses)}
    _, jac = prob.linearize()
    if jac.size:
        rank = _rank(jac)
        if rank >= prob.n:
            return {"moved": False, "reason": f"{name} is fully constrained by its mates", "poses": _poses_json(prob.poses)}
        _, _, vt = np.linalg.svd(jac, full_matrices=True)
        null = vt[rank:]
        if float(np.abs(null[:, 6 * i:6 * i + 6]).max()) < 1e-7:
            return {"moved": False, "reason": f"{name} is fully constrained by its mates", "poses": _poses_json(prob.poses)}
        step = null.T @ (null @ d)
    else:
        step = d
    own = step[6 * i:6 * i + 6]
    if float(np.linalg.norm(own)) < 1e-6 * max(1.0, float(np.linalg.norm(d))):
        return {"moved": False, "reason": f"the mates leave {name} no motion that way", "poses": _poses_json(prob.poses)}
    moved = poses_from_json(cur, {n: {"at": p.at(), "rotate": p.rotate()} for n, p in prob.stepped(step).items()}, solution)
    sol = solve(moved)
    out = {"moved": True, "poses": sol.to_json()["poses"], "conflicting": list(sol.conflicting), "dof": sol.dof}
    return out


def _poses_json(poses: dict[str, Pose]) -> dict[str, Any]:
    return {name: {"at": [round(c, 6) for c in p.at()], "rotate": [round(c, 6) for c in p.rotate()], "transform": p.matrix()}
            for name, p in poses.items()}


def rotation_from(a: AssemblySolution, b: dict[str, Any]) -> float:
    """How far the instances of solution `a` have turned to reach the poses `b` (radians, summed)."""
    total = 0.0
    for name, pose in a.poses.items():
        q = b.get(name)
        if not q:
            continue
        r_new = np.array(q["transform"])[:3, :3]
        total += float(np.linalg.norm(_log(r_new @ pose.R.T)))
    return total


def _rank(jac: np.ndarray) -> int:
    if jac.size == 0:
        return 0
    s = np.linalg.svd(jac, compute_uv=False)
    return int((s > 1e-7 * max(1.0, s[0])).sum())


def check_jacobian(state: AssemblyState, eps: float = 1e-6) -> float:
    """Max abs difference between the analytic and a central-difference Jacobian (for tests)."""
    prob = _Problem(state)
    _, jac = prob.linearize()
    num = np.zeros_like(jac)
    for i in range(prob.n):
        d = np.zeros(prob.n)
        d[i] = eps
        num[:, i] = (prob.residual_at(prob.stepped(d)) - prob.residual_at(prob.stepped(-d))) / (2 * eps)
    return float(np.abs(jac - num).max()) if jac.size else 0.0


# --- what the rest of the engine consumes --------------------------------------------

def roots(state: AssemblyState, solution: AssemblySolution) -> list[Instance]:
    """One Instance tree per instance feature, posed: what the client's tree, the
    mesh, sections, measurements and queries consume."""
    out: list[Instance] = []
    for name in state.order:
        inst = state.instances.get(name)
        if inst is None:
            continue
        pose = solution.poses.get(name, inst.pose)
        loc = pose.location()
        part = inst.part
        color = inst.effective_color()
        root = Instance(name=name, product=part.name, path=name, color=color, transform=pose.matrix(),
                        file=str(part.path), kind=part.kind)
        if part.kind == "step" and len(part.leaves) > 1:
            for leaf in part.leaves:
                out_leaf = Instance(name=leaf.name, product=leaf.product, path=f"{name}.{leaf.path}",
                                    color=inst.color or leaf.color, transform=pose.matrix(),
                                    shape=leaf.shape.moved(loc), local_shape=leaf.shape, file=str(part.path), kind="step")
                root.children.append(out_leaf)
        else:
            root.shape = part.shape.moved(loc)
            root.local_shape = part.shape
        out.append(root)
    return out


def items(state: AssemblyState, solution: AssemblySolution, posed: list[Instance]) -> list:
    """Items for the mesh, with the part's labels and tags so a click can write a selector."""
    from .evaluate import DEFAULT_TOLERANCE, Item

    out = []
    for root in posed:
        inst = state.instances[root.name]
        part = inst.part
        tol = inst.tolerance or (part.tolerance if part.kind == "step" else DEFAULT_TOLERANCE)
        # the mesh is tessellated once per product and placed per instance: the key names the
        # product (and the body inside a STEP file), never the instance or its pose
        transform = np.asarray(root.transform, float) if root.transform else None
        for leaf in root.leaves():
            if leaf.shape is None:
                continue
            within = leaf.path[len(root.path) + 1:] if leaf is not root else ""
            out.append(Item(
                name=leaf.path, path=leaf.path, shape=leaf.shape, color=leaf.color, tolerance=tol,
                cache_key=f"{part.key}|{within}|{tol}",
                local_shape=leaf.local_shape if transform is not None else None, transform=transform,
                face_labels=part.face_labels if part.kind == "part" else None,
                edge_labels=part.edge_labels if part.kind == "part" else None,
                face_tags=part.face_tags if part.kind == "part" else None,
                edge_tags=part.edge_tags if part.kind == "part" else None,
            ))
    return out


def file_poses(solution: AssemblySolution, state: AssemblyState) -> dict[str, dict[str, tuple[float, float, float]]]:
    """What write-back puts into the instance statements: solved at= and rotate= per free instance."""
    return {name: {"at": pose.at(), "rotate": pose.rotate()}
            for name, pose in solution.poses.items() if name in state.instances and not state.instances[name].fixed}


# --- queries ---------------------------------------------------------------------------

def bom(state: AssemblyState, posed: list[Instance]) -> dict[str, Any]:
    """Bill of materials: one row per part file, with its instances, material and mass."""
    rows: dict[str, dict[str, Any]] = {}
    unknown: list[str] = []
    for root in posed:
        inst = state.instances[root.name]
        part = inst.part
        try:
            rho = inst.density_value()
        except AssemblyError:
            rho = None
        vol = part.volume
        mass = round(vol / 1000.0 * rho, 6) if rho is not None else None
        if mass is None:
            unknown.append(root.name)
        key = str(part.path)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "file": _rel(part.path), "name": part.name, "kind": part.kind, "count": 0, "instances": [],
                "material": inst.material or part.material, "density": rho, "volume": round(vol, 6), "mass": mass,
                "color": color_hex(inst.effective_color()),
            }
        row["count"] += 1
        row["instances"].append(root.name)
    items_ = list(rows.values())
    total = sum(r["mass"] * r["count"] for r in items_ if r["mass"] is not None)
    return {"rows": items_, "instances": sum(r["count"] for r in items_),
            "mass": round(total, 6) if not unknown else None, "unit": "g", "unknown_mass": unknown}


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def mass(state: AssemblyState, posed: list[Instance]) -> dict[str, Any]:
    per: dict[str, float] = {}
    for root in posed:
        inst = state.instances[root.name]
        rho = inst.density_value()
        if rho is None:
            raise AssemblyError(f"instance {root.name!r}: no material; give material= on the instance or in the part's meta()")
        per[root.name] = round(inst.part.volume / 1000.0 * rho, 6)
    return {"mass": round(sum(per.values()), 6), "unit": "g", "instances": per}


def interference(posed: list[Instance], min_volume: float = 1e-6) -> dict[str, Any]:
    """Pairs of instances whose solids overlap, with the overlap volume."""
    leaves = [(root.name, leaf) for root in posed for leaf in root.leaves() if leaf.shape is not None]
    pairs: list[dict[str, Any]] = []
    boxes = [leaf.shape.bounding_box() for _, leaf in leaves]
    for i in range(len(leaves)):
        for j in range(i + 1, len(leaves)):
            if leaves[i][0] == leaves[j][0]:
                continue
            bi, bj = boxes[i], boxes[j]
            if (bi.max.X < bj.min.X or bj.max.X < bi.min.X or bi.max.Y < bj.min.Y or bj.max.Y < bi.min.Y
                    or bi.max.Z < bj.min.Z or bj.max.Z < bi.min.Z):
                continue
            vol = _overlap(leaves[i][1].shape, leaves[j][1].shape)
            if vol > min_volume:
                pairs.append({"a": leaves[i][1].path, "b": leaves[j][1].path, "volume": round(float(vol), 6)})
    return {"pairs": pairs, "count": len(pairs), "unit": "mm^3"}


def _overlap(a: Shape, b: Shape) -> float:
    """Volume of the intersection of two shapes; a failed boolean counts as no overlap."""
    with contextlib.suppress(Exception):
        r = a.intersect(b)
        if r is None:
            return 0.0
        return sum(s.volume for s in (r if isinstance(r, list) else [r]) if hasattr(s, "volume"))
    return 0.0


def export_compound(posed: list[Instance], name: str | None) -> Compound:
    """The assembly as a build123d compound with labels and colours, for STEP export."""
    from build123d import Color

    def node(inst: Instance) -> Shape:
        if inst.children:
            c = Compound(children=[node(ch) for ch in inst.children])
            c.label = inst.name
            return c
        s = inst.shape
        s.label = inst.name
        if inst.color:
            s.color = Color(*inst.color[:3])
        return s

    top = Compound(children=[node(r) for r in posed])
    top.label = str(name or "assembly")
    return top


__all__ = [
    "AXES", "MATE_KINDS", "AssemblyError", "AssemblySolution", "AssemblyState", "InstanceState", "MateState",
    "PartRecord", "Pose", "Ref", "bom", "check_jacobian", "color_hex", "export_compound", "file_poses", "interference",
    "items", "load_part", "mass", "mate_rows", "parse_color", "resolve_reference", "roots", "solve",
]
