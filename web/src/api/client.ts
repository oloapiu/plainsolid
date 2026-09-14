import type { DocSummary, DragResult, EditOp, EditResult, MeasureRef, MeasureResult, Overlay, PreviewResult, ProjectFile, SectionSpec, SketchSolution, Tree, ViewsState, WsEvent } from './types';
import { parseMesh, type ParsedMesh } from './mesh';

export class ApiError extends Error {
  constructor(public status: number, message: string, public hash?: string) {
    super(message);
  }
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = res.statusText;
    let hash: string | undefined;
    try {
      const body = await res.json();
      msg = body.error ?? body.detail ?? JSON.stringify(body);
      hash = body.hash;
    } catch { /* not json */ }
    throw new ApiError(res.status, msg, hash);
  }
  return res.json() as Promise<T>;
}

const post = (url: string, body: unknown) =>
  fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const put = (url: string, body: unknown) =>
  fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export interface MeshOptions { upto?: string | null; section?: SectionSpec | null; tolerance?: number | null; signal?: AbortSignal; overlay?: Overlay | null }

function meshQuery(o: MeshOptions): string {
  const q = new URLSearchParams();
  if (o.upto) q.set('upto', o.upto);
  if (o.tolerance) q.set('tolerance', String(o.tolerance));
  if (o.section) { q.set('plane', o.section.plane); q.set('offset', String(o.section.offset)); q.set('flip', String(o.section.flip)); }
  if (o.overlay?.rev) q.set('rev', o.overlay.rev);
  else if (o.overlay?.other) q.set('overlay', o.overlay.other);
  const s = q.toString();
  return s ? `?${s}` : '';
}

export const api = {
  listDocuments: () => fetch('/api/documents').then(json<DocSummary[]>),
  files: () => fetch('/api/files').then(json<{ root: string; files: ProjectFile[] }>),
  closeDocument: (id: string) => post(`/api/documents/${id}/close`, {}).then(json<{ closed: string; documents: DocSummary[] }>),
  export: (id: string, format: string, path: string) => post(`/api/documents/${id}/export`, { format, path }).then(json<{ path: string; format: string }>),
  openDocument: (path: string) => post('/api/documents/open', { path }).then(json<Tree>),
  newDocument: (path: string, kind: 'part' | 'assembly' | 'drawing' = 'part', of?: string) => post('/api/documents/new', { path, kind, ...(of ? { of } : {}) }).then(json<Tree>),
  tree: (id: string) => fetch(`/api/documents/${id}/tree`).then(json<Tree>),
  source: (id: string) => fetch(`/api/documents/${id}/source`).then(json<{ source: string; hash: string; path: string }>),
  putSource: (id: string, source: string, hash: string) => put(`/api/documents/${id}/source`, { source, hash }).then(json<EditResult>),
  edit: (id: string, op: EditOp, hash: string) => post(`/api/documents/${id}/edit`, { ...op, hash }).then(json<EditResult>),
  undo: (id: string) => post(`/api/documents/${id}/undo`, {}).then(json<EditResult>),
  redo: (id: string) => post(`/api/documents/${id}/redo`, {}).then(json<EditResult>),
  mesh: async (id: string, options: MeshOptions = {}): Promise<ParsedMesh> => {
    const res = await fetch(`/api/documents/${id}/mesh${meshQuery(options)}`, { signal: options.signal });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error ?? msg; } catch { /* not json */ }
      throw new ApiError(res.status, `mesh: ${msg}`);
    }
    return parseMesh(await res.arrayBuffer());
  },
  query: (id: string, kind: string) => fetch(`/api/documents/${id}/query/${kind}`).then(json<Record<string, unknown>>),
  /** The mesh ids a selector or feature name picks, to light a reference shown in the panel. */
  locate: (id: string, refs: string[], upto: string | null, signal?: AbortSignal) =>
    fetch(`/api/documents/${id}/locate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refs, upto: upto ?? undefined }), signal })
      .then(json<{ faces: number[]; edges: number[]; vertices: number[] }>),
  measure: (id: string, a: MeasureRef, b: MeasureRef | null, section: SectionSpec | null, upto: string | null) =>
    post(`/api/documents/${id}/measure`, { a, b: b ?? undefined, section: section ?? undefined, upto: upto ?? undefined }).then(json<MeasureResult>),
  previewMesh: async (id: string, op: EditOp, signal?: AbortSignal): Promise<ParsedMesh> => {
    const res = await fetch(`/api/documents/${id}/preview-mesh`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ op }), signal });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error ?? msg; } catch { /* not json */ }
      throw new ApiError(res.status, msg);
    }
    return parseMesh(await res.arrayBuffer());
  },
  preview: (id: string, op: EditOp, chooseFlip = false, signal?: AbortSignal) =>
    fetch(`/api/documents/${id}/preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ op, choose_flip: chooseFlip }), signal }).then(json<PreviewResult>),
  drag: (id: string, instance: string, poses: Record<string, { at: number[]; rotate: number[] }> | null, translate: number[] | null, rotate: number[] | null) =>
    post(`/api/documents/${id}/drag`, { instance, poses, translate, rotate }).then(json<DragResult>),
  solve: (id: string, sketch: string, drag: Record<string, [number, number]> | null, signal?: AbortSignal) =>
    fetch(`/api/documents/${id}/solve`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sketch, drag: drag ?? undefined }), signal }).then(json<SketchSolution>),
  getViews: (id: string) => fetch(`/api/documents/${id}/views`).then(json<ViewsState>),
  putViews: (id: string, views: Partial<ViewsState>) => put(`/api/documents/${id}/views`, { views }).then(json<ViewsState>),
  events: (id: string, onEvent: (e: WsEvent) => void): (() => void) => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 500;
    const connect = () => {
      if (closed) return;
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/api/documents/${id}/events`);
      ws.onmessage = (m) => { try { onEvent(JSON.parse(m.data) as WsEvent); } catch { /* ignore */ } };
      ws.onopen = () => { retry = 500; };
      ws.onclose = () => { if (!closed) { setTimeout(connect, retry); retry = Math.min(retry * 2, 5000); } };
      ws.onerror = () => ws?.close();
    };
    connect();
    return () => { closed = true; ws?.close(); };
  },
};
