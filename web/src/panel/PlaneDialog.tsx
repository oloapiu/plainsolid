import type { JsonValue } from '../api/types';
// The "+ plane" dialog: four forms, each field filled by picking in the viewport or choosing a
// standard plane. OK composes the add_feature op with selector expressions.
import { useState } from 'react';
import { openPlaneDialog, addPlane, getState, selectorTarget, featureByName, type PickTarget, type PlaneForm } from '../state/store';
import { PickField, type Slot } from './PickField';
import { sceneRef } from '../viewport/Viewport';

/** The face (or plane feature) selected when the dialog opened, as a pick, or null. */
function selectedFaceTarget(): Slot {
  const st = getState();
  const sel = st.selectedFace;
  const scene = sceneRef.current;
  const c = sel !== null && scene ? scene.entityCenter({ kind: 'face', id: sel }) : null;
  if (sel !== null && c) return selectorTarget({ kind: 'face', id: sel }, c);
  const f = featureByName(st.selected);
  if (f?.kind === 'plane' && f.variable && f.result?.plane) return { kind: 'plane', name: f.name, expr: f.variable, label: `plane ${f.name}`, standard: false };
  return null;
}

/** The edge selected when the dialog opened (the angle form turns about it), or null. */
function selectedEdgeTarget(): Slot {
  const sel = getState().selectedEdge;
  const scene = sceneRef.current;
  const c = sel !== null && scene ? scene.entityCenter({ kind: 'edge', id: sel }) : null;
  return sel !== null && c ? selectorTarget({ kind: 'edge', id: sel }, c) : null;
}

const FORMS: { id: PlaneForm; label: string }[] = [
  { id: 'offset', label: 'offset' }, { id: 'angle', label: 'angle' }, { id: 'midplane', label: 'midplane' }, { id: 'through', label: 'through' },
];

export function PlaneDialog({ form }: { form: PlaneForm }) {
  const [base, setBase] = useState<Slot>(() => (form === 'offset' || form === 'angle' ? selectedFaceTarget() : null));
  const [edge, setEdge] = useState<Slot>(() => (form === 'angle' ? selectedEdgeTarget() : null));
  const [a, setA] = useState<Slot>(() => (form === 'midplane' ? selectedFaceTarget() : null));
  const [b, setB] = useState<Slot>(null);
  const [pts, setPts] = useState<[Slot, Slot, Slot]>([null, null, null]);
  const [offset, setOffset] = useState('10');
  const [angle, setAngle] = useState('45');
  const [flip, setFlip] = useState(false);
  const [busy, setBusy] = useState(false);

  const exprOf = (t: PickTarget) => (t.kind === 'plane' && t.standard ? t.name : { expr: t.expr });
  const numeric = (t: string) => (/^-?\d+(\.\d+)?$/.test(t.trim()) ? Number(t) : { expr: t.trim() });
  const ready = form === 'offset' ? !!base : form === 'angle' ? !!base && !!edge : form === 'midplane' ? !!a && !!b : pts.every(Boolean);

  const ok = async () => {
    if (!ready || busy) return;
    setBusy(true);
    let args: Record<string, JsonValue>;
    if (form === 'offset') args = { base: exprOf(base!), offset: numeric(offset), flip };
    else if (form === 'angle') args = { base: exprOf(base!), angle: numeric(angle), about: { expr: edge!.expr }, flip };
    else if (form === 'midplane') args = { between: { expr: `(${a!.expr}, ${b!.expr})` }, flip };
    else args = { through: { expr: `(${pts.map((p) => p!.expr).join(', ')})` }, flip };
    await addPlane(args);
    setBusy(false);
  };

  return (
    <div className="props plane-dialog" data-testid="plane-dialog">
      <div className="props-title"><span>new <b>plane</b></span><button className="btn-small" onClick={() => openPlaneDialog(null)}>cancel</button></div>
      <div className="tabs small">
        {FORMS.map((f) => <button key={f.id} className={form === f.id ? 'active' : ''} onClick={() => openPlaneDialog(f.id)} data-testid={`plane-form-${f.id}`}>{f.label}</button>)}
      </div>
      <div className="fields">
        {form === 'offset' && (
          <>
            <PickField label="from" value={base} kinds={['face']} planes onPick={setBase} testId="plane-base" autoArm />
            <label className="field"><span>offset</span><input value={offset} onChange={(e) => setOffset(e.target.value)} data-testid="plane-offset" spellCheck={false} /></label>
            <div className="panel-help">a plane or planar face, moved along its normal</div>
          </>
        )}
        {form === 'angle' && (
          <>
            <PickField label="base" value={base} kinds={['face']} planes onPick={setBase} testId="plane-base" autoArm={!base} />
            <PickField label="about edge" value={edge} kinds={['edge']} planes={false} onPick={setEdge} testId="plane-edge" autoArm={!!base && !edge} />
            <label className="field"><span>angle °</span><input value={angle} onChange={(e) => setAngle(e.target.value)} data-testid="plane-angle" spellCheck={false} /></label>
            <div className="panel-help">rotated about a straight edge that lies on the base</div>
          </>
        )}
        {form === 'midplane' && (
          <>
            <PickField label="first" value={a} kinds={['face']} planes onPick={setA} testId="plane-a" />
            <PickField label="second" value={b} kinds={['face']} planes onPick={setB} testId="plane-b" />
            <div className="panel-help">halfway between two parallel faces or planes</div>
          </>
        )}
        {form === 'through' && (
          <>
            {([0, 1, 2] as const).map((i) => (
              <PickField key={i} label={`point ${i + 1}`} value={pts[i]} kinds={['vertex']} planes={false} testId={`plane-p${i + 1}`}
                         onPick={(t) => setPts((prev) => { const next = [...prev] as [Slot, Slot, Slot]; next[i] = t; return next; })} />
            ))}
            <div className="panel-help">through three vertices</div>
          </>
        )}
        <label className="field check"><input type="checkbox" checked={flip} onChange={(e) => setFlip(e.target.checked)} /><span>flip normal</span></label>
        <div className="btn-row">
          <button className="btn" disabled={!ready || busy} onClick={ok} data-testid="plane-ok">add plane</button>
        </div>
      </div>
    </div>
  );
}
