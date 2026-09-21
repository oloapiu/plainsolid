// The 2D picture of a sketch: handles (named points), curves (named lines, circles, arcs),
// their constraint status, hit testing, the constraint acceptance table and dimension
// measurement. Pure data, built from the tree feature and its solved coordinates.
import type { JsonValue, Constraint, Entity, Feature, ProjectedItem, SketchSolution } from '../api/types';

export type Pt = [number, number];
export type RefKind = 'point' | 'line' | 'circle';

export interface Handle { ref: string; entity: string; p: Pt; kind: 'end' | 'center' | 'mid' | 'corner' | 'point' }
export type Curve =
  | { ref: string; entity: string; kind: 'line'; a: Pt; b: Pt; decor?: boolean; construction?: boolean }
  | { ref: string; entity: string; kind: 'circle'; c: Pt; r: number; construction?: boolean }
  | { ref: string; entity: string; kind: 'arc'; c: Pt; r: number; a0: number; a1: number; construction?: boolean };

export interface EntityInfo { name: string; kind: string; construction: boolean; projected: boolean; builtin?: boolean; constructionSides?: Set<string> }

export const RECT_SIDES = ['bottom', 'right', 'top', 'left'] as const;
/** The sides of a rect or polygon, in the order the profile walks them. */
export function sideNames(kind: string, a: Record<string, unknown>): string[] {
  if (kind === 'rect') return [...RECT_SIDES];
  if (kind === 'polygon') return (Array.isArray(a.points) ? a.points : []).map((_, i) => `e${i}`);
  return [];
}
/** Whether a reference draws as construction: its entity, or its side of a rect or polygon. */
export function isConstructionRef(m: SketchModel, ref: string): boolean {
  const info = m.entities.get(entityOf(ref));
  if (!info) return false;
  if (info.construction) return true;
  const part = ref.slice(info.name.length + 1);
  return Boolean(part && info.constructionSides?.has(part));
}

/** References every sketch has without declaring them: its origin and its axes, fixed. */
export const BUILTIN_REFS = ['origin', 'x_axis', 'y_axis'] as const;
export const isBuiltin = (ref: string): boolean => (BUILTIN_REFS as readonly string[]).includes(entityOf(ref));
const AXIS_REACH = 1e4;  // the axes are lines without ends; drawn across the grid, hit anywhere

export interface SketchModel {
  handles: Handle[];
  curves: Curve[];
  entities: Map<string, EntityInfo>;
  free: Set<string>;         // under-constrained entity names
  conflicting: Set<string>;  // entity names touched by a conflicting constraint
  constraints: Constraint[];
  solution: SketchSolution | null;
}

const num = (v: unknown, d = 0) => (typeof v === 'number' && Number.isFinite(v) ? v : d);
const pt = (v: unknown, d: Pt = [0, 0]): Pt => (Array.isArray(v) && v.length === 2 ? [num(v[0]), num(v[1])] : d);

export function entityOf(ref: string): string { return ref.split('.')[0]; }

/** Solved args of an entity (the file's args when the solver had nothing to say). */
export function solvedArgs(e: Entity, sol: SketchSolution | null, override?: Record<string, Record<string, unknown>> | null): Record<string, unknown> {
  return { ...e.args, ...(sol?.coords?.[e.name] ?? {}), ...(override?.[e.name] ?? {}) };
}

export function buildModel(f: Feature, override?: Record<string, Record<string, unknown>> | null): SketchModel {
  const sol = f.result?.sketch ?? null;
  const handles: Handle[] = [];
  const curves: Curve[] = [];
  const entities = new Map<string, EntityInfo>();
  const conflicting = new Set<string>();
  const H = (ref: string, entity: string, p: Pt, kind: Handle['kind']) => handles.push({ ref, entity, p, kind });
  const L = (ref: string, entity: string, a: Pt, b: Pt, decor = false, construction = false) => curves.push({ ref, entity, kind: 'line', a, b, decor, construction });
  // the built-ins first, so user geometry drawn later paints over them
  entities.set('origin', { name: 'origin', kind: 'origin', construction: true, projected: true, builtin: true });
  entities.set('x_axis', { name: 'x_axis', kind: 'axis', construction: true, projected: true, builtin: true });
  entities.set('y_axis', { name: 'y_axis', kind: 'axis', construction: true, projected: true, builtin: true });
  H('origin', 'origin', [0, 0], 'point');
  L('x_axis', 'x_axis', [-AXIS_REACH, 0], [AXIS_REACH, 0]);
  L('y_axis', 'y_axis', [0, -AXIS_REACH], [0, AXIS_REACH]);
  for (const e of f.entities) {
    const a = solvedArgs(e, sol, override);
    const n = e.name;
    const cs = new Set(e.construction_sides ?? []);
    entities.set(n, { name: n, kind: e.kind, construction: e.construction, projected: e.kind === 'project' || e.kind === 'offset', constructionSides: cs });
    switch (e.kind) {
      case 'point': H(n, n, pt(a.at), 'point'); break;
      case 'line': {
        const s = pt(a.start), t = pt(a.end);
        H(`${n}.start`, n, s, 'end'); H(`${n}.end`, n, t, 'end'); H(`${n}.mid`, n, [(s[0] + t[0]) / 2, (s[1] + t[1]) / 2], 'mid');
        L(n, n, s, t);
        break;
      }
      case 'circle': {
        const c = pt(a.at);
        H(`${n}.center`, n, c, 'center');
        curves.push({ ref: n, entity: n, kind: 'circle', c, r: num(a.diameter) / 2 });
        break;
      }
      case 'arc': {
        const c = pt(a.center), s = pt(a.start), t = pt(a.end);
        H(`${n}.center`, n, c, 'center'); H(`${n}.start`, n, s, 'end'); H(`${n}.end`, n, t, 'end');
        curves.push({ ref: n, entity: n, kind: 'arc', c, r: Math.hypot(s[0] - c[0], s[1] - c[1]), a0: Math.atan2(s[1] - c[1], s[0] - c[0]), a1: Math.atan2(t[1] - c[1], t[0] - c[0]) });
        break;
      }
      case 'rect': {
        const c = pt(a.at), w = num(a.width) / 2, h = num(a.height) / 2;
        const tl: Pt = [c[0] - w, c[1] + h], tr: Pt = [c[0] + w, c[1] + h], bl: Pt = [c[0] - w, c[1] - h], br: Pt = [c[0] + w, c[1] - h];
        H(`${n}.center`, n, c, 'center'); H(`${n}.tl`, n, tl, 'corner'); H(`${n}.tr`, n, tr, 'corner'); H(`${n}.bl`, n, bl, 'corner'); H(`${n}.br`, n, br, 'corner');
        if (!macroOutline(handles, curves, conflicting, n, 'rect', a, [...RECT_SIDES], cs)) {
          L(`${n}.top`, n, tl, tr, false, cs.has('top')); L(`${n}.bottom`, n, bl, br, false, cs.has('bottom')); L(`${n}.left`, n, bl, tl, false, cs.has('left')); L(`${n}.right`, n, br, tr, false, cs.has('right'));
        }
        break;
      }
      case 'slot': {
        const c = pt(a.at), len = num(a.length), wd = num(a.width), ang = (num(a.angle) * Math.PI) / 180;
        const half = Math.max(len - wd, 0) / 2, r = wd / 2;
        const d: Pt = [Math.cos(ang), Math.sin(ang)], nrm: Pt = [-d[1], d[0]];
        const s: Pt = [c[0] - half * d[0], c[1] - half * d[1]], t: Pt = [c[0] + half * d[0], c[1] + half * d[1]];
        H(`${n}.center`, n, c, 'center'); H(`${n}.start`, n, s, 'end'); H(`${n}.end`, n, t, 'end');
        L(`${n}.axis`, n, s, t);
        curves.push({ ref: `${n}.start_arc`, entity: n, kind: 'arc', c: s, r, a0: ang + Math.PI / 2, a1: ang + 3 * Math.PI / 2 });
        curves.push({ ref: `${n}.end_arc`, entity: n, kind: 'arc', c: t, r, a0: ang - Math.PI / 2, a1: ang + Math.PI / 2 });
        L('', n, [s[0] + r * nrm[0], s[1] + r * nrm[1]], [t[0] + r * nrm[0], t[1] + r * nrm[1]], true);
        L('', n, [s[0] - r * nrm[0], s[1] - r * nrm[1]], [t[0] - r * nrm[0], t[1] - r * nrm[1]], true);
        break;
      }
      case 'polygon': {
        const pts = (Array.isArray(a.points) ? a.points : []).map((p) => pt(p));
        pts.forEach((p, i) => H(`${n}.p${i}`, n, p, 'corner'));
        if (!macroOutline(handles, curves, conflicting, n, 'polygon', a, pts.map((_, i) => `e${i}`), cs)) {
          pts.forEach((p, i) => L(`${n}.e${i}`, n, p, pts[(i + 1) % pts.length], false, cs.has(`e${i}`)));
        }
        break;
      }
      case 'project':
      case 'offset': {
        const items = sol?.projected?.[n]?.items ?? [];
        items.forEach((it, i) => addProjected(handles, curves, n, items.length === 1 ? n : `${n}.e${i}`, it));
        break;
      }
      default: break;
    }
  }
  const constraints = f.constraints ?? [];
  for (const c of constraints) if (sol?.conflicting?.includes(c.name)) for (const r of c.refs) conflicting.add(entityOf(r));
  return { handles, curves, entities, free: new Set(sol?.free_entities ?? []), conflicting, constraints, solution: sol };
}

