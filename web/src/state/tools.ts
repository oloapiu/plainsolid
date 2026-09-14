import { api } from '../api/client';
import { BODY_KINDS } from '../api/types';
import { isAssembly, set, state, withBusy } from './core';
import { docRelative, edit } from './documents';
import { cancelMeshFetch, fetchGhost, fetchMesh, plainMeshIsCurrent, statusFor } from './geometry';
import { Coalesced, RequestLane } from './requests';
import { cancelDragPreview, enterSketch, exitSketch, sketchBatch, toggleSketchSelect } from './sketch';
import { persistViews } from './views';
import type { MeshItem } from '../api/mesh';
import type {
  EditOp,
  EntityKind,
  Feature,
  Instance,
  JsonValue,
  MeasureRef,
  MeasureResult,
  PickedEntity,
  Pin,
  PoseMap,
  PreviewResult,
  SectionSpec,
} from '../api/types';
import type {
  DimKind,
  DrawingTool,
  FeatureDialogKind,
  MenuEntry,
  MoveMode,
  PickRequest,
  PickTarget,
  PlaneForm,
  PosePreview,
  State,
  Tool,
} from './core';

export function cancelDocumentTools() {
  featurePreviews.cancel();
  matePreviews.cancel();
  refSeq++;
  dragState = null;
  cancelDragPreview();
}

// ---- selection and view -------------------------------------------------------

export function select(name: string | null, face: number | null = null) {
  set({ selected: name, selectedFace: face, selectedEdge: null, selectedItem: null });
}

export function selectItem(path: string | null) {
  set({ selectedItem: path, selected: null, selectedFace: null, selectedEdge: null });
}

/** A clicked edge: selected on its own, with its owning feature (or instance) selected in the tree. */
export function selectEdge(edge: number | null) {
  if (edge === null) { set({ selectedEdge: null }); return; }
  const label = state.mesh?.header.edge_labels[String(edge)] ?? null;
  if (isAssembly(state.tree)) {
    const item = state.mesh?.header.items.find((it) => edge >= it.edges[0] && edge < it.edges[0] + it.edges[1]);
    const inst = item ? instanceFeatureOf(item.path) : null;
    set({ selectedEdge: edge, selectedFace: null, selected: inst ? inst.name : state.selected, selectedItem: inst ? null : (item?.path ?? state.selectedItem) });
    return;
  }
  set({ selectedEdge: edge, selectedFace: null, selected: label && state.tree?.features.some((f) => f.name === label) ? label : state.selected });
}

export function selectFace(face: number | null) {
  if (face === null) { set({ selectedFace: null }); return; }
  const label = state.mesh?.header.face_labels[String(face)] ?? null;
  if (isAssembly(state.tree)) {
    const item = state.mesh?.header.items.find((it) => face >= it.faces[0] && face < it.faces[0] + it.faces[1]);
    const inst = item ? instanceFeatureOf(item.path) : null;
    // a face of an instance feature selects the feature; a face of a review import selects its node
    set({ selectedFace: face, selectedItem: inst ? null : (item?.path ?? label), selected: inst?.name ?? null });
    return;
  }
  set({ selectedFace: face, selectedEdge: null, selected: label && state.tree?.features.some((f) => f.name === label) ? label : state.selected });
}

export function pick(entity: PickedEntity | null, center: [number, number, number] | null = null) {
  const req = state.pickRequest;
  if (req) {
    if (entity && req.kinds.includes(entity.kind) && center) {
      const t = selectorTarget(entity, center);
      if (t) { req.onPick(t); return; }
    }
    if (entity) set({ error: `pick ${req.kinds.join(' or ')}${req.planes ? ' or a plane' : ''}` });
    return;
  }
  if (state.tool === 'measure') {
    if (entity) measurePick({ [entity.kind]: entity.id } as MeasureRef);
    return;
  }
  if (entity?.kind === 'edge') { selectEdge(entity.id); return; }
  if (entity?.kind === 'vertex') return;
  selectFace(entity ? entity.id : null);
  if (!entity) set({ selectedEdge: null });
}

/** A click on a displayed plane square: feeds a waiting dialog, else selects the plane feature. */
export function planePicked(name: string) {
  const req = state.pickRequest;
  const standard = name === 'XY' || name === 'XZ' || name === 'YZ';
  if (req) {
    if (!req.planes) { set({ error: `pick ${req.kinds.join(' or ')}` }); return; }
    const f = standard ? null : featureByName(name);
    const expr = standard ? name : (f?.variable ?? name);
    req.onPick({ kind: 'plane', name, expr, label: standard ? `${name} plane` : `plane ${name}`, standard });
    return;
  }
  if (!standard) select(name);
}

export function requestPick(req: PickRequest | null) {
  set({ pickRequest: req, hover: null, ...(req ? { showPlanes: req.planes ? true : state.showPlanes } : {}) });
}

export function setShowPlanes(on: boolean) { set({ showPlanes: on }); }
export function setOrtho(on: boolean) { set({ ortho: on }); }

export function openPlaneDialog(form: PlaneForm | null) {
  set({ planeDialog: form, featureDialog: null, dialogPicks: [], pickRequest: null, showPlanes: form ? true : state.showPlanes, tool: 'none' });
}

