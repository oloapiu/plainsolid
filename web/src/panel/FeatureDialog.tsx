import type { JsonValue } from '../api/types';
// The "+ feature" dialogs: fillet, chamfer and shell pick entities in the viewport; revolve,
// patterns and mirror act on a sketch or feature chosen in the tree. OK composes the
// add_feature op with selector expressions; the server writes the statement.
import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import {
  useStore, requestPick, openFeatureDialog, addFeature, featureByName, setDialogPicks, addInstance, addMate, fixInstance, instanceFeatures, refreshFiles,
  getState, selectorTarget, previewMate, clearPosePreview, previewFeature, clearFeaturePreview, featureOp,
  type PickTarget, type FeatureDialog as Dialog,
} from '../state/store';
import { api } from '../api/client';
import type { Feature, MateKind, MeasureRef } from '../api/types';
import { PickField, type Slot } from './PickField';
import { sceneRef } from '../viewport/Viewport';

const ROUND = new Set(['cylinder', 'cone', 'torus', 'sphere', 'circle']);
const measureRefOf = (t: PickTarget): MeasureRef | null => (t.kind === 'face' ? { face: t.id } : t.kind === 'edge' ? { edge: t.id } : t.kind === 'vertex' ? { vertex: t.id } : null);

const TITLES: Record<Dialog['kind'], string> = {
  fillet: 'fillet', chamfer: 'chamfer', shell: 'shell', revolve: 'revolve', linear_pattern: 'linear pattern',
  circular_pattern: 'circular pattern', mirror: 'mirror', instance: 'instance', mate: 'mate', extrude: 'extrude', cut: 'cut',
};
/** Dialogs whose result the viewport previews as the fields change. */
const PREVIEW_KINDS = new Set<Dialog['kind']>(['fillet', 'chamfer', 'shell', 'revolve', 'linear_pattern', 'circular_pattern', 'mirror', 'extrude', 'cut']);

/** The face or edge selected when a dialog opens, as a pick, when the field takes that kind. */
function selectedTarget(kind: 'face' | 'edge'): PickTarget | null {
  const st = getState();
  const id = kind === 'face' ? st.selectedFace : st.selectedEdge;
  const scene = sceneRef.current;
  const c = id !== null && scene ? scene.entityCenter({ kind, id }) : null;
  return id !== null && c ? selectorTarget({ kind, id }, c) : null;
}
const MATES: { kind: MateKind; label: string }[] = [
  { kind: 'coincident', label: 'coincident' }, { kind: 'concentric', label: 'concentric' }, { kind: 'distance', label: 'distance' },
  { kind: 'parallel', label: 'parallel' }, { kind: 'angle', label: 'angle' }, { kind: 'fixed', label: 'fixed' },
];
const MATERIALS = ['al6061', 'steel', 'stainless', 'brass', 'abs', 'pla', 'petg', 'nylon', 'pc', 'pom', 'glass', 'silicone', 'rubber'];
const AXES = ['X', 'Y', 'Z', '-X', '-Y', '-Z'];
const PLANES = ['XY', 'XZ', 'YZ'];

const numeric = (t: string) => (/^-?\d+(\.\d+)?$/.test(t.trim()) ? Number(t) : { expr: t.trim() });
const exprOf = (t: PickTarget) => (t.kind === 'plane' && t.standard ? t.name : { expr: t.expr });

/** Several picked entities as one selector expression: a list when there is more than one. */
function listExpr(picks: PickTarget[]): { expr: string } {
  const exprs = picks.map((p) => p.expr);
  return { expr: exprs.length === 1 ? exprs[0] : `[${exprs.join(', ')}]` };
}

function NumField({ label, value, onChange, testId }: { label: string; value: string; onChange: (v: string) => void; testId: string }) {
  return (
    <label className="field"><span>{label}</span>
      <input value={value} onChange={(e) => onChange(e.target.value)} data-testid={testId} spellCheck={false} />
    </label>
  );
}

const same = (a: PickTarget, b: PickTarget) => a.kind === b.kind && (a as { id?: number }).id === (b as { id?: number }).id;

/** A list of picks gathered by repeated viewport clicks (fillet edges, shell faces). The pick
 * request stays armed until the user presses escape or the dialog closes; clicking a picked
 * entity again removes it. Updates go through the setter's functional form, so the closure
 * handed to the store never sees a stale list. */