// ---- rounded and bevelled corners of rect and polygon macros (mirrors corners.py) ------------

export const RECT_CORNERS = ['tl', 'tr', 'br', 'bl'] as const;

/** `corners=8` or `corners={tl: 8}` as a size per corner. */
export function cornerSpec(value: unknown, names: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  if (typeof value === 'number') { if (value > 0) for (const n of names) out[n] = value; return out; }
  if (value && typeof value === 'object') for (const [k, v] of Object.entries(value as Record<string, unknown>)) if (names.includes(k) && typeof v === 'number' && v > 0) out[k] = v;
  return out;
}

export interface CornerCut { kind: 'arc' | 'chamfer'; size: number; t: number; t1: Pt; t2: Pt; center?: Pt; start?: Pt; end?: Pt }

/** The rounding or bevel at corner p between the sides towards prev and next: the cut points
 * (t1 towards prev, t2 towards next), and for a radius the centre and the counter-clockwise arc.
 * Null when it does not fit or the sides are colinear. */
export function cornerGeometry(prev: Pt, p: Pt, next: Pt, radius?: number, chamfer?: number): CornerCut | null {
  const unit = (v: Pt): Pt | null => { const n = Math.hypot(v[0], v[1]); return n < 1e-12 ? null : [v[0] / n, v[1] / n]; };
  const u1 = unit([prev[0] - p[0], prev[1] - p[1]]), u2 = unit([next[0] - p[0], next[1] - p[1]]);
  if (!u1 || !u2) return null;
  const theta = Math.acos(Math.max(-1, Math.min(1, u1[0] * u2[0] + u1[1] * u2[1])));
  if (theta < 1e-6 || theta > Math.PI - 1e-6) return null;
  const l1 = Math.hypot(prev[0] - p[0], prev[1] - p[1]), l2 = Math.hypot(next[0] - p[0], next[1] - p[1]);
  const t = radius !== undefined ? radius / Math.tan(theta / 2) : chamfer ?? 0;
  if (t <= 0 || t > l1 + 1e-9 || t > l2 + 1e-9) return null;
  const t1: Pt = [p[0] + t * u1[0], p[1] + t * u1[1]], t2: Pt = [p[0] + t * u2[0], p[1] + t * u2[1]];
  if (radius === undefined) return { kind: 'chamfer', size: chamfer ?? 0, t, t1, t2 };
  const bis = unit([u1[0] + u2[0], u1[1] + u2[1]])!;
  const dist = radius / Math.sin(theta / 2);
  const center: Pt = [p[0] + dist * bis[0], p[1] + dist * bis[1]];
  const cross = (t1[0] - center[0]) * (t2[1] - center[1]) - (t1[1] - center[1]) * (t2[0] - center[0]);
  return { kind: 'arc', size: radius, t, t1, t2, center, start: cross > 0 ? t1 : t2, end: cross > 0 ? t2 : t1 };
}

/** The corners of a rect (counter-clockwise) or polygon (as given), each with its name. */
export function macroCornerPoints(kind: string, a: Record<string, unknown>): [string, Pt][] {
  if (kind === 'rect') {
    const c = pt(a.at), w = num(a.width) / 2, h = num(a.height) / 2;
    return [['bl', [c[0] - w, c[1] - h]], ['br', [c[0] + w, c[1] - h]], ['tr', [c[0] + w, c[1] + h]], ['tl', [c[0] - w, c[1] + h]]];
  }
  if (kind === 'polygon') return (Array.isArray(a.points) ? a.points : []).map((p, i) => [`p${i}`, pt(p)] as [string, Pt]);
  return [];
}

/** Every cut corner of a macro with its geometry, or null when one does not fit. */
export function macroCorners(kind: string, a: Record<string, unknown>): Record<string, CornerCut> | null {
  const pts = macroCornerPoints(kind, a);
  const names = pts.map(([n]) => n);
  const radii = cornerSpec(a.corners, names), bevels = cornerSpec(a.chamfers, names);
  const out: Record<string, CornerCut> = {};
  const count = pts.length;
  for (let i = 0; i < count; i++) {
    const [name, p] = pts[i];
    const r = radii[name], d = bevels[name];
    if (r === undefined && d === undefined) continue;
    const g = cornerGeometry(pts[(i + count - 1) % count][1], p, pts[(i + 1) % count][1], r, r === undefined ? d : undefined);
    if (!g) return null;
    out[name] = g;
  }
  for (let i = 0; i < count; i++) {
    const a1 = out[pts[i][0]], b1 = out[pts[(i + 1) % count][0]];
    if (a1 && b1) { const q = pts[(i + 1) % count][1], p = pts[i][1]; if (a1.t + b1.t > Math.hypot(q[0] - p[0], q[1] - p[1]) + 1e-9) return null; }
  }
  return out;
}

/** The outline of a macro with cut corners into the model: trimmed sides, arcs, chamfer lines
 * and their handles. False when it has no cuts (the caller draws the plain macro), true when
 * drawn; a misfit draws the sharp macro and flags the entity. */
