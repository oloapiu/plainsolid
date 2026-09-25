"""DXF files as sketch geometry and as drawing sheets.

A sketch's `import_dxf("outline", "vendor/plate.dxf", layer="CUT")` reads the
file's curves in millimetres: lines, arcs and circles as they are, polylines
(bulges included) and blocks exploded, splines and ellipses as short lines
within FLATTEN mm. Units come from the file's $INSUNITS; a file without them
is read as millimetres. Ends closer than SNAP mm are joined so a profile
closes, and the ends that stay open are reported with where they are. Text,
dimensions and hatching are not geometry and stay out without a word; anything
else that is not a curve (or a broken entity) is reported as left out. The solver holds the
curves rigid, placed by `at` (where the file's origin lands) and `angle`.

A drawing's `view("old", dxf="proposals/bracket.dxf")` shows the file as it
is: its curves (dashed where the linetype is), its text, its dimensions and
leaders exploded into lines, arrows and text, hatch patterns as lines and
solid fills as filled polygons.

Files are read once per content: the cache is keyed on the file's stamp.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from .solver.build import Projected, ProjItem

FLATTEN = 0.01      # mm: splines and ellipses as lines no further than this from the curve
SNAP = 0.005        # mm: curve ends closer than this are one point
GAP = 1.0           # mm: an open end this close to another is reported as a gap between them
MAX_CONVERT = 300   # curves: convert_dxf refuses more, the file would drown in statements
MAX_HATCH_LINES = 20000
ANNOTATION = {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION", "LEADER", "MLEADER", "MULTILEADER"}
TEXTS = {"TEXT", "MTEXT", "ATTRIB"}
CURVES = {"LINE", "ARC", "CIRCLE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"}
# what a profile leaves out without a word: annotation, not geometry
NOT_GEOMETRY = ANNOTATION | TEXTS | {"POINT", "VIEWPORT", "HATCH", "SOLID", "TRACE", "3DFACE", "WIPEOUT", "IMAGE", "ATTDEF"}
_CACHE: dict[tuple, Any] = {}

Pt = tuple[float, float]


class DxfError(ValueError):
    pass


@dataclass
class Profile:
    """The curves of a DXF file (or some of its layers) in millimetres, in the file's own coordinates."""

    items: list[ProjItem]
    layers: dict[str, int]  # entities with curves per layer (a polyline counts once), every layer that has any
    warnings: list[str]
    bbox: tuple[float, float, float, float] | None


@dataclass
class Sheet:
    """Everything a DXF file draws, in millimetres: segments as (kind, data) tuples
    ("line", [p, q]), ("poly", [points]), ("circle", c, r), ("arc", c, r, a0, a1)."""

    visible: list[tuple] = field(default_factory=list)
    dashed: list[tuple] = field(default_factory=list)
    annotation: list[tuple] = field(default_factory=list)
    hatch: list[tuple] = field(default_factory=list)
    texts: list[dict[str, Any]] = field(default_factory=list)
    fills: list[list[Pt]] = field(default_factory=list)
    layers: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None


# --- reading --------------------------------------------------------------------------------

def stamp(path: Path) -> tuple[str, int, int]:
    st = path.stat()
    return str(path), st.st_mtime_ns, st.st_size


def _open(path: Path):
    import ezdxf
    from ezdxf import recover

    if not path.is_file():
        raise DxfError(f"no such file: {path}")
    try:
        doc, _auditor = recover.readfile(str(path))
    except OSError as exc:
        raise DxfError(f"cannot read {path.name}: {exc}") from None
    except ezdxf.DXFStructureError as exc:
        raise DxfError(f"{path.name} is not a readable DXF file: {exc}") from None
    return doc


