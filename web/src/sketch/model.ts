// The 2D picture of a sketch: handles (named points), curves (named lines, circles, arcs),
// their constraint status, hit testing, the constraint acceptance table and dimension
// measurement. Pure data, built from the tree feature and its solved coordinates.
import type { JsonValue, Constraint, Entity, Feature, ProjectedItem, SketchSolution } from '../api/types';

export type Pt = [number, number];
export type RefKind = 'point' | 'line' | 'circle';

export interface Handle { ref: string; entity: string; p: Pt; kind: 'end' | 'center' | 'mid' | 'corner' | 'point' }
export type Curve =
  | { ref: string; entity: string; kind: 'line'; a: Pt; b: Pt; decor?: boolean }
  | { ref: string; entity: string; kind: 'circle'; c: Pt; r: number }
  | { ref: string; entity: string; kind: 'arc'; c: Pt; r: number; a0: number; a1: number };

export interface EntityInfo { name: string; kind: string; construction: boolean; projected: boolean }

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
  const H = (ref: string, entity: string, p: Pt, kind: Handle['kind']) => handles.push({ ref, entity, p, kind });
  const L = (ref: string, entity: string, a: Pt, b: Pt, decor = false) => curves.push({ ref, entity, kind: 'line', a, b, decor });
  for (const e of f.entities) {
    const a = solvedArgs(e, sol, override);
    const n = e.name;
    entities.set(n, { name: n, kind: e.kind, construction: e.construction, projected: e.kind === 'project' || e.kind === 'offset' });
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
        L(`${n}.top`, n, tl, tr); L(`${n}.bottom`, n, bl, br); L(`${n}.left`, n, bl, tl); L(`${n}.right`, n, br, tr);
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
        pts.forEach((p, i) => L(`${n}.e${i}`, n, p, pts[(i + 1) % pts.length]));
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
  const conflicting = new Set<string>();
  for (const c of constraints) if (sol?.conflicting?.includes(c.name)) for (const r of c.refs) conflicting.add(entityOf(r));
  return { handles, curves, entities, free: new Set(sol?.free_entities ?? []), conflicting, constraints, solution: sol };
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

/** The handle or curve under a point, handles first. Tolerance in sketch units. */
export function hitTest(m: SketchModel, p: Pt, tol: number, mids = false): { ref: string; kind: RefKind; handle?: Handle } | null {
  let best: Handle | null = null, bd = tol;
  for (const h of m.handles) {
    if (h.kind === 'mid' && !mids) continue;  // midpoints only on request (alt): the line itself is the common pick
    const d = Math.hypot(h.p[0] - p[0], h.p[1] - p[1]);
    if (d <= bd) { best = h; bd = d; }
  }
  if (best) return { ref: best.ref, kind: 'point', handle: best };
  let bc: Curve | null = null; bd = tol;
  for (const c of m.curves) {
    if (!c.ref) continue;
    const d = distToCurve(c, p);
    if (d <= bd) { bc = c; bd = d; }
  }
  return bc ? { ref: bc.ref, kind: bc.kind === 'line' ? 'line' : 'circle' } : null;
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
  if (sel.length === 1) {
    if (kinds[0] === 'line') out.push(mk('horizontal', sel), mk('vertical', sel));
    out.push(mk('fix', sel));
    return out;
  }
  if (sel.length === 2) {
    if (key === 'point+point') return [mk('coincident', sel), mk('horizontal', sel), mk('vertical', sel)];
    if (key === 'line+point') { const p = by('point'), l = by('line'); return [mk('coincident', [p[0], l[0]]), mk('midpoint', [p[0], l[0]])]; }
    if (key === 'circle+point') { const p = by('point'), c = by('circle'); return [mk('on', [p[0], c[0]])]; }
    if (key === 'line+line') return [mk('parallel', sel), mk('perpendicular', sel), mk('equal', sel), mk('colinear', sel)];
    if (key === 'circle+circle') return [mk('concentric', sel), mk('coradial', sel), mk('equal', sel), mk('tangent', sel)];
    if (key === 'circle+line') { const l = by('line'), c = by('circle'); return [mk('tangent', [l[0], c[0]])]; }
  }
  if (sel.length === 3 && key === 'line+point+point') { const p = by('point'), l = by('line'); return [mk('symmetric', [p[0], p[1], l[0]])]; }
  return [];
}

export interface DimensionPlan { kind: string; refs: string[]; value: number; options?: Record<string, JsonValue>; alternatives?: DimensionPlan[] }

export function dimensionFor(m: SketchModel, sel: string[]): DimensionPlan | null {
  const kinds = sel.map((r) => refKind(m, r));
  if (kinds.some((k) => k === null) || sel.length === 0 || sel.length > 2) return null;
  if (sel.length === 1) {
    const c = curveOf(m, sel[0]);
    const info = c ? m.entities.get(c.entity) : undefined;
    let own: DimensionPlan | null = null;
    if (kinds[0] === 'line' && c && c.kind === 'line') own = { kind: 'length', refs: sel, value: Math.hypot(c.b[0] - c.a[0], c.b[1] - c.a[1]) };
    if (kinds[0] === 'circle' && c && c.kind !== 'line') {
      const arc = c.kind === 'arc' || info?.kind === 'arc';
      own = arc ? { kind: 'radius', refs: sel, value: c.r } : { kind: 'diameter', refs: sel, value: 2 * c.r };
    }
    // a curve of a slot or a rect: the macro's own sizes come first, the curve's dimension stays as an alternative
    if (c && info && (info.kind === 'slot' || info.kind === 'rect')) {
      const n = c.entity;
      const len = (ref: string) => { const l = curveOf(m, ref); return l && l.kind === 'line' ? Math.hypot(l.b[0] - l.a[0], l.b[1] - l.a[1]) : 0; };
      let sizes: DimensionPlan[];
      if (info.kind === 'slot') {
        const arc = curveOf(m, `${n}.start_arc`);
        const w = arc && arc.kind !== 'line' ? 2 * arc.r : 0;
        sizes = [{ kind: 'length', refs: [`${n}.length`], value: len(`${n}.axis`) + w }, { kind: 'length', refs: [`${n}.width`], value: w }];
      } else {
        sizes = [{ kind: 'length', refs: [`${n}.width`], value: len(`${n}.top`) }, { kind: 'length', refs: [`${n}.height`], value: len(`${n}.left`) }];
      }
      return { ...sizes[0], alternatives: [sizes[1], ...(own ? [own] : [])] };
    }
    return own;
  }
  const key = [...kinds].sort().join('+');
  if (key === 'point+point') {
    const a = handleAt(m, sel[0])!, b = handleAt(m, sel[1])!;
    const d = Math.hypot(b[0] - a[0], b[1] - a[1]);
    return { kind: 'distance', refs: sel, value: d, alternatives: [
      { kind: 'distance', refs: sel, value: Math.abs(b[0] - a[0]), options: { along: 'x' } },
      { kind: 'distance', refs: sel, value: Math.abs(b[1] - a[1]), options: { along: 'y' } },
    ] };
  }
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
    return { kind: 'angle', refs: sel, value: ang };
  }
  return null;
}

