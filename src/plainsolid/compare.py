"""Overlay compare: what this document's geometry adds to and removes from
another's (a file, or this file at a git revision), as numbers (a volumetric
change report) and as three coloured items (common grey, added green, removed
red) for the viewport and the renderer. The direction is from the other to this
document: "added" is what this document has and the other does not.

The other document is a file, or the same file at a git revision, so "what did
this edit change" and "what did the CM change between proposals" are the same
question."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from build123d import Compound, Shape

from .evaluate import Evaluation, Item, evaluate
from .parse import parse_document
from .query import QueryError, _shapes

COMMON = (0.72, 0.74, 0.77, 1.0)
ADDED = (0.24, 0.68, 0.34, 1.0)
REMOVED = (0.86, 0.32, 0.26, 1.0)
MAX_REGIONS = 20


class CompareError(ValueError):
    pass


def _solid(ev: Evaluation) -> Shape:
    shapes = _shapes(ev)
    return shapes[0] if len(shapes) == 1 else Compound(shapes)


def _v(vec) -> list[float]:
    return [round(float(c), 6) for c in vec]


def _bbox(shape: Shape) -> dict[str, list[float]]:
    b = shape.bounding_box()
    return {"min": _v(b.min), "max": _v(b.max), "size": _v(b.size), "center": _v(b.center())}


def _regions(shape: Shape, tol: float) -> tuple[float, list[dict[str, Any]]]:
    """The solids of a boolean result above the tolerance, largest first."""
    solids = [s for s in shape.solids() if s.volume > tol]
    solids.sort(key=lambda s: -s.volume)
    total = round(sum(s.volume for s in solids), 6)
    return total, [{"volume": round(s.volume, 6), **_bbox(s)} for s in solids[:MAX_REGIONS]]


def compare(base: Evaluation, other: Evaluation) -> tuple[dict[str, Any], list[Item]]:
    """The report and the overlay items. Volumes below one millionth of the larger
    body are noise from the booleans and count as no change."""
    try:
        a, b = _solid(base), _solid(other)
    except QueryError as exc:
        raise CompareError(str(exc)) from None
    tol = max(a.volume, b.volume, 1.0) * 1e-6
    added_shape = a.cut(b)
    removed_shape = b.cut(a)
    common_shape = a.intersect(b)
    added, added_regions = _regions(added_shape, tol)
    removed, removed_regions = _regions(removed_shape, tol)
    common = round(sum(s.volume for s in common_shape.solids()), 6)
    report = {
        "base": {"volume": round(a.volume, 6), **_bbox(a)},
        "other": {"volume": round(b.volume, 6), **_bbox(b)},
        "common": common,
        "added": {"volume": added, "regions": added_regions, "count": len(added_regions)},
        "removed": {"volume": removed, "regions": removed_regions, "count": len(removed_regions)},
        "change": round(a.volume - b.volume, 6),
        "same": added == 0 and removed == 0,
        "unit": "mm^3",
    }
    items: list[Item] = []
    for name, shape, color in (("common", common_shape, COMMON), ("added", added_shape, ADDED),
                               ("removed", removed_shape, REMOVED)):
        solids = [s for s in shape.solids() if s.volume > tol]
        if solids:
            items.append(Item(name=name, path=name, shape=Compound(solids) if len(solids) > 1 else solids[0],
                              color=color))
    return report, items


def git_source(path: Path, rev: str) -> str:
    """The text of a file at a git revision (`HEAD`, `HEAD~1`, a branch, a commit)."""
    path = Path(path).resolve()
    try:
        top = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompareError(f"git is not available: {exc}") from None
    if top.returncode != 0:
        raise CompareError(f"{path.parent} is not inside a git repository")
    rel = path.relative_to(Path(top.stdout.strip()).resolve()).as_posix()
    show = subprocess.run(["git", "-C", str(path.parent), "show", f"{rev}:{rel}"],
                          capture_output=True, text=True, timeout=10, check=False)
    if show.returncode != 0:
        raise CompareError(f"git has no {rel} at {rev}: {show.stderr.strip()}")
    return show.stdout


def other_document(base_path: Path, other: str | None = None, rev: str | None = None):
    """Resolve the comparison source afresh, including moving git references such as HEAD."""
    base_path = Path(base_path).resolve()
    if rev:
        source = git_source(base_path, rev)
        doc = parse_document(source, str(base_path))
    elif other:
        p = Path(other)
        if not p.is_absolute():
            p = base_path.parent / p
        if not p.exists():
            raise CompareError(f"no such file: {other}")
        doc = parse_document(p.read_text(encoding="utf-8"), str(p.resolve()))
    else:
        raise CompareError("compare needs another file or a git revision (rev=HEAD)")
    if doc.errors:
        raise CompareError(f"the other document does not parse: {doc.errors[0].message}")
    return doc


def evaluate_other(base_path: Path, other: str | None = None, rev: str | None = None,
                   upto: str | None = None) -> Evaluation:
    """Evaluate the other source at its original path so relative imports resolve."""
    doc = other_document(base_path, other, rev)
    ev = evaluate(doc, upto=upto)
    if not ev.has_geometry:
        raise CompareError("the other document has no geometry")
    return ev


__all__ = ["ADDED", "COMMON", "REMOVED", "CompareError", "compare", "evaluate_other", "git_source"]
