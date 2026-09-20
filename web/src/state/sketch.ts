import { api } from '../api/client';
import { CONSTRAINT_PREFIX, takenNames } from '../sketch/model';
import { set, state } from './core';
import { edit } from './documents';
import { fetchGhost } from './geometry';
import { featureByName, featurePreviews, nextName, selectorTarget, setStatus, setUpto } from './tools';
import type { EditOp, Feature, JsonValue, PickedEntity, PlaneInfo, SketchSolution, Tree } from '../api/types';
import type { Corner, DimLock } from '../sketch/model';
import type { SketchMode, SketchTool } from './core';

// ---- sketch mode -------------------------------------------------------------

/** The plane a sketch lies on: the server's resolved plane, or the standard plane from its args. */
export function sketchFrameOf(feature: Feature): PlaneInfo | null {
  if (feature.result?.plane) return feature.result.plane;
  const on = feature.args.on;
  if (typeof on !== 'string') return null;
  const axes: Record<string, [number[], number[], number[]]> = {
    XY: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], XZ: [[1, 0, 0], [0, 0, 1], [0, -1, 0]], YZ: [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
  };
  const a = axes[on];
  if (!a) return null;
  const offset = Number(feature.args.offset ?? 0) * (feature.args.flip ? -1 : 1);
  const z = feature.args.flip ? a[2].map((c) => -c) : a[2];
  return { origin: [a[2][0] * offset, a[2][1] * offset, a[2][2] * offset] as [number, number, number],
           x_dir: a[0] as [number, number, number], y_dir: a[1] as [number, number, number], z_dir: z as [number, number, number], size: 60 };
}

/** What to call the plane in the sketch bar. */
export function planeLabelOf(feature: Feature): string {
  const on = feature.args.on;
  const text = feature.arg_texts?.on ?? (typeof on === 'string' ? on : JSON.stringify(on));
  // keep the sketch bar short: a face selector reads as "a face of body"
  const m = /^([A-Za-z_][A-Za-z0-9_]*)\.faces\b/.exec(text);
  const label = m ? `a face of ${m[1]}` : text;
  return feature.args.offset ? `${label} offset ${feature.args.offset}` : label;
}

function samePlane(a: PlaneInfo | null, b: PlaneInfo | null): boolean {
  if (!a || !b) return a === b;
  const eq = (p: number[], q: number[]) => p.every((v, i) => Math.abs(v - q[i]) < 1e-6);
  return eq(a.origin, b.origin) && eq(a.x_dir, b.x_dir) && eq(a.y_dir, b.y_dir) && eq(a.z_dir, b.z_dir);
}

/** After a refetch, follow the sketch plane if an upstream edit moved it. */
export function syncSketchFrame(tree: Tree) {
  const sm = state.sketchMode;
  if (!sm) return;
  const f = tree.features.find((x) => x.name === sm.sketch);
  if (!f) { set({ sketchMode: null }); return; }
  const frame = sketchFrameOf(f);
  if (frame && !samePlane(frame, sm.frame)) patchSketch({ frame, plane: planeLabelOf(f) });
}

export function enterSketch(feature: Feature) {
  const frame = sketchFrameOf(feature);
  if (!frame) { set({ error: `${feature.name}: its plane could not be resolved (see the feature error)` }); return; }
  const plane = planeLabelOf(feature);
  const offset = Number(feature.args.offset ?? 0);
  set({
    sketchMode: { sketch: feature.name, plane, offset, frame, construction: false, tool: null, selection: [], bodySelection: [], hover: null, highlight: [], preview: null,
                  dragging: null, locked: false, solveMs: null, dimLock: null, cornerAsk: null, dimEditing: null },
    selected: feature.name, selectedFace: null, tool: 'none', planeDialog: null, featureDialog: null, dialogPicks: [], pickRequest: null,
    orthoBeforeSketch: state.ortho, ortho: true, ghostMesh: null,
  });
  // like SolidWorks, editing a sketch shows the model as it was just before the sketch
  const features = state.tree?.features ?? [];
  const idx = features.findIndex((f) => f.name === feature.name);
  setUpto(idx > 0 ? features[idx - 1].name : feature.name).then(() => fetchGhost());
}

