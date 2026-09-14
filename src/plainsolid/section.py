"""Section cuts: split every item by a plane and keep one side, with real cap
faces (build123d split), labelled "section" so the client can colour them."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from build123d import Keep, Plane, Vector, split

from .evaluate import Item

STANDARD = {"XY": Plane.XY, "XZ": Plane.XZ, "YZ": Plane.YZ}


class SectionError(ValueError):
    pass


@dataclass(frozen=True)
class SectionSpec:
    plane: str = "XY"                       # XY | XZ | YZ | custom
    offset: float = 0.0
    flip: bool = False
    origin: tuple[float, float, float] | None = None   # custom plane
    normal: tuple[float, float, float] | None = None

    @property
    def key(self) -> str:
        return f"{self.plane}|{self.offset}|{int(self.flip)}|{self.origin}|{self.normal}"

    def to_json(self) -> dict[str, Any]:
        return {"plane": self.plane, "offset": self.offset, "flip": self.flip,
                "origin": list(self.origin) if self.origin else None,
                "normal": list(self.normal) if self.normal else None}


def parse_spec(plane: str | None = None, offset: float | None = None, flip: bool | None = None,
               origin=None, normal=None) -> SectionSpec | None:
    if plane is None:
        return None
    if plane == "custom":
        if origin is None or normal is None:
            raise SectionError("a custom section plane needs origin and normal")
        return SectionSpec("custom", float(offset or 0.0), bool(flip), tuple(float(v) for v in origin),
                           tuple(float(v) for v in normal))
    if plane not in STANDARD:
        raise SectionError(f"unknown section plane {plane!r}; use XY, XZ, YZ or custom")
    return SectionSpec(plane, float(offset or 0.0), bool(flip))


def plane_of(spec: SectionSpec) -> Plane:
    if spec.plane == "custom":
        n = Vector(*spec.normal)
        if n.length < 1e-9:
            raise SectionError("section normal must not be zero")
        base = Plane(origin=Vector(*spec.origin), z_dir=n)
    else:
        base = STANDARD[spec.plane]
    return base.offset(spec.offset) if spec.offset else base


def classify(item: Item, plane: Plane, flip: bool) -> str:
    """'keep', 'drop' or 'cut' from the item's bounding box against the plane.
    Only 'cut' items go through the kernel; the others are free."""
    bb = item.shape.bounding_box()
    n = plane.z_dir
    o = plane.origin
    corners = [Vector(x, y, z) for x in (bb.min.X, bb.max.X) for y in (bb.min.Y, bb.max.Y) for z in (bb.min.Z, bb.max.Z)]
    signed = [(c - o).dot(n) for c in corners]
    eps = 1e-6
    if max(signed) <= eps:      # entirely behind the plane (the side the normal points away from)
        return "drop" if flip else "keep"
    if min(signed) >= -eps:     # entirely in front
        return "keep" if flip else "drop"
    return "cut"


def apply(items: list[Item], spec: SectionSpec) -> list[Item]:
    """Items cut by the plane. Material on the side the normal points to is
    removed; flip keeps that side instead. Items entirely removed disappear;
    items the plane does not cross pass through untouched (and stay cacheable)."""
    plane = plane_of(spec)
    keep = Keep.TOP if spec.flip else Keep.BOTTOM
    out: list[Item] = []
    n = plane.z_dir
    for it in items:
        where = classify(it, plane, spec.flip)
        if where == "drop":
            continue
        if where == "keep":
            out.append(it)
            continue
        try:
            cut = split(it.shape, bisect_by=plane, keep=keep)
        except Exception as exc:  # noqa: BLE001
            raise SectionError(f"could not section {it.name}: {exc}") from None
        if cut is None or not cut.faces():
            continue
        old = {_k(f): lab for f, lab in zip(it.shape.faces(), it.labels(), strict=False)}
        labels, caps = [], []
        for i, f in enumerate(cut.faces()):
            k = _k(f)
            if k in old:
                labels.append(old[k])
                continue
            if _is_cap(f, plane, n):
                labels.append("section")
                caps.append(i)
            else:
                labels.append(it.name)
        out.append(Item(name=it.name, path=it.path, shape=cut, color=it.color, face_labels=labels,
                        tolerance=it.tolerance, cache_key=None, section_faces=caps, edge_labels=None))
    return out


def _k(face) -> int:
    return hash(face.wrapped)


def _is_cap(face, plane: Plane, n: Vector) -> bool:
    try:
        if face.geom_type.name != "PLANE":
            return False
        fn = face.normal_at()
        if abs(abs(fn.dot(n)) - 1.0) > 1e-4:
            return False
        c = face.center()
        return abs((c - plane.origin).dot(n)) < 1e-4
    except Exception:  # noqa: BLE001
        return False