/** Open (or close, with null) the dialog that adds a fillet, chamfer, shell, revolve, pattern or mirror. */
export function openFeatureDialog(kind: FeatureDialogKind | null, target: string | null = null) {
  clearFeaturePreview();
  set({ featureDialog: kind ? { kind, target } : null, planeDialog: null, dialogPicks: [], pickRequest: null, tool: 'none' });
}

export function setDialogPicks(picks: PickedEntity[]) { set({ dialogPicks: picks }); }

/** The last body feature bound to a variable: what a geometric selector refers to. */
export function bodyOwner(): Feature | null {
  const features = state.tree?.features ?? [];
  return [...features].reverse().find((f) => BODY_KINDS.includes(f.kind) && f.variable) ?? null;
}

const tagOp = (t: string) => (t.startsWith(':') ? `.${t.slice(1)}` : `.from_sketch("${t}")`);
export const tagText = (t: string) => (t.startsWith(':') ? t.slice(1) : t);

const within = (id: number, range: [number, number]) => id >= range[0] && id < range[0] + range[1];

/** The mesh item (assembly instance or review node) an entity belongs to. */
export function itemOf(entity: PickedEntity): MeshItem | null {
  const items = state.mesh?.header.items ?? [];
  return items.find((it) => within(entity.id, entity.kind === 'face' ? it.faces : entity.kind === 'edge' ? it.edges : it.vertices)) ?? null;
}

/** The instance feature a mesh item path belongs to (its first segment), if any. */
export function instanceFeatureOf(path: string): Feature | null {
  const f = featureByName(path.split('.')[0]);
  return f?.kind === 'instance' ? f : null;
}

/** A world point in an instance's own coordinates, through the inverse of its pose. */
export function toInstanceLocal(inst: Feature, p: [number, number, number]): [number, number, number] {
  const m = state.tree?.evaluation.assembly?.poses[inst.name]?.transform;
  if (!m) return p;
  const d = [p[0] - m[0][3], p[1] - m[1][3], p[2] - m[2][3]];
  return [0, 1, 2].map((c) => m[0][c] * d[0] + m[1][c] * d[1] + m[2][c] * d[2]) as [number, number, number];
}

/** The smallest set of identity-map tags that picks exactly this entity among its owner's
 * (within one mesh item, so two instances of the same part do not confuse each other), or null. */
function uniqueTags(kind: EntityKind, id: number, owner: string, item: MeshItem | null = null): string[] | null {
  const h = state.mesh?.header;
  if (!h || kind === 'vertex') return null;
  const labels = kind === 'face' ? h.face_labels : h.edge_labels;
  const allTags = (kind === 'face' ? h.face_tags : h.edge_tags) ?? {};
  const mine = allTags[String(id)] ?? [];
  if (!mine.length) return null;
  const range = item ? (kind === 'face' ? item.faces : item.edges) : null;
  const peers = Object.keys(labels).filter((k) => labels[k] === owner && (!range || within(Number(k), range))).map((k) => allTags[k] ?? []);
  const unique = (subset: string[]) => peers.filter((tags) => subset.every((t) => tags.includes(t))).length === 1;
  for (const t of mine) if (unique([t])) return [t];
  for (let i = 0; i < mine.length; i++) for (let j = i + 1; j < mine.length; j++) if (unique([mine[i], mine[j]])) return [mine[i], mine[j]];
  return unique(mine) ? mine : null;
}

/** The tags the identity map gave an entity, for labels. */
export function entityTags(entity: PickedEntity): string[] {
  const h = state.mesh?.header;
  if (!h) return [];
  return ((entity.kind === 'face' ? h.face_tags : entity.kind === 'edge' ? h.edge_tags : {}) ?? {})[String(entity.id)] ?? [];
}

/** A selector expression for a picked body entity: semantic when the identity map makes it
 * unique (body.faces.top, boss.edges.top.from_sketch("c")), else geometric (body.faces.nearest((2, -20, 4))). */
export function selectorTarget(entity: PickedEntity, center: [number, number, number]): PickTarget | null {
  const h = state.mesh?.header;
  const kind = entity.kind === 'face' ? 'faces' : entity.kind === 'edge' ? 'edges' : 'vertices';
  if (isAssembly(state.tree)) return instanceTarget(entity, center, kind);
  const ownerName = h ? (entity.kind === 'face' ? h.face_labels[String(entity.id)] : entity.kind === 'edge' ? h.edge_labels[String(entity.id)] : '') : '';
  const ownerFeature = featureByName(ownerName || null);
  if (ownerFeature?.variable) {
    const tags = uniqueTags(entity.kind, entity.id, ownerName);
    if (tags) {
      return { kind: entity.kind, id: entity.id, center, expr: `${ownerFeature.variable}.${kind}${tags.map(tagOp).join('')}`,
               label: `${ownerName} ${tags.map(tagText).join(' ')}`, owner: ownerName };
    }
  }
  const owner = bodyOwner();
  if (!owner?.variable) { set({ error: 'no body feature with a variable to refer to' }); return null; }
  const xyz = center.map((c) => fmtCoord(c)).join(', ');
  return { kind: entity.kind, id: entity.id, center, expr: `${owner.variable}.${kind}.nearest((${xyz}))`,
           label: `${entity.kind} ${entity.id} of ${owner.name}`, owner: owner.name };
}