function macroOutline(handles: Handle[], curves: Curve[], conflicting: Set<string>, n: string, kind: string, a: Record<string, unknown>, sides: string[], cs: Set<string>): boolean {
  if (!a.corners && !a.chamfers) return false;
  const cuts = macroCorners(kind, a);
  if (!cuts) { conflicting.add(n); return false; }
  const pts = macroCornerPoints(kind, a);
  const count = pts.length;
  for (let i = 0; i < count; i++) {
    const [name, p] = pts[i], [nextName, q] = pts[(i + 1) % count];
    const start = cuts[name]?.t2 ?? p, end = cuts[nextName]?.t1 ?? q;
    if (Math.hypot(end[0] - start[0], end[1] - start[1]) > 1e-9) curves.push({ ref: `${n}.${sides[i]}`, entity: n, kind: 'line', a: start, b: end, construction: cs.has(sides[i]) });
    const cut = cuts[nextName];
    if (!cut) continue;
    const construction = cs.has(sides[i]) || cs.has(sides[(i + 1) % count]);  // a cut beside a construction side is construction too
    if (cut.kind === 'arc') {
      const ref = `${n}.${nextName}_arc`, c = cut.center!, s = cut.start!, e = cut.end!;
      handles.push({ ref: `${ref}.center`, entity: n, p: c, kind: 'center' }, { ref: `${ref}.start`, entity: n, p: s, kind: 'end' }, { ref: `${ref}.end`, entity: n, p: e, kind: 'end' });
      curves.push({ ref, entity: n, kind: 'arc', c, r: cut.size, a0: Math.atan2(s[1] - c[1], s[0] - c[0]), a1: Math.atan2(e[1] - c[1], e[0] - c[0]), construction });
    } else {
      const ref = `${n}.${nextName}_chamfer`;
      handles.push({ ref: `${ref}.start`, entity: n, p: cut.t1, kind: 'end' }, { ref: `${ref}.end`, entity: n, p: cut.t2, kind: 'end' });
      curves.push({ ref, entity: n, kind: 'line', a: cut.t1, b: cut.t2, construction });
    }
  }
  return true;
}

// ---- trim: the piece of a curve under the cursor, up to the nearest crossings (mirrors trimming.py) ----

const TAU = 2 * Math.PI;
const sweepOf = (a0: number, a1: number) => { const s = ((a1 - a0) % TAU + TAU) % TAU; return s > 1e-9 ? s : TAU; };
function onArc(c: Curve, p: Pt): boolean {
  if (c.kind === 'circle') return true;
  if (c.kind === 'line') return false;
  const u = ((Math.atan2(p[1] - c.c[1], p[0] - c.c[0]) - c.a0) % TAU + TAU) % TAU;
  return u <= sweepOf(c.a0, c.a1) + 1e-6 || u >= TAU - 1e-6;
}
function onSegment(c: Curve, p: Pt): boolean {
  if (c.kind !== 'line') return false;
  const dx = c.b[0] - c.a[0], dy = c.b[1] - c.a[1], l2 = dx * dx + dy * dy;
  if (l2 < 1e-18) return false;
  const t = ((p[0] - c.a[0]) * dx + (p[1] - c.a[1]) * dy) / l2;
  return t >= -1e-6 && t <= 1 + 1e-6;
}
function lineLine(p: Curve, q: Curve): Pt[] {
  if (p.kind !== 'line' || q.kind !== 'line') return [];
  const den = (p.b[0] - p.a[0]) * (q.b[1] - q.a[1]) - (p.b[1] - p.a[1]) * (q.b[0] - q.a[0]);
  if (Math.abs(den) < 1e-12) return [];
  const t = ((q.a[0] - p.a[0]) * (q.b[1] - q.a[1]) - (q.a[1] - p.a[1]) * (q.b[0] - q.a[0])) / den;
  return [[p.a[0] + t * (p.b[0] - p.a[0]), p.a[1] + t * (p.b[1] - p.a[1])]];
}
function lineCircle(l: Curve, c: Curve): Pt[] {
  if (l.kind !== 'line' || c.kind === 'line') return [];
  const dx = l.b[0] - l.a[0], dy = l.b[1] - l.a[1], fx = l.a[0] - c.c[0], fy = l.a[1] - c.c[1];
  const A = dx * dx + dy * dy, B = 2 * (fx * dx + fy * dy), C = fx * fx + fy * fy - c.r * c.r;
  if (A < 1e-18) return [];
  let disc = B * B - 4 * A * C;
  if (disc < -1e-9) return [];
  disc = Math.max(disc, 0);
  const ts = [...new Set([(-B - Math.sqrt(disc)) / (2 * A), (-B + Math.sqrt(disc)) / (2 * A)])];
  return ts.map((t) => [l.a[0] + t * dx, l.a[1] + t * dy] as Pt);
}
function circleCircle(p: Curve, q: Curve): Pt[] {
  if (p.kind === 'line' || q.kind === 'line') return [];
  const d = Math.hypot(q.c[0] - p.c[0], q.c[1] - p.c[1]);
  if (d < 1e-12 || d > p.r + q.r + 1e-9 || d < Math.abs(p.r - q.r) - 1e-9) return [];
  const a = (p.r * p.r - q.r * q.r + d * d) / (2 * d), h = Math.sqrt(Math.max(p.r * p.r - a * a, 0));
  const mx = p.c[0] + a * (q.c[0] - p.c[0]) / d, my = p.c[1] + a * (q.c[1] - p.c[1]) / d;
  const rx = -(q.c[1] - p.c[1]) / d * h, ry = (q.c[0] - p.c[0]) / d * h;
  return h < 1e-12 ? [[mx, my]] : [[mx + rx, my + ry], [mx - rx, my - ry]];
}
function crossings(target: Curve, other: Curve): Pt[] {
  const pts = target.kind === 'line' ? (other.kind === 'line' ? lineLine(target, other) : lineCircle(target, other)) : other.kind === 'line' ? lineCircle(other, target) : circleCircle(target, other);
  const inside = (c: Curve, p: Pt) => (c.kind === 'line' ? onSegment(c, p) : onArc(c, p));
  return pts.filter((p) => inside(target, p) && inside(other, p));
}
function paramOf(c: Curve, p: Pt): number {
  if (c.kind === 'line') { const dx = c.b[0] - c.a[0], dy = c.b[1] - c.a[1], l2 = dx * dx + dy * dy || 1; return ((p[0] - c.a[0]) * dx + (p[1] - c.a[1]) * dy) / l2; }
  const ang = Math.atan2(p[1] - c.c[1], p[0] - c.c[0]);
  return c.kind === 'arc' ? ((ang - c.a0) % TAU + TAU) % TAU : (ang % TAU + TAU) % TAU;
}
function pointAt(c: Curve, u: number): Pt {
  if (c.kind === 'line') return [c.a[0] + u * (c.b[0] - c.a[0]), c.a[1] + u * (c.b[1] - c.a[1])];
  const ang = c.kind === 'arc' ? c.a0 + u : u;
  return [c.c[0] + c.r * Math.cos(ang), c.c[1] + c.r * Math.sin(ang)];
}

/** Whether a curve can be trimmed: a line, arc or circle of the sketch's own, not a macro's side. */
export function trimmable(m: SketchModel, ref: string): boolean {
  const info = m.entities.get(entityOf(ref));
  return !!info && !info.builtin && !info.projected && (info.kind === 'line' || info.kind === 'arc' || info.kind === 'circle') && ref === info.name;
}

