// The top bar's "open ▾" and "new ▾": a popover listing the project's documents (recent first,
// then parts, assemblies, drawings and STEP files) with a filter, and a popover that creates a
// part, an assembly or a drawing from a name and, for a drawing, the model it shows.
import { useEffect, useRef, useState } from 'react';
import { useStore, openDocument, newDocument, refreshFiles, setOverlay, getState } from './state/store';
import type { ProjectFile } from './api/types';

const RECENT_KEY = 'plainsolid.recent';
const readRecent = (): string[] => { try { return JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]') as string[]; } catch { return []; } };
const pushRecent = (p: string) => { try { localStorage.setItem(RECENT_KEY, JSON.stringify([p, ...readRecent().filter((x) => x !== p)].slice(0, 8))); } catch { /* fine */ } };

/** The current document's path relative to the project root (null outside the project). */
function currentRel(): string | null {
  const st = getState();
  const docPath = st.tree?.path, root = st.filesRoot;
  if (!docPath || !root) return null;
  const rootParts = root.replace(/\/$/, '').split('/'), docParts = docPath.split('/');
  if (docParts.slice(0, rootParts.length).join('/') !== rootParts.join('/')) return null;
  return docParts.slice(rootParts.length).join('/');
}

/** Close on a click elsewhere or escape. */
function useCloser(open: boolean, close: () => void) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) close(); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    window.addEventListener('pointerdown', onDown);
    window.addEventListener('keydown', onKey, true);
    return () => { window.removeEventListener('pointerdown', onDown); window.removeEventListener('keydown', onKey, true); };
  }, [open, close]);
  return ref;
}

const KINDS: [ProjectFile['kind'], string][] = [['part', 'parts'], ['assembly', 'assemblies'], ['drawing', 'drawings'], ['step', 'STEP files']];

export function OpenMenu({ compare, onCompareDone, openSignal }: { compare: boolean; onCompareDone: () => void; openSignal: number }) {
  const files = useStore((s) => s.files);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const mode = compare ? 'compare' : 'open';
  const close = () => { setOpen(false); if (compare) onCompareDone(); };
  const ref = useCloser(open, close);
  useEffect(() => { if (compare) setOpen(true); }, [compare]);
  useEffect(() => { if (openSignal) setOpen(true); }, [openSignal]);
  useEffect(() => { if (open) { void refreshFiles(); setFilter(''); } }, [open]);
  const q = filter.trim().toLowerCase();
  const visible = files.filter((f) => f.kind !== 'step' || !f.wrapper).filter((f) => !q || f.path.toLowerCase().includes(q));
  const recent = q ? [] : readRecent().map((p) => visible.find((f) => f.path === p)).filter((f): f is ProjectFile => !!f);
  const choose = (p: string) => {
    close();
    pushRecent(p);
    if (mode === 'compare') void setOverlay({ other: p }); else void openDocument(p);
  };
  const submit = () => { if (visible.length) choose(visible[0].path); else if (filter.trim()) choose(filter.trim()); };
  const row = (f: ProjectFile, key: string) => (
    <button key={key} className="files-row" data-testid={`file-${f.path}`} onClick={() => choose(f.path)} title={f.path}>
      <span className="mono">{f.path}</span><span className="files-kind">{f.kind === 'step' ? 'STEP' : f.kind}</span>
    </button>
  );
  return (
    <span className="menu" ref={ref}>
      <button className={`btn-small ${open ? 'active' : ''}`} type="button" onClick={() => (open ? close() : setOpen(true))} data-testid="open-menu" title="open a document (ctrl+o)">open ▾</button>
      {open && (
        <div className="menu-pop files-pop" data-testid="files-pop">
          <div className="files-title">{mode === 'compare' ? 'compare with' : 'open'}</div>
          <input autoFocus placeholder="filter, or type a path…" value={filter} onChange={(e) => setFilter(e.target.value)} spellCheck={false} data-testid="open-path"
                 onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } }} />
          {recent.length > 0 && <><div className="files-group">recent</div>{recent.map((f) => row(f, `r:${f.path}`))}</>}
          {KINDS.map(([kind, label]) => {
            const list = visible.filter((f) => f.kind === kind);
            return list.length ? <div key={kind}><div className="files-group">{label}</div>{list.map((f) => row(f, f.path))}</div> : null;
          })}
          {!visible.length && <div className="panel-help">{q ? 'no file matches · enter opens the typed path if it exists' : 'no documents in the project yet'}</div>}
          {mode === 'open' && <div className="panel-help">a STEP file opens as a viewer</div>}
        </div>
      )}
    </span>
  );
}

