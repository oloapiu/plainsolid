// The sheet of a drawing document: an SVG of the scene the server laid out (views with
// their hidden lines and hatching, DXF views with their own text, dimensions and fills,
// dimensions, notes, the frame and title block), with
// pan and zoom, selection, drag-to-move for views, dimensions and notes (their `at` is
// written back), and the dimension tool, which picks the edges the server labelled with
// selectors and places the dimension with a click.
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  useStore, select, getState, setDrawingTool, drawingPick, placeDimension, addNoteAt, moveDrawingItem, dimensionPlan, setDimKind, openContextMenu,
  type DimKind,
} from '../state/store';
import { sheetMenu } from '../menu/entries';
import type { DwgSeg, DwgText } from '../api/types';

const rad = (a: number) => (a * Math.PI) / 180;

/** SVG path data for a segment, in sheet coordinates (the enclosing group flips y). */
export function segPath(s: DwgSeg): string {
  if (s.k === 'l' || s.k === 'p') {
    const p = s.p ?? [];
    let d = `M ${p[0]} ${p[1]}`;
    for (let i = 2; i < p.length; i += 2) d += ` L ${p[i]} ${p[i + 1]}`;
    return d;
  }
  const [cx, cy] = s.c ?? [0, 0];
  const r = s.r ?? 0;
  if (s.k === 'c') return `M ${cx - r} ${cy} A ${r} ${r} 0 1 0 ${cx + r} ${cy} A ${r} ${r} 0 1 0 ${cx - r} ${cy}`;
  const [a0, a1] = s.a ?? [0, 360];
  const sweep = (((a1 - a0) % 360) + 360) % 360 || 360;
  return `M ${cx + r * Math.cos(rad(a0))} ${cy + r * Math.sin(rad(a0))} A ${r} ${r} 0 ${sweep > 180 ? 1 : 0} 1 ${cx + r * Math.cos(rad(a1))} ${cy + r * Math.sin(rad(a1))}`;
}

const ARROW = 2.5;
function arrowPoints(a: number[]): string {
  const [x, y, dx, dy] = a;
  const tx = x + dx * ARROW, ty = y + dy * ARROW, w = ARROW * 0.18;
  return `${x},${y} ${tx - dy * w},${ty + dx * w} ${tx + dy * w},${ty - dx * w}`;
}

interface Drag { kind: 'view' | 'dimension' | 'note' | 'pan'; name: string; start: [number, number]; client: [number, number]; vb: ViewBox; moved: boolean }
interface ViewBox { x: number; y: number; w: number; h: number }

