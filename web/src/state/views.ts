import { api } from '../api/client';
import { set, state } from './core';
import { sameSection } from './geometry';
import { documentEpoch } from './requests';
import { setSection } from './tools';
import type { CameraState, ViewSnapshot, ViewsState } from '../api/types';

// ---- camera and view state -------------------------------------------------------

let cameraTimer: ReturnType<typeof setTimeout> | null = null;
export function cameraChanged(camera: CameraState) {
  // a sketch borrows the orthographic projection; what the sidecar remembers is the user's own choice
  if (state.sketchMode && state.orthoBeforeSketch !== null) camera = { ...camera, ortho: state.orthoBeforeSketch };
  set({ camera }, false);  // no re-render: nothing displays the camera
  if (cameraTimer) clearTimeout(cameraTimer);
  cameraTimer = setTimeout(persistViews, 1200);
}

export function cameraApplied() {
  if (state.cameraToApply) set({ cameraToApply: null });
}

/** The viewport fitted the document's first mesh, as asked when no camera was saved for it. */
export function fitApplied() {
  if (state.fitPending) set({ fitPending: false });
}

function snapshot(): ViewSnapshot {
  return { camera: state.camera, section: state.section, pins: state.pins, visibility: state.visibility, transparency: state.transparency };
}

let viewsTimer: ReturnType<typeof setTimeout> | null = null;
let viewsLoaded = false;
export function persistViews() {
  if (!state.docId || !viewsLoaded) return;
  if (viewsTimer) clearTimeout(viewsTimer);
  viewsTimer = setTimeout(async () => {
    const id = state.docId;
    if (!id) return;
    try { await api.putViews(id, { ...snapshot(), named: state.named }); }
    catch (e) { set({ error: `views: ${(e as Error).message}`}); }
  }, 600);
}

export async function loadViews(id: string, seq: number) {
  viewsLoaded = false;
  try {
    const v: ViewsState = await api.getViews(id);
    if (seq !== documentEpoch || state.docId !== id) return;
    set({
      named: v.named ?? {}, pins: v.pins ?? [], visibility: v.visibility ?? {}, transparency: v.transparency ?? {},
      section: v.section ?? null, camera: v.camera ?? null, cameraToApply: v.camera ?? null,
      fitPending: !v.camera,  // no camera saved for this document: show it whole when its mesh arrives
      ortho: v.camera?.ortho ?? false, viewsWarning: v.warning ?? null,
    });
  } catch (e) {
    if (seq === documentEpoch && state.docId === id) set({ error: `views: ${(e as Error).message}`, fitPending: true });
  } finally {
    if (seq === documentEpoch && state.docId === id) viewsLoaded = true;
  }
}

export function saveNamedView(name: string) {
  set({ named: { ...state.named, [name]: snapshot() }, status: `saved view ${name}` });
  persistViews();
}

export async function restoreNamedView(name: string) {
  const v = state.named[name];
  if (!v) return;
  set({ pins: v.pins ?? [], visibility: v.visibility ?? {}, transparency: v.transparency ?? {}, cameraToApply: v.camera ?? null,
        camera: v.camera ?? state.camera, ortho: v.camera?.ortho ?? state.ortho, status: `view ${name}` });
  if (!sameSection(v.section ?? null, state.section)) await setSection(v.section ?? null, false);
  persistViews();
}

export function deleteNamedView(name: string) {
  const named = { ...state.named };
  delete named[name];
  set({ named, status: `deleted view ${name}` });
  persistViews();
}

export function dismissViewsWarning() { set({ viewsWarning: null }); }