/** The curve under a point, curves only and the sketch's own before the built-ins. */
export function hitCurve(m: SketchModel, p: Pt, tol: number): Curve | null {
  for (const builtin of [false, true]) {
    let best: Curve | null = null, bd = tol;
    for (const c of m.curves) {
      if (!c.ref || isBuiltin(c.ref) !== builtin) continue;
      const d = distToCurve(c, p);
      if (d <= bd) { best = c; bd = d; }
    }
    if (best) return best;
  }
  return null;
}

/** The piece of a curve that a trim at `at` would remove, as points to draw. */
export function trimPiece(m: SketchModel, ref: string, at: Pt): Pt[] | null {
  const target = curveOf(m, ref);
  if (!target) return null;
  const hits: number[] = [];
  for (const other of m.curves) {
    if (!other.ref || other.entity === target.entity) continue;
    for (const p of crossings(target, other)) {
      const u = paramOf(target, p);
      if (target.kind === 'line' && !(u > 1e-6 && u < 1 - 1e-6)) continue;
      if (target.kind === 'arc' && !(u > 1e-6 && u < sweepOf(target.a0, target.a1) - 1e-6)) continue;
      hits.push(u);
    }
  }
  hits.sort((a, b) => a - b);
  const uc = target.kind === 'line' ? Math.max(0, Math.min(1, paramOf(target, at))) : paramOf(target, at);
  const below = hits.filter((u) => u < uc), above = hits.filter((u) => u > uc);
  let u0: number, u1: number;
  if (target.kind === 'circle') {
    if (hits.length < 2) return curvePointsOf(target);
    u0 = below.length ? below[below.length - 1] : hits[hits.length - 1];
    u1 = above.length ? above[0] : hits[0];
    if (u1 <= u0) u1 += TAU;
  } else {
    u0 = below.length ? below[below.length - 1] : 0;
    u1 = above.length ? above[0] : target.kind === 'line' ? 1 : sweepOf(target.a0, target.a1);
  }
  if (target.kind === 'line') return [pointAt(target, u0), pointAt(target, u1)];
  const steps = Math.max(4, Math.ceil(((u1 - u0) / TAU) * 48));
  const out: Pt[] = [];
  for (let i = 0; i <= steps; i++) out.push(pointAt(target, u0 + ((u1 - u0) * i) / steps));
  return out;
}

// ---- corners to fillet, fillets to remove ----------------------------------------------

export type Corner = { a: string; b: string } | { entity: string; corner: string };

/** The corner a reference stands for: a macro's corner handle, or a line end that another
 * line's end meets. Null for anything else. */
export function cornerOf(m: SketchModel, ref: string): Corner | null {
  const h = m.handles.find((x) => x.ref === ref);
  if (!h) return null;
  const info = m.entities.get(h.entity);
  if (!info) return null;
  const part = ref.slice(h.entity.length + 1);
  if ((info.kind === 'rect' && (RECT_CORNERS as readonly string[]).includes(part)) || (info.kind === 'polygon' && /^p\d+$/.test(part))) return { entity: h.entity, corner: part };
  if (info.kind !== 'line' || (part !== 'start' && part !== 'end')) return null;
  const others = m.handles.filter((o) => o.entity !== h.entity && (o.kind === 'end') && m.entities.get(o.entity)?.kind === 'line' && Math.hypot(o.p[0] - h.p[0], o.p[1] - h.p[1]) < 1e-6);
  return others.length === 1 ? { a: ref, b: others[0].ref } : null;
}

/** The corners a selection stands for: corner handles, or exactly two lines meeting at one. Null when it is not all corners. */
export function cornersOf(m: SketchModel, sel: string[]): Corner[] | null {
  if (!sel.length) return null;
  const kinds = sel.map((r) => refKind(m, r));
  if (sel.length === 2 && kinds.every((k) => k === 'line')) {
    const la = curveOf(m, sel[0]), lb = curveOf(m, sel[1]);
    if (!la || !lb || la.kind !== 'line' || lb.kind !== 'line' || m.entities.get(la.entity)?.kind !== 'line' || m.entities.get(lb.entity)?.kind !== 'line') return null;
    for (const [ea, pa] of [['start', la.a], ['end', la.b]] as const) for (const [eb, pb] of [['start', lb.a], ['end', lb.b]] as const) {
      if (Math.hypot(pa[0] - pb[0], pa[1] - pb[1]) < 1e-6) return [{ a: `${sel[0]}.${ea}`, b: `${sel[1]}.${eb}` }];
    }
    return null;
  }
  const out: Corner[] = [];
  for (const r of sel) { const c = cornerOf(m, r); if (!c) return null; out.push(c); }
  return out;
}

/** A fillet or chamfer that can be removed: a macro's corner arc or chamfer by its reference, a
 * line-pair arc or bevel by the name the fillet gave it. */
