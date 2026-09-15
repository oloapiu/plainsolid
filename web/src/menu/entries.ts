// What a right-click offers, computed from where it landed, the selection and the active tool.
// Every builder returns plain entries; the ContextMenu component renders them.
import type { Feature, Instance, PickedEntity, SectionPlane } from '../api/types';
import { MATE_KINDS, TOOL_KINDS } from '../api/types';
import {
  getState, select, selectFace, selectEdge, enterSketch, openFeatureDialog, openPlaneDialog, setSuppressed, setUpto, requestDelete, addSketchOn, selectorTarget,
  setTool, measurePick, clearMeasure, pinMeasurement, setShowPlanes, isolate, showAll, allLeafPaths, setVisibility, setTransparency, leafPaths, fixInstance,
  openDocument, makeEditable, explodeImport, fetchAssemblyQueries, setDrawingTool, addNoteAt, addView, edit, requestPick, planePicked, pick, setSection, setMoveMode,
  revealCode, featureByName, itemOf, instanceFeatureOf, isAssembly, isViewer, setDimKind, instanceByPath,
  type MenuEntry, type DimKind,
} from '../state/store';
import { codeGoToLine } from '../code/CodePane';
import type { Scene3D, ViewName } from '../viewport/scene';

export const SEP: MenuEntry = { sep: true };
export const item = (label: string, run: () => void, extra: Partial<MenuEntry> = {}): MenuEntry =>
  ({ label, run, id: label.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '').toLowerCase(), ...extra });

const SECTION_AXIS: Record<SectionPlane, number> = { XY: 2, XZ: 1, YZ: 0 };
const SECTION_SIGN: Record<SectionPlane, number> = { XY: 1, XZ: -1, YZ: 1 };

/** Select the feature, show the code pane and put the cursor on its first line. */
export function goToCode(f: Feature) {
  select(f.name);
  revealCode();
  if (f.span) { const line = f.span[0]; setTimeout(() => codeGoToLine(line), 60); }
}

const rootOf = (name: string): Instance | null => (getState().tree?.evaluation.instances ?? []).find((r) => r.name === name) ?? null;

/** Visibility entries for a set of leaf paths (an instance, a viewer node). */
function visibilityEntries(paths: string[]): MenuEntry[] {
  if (!paths.length) return [];
  const st = getState();
  const visible = paths.some((p) => st.visibility[p] ?? true);
  const transparent = paths.every((p) => st.transparency[p] ?? false);
  const out = [
    item('isolate', () => isolate(paths), { title: 'show only this' }),
    item(visible ? 'hide' : 'show', () => paths.forEach((p) => setVisibility(p, !visible))),
    item(transparent ? 'opaque' : 'transparent', () => paths.forEach((p) => setTransparency(p, !transparent))),
  ];
  if (allLeafPaths().some((p) => st.visibility[p] === false)) out.push(item('show all', showAll));
  return out;
}

/** The common tail of a feature row: suppress, roll back, code, delete. */
function featureTail(f: Feature, withRollback: boolean): MenuEntry[] {
  const upto = getState().upto;
  const out: MenuEntry[] = [SEP];
  if (f.kind !== 'sketch' || true) out.push(item(f.suppressed ? 'unsuppress' : 'suppress', () => void setSuppressed(f.name, !f.suppressed), { disabled: f.read_only }));
  if (withRollback) out.push(item(upto === f.name ? 'show all features' : 'roll back to here', () => void setUpto(upto === f.name ? null : f.name)));
  out.push(item('go to code', () => goToCode(f), { disabled: !f.span }));
  out.push(SEP, item('delete', () => requestDelete(f.name), { danger: true, disabled: f.read_only }));
  return out;
}

