"""Export the evaluated body, or the posed assembly with its instance names
and colours, to STEP and STL; a drawing to SVG, DXF or PDF."""
from __future__ import annotations

from pathlib import Path

from build123d import export_step, export_stl

from . import assembly as pasm
from . import drawing as pdrawing
from .evaluate import Evaluation


class ExportError(ValueError):
    pass


def export(ev: Evaluation, fmt: str, path: str | Path) -> Path:
    path = Path(path)
    name = ev.document.meta.get("name")
    if ev.document.kind == "drawing":
        try:
            return pdrawing.export(ev, fmt, path)
        except pdrawing.DrawingError as exc:
            raise ExportError(str(exc)) from None
    if fmt.lower().lstrip(".") in ("svg", "dxf", "pdf"):
        raise ExportError(f"{fmt} is a drawing format; make a drawing of this file (plainsolid new dwg.py --kind drawing --of {Path(ev.document.path or 'part.py').name})")
    if ev.document.kind == "assembly":
        roots = ev.roots
        if not any(leaf.shape is not None for r in roots for leaf in r.leaves()):
            raise ExportError("the assembly has no instances to export")
        body = pasm.export_compound(roots, name)
    else:
        if ev.body is None:
            raise ExportError("the document has no body to export")
        body = ev.body
        if name:
            body.label = str(name)
    fmt = fmt.lower().lstrip(".")
    if fmt in ("step", "stp"):
        export_step(body, path)
    elif fmt == "stl":
        export_stl(body, path, tolerance=0.01, angular_tolerance=0.1)
    else:
        raise ExportError(f"unknown export format {fmt!r}; known: step, stl")
    return path
