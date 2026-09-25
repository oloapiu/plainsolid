import { ApiError, api } from '../api/client';
import { set, state, withBusy } from './core';
import { cancelMeshFetch, fetchGhost, fetchMesh, statusFor } from './geometry';
import { RequestLane, documentEpoch, invalidateDocumentRequests } from './requests';
import { enterSketch, syncSketchFrame } from './sketch';
import { cancelDocumentTools, dragState, setStatus } from './tools';
import { loadViews } from './views';
import { isDxf, type DxfMode, type EditOp, type EditResult, type ImportItem } from '../api/types';
import type { State } from './core';

let unsubscribeEvents: (() => void) | null = null;

let navigationSeq = 0;
interface SourceDraft { text: string; base: string; hash: string; conflict: boolean; error: string | null }
const drafts = new Map<string, SourceDraft>();
const sourceSaves = new Map<string, Promise<boolean>>();

// ---- document lifecycle ----------------------------------------------------

export async function start() {
  try {
    const docs = await api.listDocuments();
    set({ docs, status: docs.length ? 'ready' : 'no documents open' });
    await refreshFiles();
    unsubscribeWorkspace?.();
    unsubscribeWorkspace = api.workspaceEvents((e) => { if (e.event === 'open-request') handleOpenRequest(e); });
    if (docs.length) await useDocument(docs[0].id);
    await openFromQuery();
  } catch (e) {
    set({ error: `cannot reach the server: ${(e as Error).message}`, status: 'offline' });
  }
}

// ---- files from outside the project -----------------------------------------

let unsubscribeWorkspace: (() => void) | null = null;

/** What `plainsolid open` or a launcher asked for: a project file opens; a STEP file from
 * elsewhere waits for the import dialog, which copies it in. */
export function handleOpenRequest(e: { action: 'open'; path: string } | { action: 'import'; source: string; name: string; suffix: string }) {
  if (e.action === 'open') void openDocument(e.path);
  else requestImport([{ name: e.name, suffix: e.suffix, source: e.source }]);
}

/** `?open=path` and `?import=/abs/file.step` on the page URL, which `plainsolid open` uses when
 * no tab was there to hand the files to; read once and stripped, so a reload does not repeat them. */
async function openFromQuery() {
  if (typeof location === 'undefined' || !location.search) return;
  const q = new URLSearchParams(location.search);
  const opens = q.getAll('open'), imports = q.getAll('import');
  if (!opens.length && !imports.length) return;
  history.replaceState(null, '', location.pathname);
  for (const p of opens) await openDocument(p);
  requestImport(imports.map((source) => {
    const base = source.split('/').pop() ?? source;
    const dot = base.lastIndexOf('.');
    return { name: dot > 0 ? base.slice(0, dot) : base, suffix: dot > 0 ? base.slice(dot) : '', source };
  }));
}

/** Queue STEP and DXF files for the import dialog; the first one shows. */
export function requestImport(items: ImportItem[]) {
  if (items.length) set({ imports: [...state.imports, ...items] });
}

export function skipImport() {
  set({ imports: state.imports.slice(1) });
}

/** Copy the first queued file into the project as folder/name.suffix and open the copy, a DXF
 * file in the mode chosen; a DXF already in the project only opens in that mode. On failure the
 * file stays queued, so the dialog can take another name. */
export async function importNext(folder: string, name: string, mode?: DxfMode): Promise<boolean> {
  const item = state.imports[0];
  if (!item) return false;
  set({ loading: true, error: null });
  try {
    const tree = item.path !== undefined
      ? await api.openDocument(item.path, mode)
      : item.source !== undefined
        ? await api.importFile(item.source, folder, name, mode)
        : await api.uploadFile(item.file as Blob, folder, name, item.suffix, mode);
    set({ imports: state.imports.filter((i) => i !== item) });
    const docs = await api.listDocuments();
    set({ docs });
    const what = item.path !== undefined ? `opened ${item.path} as a ${mode}` : `imported ${folder ? `${folder}/` : ''}${name}${item.suffix}`;
    if (await useDocument(tree.id)) { set({ status: what }); showNewDxfSketch(tree); }
    else set({ loading: false });
    void refreshFiles();
    return true;
  } catch (e) {
    set({ error: (e as Error).message, loading: false });
    return false;
  }
}