/** A row of the feature, assembly or drawing tree. */
export function featureRowMenu(f: Feature): MenuEntry[] {
  const st = getState();
  const tree = st.tree;
  const out: MenuEntry[] = [];
  if (f.kind === 'sketch') {
    out.push(item('edit sketch', () => enterSketch(f), { disabled: f.read_only }));
    if (f.variable) out.push(item('extrude…', () => openFeatureDialog('extrude', f.name)), item('cut…', () => openFeatureDialog('cut', f.name)), item('revolve…', () => openFeatureDialog('revolve', f.name)));
    return [...out, ...featureTail(f, true)];
  }
  if (f.kind === 'plane') {
    const ready = !!f.variable && !!f.result?.plane;
    out.push(item('sketch on it', () => void addSketchOn({ kind: 'plane', name: f.name, expr: f.variable ?? f.name, label: `plane ${f.name}`, standard: false }), { disabled: !ready }));
    out.push(item('offset plane from it', () => { select(f.name); openPlaneDialog('offset'); }, { disabled: !ready }));
    out.push(item(st.showPlanes ? 'hide planes' : 'show planes', () => setShowPlanes(!getState().showPlanes)));
    return [...out, ...featureTail(f, true)];
  }
  if (f.kind === 'extrude' || f.kind === 'cut' || f.kind === 'revolve') {
    const sk = featureByName(String(f.args.sketch ?? ''));
    if (sk && !sk.read_only) out.push(item('edit its sketch', () => enterSketch(sk)));
  }
  if (TOOL_KINDS.includes(f.kind) && f.variable && !f.read_only) {
    out.push(item('pattern…', () => openFeatureDialog('linear_pattern', f.name)), item('mirror…', () => openFeatureDialog('mirror', f.name)));
  }
  if (f.kind === 'instance' && tree) {
    const root = rootOf(f.name);
    out.push(...visibilityEntries(root ? leafPaths(root) : []));
    const fixedBy = tree.features.find((x) => x.kind === 'fixed' && String(x.args.instance) === f.name && !x.suppressed);
    out.push(SEP);
    if (fixedBy) out.push(item('free it', () => requestDelete(fixedBy.name), { title: `deletes ${fixedBy.name}` }));
    else if (f.variable) out.push(item('fix here', () => void fixInstance(f.name)));
    out.push(item('mate from here…', () => openFeatureDialog('mate'), { title: 'pick a face of this instance first, or in the dialog' }));
    if (root?.kind === 'part' && root.file) out.push(item('open part', () => void openDocument(root.file!)));
    if (root?.kind === 'step' && root.solids === 1 && !f.read_only) out.push(item('edit part', () => void makeEditable({ instance: f.name })));
    const group = stepGroupOf(f);
    if (group) out.push(item(`open ${group.name}`, () => void openDocument(`${group.file}#${group.node}`), { title: `the sub-assembly ${group.node} of the file, in a tab of its own` }));
    return [...out, ...featureTail(f, false)];
  }
  if (f.kind === 'import_step') {
    const root = rootOf(f.name);
    out.push(...visibilityEntries(root ? leafPaths(root) : []));
    out.push(item('make editable', () => void explodeImport(f.name), { title: 'the bodies become instances of an assembly' }));
    return [...out, ...featureTail(f, false)];
  }
  if (MATE_KINDS.includes(f.kind)) {
    if (f.kind !== 'concentric' && f.kind !== 'parallel' && f.kind !== 'fixed') out.push(item(f.args.flip ? 'unflip' : 'flip', () => void edit({ op: 'set_argument', feature: f.name, kwarg: 'flip', value: !f.args.flip }), { disabled: f.read_only }));
    return [...out, ...featureTail(f, false)];
  }
  if (f.kind === 'view') {
    out.push(item('dimension in this view…', () => setDrawingTool('dimension')));
    const hidden = Boolean(f.args.hidden ?? !f.args.section);
    out.push(item(hidden ? 'hide hidden lines' : 'show hidden lines', () => void edit({ op: 'set_argument', feature: f.name, kwarg: 'hidden', value: !hidden }), { disabled: f.read_only }));
    return [...out, ...featureTail(f, false)];
  }
  if (f.kind === 'dimension' || f.kind === 'note') return featureTail(f, false).slice(1);  // no suppress for sheet items: code and delete
  return [...out, ...featureTail(f, true)];
}

/** A node of a STEP viewer's tree (a body or a sub-assembly of the file). */
export function viewerNodeMenu(inst: Instance): MenuEntry[] {
  const tree = getState().tree;
  const imports = (tree?.features ?? []).filter((f) => f.kind === 'import_step');
  const sub = subAssemblyOf(inst);
  return [
    ...visibilityEntries(leafPaths(inst)),
    SEP,
    ...(sub ? [item(sub === inst ? 'open sub-assembly' : `open ${sub.name}`, () => openSubAssembly(sub), { title: 'in a tab of its own, in its own coordinates' })] : []),
    item('make editable', () => { for (const f of imports) void explodeImport(f.name); }, { title: 'the file\'s bodies become instances of an assembly', disabled: !imports.length }),
  ];
}

