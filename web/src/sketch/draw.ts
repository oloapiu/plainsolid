// three.js drawing of the 2D sketch model on its plane.
import * as THREE from 'three';
import type { PlaneFrame } from '../viewport/scene';
import type { Curve, DimensionDrawing, Pt, SketchModel } from './model';
import { sweep } from './model';

export const COLORS = {
  free: 0x6fb2ff,        // under-constrained: blue
  fixed: 0xf2f5f7,       // fully constrained
  conflict: 0xff5c4d,
  construction: 0x8d97a1,
  constructionFree: 0x5d8fc4,      // a free construction line: still blue, dimmer
  constructionConflict: 0xc0554a,
  projected: 0xc9a86b,
  selected: 0xff8c1a,
  hover: 0xffe08a,
  preview: 0xffcc66,
  handle: 0x9fd3ff,
};

export function toWorld(frame: PlaneFrame, p: Pt, lift = 0.05): THREE.Vector3 {
  return frame.origin.clone().addScaledVector(frame.x, p[0]).addScaledVector(frame.y, p[1]).addScaledVector(frame.n, lift);
}

export function curvePoints(c: Curve, n = 48): Pt[] {
  if (c.kind === 'line') return [c.a, c.b];
  const out: Pt[] = [];
  const a0 = c.kind === 'arc' ? c.a0 : 0, sw = c.kind === 'arc' ? sweep(c.a0, c.a1) : 2 * Math.PI;
  const steps = Math.max(8, Math.ceil((sw / (2 * Math.PI)) * n));
  for (let i = 0; i <= steps; i++) { const t = a0 + (i / steps) * sw; out.push([c.c[0] + c.r * Math.cos(t), c.c[1] + c.r * Math.sin(t)]); }
  return out;
}

export function polyline(frame: PlaneFrame, pts: Pt[], color: number, dashed = false, width = 1): THREE.Line {
  const geom = new THREE.BufferGeometry().setFromPoints(pts.map((p) => toWorld(frame, p)));
  const mat = dashed
    ? new THREE.LineDashedMaterial({ color, dashSize: 2, gapSize: 1.5, depthTest: false, linewidth: width })
    : new THREE.LineBasicMaterial({ color, depthTest: false, linewidth: width });
  const line = new THREE.Line(geom, mat);
  if (dashed) line.computeLineDistances();
  line.renderOrder = 10;
  return line;
}

export function points(frame: PlaneFrame, pts: Pt[], color: number, size = 6): THREE.Points {
  const geom = new THREE.BufferGeometry().setFromPoints(pts.map((p) => toWorld(frame, p, 0.08)));
  const mat = new THREE.PointsMaterial({ color, size, sizeAttenuation: false, depthTest: false });
  const obj = new THREE.Points(geom, mat);
  obj.renderOrder = 12;
  return obj;
}

export function planeGrid(frame: PlaneFrame, size: number): THREE.GridHelper {
  const s = Math.ceil((size * 2.4) / 10) * 10;
  const grid = new THREE.GridHelper(s, s / 10, 0x6f7a86, 0x4a525b);
  const m = new THREE.Matrix4().makeBasis(frame.x, frame.n, frame.y.clone().negate());
  grid.quaternion.setFromRotationMatrix(m);
  grid.position.copy(frame.origin).addScaledVector(frame.n, 0.02);
  (grid.material as THREE.Material).depthTest = false;
  grid.renderOrder = 5;
  return grid;
}

export function disposeGroup(g: THREE.Object3D) {
  g.traverse((o) => {
    const anyO = o as THREE.Mesh;
    if (anyO.geometry) anyO.geometry.dispose();
    const mat = (anyO as unknown as { material?: THREE.Material }).material;
    if (mat) mat.dispose();
  });
}

export interface DrawState { selection: Set<string>; hover: string | null; highlight: Set<string> }

/** The whole sketch as one group: curves coloured by status, handles as points. */
export function drawModel(frame: PlaneFrame, m: SketchModel, st: DrawState): THREE.Group {
  const g = new THREE.Group();
  const entOf = (ref: string) => ref.split('.')[0];
  const isOn = (ref: string, entity: string) => st.selection.has(ref) || st.selection.has(entity) || st.highlight.has(ref) || st.highlight.has(entity);
  for (const c of m.curves) {
    const info = m.entities.get(c.entity);
    let color = COLORS.fixed;
    let dashed = false;
    if (info?.projected) { color = COLORS.projected; dashed = info.construction; }
    else if (info?.construction) {
      dashed = true;
      color = m.conflicting.has(c.entity) ? COLORS.constructionConflict : m.free.has(c.entity) ? COLORS.constructionFree : COLORS.construction;
    }
    else if (m.conflicting.has(c.entity)) color = COLORS.conflict;
    else if (m.free.has(c.entity)) color = COLORS.free;
    if (c.ref && st.hover === c.ref) color = COLORS.hover;
    else if (c.ref && isOn(c.ref, c.entity)) color = COLORS.selected;
    else if (!c.ref && (st.selection.has(c.entity) || st.highlight.has(c.entity))) color = COLORS.selected;
    g.add(polyline(frame, curvePoints(c), color, dashed));
  }
  const plain: Pt[] = [], lit: Pt[] = [], hov: Pt[] = [];
  for (const h of m.handles) {
    if (h.kind === 'mid') continue;
    if (st.hover === h.ref) hov.push(h.p);
    else if (isOn(h.ref, entOf(h.ref))) lit.push(h.p);
    else plain.push(h.p);
  }
  if (plain.length) g.add(points(frame, plain, COLORS.handle, 5));
  if (lit.length) g.add(points(frame, lit, COLORS.selected, 8));
  if (hov.length) g.add(points(frame, hov, COLORS.hover, 9));
  return g;
}

export function snap(v: number, step = 1): number { return Math.round(v / step) * step; }
export const round3 = (v: number) => Math.round(v * 1000) / 1000;

/** A dimension: its lines and arrowheads, in one colour. Arrow sizes are in pixels at draw time. */
export function drawDimension(frame: PlaneFrame, d: DimensionDrawing, color: number, px: number): THREE.Group {
  const g = new THREE.Group();
  for (const l of d.lines) g.add(polyline(frame, l, color));
  const len = 7 * px, half = 2.2 * px;
  for (const a of d.arrows) {
    const n: Pt = [-a.dir[1], a.dir[0]];
    const base: Pt = [a.tip[0] - a.dir[0] * len, a.tip[1] - a.dir[1] * len];
    g.add(polyline(frame, [[base[0] + n[0] * half, base[1] + n[1] * half], a.tip, [base[0] - n[0] * half, base[1] - n[1] * half]], color));
  }
  return g;
}