/** Stop the server; every tab loses it. */
export async function quitServer() {
  try {
    await api.shutdown();
    set({ status: 'server stopped', error: 'the server was stopped; run plainsolid serve, or open a STEP file from the file manager, to start it again' });
  } catch (e) {
    set({ error: (e as Error).message });
  }
}

/** A part just written for a DXF holds only the sketch of its curves, which a part without a body
 * would not show: open it for editing, where the curves, the warnings and extrude or cut are. */
function showNewDxfSketch(opened: { created?: boolean; kind: string }) {
  if (!opened.created || opened.kind !== 'part') return;
  const sketch = state.tree?.features.find((f) => f.kind === 'sketch' && f.entities.some((e) => e.kind === 'import_dxf'));
  if (sketch) enterSketch(sketch);
}

/** Open a project file; a DXF file first asks, in the import dialog, whether it opens as a part
 * or as a drawing. */
export async function openDocument(path: string, mode?: DxfMode) {
  if (isDxf(path) && !mode) {
    const base = path.split('/').pop() ?? path;
    requestImport([{ name: base.slice(0, base.length - 4), suffix: base.slice(-4), path }]);
    return;
  }
  set({ loading: true, error: null });
  try {
    const tree = await api.openDocument(path, mode);
    const docs = await api.listDocuments();
    set({ docs });
    if (!await useDocument(tree.id)) set({ loading: false });
    else showNewDxfSketch(tree);
  } catch (e) {
    set({ error: (e as Error).message, loading: false });
  }
}

export async function newDocument(path: string, kind: 'part' | 'assembly' | 'drawing' = 'part', of?: string) {
  set({ loading: true, error: null });
  try {
    const tree = await api.newDocument(path, kind, of);
    const docs = await api.listDocuments();
    set({ docs });
    if (await useDocument(tree.id)) set({ status: `created ${path}` });
    else set({ loading: false });
  } catch (e) {
    set({ error: (e as Error).message, loading: false });
  }
}

/** Close a document; the view moves to another open one, or to nothing. */
export async function closeDocument(id: string) {
  if (state.closingDoc) return;
  set({ closingDoc: id });
  try {
    if (!await flushSource(id)) return;
    const r = await api.closeDocument(id);
    set({ docs: r.documents });
    if (state.docId === id) {
      if (r.documents.length) await useDocument(r.documents[0].id);
      else clearDocument();
    }
  } catch (e) {
    set({ error: (e as Error).message });
  } finally {
    set({ closingDoc: null });
  }
}

function clearDocument() {
  navigationSeq++;
  invalidateDocumentRequests();
  refetchRequests.cancel();
  unsubscribeEvents?.();
  unsubscribeEvents = null;
  cancelMeshFetch();
  cancelDocumentTools();
  set({ docId: null, tree: null, source: '', sourceDoc: null, sourceBase: '', codeDirty: false, saveState: 'saved', sourceHash: '', revision: '', hash: '', mesh: null, plainMesh: null, selected: null, selectedFace: null, selectedItem: null,
        sketchMode: null, ghostMesh: null, queries: null, externalPending: false, error: null,
        posePreview: null, previewNote: null, measure: { picks: [], result: null, pending: false }, status: 'no documents open' });
}

/** Export the current document (a part's body, or the posed assembly with its instance names
 * and colours) to a STEP or STL file; a relative path lands next to the document. */
export async function exportDocument(format: 'step' | 'stl' | 'pdf' | 'dxf' | 'svg', path?: string): Promise<string | null> {
  const id = state.docId;
  const tree = state.tree;
  if (!id || !tree) return null;
  const name = String(tree.meta.name ?? tree.path?.split('/').pop()?.replace(/\.py$/, '') ?? 'model');
  const target = (path ?? `${name}.${format}`).trim();
  if (!target) return null;
  try {
    const r = await api.export(id, format, target);
    setStatus(`exported ${r.path}`);
    return r.path;
  } catch (e) {
    set({ error: (e as Error).message });
    return null;
  }
}