/** Assemblies: a reference on an instance in the part's own coordinates. Semantic through the
 * part's identity map when the tags are unique within that instance (`lid.faces.of("plate").bottom`),
 * else by the nearest point in local coordinates. Vendor STEP instances carry no labels. */
function instanceTarget(entity: PickedEntity, center: [number, number, number], kind: string): PickTarget | null {
  const h = state.mesh?.header;
  const item = itemOf(entity);
  const inst = item ? instanceFeatureOf(item.path) : null;
  if (!h || !item || !inst) { set({ error: 'pick a face, edge or vertex of an instance' }); return null; }
  if (!inst.variable) { set({ error: `assign instance ${inst.name} to a variable to refer to it` }); return null; }
  const ownerName = entity.kind === 'face' ? h.face_labels[String(entity.id)] : entity.kind === 'edge' ? h.edge_labels[String(entity.id)] : '';
  if (ownerName && ownerName !== item.name) {
    const tags = uniqueTags(entity.kind, entity.id, ownerName, item);
    if (tags) {
      return { kind: entity.kind, id: entity.id, center, expr: `${inst.variable}.${kind}.of("${ownerName}")${tags.map(tagOp).join('')}`,
               label: `${inst.name} ${ownerName} ${tags.map(tagText).join(' ')}`, owner: inst.name };
    }
  }
  const xyz = toInstanceLocal(inst, center).map((c) => fmtCoord(c)).join(', ');
  return { kind: entity.kind, id: entity.id, center, expr: `${inst.variable}.${kind}.nearest((${xyz}))`,
           label: `${entity.kind} ${entity.id} of ${inst.name}`, owner: inst.name };
}

const PREFIX: Record<string, string> = { linear_pattern: 'pattern', circular_pattern: 'pattern', dimension: 'dim', note: 'note' };

/** Add a feature composed by the server from `args` (selector expressions as {expr}) at the end of the file. */
/** The add_feature op a dialog would write, named the way addFeature names it. */
export function featureOp(kind: string, args: Record<string, JsonValue>, after?: string, wanted?: string): Extract<EditOp, { op: 'add_feature' }> | null {
  const tree = state.tree;
  if (!tree) return null;
  const taken = tree.features.map((f) => f.name);
  const name = wanted && !taken.includes(wanted) ? wanted : nextName(wanted ?? PREFIX[kind] ?? kind, taken);
  return { op: 'add_feature', kind, name, args, ...(after ? { after } : {}) };
}

export async function addFeature(kind: string, args: Record<string, JsonValue>, after?: string, wanted?: string): Promise<string | null> {
  const op = featureOp(kind, args, after, wanted);
  if (!op) return null;
  const name = String(op.name);
  clearFeaturePreview();
  const ok = await edit(op);
  if (!ok) return null;
  set({ featureDialog: null, dialogPicks: [], pickRequest: null });
  // a feature made from the sketch being edited closes the sketch: the feature is the thing now
  if (state.sketchMode && (kind === 'extrude' || kind === 'cut' || kind === 'revolve')) exitSketch();
  select(name);
  const f = featureByName(name);
  setStatus(f?.result?.error ? `added ${name}: ${f.result.error.message}` : `added ${name}`);
  return name;
}

/** Show what a feature would make: the server meshes the document with the edit applied, nothing
 * written, and the result draws over the current body, which fades back. Changes faster than the
 * server answers coalesce: one request in flight, only the newest change waits its turn. */
export const featurePreviews = new Coalesced(previewFeatureNow, false);
export function previewFeature(op: EditOp): Promise<boolean> { return featurePreviews.call(op); }
async function previewFeatureNow(lane: RequestLane, op: EditOp): Promise<boolean> {
  const id = state.docId;
  if (!id) return false;
  const request = lane.start(true);
  try {
    const m = await withBusy(() => api.previewMesh(id, op, request.signal));
    if (!request.current()) return false;
    const h = m.header;
    const note = h.ok === false ? (h.error ?? 'the feature fails') : `preview: ${h.faces ?? 0} faces`;
    set({ ghostMesh: h.ok === false ? null : m, ghostStyle: 'preview', previewNote: note });
    return h.ok !== false;
  } catch (e) {
    if (request.current()) set({ previewNote: (e as Error).message });
    return false;
  }
}

/** Back to no preview: in sketch mode the faint finished body returns. */
export function clearFeaturePreview() {
  featurePreviews.cancel();
  if (state.ghostStyle === 'preview' || state.previewNote) {
    set({ ghostMesh: null, ghostStyle: 'ghost', previewNote: null });
    if (state.sketchMode) void fetchGhost();
  }
}

/** Set one field of the meta() line: a string, a number, or null to drop it. */
export async function setMeta(key: string, value: string | number | null): Promise<boolean> {
  return edit({ op: 'set_meta', key, value });
}

export async function setSuppressed(name: string, on: boolean): Promise<boolean> {
  return edit({ op: 'set_argument', feature: name, kwarg: 'suppressed', value: on });
}

// ---- assemblies -----------------------------------------------------------------

/** Instance features of the current assembly that a mate can refer to. */
export function instanceFeatures(): Feature[] {
  return (state.tree?.features ?? []).filter((f) => f.kind === 'instance' && !!f.variable);
}

