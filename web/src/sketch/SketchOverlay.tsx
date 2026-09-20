import { freeText } from '../panel/PropertyPanel';
import { useEffect, useMemo, useRef, useState } from 'react';
import * as THREE from 'three';
import { sceneRef } from '../viewport/Viewport';
import { frameFromInfo, type PlaneFrame } from '../viewport/scene';
import {
  useStore, edit, hoverEntity, exitSketch, featureByName, nextName, setSketchTool, setStatus, setError, sketchBatch, sketchNames, toggleSketchSelect, setSketchHover, startDrag, previewDrag, endDrag, cancelDragPreview, addConstraint, beginDimension, expressionNames, expressionValue, toggleConstructionMode, toggleConstructionSelection, type SketchTool, useBodyInRelation, selectorTarget, toggleBodySelect, convertBodySelection,
  openContextMenu, openFeatureDialog, deleteSketchSelection, getState, type MenuEntry,
} from '../state/store';
import { item, SEP } from '../menu/entries';
import type { EditOp, JsonValue, PickedEntity } from '../api/types';
import {
  buildModel, hitTest, dragTarget, validConstraints, dimensionFor, dimensionGeometry, entityOf, fmtNum, mid, type DimensionPlan, type Pt, type SketchModel, refKind, curvePointsOf,
  curveOf, nearestOnCurve, CONSTRAINT_PREFIX,
} from './model';
import { drawModel, planeGrid, polyline, points, toWorld, disposeGroup, snap, round3, curvePoints, COLORS } from './draw';
import { SketchLabels, type Placements } from './SketchLabels';
import { ExprInput } from '../panel/ExprInput';

const TOOLS: { id: SketchTool; label: string; key: string; hint: string }[] = [
  { id: 'line', label: 'line', key: 'l', hint: 'click start, then end; keeps chaining, esc stops' },
  { id: 'circle', label: 'circle', key: 'c', hint: 'click centre, then a point on the circle' },
  { id: 'arc', label: 'arc', key: 'a', hint: 'click centre, start, then end (counter-clockwise)' },
  { id: 'rect', label: 'rect', key: 'r', hint: 'click two opposite corners' },
  { id: 'slot', label: 'slot', key: 's', hint: 'click both arc centres, then a point for the width' },
  { id: 'polygon', label: 'polygon', key: 'p', hint: 'click points, double-click or click the first point to close' },
  { id: 'point', label: 'point', key: 'o', hint: 'click to place a point' },
];

/** A point the cursor snapped to: a handle (coincident), a midpoint, or a point on a curve. */
interface Snapped { p: Pt; ref: string | null; snap?: 'handle' | 'mid' | 'on' }
/** A value box waiting at a point of the plane: a dimension's value after its placement click, an offset's distance after its side click. */
type Pending = { kind: 'dimension'; plan: DimensionPlan; at: Pt } | { kind: 'offset'; at: Pt };