function patchSketch(patch: Partial<SketchMode>) {
  if (state.sketchMode) set({ sketchMode: { ...state.sketchMode, ...patch } });
}

export function setSketchTool(tool: SketchTool) {
  if (!state.sketchMode) return;
  const next = state.sketchMode.tool === tool ? null : tool;
  // dimension and offset act on the selection; drawing tools start from a clean slate
  patchSketch({ tool: next, dimLock: null, hover: null, ...(next === 'dimension' || next === 'offset' ? {} : { selection: next ? [] : state.sketchMode.selection }) });
}

export function exitSketch() {
  cancelDragPreview();
  featurePreviews.cancel();
  set({ sketchMode: null, ghostMesh: null, ghostStyle: 'ghost', previewNote: null, ortho: state.orthoBeforeSketch ?? state.ortho, orthoBeforeSketch: null, hover: null });
  if (state.upto) setUpto(null);
}

export function setSketchSelection(selection: string[]) { patchSketch({ selection, dimLock: null }); }

/** Sketch mode: a plain click on the body selects a face, edge or vertex for conversion. */
export function toggleBodySelect(entity: PickedEntity | null, additive: boolean) {
  const sm = state.sketchMode;
  if (!sm) return;
  if (!entity) { if (sm.bodySelection.length) patchSketch({ bodySelection: [] }); return; }
  const has = sm.bodySelection.some((e) => e.kind === entity.kind && e.id === entity.id);
  const next = additive ? (has ? sm.bodySelection.filter((e) => !(e.kind === entity.kind && e.id === entity.id)) : [...sm.bodySelection, entity]) : has && sm.bodySelection.length === 1 ? [] : [entity];
  patchSketch({ bodySelection: next, selection: additive ? sm.selection : [], dimLock: null });
}

/** Convert the selected body entities into sketch geometry that follows the body (a face gives its outline). */
export async function convertBodySelection(construction: boolean): Promise<number> {
  const sm = state.sketchMode;
  const feature = sketchFeature();
  if (!sm || !feature || !sm.bodySelection.length) return 0;
  if (!feature.variable) { set({ error: 'the sketch must be assigned to a variable to convert body geometry' }); return 0; }
  const scene = sceneCenter;
  const ops: EditOp[] = [];
  const names: string[] = [];
  const taken = new Set(sketchNames());
  for (const ent of sm.bodySelection) {
    const center = scene ? scene(ent) : null;
    if (!center) continue;
    const target = selectorTarget(ent, center);
    if (!target || target.kind === 'plane') continue;
    const name = nextName(ent.kind, taken);
    taken.add(name);
    names.push(name);
    ops.push({ op: 'add_sketch_entity', sketch: sm.sketch, kind: 'project', name, args: { selector: { expr: target.expr }, construction } });
  }
  if (!ops.length) return 0;
  const ok = await sketchBatch(ops, `converted ${names.join(', ')}${construction ? ' (construction)' : ''}`);
  if (ok) patchSketch({ bodySelection: [], selection: names });
  return ok ? names.length : 0;
}

/** Where the viewport reports an entity's centre from; set by the viewport once its scene exists. */
export let sceneCenter: ((entity: PickedEntity) => [number, number, number] | null) | null = null;
export function setSceneCenter(fn: typeof sceneCenter) { sceneCenter = fn; }

/** Draw-as-construction mode: the drawing tools write construction=True. */
export function toggleConstructionMode() {
  const sm = state.sketchMode;
  if (sm) { patchSketch({ construction: !sm.construction }); setStatus(sm.construction ? 'drawing profile geometry' : 'drawing construction geometry'); }
}