/** Add an instance of a part or STEP file, named after the file. */
export async function addInstance(path: string, color?: string, material?: string): Promise<string | null> {
  path = docRelative(path);
  const stem = (path.split('/').pop() ?? 'part').replace(/\.(py|step|stp)$/i, '').replace(/[^A-Za-z0-9_]/g, '_') || 'part';
  const args: Record<string, JsonValue> = { path };
  if (color) args.color = color;
  if (material) args.material = material;
  return addFeature('instance', args, undefined, /^[0-9]/.test(stem) ? `p_${stem}` : stem);
}

/** Add a mate between two references (selector expressions on instances). */
export function mateArgs(a: string, b: string, value?: number | { expr: string }, flip = false): Record<string, JsonValue> {
  const args: Record<string, JsonValue> = { a: { expr: a }, b: { expr: b } };
  if (value !== undefined) args.value = value;
  if (flip) args.flip = true;
  return args;
}

export async function addMate(kind: string, a: string, b: string, value?: number | { expr: string }, flip = false): Promise<string | null> {
  return addFeature(kind, mateArgs(a, b, value, flip));
}

// ---- previews: instances drawn at other poses without a new mesh ---------------------------

/** The mesh's current poses (the file's), by instance name. */
function currentTransforms(): Record<string, number[][]> {
  const out: Record<string, number[][]> = {};
  for (const [name, p] of Object.entries(state.tree?.evaluation.assembly?.poses ?? {})) out[name] = p.transform;
  return out;
}

function previewFrom(poses: PoseMap): PosePreview {
  const cur = currentTransforms();
  const out: PosePreview = {};
  for (const [name, p] of Object.entries(poses)) if (cur[name]) out[name] = { from: cur[name], to: p.transform };
  return out;
}

const matePreviews = new Coalesced(previewMateNow, null);
/** Show the assembly as it would be with a mate added (or any edit), nothing written. With
 * chooseFlip the server tries both orientations and answers which flip turns the parts less.
 * Coalesced like feature previews: one in flight, the newest change waits. */
export function previewMate(kind: string, a: string, b: string, value?: number | { expr: string }, flip = false, chooseFlip = false): Promise<PreviewResult | null> {
  return matePreviews.call(kind, a, b, value, flip, chooseFlip);
}
async function previewMateNow(lane: RequestLane, kind: string, a: string, b: string, value: number | { expr: string } | undefined, flip: boolean, chooseFlip: boolean): Promise<PreviewResult | null> {
  const id = state.docId;
  const tree = state.tree;
  if (!id || !tree) return null;
  const request = lane.start(true);
  const name = nextName(kind, tree.features.map((f) => f.name));
  try {
    const r = await withBusy(() => api.preview(id, { op: 'add_feature', kind, name, args: mateArgs(a, b, value, flip) }, chooseFlip, request.signal));
    if (!request.current()) return null;
    const res = r.results?.[name];
    const conflict = r.conflicting?.includes(name);
    const note = !r.ok ? (res?.error?.message ?? r.error ?? r.errors?.[0]?.message ?? 'the mate fails')
      : conflict ? `not satisfied: conflicts with ${(r.conflicting ?? []).filter((n) => n !== name).join(', ') || 'the other mates'}`
      : r.assembly ? (r.assembly.dof === 0 ? 'preview: fully constrained' : `preview: ${r.assembly.free.length} instance${r.assembly.free.length === 1 ? '' : 's'} still free`) : 'preview';
    set({ posePreview: r.ok ? previewFrom(r.poses) : null, previewNote: note });
    return r;
  } catch (e) {
    if (request.current()) set({ posePreview: null, previewNote: (e as Error).message });
    return null;
  }
}

export function clearPosePreview() {
  matePreviews.cancel();
  if (state.posePreview || state.previewNote) set({ posePreview: null, previewNote: null });
}

// ---- the move tool: drag an instance along the motions its mates leave free -------------------

export function setMoveMode(mode: MoveMode) { set({ moveMode: mode, tool: 'move', status: mode === 'translate' ? 'move: drag a part to slide it' : 'move: drag a part to turn it' }); }

interface DragState { name: string; poses: Record<string, { at: number[]; rotate: number[] }> | null; pending: { t: number[]; r: number[] }; inflight: boolean; moved: boolean; last: PoseMap | null }
export let dragState: DragState | null = null;

/** A left press on an instance with the move tool: take the drag when it is an instance feature. */
export function beginInstanceDrag(entity: PickedEntity): boolean {
  const item = itemOf(entity);
  const inst = item ? instanceFeatureOf(item.path) : null;
  if (!inst || !state.tree?.evaluation.assembly?.poses[inst.name]) return false;
  dragState = { name: inst.name, poses: null, pending: { t: [0, 0, 0], r: [0, 0, 0] }, inflight: false, moved: false, last: null };
  select(inst.name);
  return true;
}

/** Screen motion becomes a translation in the camera plane or a rotation about the screen axes;
 * the server projects it onto the free motions. Steps are chained: each answer is the next start. */