/** The node of a STEP file an instance came out of (`file.step#root.sub.body` sits in `root.sub`),
 * when that node is a sub-assembly and not the file's root. The file is the absolute path the evaluation loaded. */
export function stepGroupOf(f: Feature): { name: string; node: string; file: string } | null {
  if (f.kind !== 'instance') return null;
  const fragment = String(f.args.path).split('#')[1] ?? '';
  const node = fragment.slice(0, fragment.lastIndexOf('.'));
  const root = rootOf(f.name);
  return node.includes('.') && root?.kind === 'step' && root.file ? { name: node.split('.').pop()!, node, file: root.file } : null;
}

/** The grey header row above the instances that came out of one node of a STEP file. */
export function stepGroupMenu(node: string, members: Feature[]): MenuEntry[] {
  const leaves = members.flatMap((f) => { const r = rootOf(f.name); return r ? leafPaths(r) : []; });
  const group = members.map(stepGroupOf).find((g) => g && g.node === node) ?? null;
  return [
    ...visibilityEntries(leaves),
    SEP,
    ...(group ? [item('open sub-assembly', () => void openDocument(`${group.file}#${group.node}`), { title: 'in a tab of its own, in its own coordinates' })] : []),
  ];
}

/** The sub-assembly a viewer row can open in a tab: the row itself, or a body's parent.
 * Never the file's root, which is this document, and only nodes that know their file. */
export function subAssemblyOf(inst: Instance): Instance | null {
  const dot = inst.path.lastIndexOf('.');
  const sub = inst.children.length ? inst : dot > 0 ? instanceByPath(inst.path.slice(0, dot)) : null;
  return sub && sub.path.includes('.') && sub.file && sub.node ? sub : null;
}

/** Open a node of a STEP file as a document of its own (the server writes its wrapper). */
export function openSubAssembly(sub: Instance) {
  void openDocument(`${sub.file}#${sub.node}`);
}

/** The viewport: the active tool or pick first, then what is under the cursor, then the view. */
export function viewportMenu(entity: PickedEntity | null, center: [number, number, number] | null, scene: Scene3D): MenuEntry[] {
  const st = getState();
  const tree = st.tree;
  const out: MenuEntry[] = [];
  const req = st.pickRequest;
  if (req) {
    if (entity && req.kinds.includes(entity.kind) && center) out.push(item(`use this ${entity.kind}`, () => pick(entity, center)));
    if (req.planes) for (const n of ['XY', 'XZ', 'YZ']) out.push(item(`${n} plane`, () => planePicked(n)));
    out.push(item(req.multi ? 'done picking' : 'cancel the pick', () => requestPick(null)), SEP);
  }
  if (st.tool === 'section' && st.section) {
    const s = st.section;
    if (entity?.kind === 'face' && center) out.push(item('set the offset to this face', () => setSection({ ...s, offset: Math.round(center[SECTION_AXIS[s.plane]] * SECTION_SIGN[s.plane] * 1000) / 1000 })));
    out.push(item('flip the section', () => void setSection({ ...s, flip: !s.flip })), item('clear the section', () => void setSection(null)), SEP);
  }
  if (st.tool === 'move') out.push(item('slide', () => setMoveMode('translate')), item('turn', () => setMoveMode('rotate')), item('stop moving', () => setTool('move')), SEP);
  if (st.tool === 'measure' && st.measure.result) out.push(item('pin the measurement', pinMeasurement), item('clear the measurement', clearMeasure), SEP);
  if (entity && tree && !req) {
    if (isAssembly(tree)) {
      const itm = itemOf(entity);
      const inst = itm ? instanceFeatureOf(itm.path) : null;
      if (inst) out.push(...featureRowMenu(inst).filter((e) => e.label !== 'go to code' && e.label !== 'delete').filter((e, i, all) => !(e.sep && (i === all.length - 1 || all[i + 1]?.sep))));
      else if (itm) { const node = instanceByPath(itm.path); if (node) out.push(...viewerNodeMenu(node)); }
      if (inst && entity.kind === 'face') out.push(item('mate from this face…', () => { selectFace(entity.id); openFeatureDialog('mate'); }));
    } else {
      out.push(...partEntityEntries(entity, center, scene));
    }
    out.push(item('measure from here', () => { if (getState().tool !== 'measure') setTool('measure'); void measurePick({ [entity.kind]: entity.id } as never); }));
    out.push(SEP);
  }
  const views: ViewName[] = ['iso', 'front', 'top', 'right'];
  out.push(item('fit', () => scene.fit()), ...views.map((v) => item(`${v} view`, () => scene.setView(v))));
  out.push(item(st.showPlanes ? 'hide planes' : 'show planes', () => setShowPlanes(!getState().showPlanes)));
  if (isAssembly(tree) && allLeafPaths().some((p) => st.visibility[p] === false)) out.push(item('show all', showAll));
  if (isAssembly(tree) && !isViewer(tree)) out.push(item('check interference', () => { select(null); void fetchAssemblyQueries(true); }));
  if (st.selected || st.selectedFace !== null || st.selectedEdge !== null || st.selectedItem) out.push(item('clear selection', () => select(null)));
  return out;
}