export function DrawingSheet() {
  const tree = useStore((s) => s.tree);
  const selected = useStore((s) => s.selected);
  const tool = useStore((s) => s.drawingTool);
  const picks = useStore((s) => s.drawingPicks);
  const dimKind = useStore((s) => s.dimKind);
  const loading = useStore((s) => s.loading);
  const scene = tree?.evaluation.drawing ?? null;
  const svgRef = useRef<SVGSVGElement>(null);
  const [vb, setVb] = useState<ViewBox>({ x: -10, y: -10, w: 317, h: 230 });
  const [offset, setOffset] = useState<{ name: string; kind: string; dx: number; dy: number } | null>(null);
  const drag = useRef<Drag | null>(null);
  const W = scene?.sheet.width ?? 297, H = scene?.sheet.height ?? 210;

  // fit the sheet when the document changes
  useEffect(() => { setVb({ x: -10, y: -10, w: W + 20, h: H + 20 }); }, [tree?.id, W, H]);

  // wheel zoom about the cursor (a native listener, so the page does not scroll)
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const [sx, syn] = toNative(el, vbRef.current, e.clientX, e.clientY);
      const f = Math.exp(e.deltaY * 0.0015);
      setVb((v) => {
        const w = Math.min(Math.max(v.w * f, 20), 4000), h = v.h * (w / v.w);
        return { x: sx - (sx - v.x) * (w / v.w), y: syn - (syn - v.y) * (h / v.h), w, h };
      });
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);
  const vbRef = useRef(vb);
  vbRef.current = vb;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.closest('.cm-editor'))) return;
      if (e.key === 'Escape' && getState().drawingTool) setDrawingTool(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const toSheet = (clientX: number, clientY: number): [number, number] => {
    const el = svgRef.current;
    if (!el) return [0, 0];
    const [x, yn] = toNative(el, vb, clientX, clientY);
    return [x, H - yn];
  };

  const onDown = (e: React.PointerEvent<SVGSVGElement>) => {
    if (e.button !== 0 && e.button !== 1) return;
    const target = e.target as SVGElement;
    const item = target.closest('[data-item]') as SVGElement | null;
    const pick = target.closest('[data-ref]') as SVGElement | null;
    const p = toSheet(e.clientX, e.clientY);
    if (tool === 'dimension' && e.button === 0) {
      if (pick) { drawingPick(pick.dataset.ref!, pick.dataset.view!, pick.dataset.round === '1'); return; }
      if (getState().drawingPicks.length) { void placeDimension(p); return; }
    }
    if (tool === 'note' && e.button === 0) { void addNoteAt(p); return; }
    const kind = item?.dataset.kind as Drag['kind'] | undefined;
    drag.current = { kind: item && kind && e.button === 0 ? kind : 'pan', name: item?.dataset.item ?? '', start: p, client: [e.clientX, e.clientY], vb, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    if (d.kind === 'pan') {
      const el = svgRef.current!;
      const s = scaleOf(el, d.vb);
      setVb({ ...d.vb, x: d.vb.x - (e.clientX - d.client[0]) / s, y: d.vb.y - (e.clientY - d.client[1]) / s });
      d.moved = true;
      return;
    }
    const p = toSheet(e.clientX, e.clientY);
    const dx = p[0] - d.start[0], dy = p[1] - d.start[1];
    if (Math.hypot(dx, dy) > 0.3) d.moved = true;
    if (d.moved) setOffset({ name: d.name, kind: d.kind, dx, dy });
  };
  const onUp = (e: React.PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    drag.current = null;
    setOffset(null);
    if (!d) return;
    if (d.kind === 'pan') { if (!d.moved && !tool) select(null); return; }
    if (!d.moved) { select(d.name); return; }
    const p = toSheet(e.clientX, e.clientY);
    const dx = Math.round((p[0] - d.start[0]) * 10) / 10, dy = Math.round((p[1] - d.start[1]) * 10) / 10;
    select(d.name);
    void moveDrawingItem(d.name, [dx, dy]);
  };

  // a right-click selects the item under it and opens its menu; on the paper, the sheet's menu
  const onContext = (e: React.MouseEvent<SVGSVGElement>) => {
    e.preventDefault();
    const item = (e.target as SVGElement).closest('[data-item]') as SVGElement | null;
    const p = toSheet(e.clientX, e.clientY);
    const target = item?.dataset.item ? { kind: item.dataset.kind ?? '', name: item.dataset.item } : null;
    if (target) select(target.name);
    openContextMenu(e.clientX, e.clientY, sheetMenu(target, p), target ? `${target.kind} ${target.name}` : 'sheet');
  };

  const picked = useMemo(() => new Set(picks.map((p) => p.ref)), [picks]);
  const plan = tool === 'dimension' ? dimensionPlan() : null;
  if (!scene) return <div className="sheet-host" data-testid="sheet"><div className="panel-empty">{loading ? 'evaluating…' : 'no drawing'}</div></div>;

  // live offsets while dragging: a view carries the dimensions and notes placed relative to it
  const shift = (kind: string, name: string, view: string | null): [number, number] => {
    if (!offset) return [0, 0];
    if (offset.kind === kind && offset.name === name) return [offset.dx, offset.dy];
    if (offset.kind === 'view' && view === offset.name) return [offset.dx, offset.dy];
    return [0, 0];
  };
  const dimViewOf = (name: string) => scene.dimensions.find((d) => d.name === name)?.view ?? null;

  return (
    <div className="sheet-host" data-testid="sheet">
      <svg ref={svgRef} viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`} preserveAspectRatio="xMidYMid meet" className={`sheet ${tool ? 'tool' : ''}`}
           onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp} onContextMenu={onContext} data-testid="sheet-svg">
        <g transform={`translate(0 ${H}) scale(1 -1)`}>
          <rect className="sheet-paper" x={0} y={0} width={W} height={H} />
          {scene.frame.lines.map((l, i) => <line key={`f${i}`} className="dwg-frame" x1={l[0]} y1={l[1]} x2={l[2]} y2={l[3]} />)}
          {scene.views.map((v) => {
            const [dx, dy] = shift('view', v.name, null);
            const b = v.bbox;
            const sel = selected === v.name;
            return (
              <g key={v.name} transform={`translate(${dx} ${dy})`} data-testid={`dwg-view-${v.name}`}>
                <rect className={`dwg-view-hit ${sel ? 'selected' : ''}`} x={b[0] - 2} y={b[1] - 2} width={b[2] - b[0] + 4} height={b[3] - b[1] + 4}
                      data-item={v.name} data-kind="view" />
                {v.hatch.map((s, i) => <path key={`h${i}`} className="dwg-hatch" d={segPath(s)} />)}
                {v.hidden.map((s, i) => <path key={`d${i}`} className="dwg-hidden" d={segPath(s)} />)}
                {v.visible.map((s, i) => <path key={`v${i}`} className="dwg-visible" d={segPath(s)} />)}
                {(v.annotation ?? []).map((s, i) => <path key={`n${i}`} className="dwg-annotation" d={segPath(s)} />)}
                {(v.fills ?? []).map((p, i) => <polygon key={`f${i}`} className="dwg-fill" points={p.reduce((acc, c, j) => acc + (j % 2 ? `,${c}` : `${j ? ' ' : ''}${c}`), '')} />)}
                {v.traces.map((t, i) => (
                  <g key={`t${i}`} className="dwg-section">
                    <line x1={t.line[0]} y1={t.line[1]} x2={t.line[2]} y2={t.line[3]} />
                    {t.lines.map((l, j) => <line key={j} x1={l[0]} y1={l[1]} x2={l[2]} y2={l[3]} />)}
                    {t.arrows.map((a, j) => <polygon key={`a${j}`} className="dwg-section-arrow" points={arrowPoints(a)} />)}
                  </g>
                ))}
                {tool === 'dimension' && v.visible.filter((s) => s.ref).map((s, i) => (
                  <path key={`p${i}`} className={`dwg-pick ${picked.has(s.ref!) ? 'picked' : ''}`} d={segPath(s)}
                        data-ref={s.ref} data-view={v.name} data-round={s.k === 'c' || s.k === 'a' ? '1' : '0'} data-testid={`dwg-pick-${v.name}`}>
                    <title>{s.ref}</title>
                  </path>
                ))}
                {sel && <rect className="dwg-selected-box" x={b[0] - 2} y={b[1] - 2} width={b[2] - b[0] + 4} height={b[3] - b[1] + 4} />}
              </g>
            );
          })}
          {scene.dimensions.map((d) => {
            const [dx, dy] = shift('dimension', d.name, d.view);
            const sel = selected === d.name;
            return (
              <g key={d.name} className={`dwg-dim ${sel ? 'selected' : ''}`} transform={`translate(${dx} ${dy})`} data-item={d.name} data-kind="dimension" data-testid={`dwg-dim-${d.name}`}>
                {d.lines.map((l, i) => <line key={i} x1={l[0]} y1={l[1]} x2={l[2]} y2={l[3]} />)}
                {d.arc && <path d={segPath({ k: 'a', c: [d.arc[0], d.arc[1]], r: d.arc[2], a: [d.arc[3], d.arc[4]] })} />}
                {d.arrows.map((a, i) => <polygon key={`a${i}`} className="dwg-dim-arrow" points={arrowPoints(a)} />)}
                {d.lines.map((l, i) => <line key={`h${i}`} className="dwg-hit" x1={l[0]} y1={l[1]} x2={l[2]} y2={l[3]} />)}
              </g>
            );
          })}
        </g>
        {/* text is drawn upright in the sheet's own frame (y down), so it is outside the flipped group */}
        {scene.frame.texts.map((t, i) => <Text key={`ft${i}`} t={t} H={H} cls="dwg-text dwg-frame-text" />)}
        {scene.views.map((v) => {
          const [dx, dy] = shift('view', v.name, null);
          return (
            <g key={`vt${v.name}`} transform={`translate(${dx} ${-dy})`}>
              {v.label && v.label_at && <Text t={{ at: v.label_at, text: v.label, size: 3.5, anchor: 'middle' }} H={H} cls="dwg-text dwg-label" />}
              {v.traces.flatMap((tr) => tr.texts).map((t, i) => <Text key={i} t={t} H={H} cls="dwg-text" />)}
              {(v.texts ?? []).map((t, i) => <Text key={`x${i}`} t={t} H={H} cls="dwg-text" />)}
            </g>
          );
        })}
        {scene.dimensions.map((d) => {
          const [dx, dy] = shift('dimension', d.name, d.view);
          return d.label && (
            <g key={`dt${d.name}`} transform={`translate(${dx} ${-dy})`} data-item={d.name} data-kind="dimension">
              <Text t={{ at: d.label.at, text: d.label.text, size: 3.5, anchor: d.label.anchor, angle: d.label.angle }} H={H} cls={`dwg-text dwg-dim-text ${selected === d.name ? 'selected' : ''}`} testId={`dwg-dim-text-${d.name}`} />
            </g>
          );
        })}
        {scene.notes.map((n) => {
          const [dx, dy] = shift('note', n.name, n.view);
          return (
            <g key={`n${n.name}`} transform={`translate(${dx} ${-dy})`} data-item={n.name} data-kind="note" className={`dwg-note ${selected === n.name ? 'selected' : ''}`} data-testid={`dwg-note-${n.name}`}>
              {n.text.split('\n').map((line, i) => <Text key={i} t={{ at: [n.at[0], n.at[1] - i * n.size * 1.5], text: line, size: n.size, anchor: 'start' }} H={H} cls="dwg-text dwg-note-text" />)}
            </g>
          );
        })}
        {scene.dimensions.map((d) => {
          const [dx, dy] = shift('dimension', d.name, dimViewOf(d.name));
          return d.label && <circle key={`dh${d.name}`} className="dwg-hit-dot" cx={d.label.at[0] + dx} cy={H - d.label.at[1] - dy} r={3} data-item={d.name} data-kind="dimension" />;
        })}
      </svg>
      {tool && (
        <div className="tool-hint sheet-hint" data-testid="sheet-hint">
          {tool === 'dimension' ? (
            <>
              <span>dimension: {picks.length === 0 ? 'pick an edge' : plan && plan.ok ? `click where the ${plan.kind} goes` : plan && !plan.ok ? plan.why : ''}</span>
              <select className="tool-num" value={dimKind} onChange={(e) => setDimKind(e.target.value as DimKind)} data-testid="dim-kind">
                {['auto', 'distance', 'diameter', 'radius', 'angle'].map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
              <span className="mono">{picks.map((p) => p.ref).join(' · ')}</span>
              <span>· esc</span>
            </>
          ) : <span>note: click where the text goes · esc</span>}
        </div>
      )}
      {scene.model.error && <div className="tool-hint sheet-error" data-testid="sheet-error">{scene.model.error}</div>}
      {loading && <div className="viewport-badge">evaluating…</div>}
    </div>
  );
}

function Text({ t, H, cls, testId }: { t: DwgText; H: number; cls: string; testId?: string }) {
  const angle = t.angle ?? 0;
  return (
    <text className={cls} transform={`translate(${t.at[0]} ${H - t.at[1]})${angle ? ` rotate(${-angle})` : ''}`}
          fontSize={t.size / 0.72} textAnchor={t.anchor} data-testid={testId}>{t.text}</text>
  );
}

/** The viewBox scale for a "meet" fit in the element's box. */
function scaleOf(el: SVGSVGElement, vb: ViewBox): number {
  const r = el.getBoundingClientRect();
  return Math.min(r.width / vb.w, r.height / vb.h);
}

/** A client point in the svg's native (y down) sheet coordinates. */
function toNative(el: SVGSVGElement, vb: ViewBox, clientX: number, clientY: number): [number, number] {
  const r = el.getBoundingClientRect();
  const s = scaleOf(el, vb);
  const ox = (r.width - vb.w * s) / 2, oy = (r.height - vb.h * s) / 2;
  return [vb.x + (clientX - r.left - ox) / s, vb.y + (clientY - r.top - oy) / s];
}