/** The project's model and STEP files, for the open box and the instance dialog. */
export async function refreshFiles() {
  try { const r = await api.files(); set({ files: r.files, filesRoot: r.root }); } catch { /* the chooser just stays empty */ }
}

/** A path chosen from the project list, rewritten relative to the current document's directory,
 * which is what instance() and import_step() paths are relative to. Other text is left alone. */
export function docRelative(chosen: string): string {
  const docPath = state.tree?.path;
  const root = state.filesRoot;
  if (!docPath || !root || !state.files.some((f) => f.path === chosen)) return chosen;
  const rootParts = root.replace(/\/$/, '').split('/');
  const docParts = docPath.split('/');
  if (docParts.slice(0, rootParts.length).join('/') !== rootParts.join('/')) return chosen;
  const docDir = docParts.slice(rootParts.length, -1);  // the document's directory below the root
  const target = chosen.split('/');
  let common = 0;
  while (common < docDir.length && common < target.length - 1 && docDir[common] === target[common]) common++;
  return [...Array(docDir.length - common).fill('..'), ...target.slice(common)].join('/');
}

export async function useDocument(id: string): Promise<boolean> {
  const intent = ++navigationSeq;
  if (state.docId === id && state.sourceDoc === id) return true;
  if (state.sourceDoc && !await flushSource(state.sourceDoc)) return false;
  if (intent !== navigationSeq) return false;
  const seq = invalidateDocumentRequests();
  unsubscribeEvents?.();
  unsubscribeEvents = null;
  refetchRequests.cancel();
  cancelMeshFetch();
  cancelDocumentTools();
  set({
    docId: id, tree: null, hash: '', revision: '', sourceHash: '', saveState: 'saved', mesh: null, loading: true, queries: null, summary: null, overlay: null, featureDialog: null, dialogPicks: [], refHighlight: [], treeHover: null, selected: null, selectedFace: null, selectedEdge: null, selectedItem: null, hover: null, upto: null, sketchMode: null, ghostMesh: null, ghostStyle: 'ghost', ortho: state.orthoBeforeSketch ?? state.ortho, orthoBeforeSketch: null,
    codeDirty: false, sourceDoc: null, source: '', sourceBase: '', externalPending: false, tool: 'none', section: null, previewOffset: null, plainMesh: null,
    sectionPending: false, sectionSeconds: null, visibility: {},
    transparency: {}, measure: { picks: [], result: null, pending: false }, pins: [], named: {}, cameraToApply: null, fitPending: false,
    viewsWarning: null, planeDialog: null, pickRequest: null, drawingTool: null, drawingPicks: [], posePreview: null, previewNote: null,
  });
  await loadViews(id, seq);
  if (seq !== documentEpoch) return false;
  await refetch();
  if (seq !== documentEpoch || state.docId !== id) return false;
  unsubscribeEvents = api.events(id, (ev) => {
    if (seq !== documentEpoch || state.docId !== id) return;
    if (ev.event === 'hello') {
      // Reconnects must also catch dependency-only changes, which keep the source hash.
      if (ev.hash !== state.hash || ev.revision !== state.revision) void refetch();
      return;
    }
    if (ev.event === 'dependency') { void refetch(); return; }
    if (ev.hash === state.hash && ev.revision === state.revision) return;
    if (writesInFlight > 0 && ev.event === 'changed') return;
    void refetch();
  });
  return true;
}