/** A face, edge or vertex of a part's body. */
function partEntityEntries(entity: PickedEntity, center: [number, number, number] | null, scene: Scene3D): MenuEntry[] {
  const st = getState();
  const out: MenuEntry[] = [];
  const label = st.mesh ? (entity.kind === 'face' ? st.mesh.header.face_labels[String(entity.id)] : entity.kind === 'edge' ? st.mesh.header.edge_labels[String(entity.id)] : '') : '';
  const owner = featureByName(label || null);
  const sketchOf = owner && (owner.kind === 'extrude' || owner.kind === 'cut' || owner.kind === 'revolve') ? featureByName(String(owner.args.sketch ?? '')) : null;
  if (entity.kind === 'face') {
    out.push(item('sketch on this face', () => { const t = center ? selectorTarget(entity, center) : null; if (t) void addSketchOn(t); }, { disabled: !center }));
    out.push(item('offset plane from it…', () => { selectFace(entity.id); openPlaneDialog('offset'); }));
    out.push(item('normal to', () => scene.lookAtFace(entity.id), { title: 'ctrl+0' }));
    out.push(item('shell removing it…', () => { selectFace(entity.id); openFeatureDialog('shell'); }));
  } else if (entity.kind === 'edge') {
    out.push(item('fillet…', () => { selectEdge(entity.id); openFeatureDialog('fillet'); }));
    out.push(item('chamfer…', () => { selectEdge(entity.id); openFeatureDialog('chamfer'); }));
    out.push(item('plane at an angle about it…', () => { selectEdge(entity.id); openPlaneDialog('angle'); }));
  }
  if (sketchOf && !sketchOf.read_only) out.push(item(`edit sketch ${sketchOf.name}`, () => enterSketch(sketchOf), { title: `the sketch ${owner!.name} was made from` }));
  return out;
}

/** The sheet of a drawing: the dimension tool's kinds, the item under the cursor, or the sheet itself. */
export function sheetMenu(target: { kind: string; name: string } | null, point: [number, number]): MenuEntry[] {
  const st = getState();
  const out: MenuEntry[] = [];
  if (st.drawingTool === 'dimension') {
    for (const k of ['distance', 'diameter', 'radius', 'angle'] as DimKind[]) out.push(item(`${k}${st.dimKind === k ? ' ✓' : ''}`, () => setDimKind(k)));
    out.push(item('cancel the dimension', () => setDrawingTool(null)), SEP);
  } else if (st.drawingTool === 'note') out.push(item('cancel the note', () => setDrawingTool(null)), SEP);
  const f = target ? featureByName(target.name) : null;
  if (f) { out.push(...featureRowMenu(f)); if (!out[out.length - 1]?.sep) out.push(SEP); }
  else {
    out.push(item('note here…', () => void addNoteAt(point)));
    for (const d of ['front', 'top', 'right', 'iso']) out.push(item(`add ${d} view here`, () => void addView(d, undefined, [Math.round(point[0]), Math.round(point[1])])));
    out.push(SEP);
  }
  if (st.selected) out.push(item('clear selection', () => select(null)));
  return out;
}