/** Flip the selected entities between construction and profile geometry, as one commit. */
export async function toggleConstructionSelection(): Promise<boolean> {
  const sm = state.sketchMode;
  const f = sketchFeature();
  if (!sm || !f) return false;
  const names = [...new Set(sm.selection.map((r) => r.split('.')[0]))];
  const ents = names.map((n) => f.entities.find((e) => e.name === n)).filter((e): e is NonNullable<typeof e> => !!e && e.kind !== 'point' && e.kind !== 'project');
  if (!ents.length) { setStatus('select lines, circles, arcs, rects, slots or polygons'); return false; }
  const value = !ents.every((e) => e.construction);
  const ops: EditOp[] = ents.map((e) => ({ op: 'set_entity_argument', sketch: sm.sketch, entity: e.name, kwarg: 'construction', value }));
  return sketchBatch(ops, `${value ? 'construction' : 'profile'}: ${ents.map((e) => e.name).join(', ')}`);
}

export function toggleSketchSelect(ref: string | null, additive: boolean) {
  const sm = state.sketchMode;
  if (!sm) return;
  if (ref === null) { patchSketch({ selection: [], dimLock: null }); return; }
  if (!additive) { patchSketch({ selection: sm.selection.length === 1 && sm.selection[0] === ref ? [] : [ref], dimLock: null }); return; }
  const selection = sm.selection.includes(ref) ? sm.selection.filter((r) => r !== ref) : [...sm.selection, ref].slice(-3);
  patchSketch({ selection, dimLock: null });
}

export function setSketchHover(ref: string | null) {
  if (state.sketchMode && state.sketchMode.hover !== ref) patchSketch({ hover: ref });
}

export function setSketchHighlight(refs: string[]) {
  const cur = state.sketchMode?.highlight ?? [];
  if (state.sketchMode && (cur.length !== refs.length || cur.some((r, i) => r !== refs[i]))) patchSketch({ highlight: refs });
}

export function setDimEditing(name: string | null) { patchSketch({ dimEditing: name }); }
/** The dimension tool: on until stopped, it dimensions the selection (its picks) and stays on after each placement. */
export function startDimension() { patchSketch({ tool: 'dimension', dimLock: null, hover: null }); }
export function setDimLock(lock: DimLock) { patchSketch({ dimLock: lock }); }

/** Ask the radius (or setback) for these corners: the overlay shows a value box at `at`. */
export function askCorner(what: 'fillet' | 'chamfer', corners: Corner[], at: [number, number]) {
  patchSketch({ cornerAsk: { what, corners, at }, tool: null });
}

export function cancelCornerAsk() { patchSketch({ cornerAsk: null }); }

/** Fillet or chamfer corners at one size: the server composes the arcs, relations, sharps and dimension as one edit. */
export async function filletCorners(what: 'fillet' | 'chamfer', corners: Corner[], size: number): Promise<boolean> {
  const sm = state.sketchMode;
  if (!sm) return false;
  patchSketch({ cornerAsk: null });
  const ok = await edit({ op: 'fillet_corners', sketch: sm.sketch, corners: corners as unknown as Record<string, string>[], size, kind: what });
  if (ok) { setStatus(`${what === 'fillet' ? 'filleted' : 'chamfered'} ${corners.length} corner${corners.length === 1 ? '' : 's'} at ${size}`); patchSketch({ selection: [] }); }
  return ok;
}

/** Take a fillet or chamfer apart again. */
export async function unfillet(entity: string): Promise<boolean> {
  const sm = state.sketchMode;
  if (!sm) return false;
  const ok = await edit({ op: 'unfillet', sketch: sm.sketch, entity });
  if (ok) { setStatus(`removed ${entity}`); patchSketch({ selection: [] }); }
  return ok;
}

