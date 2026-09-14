import { api } from '../api/client';
import { isAssembly, set, state, withBusy } from './core';
import { refetch } from './documents';
import { RequestLane, requestScope } from './requests';
import type { Bom, CompareReport, Instance, Interference, Overlay, SectionSpec, Summary, Tree } from '../api/types';

/** Sketch mode: the finished body for the faint preview behind the pre-sketch body. */
export async function fetchGhost() {
  const id = state.docId;
  const revision = state.revision;
  const current = requestScope(true);
  const sm = state.sketchMode;
  if (!id || !sm) return;
  const features = state.tree?.features ?? [];
  const last = features[features.length - 1]?.name;
  if (state.ghostStyle === 'preview') return;  // a feature preview owns the ghost for now
  if (!last || last === sm.sketch || last === state.upto) { set({ ghostMesh: null }); return; }  // nothing after the sketch
  try {
    const m = await api.mesh(id, { section: state.section });
    if (current() && m.header.revision === revision && state.sketchMode?.sketch === sm.sketch && (state.ghostStyle as string) !== 'preview') set({ ghostMesh: m });
  } catch { if (current()) set({ ghostMesh: null }); }
}

// ---- mesh fetching: one latest-wins pipeline for plain and sectioned meshes -----------
//
// With a section wanted, the plain (uncut) mesh is fetched first so the viewport can show
// a GPU-clipped preview at once; the capped mesh from the server follows and replaces it.
// A newer fetch aborts the older one, and a late response is ignored by sequence number.

const meshRequests = new RequestLane();

export function cancelMeshFetch() {
  meshRequests.cancel();
}

export function plainMeshIsCurrent(): boolean {
  const pm = state.plainMesh;
  return !!pm && pm.header.revision === state.revision && (pm.header.upto ?? null) === (state.upto ?? null);
}

export function sameSection(a: SectionSpec | null | undefined, b: SectionSpec | null | undefined): boolean {
  if (!a || !b) return !a && !b;
  return a.plane === b.plane && Math.abs(a.offset - b.offset) < 1e-6 && Boolean(a.flip) === Boolean(b.flip);
}

/** True when the installed mesh already is the wanted section (so no clip is needed). */
export function installedSectionMatches(): boolean {
  return sameSection(state.mesh?.header.section ?? null, state.section);
}

export async function fetchMesh(status?: string) {
  const id = state.docId;
  if (!id) return;
  const request = meshRequests.start();
  const { upto, section } = state;
  const done = status ?? (state.tree ? statusFor(state.tree) : state.status);
  if (state.overlay) {
    // the comparison: three coloured items and the report in the header; it follows every edit
    set({ loading: true, error: null, sectionPending: false });
    try {
      const m = await withBusy(() => api.mesh(id, { upto, overlay: state.overlay, signal: request.signal }));
      if (!request.current()) return;
      if (m.header.revision !== state.revision) { await refetch(); return; }
      set({ mesh: m, loading: false, status: compareStatus(state.overlay, m.header.compare ?? null) });
    } catch (e) {
      if (!request.current()) return;
      set({ error: (e as Error).message, loading: false, overlay: null });
      void fetchMesh(done);
    }
    return;
  }
  try {
    if (!plainMeshIsCurrent()) {
      if (!section) set({ loading: true });
      const plain = await withBusy(() => api.mesh(id, { upto, signal: request.signal }));
      if (!request.current()) return;
      if (plain.header.revision !== state.revision) { await refetch(); return; }
      set({ plainMesh: plain, mesh: plain, loading: false, ...(section ? {} : { status: done }) });
    } else if (!section) {
      set({ mesh: state.plainMesh, loading: false, status: done, sectionPending: false });
    }
    if (!section) return;
    set({ sectionPending: true, error: null });
    const t0 = performance.now();
    const capped = await withBusy(() => api.mesh(id, { upto, section, signal: request.signal }));
    if (!request.current()) return;
    if (capped.header.revision !== state.revision) { await refetch(); return; }
    const seconds = (performance.now() - t0) / 1000;
    set({ mesh: capped, loading: false, sectionPending: false, sectionSeconds: seconds,
          sectionInstalls: state.sectionInstalls + 1, status: done });
  } catch (e) {
    if (!request.current()) return;
    // keep whatever is installed (the clip preview stays); say what went wrong
    set({ error: (e as Error).message, loading: false, sectionPending: false });
  }
}