export function moveInstanceDrag(dx: number, dy: number, axes: { right: number[]; up: number[]; worldPerPixel: number }) {
  const d = dragState;
  if (!d) return;
  if (state.moveMode === 'translate') {
    const k = axes.worldPerPixel;
    for (let i = 0; i < 3; i++) d.pending.t[i] += axes.right[i] * dx * k - axes.up[i] * dy * k;
  } else {
    const k = 0.01;
    for (let i = 0; i < 3; i++) d.pending.r[i] += axes.up[i] * dx * k + axes.right[i] * dy * k;
  }
  void flushDrag();
}

async function flushDrag() {
  const d = dragState;
  const id = state.docId;
  if (!d || d.inflight || !id) return;
  const t = d.pending.t, r = d.pending.r;
  if (Math.hypot(...t) < 1e-9 && Math.hypot(...r) < 1e-9) return;
  d.pending = { t: [0, 0, 0], r: [0, 0, 0] };
  d.inflight = true;
  try {
    const res = await api.drag(id, d.name, d.poses, state.moveMode === 'translate' ? t : null, state.moveMode === 'rotate' ? r : null);
    if (dragState !== d) return;
    if (res.moved) {
      d.moved = true;
      d.last = res.poses;
      d.poses = Object.fromEntries(Object.entries(res.poses).map(([n, p]) => [n, { at: p.at, rotate: p.rotate }]));
      set({ posePreview: previewFrom(res.poses), previewNote: res.conflicting?.length ? `not satisfied: ${res.conflicting.join(', ')}` : null, status: `moving ${d.name}` });
    } else if (res.reason) setStatus(res.reason);
  } catch (e) {
    if (dragState === d) setStatus((e as Error).message);
  } finally {
    if (dragState === d) { d.inflight = false; void flushDrag(); }
  }
}

/** Release: the previewed poses are written into the instance statements, or nothing changed. */
export async function endInstanceDrag() {
  const d = dragState;
  dragState = null;
  if (!d) return;
  if (!d.moved || !d.last) { clearPosePreview(); return; }
  const cur = state.tree?.evaluation.assembly?.poses ?? {};
  const poses: Record<string, { at: number[]; rotate: number[] }> = {};
  for (const [name, p] of Object.entries(d.last)) {
    const c = cur[name];
    if (!c || p.at.some((v, i) => Math.abs(v - c.at[i]) > 1e-6) || p.rotate.some((v, i) => Math.abs(v - c.rotate[i]) > 1e-6)) poses[name] = { at: p.at, rotate: p.rotate };
  }
  if (!Object.keys(poses).length) { clearPosePreview(); return; }
  const ok = await edit({ op: 'write_poses', poses });
  if (!ok) clearPosePreview();
  else setStatus(`moved ${d.name}`);
}

/** Anchor an instance where it is. */
export async function fixInstance(name: string): Promise<string | null> {
  const f = featureByName(name);
  if (!f?.variable) { set({ error: `assign instance ${name} to a variable to fix it` }); return null; }
  return addFeature('fixed', { instance: { expr: f.variable } }, undefined, `fix_${name}`);
}

const fmtCoord = (v: number) => { const r = Math.round(v * 1000) / 1000; return String(Object.is(r, -0) ? 0 : r); };

// ---- drawings -------------------------------------------------------------------

const round1 = (v: number) => { const r = Math.round(v * 10) / 10; return Object.is(r, -0) ? 0 : r; };

export function setDrawingTool(tool: DrawingTool) {
  set({ drawingTool: tool, drawingPicks: [], error: null, status: tool === 'dimension' ? 'dimension: pick an edge on the sheet' : tool === 'note' ? 'note: click where the text goes' : state.status });
}

export function setDimKind(kind: DimKind) { set({ dimKind: kind }); }

/** An edge picked on the sheet: toggles it in the list; the third pick replaces the second. */
export function drawingPick(ref: string, view: string, round: boolean) {
  const picks = state.drawingPicks;
  if (picks.some((p) => p.ref === ref)) { set({ drawingPicks: picks.filter((p) => p.ref !== ref) }); return; }
  if (picks.length && picks[0].view !== view) { setError('both edges of a dimension must be in the same view'); return; }
  const next = picks.length >= 2 ? [picks[0], { ref, view, round }] : [...picks, { ref, view, round }];
  set({ drawingPicks: next, error: null, status: next.length === 1
    ? (round ? 'click to place a diameter, or pick a second edge for a distance' : 'pick a second edge, or click to place if the kind is set')
    : 'click where the dimension goes' });
}

/** What the picks so far would measure, or why they cannot. */
export function dimensionPlan(): { kind: string; ok: true } | { ok: false; why: string } {
  const picks = state.drawingPicks;
  const choice = state.dimKind;
  if (!picks.length) return { ok: false, why: 'pick an edge first' };
  const kind = choice !== 'auto' ? choice : picks.length === 1 ? (picks[0].round ? 'diameter' : '') : 'distance';
  if (!kind) return { ok: false, why: 'pick a second edge for a distance, or a round edge for a diameter' };
  if ((kind === 'distance' || kind === 'angle') && picks.length < 2) return { ok: false, why: `a ${kind} needs two edges` };
  return { kind, ok: true };
}