export function removableCut(m: SketchModel, ref: string): { entity: string; what: 'fillet' | 'chamfer' } | null {
  const c = curveOf(m, ref);
  if (!c) return null;
  const macro = /^([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z0-9_]+_(arc|chamfer)$/.exec(ref);
  if (macro) return { entity: ref, what: macro[2] === 'arc' ? 'fillet' : 'chamfer' };
  if (/^fillet\d+$/.test(c.entity) && c.kind === 'arc') return { entity: c.entity, what: 'fillet' };
  if (/^chamfer\d+$/.test(c.entity) && c.kind === 'line') return { entity: c.entity, what: 'chamfer' };
  return null;
}

function addProjected(handles: Handle[], curves: Curve[], entity: string, ref: string, it: ProjectedItem) {
  if (it.kind === 'line' && it.start && it.end) {
    handles.push({ ref: `${ref}.start`, entity, p: it.start, kind: 'end' }, { ref: `${ref}.end`, entity, p: it.end, kind: 'end' },
                 { ref: `${ref}.mid`, entity, p: [(it.start[0] + it.end[0]) / 2, (it.start[1] + it.end[1]) / 2], kind: 'mid' });
    curves.push({ ref, entity, kind: 'line', a: it.start, b: it.end });
  } else if (it.kind === 'circle' && it.center) {
    handles.push({ ref: `${ref}.center`, entity, p: it.center, kind: 'center' });
    curves.push({ ref, entity, kind: 'circle', c: it.center, r: it.radius ?? 0 });
  } else if (it.kind === 'arc' && it.center && it.start && it.end) {
    handles.push({ ref: `${ref}.center`, entity, p: it.center, kind: 'center' }, { ref: `${ref}.start`, entity, p: it.start, kind: 'end' }, { ref: `${ref}.end`, entity, p: it.end, kind: 'end' });
    curves.push({ ref, entity, kind: 'arc', c: it.center, r: it.radius ?? Math.hypot(it.start[0] - it.center[0], it.start[1] - it.center[1]),
                  a0: Math.atan2(it.start[1] - it.center[1], it.start[0] - it.center[0]), a1: Math.atan2(it.end[1] - it.center[1], it.end[0] - it.center[0]) });
  } else if (it.kind === 'point' && it.at) {
    handles.push({ ref, entity, p: it.at, kind: 'point' });
  }
}

// ---- reference kinds and lookups ------------------------------------------------------

export function refKind(m: SketchModel, ref: string): RefKind | null {
  if (m.handles.some((h) => h.ref === ref)) return 'point';
  const c = m.curves.find((x) => x.ref === ref);
  if (!c) return null;
  return c.kind === 'line' ? 'line' : 'circle';
}

export function handleAt(m: SketchModel, ref: string): Pt | null { return m.handles.find((h) => h.ref === ref)?.p ?? null; }
export function curveOf(m: SketchModel, ref: string): Curve | null { return m.curves.find((c) => c.ref === ref) ?? null; }

/** The nearest point of a curve to p: for snapping a new point onto a line, circle or arc. */
export function nearestOnCurve(c: Curve, p: Pt): Pt {
  if (c.kind === 'line') {
    const dx = c.b[0] - c.a[0], dy = c.b[1] - c.a[1], l2 = dx * dx + dy * dy;
    const t = l2 ? Math.max(0, Math.min(1, ((p[0] - c.a[0]) * dx + (p[1] - c.a[1]) * dy) / l2)) : 0;
    return [c.a[0] + t * dx, c.a[1] + t * dy];
  }
  if (Math.hypot(p[0] - c.c[0], p[1] - c.c[1]) < 1e-9) return [c.c[0] + c.r, c.c[1]];
  let ang = Math.atan2(p[1] - c.c[1], p[0] - c.c[0]);
  if (c.kind === 'arc') {
    const tau = 2 * Math.PI;
    const span = (((c.a1 - c.a0) % tau) + tau) % tau || tau;
    const rel = (((ang - c.a0) % tau) + tau) % tau;
    if (rel > span) ang = rel - span < tau - rel ? c.a1 : c.a0;  // outside the arc: its nearer end
  }
  return [c.c[0] + c.r * Math.cos(ang), c.c[1] + c.r * Math.sin(ang)];
}

/** Points that show a reference: for labels, glyphs and highlights. */
export function refAnchor(m: SketchModel, ref: string): Pt | null {
  const h = handleAt(m, ref);
  if (h) return h;
  const c = curveOf(m, ref);
  if (!c) return null;
  if (c.kind === 'line') return [(c.a[0] + c.b[0]) / 2, (c.a[1] + c.b[1]) / 2];
  const a = c.kind === 'arc' ? midAngle(c.a0, c.a1) : Math.PI / 4;
  return [c.c[0] + c.r * Math.cos(a), c.c[1] + c.r * Math.sin(a)];
}

export function sweep(a0: number, a1: number): number { let s = (a1 - a0) % (2 * Math.PI); if (s < 1e-9) s += 2 * Math.PI; return s; }
export function midAngle(a0: number, a1: number): number { return a0 + sweep(a0, a1) / 2; }

// ---- hit testing ------------------------------------------------------------------

export function distToCurve(c: Curve, p: Pt): number {
  if (c.kind === 'line') return distToSegment(p, c.a, c.b);
  const d = Math.hypot(p[0] - c.c[0], p[1] - c.c[1]);
  if (c.kind === 'circle') return Math.abs(d - c.r);
  const ang = Math.atan2(p[1] - c.c[1], p[0] - c.c[0]);
  const within = ((ang - c.a0) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) <= sweep(c.a0, c.a1);
  if (within) return Math.abs(d - c.r);
  const s: Pt = [c.c[0] + c.r * Math.cos(c.a0), c.c[1] + c.r * Math.sin(c.a0)], e: Pt = [c.c[0] + c.r * Math.cos(c.a1), c.c[1] + c.r * Math.sin(c.a1)];
  return Math.min(Math.hypot(p[0] - s[0], p[1] - s[1]), Math.hypot(p[0] - e[0], p[1] - e[1]));
}

export function distToSegment(p: Pt, a: Pt, b: Pt): number {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy;
  const t = l2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2)) : 0;
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

/** The handle or curve under a point: handles before curves, and within each the sketch's own
 * geometry before the built-ins, so the origin is picked over an axis and over a line through it,
 * but never over a handle sitting on it. Tolerance in sketch units. */
export function hitTest(m: SketchModel, p: Pt, tol: number, mids = false): { ref: string; kind: RefKind; handle?: Handle } | null {
  for (const builtin of [false, true]) {
    let best: Handle | null = null, bd = tol;
    for (const h of m.handles) {
      if (h.kind === 'mid' && !mids) continue;  // midpoints only on request (alt): the line itself is the common pick
      if (isBuiltin(h.ref) !== builtin) continue;
      const d = Math.hypot(h.p[0] - p[0], h.p[1] - p[1]);
      if (d <= bd) { best = h; bd = d; }
    }
    if (best) return { ref: best.ref, kind: 'point', handle: best };
  }
  for (const builtin of [false, true]) {
    let bc: Curve | null = null, bd = tol;
    for (const c of m.curves) {
      if (!c.ref || isBuiltin(c.ref) !== builtin) continue;
      const d = distToCurve(c, p);
      if (d <= bd) { bc = c; bd = d; }
    }
    if (bc) return { ref: bc.ref, kind: bc.kind === 'line' ? 'line' : 'circle' };
  }
  return null;
}

/** The point to drag when the user grabs a reference. Null when it cannot be dragged. */
export function dragTarget(m: SketchModel, ref: string): string | null {
  const info = m.entities.get(entityOf(ref));
  if (!info || info.projected) return null;
  if (m.handles.some((h) => h.ref === ref)) return ref;
  const c = curveOf(m, ref);
  if (!c) return null;
  if (info.kind === 'line') return `${info.name}.mid`;
  if (info.kind === 'circle') return `${info.name}.rim`;  // the curve: the radius follows, the centre stays
  if (info.kind === 'arc' || info.kind === 'rect' || info.kind === 'slot') return `${info.name}.center`;
  return null;
}

// ---- constraints valid for a selection ------------------------------------------------

export interface ConstraintChoice { kind: string; label: string; refs: string[]; options?: Record<string, JsonValue> }

const LABELS: Record<string, string> = {
  coincident: 'coincident', horizontal: 'horizontal', vertical: 'vertical', parallel: 'parallel', perpendicular: 'perpendicular',
  equal: 'equal', tangent: 'tangent', concentric: 'concentric', coradial: 'coradial', colinear: 'colinear', symmetric: 'symmetric',
  midpoint: 'midpoint', on: 'on', fix: 'fix',
};