export function SketchOverlay() {
  const sm = useStore((s) => s.sketchMode)!;
  const tree = useStore((s) => s.tree);
  const hover3d = useStore((s) => s.hover);
  const mesh = useStore((s) => s.mesh);
  const feature = featureByName(sm.sketch);
  const [cursor, setCursor] = useState<Pt | null>(null);
  const [nPoints, setNPoints] = useState(0);
  const [tick, setTick] = useState(0);
  const [pending, setPending] = useState<Pending | null>(null);
  const [dimAlt, setDimAlt] = useState(0);
  const [snapGlyph, setSnapGlyph] = useState<{ x: number; y: number; text: string; title: string } | null>(null);
  const [offsetDistance, setOffsetDistance] = useState('2');  // the last distance used, offered again
  const storageKey = `plainsolid:dims:${tree?.path ?? ''}:${sm.sketch}`;
  const [placements, setPlacements] = useState<Placements>(() => loadPlacements(storageKey));
  const pointsRef = useRef<Snapped[]>([]);
  const preview = useRef<THREE.Object3D | null>(null);
  const group = useRef<THREE.Group | null>(null);
  const frameRef = useRef<PlaneFrame | null>(null);
  const modelRef = useRef<SketchModel | null>(null);
  const dragRef = useRef<string | null>(null);
  const chainRef = useRef<Snapped | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  pendingRef.current = pending;

  const model = useMemo(() => (feature ? buildModel(feature, sm.preview) : null), [feature, sm.preview]);
  modelRef.current = model;

  // frame, grid, camera, ghosted body, camera tick for the label layer
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const frame = frameFromInfo(sm.frame);
    frameRef.current = frame;
    scene.setSketchFrame(frame);
    scene.setGhost(true);
    const grid = planeGrid(frame, scene.modelSize());
    scene.overlay.add(grid);
    const prevCam = scene.onCameraChange;
    scene.onCameraChange = (c) => { prevCam(c); setTick((t) => t + 1); };
    const onResize = () => setTick((t) => t + 1);
    window.addEventListener('resize', onResize);
    setTick((t) => t + 1);
    return () => {
      window.removeEventListener('resize', onResize);
      scene.onCameraChange = prevCam;
      scene.overlay.remove(grid);
      scene.setSketchFrame(null);
      scene.setGhost(false);
      scene.overlay.clear();
      scene.requestRender();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sm.sketch, sm.frame]);

  useEffect(() => { setPlacements(loadPlacements(storageKey)); }, [storageKey]);
  useEffect(() => { setTick((t) => t + 1); }, [mesh]);

  // the drawing: entities, handles, dimension lines
  useEffect(() => {
    const scene = sceneRef.current, frame = frameRef.current;
    if (!scene || !frame) return;
    if (group.current) { scene.overlay.remove(group.current); disposeGroup(group.current); }
    if (!model) return;
    const g = drawModel(frame, model, { selection: new Set(sm.selection), hover: sm.hover, highlight: new Set(sm.highlight) });
    for (const c of model.constraints) {
      if (!c.dimension) continue;
      const geo = dimensionGeometry(model, c);
      if (!geo) continue;
      const label = placements[c.name] ?? geo.label;
      const lit = sm.highlight.includes(c.name) || c.refs.some((r) => sm.highlight.includes(r));
      const color = model.solution?.conflicting?.includes(c.name) ? COLORS.conflict : lit ? COLORS.selected : 0xd8c27a;
      g.add(polyline(frame, [geo.a, geo.b], color));
      g.add(polyline(frame, [mid(geo.a, geo.b), label], color, true));
    }
    group.current = g;
    scene.overlay.add(g);
    scene.requestRender();
    setTick((t) => t + 1);
  }, [model, sm.selection, sm.hover, sm.highlight, placements]);

  // tool interaction
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    pointsRef.current = [];
    setNPoints(0);
    if (sm.tool !== 'dimension' && sm.tool !== 'offset') setPending(null);
    const setPreview = (obj: THREE.Object3D | null) => {
      if (preview.current) { scene.overlay.remove(preview.current); disposeGroup(preview.current); }
      preview.current = obj;
      if (obj) scene.overlay.add(obj);
      scene.requestRender();
    };
    const frame = frameRef.current!;
    const tool = sm.tool;
    scene.sketchOrbitFree = !tool && !pendingRef.current;
    const tol = () => 8 * scene.pixelSize();
    // a hook for the verification script: what a point on the plane would hit
    (window as unknown as { __plainsolid: Record<string, unknown> }).__plainsolid.sketchHit = (u: number, v: number) => { const m = modelRef.current; return m ? { hit: hitTest(m, [u, v], tol()), tol: tol(), target: (() => { const h = hitTest(m, [u, v], tol()); return h ? dragTarget(m, h.ref) : null; })() } : null; };
    let altKey = false, shiftKey = false;
    // snapping: handles first (ends, centres, corners, midpoints), then a point on a curve; shift held turns it off
    const snapPoint = (u: number, v: number): Snapped => {
      const m = modelRef.current;
      if (!m || shiftKey) return { p: [snap(u), snap(v)], ref: null };
      const hit = hitTest(m, [u, v], tol(), true);
      if (hit?.handle) return { p: hit.handle.p, ref: hit.ref, snap: hit.handle.kind === 'mid' ? 'mid' : 'handle' };
      const c = hit ? curveOf(m, hit.ref) : null;
      if (hit && c) { const q = nearestOnCurve(c, [u, v]); return { p: [round3(q[0]), round3(q[1])], ref: hit.ref, snap: 'on' }; }
      return { p: [snap(u), snap(v)], ref: null };
    };
    void altKey;
    const onMod = (e: KeyboardEvent) => { altKey = e.altKey; shiftKey = e.shiftKey; };
    // the arc a point sits at the end of, when the arc's tangent there runs within 10° of a direction
    const tangentArc = (s: Snapped, towards: Pt): string | null => {
      const m = modelRef.current;
      if (!m || s.snap !== 'handle' || !s.ref || !/\.(start|end)$/.test(s.ref)) return null;
      const ent = entityOf(s.ref);
      const c = curveOf(m, ent);
      if (!c || c.kind !== 'arc') return null;
      const d = [towards[0] - s.p[0], towards[1] - s.p[1]], r = [s.p[0] - c.c[0], s.p[1] - c.c[1]];
      const ld = Math.hypot(d[0], d[1]), lr = Math.hypot(r[0], r[1]);
      if (ld < 1e-6 || lr < 1e-6) return null;
      return Math.abs((d[0] * r[0] + d[1] * r[1]) / (ld * lr)) < 0.17 ? ent : null;
    };
    const glyphFor = (sn: Snapped, from: Snapped | null): { text: string; title: string } | null => {
      if (sn.snap === 'handle') return { text: '•', title: `coincident with ${sn.ref}` };
      if (sn.snap === 'mid') return { text: '⊣', title: `midpoint of ${sn.ref?.replace(/\.mid$/, '')}` };
      if (sn.snap === 'on') return { text: '⊙', title: `on ${sn.ref}` };
      if (tool === 'line' && from) {
        const t = tangentArc(from, sn.p);
        if (t) return { text: '◠', title: `tangent to ${t}` };
        const ang = Math.abs((Math.atan2(sn.p[1] - from.p[1], sn.p[0] - from.p[0]) * 180) / Math.PI);
        if (ang < 3 || ang > 177) return { text: '—', title: 'horizontal' };
        if (Math.abs(ang - 90) < 3) return { text: '|', title: 'vertical' };
      }
      return null;
    };
    const showGlyph = (sn: Snapped, from: Snapped | null) => {
      const g = glyphFor(sn, from);
      if (!g) { setSnapGlyph(null); return; }
      const [x, y] = scene.toScreen(toWorld(frame, sn.p));
      setSnapGlyph({ x, y, ...g });
    };
    window.addEventListener('keydown', onMod); window.addEventListener('keyup', onMod);
    const names = () => sketchNames();
    const sketch = sm.sketch;
    const asConstruction = sm.construction;
    const dashed = asConstruction;  // the rubber band draws like what it will make
    const entityOp = (kind: string, name: string, args: Record<string, JsonValue>): EditOp =>
      ({ op: 'add_sketch_entity', sketch, kind, name, args: asConstruction && kind !== 'point' ? { ...args, construction: true } : args });
    const constraintOp = (kind: string, refs: string[]): EditOp => ({ op: 'add_constraint', sketch, kind, name: nextName(CONSTRAINT_PREFIX[kind] ?? kind, [...names(), ...pendingNames]), refs });
    let pendingNames: string[] = [];
    /** The constraint a snapped point gets: coincident with a handle, midpoint of a line, on a curve. */
    const snapOp = (pointRef: string, s: Snapped): EditOp | null => {
      if (!s.ref) return null;
      if (s.snap === 'mid') return constraintOp('midpoint', [pointRef, s.ref.replace(/\.mid$/, '')]);
      if (s.snap === 'on') { const c = modelRef.current ? curveOf(modelRef.current, s.ref) : null; return constraintOp(c && c.kind !== 'line' ? 'on' : 'coincident', [pointRef, s.ref]); }
      return constraintOp('coincident', [pointRef, s.ref]);
    };
    const pushOp = (ops: EditOp[], op: EditOp | null) => { if (op) { ops.push(op); if ('name' in op && op.name) pendingNames.push(op.name); } };
    const reset = () => { pointsRef.current = []; setNPoints(0); setPreview(null); setSnapGlyph(null); };

    const commitLine = async (a: Snapped, b: Snapped) => {
      const name = nextName('line', names());
      pendingNames = [name];
      const ops: EditOp[] = [entityOp('line', name, { start: a.p, end: b.p })];
      pushOp(ops, snapOp(`${name}.start`, a));
      pushOp(ops, snapOp(`${name}.end`, b));
      const ang = Math.abs((Math.atan2(b.p[1] - a.p[1], b.p[0] - a.p[0]) * 180) / Math.PI);
      if (ang < 3 || ang > 177) pushOp(ops, constraintOp('horizontal', [name]));
      else if (Math.abs(ang - 90) < 3) pushOp(ops, constraintOp('vertical', [name]));
      // a line leaving an arc's end along its tangent stays tangent
      const tangentTo = tangentArc(a, b.p) ?? tangentArc(b, a.p);
      if (tangentTo) pushOp(ops, constraintOp('tangent', [name, tangentTo]));
      pendingNames = [];
      const ok = await sketchBatch(ops, `added ${name}${ops.length > 1 ? ` with ${ops.length - 1} constraint(s)` : ''}`);
      return ok ? name : null;
    };
    const commitEntity = async (kind: string, args: Record<string, JsonValue>, coincidences: [string, Snapped][] = []) => {
      const name = nextName(kind === 'project' ? 'proj' : kind, names());
      pendingNames = [name];
      const ops: EditOp[] = [entityOp(kind, name, args)];
      for (const [part, s] of coincidences) pushOp(ops, snapOp(`${name}.${part}`, s));
      pendingNames = [];
      const ok = await sketchBatch(ops, `added ${name}`);
      return ok ? name : null;
    };
    const finishPolygon = () => {
      const pts = pointsRef.current;
      if (pts.length >= 3) commitEntity('polygon', { points: pts.map((s) => s.p) });
      reset();
    };

    scene.onPlaneMove = (u, v, body) => {
      const m = modelRef.current;
      const p: Pt = [round3(u), round3(v)];
      setCursor(p);
      const pts = pointsRef.current;
      if (!tool || tool === 'dimension') {
        const hit = m ? hitTest(m, [u, v], tol()) : null;
        setSketchHover(hit?.ref ?? null);
        if (!tool) hoverEntity(hit ? null : body);  // a body edge or vertex lights up under a modifier
        return;
      }
      if (tool === 'offset') { setSketchHover(null); return; }
      if (pts.length === 0) { const sn = snapPoint(u, v); setSketchHover(sn.snap === 'handle' || sn.snap === 'mid' ? sn.ref : null); showGlyph(sn, null); return; }
      const sn = snapPoint(u, v);
      const s = sn.p;
      showGlyph(sn, tool === 'line' ? pts[0] : null);
      const c = pts[0].p;
      if (tool === 'line') setPreview(polyline(frame, [c, s], COLORS.preview, dashed));
      else if (tool === 'circle') setPreview(polyline(frame, curvePoints({ ref: '', entity: '', kind: 'circle', c, r: Math.hypot(s[0] - c[0], s[1] - c[1]) }), COLORS.preview, dashed));
      else if (tool === 'arc') {
        if (pts.length === 1) setPreview(polyline(frame, [c, s], COLORS.preview, true));
        else { const st = pts[1].p; const r = Math.hypot(st[0] - c[0], st[1] - c[1]); setPreview(polyline(frame, curvePoints({ ref: '', entity: '', kind: 'arc', c, r, a0: Math.atan2(st[1] - c[1], st[0] - c[0]), a1: Math.atan2(s[1] - c[1], s[0] - c[0]) }), COLORS.preview, dashed)); }
      }
      else if (tool === 'rect') setPreview(polyline(frame, [c, [s[0], c[1]], s, [c[0], s[1]], c], COLORS.preview, dashed));
      else if (tool === 'slot') {
        if (pts.length === 1) setPreview(polyline(frame, [c, s], COLORS.preview, true));
        else { const b = pts[1].p; const w = 2 * distToLine(s, c, b); setPreview(slotPreview(frame, c, b, w, dashed)); }
      }
      else if (tool === 'polygon') setPreview(polyline(frame, [...pts.map((x) => x.p), s], COLORS.preview, dashed));
    };

    scene.onPlaneClick = async (u, v, ev, body) => {
      cancelDragPreview();
      const m = modelRef.current;
      if (pendingRef.current) return;
      if (!tool) {
        const hit = m ? hitTest(m, [u, v], tol()) : null;
        const additive = ev.shiftKey || ev.ctrlKey || ev.metaKey;
        if (!hit && additive && body && body.kind !== 'face') { hoverEntity(null); void useBodyInRelation(body, scene.entityCenter(body)); return; }
        // a plain click on the body selects a face, edge or vertex to convert; empty plane clears both selections
        if (!hit && body) { toggleBodySelect(body, additive); return; }
        toggleSketchSelect(hit?.ref ?? null, additive);
        if (!hit && !additive) toggleBodySelect(null, false);
        return;
      }
      if (tool === 'offset') { setPending({ kind: 'offset', at: [round3(u), round3(v)] }); return; }
      if (tool === 'dimension') {
        if (sm.dimPlacing) { setPending({ kind: 'dimension', plan: sm.dimPlacing, at: [round3(u), round3(v)] }); setDimAlt(0); return; }
        const hit = m ? hitTest(m, [u, v], tol()) : null;
        toggleSketchSelect(hit?.ref ?? null, true);
        return;
      }
      const s = snapPoint(u, v);
      const pts = pointsRef.current;
      const same = (a: Pt, b: Pt) => a[0] === b[0] && a[1] === b[1];
      if (tool === 'point') { commitEntity('point', { at: s.p }); return; }
      if (tool === 'line') {
        if (pts.length === 0) { pts.push(s); setNPoints(1); return; }
        if (same(pts[0].p, s.p)) return;
        const name = await commitLine(pts[0], s);
        reset();
        if (name) { const next: Snapped = { p: s.p, ref: s.ref ?? `${name}.end` }; pointsRef.current = [next]; chainRef.current = next; setNPoints(1); }
      } else if (tool === 'circle') {
        if (pts.length === 0) { pts.push(s); setNPoints(1); return; }
        const d = 2 * Math.hypot(s.p[0] - pts[0].p[0], s.p[1] - pts[0].p[1]);
        if (d > 0) commitEntity('circle', { diameter: Math.round(d * 100) / 100, at: pts[0].p }, [['center', pts[0]]]);
        reset();
      } else if (tool === 'arc') {
        if (pts.length < 2) { pts.push(s); setNPoints(pts.length); return; }
        const [c, st] = pts;
        if (!same(c.p, st.p) && !same(st.p, s.p)) commitEntity('arc', { center: c.p, start: st.p, end: s.p }, [['center', c], ['start', st], ['end', s]]);
        reset();
      } else if (tool === 'rect') {
        if (pts.length === 0) { pts.push(s); setNPoints(1); return; }
        const a = pts[0].p, w = Math.abs(s.p[0] - a[0]), h = Math.abs(s.p[1] - a[1]);
        // the corners that were snapped get their coincidences: the first click's corner and the opposite one
        const first = `${a[1] < s.p[1] ? 'b' : 't'}${a[0] < s.p[0] ? 'l' : 'r'}`, second = `${a[1] < s.p[1] ? 't' : 'b'}${a[0] < s.p[0] ? 'r' : 'l'}`;
        if (w > 0 && h > 0) commitEntity('rect', { width: w, height: h, at: [(s.p[0] + a[0]) / 2, (s.p[1] + a[1]) / 2] }, [[first, pts[0]], [second, s]]);
        reset();
      } else if (tool === 'slot') {
        if (pts.length < 2) { if (pts.length === 0 || !same(pts[0].p, s.p)) { pts.push(s); setNPoints(pts.length); } return; }
        const a = pts[0].p, b = pts[1].p;
        const w = round3(2 * distToLine(s.p, a, b));
        const len = round3(Math.hypot(b[0] - a[0], b[1] - a[1]) + w);
        const angle = round3((Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI);
        if (w > 0) commitEntity('slot', { length: len, width: w, at: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], angle }, [['start', pts[0]], ['end', pts[1]]]);
        reset();
      } else if (tool === 'polygon') {
        if (pts.length >= 3 && same(pts[0].p, s.p)) { finishPolygon(); return; }
        if (pts.length && same(pts[pts.length - 1].p, s.p)) return;
        pts.push(s); setNPoints(pts.length);
      }
    };
    scene.onPlaneDoubleClick = () => { if (tool === 'polygon') finishPolygon(); };

    // the right-click menu: the tool's own choices first, then the entity, body geometry or sketch under the
    // cursor. On an entity the sections come in a fixed order, the same as the panel's selected section:
    // relations, dimension, edits (offset, construction), delete. Tools are listed only on empty space.
    scene.onPlaneContext = (u, v, ev, body) => {
      const m = modelRef.current;
      const out: MenuEntry[] = [];
      let title = `sketch ${sketch}`;
      const toolItems = (skip: SketchTool) => TOOLS.filter((t) => t.id !== skip).map((t) => item(t.label, () => setSketchTool(t.id), { key: t.key }));
      if (tool && tool !== 'dimension' && tool !== 'project' && tool !== 'offset') {
        if (tool === 'line' && pointsRef.current.length) {
          out.push(item('end the chain here', () => { reset(); chainRef.current = null; }));
          out.push(item('undo the last point', () => { pointsRef.current.pop(); setNPoints(pointsRef.current.length); if (!pointsRef.current.length) reset(); }));
        }
        out.push(item(sm.construction ? 'draw profile geometry' : 'draw construction geometry', toggleConstructionMode, { key: 'x' }));
        out.push(item('stop the tool', () => setSketchTool(null), { key: 'esc' }), SEP, ...toolItems(tool));
        openContextMenu(ev.clientX, ev.clientY, out, `${tool} tool`);
        return;
      }
      if (tool === 'dimension') { out.push(item('cancel the dimension', () => { setPending(null); setSketchTool(null); }, { key: 'esc' })); openContextMenu(ev.clientX, ev.clientY, out, 'dimension'); return; }
      if (tool === 'offset') { out.push(item('cancel the offset', () => { setPending(null); setSketchTool(null); }, { key: 'esc' })); openContextMenu(ev.clientX, ev.clientY, out, 'offset'); return; }
      if (tool === 'project') {
        if (body) out.push(item(body.kind === 'face' ? 'convert the face outline' : `convert this ${body.kind}`, () => void projectPick(body)));
        out.push(item('stop converting', () => setSketchTool(null), { key: 'esc' }));
        openContextMenu(ev.clientX, ev.clientY, out, body ? `body ${body.kind} ${body.id}` : 'convert');
        return;
      }
      const hit = m ? hitTest(m, [u, v], tol()) : null;
      if (hit && m) {
        const sel = sm.selection.includes(hit.ref) ? sm.selection : [hit.ref];
        if (!sm.selection.includes(hit.ref)) toggleSketchSelect(hit.ref, false);
        title = sel.join(' · ');
        for (const c of validConstraints(m, sel)) out.push(item(c.label, () => void addConstraint(c.kind, c.refs, c.options)));
        const plan = dimensionFor(m, sel);
        if (plan) out.push(item(`dimension (${plan.kind})`, () => beginDimension(plan), { key: 'd' }));
        const edits: MenuEntry[] = [];
        const curves = sel.filter((r) => refKind(m, r) !== 'point' && !r.endsWith('.axis'));
        if (curves.length) edits.push(item('offset…', () => setSketchTool('offset'), { title: 'click the side to offset to, then type the distance' }));
        const ents = [...new Set(sel.map(entityOf))].map((n) => m.entities.get(n)).filter((e) => e && !e.projected && e.kind !== 'point');
        if (ents.length) edits.push(item(ents.every((e) => e!.construction) ? 'make profile geometry' : 'make construction geometry', () => void toggleConstructionSelection()));
        if (out.length && edits.length) out.push(SEP);
        out.push(...edits);
        if (out.length) out.push(SEP);
        out.push(item('delete', () => void deleteSketchSelection(), { danger: true, key: 'del' }));
      } else if (body) {
        title = `body ${body.kind} ${body.id}`;
        out.push(item(body.kind === 'face' ? 'convert the face outline' : `convert this ${body.kind}`, () => { toggleBodySelect(body, false); void convertBodySelection(false); }));
        out.push(item('convert as construction', () => { toggleBodySelect(body, false); void convertBodySelection(true); }));
        if (body.kind !== 'face') out.push(item('use it in a relation', () => void useBodyInRelation(body, scene.entityCenter(body)), { key: 'shift+click' }));
      } else {
        out.push(...toolItems(null), SEP);
        const f = featureByName(sketch);
        if (f?.variable) out.push(item('extrude…', () => openFeatureDialog('extrude', sketch)), item('cut…', () => openFeatureDialog('cut', sketch)));
        out.push(item('offset the profile…', () => { toggleSketchSelect(null, false); setSketchTool('offset'); }, { title: 'offset every profile curve: click the side, then type the distance' }));
        out.push(SEP, item('normal to', () => scene.normalTo(), { key: 'ctrl+0' }), item('fit', () => scene.fit(), { key: 'f' }));
        if (getState().sketchMode?.selection.length) out.push(item('clear selection', () => toggleSketchSelect(null, false), { key: 'esc' }));
        out.push(SEP, item('exit sketch', exitSketch));
      }
      openContextMenu(ev.clientX, ev.clientY, out, title);
    };

    // dragging in select mode
    scene.onPlaneDown = (u, v) => {
      if (tool || pendingRef.current) return false;
      const m = modelRef.current;
      const hit = m ? hitTest(m, [u, v], tol()) : null;
      if (!hit || !m) return false;
      const target = dragTarget(m, hit.ref);
      if (!target) return false;
      dragRef.current = target;
      startDrag(target);
      return true;
    };
    scene.onPlaneDrag = (u, v) => { if (dragRef.current) previewDrag(dragRef.current, [round3(u), round3(v)]); };
    scene.onPlaneUp = (u, v) => { const t = dragRef.current; dragRef.current = null; if (t) endDrag(t, [round3(u), round3(v)]); };

    // projection: pick the ghosted body. The viewport owns the default handlers (they pass the
    // entity's position along, which dialog picks need), so keep them and put them back after.
    const previous = { onPick: scene.onPick, onHover: scene.onHover };
    if (tool === 'project') {
      scene.sketchPickBody = true;
      scene.setPickMode('all');
      scene.onPick = (ent) => projectPick(ent);
      scene.onHover = (ent) => hoverEntity(ent);
    }

    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.closest('.cm-editor'))) return;
      if (e.key === 'Escape') {
        // never leaves the sketch: that is the exit button's job
        if (pendingRef.current) { setPending(null); setSketchTool(null); return; }
        if (pointsRef.current.length) { reset(); chainRef.current = null; }
        else if (tool) { setSketchTool(null); }
        else if (sm.selection.length || sm.bodySelection.length) { toggleSketchSelect(null, false); toggleBodySelect(null, false); }
        return;
      }
      if (e.key === 'Enter' && tool === 'polygon') { finishPolygon(); return; }
      if (e.key === 'Delete' || e.key === 'Backspace') { deleteSelection(); return; }
      if (e.key === 'd' && !tool) { startDimension(); return; }
      if (e.key === 'e') { setSketchTool('project'); return; }
      if (e.key === 'x') { toggleConstructionMode(); return; }  // the switch, and only the switch
      const hit = TOOLS.find((x) => x.key === e.key);
      if (hit && hit.id !== tool) setSketchTool(hit.id);
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('keydown', onMod); window.removeEventListener('keyup', onMod);
      scene.onPlaneMove = () => {}; scene.onPlaneClick = () => {}; scene.onPlaneDoubleClick = () => {}; scene.onPlaneContext = () => {};
      setSnapGlyph(null);
      scene.onPlaneDown = () => false; scene.onPlaneDrag = () => {}; scene.onPlaneUp = () => {};
      scene.sketchOrbitFree = false;
      if (tool === 'project') { scene.sketchPickBody = false; scene.setPickMode('faces'); scene.onPick = previous.onPick; scene.onHover = previous.onHover; hoverEntity(null); }
      setPreview(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sm.tool, sm.sketch, sm.dimPlacing, sm.selection.length, sm.construction]);

  /** Convert entities: a body edge, vertex or face outline becomes sketch geometry that follows the
   * body. Real profile geometry unless draw-as-construction is on; the tool stays on for more picks. */
  const projectPick = async (ent: PickedEntity | null) => {
    const scene = sceneRef.current;
    if (!ent || !scene || !tree || !feature) return;
    const center = scene.entityCenter(ent);
    if (!center) return;
    if (!feature.variable) { setError('convert: the sketch must be assigned to a variable'); return; }
    const target = selectorTarget(ent, center);
    if (!target || target.kind === 'plane') return;
    const name = nextName(ent.kind, sketchNames());
    const ok = await edit({ op: 'add_sketch_entity', sketch: feature.name, kind: 'project', name,
                            args: { selector: { expr: target.expr }, construction: sm.construction } });
    if (ok) setStatus(`converted ${ent.kind} ${ent.id} as ${name}${sm.construction ? ' (construction)' : ''} · pick more or esc`);
  };

  /** Offset entities: the selected curves (or the whole profile when nothing is selected) by the
   * distance typed in the value box, on the side that was clicked. */
  const commitOffset = async (click: Pt, text: string) => {
    const m = modelRef.current;
    if (!m || !feature) return;
    const value = expressionValue(text);
    if (value === null) { setError('offset: give a distance'); return; }
    setOffsetDistance(text);
    const picked = [...new Set(sm.selection.filter((r) => refKind(m, r) !== 'point' && !r.endsWith('.axis')))];
    const refs = picked.length ? picked
      : feature.entities.filter((e) => !e.construction && e.kind !== 'point').map((e) => e.name);
    if (!refs.length) { setError('offset: select curves first, or draw a profile'); return; }
    const chain = orderedChain(m, refs);
    if (!chain) { setError('offset: the curves must form one connected chain'); return; }
    let side: string;
    if (chain.closed) side = pointInPolygon(click, chain.points) ? 'inside' : 'outside';
    else {
      const seg = nearestSegment(chain.points, click);
      const d: Pt = [chain.points[seg + 1][0] - chain.points[seg][0], chain.points[seg + 1][1] - chain.points[seg][1]];
      side = d[0] * (click[1] - chain.points[seg][1]) - d[1] * (click[0] - chain.points[seg][0]) > 0 ? 'left' : 'right';
    }
    const name = nextName('offset', sketchNames());
    const ok = await edit({ op: 'add_sketch_entity', sketch: feature.name, kind: 'offset', name,
                            args: { of: refs, distance: value, side, corners: 'sharp', construction: sm.construction } });
    if (ok) { setStatus(`added ${name} ${side} of ${refs.length} curve${refs.length === 1 ? '' : 's'}`); setSketchTool(null); toggleSketchSelect(null, false); }
  };

  const startDimension = () => {
    if (!model) return;
    const plan = dimensionFor(model, sm.selection);
    beginDimension(plan);
    if (!plan) setStatus(sm.selection.length ? 'no dimension fits this selection' : 'dimension: pick one or two entities, then click to place');
  };

  const deleteSelection = () => void deleteSketchSelection();

  const commitPending = async (text: string) => {
    if (!pending) return;
    if (pending.kind === 'offset') { setPending(null); await commitOffset(pending.at, text); return; }
    const plan = dimAlt > 0 && pending.plan.alternatives ? pending.plan.alternatives[dimAlt - 1] : pending.plan;
    const value = expressionValue(text);
    if (value === null) return;
    const name = await addConstraint(plan.kind, plan.refs, plan.options, value);
    if (name) place(name, pending.at);
    setPending(null);
  };

  const place = (name: string, p: Pt) => {
    setPlacements((prev) => { const next = { ...prev, [name]: p }; try { localStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* no storage */ } return next; });
  };

  const scene = sceneRef.current;
  const frame = frameRef.current;
  const tool = TOOLS.find((t) => t.id === sm.tool);
  const sol = model?.solution ?? null;
  const dofText = sol ? (sol.fully_constrained ? 'fully constrained' : `${sol.dof} degree${sol.dof === 1 ? '' : 's'} of freedom`) : feature?.result?.error ? 'sketch failed' : '';
  const pendingScreen = pending && scene && frame ? scene.toScreen(toWorld(frame, pending.at)) : null;
  const pendingPlan = pending?.kind === 'dimension' ? (dimAlt > 0 && pending.plan.alternatives ? pending.plan.alternatives[dimAlt - 1] : pending.plan) : null;
  const hoverText = sm.tool === 'project' && hover3d ? `${hover3d.kind} ${hover3d.id} · click to convert`
    : !sm.tool && hover3d ? `body ${hover3d.kind} ${hover3d.id} · click to select it for conversion · shift+click relates an edge or vertex` : sm.hover ?? '';

  return (
    <>
      <div className="sketch-top">
      <div className="sketch-bar" data-testid="sketch-bar">
        <span className="sketch-title">sketch <b>{sm.sketch}</b> on {sm.plane}</span>
        {TOOLS.map((t) => (
          <button key={t.id} className={`btn-small ${sm.tool === t.id ? 'active' : ''} ${sm.construction && sm.tool === t.id ? 'construction' : ''}`} title={`${t.hint} (${t.key})`} onClick={() => setSketchTool(t.id)}>{t.label}</button>
        ))}
        <button className={`btn-small ${sm.tool === 'dimension' ? 'active' : ''}`} title="dimension the selection, then click to place it (d)" data-testid="sketch-dimension" onClick={() => (sm.tool === 'dimension' ? setSketchTool(null) : startDimension())}>dimension</button>
        <label className={`switch ${sm.construction ? 'on' : ''}`} title="the construction switch: new geometry, converts and offsets come out as construction while it is on (x)">
          <input type="checkbox" checked={sm.construction} onChange={toggleConstructionMode} data-testid="sketch-construction-mode" />construction
        </label>
        <button className={`btn-small ${sm.tool === 'project' ? 'active' : ''} ${sm.construction && sm.tool === 'project' ? 'construction' : ''}`} title="convert body edges, vertices or a face outline into sketch geometry that follows the body (e); stays on until esc" data-testid="sketch-project" onClick={() => setSketchTool(sm.tool === 'project' ? null : 'project')}>convert</button>
        <span className="sketch-hint">
          {tool ? `${sm.construction ? 'construction · ' : ''}${tool.hint}${nPoints ? ` (${nPoints} placed)` : ''}`
            : sm.tool === 'dimension' ? (sm.dimPlacing ? 'click to place the dimension' : 'pick one or two entities')
            : sm.tool === 'project' ? (hoverText || `${sm.construction ? 'construction · ' : ''}hover the body: click edges, vertices or a face outline to convert them · esc when done`)
            : sm.tool === 'offset' ? (pending ? 'type the distance' : `offset ${sm.selection.length ? 'the selection' : 'the whole profile'}: click the side to offset to, then type the distance`)
            : sm.dragging ? (sm.locked ? 'locked: fully constrained' : 'dragging…')
            : hoverText || 'select: click, shift+click adds · right-click for relations and edits · drag handles · d dimension · e convert · x construction · esc'}
        </span>
        {cursor && <span className="sketch-cursor">{cursor[0]}, {cursor[1]}</span>}
        <span className={`sketch-dof ${sol?.fully_constrained ? 'ok' : sol?.conflicting?.length ? 'bad' : ''}`} data-testid="sketch-dof"
              title={sol && !sol.fully_constrained && sol.free_entities.length ? `free: ${freeText(sol)}` : undefined}>{dofText}{sm.solveMs !== null && sm.dragging ? ` · ${sm.solveMs.toFixed(0)} ms` : ''}</span>
        <button className="btn-small" onClick={exitSketch} data-testid="sketch-exit">exit sketch</button>
      </div>
      {(sol?.redundant?.length || sol?.conflicting?.length || feature?.result?.error) ? (
        <div className="sketch-warn" data-testid="sketch-warn">
          {feature?.result?.error && <span>{feature.result.error.message}</span>}
          {sol?.conflicting?.length ? <span>conflicting: {sol.conflicting.join(', ')}</span> : null}
          {sol?.redundant?.length ? <span>redundant: {sol.redundant.join(', ')}</span> : null}
        </div>
      ) : null}
      </div>
      {scene && frame && model && <SketchLabels scene={scene} frame={frame} model={model} placements={placements} onPlace={place} tick={tick} />}
      {snapGlyph && sm.tool && <span className="snap-glyph" style={{ left: snapGlyph.x + 16, top: snapGlyph.y - 16 }} title={snapGlyph.title} data-testid="snap-glyph">{snapGlyph.text}</span>}
      {pending && pendingScreen && (
        <div className="dim-label editing pending" style={{ left: pendingScreen[0], top: pendingScreen[1] }} data-testid={pending.kind === 'offset' ? 'offset-pending' : 'dim-pending'}>
          {pending.kind === 'dimension' && pending.plan.alternatives && (
            <span className="dim-alts">
              {[pending.plan, ...pending.plan.alternatives].map((p, i) => (
                <button key={i} className={`btn-small ${i === dimAlt ? 'active' : ''}`} onClick={() => setDimAlt(i)}>{p.options?.along ? `d${p.options.along}` : 'dist'}</button>
              ))}
            </span>
          )}
          {pending.kind === 'dimension' && pendingPlan
            ? <ExprInput text={fmtNum(pendingPlan.value)} names={expressionNames()} autoFocus commitUnchanged testId="dim-pending-input" onCommit={commitPending} onCancel={() => setPending(null)} />
            : <><span className="dim-kind">offset </span><ExprInput text={offsetDistance} names={expressionNames()} autoFocus commitUnchanged testId="offset-pending-input" onCommit={commitPending} onCancel={() => { setPending(null); setSketchTool(null); }} /></>}
        </div>
      )}
    </>
  );
}