def units_factor(doc) -> tuple[float, str | None]:
    """Millimetres per drawing unit, and a warning when the file does not say."""
    from ezdxf import units

    try:
        code = int(doc.header.get("$INSUNITS", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 0:
        return 1.0, "the file names no units ($INSUNITS); read as millimetres"
    try:
        return float(units.conversion_factor(code, units.MM)), None
    except Exception:  # noqa: BLE001 - an exotic code
        return 1.0, f"unknown units code {code}; read as millimetres"


def _layers_wanted(doc, layer: str | list[str] | None):
    """Which layers to read, as a test on a layer name: the named ones (checked against the
    file), or every layer that is on and thawed."""
    if layer is None:
        hidden = {lay.dxf.name for lay in doc.layers if lay.is_off() or lay.is_frozen()}
        return lambda name: name not in hidden
    names = [layer] if isinstance(layer, str) else list(layer)
    known = {lay.dxf.name.lower(): lay.dxf.name for lay in doc.layers}
    chosen = set()
    for n in names:
        real = known.get(str(n).lower())
        if real is None:
            raise DxfError(f"no layer {n!r} in the file; its layers: {', '.join(sorted(known.values()))}")
        chosen.add(real)
    return lambda name: name in chosen


def _walk(entities, inherited: str | None = None, depth: int = 0):
    """(type, layer, entity) for every entity, blocks exploded; an entity on layer 0 inside a
    block takes the layer of the block reference, as CAD programs draw it."""
    for e in entities:
        t = e.dxftype()
        layer = e.dxf.get("layer", "0") or "0"
        if layer == "0" and inherited:
            layer = inherited
        if t == "INSERT":
            if depth > 16:
                continue
            for att in getattr(e, "attribs", []):
                yield "ATTRIB", att.dxf.get("layer", layer) or layer, att
            try:
                copies = list(e.multi_insert()) if getattr(e, "mcount", 1) > 1 else [e]
                for ins in copies:
                    yield from _walk(list(ins.virtual_entities()), layer, depth + 1)
            except Exception:  # noqa: BLE001 - a broken block reference is left out
                yield "?INSERT", layer, e
        else:
            yield t, layer, e


def _wcs(e, p) -> Pt:
    try:
        v = e.ocs().to_wcs(p)
    except Exception:  # noqa: BLE001 - entities without an OCS are in WCS already
        v = p
    return float(v[0]), float(v[1])


def _curve_items(t: str, e, f: float) -> list[ProjItem]:
    """A curve entity as sketch items in millimetres; nothing for anything else."""
    if t == "LINE":
        s, q = e.dxf.start, e.dxf.end
        return [ProjItem("line", {"start": (s[0] * f, s[1] * f), "end": (q[0] * f, q[1] * f)})]
    if t == "CIRCLE":
        c = _wcs(e, e.dxf.center)
        return [ProjItem("circle", {"center": (c[0] * f, c[1] * f), "radius": float(e.dxf.radius) * f})]
    if t == "ARC":
        c = _wcs(e, e.dxf.center)
        s, q = e.start_point, e.end_point
        if e.dxf.get("extrusion", (0, 0, 1))[2] < 0:  # seen from below: counter-clockwise the other way
            s, q = q, s
        r = float(e.dxf.radius) * f
        if math.hypot(s[0] - q[0], s[1] - q[1]) * f < 1e-9:
            return [ProjItem("circle", {"center": (c[0] * f, c[1] * f), "radius": r})]
        return [ProjItem("arc", {"center": (c[0] * f, c[1] * f), "radius": r,
                                 "start": (s[0] * f, s[1] * f), "end": (q[0] * f, q[1] * f)})]
    if t in ("LWPOLYLINE", "POLYLINE"):
        out: list[ProjItem] = []
        for sub in e.virtual_entities():
            out.extend(_curve_items(sub.dxftype(), sub, f))
        return out
    if t in ("SPLINE", "ELLIPSE"):
        pts = [(float(p[0]) * f, float(p[1]) * f) for p in e.flattening(FLATTEN / f)]
        return [ProjItem("line", {"start": a, "end": b}) for a, b in pairwise(pts)
                if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-9]
    return []


def read_profile(path: Path, layer: str | list[str] | None = None) -> Profile:
    """The curves of the file, ends joined; cached until the file changes."""
    path = Path(path).resolve()
    if not path.is_file():
        raise DxfError(f"no such file: {path}")
    key = ("profile", stamp(path), json_key(layer))
    hit = _CACHE.get(key)
    if hit is None:
        hit = _read_profile(path, layer)
        _remember(key, hit)
    return hit


def json_key(layer) -> Any:
    return tuple(layer) if isinstance(layer, list) else layer


def _remember(key: tuple, value: Any) -> None:
    if len(_CACHE) >= 32:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = value


def _read_profile(path: Path, layer) -> Profile:
    doc = _open(path)
    f, unit_warning = units_factor(doc)
    wanted = _layers_wanted(doc, layer)
    items: list[ProjItem] = []
    layers: dict[str, int] = defaultdict(int)
    left_out: dict[str, int] = defaultdict(int)
    for t, lay, e in _walk(doc.modelspace()):
        if t not in CURVES:
            if t not in NOT_GEOMETRY:
                left_out[t.lstrip("?")] += 1
            continue
        try:
            got = _curve_items(t, e, f)
        except Exception:  # noqa: BLE001 - one bad entity does not lose the file
            left_out[f"broken {t}"] += 1
            continue
        got = [it for it in got if not _degenerate(it)]
        if not got:
            continue
        layers[lay] += 1
        if wanted(lay):
            items.extend(got)
    warnings = [unit_warning] if unit_warning else []
    if not items:
        which = f" on layer {layer!r}" if isinstance(layer, str) else " on those layers" if layer else ""
        raise DxfError(f"{path.name} has no lines, arcs or circles{which}"
                       + (f"; layers with curves: {', '.join(sorted(layers))}" if layers else ""))
    warnings += _join_ends(items)
    if left_out:
        warnings.append("left out: " + ", ".join(f"{n} {k}" for k, n in sorted(left_out.items())))
    return Profile(items, dict(layers), warnings, _items_bbox(items))


def _degenerate(it: ProjItem) -> bool:
    c = it.coords
    if it.kind == "line":
        return math.hypot(c["end"][0] - c["start"][0], c["end"][1] - c["start"][1]) < 1e-9
    return c.get("radius", 1.0) < 1e-9


def _join_ends(items: list[ProjItem]) -> list[str]:
    """Move the ends of lines and arcs that are within SNAP of each other onto their mean, so
    the profile closes exactly; report the ends that stay open, with the gaps under GAP mm."""
    ends: list[tuple[int, str]] = [(i, k) for i, it in enumerate(items) if it.kind in ("line", "arc") for k in ("start", "end")]
    if not ends:
        return []
    parent = list(range(len(ends)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    pos = [items[i].coords[k] for i, k in ends]
    for n, (x, y) in enumerate(pos):
        gx, gy = math.floor(x / SNAP), math.floor(y / SNAP)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for m in grid.get((gx + dx, gy + dy), ()):
                    if math.hypot(pos[m][0] - x, pos[m][1] - y) <= SNAP:
                        parent[find(n)] = find(m)
        grid[(gx, gy)].append(n)
    groups: dict[int, list[int]] = defaultdict(list)
    for n in range(len(ends)):
        groups[find(n)].append(n)
    open_ends: list[Pt] = []
    for members in groups.values():
        if len(members) == 1:
            open_ends.append(pos[members[0]])
            continue
        mx = sum(pos[n][0] for n in members) / len(members)
        my = sum(pos[n][1] for n in members) / len(members)
        for n in members:
            i, k = ends[n]
            items[i] = ProjItem(items[i].kind, {**items[i].coords, k: (mx, my)})
    if not open_ends:
        return []
    gaps: list[tuple[float, Pt, Pt]] = []
    if len(open_ends) <= 2000:
        seen: set[int] = set()
        for a, p in enumerate(open_ends):
            if a in seen:
                continue
            best, bd = None, GAP
            for b, q in enumerate(open_ends):
                if b != a and b not in seen:
                    d = math.hypot(q[0] - p[0], q[1] - p[1])
                    if d <= bd:
                        best, bd = b, d
            if best is not None:
                seen.update((a, best))
                gaps.append((bd, p, open_ends[best]))
    text = f"{len(open_ends)} open end(s)"
    if gaps:
        gaps.sort(key=lambda g: -g[0])
        shown = ", ".join(f"{_n(d)} mm at ({_n(p[0])}, {_n(p[1])})" for d, p, _q in gaps[:3])
        text += f"; {len(gaps)} gap(s) under {_n(GAP)} mm where the outline does not close: {shown}" + (" …" if len(gaps) > 3 else "")
    return [text]


def _n(v: float) -> str:
    t = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if t in ("-0", "") else t


def _items_bbox(items: list[ProjItem]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for it in items:
        c = it.coords
        if it.kind in ("circle", "arc"):
            cx, cy = c["center"]
            r = c["radius"]
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
        for k in ("start", "end"):
            if k in c:
                xs.append(c[k][0])
                ys.append(c[k][1])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


# --- placing ----------------------------------------------------------------------------------

def place(p: Pt, at: Pt, angle: float) -> Pt:
    """A point of the file's coordinates in the sketch: rotated by `angle` degrees, then moved to `at`."""
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return (at[0] + c * p[0] - s * p[1], at[1] + s * p[0] + c * p[1])


def placed(name: str, profile: Profile, at: Pt, angle: float) -> Projected:
    out = []
    for it in profile.items:
        coords = {k: (place(v, at, angle) if k in ("start", "end", "center", "at") else v) for k, v in it.coords.items()}
        out.append(ProjItem(it.kind, coords))
    return Projected(name, out)


def resolve(feature, base: Path) -> tuple[dict[str, Projected], dict[str, list[str]]]:
    """Every import_dxf entity of a sketch, read and placed where the file says, with the
    reader's warnings per entity."""
    out: dict[str, Projected] = {}
    warnings: dict[str, list[str]] = {}
    for e in feature.entities:
        if e.kind != "import_dxf":
            continue
        p = Path(e.args["path"])
        path = p if p.is_absolute() else base / p
        try:
            prof = read_profile(path, e.args.get("layer"))
        except DxfError as exc:
            raise ValueError(f"import_dxf {e.name!r}: {exc}") from None
        at = tuple(e.args.get("at") or (0.0, 0.0))
        out[e.name] = placed(e.name, prof, (float(at[0]), float(at[1])), float(e.args.get("angle") or 0.0))
        warnings[e.name] = list(prof.warnings)
    return out, warnings


# --- a sheet ----------------------------------------------------------------------------------

def read_sheet(path: Path, layer: str | list[str] | None = None) -> Sheet:
    path = Path(path).resolve()
    if not path.is_file():
        raise DxfError(f"no such file: {path}")
    key = ("sheet", stamp(path), json_key(layer))
    hit = _CACHE.get(key)
    if hit is None:
        hit = _read_sheet(path, layer)
        _remember(key, hit)
    return hit


def _dashed(doc, e, layer: str) -> bool:
    lt = str(e.dxf.get("linetype", "BYLAYER") or "BYLAYER").upper()
    if lt == "BYLAYER":
        try:
            lt = str(doc.layers.get(layer).dxf.get("linetype", "CONTINUOUS")).upper()
        except Exception:  # noqa: BLE001 - an unknown layer draws continuous
            lt = "CONTINUOUS"
    return lt not in ("CONTINUOUS", "BYBLOCK", "")


def _segs(items: list[ProjItem]) -> list[tuple]:
    out: list[tuple] = []
    for it in items:
        c = it.coords
        if it.kind == "line":
            out.append(("line", [c["start"], c["end"]]))
        elif it.kind == "circle":
            out.append(("circle", c["center"], c["radius"]))
        elif it.kind == "arc":
            cx, cy = c["center"]
            a0 = math.degrees(math.atan2(c["start"][1] - cy, c["start"][0] - cx)) % 360.0
            a1 = math.degrees(math.atan2(c["end"][1] - cy, c["end"][0] - cx)) % 360.0
            out.append(("arc", c["center"], c["radius"], a0, a1))
    return out


def _text_items(t: str, e, f: float) -> list[dict[str, Any]]:
    """A TEXT, ATTRIB or MTEXT as baseline-anchored lines: at, text, size, anchor, angle."""
    if t == "MTEXT":
        lines = e.plain_text(split=True) or []
        h = float(e.dxf.get("char_height", 2.5)) * f
        rot = float(e.get_rotation()) if hasattr(e, "get_rotation") else float(e.dxf.get("rotation", 0.0))
        ins = e.dxf.insert
        ap = int(e.dxf.get("attachment_point", 1) or 1)
        col, row = (ap - 1) % 3, (ap - 1) // 3
        anchor = ("start", "middle", "end")[col]
        spacing = h * 1.667 * float(e.dxf.get("line_spacing_factor", 1.0) or 1.0)
        total = (len(lines) - 1) * spacing + h
        first = -h if row == 0 else (total / 2 - h) if row == 1 else (len(lines) - 1) * spacing
        ux, uy = -math.sin(math.radians(rot)), math.cos(math.radians(rot))
        out = []
        for i, text in enumerate(lines):
            if not text.strip():
                continue
            off = first - i * spacing
            out.append({"at": (ins[0] * f + ux * off, ins[1] * f + uy * off), "text": text, "size": h, "anchor": anchor, "angle": rot})
        return out
    text = e.plain_text() if hasattr(e, "plain_text") else str(e.dxf.get("text", ""))
    if not text.strip():
        return []
    h = float(e.dxf.get("height", 2.5)) * f
    rot = float(e.dxf.get("rotation", 0.0))
    align, p1, p2 = e.get_placement()
    name = align.name
    p = _wcs(e, p1)
    if name in ("ALIGNED", "FIT") and p2 is not None:
        q = _wcs(e, p2)
        p = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        anchor = "middle"
    else:
        anchor = "end" if "RIGHT" in name else "middle" if ("CENTER" in name or name == "MIDDLE") else "start"
    drop = h if name.startswith("TOP") else h / 2 if (name.startswith("MIDDLE")) else 0.0
    ux, uy = -math.sin(math.radians(rot)), math.cos(math.radians(rot))
    return [{"at": (p[0] * f - ux * drop, p[1] * f - uy * drop), "text": text, "size": h, "anchor": anchor, "angle": rot}]


def _read_sheet(path: Path, layer) -> Sheet:
    from ezdxf import path as dxfpath
    from ezdxf.render import hatching

    doc = _open(path)
    f, unit_warning = units_factor(doc)
    wanted = _layers_wanted(doc, layer)
    sheet = Sheet()
    left_out: dict[str, int] = defaultdict(int)
    layers: dict[str, int] = defaultdict(int)

    def take(t: str, lay: str, e, annotation: bool) -> None:
        if t in CURVES:
            segs = _segs([it for it in _curve_items(t, e, f) if not _degenerate(it)])
            if annotation:
                sheet.annotation.extend(segs)
            elif _dashed(doc, e, lay):
                sheet.dashed.extend(segs)
            else:
                sheet.visible.extend(segs)
        elif t in TEXTS:
            sheet.texts.extend(_text_items(t, e, f))
        elif t in ("SOLID", "TRACE", "3DFACE"):
            pts = [(float(v[0]) * f, float(v[1]) * f) for v in (e.wcs_vertices() if hasattr(e, "wcs_vertices") else [])]
            if len(pts) >= 3:
                sheet.fills.append(pts)
        elif t == "HATCH":
            if e.dxf.get("solid_fill", 0):
                for p in dxfpath.from_hatch(e):
                    pts = [(float(v[0]) * f, float(v[1]) * f) for v in p.flattening(FLATTEN / f)]
                    if len(pts) >= 3:
                        sheet.fills.append(pts)
            else:
                for n, line in enumerate(hatching.hatch_entity(e)):
                    a, b = line.start, line.end
                    sheet.hatch.append(("line", [(a[0] * f, a[1] * f), (b[0] * f, b[1] * f)]))
                    if n >= MAX_HATCH_LINES:
                        left_out["hatch lines past the limit"] += 1
                        break
        elif t in ANNOTATION:
            for st, slay, sub in _walk(list(e.virtual_entities()), lay):  # arrowheads come as block references
                take(st, slay, sub, True)
        elif t not in ("POINT", "VIEWPORT"):
            left_out[t.lstrip("?")] += 1

    for t, lay, e in _walk(doc.modelspace()):
        if not wanted(lay):
            continue
        layers[lay] += 1
        try:
            take(t, lay, e, False)
        except Exception:  # noqa: BLE001 - one bad entity does not lose the sheet
            left_out[f"broken {t}"] += 1
    sheet.layers = dict(layers)
    if unit_warning:
        sheet.warnings.append(unit_warning)
    if left_out:
        sheet.warnings.append("not drawn: " + ", ".join(f"{n} {k}" for k, n in sorted(left_out.items())))
    sheet.bbox = _sheet_bbox(sheet)
    if sheet.bbox is None:
        raise DxfError(f"{path.name} draws nothing" + (" on those layers" if layer else ""))
    return sheet


def _sheet_bbox(sheet: Sheet) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for seg in (*sheet.visible, *sheet.dashed, *sheet.annotation, *sheet.hatch):
        if seg[0] in ("line", "poly"):
            xs += [p[0] for p in seg[1]]
            ys += [p[1] for p in seg[1]]
        else:
            (cx, cy), r = seg[1], seg[2]
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
    for poly in sheet.fills:
        xs += [p[0] for p in poly]
        ys += [p[1] for p in poly]
    for t in sheet.texts:
        xs.append(t["at"][0])
        ys.append(t["at"][1])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def layers_of(path: Path) -> dict[str, Any]:
    """What the open dialog shows: units, extents and the curves per layer."""
    doc = _open(Path(path))
    f, unit_warning = units_factor(doc)
    counts: dict[str, int] = defaultdict(int)
    for t, lay, _e in _walk(doc.modelspace()):
        if t in CURVES:
            counts[lay] += 1
    return {"layers": dict(counts), "mm_per_unit": f, "warning": unit_warning}


# --- the wrappers that open a DXF -------------------------------------------------------------

SHEET_ORDER = ("A4", "A3")
REDUCTIONS = (1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01)


def sheet_fit(width: float, height: float) -> tuple[str, float, tuple[float, float]]:
    """The smallest sheet and the largest standard scale (1:1 down to 1:100) at which a
    drawing of this size fits above the title block, and where its centre goes."""
    from .drawing import MARGIN, SHEETS, TITLE_H

    for scale in REDUCTIONS:
        for name in SHEET_ORDER:
            w, h = SHEETS[name]
            room_w, room_h = w - 2 * MARGIN - 10, h - 2 * MARGIN - TITLE_H - 10
            if width * scale <= room_w and height * scale <= room_h:
                return name, scale, (round(w / 2, 1), round((MARGIN + TITLE_H + h - MARGIN) / 2, 1))
    w, h = SHEETS["A3"]
    return "A3", REDUCTIONS[-1], (round(w / 2, 1), round((MARGIN + TITLE_H + h - MARGIN) / 2, 1))


def part_source(dxf_name: str, label: str) -> str:
    """A part holding the DXF's curves in a sketch on XY, where the file puts them, and nothing
    else: what to make of them (an extrude, a cut, a revolve, one loop of several) is the
    person's choice, and the sketch alone never fails."""
    from .literals import python_literal

    return (
        "from plainsolid import *\n\n"
        f"meta(name={python_literal(label)})\n\n"
        'profile = sketch("profile", on=XY)\n'
        f'profile.import_dxf("outline", {python_literal(dxf_name)})\n'
        'profile.fix("placed", "outline")\n'
    )


def drawing_source(dxf_path: Path, dxf_name: str, label: str, name: str | None = None) -> str:
    """A drawing sheet showing the DXF as it is, on the smallest sheet that holds it, titled
    `label` and named `name` (default `label_dwg`)."""
    from .literals import python_literal

    try:
        sheet = read_sheet(dxf_path)
        x0, y0, x1, y1 = sheet.bbox
        size, scale, at = sheet_fit(x1 - x0, y1 - y0)
    except DxfError:
        size, scale, at = "A3", 1.0, (210.0, 164.5)
    fields = ['kind="drawing"', f"name={python_literal(name or label + '_dwg')}", f"title={python_literal(label)}", f'sheet="{size}"']
    if scale != 1.0:
        fields.append(f"scale={scale:g}")
    return (
        "from plainsolid import *\n\n"
        f"meta({', '.join(fields)})\n\n"
        f'sheet = view("sheet", dxf={python_literal(dxf_name)}, at=({at[0]:g}, {at[1]:g}))\n'
    )


# --- converting an import into sketch geometry --------------------------------------------------

def convert_ops(feature, projected: dict[str, Projected] | None, entity: str) -> list[dict[str, Any]]:
    """The batch that turns an import_dxf entity into lines, arcs and circles where it stands:
    each curve becomes an entity named after the import (`outline_0`, ...), touching ends get a
    coincident, and the relations on its curves move to the new entities. Relations on the import
    as a whole (its `fix`, its `.origin`) go: the new geometry is free, and stays where it is."""
    from .filleting import Names

    e = feature.entity(entity)
    if e is None:
        raise DxfError(f"no entity {entity!r}")
    if e.kind != "import_dxf":
        raise DxfError(f"{entity!r} is a {e.kind}, not a DXF import")
    pr = (projected or {}).get(entity)
    if pr is None:
        raise DxfError(f"{entity!r} could not be read; fix the import first")
    items = [it for it in pr.items if it.kind in ("line", "arc", "circle")]
    if len(items) > MAX_CONVERT:
        raise DxfError(f"{entity!r} has {len(items)} curves, more than {MAX_CONVERT} to write as statements; "
                       "import one layer at a time with layer=, or keep it as an import")
    sketch = feature.name
    names = Names(feature)
    names.taken.discard(entity)
    new = [_fresh(names, f"{entity}_{i}") for i in range(len(items))]
    ops: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    single = len(pr.items) == 1
    for c in feature.constraints:
        if not any(r.split(".")[0] == entity for r in c.refs):
            continue
        ops.append({"op": "delete_constraint", "sketch": sketch, "constraint": c.name})
        refs = [_repoint(r, entity, new, single) for r in c.refs]
        if any(r is None for r in refs):
            continue  # on the import as a whole, or on its origin
        moved.append({"op": "add_constraint", "sketch": sketch, "kind": c.kind, "name": c.name, "refs": refs,
                      **({"value": {"expr": c.value_text}} if c.value_text else {"value": c.value} if c.value is not None else {}),
                      **({"options": dict(c.options)} if c.options else {})})
    ops.append({"op": "delete_sketch_entity", "sketch": sketch, "entity": entity})
    construction = {"construction": True} if e.construction else {}
    for name, it in zip(new, items, strict=True):
        c = it.coords
        if it.kind == "line":
            args = {"start": _r(c["start"]), "end": _r(c["end"])}
        elif it.kind == "arc":
            args = {"center": _r(c["center"]), "start": _r(c["start"]), "end": _r(c["end"])}
        else:
            args = {"diameter": _clean(2 * c["radius"]), "at": _r(c["center"])}
        ops.append({"op": "add_sketch_entity", "sketch": sketch, "kind": it.kind, "name": name, "args": {**args, **construction}})
    ends: dict[tuple[float, float], list[str]] = defaultdict(list)
    for name, it in zip(new, items, strict=True):
        if it.kind in ("line", "arc"):
            for k in ("start", "end"):
                p = it.coords[k]
                ends[(round(p[0], 6), round(p[1], 6))].append(f"{name}.{k}")
    for refs in ends.values():
        for a, b in pairwise(refs):
            ops.append({"op": "add_constraint", "sketch": sketch, "kind": "coincident", "name": names.next("coincident"), "refs": [a, b]})
    return ops + moved


def _fresh(names, base: str) -> str:
    name, i = base, 1
    while name in names.taken:
        i += 1
        name = f"{base}_{i}"
    names.taken.add(name)
    return name


def _repoint(ref: str, entity: str, new: list[str], single: bool) -> str | None:
    """A reference into the import, in terms of the converted entities; None when it names the
    import as a whole or its origin."""
    head, _, rest = ref.partition(".")
    if head != entity:
        return ref
    if not rest:
        return new[0] if single else None
    first, _, tail = rest.partition(".")
    if first == "origin":
        return None
    if first.startswith("e") and first[1:].isdigit():
        i = int(first[1:])
        if i >= len(new):
            return None
        return new[i] + (f".{tail}" if tail else "")
    return f"{new[0]}.{rest}"  # the first curve's own parts: outline.start when it has one curve


def _r(p: Pt) -> list[float]:
    return [_clean(p[0]), _clean(p[1])]


def _clean(v: float) -> float | int:
    """A coordinate as the file would show it: whole numbers without a decimal point."""
    r = round(float(v), 6)
    return int(r) if r == int(r) else r
