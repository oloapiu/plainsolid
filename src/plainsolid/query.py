"""Geometry queries on an evaluated document. All results are JSON-safe."""
from __future__ import annotations

from typing import Any

from build123d import CenterOf, Part

from .evaluate import Evaluation
from .materials import MATERIALS


class QueryError(ValueError):
    pass


def _body(ev: Evaluation) -> Part:
    if ev.body is None:
        raise QueryError("the document has no body")
    return ev.body


def _shapes(ev: Evaluation) -> list:
    """All measurable shapes: the body of a part, or every leaf of an assembly."""
    if ev.body is not None:
        return [ev.body]
    if ev.document.kind == "drawing":
        raise QueryError(f"a drawing has no geometry to measure; query its model {ev.document.meta.get('of') or ''}".rstrip())
    shapes = [leaf.shape for root in ev.roots for leaf in root.leaves() if leaf.shape is not None]
    if not shapes:
        raise QueryError("the document has no geometry")
    return shapes


def _v(vec) -> list[float]:
    return [round(float(c), 6) for c in vec]


def volume(ev: Evaluation) -> dict[str, Any]:
    return {"volume": round(sum(s.volume for s in _shapes(ev)), 6), "unit": "mm^3"}


def area(ev: Evaluation) -> dict[str, Any]:
    return {"area": round(sum(s.area for s in _shapes(ev)), 6), "unit": "mm^2"}


def bbox(ev: Evaluation) -> dict[str, Any]:
    boxes = [s.bounding_box() for s in _shapes(ev)]
    lo = [min(b.min.X for b in boxes), min(b.min.Y for b in boxes), min(b.min.Z for b in boxes)]
    hi = [max(b.max.X for b in boxes), max(b.max.Y for b in boxes), max(b.max.Z for b in boxes)]
    return {"min": _v(lo), "max": _v(hi), "size": _v([h - l for l, h in zip(lo, hi, strict=True)]),
            "center": _v([(l + h) / 2 for l, h in zip(lo, hi, strict=True)])}


def counts(ev: Evaluation) -> dict[str, Any]:
    shapes = _shapes(ev)
    out = {"solids": sum(len(s.solids()) for s in shapes), "faces": sum(len(s.faces()) for s in shapes),
           "edges": sum(len(s.edges()) for s in shapes), "vertices": sum(len(s.vertices()) for s in shapes)}
    if ev.body is None:
        out["instances"] = len(shapes)
    return out


def center_of_mass(ev: Evaluation) -> dict[str, Any]:
    shapes = _shapes(ev)
    total = sum(s.volume for s in shapes)
    if total <= 0:
        raise QueryError("zero volume")
    acc = [0.0, 0.0, 0.0]
    for s in shapes:
        c = s.center(CenterOf.MASS)
        for i, comp in enumerate((c.X, c.Y, c.Z)):
            acc[i] += comp * s.volume
    return {"center_of_mass": _v([a / total for a in acc])}


def density_for(ev: Evaluation, material: str | None = None, density: float | None = None) -> float | None:
    if density is not None:
        return float(density)
    name = material or ev.document.meta.get("material")
    if name is None:
        return None
    key = str(name).lower().replace(" ", "").replace("-", "")
    if key not in MATERIALS:
        raise QueryError(f"unknown material {name!r}; known: {sorted(MATERIALS)} or give density=")
    return MATERIALS[key]


def mass(ev: Evaluation, material: str | None = None, density: float | None = None) -> dict[str, Any]:
    if ev.document.kind == "assembly":
        from . import assembly as pasm

        if not ev.posed:
            raise QueryError("the assembly has no instances")
        try:
            return pasm.mass(ev.assembly, ev.posed)
        except pasm.AssemblyError as exc:
            raise QueryError(str(exc)) from None
    b = _body(ev)
    rho = density_for(ev, material, density)
    if rho is None:
        raise QueryError("no material in meta() and no density given")
    vol = b.volume
    return {"mass": round(vol / 1000.0 * rho, 6), "unit": "g", "density": rho, "volume": round(vol, 6)}


def bom(ev: Evaluation) -> dict[str, Any]:
    """Bill of materials of an assembly: one row per part file with its instances."""
    from . import assembly as pasm

    if ev.document.kind != "assembly":
        raise QueryError("a bill of materials needs an assembly document")
    return pasm.bom(ev.assembly, ev.posed)


def interference(ev: Evaluation) -> dict[str, Any]:
    """Pairs of instances whose solids overlap."""
    from . import assembly as pasm

    if ev.document.kind != "assembly":
        raise QueryError("an interference check needs an assembly document")
    return pasm.interference(ev.roots)


def summary(ev: Evaluation) -> dict[str, Any]:
    if ev.document.kind == "drawing":
        from . import drawing as pdrawing

        return pdrawing.summary(ev)
    out: dict[str, Any] = {}
    out.update(volume(ev)); out.pop("unit")
    out["area"] = area(ev)["area"]
    out["bbox"] = bbox(ev)
    out["counts"] = counts(ev)
    out.update(center_of_mass(ev))
    try:
        out["mass"] = mass(ev)["mass"] if ev.body is not None or ev.posed else None
    except QueryError:
        out["mass"] = None
    if ev.document.kind == "assembly" and ev.solution is not None:
        out["dof"] = ev.solution.dof
        out["free"] = list(ev.solution.free)
    return out


QUERIES = {
    "volume": volume, "area": area, "bbox": bbox, "counts": counts,
    "center_of_mass": center_of_mass, "mass": mass, "summary": summary,
    "bom": bom, "interference": interference,
}


def run(ev: Evaluation, kind: str, **kwargs: Any) -> dict[str, Any]:
    if kind not in QUERIES:
        raise QueryError(f"unknown query {kind!r}; known: {sorted(QUERIES)}")
    return QUERIES[kind](ev, **kwargs)