export function validConstraints(m: SketchModel, sel: string[]): ConstraintChoice[] {
  const kinds = sel.map((r) => refKind(m, r));
  if (kinds.some((k) => k === null)) return [];
  const key = [...kinds].sort().join('+');
  const by = (k: RefKind) => sel.filter((_, i) => kinds[i] === k);
  const mk = (kind: string, refs: string[], options?: Record<string, JsonValue>): ConstraintChoice => ({ kind, label: LABELS[kind] ?? kind, refs, options });
  const out: ConstraintChoice[] = [];
  const builtin = sel.some(isBuiltin);
  if (sel.length === 1) {
    if (builtin) return [];  // fixed already, nothing to hold
    if (kinds[0] === 'line') out.push(mk('horizontal', sel), mk('vertical', sel));
    out.push(mk('fix', sel));
    return out;
  }
  if (sel.length === 2) {
    if (key === 'point+point') return [mk('coincident', sel), mk('horizontal', sel), mk('vertical', sel)];
    if (key === 'line+point') { const p = by('point'), l = by('line'); return isBuiltin(l[0]) ? [mk('coincident', [p[0], l[0]])] : [mk('coincident', [p[0], l[0]]), mk('midpoint', [p[0], l[0]])]; }
    if (key === 'circle+point') { const p = by('point'), c = by('circle'); return [mk('on', [p[0], c[0]])]; }
    if (key === 'line+line') return builtin ? [mk('parallel', sel), mk('perpendicular', sel), mk('colinear', sel)] : [mk('parallel', sel), mk('perpendicular', sel), mk('equal', sel), mk('colinear', sel)];
    if (key === 'circle+circle') return [mk('concentric', sel), mk('coradial', sel), mk('equal', sel), mk('tangent', sel)];
    if (key === 'circle+line') { const l = by('line'), c = by('circle'); return [mk('tangent', [l[0], c[0]])]; }
  }
  if (sel.length === 3 && key === 'line+point+point') { const p = by('point'), l = by('line'); return [mk('symmetric', [p[0], p[1], l[0]])]; }
  return [];
}

export interface DimensionPlan { kind: string; refs: string[]; value: number; options?: Record<string, JsonValue> }
export type DimLock = 'x' | 'y' | 'aligned' | null;

/** How a distance between two points is measured, from where the cursor is: above or below the
 * pair gives the horizontal component, left or right of it the vertical one, the diagonal zones
 * the true distance. Null when the pair is already horizontal or vertical. */
export function orientationFor(a: Pt, b: Pt, cursor: Pt): 'x' | 'y' | null {
  const minx = Math.min(a[0], b[0]), maxx = Math.max(a[0], b[0]), miny = Math.min(a[1], b[1]), maxy = Math.max(a[1], b[1]);
  if (maxx - minx < 1e-6 || maxy - miny < 1e-6) return null;
  const inX = cursor[0] >= minx && cursor[0] <= maxx, inY = cursor[1] >= miny && cursor[1] <= maxy;
  if (inX && !inY) return 'x';
  if (inY && !inX) return 'y';
  return null;
}

/** The centre handle of a circle or arc curve: the handle of the same entity sitting at its centre. */
export function centerRef(m: SketchModel, ref: string): string | null {
  const c = curveOf(m, ref);
  if (!c || c.kind === 'line') return null;
  return m.handles.find((h) => h.entity === c.entity && Math.hypot(h.p[0] - c.c[0], h.p[1] - c.c[1]) < 1e-6)?.ref ?? null;
}

/** The dimension a selection takes. With a cursor, the placement decides what a pair of points
 * measures and which sector of two lines the angle is in; a lock from the menu overrides the cursor. */
export function dimensionFor(m: SketchModel, sel: string[], cursor?: Pt, lock: DimLock = null): DimensionPlan | null {
  const kinds = sel.map((r) => refKind(m, r));
  if (kinds.some((k) => k === null) || sel.length === 0 || sel.length > 2) return null;
  if (sel.length === 1) {
    if (isBuiltin(sel[0])) return null;
    const c = curveOf(m, sel[0]);
    const info = c ? m.entities.get(c.entity) : undefined;
    // a side of a rect or a slot: the macro's own size (its corner arcs and chamfers are ordinary curves)
    if (c && info && (info.kind === 'slot' || info.kind === 'rect') && /^(top|bottom|left|right|axis|start_arc|end_arc)$/.test(sel[0].slice(c.entity.length + 1))) {
      const n = c.entity, part = sel[0].slice(n.length + 1);
      const len = (ref: string) => { const l = curveOf(m, ref); return l && l.kind === 'line' ? Math.hypot(l.b[0] - l.a[0], l.b[1] - l.a[1]) : 0; };
      if (info.kind === 'rect') return part === 'left' || part === 'right' ? { kind: 'length', refs: [`${n}.height`], value: len(`${n}.left`) } : { kind: 'length', refs: [`${n}.width`], value: len(`${n}.top`) };
      const arc = curveOf(m, `${n}.start_arc`);
      const w = arc && arc.kind !== 'line' ? 2 * arc.r : 0;
      return part === 'axis' ? { kind: 'length', refs: [`${n}.length`], value: len(`${n}.axis`) + w } : { kind: 'length', refs: [`${n}.width`], value: w };
    }
    if (kinds[0] === 'line' && c && c.kind === 'line') return { kind: 'length', refs: sel, value: Math.hypot(c.b[0] - c.a[0], c.b[1] - c.a[1]) };
    if (kinds[0] === 'circle' && c && c.kind !== 'line') {
      const arc = c.kind === 'arc' || info?.kind === 'arc';
      return arc ? { kind: 'radius', refs: sel, value: c.r } : { kind: 'diameter', refs: sel, value: 2 * c.r };
    }
    return null;
  }
  const key = [...kinds].sort().join('+');
  const pointPair = (pa: string, pb: string, refs: string[]): DimensionPlan => {
    const a = handleAt(m, pa)!, b = handleAt(m, pb)!;
    const along = lock === 'aligned' ? null : lock ?? (cursor ? orientationFor(a, b, cursor) : null);
    const value = along === 'x' ? Math.abs(b[0] - a[0]) : along === 'y' ? Math.abs(b[1] - a[1]) : Math.hypot(b[0] - a[0], b[1] - a[1]);
    return along ? { kind: 'distance', refs, value, options: { along } } : { kind: 'distance', refs, value };
  };
  if (key === 'point+point') return pointPair(sel[0], sel[1], sel);
  if (key === 'line+point') {
    const p = sel[kinds[0] === 'point' ? 0 : 1], l = sel[kinds[0] === 'line' ? 0 : 1];
    const c = curveOf(m, l);
    if (!c || c.kind !== 'line') return null;
    return { kind: 'distance', refs: [p, l], value: distToLine(handleAt(m, p)!, c.a, c.b) };
  }
  if (key === 'line+line') {
    const a = curveOf(m, sel[0]), b = curveOf(m, sel[1]);
    if (!a || !b || a.kind !== 'line' || b.kind !== 'line') return null;
    const ang = angleBetween(a, b);
    if (Math.abs(ang) < 0.5 || Math.abs(Math.abs(ang) - 180) < 0.5) return { kind: 'distance', refs: sel, value: distToLine(b.a, a.a, a.b) };
    // the sector the cursor sits in: the same side of both lines' directions, or against the second one's
    let reverse = false;
    if (cursor) {
      const x = intersect(a.a, a.b, b.a, b.b) ?? mid(mid(a.a, a.b), mid(b.a, b.b));
      const s1 = (cursor[0] - x[0]) * (a.b[0] - a.a[0]) + (cursor[1] - x[1]) * (a.b[1] - a.a[1]) >= 0;
      const s2 = (cursor[0] - x[0]) * (b.b[0] - b.a[0]) + (cursor[1] - x[1]) * (b.b[1] - b.a[1]) >= 0;
      reverse = s1 !== s2;
    }
    const value = reverse ? 180 - Math.abs(ang) : Math.abs(ang);
    return reverse ? { kind: 'angle', refs: sel, value, options: { reverse: true } } : { kind: 'angle', refs: sel, value };
  }
  // circles measure from their centres
  if (key === 'circle+circle') {
    const ca = centerRef(m, sel[0]), cb = centerRef(m, sel[1]);
    return ca && cb ? pointPair(ca, cb, [ca, cb]) : null;
  }
  if (key === 'circle+point') {
    const p = sel[kinds[0] === 'point' ? 0 : 1], c = centerRef(m, sel[kinds[0] === 'circle' ? 0 : 1]);
    return c ? pointPair(p, c, [p, c]) : null;
  }
  if (key === 'circle+line') {
    const l = sel[kinds[0] === 'line' ? 0 : 1], c = centerRef(m, sel[kinds[0] === 'circle' ? 0 : 1]);
    const line = curveOf(m, l);
    if (!c || !line || line.kind !== 'line') return null;
    return { kind: 'distance', refs: [c, l], value: distToLine(handleAt(m, c)!, line.a, line.b) };
  }
  return null;
}