/** Write a dimension from the picks, placed at a sheet point (stored relative to its view's centre). */
export async function placeDimension(at: [number, number]): Promise<string | null> {
  const plan = dimensionPlan();
  if (!plan.ok) { setError(plan.why); return null; }
  const picks = state.drawingPicks;
  const view = state.tree?.evaluation.drawing?.views.find((v) => v.name === picks[0].view);
  if (!view) { setError(`view ${picks[0].view} is not on the sheet`); return null; }
  const two = picks.length > 1 && plan.kind !== 'diameter' && plan.kind !== 'radius';
  const args: Record<string, JsonValue> = {
    a: { expr: picks[0].ref }, ...(two ? { b: { expr: picks[1].ref } } : {}),
    at: [round1(at[0] - view.at[0]), round1(at[1] - view.at[1])], ...(state.dimKind !== 'auto' ? { kind: plan.kind } : {}),
  };
  const name = await addFeature('dimension', args);
  set({ drawingPicks: [], drawingTool: 'dimension' });
  return name;
}

/** A note at a sheet point; the text comes from the caller or a prompt. */
export async function addNoteAt(at: [number, number], text?: string): Promise<string | null> {
  const t = (text ?? window.prompt('Note text:', '') ?? '').trim();
  if (!t) return null;
  const name = await addFeature('note', { text: t, at: [round1(at[0]), round1(at[1])] });
  set({ drawingTool: null });
  return name;
}

/** Move a view, a dimension or a note by a sheet offset: its `at` is rewritten. */
export async function moveDrawingItem(name: string, delta: [number, number]): Promise<boolean> {
  const f = featureByName(name);
  if (!f) return false;
  const at = (f.args.at as number[] | undefined) ?? [0, 0];
  return edit({ op: 'set_argument', feature: name, kwarg: 'at', value: { expr: `(${round1(at[0] + delta[0])}, ${round1(at[1] + delta[1])})` } });
}

/** A new view named after its direction (or "section"), placed at a free spot on the sheet. */
export async function addView(direction: string, section?: { plane: string; offset: number }, at?: [number, number]): Promise<string | null> {
  const scene = state.tree?.evaluation.drawing;
  if (!scene) return null;
  let spot = at;
  if (!spot) {
    const boxes = scene.views.map((v) => v.bbox);
    const right = boxes.length ? Math.max(...boxes.map((b) => b[2])) : 20;
    spot = right + 45 < scene.sheet.width - 30 ? [right + 45, scene.sheet.height * 0.62] : [scene.sheet.width * 0.3, scene.sheet.height * 0.3];
    spot = [round1(spot[0]), round1(spot[1])];
  }
  const args: Record<string, JsonValue> = section ? { section: section.plane, offset: section.offset, at: spot } : { direction, at: spot };
  return addFeature('view', args, undefined, section ? 'section' : direction);
}

/** The drawing's model as a project-relative path, for opening it in a tab. */
export function modelDocPath(): string | null {
  const rel = state.tree?.meta.of;
  const docPath = state.tree?.path;
  const root = state.filesRoot;
  if (typeof rel !== 'string' || !docPath || !root) return null;
  const rootParts = root.replace(/\/$/, '').split('/');
  const docParts = docPath.split('/');
  if (docParts.slice(0, rootParts.length).join('/') !== rootParts.join('/')) return null;
  const parts = docParts.slice(rootParts.length, -1);
  for (const seg of rel.split('/')) {
    if (seg === '..') parts.pop(); else if (seg && seg !== '.') parts.push(seg);
  }
  return parts.join('/');
}

/** Add a sketch on a standard plane, a plane feature, or a body face, then enter it. */
export async function addSketchOn(target: PickTarget | 'XY' | 'XZ' | 'YZ'): Promise<string | null> {
  const tree = state.tree;
  if (!tree) return null;
  const name = nextName('sketch', tree.features.map((f) => f.name));
  const on = typeof target === 'string' ? target : target.kind === 'plane' ? (target.standard ? target.name : { expr: target.expr }) : { expr: target.expr };
  const ok = await edit({ op: 'add_feature', kind: 'sketch', name, args: { on } });
  if (!ok) return null;
  set({ pickRequest: null, selectedFace: null });
  const f = featureByName(name);
  if (f) { if (f.result?.plane) enterSketch(f); else select(name); }
  setStatus(`added ${name}`);
  return name;
}

/** Add a plane feature from the dialog's arguments (selector expressions already composed). */
export async function addPlane(args: Record<string, JsonValue>): Promise<string | null> {
  const tree = state.tree;
  if (!tree) return null;
  const name = nextName('plane', tree.features.map((f) => f.name));
  const ok = await edit({ op: 'add_feature', kind: 'plane', name, args });
  if (!ok) return null;
  set({ planeDialog: null, pickRequest: null, showPlanes: true });
  select(name);
  setStatus(`added ${name}`);
  return name;
}

/** Delete a feature; the server cascades to its entities and dependants. */
export async function deleteFeature(name: string): Promise<boolean> {
  const dep = featureByName(name)?.dependents;
  const ok = await edit({ op: 'delete_feature', feature: name });
  if (ok) {
    const extra = dep && (dep.features.length || dep.entities) ? ` with ${[dep.features.length ? dep.features.join(', ') : '', dep.entities ? `${dep.entities} sketch entit${dep.entities === 1 ? 'y' : 'ies'}` : ''].filter(Boolean).join(' and ')}` : '';
    setStatus(`deleted ${name}${extra}`);
    if (state.selected === name || (dep && dep.features.includes(state.selected ?? ''))) select(null);
  }
  return ok;
}

