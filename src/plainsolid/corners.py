"""Rounded and bevelled corners: the arc or the chamfer line at a corner between two sides.
Shared by the profile builder, the solver's derived references and the fillet operation,
and mirrored in the browser's sketch model, so every reader agrees on where a fillet goes."""
from __future__ import annotations

import math
from typing import Any

Pt = tuple[float, float]

RECT_CORNERS = ("tl", "tr", "br", "bl")
# the rect's corners counter-clockwise, as the profile walks them
RECT_ORDER = ("bl", "br", "tr", "tl")
RECT_SIGNS = {"tl": (-1.0, 1.0), "tr": (1.0, 1.0), "br": (1.0, -1.0), "bl": (-1.0, -1.0)}


RECT_SIDES = ("bottom", "right", "top", "left")  # in the order the profile walks them


def side_names(kind: str, args: dict[str, Any]) -> tuple[str, ...]:
    """The sides of a rect or polygon in walking order, by name."""
    if kind == "rect":
        return RECT_SIDES
    if kind == "polygon":
        return tuple(f"e{i}" for i in range(len(args.get("points", []))))
    return ()


def corner_names(kind: str, args: dict[str, Any]) -> tuple[str, ...]:
    if kind == "rect":
        return RECT_CORNERS
    if kind == "polygon":
        return tuple(f"p{i}" for i in range(len(args.get("points", []))))
    return ()


def corner_spec(value: Any, names: tuple[str, ...], what: str) -> dict[str, float]:
    """`corners=8` or `corners={"tl": 8}` as a size per corner; corners at zero are absent."""
    if value is None:
        return {}
    if isinstance(value, bool):
        raise ValueError(f"{what}: expected a number or a dict of corner names to numbers, got {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"{what}: a corner size cannot be negative, got {value!r}")
        return {n: float(value) for n in names} if value > 0 else {}
    if isinstance(value, dict):
        out: dict[str, float] = {}
        for k, v in value.items():
            if k not in names:
                raise ValueError(f"{what}: unknown corner {k!r}; the corners are {', '.join(names)}")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{what}: corner {k!r} expected a number, got {v!r}")
            if v < 0:
                raise ValueError(f"{what}: corner {k!r} cannot be negative, got {v!r}")
            if v > 0:
                out[k] = float(v)
        return out
    raise ValueError(f"{what}: expected a number or a dict of corner names to numbers, got {value!r}")


def _unit(v: Pt) -> Pt:
    n = math.hypot(v[0], v[1])
    if n < 1e-12:
        raise ValueError("a side of zero length")
    return (v[0] / n, v[1] / n)


def corner_geometry(prev: Pt, p: Pt, nxt: Pt, *, radius: float | None = None, chamfer: float | None = None) -> dict[str, Any]:
    """The rounding or bevel at corner p between the sides towards prev and nxt: the points where
    the sides are cut (t1 towards prev, t2 towards nxt), how far along each side that is (t), and
    for a radius the centre and the arc's counter-clockwise start and end. Raises when it does
    not fit on either side or the sides are colinear."""
    u1, u2 = _unit((prev[0] - p[0], prev[1] - p[1])), _unit((nxt[0] - p[0], nxt[1] - p[1]))
    cos_t = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
    theta = math.acos(cos_t)
    if theta < 1e-6 or theta > math.pi - 1e-6:
        raise ValueError("the sides are colinear")
    l1, l2 = math.hypot(prev[0] - p[0], prev[1] - p[1]), math.hypot(nxt[0] - p[0], nxt[1] - p[1])
    if radius is not None:
        t = radius / math.tan(theta / 2)
    elif chamfer is not None:
        t = chamfer
    else:
        raise ValueError("a radius or a chamfer is needed")
    if t > l1 + 1e-9 or t > l2 + 1e-9:
        raise ValueError(f"needs {t:.4g} along each side, the sides are {l1:.4g} and {l2:.4g}")
    t1: Pt = (p[0] + t * u1[0], p[1] + t * u1[1])
    t2: Pt = (p[0] + t * u2[0], p[1] + t * u2[1])
    out: dict[str, Any] = {"t1": t1, "t2": t2, "t": t}
    if radius is not None:
        bis = _unit((u1[0] + u2[0], u1[1] + u2[1]))
        dist = radius / math.sin(theta / 2)
        c: Pt = (p[0] + dist * bis[0], p[1] + dist * bis[1])
        cross = (t1[0] - c[0]) * (t2[1] - c[1]) - (t1[1] - c[1]) * (t2[0] - c[0])
        out.update(center=c, start=t1 if cross > 0 else t2, end=t2 if cross > 0 else t1)
    return out


def macro_corner_points(kind: str, args: dict[str, Any]) -> list[tuple[str, Pt]]:
    """The corners of a rect or polygon in the order the profile walks them (a rect
    counter-clockwise, a polygon as its points were given), each with its name."""
    if kind == "rect":
        cx, cy = args["at"]
        w, h = args["width"] / 2, args["height"] / 2
        return [(n, (cx + RECT_SIGNS[n][0] * w, cy + RECT_SIGNS[n][1] * h)) for n in RECT_ORDER]
    if kind == "polygon":
        return [(f"p{i}", (float(x), float(y))) for i, (x, y) in enumerate(args["points"])]
    return []


def macro_corners(kind: str, args: dict[str, Any], what: str) -> dict[str, dict[str, Any]]:
    """Every rounded or bevelled corner of a rect or polygon with its geometry, checked to fit:
    a side shared by two cut corners must be long enough for both. Raises naming the corner."""
    names = corner_names(kind, args)
    radii = corner_spec(args.get("corners"), names, f"{what}: corners")
    bevels = corner_spec(args.get("chamfers"), names, f"{what}: chamfers")
    both = sorted(set(radii) & set(bevels))
    if both:
        raise ValueError(f"{what}: corner {both[0]!r} cannot be both rounded and chamfered")
    if not radii and not bevels:
        return {}
    pts = macro_corner_points(kind, args)
    n = len(pts)
    out: dict[str, dict[str, Any]] = {}
    for i, (name, p) in enumerate(pts):
        r, d = radii.get(name), bevels.get(name)
        if r is None and d is None:
            continue
        prev, nxt = pts[i - 1][1], pts[(i + 1) % n][1]
        try:
            g = corner_geometry(prev, p, nxt, radius=r, chamfer=d)
        except ValueError as exc:
            size = f"radius {r:g}" if r is not None else f"chamfer {d:g}"
            raise ValueError(f"{what}: corner {name} {size} does not fit ({exc})") from None
        g["kind"] = "arc" if r is not None else "chamfer"
        g["size"] = r if r is not None else d
        out[name] = g
    # a side between two cut corners must hold both cuts
    for i, (name, p) in enumerate(pts):
        nxt_name, q = pts[(i + 1) % n]
        a, b = out.get(name), out.get(nxt_name)
        if a and b:
            side = math.hypot(q[0] - p[0], q[1] - p[1])
            if a["t"] + b["t"] > side + 1e-9:
                raise ValueError(f"{what}: corner {name} does not fit next to {nxt_name} "
                                 f"({a['t']:.4g} + {b['t']:.4g} on a side of {side:.4g})")
    return out