/** The text of a dimension's label. */
export function dimText(kind: string, value: number): string {
  return kind === 'diameter' ? `Ø${fmtNum(value)}` : kind === 'radius' ? `R${fmtNum(value)}` : kind === 'angle' ? `${fmtNum(value)}°` : fmtNum(value);
}

function distToLine(p: Pt, a: Pt, b: Pt): number {
  const dx = b[0] - a[0], dy = b[1] - a[1], n = Math.hypot(dx, dy);
  return n ? Math.abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / n : 0;
}

function angleBetween(a: { a: Pt; b: Pt }, b: { a: Pt; b: Pt }): number {
  const d1 = [a.b[0] - a.a[0], a.b[1] - a.a[1]], d2 = [b.b[0] - b.a[0], b.b[1] - b.a[1]];
  return (Math.atan2(d1[0] * d2[1] - d1[1] * d2[0], d1[0] * d2[0] + d1[1] * d2[1]) * 180) / Math.PI;
}

// ---- dimension drawing: extension lines, a dimension line through the label, arrowheads --------

/** Polylines and arrowheads of a dimension, and where its text sits, all in sketch units. */
export interface DimensionDrawing { lines: Pt[][]; arrows: { tip: Pt; dir: Pt }[]; text: Pt }

/** The measured span of a dimension: the two points it runs between, or the circle, or the lines of an angle. */
function span(m: SketchModel, kind: string, refs: string[]): { a: Pt; b: Pt } | null {
  if (kind === 'length') {
    const size = /^([A-Za-z_][A-Za-z0-9_]*)\.(width|height|length)$/.exec(refs[0]);
    if (size) return macroSizeSpan(m, size[1], size[2]);
    const line = curveOf(m, refs[0]);
    return line && line.kind === 'line' ? { a: line.a, b: line.b } : null;
  }
  if (kind !== 'distance') return null;
  const ka = refKind(m, refs[0]), kb = refKind(m, refs[1]);
  if (ka === 'point' && kb === 'point') return { a: handleAt(m, refs[0])!, b: handleAt(m, refs[1])! };
  if (ka === 'line' && kb === 'line') {
    const line = curveOf(m, refs[0]), other = curveOf(m, refs[1]);
    if (!line || !other || line.kind !== 'line' || other.kind !== 'line') return null;
    const a = mid(line.a, line.b);
    return { a, b: footOnLine(a, other.a, other.b) };
  }
  const p = refs[ka === 'point' ? 0 : 1], l = refs[ka === 'line' ? 0 : 1];
  const line = curveOf(m, l), pp = handleAt(m, p);
  if (!line || line.kind !== 'line' || !pp) return null;
  return { a: pp, b: footOnLine(pp, line.a, line.b) };
}

/** What a macro's size measures: a rect's width along its top (sharp corner to sharp corner), its
 * height along its left side, a slot's length tip to tip and its width across the start arc. */
function macroSizeSpan(m: SketchModel, n: string, part: string): { a: Pt; b: Pt } | null {
  const info = m.entities.get(n);
  if (info?.kind === 'rect') {
    const tl = handleAt(m, `${n}.tl`), tr = handleAt(m, `${n}.tr`), bl = handleAt(m, `${n}.bl`);
    if (!tl || !tr || !bl) return null;
    return part === 'width' ? { a: tl, b: tr } : part === 'height' ? { a: bl, b: tl } : null;
  }
  if (info?.kind === 'slot') {
    const s = handleAt(m, `${n}.start`), e = handleAt(m, `${n}.end`), arc = curveOf(m, `${n}.start_arc`);
    if (!s || !e || !arc || arc.kind === 'line') return null;
    const L = Math.hypot(e[0] - s[0], e[1] - s[1]) || 1, d: Pt = [(e[0] - s[0]) / L, (e[1] - s[1]) / L], r = arc.r;
    if (part === 'length') return { a: [s[0] - d[0] * r, s[1] - d[1] * r], b: [e[0] + d[0] * r, e[1] + d[1] * r] };
    if (part === 'width') return { a: [s[0] - d[1] * r, s[1] + d[0] * r], b: [s[0] + d[1] * r, s[1] - d[0] * r] };
  }
  return null;
}

/** A linear dimension between P and Q: the dimension line runs through the label, parallel to PQ,
 * extension lines reach it from P and Q, and the text sits on it under the label. */
function linear(P: Pt, Q: Pt, label: Pt, px: number): DimensionDrawing | null {
  const dx = Q[0] - P[0], dy = Q[1] - P[1], L = Math.hypot(dx, dy);
  if (L < 1e-9) return null;
  const u: Pt = [dx / L, dy / L], n: Pt = [-u[1], u[0]];
  const d = (label[0] - P[0]) * n[0] + (label[1] - P[1]) * n[1];
  const t = (label[0] - P[0]) * u[0] + (label[1] - P[1]) * u[1];
  const s = d >= 0 ? 1 : -1, gap = 2.5 * px * s, over = 6 * px * s;
  const at = (p: Pt, along: number, off: number): Pt => [p[0] + u[0] * along + n[0] * off, p[1] + u[1] * along + n[1] * off];
  const A = at(P, 0, d), B = at(P, L, d);
  const lines: Pt[][] = [[at(P, 0, gap), at(P, 0, d + over)], [at(P, L, gap), at(P, L, d + over)], [at(P, Math.min(0, t), d), at(P, Math.max(L, t), d)]];
  return { lines, arrows: [{ tip: A, dir: [-u[0], -u[1]] }, { tip: B, dir: u }], text: at(P, t, d) };
}