/** The Delete key or a menu entry: delete at once, or ask first when the server would cascade. */
export function requestDelete(name: string | null = state.selected) {
  const f = featureByName(name);
  if (!f || f.read_only) return;
  const dep = f.dependents;
  if (dep && (dep.features.length || dep.entities)) { set({ deleteConfirm: f.name, selected: f.name }); return; }
  void deleteFeature(f.name);
}

export function setDeleteConfirm(name: string | null) { if (state.deleteConfirm !== name) set({ deleteConfirm: name }); }

/** Open the right-click menu at a screen position; a menu with no real entry does not open. */
export function openContextMenu(x: number, y: number, entries: MenuEntry[], title?: string) {
  const real = entries.filter((e, i, all) => !e.sep || (i > 0 && i < all.length - 1 && !all[i - 1].sep));
  if (!real.some((e) => !e.sep)) return;
  set({ contextMenu: { x, y, title, entries: real } });
}

export function closeContextMenu() { if (state.contextMenu) set({ contextMenu: null }); }

/** Ask the app to show the code pane ("go to code"). */
export function revealCode() { set({ codeReveal: state.codeReveal + 1 }); }

/** Sketch mode: delete the selected entities as one commit. */
export async function deleteSketchSelection(): Promise<boolean> {
  const sm = state.sketchMode;
  if (!sm || !sm.selection.length) return false;
  const names = [...new Set(sm.selection.map((r) => r.split('.')[0]))];
  const ops: EditOp[] = names.map((n) => ({ op: 'delete_sketch_entity', sketch: sm.sketch, entity: n }));
  const ok = await sketchBatch(ops, `deleted ${names.join(', ')}`);
  if (ok) toggleSketchSelect(null, false);
  return ok;
}

/** The tree row under the mouse (a feature name, or an instance path in a STEP viewer's tree). */
export function setTreeHover(hover: { feature?: string; item?: string } | null) {
  const cur = state.treeHover;
  if ((cur?.feature ?? null) !== (hover?.feature ?? null) || (cur?.item ?? null) !== (hover?.item ?? null)) set({ treeHover: hover });
}

// ---- a reference under the mouse: the server says which mesh ids it picks ------------------

let refSeq = 0;
let refTimer: ReturnType<typeof setTimeout> | null = null;
const refCache = new Map<string, PickedEntity[]>();

/** Light what a selector (or feature name) shown in the panel refers to; null clears. Debounced,
 * latest-wins, remembered per document state so a second hover costs nothing. A reference the
 * finished body no longer has (a fillet's edge became a face) falls back to lighting the feature
 * that holds it, so hovering a fillet's edges shows the fillet. */
export function hoverRefs(refs: string[] | null, fallback: string | null = null) {
  refSeq++;
  if (refTimer) { clearTimeout(refTimer); refTimer = null; }
  const id = state.docId;
  const exprs = (refs ?? []).map((r) => r.trim()).filter(Boolean);
  if (!id || !exprs.length || state.sketchMode) { if (state.refHighlight.length) set({ refHighlight: [] }); return; }
  const key = `${id}|${state.revision}|${state.upto ?? ''}|${exprs.join('\u0000')}|${fallback ?? ''}`;
  const cached = refCache.get(key);
  if (cached) { set({ refHighlight: cached }); return; }
  const seq = refSeq, revision = state.revision, upto = state.upto;
  const toEntities = (ids: { faces: number[]; edges: number[]; vertices: number[] }): PickedEntity[] => [
    ...ids.faces.map((i) => ({ kind: 'face' as const, id: i })), ...ids.edges.map((i) => ({ kind: 'edge' as const, id: i })), ...ids.vertices.map((i) => ({ kind: 'vertex' as const, id: i })),
  ];
  refTimer = setTimeout(async () => {
    refTimer = null;
    let ents: PickedEntity[] = [];
    try { ents = toEntities(await api.locate(id, exprs, upto)); } catch { /* nothing on the finished body */ }
    if (!ents.length && fallback) {
      try { ents = toEntities(await api.locate(id, [fallback], upto)); } catch { /* then nothing lights */ }
    }
    if (seq !== refSeq || state.docId !== id || state.revision !== revision || state.upto !== upto) return;
    if (refCache.size > 200) refCache.clear();
    refCache.set(key, ents);
    if (seq === refSeq && state.docId === id) set({ refHighlight: ents });
  }, 120);
}

export function hoverEntity(entity: PickedEntity | null) {
  const h = state.hover;
  if ((h?.kind ?? null) !== (entity?.kind ?? null) || (h?.id ?? null) !== (entity?.id ?? null)) set({ hover: entity });
}

export async function setUpto(name: string | null) {
  if (!state.docId) return;
  set({ upto: name });
  await fetchMesh(name ? `rolled back to ${name}` : undefined);
}

export function setStatus(status: string) { set({ status }); }
export function setError(error: string | null) { set({ error }); }

// ---- tools: section and measure ------------------------------------------------

export function setTool(tool: Tool) {
  const next = state.tool === tool ? 'none' : tool;
  if (next !== 'move') { dragState = null; if (state.tool === 'move') clearPosePreview(); }
  set({ tool: next, hover: null, measure: next === 'measure' ? state.measure : { picks: [], result: null, pending: false } });
}