function distToLine(p: Pt, a: Pt, b: Pt): number {
  const dx = b[0] - a[0], dy = b[1] - a[1], n = Math.hypot(dx, dy);
  return n ? Math.abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / n : 0;
}

function angleBetween(a: { a: Pt; b: Pt }, b: { a: Pt; b: Pt }): number {
  const d1 = [a.b[0] - a.a[0], a.b[1] - a.a[1]], d2 = [b.b[0] - b.a[0], b.b[1] - b.a[1]];
  return (Math.atan2(d1[0] * d2[1] - d1[1] * d2[0], d1[0] * d2[0] + d1[1] * d2[1]) * 180) / Math.PI;
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

/** Where a dimension's measured span lies: two anchor points and a default label offset. */
export function dimensionGeometry(m: SketchModel, c: Constraint): { a: Pt; b: Pt; label: Pt } | null {
  const k = c.kind;
  if (k === 'length' || (k === 'distance' && c.refs.length === 2 && refKind(m, c.refs[0]) === 'line' && refKind(m, c.refs[1]) === 'line')) {
    const line = curveOf(m, c.refs[0]);
    if (!line || line.kind !== 'line') return null;
    if (k === 'distance') {
      const other = curveOf(m, c.refs[1]);
      if (!other || other.kind !== 'line') return null;
      const a = mid(line.a, line.b), b = footOnLine(a, other.a, other.b);
      return { a, b, label: mid(a, b) };
    }
    return { a: line.a, b: line.b, label: offsetFrom(line.a, line.b, 6) };
  }
  if (k === 'diameter' || k === 'radius') {
    const c0 = curveOf(m, c.refs[0]);
    if (!c0 || c0.kind === 'line') return null;
    const ang = c0.kind === 'arc' ? midAngle(c0.a0, c0.a1) : Math.PI / 4;
    const rim: Pt = [c0.c[0] + c0.r * Math.cos(ang), c0.c[1] + c0.r * Math.sin(ang)];
    const start: Pt = k === 'diameter' ? [c0.c[0] - c0.r * Math.cos(ang), c0.c[1] - c0.r * Math.sin(ang)] : c0.c;
    return { a: start, b: rim, label: [rim[0] + 4 * Math.cos(ang), rim[1] + 4 * Math.sin(ang)] };
  }
  if (k === 'distance') {
    const ka = refKind(m, c.refs[0]), kb = refKind(m, c.refs[1]);
    if (ka === 'point' && kb === 'point') {
      const a = handleAt(m, c.refs[0])!, b = handleAt(m, c.refs[1])!;
      const along = c.options?.along;
      const bb: Pt = along === 'x' ? [b[0], a[1]] : along === 'y' ? [a[0], b[1]] : b;
      return { a, b: bb, label: offsetFrom(a, bb, 5) };
    }
    const p = c.refs[ka === 'point' ? 0 : 1], l = c.refs[ka === 'line' ? 0 : 1];
    const line = curveOf(m, l), pp = handleAt(m, p);
    if (!line || line.kind !== 'line' || !pp) return null;
    const foot = footOnLine(pp, line.a, line.b);
    return { a: pp, b: foot, label: mid(pp, foot) };
  }
  if (k === 'angle') {
    const a = curveOf(m, c.refs[0]), b = curveOf(m, c.refs[1]);
    if (!a || !b || a.kind !== 'line' || b.kind !== 'line') return null;
    const x = intersect(a.a, a.b, b.a, b.b) ?? mid(mid(a.a, a.b), mid(b.a, b.b));
    return { a: x, b: mid(a.a, a.b), label: mid(mid(a.a, a.b), mid(b.a, b.b)) };
  }
  return null;
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