export function dimensionDrawing(m: SketchModel, kind: string, refs: string[], options: Record<string, unknown> | undefined, label: Pt, px: number): DimensionDrawing | null {
  if (kind === 'length' || kind === 'distance') {
    const sp = span(m, kind, refs);
    if (!sp) return null;
    const along = options?.along;
    const b: Pt = along === 'x' ? [sp.b[0], sp.a[1]] : along === 'y' ? [sp.a[0], sp.b[1]] : sp.b;
    return linear(sp.a, b, label, px);
  }
  if (kind === 'diameter' || kind === 'radius') {
    const c = curveOf(m, refs[0]);
    if (!c || c.kind === 'line') return null;
    const dl = Math.hypot(label[0] - c.c[0], label[1] - c.c[1]);
    const w: Pt = dl > 1e-9 ? [(label[0] - c.c[0]) / dl, (label[1] - c.c[1]) / dl] : [Math.SQRT1_2, Math.SQRT1_2];
    const rim: Pt = [c.c[0] + c.r * w[0], c.c[1] + c.r * w[1]];
    const far: Pt = dl > c.r ? label : rim;
    if (kind === 'radius') return { lines: [[c.c, far]], arrows: [{ tip: rim, dir: w }], text: label };
    const opp: Pt = [c.c[0] - c.r * w[0], c.c[1] - c.r * w[1]];
    return { lines: [[opp, far]], arrows: [{ tip: rim, dir: w }, { tip: opp, dir: [-w[0], -w[1]] }], text: label };
  }
  if (kind === 'angle') {
    const a = curveOf(m, refs[0]), b = curveOf(m, refs[1]);
    if (!a || !b || a.kind !== 'line' || b.kind !== 'line') return null;
    const x = intersect(a.a, a.b, b.a, b.b) ?? mid(mid(a.a, a.b), mid(b.a, b.b));
    const unit = (l: { a: Pt; b: Pt }): Pt => { const dx = l.b[0] - l.a[0], dy = l.b[1] - l.a[1], n = Math.hypot(dx, dy) || 1; return [dx / n, dy / n]; };
    const d1 = unit(a), d2 = unit(b);
    // the sector: the label's side of the first line's direction, and the same or the opposite side of the second's
    const s1 = (label[0] - x[0]) * d1[0] + (label[1] - x[1]) * d1[1] >= 0 ? 1 : -1;
    const s2 = options?.reverse ? -s1 : s1;
    const r1: Pt = [s1 * d1[0], s1 * d1[1]], r2: Pt = [s2 * d2[0], s2 * d2[1]];
    const R = Math.max(Math.hypot(label[0] - x[0], label[1] - x[1]), 4 * px);
    let a0 = Math.atan2(r1[1], r1[0]), a1 = Math.atan2(r2[1], r2[0]);
    let sw = a1 - a0;
    while (sw > Math.PI) sw -= 2 * Math.PI;
    while (sw < -Math.PI) sw += 2 * Math.PI;
    if (sw < 0) { [a0, a1] = [a1, a0]; sw = -sw; }
    const steps = Math.max(6, Math.ceil(sw / 0.1));
    const arc: Pt[] = [];
    for (let i = 0; i <= steps; i++) { const t = a0 + (sw * i) / steps; arc.push([x[0] + R * Math.cos(t), x[1] + R * Math.sin(t)]); }
    const over = 6 * px;
    const ray = (r: Pt): Pt[] => [x, [x[0] + r[0] * (R + over), x[1] + r[1] * (R + over)]];
    const tang = (t: number, sign: number): Pt => [-Math.sin(t) * sign, Math.cos(t) * sign];
    return { lines: [arc, ray(r1), ray(r2)], arrows: [{ tip: arc[0], dir: tang(a0, -1) }, { tip: arc[arc.length - 1], dir: tang(a1, 1) }], text: label };
  }
  return null;
}

/** Where a dimension's label goes when the file does not say: beside its span. */
export function defaultLabel(m: SketchModel, c: Constraint): Pt | null {
  const k = c.kind;
  if (k === 'length' || k === 'distance') {
    const sp = span(m, k, c.refs);
    if (!sp) return null;
    const along = c.options?.along;
    const b: Pt = along === 'x' ? [sp.b[0], sp.a[1]] : along === 'y' ? [sp.a[0], sp.b[1]] : sp.b;
    return offsetFrom(sp.a, b, 6);
  }
  if (k === 'diameter' || k === 'radius') {
    const c0 = curveOf(m, c.refs[0]);
    if (!c0 || c0.kind === 'line') return null;
    const ang = c0.kind === 'arc' ? midAngle(c0.a0, c0.a1) : Math.PI / 4;
    return [c0.c[0] + (c0.r + 4) * Math.cos(ang), c0.c[1] + (c0.r + 4) * Math.sin(ang)];
  }
  if (k === 'angle') {
    const a = curveOf(m, c.refs[0]), b = curveOf(m, c.refs[1]);
    if (!a || !b || a.kind !== 'line' || b.kind !== 'line') return null;
    return mid(mid(a.a, a.b), mid(b.a, b.b));
  }
  return null;
}

/** The label position of a dimension: the file's at=, else the default. */
export function labelOf(m: SketchModel, c: Constraint): Pt | null {
  const at = c.options?.at;
  if (Array.isArray(at) && at.length === 2 && typeof at[0] === 'number' && typeof at[1] === 'number') return [at[0], at[1]];
  return defaultLabel(m, c);
}

export const CONSTRAINT_PREFIX: Record<string, string> = {
  coincident: 'c', horizontal: 'h', vertical: 'v', parallel: 'pa', perpendicular: 'pe', equal: 'eq', tangent: 't',
  concentric: 'cc', coradial: 'cr', colinear: 'cl', symmetric: 'sy', midpoint: 'mp', on: 'on', fix: 'fx',
  distance: 'd', length: 'len', diameter: 'dia', radius: 'rad', angle: 'ang',
};

export const GLYPH: Record<string, string> = {
  horizontal: 'H', vertical: 'V', parallel: '∥', perpendicular: '⟂', equal: '=', tangent: '⌒', concentric: '◎',
  coradial: '◉', colinear: '⋯', symmetric: '⇔', midpoint: '⊣', on: '⊙', fix: '⚓', coincident: '•',
};

/** Every name already used in the sketch: entities and constraints share one namespace. */
export function takenNames(f: Feature): string[] {
  return [...f.entities.map((e) => e.name), ...(f.constraints ?? []).map((c) => c.name)];
}

export const mid = (a: Pt, b: Pt): Pt => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
function offsetFrom(a: Pt, b: Pt, d: number): Pt {
  const dx = b[0] - a[0], dy = b[1] - a[1], n = Math.hypot(dx, dy) || 1;
  return [(a[0] + b[0]) / 2 - (dy / n) * d, (a[1] + b[1]) / 2 + (dx / n) * d];
}
function footOnLine(p: Pt, a: Pt, b: Pt): Pt {
  const dx = b[0] - a[0], dy = b[1] - a[1], l2 = dx * dx + dy * dy || 1;
  const t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2;
  return [a[0] + t * dx, a[1] + t * dy];
}
function intersect(a: Pt, b: Pt, c: Pt, d: Pt): Pt | null {
  const den = (a[0] - b[0]) * (c[1] - d[1]) - (a[1] - b[1]) * (c[0] - d[0]);
  if (Math.abs(den) < 1e-9) return null;
  const t = ((a[0] - c[0]) * (c[1] - d[1]) - (a[1] - c[1]) * (c[0] - d[0])) / den;
  return [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])];
}

export const fmtNum = (v: number) => (Math.round(v * 1000) / 1000).toString();

/** Sample points along a curve, in order from its start (lines: both ends). */
export function curvePointsOf(c: Curve, n = 48): Pt[] {
  if (c.kind === 'line') return [c.a, c.b];
  const out: Pt[] = [];
  const a0 = c.kind === 'arc' ? c.a0 : 0, sw = c.kind === 'arc' ? sweep(c.a0, c.a1) : 2 * Math.PI;
  const steps = Math.max(8, Math.ceil((sw / (2 * Math.PI)) * n));
  for (let i = 0; i <= steps; i++) { const t = a0 + (i / steps) * sw; out.push([c.c[0] + c.r * Math.cos(t), c.c[1] + c.r * Math.sin(t)]); }
  return out;
}