const refetchRequests = new RequestLane();
export async function refetch() {
  const id = state.docId;
  if (!id) return;
  const request = refetchRequests.start();
  set({ loading: true });
  try {
    const [tree, src] = await withBusy(() => Promise.all([api.tree(id), api.source(id)]));
    if (!request.current()) return;
    if (tree.hash !== src.hash) { await refetch(); return; }
    const draft = drafts.get(id);
    const conflict = !!draft && src.source !== draft.base && !sourceSaves.has(id);
    if (conflict) { draft.conflict = true; draft.error = 'the file changed while you were typing; reload or explicitly use your version'; }
    const patch: Partial<State> = { tree, hash: tree.hash, revision: tree.revision, error: draft?.error ?? null };
    if (conflict) { patch.externalPending = true; patch.saveState = 'conflict'; }
    if (!dragState) { patch.posePreview = null; patch.previewNote = null; }  // a new mesh comes at the file's poses
    if (!draft) { patch.source = src.source; patch.sourceBase = src.source; patch.sourceHash = src.hash; patch.sourceDoc = id; patch.externalPending = false; patch.codeDirty = false; patch.saveState = 'saved'; }
    else if (state.sourceDoc !== id) {
      patch.source = draft.text; patch.sourceBase = draft.base; patch.sourceHash = draft.hash; patch.sourceDoc = id;
      patch.codeDirty = true; patch.externalPending = draft.conflict;
      patch.saveState = draft.conflict ? 'conflict' : draft.error ? 'error' : 'unsaved';
    }
    if (state.upto && !tree.features.some((f) => f.name === state.upto)) patch.upto = null;
    if (state.selected && !tree.features.some((f) => f.name === state.selected)) patch.selected = null;
    set(patch);
    syncSketchFrame(tree);
    await fetchMesh(statusFor(tree));
    if (state.sketchMode) void fetchGhost();
  } catch (e) {
    if (!request.current()) return;
    set({ error: (e as Error).message, loading: false });
  }
}

export async function reloadSource() {
  const id = state.sourceDoc;
  if (!id || sourceSaves.has(id)) return;
  drafts.delete(id);
  if (sourceTimer) clearTimeout(sourceTimer);
  set({ codeDirty: false, saveState: 'saved', externalPending: false, error: null });
  await refetch();
}

/** An explicit choice in the conflict banner; further typing alone never overwrites a conflict. */
export async function overwriteSource() {
  const id = state.sourceDoc;
  const draft = id ? drafts.get(id) : undefined;
  if (!id || !draft || sourceSaves.has(id)) return;
  try {
    const server = await api.source(id);
    if (drafts.get(id) !== draft) return;
    draft.base = server.source; draft.hash = server.hash; draft.conflict = false; draft.error = null;
    await flushSource(id);
  } catch (e) {
    draft.error = (e as Error).message;
    if (state.sourceDoc === id) set({ error: draft.error, saveState: 'conflict' });
  }
}

// ---- edits --------------------------------------------------------------------

export async function edit(op: EditOp): Promise<boolean> {
  return (await editResult(op)) !== null;
}

/** Apply an edit and hand back the server's result (poses, the part a make_editable wrote, ...). */
/** Writes this client has in flight: the server's "changed" event for them needs no refetch of its own. */
let writesInFlight = 0;

export async function editResult(op: EditOp): Promise<EditResult | null> {
  const id = state.docId;
  if (!id) return null;
  const seq = documentEpoch;
  if (!await flushSource(id) || state.docId !== id || seq !== documentEpoch) return null;
  writesInFlight++;
  try {
    let res;
    try {
      res = await withBusy(() => api.edit(id, op, state.hash));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const current = await api.source(id);
        res = await withBusy(() => api.edit(id, op, current.hash));
      } else throw e;
    }
    if (state.docId !== id || seq !== documentEpoch) return null;
    set({ hash: res.hash, error: null, status: res.changed ? 'edited' : 'no change' });
    if (res.changed) await refetch();
    return res;
  } catch (e) {
    if (state.docId === id && seq === documentEpoch) set({ error: (e as Error).message });
    return null;
  } finally {
    writesInFlight--;
  }
}

/** A STEP viewer becomes an assembly: one instance per body of the import, pointing into the
 * STEP file, posed as the file has them. No part file is written yet. */
export async function explodeImport(feature: string): Promise<EditResult | null> {
  const r = await editResult({ op: 'explode_import', feature });
  if (r) {
    const n = new Set(Object.values(r.instances ?? {})).size;
    const skipped = r.skipped?.length ? `; ${r.skipped.length} without a solid skipped` : '';
    setStatus(`${n} bodies are now instances${skipped}`);
  }
  return r;
}

