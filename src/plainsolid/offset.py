"""Offset entities: sketch curves derived from other curves at a distance.

`s.offset("o1", ["line1", "arc1"], 2, side="left")` follows its sources: after
every solve the offset is recomputed from the solved coordinates, so it is
never written back and always agrees with what it offsets. Its pieces are
fixed geometry for the solver (`o1.e0`, `o1.e1`, ... or `o1` when there is
one), and profile geometry unless the entity is construction.

A closed loop offsets `outside` or `inside`; an open chain `left` or `right`
of its direction. Corners are sharp (`corners="sharp"`) or rounded with arcs
(`corners="round"`).
"""
from __future__ import annotations

from build123d import Edge, Kind, Plane, Side, Wire

from .model import Feature
from .projection import edge_item
from .sketchgeom import SketchError, profile_edges
from .solver.build import Projected

SIDES = ("outside", "inside", "left", "right")
CORNERS = ("sharp", "round")


def references_offsets(feature: Feature) -> bool:
    """Does any constraint of the sketch name an offset entity (or a piece of one)?"""
    names = {e.name for e in feature.entities if e.kind == "offset"}
    return any(r.split(".")[0] in names for c in feature.constraints for r in c.refs)


def compute_offsets(feature: Feature, coords, projected: dict[str, Projected]) -> dict[str, Projected]:
    """Every offset entity of the sketch as derived items, from the entity
    coordinates (solved when `coords` is given, drawn otherwise) and the
    projected body geometry. Offsets defined earlier feed later ones."""
    out: dict[str, Projected] = {}
    for e in feature.entities:
        if e.kind != "offset":
            continue
        available = {**projected, **out}
        labelled = profile_edges(feature, coords, available, include_construction=True)
        edges = _source_edges(e.args["of"], labelled, e.name)
        items = [edge_item(ed, Plane.XY) for ed in _offset_edges(edges, e.args, e.name)]
        out[e.name] = Projected(e.name, items)
    return out


def _source_edges(refs, labelled: list[tuple[Edge, str]], name: str) -> list[Edge]:
    edges: list[Edge] = []
    for ref in refs:
        exact = [ed for ed, label in labelled if label == ref]
        under = [ed for ed, label in labelled if label.startswith(ref + ".")]
        found = exact or under
        if not found:
            raise SketchError(f"offset {name!r}: {ref!r} is not a curve of this sketch (points and other offsets' names must come earlier)")
        edges.extend(found)
    if not edges:
        raise SketchError(f"offset {name!r}: nothing to offset")
    return edges


def _offset_edges(edges: list[Edge], args, name: str) -> list[Edge]:
    distance = float(args["distance"])
    side = str(args.get("side", "outside"))
    corners = str(args.get("corners", "sharp"))
    kind = Kind.ARC if corners == "round" else Kind.INTERSECTION
    # a lone circle: the same circle, bigger or smaller
    if len(edges) == 1 and edges[0].geom_type.name == "CIRCLE" and edges[0].is_closed:
        r = float(edges[0].radius)
        new_r = r + distance if side == "outside" else r - distance
        if new_r <= 1e-6:
            raise SketchError(f"offset {name!r}: {distance} is more than the circle's radius {r:.4g}")
        if side in ("left", "right"):
            raise SketchError(f"offset {name!r}: a circle offsets outside or inside")
        c = edges[0].arc_center
        return [Edge.make_circle(new_r, Plane((c.X, c.Y, 0)))]
    wires = Wire.combine(edges)
    if len(wires) != 1:
        raise SketchError(f"offset {name!r}: the sources must form one connected chain, they make {len(wires)}")
    wire = wires[0]
    if wire.is_closed:
        if side not in ("outside", "inside"):
            raise SketchError(f"offset {name!r}: a closed loop offsets outside or inside, not {side}")
        signed = distance if side == "outside" else -distance
        try:
            result = wire.offset_2d(signed, kind=kind)
        except Exception as exc:  # noqa: BLE001
            raise SketchError(f"offset {name!r}: the kernel could not offset the loop by {signed}: {exc}") from None
        if side == "inside" and abs(_area(result)) >= abs(_area(wire)):
            raise SketchError(f"offset {name!r}: {distance} inside collapses the loop")
    else:
        if side not in ("left", "right"):
            raise SketchError(f"offset {name!r}: an open chain offsets left or right of its direction, not {side}")
        try:
            result = wire.offset_2d(distance, kind=kind, side=Side.LEFT if side == "left" else Side.RIGHT, closed=False)
        except Exception as exc:  # noqa: BLE001
            raise SketchError(f"offset {name!r}: the kernel could not offset the chain by {distance}: {exc}") from None
    out = list(result.edges()) if isinstance(result, Wire) else [result]
    if not out:
        raise SketchError(f"offset {name!r}: the offset vanished")
    return out


def _area(w: Wire) -> float:
    try:
        from build123d import Face

        return float(Face(w).area)
    except Exception:  # noqa: BLE001
        return 0.0
