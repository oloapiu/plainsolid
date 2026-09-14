import { useEffect, useRef, useState } from 'react';
import { useStore, setSection, setPreviewOffset, setTool, fmt } from '../state/store';
import type { SectionPlane, SectionSpec } from '../api/types';

const AXIS: Record<SectionPlane, number> = { XY: 2, XZ: 1, YZ: 0 };
const SIGN: Record<SectionPlane, number> = { XY: 1, XZ: -1, YZ: 1 };
const COMMIT_DEBOUNCE_MS = 150;

export function SectionPanel() {
  const section = useStore((s) => s.section);
  const mesh = useStore((s) => s.plainMesh ?? s.mesh);
  const pending = useStore((s) => s.sectionPending);
  const seconds = useStore((s) => s.sectionSeconds);
  const error = useStore((s) => s.error);
  const [plane, setPlane] = useState<SectionPlane>(section?.plane ?? 'XY');
  const [offset, setOffset] = useState<number>(section?.offset ?? 0);
  const [flip, setFlip] = useState<boolean>(section?.flip ?? false);
  const [text, setText] = useState(String(section?.offset ?? 0));
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (section) { setPlane(section.plane); setOffset(section.offset); setFlip(section.flip); setText(String(section.offset)); }
  }, [section]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  // slider range from the uncut mesh's bbox along the plane normal
  const bb = mesh?.header.bbox ?? [[-50, -50, -50], [50, 50, 50]];
  const axis = AXIS[plane], sign = SIGN[plane];
  const lo = Math.min(bb[0][axis], bb[1][axis]) * sign, hi = Math.max(bb[0][axis], bb[1][axis]) * sign;
  const [min, max] = [Math.floor(Math.min(lo, hi) - 1), Math.ceil(Math.max(lo, hi) + 1)];
  const step = Math.max(0.1, Math.round((max - min) / 400 * 10) / 10);

  // commits are debounced and latest-wins: a burst of slider releases or typed values
  // becomes one request, and the store aborts any older request still in flight
  const commit = (p: SectionPlane, o: number, f: boolean) => {
    const spec: SectionSpec = { plane: p, offset: Math.round(o * 1000) / 1000, flip: f };
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => { timer.current = null; setSection(spec); }, COMMIT_DEBOUNCE_MS);
  };
  const onPlane = (p: SectionPlane) => { setPlane(p); commit(p, offset, flip); };
  const onFlip = (f: boolean) => { setFlip(f); commit(plane, offset, f); };
  const onText = () => { const v = Number(text); if (Number.isFinite(v)) { setOffset(v); commit(plane, v, flip); } else setText(String(offset)); };
  const onClear = () => { if (timer.current) { clearTimeout(timer.current); timer.current = null; } setSection(null); };
  const sectionError = error && /^mesh:/.test(error) ? error : null;

  return (
    <div className="tool-panel" data-testid="section-panel">
      <span className="tool-title">section</span>
      <div className="btn-group">
        {(['XY', 'XZ', 'YZ'] as SectionPlane[]).map((p) => (
          <button key={p} className={`btn-small ${plane === p && section ? 'active' : ''}`} onClick={() => onPlane(p)}>{p}</button>
        ))}
      </div>
      <input type="range" min={min} max={max} step={step} value={offset} data-testid="section-slider"
             onChange={(e) => { const v = Number(e.target.value); setOffset(v); setText(String(v)); if (section) setPreviewOffset(v); }}
             onPointerUp={() => commit(plane, offset, flip)} onKeyUp={(e) => { if (e.key.startsWith('Arrow')) commit(plane, offset, flip); }} />
      <input className="tool-num" value={text} data-testid="section-offset" onChange={(e) => setText(e.target.value)}
             onBlur={onText} onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }} />
      <label className="tool-check"><input type="checkbox" checked={flip} onChange={(e) => onFlip(e.target.checked)} /> flip</label>
      <span className="tool-status" data-testid="section-status">
        {pending ? 'sectioning…' : sectionError ? sectionError : seconds !== null && section ? `${fmt(seconds)} s` : ''}
      </span>
      {section && <button className="btn-small" onClick={onClear} title="remove the section" data-testid="section-clear">clear</button>}
      <button className="btn-small" onClick={() => setTool('section')} title="close">×</button>
    </div>
  );
}