/** Give a vendor body its own part file (an instance of a STEP file, or a body of a review
 * import, which first becomes instances) and open that part in a tab. */
export async function makeEditable(target: { instance?: string; leaf?: string }): Promise<EditResult | null> {
  const r = await editResult({ op: 'make_editable', ...target });
  if (r?.part) {
    setStatus(`${r.instance} now comes from ${r.part.split('/').pop()}${r.repointed && r.repointed.length > 1 ? ` (with ${r.repointed.filter((n) => n !== r.instance).join(', ')})` : ''}`);
    await openDocument(r.part);
  }
  return r;
}

let sourceTimer: ReturnType<typeof setTimeout> | null = null;
export function sourceEdited(text: string) {
  const id = state.sourceDoc;
  if (!id || id !== state.docId || state.closingDoc === id) return;
  const draft = drafts.get(id) ?? { text, base: state.sourceBase, hash: state.sourceHash, conflict: false, error: null };
  draft.text = text;
  drafts.set(id, draft);
  set({ source: text, codeDirty: true, saveState: draft.conflict ? 'conflict' : 'unsaved' });
  if (sourceTimer) clearTimeout(sourceTimer);
  sourceTimer = setTimeout(() => { void flushSource(id); }, 500);
}

/** Serialize saves per document and drain edits typed during an in-flight request. */
export async function flushSource(id: string | null = state.sourceDoc): Promise<boolean> {
  if (!id) return true;
  const pending = sourceSaves.get(id);
  if (pending) return pending;
  if (!drafts.has(id)) return true;
  const promise = saveDraft(id);
  sourceSaves.set(id, promise);
  try { return await promise; } finally { sourceSaves.delete(id); }
}

async function saveDraft(id: string): Promise<boolean> {
  writesInFlight++;
  try {
    while (drafts.has(id)) {
      const draft = drafts.get(id)!;
      const text = draft.text;
      if (draft.conflict) return false;
      if (!text.trim()) {
        draft.error = 'the file would be empty; not saved';
        if (state.sourceDoc === id) set({ error: draft.error, saveState: 'error' });
        return false;
      }
      if (state.sourceDoc === id) set({ saveState: 'saving' });
      let res: EditResult;
      try {
        try {
          res = await withBusy(() => api.putSource(id, text, draft.hash));
        } catch (e) {
          if (!(e instanceof ApiError) || e.status !== 409) throw e;
          const server = await api.source(id);
          if (server.source !== draft.base) {
            draft.conflict = true;
            throw new Error('the file changed while you were typing; reload or explicitly use your version');
          }
          res = await api.putSource(id, text, server.hash);
        }
      } catch (e) {
        draft.error = (e as Error).message;
        if (state.sourceDoc === id) set({ error: draft.error, externalPending: draft.conflict, saveState: draft.conflict ? 'conflict' : 'error' });
        return false;
      }
      draft.base = text; draft.hash = res.hash; draft.error = null;
      if (draft.text === text) drafts.delete(id);
      if (state.sourceDoc === id) {
        set({ sourceBase: text, sourceHash: res.hash, codeDirty: drafts.has(id), saveState: drafts.has(id) ? 'unsaved' : 'saved', externalPending: false, error: null });
        if (state.docId === id) await refetch();
      }
    }
    return true;
  } finally { writesInFlight--; }
}

async function changeHistory(direction: 'undo' | 'redo') {
  const id = state.docId, seq = documentEpoch;
  if (!id || !await flushSource(id) || state.docId !== id || seq !== documentEpoch) return;
  writesInFlight++;
  try {
    const r = await withBusy(() => api[direction](id));
    if (state.docId !== id || seq !== documentEpoch) return;
    set({ hash: r.hash, status: r.changed ? direction : `nothing to ${direction}` });
    if (r.changed) await refetch();
  } catch (e) {
    if (state.docId === id && seq === documentEpoch) set({ error: (e as Error).message });
  } finally { writesInFlight--; }
}
export async function undo() { await changeHistory('undo'); }
export async function redo() { await changeHistory('redo'); }
