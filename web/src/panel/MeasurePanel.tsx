import { useState } from 'react';
import { useStore, measurePick, clearMeasure, pinMeasurement, removePin, fmt, setTool } from '../state/store';
import type { MeasureInfo, MeasureRef } from '../api/types';

function parseRef(text: string): MeasureRef | null {
  const t = text.trim();
  const m = /^(face|edge|vertex)\s*[:#]?\s*(\d+)$/.exec(t);
  if (m) return { [m[1]]: Number(m[2]) } as MeasureRef;
  const p = /^(?:point\s*[:]?\s*)?(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)$/.exec(t);
  if (p) return { point: [Number(p[1]), Number(p[2]), Number(p[3])] };
  return null;
}

const refText = (r: MeasureRef) => {
  const [k, v] = Object.entries(r)[0];
  return k === 'point' ? `point ${(v as number[]).map(fmt).join(', ')}` : `${k} ${v}`;
};

function Info({ label, info }: { label: string; info: MeasureInfo }) {
  const rows: [string, string][] = [];
  if (info.item) rows.push(['item', info.label && info.label !== info.item ? `${info.item} (${info.label})` : info.item]);
  if (info.type) rows.push(['type', info.type]);
  if (info.diameter !== undefined) rows.push(['diameter', `${fmt(info.diameter)} mm`]);
  else if (info.radius !== undefined) rows.push(['radius', `${fmt(info.radius)} mm`]);
  if (info.length !== undefined) rows.push(['length', `${fmt(info.length)} mm`]);
  if (info.area !== undefined) rows.push(['area', `${fmt(info.area)} mm²`]);
  if (info.position) rows.push(['position', info.position.map(fmt).join(', ')]);
  else if (info.center) rows.push(['center', info.center.map(fmt).join(', ')]);
  if (info.normal) rows.push(['normal', info.normal.map(fmt).join(', ')]);
  if (info.axis) rows.push(['axis', info.axis.map(fmt).join(', ')]);
  return (
    <div className="measure-entity">
      <div className="measure-label">{label}: {info.kind}{info.id !== undefined ? ` ${info.id}` : ''}</div>
      {rows.map(([k, v]) => <div key={k} className="measure-row"><span>{k}</span><span className="mono">{v}</span></div>)}
    </div>
  );
}

export function MeasurePanel() {
  const measure = useStore((s) => s.measure);
  const pins = useStore((s) => s.pins);
  const [a, setA] = useState('');
  const [b, setB] = useState('');
  const r = measure.result;
  const typed = async () => {
    const ra = parseRef(a);
    if (!ra) return;
    clearMeasure();
    await measurePick(ra);
    const rb = parseRef(b);
    if (rb) await measurePick(rb);
  };
  return (
    <div className="props measure" data-testid="measure-panel">
      <div className="props-title"><span>measure</span><button className="btn-small" onClick={() => setTool('measure')}>close</button></div>
      <div className="panel-help">click a face, edge or vertex in the viewport, then a second one. Or type references:</div>
      <div className="fields">
        <label className="field"><span>a</span><input value={a} placeholder="face:3 · edge:5 · vertex:2 · 0,0,0" onChange={(e) => setA(e.target.value)} data-testid="measure-a" /></label>
        <label className="field"><span>b</span><input value={b} placeholder="optional" onChange={(e) => setB(e.target.value)} data-testid="measure-b" /></label>
        <div className="btn-row">
          <button className="btn" onClick={typed} data-testid="measure-go">measure</button>
          <button className="btn" onClick={clearMeasure}>clear</button>
          <button className="btn" disabled={!r} onClick={pinMeasurement} data-testid="measure-pin">pin</button>
        </div>
      </div>
      {measure.pending && <div className="panel-help">measuring…</div>}
      {measure.picks.length > 0 && !r && !measure.pending && <div className="panel-help">picked {measure.picks.map(refText).join(' → ')}</div>}
      {r && (
        <div className="measure-result" data-testid="measure-result">
          {r.distance !== undefined && (
            <div className="measure-big">
              <div className="measure-distance">{fmt(r.distance)} <span>mm</span></div>
              {r.delta && <div className="measure-row"><span>Δx Δy Δz</span><span className="mono">{r.delta.map(fmt).join('  ')}</span></div>}
              {r.angle !== undefined && <div className="measure-row"><span>angle</span><span className="mono">{fmt(r.angle)}°</span></div>}
              {r.axis_distance !== undefined && <div className="measure-row"><span>axis distance</span><span className="mono">{fmt(r.axis_distance)} mm</span></div>}
              {r.center_distance !== undefined && <div className="measure-row"><span>centre distance</span><span className="mono">{fmt(r.center_distance)} mm</span></div>}
            </div>
          )}
          <Info label="a" info={r.a} />
          {r.b && <Info label="b" info={r.b} />}
        </div>
      )}
      <div className="props-title"><span>pinned</span></div>
      {pins.length === 0 && <div className="panel-help">pinned measurements stay in the view and in snapshots</div>}
      {pins.map((p, i) => (
        <div key={i} className="pin-row" data-testid="pin-row">
          <span className="mono">{p.text}</span>
          <span className="pin-refs">{refText(p.a)}{p.b ? ` → ${refText(p.b)}` : ''}</span>
          <button className="btn-small danger" onClick={() => removePin(i)} title="remove pin">×</button>
        </div>
      ))}
    </div>
  );
}