function loadPlacements(key: string): Placements {
  try { const raw = localStorage.getItem(key); return raw ? (JSON.parse(raw) as Placements) : {}; } catch { return {}; }
}

function distToLine(p: Pt, a: Pt, b: Pt): number {
  const dx = b[0] - a[0], dy = b[1] - a[1], n = Math.hypot(dx, dy);
  return n ? Math.abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / n : Math.hypot(p[0] - a[0], p[1] - a[1]);
}

function slotPreview(frame: PlaneFrame, a: Pt, b: Pt, w: number, dashed = false): THREE.Object3D {
  const g = new THREE.Group();
  const r = w / 2, ang = Math.atan2(b[1] - a[1], b[0] - a[0]);
  const n: Pt = [-Math.sin(ang), Math.cos(ang)];
  g.add(polyline(frame, [[a[0] + r * n[0], a[1] + r * n[1]], [b[0] + r * n[0], b[1] + r * n[1]]], COLORS.preview, dashed));
  g.add(polyline(frame, [[a[0] - r * n[0], a[1] - r * n[1]], [b[0] - r * n[0], b[1] - r * n[1]]], COLORS.preview, dashed));
  g.add(polyline(frame, curvePoints({ ref: '', entity: '', kind: 'arc', c: a, r, a0: ang + Math.PI / 2, a1: ang + 3 * Math.PI / 2 }), COLORS.preview, dashed));
  g.add(polyline(frame, curvePoints({ ref: '', entity: '', kind: 'arc', c: b, r, a0: ang - Math.PI / 2, a1: ang + Math.PI / 2 }), COLORS.preview, dashed));
  g.add(points(frame, [a, b], COLORS.preview, 5));
  return g;
}