/** Move a dimension's label: the at= keyword on its statement, one undo step. */
export async function placeDimensionLabel(name: string, p: [number, number]) {
  const sm = state.sketchMode;
  if (!sm) return;
  const at = [Math.round(p[0] * 10) / 10, Math.round(p[1] * 10) / 10];
  if (await edit({ op: 'set_constraint_argument', sketch: sm.sketch, constraint: name, kwarg: 'at', value: at })) setStatus(`placed ${name}`);
}

export function sketchNames(): string[] {
  const f = featureByName(state.sketchMode?.sketch ?? null);
  return f ? takenNames(f) : [];
}

export function sketchFeature(): Feature | null { return featureByName(state.sketchMode?.sketch ?? null); }

/** Names an expression may use, for autocomplete. */
export function expressionNames(): string[] {
  const n = state.tree?.names;
  return n ? [...n.params, ...n.dimensions] : (state.tree?.params.map((p) => p.name) ?? []);
}

// ---- sketch edits ------------------------------------------------------------

export async function addConstraint(kind: string, refs: string[], options?: Record<string, JsonValue>, value?: number | { expr: string }): Promise<string | null> {
  const sm = state.sketchMode;
  if (!sm) return null;
  const name = nextName(CONSTRAINT_PREFIX[kind] ?? kind, sketchNames());
  const op: EditOp = { op: 'add_constraint', sketch: sm.sketch, kind, name, refs };
  if (options && Object.keys(options).length) op.options = options;
  if (value !== undefined) op.value = value;
  const ok = await edit(op);
  if (ok) { setStatus(`added ${kind} ${name}`); patchSketch({ selection: [], dimLock: null }); }  // the dimension tool stays on for the next one
  return ok ? name : null;
}

/** Sketch mode: a body edge or vertex clicked with a modifier joins the selection as converted
 * construction geometry (a project entity written on the spot, named after what it is). */
export async function useBodyInRelation(entity: PickedEntity, center: [number, number, number] | null): Promise<string | null> {
  const sm = state.sketchMode;
  const feature = sketchFeature();
  if (!sm || !feature) return null;
  if (entity.kind === 'face') { set({ error: 'relations take edges and vertices; convert a face with the convert tool' }); return null; }
  if (!feature.variable) { set({ error: 'the sketch must be assigned to a variable to convert body geometry' }); return null; }
  if (!center) return null;
  const target = selectorTarget(entity, center);
  if (!target || target.kind === 'plane') return null;
  const name = nextName(entity.kind, sketchNames());
  const ok = await edit({ op: 'add_sketch_entity', sketch: sm.sketch, kind: 'project', name, args: { selector: { expr: target.expr }, construction: true } });
  if (!ok) return null;
  toggleSketchSelect(name, true);
  setStatus(`converted ${entity.kind} ${entity.id} as ${name}: pick a relation`);
  return name;
}

export async function deleteConstraint(name: string) {
  const sm = state.sketchMode;
  const sketch = sm?.sketch ?? state.selected;
  if (!sketch) return;
  if (await edit({ op: 'delete_constraint', sketch, constraint: name })) setStatus(`deleted ${name}`);
}

const MATH_NAMES = new Set(['math', 'abs', 'min', 'max', 'round', 'pi', 'sqrt', 'sin', 'cos', 'tan', 'radians', 'degrees', 'True', 'False']);