export async function setSection(section: SectionSpec | null, persist = true) {
  const measure = { picks: [], result: null, pending: false };
  if (!section) {
    // off: drop any in-flight request and show the uncut mesh at once when we have it
    cancelMeshFetch();
    set({ section: null, previewOffset: null, sectionPending: false, measure });
    if (plainMeshIsCurrent()) set({ mesh: state.plainMesh, status: state.tree ? statusFor(state.tree) : state.status });
    else await fetchMesh();
  } else {
    set({ section, previewOffset: null, measure });
    await fetchMesh(`section ${section.plane} at ${section.offset}`);
  }
  if (persist) persistViews();
}

export function setPreviewOffset(offset: number | null) {
  // previews clip the uncut mesh, so material on either side of the last cut shows correctly
  const patch: Partial<State> = { previewOffset: offset };
  if (offset !== null && plainMeshIsCurrent() && state.mesh !== state.plainMesh) patch.mesh = state.plainMesh;
  set(patch);
}

/** Every leaf path of the assembly: instances, or the bodies of a review import. */
export function allLeafPaths(): string[] { return (state.tree?.evaluation.instances ?? []).flatMap(leafPaths); }

/** Show only these paths (alt+click on a visibility checkbox, or "isolate" in a menu). */
export function isolate(paths: string[]) {
  const keep = new Set(paths);
  const visibility: Record<string, boolean> = {};
  for (const p of allLeafPaths()) visibility[p] = keep.has(p);
  set({ visibility, status: `showing only ${paths.length === 1 ? paths[0] : `${paths.length} bodies`}` });
  persistViews();
}

export function showAll() { set({ visibility: {}, status: 'showing everything' }); persistViews(); }

export function setVisibility(path: string, visible: boolean) {
  set({ visibility: { ...state.visibility, [path]: visible } });
  persistViews();
}

export function setTransparency(path: string, on: boolean) {
  set({ transparency: { ...state.transparency, [path]: on } });
  persistViews();
}

export async function measurePick(ref: MeasureRef) {
  const id = state.docId;
  if (!id) return;
  let picks = [...state.measure.picks, ref];
  if (picks.length > 2) picks = [ref];
  set({ measure: { picks, result: null, pending: true } });
  try {
    const result = await withBusy(() => api.measure(id, picks[0], picks[1] ?? null, state.section, state.upto));
    if (state.measure.picks !== picks) return;
    set({ measure: { picks, result, pending: false }, error: null });
  } catch (e) {
    if (state.measure.picks !== picks) return;
    set({ measure: { picks: [], result: null, pending: false }, error: (e as Error).message });
  }
}

export function clearMeasure() {
  set({ measure: { picks: [], result: null, pending: false } });
}

export function pinMeasurement() {
  const m = state.measure;
  if (!m.result) return;
  const pin: Pin = { a: m.picks[0], b: m.picks[1] ?? null, text: measureText(m.result), points: measurePoints(m.result), result: m.result };
  set({ pins: [...state.pins, pin], measure: { picks: [], result: null, pending: false }, status: `pinned ${pin.text}` });
  persistViews();
}

export function removePin(index: number) {
  set({ pins: state.pins.filter((_, i) => i !== index) });
  persistViews();
}

export function measureText(r: MeasureResult): string {
  if (r.distance !== undefined) return `${fmt(r.distance)} mm${r.angle !== undefined && Math.abs(r.angle) > 1e-6 ? ` · ${fmt(r.angle)}°` : ''}`;
  const a = r.a;
  if (a.diameter !== undefined) return `Ø${fmt(a.diameter)}`;
  if (a.length !== undefined) return `L ${fmt(a.length)} mm`;
  if (a.area !== undefined) return `A ${fmt(a.area)} mm²`;
  if (a.position) return `(${a.position.map(fmt).join(', ')})`;
  return a.type ?? a.kind;
}

export function measurePoints(r: MeasureResult): number[][] {
  if (r.closest) return r.closest;
  const a = r.a;
  return [a.position ?? a.center ?? a.axis_point ?? [0, 0, 0]];
}

export const fmt = (v: number) => (Math.abs(v) >= 100 ? v.toFixed(1) : Math.abs(v) >= 10 ? v.toFixed(2) : v.toFixed(3)).replace(/\.?0+$/, '');

// ---- helpers -----------------------------------------------------------------

export function nextName(prefix: string, taken: Iterable<string>): string {
  const names = new Set(taken);
  for (let i = 1; ; i++) if (!names.has(`${prefix}${i}`)) return `${prefix}${i}`;
}

export function featureByName(name: string | null): Feature | null {
  return name ? state.tree?.features.find((f) => f.name === name) ?? null : null;
}

export function instanceByPath(path: string | null): Instance | null {
  if (!path) return null;
  const roots = state.tree?.evaluation.instances ?? [];
  let found: Instance | null = null;
  const walk = (i: Instance) => { if (i.path === path) found = i; else i.children.forEach(walk); };
  roots.forEach(walk);
  return found;
}

export function leafPaths(inst: Instance): string[] {
  return inst.children.length ? inst.children.flatMap(leafPaths) : [inst.path];
}