function compareStatus(overlay: Overlay, report: CompareReport | null): string {
  const other = overlay.rev ? `this file at ${overlay.rev}` : overlay.other ?? '';
  if (!report) return `comparing with ${other}`;
  if (report.same) return `comparing with ${other}: the same geometry`;
  return `comparing with ${other}: +${report.added.volume.toFixed(1)} mm³ added (green), −${report.removed.volume.toFixed(1)} mm³ removed (red)`;
}

/** Compare the document with another file (relative to the project) or with itself at a git revision; null stops. */
export async function setOverlay(overlay: Overlay | null) {
  if (!state.docId) return;
  if (overlay && !overlay.other && !overlay.rev) overlay = null;
  set({ overlay, selectedFace: null, selectedEdge: null, selectedItem: null });
  await fetchMesh();
}

export function statusFor(tree: Tree): string {
  const failed = tree.features.filter((f) => f.result && !f.result.ok).length;
  const leaves = countLeaves(tree.evaluation.instances ?? []);
  const scene = tree.evaluation.drawing;
  const base = isAssembly(tree)
    ? `assembly, ${leaves} instances, ${(tree.evaluation.seconds * 1000).toFixed(0)} ms`
    : scene
    ? `drawing of ${scene.model.name ?? scene.model.path}, ${scene.views.length} views, ${scene.dimensions.length} dimensions, ${(tree.evaluation.seconds * 1000).toFixed(0)} ms`
    : `${tree.features.length} features, ${(tree.evaluation.seconds * 1000).toFixed(0)} ms`;
  if (tree.errors.length) return `${base}, file error: ${tree.errors[0].message}`;
  return failed ? `${base}, ${failed} failed` : base;
}

function countLeaves(roots: Instance[]): number {
  let n = 0;
  const walk = (i: Instance) => { if (i.children.length) i.children.forEach(walk); else n++; };
  roots.forEach(walk);
  return n;
}

/** Fetch the bill of materials and the interference check for the current assembly. */
export async function fetchAssemblyQueries(interference = false): Promise<void> {
  const id = state.docId;
  if (!id || !isAssembly(state.tree) || state.tree?.id !== id) return;  // the tree shown may still be the previous tab's
  const revision = state.revision;
  const current = requestScope(true);
  const cur = state.queries?.revision === revision ? state.queries : { revision, bom: null, interference: null, pending: false };
  set({ queries: { ...cur, pending: true } });
  try {
    const bom = cur.bom ?? (await api.query(id, 'bom') as unknown as Bom);
    const inter = interference ? (await api.query(id, 'interference') as unknown as Interference) : cur.interference;
    if (!current() || (bom as Bom & { revision: string }).revision !== revision) return;
    set({ queries: { revision, bom, interference: inter, pending: false }, error: null });
  } catch (e) {
    if (current()) set({ queries: { ...cur, pending: false }, error: (e as Error).message });
  }
}

/** The part overview's numbers: volume, area, box, counts and mass, for the current hash. */
export async function fetchSummary(): Promise<void> {
  const id = state.docId, revision = state.revision;
  const current = requestScope(true);
  if (!id || !state.tree || state.tree.id !== id || state.tree.kind === 'drawing') return;
  try {
    const data = await api.query(id, 'summary') as unknown as Summary;
    if (current() && (data as Summary & { revision: string }).revision === revision) set({ summary: { revision, data } });
  } catch { /* the overview line just stays empty */ }
}