/** The selected curves walked end to end: ordered sample points, and whether they close. */
function orderedChain(m: SketchModel, refs: string[]): { points: Pt[]; closed: boolean } | null {
  const curves = m.curves.filter((c) => !(c.kind === 'line' && c.decor) && !c.ref.endsWith('.axis') && (refs.includes(c.ref) || refs.includes(c.entity)));
  if (!curves.length) return null;
  const near = (a: Pt, b: Pt) => Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-3;
  if (curves.length === 1 && curves[0].kind === 'circle') return { points: curvePointsOf(curves[0]), closed: true };
  const left = curves.slice(1);
  let pts = curvePointsOf(curves[0]);
  let guard = 0;
  while (left.length && guard++ < 200) {
    const end = pts[pts.length - 1], start = pts[0];
    const i = left.findIndex((c) => { const q = curvePointsOf(c); return near(q[0], end) || near(q[q.length - 1], end) || near(q[0], start) || near(q[q.length - 1], start); });
    if (i < 0) return null;
    const c = left.splice(i, 1)[0];
    let q = curvePointsOf(c);
    if (near(q[0], end)) pts = [...pts, ...q.slice(1)];
    else if (near(q[q.length - 1], end)) pts = [...pts, ...q.slice(0, -1).reverse()];
    else if (near(q[q.length - 1], start)) pts = [...q.slice(0, -1), ...pts];
    else { q = q.reverse(); pts = [...q.slice(0, -1), ...pts]; }
  }
  if (left.length) return null;
  return { points: pts, closed: near(pts[0], pts[pts.length - 1]) };
}

function pointInPolygon(p: Pt, poly: Pt[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if (yi > p[1] !== yj > p[1] && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function nearestSegment(pts: Pt[], p: Pt): number {
  let best = 0, bestD = Infinity;
  for (let i = 0; i + 1 < pts.length; i++) {
    const d = distToSegmentLocal(p, pts[i], pts[i + 1]);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

function distToSegmentLocal(p: Pt, a: Pt, b: Pt): number {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy;
  const t = l2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2)) : 0;
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}