type NewKind = 'part' | 'assembly' | 'drawing';
const HELP: Record<NewKind, string> = {
  part: 'an empty part; the file goes next to the current document',
  assembly: 'an empty assembly; then add instances of parts and STEP files',
  drawing: 'a sheet with four third-angle views of the model',
};

export function NewMenu() {
  const files = useStore((s) => s.files);
  const tree = useStore((s) => s.tree);
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<NewKind | null>(null);
  const [name, setName] = useState('');
  const [of, setOf] = useState('');
  const close = () => setOpen(false);
  const ref = useCloser(open, close);
  useEffect(() => { if (open) { void refreshFiles(); setKind(null); } }, [open]);
  const models = files.filter((f) => f.kind === 'part' || f.kind === 'assembly');
  const pickKind = (k: NewKind) => {
    setKind(k);
    const cur = currentRel();
    const dir = cur && cur.includes('/') ? cur.slice(0, cur.lastIndexOf('/') + 1) : '';
    const stem = cur ? (cur.split('/').pop() ?? 'model').replace(/\.py$/, '') : 'model';
    const taken = new Set(files.map((f) => f.path));
    let n = 1;
    while (taken.has(`${dir}${k}${n}.py`)) n++;
    setName(k === 'drawing' ? `${dir}${stem}_dwg.py` : `${dir}${k}${n}.py`);
    setOf(k === 'drawing' ? (cur && tree && tree.kind !== 'drawing' ? cur : (models[0]?.path ?? '')) : '');
  };
  const create = () => {
    const p = name.trim();
    if (!kind || !p || (kind === 'drawing' && !of)) return;
    close();
    void newDocument(p, kind, kind === 'drawing' ? of : undefined);
  };
  return (
    <span className="menu" ref={ref}>
      <button className={`btn-small ${open ? 'active' : ''}`} type="button" onClick={() => (open ? close() : setOpen(true))} data-testid="new-menu" title="create a part, an assembly or a drawing">new ▾</button>
      {open && (
        <div className="menu-pop files-pop" data-testid="new-pop">
          <div className="files-title">new</div>
          <div className="btn-row">
            {(['part', 'assembly', 'drawing'] as NewKind[]).map((k) => (
              <button key={k} className={`btn-small ${kind === k ? 'active' : ''}`} type="button" onClick={() => pickKind(k)} data-testid={`new-${k}`}>{k}</button>
            ))}
          </div>
          {kind && (
            <form onSubmit={(e) => { e.preventDefault(); create(); }}>
              <label className="files-field"><span>file</span><input autoFocus value={name} onChange={(e) => setName(e.target.value)} spellCheck={false} data-testid="new-name" /></label>
              {kind === 'drawing' && (
                <label className="files-field"><span>of</span>
                  <select value={of} onChange={(e) => setOf(e.target.value)} data-testid="new-of">
                    {!models.length && <option value="">no part or assembly yet</option>}
                    {models.map((m) => <option key={m.path} value={m.path}>{m.path}</option>)}
                  </select>
                </label>
              )}
              <div className="btn-row"><button className="btn-small" type="submit" data-testid="new-create" disabled={!name.trim() || (kind === 'drawing' && !of)}>create {kind}</button></div>
              <div className="panel-help">{HELP[kind]} · paths are relative to the project</div>
            </form>
          )}
          {!kind && <div className="panel-help">pick what to create</div>}
        </div>
      )}
    </span>
  );
}