function MultiPick({ label, kinds, picks, setPicks, testId, autoArm }: {
  label: string; kinds: ('face' | 'edge' | 'vertex')[]; picks: PickTarget[]; setPicks: Dispatch<SetStateAction<PickTarget[]>>; testId: string; autoArm?: boolean;
}) {
  const active = useStore((s) => s.pickRequest);
  const [arming, setArming] = useState(false);
  const mine = arming && !!active;
  useEffect(() => { if (!active) setArming(false); }, [active]);
  useEffect(() => { if (autoArm) arm(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const arm = () => {
    if (mine) { requestPick(null); return; }
    setArming(true);
    requestPick({
      kinds, planes: false, multi: true, hint: `pick ${label} one after another · click again to unpick · esc when done`,
      onPick: (t) => {
        if (t.kind === 'plane') return;
        setPicks((prev) => (prev.some((p) => same(p, t)) ? prev.filter((p) => !same(p, t)) : [...prev, t]));
      },
    });
  };
  // keep the store's lit entities in step with this list
  useEffect(() => { setDialogPicks(picks.filter((p) => p.kind !== 'plane').map((p) => ({ kind: p.kind as 'face' | 'edge' | 'vertex', id: (p as { id: number }).id }))); }, [picks]);
  const remove = (i: number) => setPicks((prev) => prev.filter((_, j) => j !== i));
  return (
    <>
      <div className="pick-field" data-testid={testId}>
        <span className="pick-label">{label}</span>
        <span className={`pick-value ${picks.length ? '' : 'empty'}`}>{picks.length ? `${picks.length} picked` : 'nothing picked'}</span>
        <button className={`btn-small ${mine ? 'active' : ''}`} onClick={arm} title={mine ? 'stop picking' : 'click entities in the viewport, one after another'}>{mine ? `picking… (${picks.length})` : 'pick'}</button>
      </div>
      {picks.length > 0 && (
        <div className="pick-list">
          {picks.map((p, i) => <span key={i} className="pick-chip" title={p.expr}>{p.label}<button onClick={() => remove(i)} title="remove">×</button></span>)}
        </div>
      )}
    </>
  );
}

function AxisChoice({ label, value, onChange, pick, setPick, kinds, testId, extra }: {
  label: string; value: string; onChange: (v: string) => void; pick: Slot; setPick: (t: Slot) => void;
  kinds: ('face' | 'edge')[]; testId: string; extra?: { value: string; label: string }[];
}) {
  return (
    <>
      <label className="field inline"><span>{label}</span>
        <select value={value} onChange={(e) => { onChange(e.target.value); if (e.target.value !== '__pick') setPick(null); }} data-testid={testId}>
          {(extra ?? []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          {AXES.map((a) => <option key={a} value={a}>{a}</option>)}
          <option value="__pick">from the body…</option>
        </select>
      </label>
      {value === '__pick' && <PickField label="" value={pick} kinds={kinds} planes={false} onPick={setPick} testId={`${testId}-pick`} />}
    </>
  );
}

/** A mate reference: a face, edge or vertex picked in the viewport, or a standard plane or axis of an instance. */
function RefField({ label, value, onPick, testId }: { label: string; value: Slot; onPick: (t: PickTarget) => void; testId: string }) {
  const insts = instanceFeatures();
  const choose = (expr: string) => {
    if (!expr) return;
    onPick({ kind: 'plane', name: expr, expr, label: expr, standard: false });
  };
  return (
    <>
      <PickField label={label} value={value} kinds={['face', 'edge', 'vertex']} planes={false} onPick={onPick} testId={testId} />
      <label className="field inline"><span></span>
        <select className="pick-select" value="" onChange={(e) => choose(e.target.value)} title="a standard plane or axis of an instance" data-testid={`${testId}-select`}>
          <option value="">plane or axis…</option>
          {insts.map((f) => (
            <optgroup key={f.name} label={f.name}>
              {['XY', 'XZ', 'YZ'].map((p) => <option key={p} value={`${f.variable}.planes.${p}`}>{f.name} plane {p}</option>)}
              {['X', 'Y', 'Z'].map((a) => <option key={a} value={`${f.variable}.axes.${a}`}>{f.name} axis {a}</option>)}
            </optgroup>
          ))}
        </select>
      </label>
    </>
  );
}

export function FeatureDialog({ dialog }: { dialog: Dialog }) {
  const tree = useStore((s) => s.tree);
  const files = useStore((s) => s.files);
  const kind = dialog.kind;
  const target: Feature | null = featureByName(dialog.target);
  const [path, setPath] = useState('');
  const [color, setColor] = useState('');
  const [material, setMaterial] = useState('');
  const [mateKind, setMateKind] = useState<MateKind>('coincident');
  const [refA, setRefA] = useState<Slot>(null);
  const [refB, setRefB] = useState<Slot>(null);
  const [mateValue, setMateValue] = useState('0');
  const [flip, setFlip] = useState(false);
  const [fixTarget, setFixTarget] = useState(dialog.target ?? '');
  const [picks, setPicks] = useState<PickTarget[]>(() => {
    const t = kind === 'fillet' || kind === 'chamfer' ? selectedTarget('edge') : kind === 'shell' ? selectedTarget('face') : null;
    return t ? [t] : [];
  });
  const [depth, setDepth] = useState('10');
  const [through, setThrough] = useState(kind === 'cut');
  const [symmetric, setSymmetric] = useState(false);
  const [flipSide, setFlipSide] = useState(false);
  const [draft, setDraft] = useState('0');
  const [radius, setRadius] = useState('2');
  const [distance, setDistance] = useState('1');
  const [distance2, setDistance2] = useState('');
  const [thickness, setThickness] = useState('2');
  const [outward, setOutward] = useState(false);
  const [axisPick, setAxisPick] = useState<Slot>(() => (kind === 'revolve' ? selectedTarget('edge') : null));
  const [axis, setAxis] = useState(kind === 'revolve' ? (axisPick ? '__pick' : '') : 'Z');
  const [angle, setAngle] = useState('360');
  const [op, setOp] = useState<'add' | 'cut'>('add');
  const [count, setCount] = useState('3');
  const [spacing, setSpacing] = useState('10');
  const [direction, setDirection] = useState('X');
  const [dirPick, setDirPick] = useState<Slot>(null);
  const [second, setSecond] = useState(false);
  const [count2, setCount2] = useState('2');
  const [spacing2, setSpacing2] = useState('10');
  const [direction2, setDirection2] = useState('Y');
  const [dir2Pick, setDir2Pick] = useState<Slot>(null);
  const [aboutPick, setAboutPick] = useState<Slot>(() => (kind === 'mirror' ? selectedTarget('face') : null));
  const [about, setAbout] = useState(aboutPick ? '__pick' : 'YZ');
  const [busy, setBusy] = useState(false);
  const [kindTouched, setKindTouched] = useState(false);
  const previewNote = useStore((s) => s.previewNote);
  const refs = useRef({ a: null as Slot, b: null as Slot });
  refs.current = { a: refA, b: refB };
  const chosenFor = useRef('');

  // a mate: picking starts at once, a face selected beforehand is the first reference, and the
  // second pick follows the first; escape leaves pick mode and the "pick" buttons take over
  const armRef = (which: 'a' | 'b') => {
    requestPick({
      kinds: ['face', 'edge', 'vertex'], planes: false, hint: `pick reference ${which}: a face, edge or vertex of an instance`,
      onPick: (t) => {
        requestPick(null);
        if (which === 'a') { setRefA(t); if (!refs.current.b) armRef('b'); } else setRefB(t);
      },
    });
  };
  useEffect(() => {
    if (kind !== 'mate') return;
    const sel = getState().selectedFace;
    const scene = sceneRef.current;
    const c = sel !== null && scene ? scene.entityCenter({ kind: 'face', id: sel }) : null;
    const t = sel !== null && c ? selectorTarget({ kind: 'face', id: sel }, c) : null;
    if (t) { setRefA(t); armRef('b'); } else armRef('a');
    return () => { requestPick(null); clearPosePreview(); };
  }, [kind]);
  // both references stay lit while the mate is being made
  useEffect(() => {
    if (kind !== 'mate') return;
    setDialogPicks([refA, refB].flatMap((r) => (r && r.kind !== 'plane' ? [{ kind: r.kind, id: r.id }] : [])));
  }, [refA, refB, kind]);
  // the kind follows the picks until it is chosen by hand: two round entities are concentric
  useEffect(() => {
    if (kind !== 'mate' || kindTouched || !refA || !refB) return;
    const ma = measureRefOf(refA), mb = measureRefOf(refB);
    const id = getState().docId;
    if (!ma || !mb || !id) return;
    let live = true;
    api.measure(id, ma, mb, null, null).then((r) => {
      if (!live) return;
      const round = ROUND.has(r.a.type ?? '') && ROUND.has(r.b?.type ?? '');
      setMateKind(round ? 'concentric' : 'coincident');
    }).catch(() => {});
    return () => { live = false; };
  }, [refA, refB, kind, kindTouched]);
  // the preview: the assembly as it would be with the mate, refreshed as kind, value and flip change;
  // a new pair of references (or kind) lets the server choose the orientation that turns the parts less
  useEffect(() => {
    if (kind !== 'mate' || mateKind === 'fixed' || !refA || !refB) { if (kind === 'mate') clearPosePreview(); return; }
    const needs = mateKind === 'distance' || mateKind === 'angle';
    if (needs && !mateValue.trim()) return;
    const key = `${refA.expr}|${refB.expr}|${mateKind}`;
    const choose = chosenFor.current !== key && mateKind !== 'concentric' && mateKind !== 'parallel';
    const timer = setTimeout(() => {
      void previewMate(mateKind, refA.expr, refB.expr, needs ? numeric(mateValue) : undefined, flip, choose).then((r) => {
        if (r && choose) { chosenFor.current = key; if (typeof r.flip === 'boolean' && r.flip !== flip) setFlip(r.flip); }
      });
    }, choose ? 0 : 120);
    return () => clearTimeout(timer);
  }, [refA, refB, mateKind, mateValue, flip, kind]);

  const sketchLines = kind === 'revolve' && target ? target.entities.filter((e) => e.kind === 'line' || e.kind === 'project') : [];
  useEffect(() => { if (kind === 'instance') void refreshFiles(); }, [kind]);
  useEffect(() => { if (kind === 'revolve' && !axis) setAxis(sketchLines.find((e) => e.construction)?.name ?? sketchLines[0]?.name ?? 'Z'); }, [kind, target?.name]);
  const planeFeatures = (tree?.features ?? []).filter((f) => f.kind === 'plane' && f.variable);

  const axisValue = (v: string, p: Slot) => (v === '__pick' ? (p ? { expr: p.expr } : null) : sketchLines.some((e) => e.name === v) ? v : v);
  const needsValue = mateKind === 'distance' || mateKind === 'angle';
  const numberish = (t: string) => t.trim().length > 0;
  const ready = kind === 'fillet' || kind === 'chamfer' ? picks.length > 0
    : kind === 'extrude' ? !!target && numberish(depth)
    : kind === 'cut' ? !!target && (through || numberish(depth))
    : kind === 'shell' ? true
    : kind === 'instance' ? path.trim().length > 0
    : kind === 'mate' ? (mateKind === 'fixed' ? !!fixTarget : !!refA && !!refB && (!needsValue || mateValue.trim().length > 0))
    : kind === 'revolve' ? !!target && !!axisValue(axis, axisPick)
    : kind === 'mirror' ? (about !== '__pick' || !!aboutPick)
    : !!target && (kind === 'linear_pattern' ? (direction !== '__pick' || !!dirPick) && (!second || direction2 !== '__pick' || !!dir2Pick) : (axis !== '__pick' || !!axisPick));

  const ok = async () => {
    if (!ready || busy) return;
    setBusy(true);
    let args: Record<string, JsonValue> = {};
    if (kind === 'instance') { await addInstance(path.trim(), color.trim() || undefined, material.trim() || undefined); setBusy(false); return; }
    if (kind === 'mate') {
      if (mateKind === 'fixed') await fixInstance(fixTarget);
      else await addMate(mateKind, refA!.expr, refB!.expr, needsValue ? numeric(mateValue) : undefined, flip && mateKind !== 'concentric' && mateKind !== 'parallel');
      setBusy(false);
      return;
    }
    args = buildArgs() ?? {};
    await addFeature(kind, args, kind === 'extrude' || kind === 'cut' ? target?.name : undefined);
    setBusy(false);
  };

  /** The arguments the dialog's fields describe, or null while something is missing. */
  function buildArgs(): Record<string, JsonValue> | null {
    const targetExpr = target ? { expr: target.variable ?? target.name } : null;
    if (kind === 'fillet') return picks.length ? { edges: listExpr(picks), radius: numeric(radius) } : null;
    if (kind === 'chamfer') return picks.length ? { edges: listExpr(picks), distance: numeric(distance), ...(distance2.trim() ? { distance2: numeric(distance2) } : {}) } : null;
    if (kind === 'shell') return { faces: picks.length ? listExpr(picks) : null, thickness: numeric(thickness), outward };
    if (kind === 'extrude' || kind === 'cut') {
      if (!target) return null;
      const a: Record<string, JsonValue> = { sketch: target.variable ?? target.name };
      if (kind === 'cut' && through) a.through = true; else a.depth = numeric(depth);
      if (symmetric) a.symmetric = true;
      if (flipSide) a.flip = true;
      if (kind === 'extrude' && draft.trim() && draft.trim() !== '0') a.draft = numeric(draft);
      return a;
    }
    if (kind === 'revolve') return target && axisValue(axis, axisPick) ? { sketch: target.variable ?? target.name, axis: axisValue(axis, axisPick), angle: numeric(angle), op } : null;
    if (kind === 'linear_pattern') {
      if (!target || (direction === '__pick' && !dirPick) || (second && direction2 === '__pick' && !dir2Pick)) return null;
      const a: Record<string, JsonValue> = { feature: targetExpr, count: Number(count), spacing: numeric(spacing), direction: direction === '__pick' ? { expr: dirPick!.expr } : direction };
      if (second) Object.assign(a, { count2: Number(count2), spacing2: numeric(spacing2), direction2: direction2 === '__pick' ? { expr: dir2Pick!.expr } : direction2 });
      return a;
    }
    if (kind === 'circular_pattern') return target && (axis !== '__pick' || axisPick) ? { feature: targetExpr, count: Number(count), axis: axis === '__pick' ? { expr: axisPick!.expr } : axis, angle: numeric(angle) } : null;
    if (kind === 'mirror') return about === '__pick' && !aboutPick ? null : { ...(target ? { feature: targetExpr } : {}), about: about === '__pick' ? exprOf(aboutPick!) : PLANES.includes(about) ? about : { expr: featureByName(about)?.variable ?? about } };
    return null;
  }

  // the viewport previews the feature as the fields change, nothing written until "add"
  const previewSig = JSON.stringify([kind, picks.map((p) => p.expr), radius, distance, distance2, thickness, outward, axis, axisPick?.expr, angle, op, count, spacing, direction,
                                     dirPick?.expr, second, count2, spacing2, direction2, dir2Pick?.expr, about, aboutPick?.expr, depth, through, symmetric, flipSide, draft, ready]);
  useEffect(() => {
    if (!PREVIEW_KINDS.has(kind)) return;
    if (!ready) { clearFeaturePreview(); return; }
    const args = buildArgs();
    const opDef = args ? featureOp(kind, args, kind === 'extrude' || kind === 'cut' ? target?.name : undefined) : null;
    if (!opDef) return;
    const timer = setTimeout(() => { void previewFeature(opDef); }, 150);
    return () => clearTimeout(timer);
  }, [previewSig]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => clearFeaturePreview(), []);

  return (
    <div className="props plane-dialog" data-testid="feature-dialog">
      <div className="props-title">
        <span>new <b>{TITLES[kind]}</b>{target ? <span className="panel-help"> of {target.name}</span> : null}</span>
        <button className="btn-small" onClick={() => openFeatureDialog(null)}>cancel</button>
      </div>
      <div className="fields">
        {(kind === 'fillet' || kind === 'chamfer') && <MultiPick label="edges" kinds={['edge']} picks={picks} setPicks={setPicks} testId="feature-edges" autoArm />}
        {kind === 'fillet' && <NumField label="radius" value={radius} onChange={setRadius} testId="feature-radius" />}
        {kind === 'chamfer' && (
          <>
            <NumField label="distance" value={distance} onChange={setDistance} testId="feature-distance" />
            <NumField label="distance 2" value={distance2} onChange={setDistance2} testId="feature-distance2" />
            <div className="panel-help">leave distance 2 empty for an equal chamfer</div>
          </>
        )}
        {kind === 'shell' && (
          <>
            <MultiPick label="open faces" kinds={['face']} picks={picks} setPicks={setPicks} testId="feature-faces" autoArm />
            <NumField label="thickness" value={thickness} onChange={setThickness} testId="feature-thickness" />
            <label className="field check"><input type="checkbox" checked={outward} onChange={(e) => setOutward(e.target.checked)} /><span>walls grow outward</span></label>
            <div className="panel-help">no open faces hollows the body closed</div>
          </>
        )}
        {kind === 'revolve' && (
          <>
            {!target && <div className="props-error">select a sketch first</div>}
            <AxisChoice label="axis" value={axis} onChange={setAxis} pick={axisPick} setPick={setAxisPick} kinds={['edge', 'face']} testId="feature-axis"
                        extra={sketchLines.map((e) => ({ value: e.name, label: `${e.name}${e.construction ? ' (construction)' : ''}` }))} />
            <NumField label="angle °" value={angle} onChange={setAngle} testId="feature-angle" />
            <label className="field inline"><span>operation</span>
              <select value={op} onChange={(e) => setOp(e.target.value as 'add' | 'cut')} data-testid="feature-op"><option value="add">add material</option><option value="cut">cut material</option></select>
            </label>
            <div className="panel-help">the axis must lie in the sketch plane, the profile on one side of it</div>
          </>
        )}
        {kind === 'linear_pattern' && (
          <>
            {!target && <div className="props-error">select an extrude, cut, revolve or pattern first</div>}
            <NumField label="count" value={count} onChange={setCount} testId="feature-count" />
            <NumField label="spacing" value={spacing} onChange={setSpacing} testId="feature-spacing" />
            <AxisChoice label="direction" value={direction} onChange={setDirection} pick={dirPick} setPick={setDirPick} kinds={['edge']} testId="feature-direction" />
            <label className="field check"><input type="checkbox" checked={second} onChange={(e) => setSecond(e.target.checked)} data-testid="feature-second" /><span>second direction</span></label>
            {second && (
              <>
                <NumField label="count 2" value={count2} onChange={setCount2} testId="feature-count2" />
                <NumField label="spacing 2" value={spacing2} onChange={setSpacing2} testId="feature-spacing2" />
                <AxisChoice label="direction 2" value={direction2} onChange={setDirection2} pick={dir2Pick} setPick={setDir2Pick} kinds={['edge']} testId="feature-direction2" />
              </>
            )}
          </>
        )}
        {kind === 'circular_pattern' && (
          <>
            {!target && <div className="props-error">select an extrude, cut, revolve or pattern first</div>}
            <NumField label="count" value={count} onChange={setCount} testId="feature-count" />
            <AxisChoice label="axis" value={axis} onChange={setAxis} pick={axisPick} setPick={setAxisPick} kinds={['edge', 'face']} testId="feature-axis" />
            <NumField label="angle °" value={angle} onChange={setAngle} testId="feature-angle" />
            <div className="panel-help">360 spreads the copies evenly; less spreads them over that arc</div>
          </>
        )}
        {kind === 'instance' && (
          <>
            <label className="field"><span>file</span>
              <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="lid.py or vendor/gland.step" list="project-instances" data-testid="instance-path" spellCheck={false} autoFocus />
            </label>
            <datalist id="project-instances">{files.filter((f) => f.kind !== 'assembly').map((f) => <option key={f.path} value={f.path}>{f.kind === 'step' ? 'STEP file' : 'part'}</option>)}</datalist>
            <label className="field"><span>colour</span>
              <input value={color} onChange={(e) => setColor(e.target.value)} placeholder="#rrggbb (optional)" data-testid="instance-color-input" spellCheck={false} />
            </label>
            <label className="field inline"><span>material</span>
              <select value={material} onChange={(e) => setMaterial(e.target.value)} data-testid="instance-material-select">
                <option value="">the part's own</option>
                {MATERIALS.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <div className="panel-help">a part file is evaluated in its own coordinates; a STEP file is a rigid solid with geometric references only. The instance appears at the origin: mate it into place.</div>
          </>
        )}
        {kind === 'mate' && (
          <>
            <label className="field inline"><span>mate</span>
              <select value={mateKind} onChange={(e) => { setKindTouched(true); setMateKind(e.target.value as MateKind); }} data-testid="mate-kind">
                {MATES.map((m) => <option key={m.kind} value={m.kind}>{m.label}</option>)}
              </select>
            </label>
            {mateKind === 'fixed' ? (
              <label className="field inline"><span>instance</span>
                <select value={fixTarget} onChange={(e) => setFixTarget(e.target.value)} data-testid="mate-fix-target">
                  <option value="">choose…</option>
                  {instanceFeatures().map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
                </select>
              </label>
            ) : (
              <>
                <RefField label="a" value={refA} onPick={setRefA} testId="mate-a" />
                <RefField label="b" value={refB} onPick={setRefB} testId="mate-b" />
                {needsValue && <NumField label={mateKind === 'angle' ? 'angle °' : 'distance'} value={mateValue} onChange={setMateValue} testId="mate-value" />}
                {mateKind !== 'concentric' && mateKind !== 'parallel' && (
                  <label className="field check"><span>flip</span><input type="checkbox" checked={flip} onChange={(e) => setFlip(e.target.checked)} data-testid="mate-flip-input" /></label>
                )}
                {previewNote && <div className={`sketch-status ${previewNote.startsWith('preview') ? 'ok' : ''}`} data-testid="mate-preview">{previewNote}</div>}
                <div className="panel-help">pick a face, edge or vertex of each instance in the viewport, or choose one of its planes or axes. The parts move to show the mate; the orientation that turns them less is chosen, flip reverses it. Nothing is written until you add the mate.</div>
              </>
            )}
          </>
        )}
        {(kind === 'extrude' || kind === 'cut') && (
          <>
            <div className="panel-help">{target ? `${kind === 'cut' ? 'cuts with' : 'extrudes'} ${target.name}` : 'needs a sketch'}</div>
            {kind === 'cut' && <label className="field check"><span>through all</span><input type="checkbox" checked={through} onChange={(e) => setThrough(e.target.checked)} data-testid="feature-through" /></label>}
            {!(kind === 'cut' && through) && <NumField label="depth" value={depth} onChange={setDepth} testId="feature-depth" />}
            <label className="field check"><span>symmetric</span><input type="checkbox" checked={symmetric} onChange={(e) => setSymmetric(e.target.checked)} data-testid="feature-symmetric" /></label>
            <label className="field check"><span>flip side</span><input type="checkbox" checked={flipSide} onChange={(e) => setFlipSide(e.target.checked)} data-testid="feature-flip" /></label>
            {kind === 'extrude' && <NumField label="draft °" value={draft} onChange={setDraft} testId="feature-draft" />}
            <div className="panel-help">the viewport shows the result; add writes it after the sketch and closes the sketch</div>
          </>
        )}
        {kind === 'mirror' && (
          <>
            <div className="panel-help">{target ? `mirrors ${target.name}` : 'mirrors the whole body'}</div>
            <label className="field inline"><span>about</span>
              <select value={about} onChange={(e) => { setAbout(e.target.value); if (e.target.value !== '__pick') setAboutPick(null); }} data-testid="feature-about">
                {PLANES.map((p) => <option key={p} value={p}>{p}</option>)}
                {planeFeatures.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
                <option value="__pick">a planar face…</option>
              </select>
            </label>
            {about === '__pick' && <PickField label="" value={aboutPick} kinds={['face']} planes onPick={setAboutPick} testId="feature-about-pick" />}
          </>
        )}
        {PREVIEW_KINDS.has(kind) && previewNote && <div className={`sketch-status ${previewNote.startsWith('preview') ? 'ok' : ''}`} data-testid="feature-preview">{previewNote}</div>}
        <div className="btn-row">
          <button className="btn" disabled={!ready || busy} onClick={ok} data-testid="feature-ok">add {TITLES[kind]}</button>
          <button className="btn" onClick={() => openFeatureDialog(null)} title="close without adding" data-testid="feature-cancel">cancel</button>
        </div>
      </div>
    </div>
  );
}