/** Names in an expression that the document does not define. */
export function unknownNames(text: string): string[] {
  const known = new Set([...expressionNames(), ...MATH_NAMES]);
  const out = new Set<string>();
  for (const m of text.matchAll(/[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*/g)) {
    const name = m[0];
    if (known.has(name) || known.has(name.split('.')[0]) && name.startsWith('math.')) continue;
    out.add(name);
  }
  return [...out];
}

export function expressionValue(text: string): number | { expr: string } | null {
  const t = text.trim();
  if (!t) return null;
  if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
  const bad = unknownNames(t);
  if (bad.length) { set({ error: `unknown name${bad.length > 1 ? 's' : ''} in expression: ${bad.join(', ')}` }); return null; }
  return { expr: t };
}

export async function setConstraintValue(name: string, text: string) {
  const sm = state.sketchMode;
  const sketch = sm?.sketch ?? state.selected;
  if (!sketch) return;
  const t = text.trim();
  if (!t) return;
  const value = expressionValue(t);
  if (value === null) return;
  if (await edit({ op: 'set_constraint_value', sketch, constraint: name, value })) setStatus(`${name} = ${t}`);
  patchSketch({ dimEditing: null });
}

/** Several sketch ops as one commit and one undo step. */
export async function sketchBatch(ops: EditOp[], status: string): Promise<boolean> {
  const sm = state.sketchMode;
  if (!sm || !ops.length) return false;
  const ok = ops.length === 1 ? await edit(ops[0]) : await edit({ op: 'batch', sketch: sm.sketch, ops });
  if (ok) setStatus(status);
  return ok;
}

// ---- drag: solve previews at ~30 Hz, latest-wins, then one commit -----------------------

let dragBusy = false;
let dragQueued: [string, [number, number]] | null = null;
let dragAbort: AbortController | null = null;
let dragSeq = 0;

export function startDrag(ref: string) { patchSketch({ dragging: ref, locked: false, preview: null }); }

export function previewDrag(ref: string, p: [number, number]) {
  const sm = state.sketchMode;
  if (!sm || !state.docId) return;
  if (dragBusy) { dragQueued = [ref, p]; return; }
  dragBusy = true;
  const seq = ++dragSeq;
  const ctl = new AbortController();
  dragAbort = ctl;
  const id = state.docId, sketch = sm.sketch;
  const t0 = performance.now();
  api.solve(id, sketch, { [ref]: p }, ctl.signal).then((sol) => {
    if (seq !== dragSeq || !state.sketchMode || state.sketchMode.dragging !== ref) return;
    patchSketch({ preview: sol.coords, locked: !moved(sol, ref, p), solveMs: performance.now() - t0 });
  }).catch((e) => {
    if (!ctl.signal.aborted && seq === dragSeq) set({ error: (e as Error).message });
  }).finally(() => {
    if (seq !== dragSeq) return;
    dragBusy = false;
    if (dragQueued) { const q = dragQueued; dragQueued = null; previewDrag(q[0], q[1]); }
  });
}

/** Did the solution put the dragged point near where it was asked to go? */
function moved(sol: SketchSolution, ref: string, target: [number, number]): boolean {
  const [ent, part] = ref.split('.');
  const c = sol.coords[ent];
  if (!c) return false;
  if (part === 'rim') {  // a circle's curve: did the radius reach the cursor?
    const ctr = (c.at ?? c.center) as number[] | undefined;
    const r = c.radius != null ? Number(c.radius) : c.diameter != null ? Number(c.diameter) / 2 : null;
    if (!ctr || r === null) return true;
    return Math.abs(Math.hypot(ctr[0] - target[0], ctr[1] - target[1]) - r) < 1e-3;
  }
  const key = part === 'start' || part === 'end' || part === 'center' ? part : part === 'mid' ? null : part ? part : 'at';
  let pt: unknown = key ? c[key] : null;
  if (key === 'center' && !pt) pt = c.at;
  if (!Array.isArray(pt)) return true;  // derived points: assume the solver moved something
  return Math.hypot(Number(pt[0]) - target[0], Number(pt[1]) - target[1]) < 1e-3;
}

export function cancelDragPreview() {
  dragSeq++;
  dragAbort?.abort();
  dragAbort = null;
  dragQueued = null;
  dragBusy = false;
  patchSketch({ dragging: null, preview: null });
}

export async function endDrag(ref: string, p: [number, number]) {
  const sm = state.sketchMode;
  cancelDragPreview();
  if (!sm) return;
  const ok = await edit({ op: 'solve_sketch', sketch: sm.sketch, drag: { [ref]: p } });
  if (ok) setStatus(state.status === 'no change' ? `${ref} is fully constrained` : `moved ${ref}`);
}
