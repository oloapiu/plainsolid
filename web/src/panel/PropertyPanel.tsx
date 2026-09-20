import { useEffect, useMemo, useRef, useState } from 'react';
import {
  useStore, edit, select, enterSketch, exitSketch, featureByName, instanceByPath, leafPaths, setVisibility, setTransparency, isAssembly,
  expressionNames, setSketchHighlight, deleteConstraint, setConstraintValue, unknownNames, setError, deleteFeature, openFeatureDialog, setSuppressed,
  fixInstance, fetchAssemblyQueries, fmt, makeEditable, openDocument, isDrawing, exportDocument, modelDocPath, setMeta,
  setOverlay, isViewer, setDeleteConfirm, fetchSummary, toggleSketchSelect, hoverRefs,
  addConstraint, beginDimension, toggleConstructionSelection, setSketchTool, deleteSketchSelection, convertBodySelection, toggleBodySelect,
} from '../state/store';
import { ParamPanel } from '../params/ParamPanel';
import { PlaneDialog } from './PlaneDialog';
import { FeatureDialog } from './FeatureDialog';
import { MATE_KINDS, TOOL_KINDS, VIEW_DIRECTIONS, DIMENSION_KINDS, type CompareRegion, type Constraint, type Entity, type Feature, type EditValue, type Overlay, type Tree } from '../api/types';
import { MeasurePanel } from './MeasurePanel';
import { ExprInput } from './ExprInput';
import { GLYPH, buildModel, validConstraints, dimensionFor, entityOf, refKind } from '../sketch/model';
import { subAssemblyOf, openSubAssembly } from '../menu/entries';

function parseValue(text: string): EditValue {
  const t = text.trim();
  if (t === '') return null;
  if (t === 'true' || t === 'false') return t === 'true';
  if (/^-?\d+(\.\d+)?$/.test(t)) return { expr: t };  // numbers go as typed, so "4.0" stays "4.0" in the file
  const bad = unknownNames(t);
  if (bad.length) { setError(`unknown name${bad.length > 1 ? 's' : ''} in expression: ${bad.join(', ')}`); return null; }
  return { expr: t };
}

function ExprField({ label, text, onCommit, disabled }: { label: string; text: string; onCommit: (v: EditValue) => void; disabled?: boolean }) {
  return (
    <label className="field">
      <span>{label}</span>
      <ExprInput text={text} names={expressionNames()} disabled={disabled} onCommit={(t) => { const v = parseValue(t); if (v !== null) onCommit(v); }} />
    </label>
  );
}

function BoolField({ label, value, onCommit, disabled, testId }: { label: string; value: boolean; onCommit: (v: boolean) => void; disabled?: boolean; testId?: string }) {
  return (
    <label className="field check">
      <span>{label}</span>
      <input type="checkbox" checked={value} disabled={disabled} onChange={(e) => onCommit(e.target.checked)} data-testid={testId} />
    </label>
  );
}

/** A read-only line showing a selector or reference expression as written in the file; hovering
 * it lights what the expression picks in the viewport. */
function RefLine({ label, text, noHover, fallback }: { label: string; text: string | undefined; noHover?: boolean; fallback?: string }) {
  const refs = text && !noHover ? [text] : null;
  return (
    <div className="measure-row" onMouseEnter={() => hoverRefs(refs, fallback ?? null)} onMouseLeave={() => hoverRefs(null)} data-testid={refs ? 'ref-line' : undefined}>
      <span>{label}</span><span className={`mono refs ${refs ? 'hoverable' : ''}`} title={refs ? `${text} · hover lights what it picks` : text}>{text ?? '—'}</span>
    </div>
  );
}

export function PropertyPanel() {
  const tree = useStore((s) => s.tree);
  const selected = useStore((s) => s.selected);
  const selectedItem = useStore((s) => s.selectedItem);
  const selectedFace = useStore((s) => s.selectedFace);
  const selectedEdge = useStore((s) => s.selectedEdge);
  const mesh = useStore((s) => s.mesh);
  const sketchMode = useStore((s) => s.sketchMode);
  const tool = useStore((s) => s.tool);
  const planeDialog = useStore((s) => s.planeDialog);
  const featureDialog = useStore((s) => s.featureDialog);
  const visibility = useStore((s) => s.visibility);
  const transparency = useStore((s) => s.transparency);
  const overlay = useStore((s) => s.overlay);
  const f = featureByName(selected);
  if (!tree) return <div className="panel-empty">no document</div>;
  if (!f && overlay && !isDrawing(tree)) return <ComparePanel tree={tree} overlay={overlay} />;
  if (tool === 'measure') return <MeasurePanel />;
  if (planeDialog) return <PlaneDialog form={planeDialog} />;
  if (featureDialog) return <FeatureDialog key={`${featureDialog.kind}:${featureDialog.target ?? ''}`} dialog={featureDialog} />;
  const inst = instanceByPath(selectedItem);
  if (inst) {
    const leaves = leafPaths(inst);
    const visible = leaves.some((p) => visibility[p] ?? true);
    const transparent = leaves.every((p) => transparency[p] ?? false);
    const color = inst.color ? `rgb(${inst.color.slice(0, 3).map((c) => Math.round(c * 255)).join(',')})` : null;
    const sub = inst.children.length ? subAssemblyOf(inst) : null;
    return (
      <div className="props" data-testid="instance-props">
        <div className="props-title"><span>{inst.children.length ? 'assembly' : 'instance'} <b>{inst.name}</b></span></div>
        <div className="fields">
          <div className="measure-row"><span>path</span><span className="mono">{inst.path}</span></div>
          <div className="measure-row"><span>product</span><span className="mono">{inst.product}</span></div>
          {inst.children.length > 0 && <div className="measure-row"><span>children</span><span className="mono">{inst.children.length}</span></div>}
          {inst.children.length === 0 && <div className="measure-row"><span>solids</span><span className="mono">{inst.solids}</span></div>}
          <div className="measure-row"><span>colour</span><span><span className="swatch" style={{ background: color ?? 'transparent' }} /> {inst.color ? inst.color.slice(0, 3).map((c) => c.toFixed(2)).join(', ') : 'none'}</span></div>
          {selectedFace !== null && <div className="measure-row"><span>face</span><span className="mono">{selectedFace}</span></div>}
          <BoolField label="visible" value={visible} onCommit={(v) => leaves.forEach((p) => setVisibility(p, v))} />
          <BoolField label="transparent" value={transparent} onCommit={(v) => leaves.forEach((p) => setTransparency(p, v))} />
          {sub && <div className="btn-row"><button className="btn" onClick={() => openSubAssembly(sub)} title="open this sub-assembly in a tab of its own, in its own coordinates" data-testid="open-sub-assembly">open sub-assembly</button></div>}
          <div className="panel-help">{inst.children.length ? 'a sub-assembly of the STEP file' : 'a body of the STEP file'}: "make editable" in the tree header turns the file's bodies into instances that can be moved and edited</div>
        </div>
      </div>
    );
  }
  if (!f && isViewer(tree)) return <ViewerPanel tree={tree} />;
  if (!f && isAssembly(tree)) return <AssemblyPanel tree={tree} />;
  if (!f && isDrawing(tree)) return <DrawingPanel tree={tree} />;
  if (!f) return <PartPanel tree={tree} />;
  const ro = f.read_only;
  const setArg = (kwarg: string, value: EditValue) => edit({ op: 'set_argument', feature: f.name, kwarg, value });
  const isTool = TOOL_KINDS.includes(f.kind);
  const faceTags = selectedFace !== null ? (mesh?.header.face_tags?.[String(selectedFace)] ?? []) : [];
  const isMate = MATE_KINDS.includes(f.kind);
  const isDwg = f.kind === 'view' || f.kind === 'dimension' || f.kind === 'note';
  const timing = f.result && f.kind !== 'sketch' && f.kind !== 'plane' && !isMate && f.kind !== 'dimension' && f.kind !== 'note'
    ? f.kind === 'view' ? `${f.result.faces_created} visible edges, ${(f.result.seconds * 1000).toFixed(0)} ms`
    : `${f.result.faces_created} faces${f.kind === 'instance' ? '' : ' created'}, ${(f.result.seconds * 1000).toFixed(0)} ms` : '';

  return (
    <div className="props">
      <div className="props-title">
        <span>{f.kind.replace('_', ' ')} <b>{f.name}</b></span>
        {!ro && <DeleteButton f={f} />}
      </div>
      {ro && <div className="panel-help">read-only: created in a loop or function. Edit it in the code pane.</div>}
      {f.result?.error && <div className="props-error">{f.result.error.message}</div>}
      {f.result?.warnings.map((w, i) => <div key={i} className="props-warn">{w}</div>)}
      {selectedFace !== null && <div className="panel-help">selected face {selectedFace}{faceTags.length ? `: ${faceTags.map((t) => t.replace(/^:/, '')).join(' ')}` : ''}</div>}
      {selectedEdge !== null && <EdgeInfo edge={selectedEdge} />}
      {f.kind === 'sketch' && <SketchProps f={f} inSketch={sketchMode?.sketch === f.name} />}
      {f.kind === 'plane' && (
        <div className="fields" data-testid="plane-props">
          <div className="mono refs">{planeDefinition(f)}</div>
          {'offset' in f.arg_texts && <ExprField label="offset" text={f.arg_texts.offset} disabled={ro} onCommit={(v) => setArg('offset', v)} />}
          {'angle' in f.arg_texts && <ExprField label="angle °" text={f.arg_texts.angle} disabled={ro} onCommit={(v) => setArg('angle', v)} />}
          <BoolField label="flip normal" value={Boolean(f.args.flip)} disabled={ro} onCommit={(v) => setArg('flip', v)} />
          {f.result?.plane && <div className="panel-help">origin {f.result.plane.origin.map((c) => c.toFixed(2)).join(', ')} · normal {f.result.plane.z_dir.map((c) => c.toFixed(2)).join(', ')}</div>}
          <div className="panel-help">select this plane, then + sketch to draw on it</div>
        </div>
      )}
      {(f.kind === 'extrude' || f.kind === 'cut') && (
        <div className="fields">
          <div className="panel-help">sketch: {String(f.args.sketch)}</div>
          {f.args.upto ? <RefLine label="up to" text={f.arg_texts.upto} /> : (
            <>
              {(f.kind === 'cut' || f.args.op === 'cut') && <BoolField label="through all" value={Boolean(f.args.through)} disabled={ro}
                onCommit={(v) => v ? setArg('through', true) : setArg('depth', 10).then(() => setArg('through', false))} />}
              {!f.args.through && (
                <ExprField label="depth" text={f.arg_texts.depth ?? String(f.args.depth ?? '')} disabled={ro} onCommit={(v) => setArg('depth', v)} />
              )}
            </>
          )}
          {!f.args.upto && <BoolField label="symmetric" value={Boolean(f.args.symmetric)} disabled={ro} onCommit={(v) => setArg('symmetric', v)} />}
          <BoolField label="flip" value={Boolean(f.args.flip)} disabled={ro} onCommit={(v) => setArg('flip', v)} />
          {f.kind === 'extrude' && <ExprField label="draft °" text={f.arg_texts.draft ?? String(f.args.draft ?? 0)} disabled={ro} onCommit={(v) => setArg('draft', v)} />}
          {f.kind === 'extrude' && (
            <label className="field inline"><span>operation</span>
              <select value={String(f.args.op ?? 'add')} disabled={ro} onChange={(e) => setArg('op', e.target.value)}><option value="add">add material</option><option value="cut">cut material</option></select>
            </label>
          )}
        </div>
      )}
      {f.kind === 'revolve' && (
        <div className="fields">
          <div className="panel-help">sketch: {String(f.args.sketch)}</div>
          <RefLine label="axis" text={f.arg_texts.axis} />
          <ExprField label="angle °" text={f.arg_texts.angle ?? String(f.args.angle ?? 360)} disabled={ro} onCommit={(v) => setArg('angle', v)} />
          <label className="field inline"><span>operation</span>
            <select value={String(f.args.op ?? 'add')} disabled={ro} onChange={(e) => setArg('op', e.target.value)}><option value="add">add material</option><option value="cut">cut material</option></select>
          </label>
        </div>
      )}
      {f.kind === 'fillet' && (
        <div className="fields">
          <RefLine label="edges" text={f.arg_texts.edges} fallback={f.name} />
          <ExprField label="radius" text={f.arg_texts.radius ?? String(f.args.radius ?? '')} disabled={ro} onCommit={(v) => setArg('radius', v)} />
        </div>
      )}
      {f.kind === 'chamfer' && (
        <div className="fields">
          <RefLine label="edges" text={f.arg_texts.edges} fallback={f.name} />
          <ExprField label="distance" text={f.arg_texts.distance ?? String(f.args.distance ?? '')} disabled={ro} onCommit={(v) => setArg('distance', v)} />
          <ExprField label="distance 2" text={f.arg_texts.distance2 ?? ''} disabled={ro} onCommit={(v) => setArg('distance2', v)} />
        </div>
      )}
      {f.kind === 'shell' && (
        <div className="fields">
          <RefLine label="open faces" text={f.arg_texts.faces === 'None' ? 'none (closed)' : f.arg_texts.faces} noHover={f.arg_texts.faces === 'None'} fallback={f.name} />
          <ExprField label="thickness" text={f.arg_texts.thickness ?? String(f.args.thickness ?? '')} disabled={ro} onCommit={(v) => setArg('thickness', v)} />
          <BoolField label="outward" value={Boolean(f.args.outward)} disabled={ro} onCommit={(v) => setArg('outward', v)} />
        </div>
      )}
      {f.kind === 'linear_pattern' && (
        <div className="fields">
          <RefLine label="repeats" text={f.arg_texts.feature} />
          <ExprField label="count" text={f.arg_texts.count ?? String(f.args.count ?? '')} disabled={ro} onCommit={(v) => setArg('count', v)} />
          <ExprField label="spacing" text={f.arg_texts.spacing ?? String(f.args.spacing ?? '')} disabled={ro} onCommit={(v) => setArg('spacing', v)} />
          <RefLine label="direction" text={f.arg_texts.direction ?? String(f.args.direction ?? 'X')} />
          {f.args.count2 != null && (
            <>
              <ExprField label="count 2" text={f.arg_texts.count2 ?? String(f.args.count2)} disabled={ro} onCommit={(v) => setArg('count2', v)} />
              <ExprField label="spacing 2" text={f.arg_texts.spacing2 ?? String(f.args.spacing2 ?? '')} disabled={ro} onCommit={(v) => setArg('spacing2', v)} />
              <RefLine label="direction 2" text={f.arg_texts.direction2 ?? String(f.args.direction2 ?? 'Y')} />
            </>
          )}
        </div>
      )}
      {f.kind === 'circular_pattern' && (
        <div className="fields">
          <RefLine label="repeats" text={f.arg_texts.feature} />
          <ExprField label="count" text={f.arg_texts.count ?? String(f.args.count ?? '')} disabled={ro} onCommit={(v) => setArg('count', v)} />
          <RefLine label="axis" text={f.arg_texts.axis ?? String(f.args.axis ?? 'Z')} />
          <ExprField label="angle °" text={f.arg_texts.angle ?? String(f.args.angle ?? 360)} disabled={ro} onCommit={(v) => setArg('angle', v)} />
        </div>
      )}
      {f.kind === 'mirror' && (
        <div className="fields">
          <RefLine label="mirrors" text={f.arg_texts.feature ?? 'the whole body'} />
          <RefLine label="about" text={f.arg_texts.about ?? String(f.args.about ?? 'YZ')} />
        </div>
      )}
      {f.kind === 'import_step' && (
        <div className="fields"><RefLine label="file" text={String(f.args.path)} noHover /></div>
      )}
      {f.kind === 'instance' && <InstanceProps f={f} tree={tree} />}
      {f.kind === 'view' && <ViewProps f={f} tree={tree} />}
      {f.kind === 'dimension' && <DimensionProps f={f} tree={tree} />}
      {f.kind === 'note' && <NoteProps f={f} tree={tree} />}
      {f.kind === 'fixed' && (
        <div className="fields"><RefLine label="instance" text={f.arg_texts.instance} noHover /><div className="panel-help">anchored at the pose written in its statement</div></div>
      )}
      {isMate && f.kind !== 'fixed' && (
        <div className="fields" data-testid="mate-props">
          <RefLine label="a" text={f.arg_texts.a} />
          <RefLine label="b" text={f.arg_texts.b} />
          {(f.kind === 'distance' || f.kind === 'angle') && (
            <ExprField label={f.kind === 'angle' ? 'angle °' : 'distance'} text={f.arg_texts.value ?? String(f.args.value ?? '')} disabled={ro} onCommit={(v) => setArg('value', v)} />
          )}
          {f.kind !== 'concentric' && f.kind !== 'parallel' && (
            <BoolField label="flip" value={Boolean(f.args.flip)} disabled={ro} onCommit={(v) => setArg('flip', v)} testId="mate-flip" />
          )}
          <div className="panel-help">{MATE_HELP[f.kind]}</div>
        </div>
      )}
      {f.kind !== 'sketch' && (
        <div className="fields">
          {isTool && !ro && f.variable && !isDwg && (
            <div className="btn-row" data-testid="repeat-buttons">
              <button className="btn" onClick={() => openFeatureDialog('linear_pattern', f.name)} title="repeat this feature along a direction">pattern</button>
              <button className="btn" onClick={() => openFeatureDialog('circular_pattern', f.name)} title="repeat this feature about an axis">circular</button>
              <button className="btn" onClick={() => openFeatureDialog('mirror', f.name)} title="mirror this feature across a plane">mirror</button>
            </div>
          )}
          {isTool && !ro && !f.variable && <div className="panel-help">assign the feature to a variable to pattern or mirror it from here</div>}
          <BoolField label="suppressed" value={Boolean(f.suppressed)} disabled={ro} onCommit={(v) => setSuppressed(f.name, v)} testId="suppress" />
          {timing && <div className="panel-help">{timing}</div>}
        </div>
      )}
    </div>
  );
}

const MATE_HELP: Record<string, string> = {
  coincident: 'two body faces touch (normals opposed); planes, axes and points simply coincide. flip reverses the facing.',
  concentric: 'cylindrical faces, circular edges or axes share one axis',
  distance: 'b lies the value along a\'s normal; for points and axes, their separation',
  parallel: 'the two directions stay parallel',
  angle: 'the angle between the two planes or axes; 0 is the coincident orientation',
};

/** What a selected edge is: its owner, labels and length, and the features that start from it. */
function EdgeInfo({ edge }: { edge: number }) {
  const mesh = useStore((s) => s.mesh);
  const tree = useStore((s) => s.tree);
  const h = mesh?.header;
  if (!h || !mesh) return null;
  const owner = h.edge_labels[String(edge)] || '';
  const tags = (h.edge_tags?.[String(edge)] ?? []).map((t) => t.replace(/^:/, ''));
  const [start, count] = h.edge_ranges[edge] ?? [0, 0];
  let length = 0;
  for (let i = start; i < start + count - 1; i++) {
    const a = i * 3, b = (i + 1) * 3;
    length += Math.hypot(mesh.edgePositions[b] - mesh.edgePositions[a], mesh.edgePositions[b + 1] - mesh.edgePositions[a + 1], mesh.edgePositions[b + 2] - mesh.edgePositions[a + 2]);
  }
  const part = tree?.kind === 'part';
  return (
    <div className="fields" data-testid="edge-info">
      <div className="measure-row"><span>edge</span><span className="mono">{edge}{owner ? ` · ${owner}` : ''}{tags.length ? ` · ${tags.join(' ')}` : ''} · {fmt(length)} mm</span></div>
      {part && (
        <div className="btn-row">
          <button className="btn" onClick={() => openFeatureDialog('fillet', null)} title="round this edge (and more picked in the viewport)" data-testid="edge-fillet">fillet</button>
          <button className="btn" onClick={() => openFeatureDialog('chamfer', null)} title="bevel this edge (and more picked in the viewport)" data-testid="edge-chamfer">chamfer</button>
        </div>
      )}
    </div>
  );
}

/** Sheet coordinates: "x, y" in mm, written back as a tuple. */
function SheetPoint({ label, value, onCommit, disabled, testId }: { label: string; value: number[]; onCommit: (v: [number, number]) => void; disabled?: boolean; testId?: string }) {
  const text = `${value[0]}, ${value[1]}`;
  const [val, setVal] = useState(text);
  useEffect(() => setVal(text), [text]);
  const commit = () => {
    const parts = val.split(',').map((s) => Number(s.trim()));
    if (parts.length === 2 && parts.every((n) => Number.isFinite(n)) && val.trim() !== text) onCommit([parts[0], parts[1]]);
  };
  return (
    <label className="field"><span>{label}</span>
      <input value={val} disabled={disabled} onChange={(e) => setVal(e.target.value)} onBlur={commit}
             onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }} data-testid={testId} />
    </label>
  );
}

/** A view: where it looks from (or the plane it cuts along), its place and scale on the sheet, hidden lines. */
function ViewProps({ f, tree }: { f: Feature; tree: Tree }) {
  const ro = f.read_only;
  const setArg = (kwarg: string, value: EditValue) => edit({ op: 'set_argument', feature: f.name, kwarg, value });
  const v = tree.evaluation.drawing?.views.find((x) => x.name === f.name) ?? null;
  const section = f.args.section != null ? String(f.args.section) : null;
  const at = (f.args.at as number[] | undefined) ?? [0, 0];
  const sheetScale = tree.evaluation.drawing?.sheet.scale ?? 1;
  return (
    <div className="fields" data-testid="view-props">
      {section ? (
        <>
          <RefLine label="section" text={f.arg_texts.section ?? section} />
          <ExprField label="offset" text={f.arg_texts.offset ?? String(f.args.offset ?? 0)} disabled={ro} onCommit={(x) => setArg('offset', x)} />
          <BoolField label="flip side" value={Boolean(f.args.flip)} disabled={ro} onCommit={(x) => setArg('flip', x)} testId="view-flip" />
        </>
      ) : (
        <label className="field inline"><span>from</span>
          <select value={String(f.args.direction ?? 'front')} disabled={ro} onChange={(e) => setArg('direction', { expr: e.target.value.toUpperCase() })} data-testid="view-direction">
            {VIEW_DIRECTIONS.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
      )}
      <SheetPoint label="at (mm)" value={at} disabled={ro} onCommit={(p) => setArg('at', { expr: `(${p[0]}, ${p[1]})` })} testId="view-at" />
      <label className="field"><span>scale</span>
        <TextField text={f.args.scale != null ? String(f.args.scale) : ''} placeholder={`sheet's (${sheetScale})`} disabled={ro} onCommit={(t) => setArg('scale', t ? { expr: t } : null)} testId="view-scale" />
      </label>
      <BoolField label="hidden lines" value={v ? v.hidden_lines : Boolean(f.args.hidden ?? !section)} disabled={ro} onCommit={(x) => setArg('hidden', x)} testId="view-hidden" />
      {v && <div className="panel-help">{v.label ? `${v.label} · ` : ''}{fmt(v.bbox[2] - v.bbox[0])} × {fmt(v.bbox[3] - v.bbox[1])} mm on the sheet · {v.visible.length} visible, {v.hidden.length} hidden edges</div>}
      <div className="panel-help">drag the view on the sheet to move it; its dimensions follow</div>
    </div>
  );
}

/** A dimension: its references, what it measures, its text and placement. */
function DimensionProps({ f, tree }: { f: Feature; tree: Tree }) {
  const ro = f.read_only;
  const setArg = (kwarg: string, value: EditValue) => edit({ op: 'set_argument', feature: f.name, kwarg, value });
  const d = tree.evaluation.drawing?.dimensions.find((x) => x.name === f.name) ?? null;
  const at = (f.args.at as number[] | undefined) ?? [0, 0];
  const two = f.args.b != null;
  const kind = f.args.kind != null ? String(f.args.kind) : 'auto';
  const effective = d?.kind ?? (kind === 'auto' ? (two ? 'distance' : 'diameter') : kind);
  return (
    <div className="fields" data-testid="dimension-props">
      <RefLine label="a" text={f.arg_texts.a} />
      {two && <RefLine label="b" text={f.arg_texts.b} />}
      <label className="field inline"><span>measures</span>
        <select value={kind} disabled={ro} onChange={(e) => setArg('kind', e.target.value === 'auto' ? null : e.target.value)} data-testid="dimension-kind">
          <option value="auto">auto ({two ? 'distance' : 'diameter'})</option>
          {DIMENSION_KINDS.filter((k) => (two ? k === 'distance' || k === 'angle' : k === 'diameter' || k === 'radius')).map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
      </label>
      {effective === 'distance' && (
        <label className="field inline"><span>along</span>
          <select value={f.args.along != null ? String(f.args.along) : ''} disabled={ro} onChange={(e) => setArg('along', e.target.value || null)} data-testid="dimension-along">
            <option value="">between the references</option><option value="x">x (horizontal)</option><option value="y">y (vertical)</option>
          </select>
        </label>
      )}
      <label className="field"><span>text</span>
        <TextField text={f.args.text != null ? String(f.args.text) : ''} placeholder={d ? `measured: ${d.text}` : 'measured value'} disabled={ro} onCommit={(t) => setArg('text', t || null)} testId="dimension-text" />
      </label>
      <SheetPoint label="at (view mm)" value={at} disabled={ro} onCommit={(p) => setArg('at', { expr: `(${p[0]}, ${p[1]})` })} testId="dimension-at" />
      {d && <div className="sketch-status ok" data-testid="dimension-value">{d.kind} in view {d.view}: {d.text}{f.args.text ? ` (measured ${fmt(d.value)})` : ''}</div>}
      <div className="panel-help">placement is relative to the view's centre; drag the text on the sheet to move it</div>
    </div>
  );
}

/** A note: its text, size and place, on the sheet or beside a view. */
function NoteProps({ f, tree }: { f: Feature; tree: Tree }) {
  const ro = f.read_only;
  const setArg = (kwarg: string, value: EditValue) => edit({ op: 'set_argument', feature: f.name, kwarg, value });
  const at = (f.args.at as number[] | undefined) ?? [0, 0];
  const text = String(f.args.text ?? '');
  const [val, setVal] = useState(text);
  useEffect(() => setVal(text), [text]);
  const views = tree.features.filter((x) => x.kind === 'view' && x.variable);
  return (
    <div className="fields" data-testid="note-props">
      <label className="field"><span>text</span>
        <textarea className="note-text" value={val} disabled={ro} rows={3} onChange={(e) => setVal(e.target.value)} spellCheck={false} data-testid="note-text"
                  onBlur={() => { if (val.trim() && val !== text) setArg('text', val); }} />
      </label>
      <ExprField label="size (mm)" text={f.arg_texts.size ?? String(f.args.size ?? 3.5)} disabled={ro} onCommit={(x) => setArg('size', x)} />
      <label className="field inline"><span>beside</span>
        <select value={f.args.view != null ? String(f.args.view) : ''} disabled={ro} onChange={(e) => setArg('view', e.target.value ? { expr: e.target.value } : null)} data-testid="note-view">
          <option value="">the sheet</option>
          {views.map((v) => <option key={v.name} value={v.variable!}>{v.name}</option>)}
        </select>
      </label>
      <SheetPoint label={f.args.view != null ? 'at (view mm)' : 'at (mm)'} value={at} disabled={ro} onCommit={(p) => setArg('at', { expr: `(${p[0]}, ${p[1]})` })} testId="note-at" />
      <div className="panel-help">drag the text on the sheet to move it</div>
    </div>
  );
}

/** Text fields for meta() entries: empty drops the keyword, so the placeholder (a default or the
 * model's value) applies. */
function MetaFields({ meta, fields, placeholders = {} }: { meta: Record<string, unknown>; fields: [string, string][]; placeholders?: Record<string, string> }) {
  return (
    <>
      {fields.map(([key, hint]) => (
        <label key={key} className="field"><span>{key}</span>
          <TextField text={meta[key] != null ? String(meta[key]) : ''} placeholder={placeholders[key] ? `${placeholders[key]} (the model's)` : hint}
                     onCommit={(t) => setMeta(key, t || null)} testId={`meta-${key}`} />
        </label>
      ))}
    </>
  );
}

/** Nothing selected in a drawing: the sheet, its scale, the title block fields, its model, export. */
function DrawingPanel({ tree }: { tree: Tree }) {
  const scene = tree.evaluation.drawing ?? null;
  const name = String(tree.meta.name ?? tree.path?.split('/').pop()?.replace(/\.py$/, '') ?? 'drawing');
  const doExport = (format: 'pdf' | 'dxf' | 'svg') => {
    const target = window.prompt(`Export ${format.toUpperCase()} to (relative to the drawing):`, `${name}.${format}`);
    if (target) void exportDocument(format, target);
  };
  const model = modelDocPath();
  const modelMeta = scene?.model.meta ?? {};
  return (
    <div className="props" data-testid="drawing-props">
      <div className="props-title"><span>drawing <b>{name}</b></span></div>
      <div className="fields">
        <div className="measure-row"><span>model</span><span className="mono">{String(tree.meta.of ?? '—')}{scene?.model.kind ? ` · ${scene.model.kind}` : ''}</span></div>
        {scene?.model.error && <div className="props-error">{scene.model.error}</div>}
        <label className="field inline"><span>sheet</span>
          <select value={String(tree.meta.sheet ?? 'A4')} onChange={(e) => setMeta('sheet', e.target.value)} data-testid="meta-sheet">
            {[['A4', 'A4 · 297 × 210'], ['A3', 'A3 · 420 × 297'], ['letter', 'letter · 279.4 × 215.9']].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
        <label className="field"><span>scale</span>
          <TextField text={tree.meta.scale != null ? String(tree.meta.scale) : ''} placeholder="1 (views may override)" onCommit={(t) => { const n = Number(t); if (!t) void setMeta('scale', null); else if (Number.isFinite(n) && n > 0) void setMeta('scale', n); }} testId="meta-scale" />
        </label>
        <div className="section-title">title block</div>
        <MetaFields meta={tree.meta} fields={[['title', modelMeta.name ?? 'the model\'s name'], ['revision', 'A'], ['author', ''], ['date', '2026-09-05']]} placeholders={{ revision: modelMeta.revision ?? '', author: modelMeta.author ?? '' }} />
        <div className="panel-help">the part name and material always come from the model{modelMeta.material ? ` (${modelMeta.material})` : ''}</div>
        <div className="btn-row">
          <button className="btn" onClick={() => doExport('pdf')} data-testid="export-pdf">export PDF</button>
          <button className="btn" onClick={() => doExport('dxf')} data-testid="export-dxf">export DXF</button>
          <button className="btn" onClick={() => doExport('svg')} data-testid="export-svg">export SVG</button>
        </div>
        {model && <div className="btn-row"><button className="btn" onClick={() => void openDocument(model)} data-testid="open-model-panel">open model</button></div>}
        <div className="panel-help">click a view, a dimension or a note on the sheet or in the tree · drag to move · scroll to zoom</div>
        <div className="section-title">parameters</div>
      </div>
      <ParamPanel />
    </div>
  );
}

/** An instance's file, colour, material, pose and constraint status. */
function InstanceProps({ f, tree }: { f: Feature; tree: Tree }) {
  const visibility = useStore((s) => s.visibility);
  const transparency = useStore((s) => s.transparency);
  const root = (tree.evaluation.instances ?? []).find((r) => r.name === f.name) ?? null;
  const sol = tree.evaluation.assembly ?? null;
  const pose = sol?.poses[f.name] ?? null;
  const fixedBy = tree.features.find((x) => x.kind === 'fixed' && String(x.args.instance) === f.name && !x.suppressed && x.result?.ok) ?? null;
  const leaves = root ? leafPaths(root) : [];
  const visible = leaves.length === 0 || leaves.some((p) => visibility[p] ?? true);
  const transparent = leaves.length > 0 && leaves.every((p) => transparency[p] ?? false);
  const ro = f.read_only;
  const setArg = (kwarg: string, value: EditValue) => edit({ op: 'set_argument', feature: f.name, kwarg, value });
  const color = root?.color ? `rgb(${root.color.slice(0, 3).map((c) => Math.round(c * 255)).join(',')})` : null;
  const status = fixedBy ? `fixed by ${fixedBy.name}` : sol?.free.includes(f.name) ? 'under-constrained: mates leave it free' : sol ? 'held by its mates' : '';
  return (
    <div className="fields" data-testid="instance-props">
      <RefLine label="file" text={String(f.args.path)} noHover />
      <div className="measure-row"><span>product</span><span className="mono">{root ? `${root.product} · ${root.kind}` : '—'}</span></div>
      <label className="field"><span>colour</span>
        <span className="colour-field"><span className="swatch" style={{ background: color ?? 'transparent', borderStyle: color ? 'solid' : 'dashed' }} />
          <TextField text={typeof f.args.color === 'string' ? f.args.color : ''} placeholder="#rrggbb or the part's" disabled={ro} onCommit={(t) => setArg('color', t || null)} testId="instance-color" /></span>
      </label>
      <label className="field"><span>material</span>
        <TextField text={typeof f.args.material === 'string' ? f.args.material : ''} placeholder="the part's" disabled={ro} onCommit={(t) => setArg('material', t || null)} testId="instance-material" />
      </label>
      <PoseField label="at" value={f.args.at as number[]} disabled={ro} onCommit={(v) => setArg('at', { expr: `(${v.join(', ')})` })} />
      <PoseField label="rotate °" value={f.args.rotate as number[]} disabled={ro} onCommit={(v) => setArg('rotate', { expr: `(${v.join(', ')})` })} />
      {pose && <div className="panel-help">solved: at {pose.at.map(fmt).join(', ')} · rotate {pose.rotate.map(fmt).join(', ')}</div>}
      {status && <div className={`sketch-status ${fixedBy || !sol?.free.includes(f.name) ? 'ok' : ''}`} data-testid="instance-status">{status}</div>}
      <div className="btn-row">
        {root?.kind === 'part' && root.file && <button className="btn" onClick={() => openDocument(root.file!)} title="open the part file in a tab; the assembly follows its edits" data-testid="open-part">open part</button>}
        {root?.kind === 'step' && root.solids === 1 && !ro && (
          <button className="btn" onClick={() => makeEditable({ instance: f.name })} title="write a part file wrapping this STEP body and open it in a tab; every instance of the same product follows" data-testid="edit-part">edit part</button>
        )}
        {!fixedBy && !ro && f.variable && <button className="btn" onClick={() => fixInstance(f.name)} title="anchor the instance where it is" data-testid="fix-instance">fix here</button>}
      </div>
      {root?.kind === 'step' && root.solids !== 1 && <div className="panel-help">a STEP instance of {root.solids} solids stays rigid; a part is one solid</div>}
      <BoolField label="visible" value={visible} onCommit={(v) => leaves.forEach((p) => setVisibility(p, v))} />
      <BoolField label="transparent" value={transparent} onCommit={(v) => leaves.forEach((p) => setTransparency(p, v))} />
    </div>
  );
}

function TextField({ text, placeholder, onCommit, disabled, testId }: { text: string; placeholder?: string; onCommit: (t: string) => void; disabled?: boolean; testId?: string }) {
  const [val, setVal] = useState(text);
  useEffect(() => setVal(text), [text]);
  const commit = () => { if (val.trim() !== text) onCommit(val.trim()); };
  return <input value={val} placeholder={placeholder} disabled={disabled} onChange={(e) => setVal(e.target.value)} onBlur={commit}
                onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }} data-testid={testId} spellCheck={false} />;
}

function PoseField({ label, value, onCommit, disabled }: { label: string; value: number[]; onCommit: (v: number[]) => void; disabled?: boolean }) {
  const text = (value ?? [0, 0, 0]).map((v) => String(Math.round(v * 10000) / 10000)).join(', ');
  const [val, setVal] = useState(text);
  useEffect(() => setVal(text), [text]);
  const commit = () => {
    const parts = val.split(',').map((s) => Number(s.trim()));
    if (parts.length === 3 && parts.every((n) => Number.isFinite(n)) && val.trim() !== text) onCommit(parts);
  };
  return (
    <label className="field">
      <span>{label}</span>
      <input value={val} disabled={disabled} onChange={(e) => setVal(e.target.value)} onBlur={commit}
             onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }} />
    </label>
  );
}

/** "slot1 (angle), line3 (end)": the loose variables per free entity, a point's x and y folded into one word. */
export function freeText(sol: { free_entities: string[]; free_variables?: Record<string, string[]> }): string {
  return sol.free_entities.map((name) => {
    const vars = sol.free_variables?.[name];
    if (!vars || !vars.length) return name;
    const words = [...new Set(vars.map((v) => v.replace(/\.(x|y)$/, '')))];
    return `${name} (${words.join(', ')})`;
  }).join(', ');
}

/** Comparing with another document: the volumetric change report behind the coloured overlay. */
function ComparePanel({ tree, overlay }: { tree: Tree; overlay: Overlay }) {
  const mesh = useStore((s) => s.mesh);
  const loading = useStore((s) => s.loading);
  const report = mesh?.header.compare ?? null;
  const other = overlay.rev ? `this file at ${overlay.rev}` : overlay.other ?? '';
  const region = (r: CompareRegion, sign: string) => `${sign}${fmt(r.volume)} mm³ at (${r.center.map((c) => c.toFixed(1)).join(', ')}), ${r.size.map((c) => c.toFixed(1)).join(' × ')}`;
  return (
    <div className="props" data-testid="compare-props">
      <div className="props-title"><span>compare <b>{String(tree.meta.name ?? '')}</b> with {other}</span></div>
      {!report && <div className="panel-help">{loading ? 'computing…' : 'no comparison yet'}</div>}
      {report && (
        <div className="fields" data-testid="compare-report">
          <div className="measure-row"><span>this</span><span className="mono">{fmt(report.base.volume)} mm³</span></div>
          <div className="measure-row"><span>other</span><span className="mono">{fmt(report.other.volume)} mm³</span></div>
          <div className="measure-row"><span>added</span><span className="mono compare-added">{fmt(report.added.volume)} mm³ in {report.added.count} region{report.added.count === 1 ? '' : 's'}</span></div>
          <div className="measure-row"><span>removed</span><span className="mono compare-removed">{fmt(report.removed.volume)} mm³ in {report.removed.count} region{report.removed.count === 1 ? '' : 's'}</span></div>
          {report.same && <div className="sketch-status ok">the same geometry</div>}
          {report.added.regions.slice(0, 6).map((r, i) => <div key={`a${i}`} className="panel-help compare-added">{region(r, '+')}</div>)}
          {report.removed.regions.slice(0, 6).map((r, i) => <div key={`r${i}`} className="panel-help compare-removed">{region(r, '−')}</div>)}
        </div>
      )}
      <div className="panel-help">green is what this document has and the other does not, red the reverse, grey what both share; the comparison follows every edit</div>
      <div className="row"><button className="btn" onClick={() => void setOverlay(null)} data-testid="compare-stop-panel">stop comparing</button></div>
    </div>
  );
}

/** Nothing selected in a part: its numbers, the meta fields, whatever fails, and the parameters. */
function PartPanel({ tree }: { tree: Tree }) {
  const mesh = useStore((s) => s.mesh);
  const selectedFace = useStore((s) => s.selectedFace);
  const selectedEdge = useStore((s) => s.selectedEdge);
  const tags = selectedFace !== null ? (mesh?.header.face_tags?.[String(selectedFace)] ?? []) : [];
  return (
    <div className="props" data-testid="part-props">
      <div className="props-title"><span>part <b>{String(tree.meta.name ?? '')}</b></span></div>
      <div className="panel-help">click a face or an edge, or a feature in the tree{selectedFace !== null ? ` · face ${selectedFace}: ${mesh?.header.face_labels[String(selectedFace)] || 'unknown feature'}${tags.length ? ` · ${tags.map((t) => t.replace(/^:/, '')).join(' ')}` : ''}` : ''}</div>
      {selectedEdge !== null && <EdgeInfo edge={selectedEdge} />}
      <div className="fields">
        <SummaryLine tree={tree} />
        <MetaFields meta={tree.meta} fields={[['material', 'al6061, steel, abs, …'], ['revision', 'A'], ['author', '']]} />
        <Problems tree={tree} />
        <div className="section-title">parameters</div>
      </div>
      <ParamPanel />
      <div className="panel-help">a parameter is a top-level `name = number` line; the material gives the mass and the bill of materials their density; material, revision and author feed a drawing's title block</div>
    </div>
  );
}

/** One line of numbers from the summary query: mass, volume, box, faces. */
function SummaryLine({ tree }: { tree: Tree }) {
  const summary = useStore((s) => s.summary);
  const revision = useStore((s) => s.revision);
  useEffect(() => { if (tree.evaluation.has_body && (!summary || summary.revision !== revision)) void fetchSummary(); }, [revision, tree]);
  const s = summary?.revision === revision ? summary.data : null;
  if (!s) return tree.evaluation.has_body ? <div className="measure-row"><span>size</span><span className="mono">…</span></div> : null;
  return (
    <div className="measure-row" data-testid="part-summary"><span>size</span>
      <span className="mono">{s.mass != null ? `${fmt(s.mass)} g · ` : ''}{fmt(s.volume / 1000)} cm³ · {s.bbox.size.map(fmt).join(' × ')} mm · {s.counts.faces} faces</span>
    </div>
  );
}

/** File errors and failing or warning features, each a click away. */
function Problems({ tree }: { tree: Tree }) {
  const failing = tree.features.filter((f) => f.result && (!f.result.ok || f.result.warnings.length));
  if (!failing.length && !tree.errors.length) return null;
  return (
    <>
      <div className="section-title">problems</div>
      {tree.errors.map((e, i) => <div key={`e${i}`} className="props-error">line {e.line ?? '?'}: {e.message}</div>)}
      {failing.map((f) => (
        <div key={f.name} className={`props-problem ${f.result?.ok ? 'props-warn' : 'props-error'}`} onClick={() => select(f.name)} data-testid={`problem-${f.name}`}>
          {f.name}: {f.result?.error?.message ?? f.result?.warnings.join('; ')}
        </div>
      ))}
    </>
  );
}

/** Nothing selected in a STEP viewer: the file and its bodies; the tree header turns it into an assembly. */
function ViewerPanel({ tree }: { tree: Tree }) {
  const roots = tree.evaluation.instances ?? [];
  const bodies = roots.reduce((n, r) => n + leafPaths(r).length, 0);
  const files = tree.features.filter((f) => f.kind === 'import_step').map((f) => String(f.args.path));
  return (
    <div className="props" data-testid="viewer-props">
      <div className="props-title"><span>STEP viewer <b>{String(tree.meta.name ?? '')}</b></span></div>
      <div className="fields">
        {files.map((p) => <RefLine key={p} label="file" text={p} />)}
        <div className="measure-row"><span>bodies</span><span className="mono">{bodies}</span></div>
        <div className="panel-help">click a body in the viewport or the tree · section, measure and snapshot work as they are · "make editable" in the tree header turns the bodies into instances that can be moved, mated and edited</div>
      </div>
    </div>
  );
}

/** Nothing selected in an assembly: its state, bill of materials and interference check. */
function AssemblyPanel({ tree }: { tree: Tree }) {
  const queries = useStore((s) => s.queries);
  const revision = useStore((s) => s.revision);
  const selectedFace = useStore((s) => s.selectedFace);
  useEffect(() => { if (!queries || queries.revision !== revision) void fetchAssemblyQueries(false); }, [revision]);
  const sol = tree.evaluation.assembly ?? null;
  const current = queries?.revision === revision ? queries : null;
  const bom = current?.bom ?? null;
  const inter = current?.interference ?? null;
  return (
    <div className="props" data-testid="assembly-props">
      <div className="props-title"><span>assembly <b>{String(tree.meta.name ?? '')}</b></span></div>
      <div className="panel-help">click an instance in the viewport or the tree, or a mate{selectedFace !== null ? ` · face ${selectedFace}` : ''}</div>
      {sol && (
        <div className={`sketch-status ${sol.conflicting.length ? '' : sol.dof === 0 ? 'ok' : ''}`}>
          {sol.conflicting.length ? `not satisfied: ${sol.conflicting.join(', ')}` : sol.dof === 0 ? 'fully constrained' : `${sol.dof} degree${sol.dof === 1 ? '' : 's'} of freedom`}
          {sol.free.length > 0 && <span className="panel-help"> free: {sol.free.join(', ')}</span>}
          {sol.redundant.length > 0 && <span className="panel-help"> redundant: {sol.redundant.join(', ')}</span>}
        </div>
      )}
      <div className="section-title">bill of materials</div>
      {!bom && <div className="panel-help">{current?.pending ? 'computing…' : 'no instances'}</div>}
      {bom && (
        <table className="entities bom" data-testid="bom-table">
          <tbody>
            {bom.rows.map((r) => (
              <tr key={r.file} className="entity-row" title={`${r.file} · ${r.instances.join(', ')}`}>
                <td><span className="swatch" style={{ background: r.color ?? 'transparent', borderStyle: r.color ? 'solid' : 'dashed' }} /></td>
                <td className="entity-name">{r.name}</td>
                <td className="mono">{r.count}×</td>
                <td className="mono">{r.material ?? '—'}</td>
                <td className="mono">{r.mass != null ? `${fmt(r.mass * r.count)} g` : '?'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {bom && <div className="panel-help" data-testid="bom-total">{bom.instances} instances · {bom.mass != null ? `${fmt(bom.mass)} g` : `mass unknown: ${bom.unknown_mass.join(', ')} need a material`}</div>}
      <div className="btn-row">
        <button className="btn" onClick={() => fetchAssemblyQueries(true)} disabled={!!current?.pending} data-testid="check-interference">check interference</button>
      </div>
      {inter && (inter.count === 0
        ? <div className="sketch-status ok" data-testid="interference-result">no interference</div>
        : <div data-testid="interference-result">{inter.pairs.map((p) => <div key={`${p.a}|${p.b}`} className="props-error">{p.a} overlaps {p.b}: {fmt(p.volume)} mm³</div>)}</div>)}
      <div className="section-title">parameters</div>
      <ParamPanel />
    </div>
  );
}

function planeDefinition(f: Feature): string {
  const t = f.arg_texts;
  if (t.between) return `midplane between ${t.between}`;
  if (t.through) return `through ${t.through}`;
  const base = t.base ?? String(f.args.base ?? '');
  if (t.about) return `${base} at ${t.angle ?? f.args.angle}° about ${t.about}`;
  return `${base}${f.args.offset ? ` offset ${t.offset ?? f.args.offset}` : ''}`;
}

/** Delete with a confirmation listing what goes with the feature (the server cascades). */
function DeleteButton({ f }: { f: Feature }) {
  // the Delete key and the context menu open the same confirmation through the store
  const open = useStore((s) => s.deleteConfirm) === f.name;
  const setOpen = (on: boolean) => setDeleteConfirm(on ? f.name : null);
  const dep = f.dependents ?? { features: [], entities: 0 };
  const cascade = dep.features.length > 0 || dep.entities > 0;
  const run = async () => { setOpen(false); if (await deleteFeature(f.name)) select(null); };
  const parts = [dep.features.length ? dep.features.join(', ') : '', dep.entities ? `${dep.entities} sketch entit${dep.entities === 1 ? 'y' : 'ies'}` : ''].filter(Boolean);
  return (
    <span className="delete-wrap">
      <button className="btn-small danger" onClick={() => (cascade ? setOpen(!open) : run())} title="delete this feature" data-testid="delete-feature">delete</button>
      {open && (
        <div className="popover" data-testid="delete-confirm">
          <div>Delete <b>{f.name}</b>? This also deletes {parts.join(' and ')}.</div>
          <div className="btn-row">
            <button className="btn-small danger" onClick={run} data-testid="delete-confirm-ok">delete all</button>
            <button className="btn-small" onClick={() => setOpen(false)}>keep</button>
          </div>
        </div>
      )}
    </span>
  );
}

function SketchProps({ f, inSketch }: { f: Feature; inSketch: boolean }) {
  const sol = f.result?.sketch ?? null;
  const constraints = f.constraints ?? [];
  return (
    <div className="fields">
      {inSketch && <SketchSelected f={f} />}
      <RefLine label="on" text={`${f.arg_texts.on ?? String(f.args.on)}${f.args.offset ? ` offset ${f.args.offset}` : ''}${f.args.flip ? ' flipped' : ''}`} noHover={typeof f.args.on === 'string'} />
      <div className="panel-help">{f.entities.length} entities · {constraints.length} constraints</div>
      {sol && (
        <div className={`sketch-status ${sol.fully_constrained ? 'ok' : ''}`} data-testid="sketch-status">
          {sol.fully_constrained ? 'fully constrained' : `${sol.dof} degree${sol.dof === 1 ? '' : 's'} of freedom`}
          {sol.free_entities.length > 0 && <span className="panel-help"> free: {freeText(sol)}</span>}
        </div>
      )}
      <div className="btn-row">
        {!f.read_only && (inSketch
          ? <button className="btn" onClick={exitSketch}>exit sketch</button>
          : <button className="btn" onClick={() => enterSketch(f)} title="draw entities on the plane">edit sketch</button>)}
        {f.variable && <button className="btn" onClick={() => openFeatureDialog('extrude', f.name)} title="extrude the profile: set the depth, watch the preview, add" data-testid="add-extrude">extrude</button>}
        {f.variable && <button className="btn" onClick={() => openFeatureDialog('cut', f.name)} title="cut with the profile: through all or to a depth, with a preview" data-testid="add-cut">cut</button>}
        {f.variable && <button className="btn" onClick={() => openFeatureDialog('revolve', f.name)} title="revolve the profile about a line of the sketch or a global axis" data-testid="add-revolve">revolve</button>}
      </div>
      {!f.variable && <div className="panel-help">assign the sketch to a variable to extrude it from here</div>}
      <BoolField label="suppressed" value={Boolean(f.suppressed)} disabled={f.read_only} onCommit={(v) => setSuppressed(f.name, v)} testId="suppress" />
      <table className="entities" data-testid="entity-list">
        <tbody>
          {f.entities.map((e) => <EntityRow key={e.name} sketch={f.name} e={e} ro={f.read_only} inSketch={inSketch} />)}
        </tbody>
      </table>
      {constraints.length > 0 && <div className="section-title">constraints</div>}
      <table className="entities constraints" data-testid="constraint-list">
        <tbody>
          {constraints.map((c) => <ConstraintRow key={c.name} c={c} ro={f.read_only} inSketch={inSketch} sol={sol} />)}
        </tbody>
      </table>
    </div>
  );
}

/** What the viewport's selection can take, at the top of the sketch panel: relations, a dimension,
 * construction, offset, delete; body picks with their convert buttons. The right-click menu offers the same. */
function SketchSelected({ f }: { f: Feature }) {
  const sm = useStore((s) => s.sketchMode);
  const preview = sm?.preview ?? null;
  const model = useMemo(() => buildModel(f, preview), [f, preview]);
  if (!sm) return null;
  const sel = sm.selection, body = sm.bodySelection;
  if (!sel.length && !body.length) return null;
  const choices = sel.length ? validConstraints(model, sel) : [];
  const plan = sel.length ? dimensionFor(model, sel) : null;
  const ents = [...new Set(sel.map(entityOf))].map((n) => model.entities.get(n)).filter((e) => e && !e.projected && e.kind !== 'point');
  const allConstruction = ents.length > 0 && ents.every((e) => e!.construction);
  const curves = sel.filter((r) => refKind(model, r) !== 'point' && !r.endsWith('.axis'));
  return (
    <div className="sketch-selected" data-testid="sketch-selected">
      {sel.length > 0 && (
        <>
          <div className="sel-names">{sel.join(' · ')}</div>
          <div className="btn-row">
            {choices.map((c) => (
              <button key={c.kind} className="btn-small" data-testid={`constrain-${c.kind}`} onClick={() => void addConstraint(c.kind, c.refs, c.options)}>{c.label}</button>
            ))}
            {plan && <button className="btn-small" data-testid="constrain-dimension" title="dimension the selection, then click to place it (d)" onClick={() => beginDimension(plan)}>{plan.kind}</button>}
            {!choices.length && !plan && <span className="panel-help">no relation fits this selection</span>}
          </div>
          <div className="btn-row">
            {ents.length > 0 && (
              <button className={`btn-small ${allConstruction ? 'active' : ''}`} data-testid="constrain-construction" onClick={() => void toggleConstructionSelection()}
                      title={allConstruction ? 'make profile geometry' : 'make construction geometry'}>construction</button>
            )}
            {curves.length > 0 && <button className="btn-small" data-testid="sketch-offset" title="offset the selected curves: click the side, then type the distance" onClick={() => setSketchTool('offset')}>offset…</button>}
            <button className="btn-small danger" onClick={() => void deleteSketchSelection()} title="delete the selected entities (del)">delete</button>
            <button className="btn-small" onClick={() => toggleSketchSelect(null, false)}>clear</button>
          </div>
        </>
      )}
      {body.length > 0 && (
        <>
          <div className="sel-names" data-testid="body-selected">body: {body.map((e) => `${e.kind} ${e.id}`).join(' · ')}</div>
          <div className="btn-row">
            <button className="btn-small" data-testid="convert-body" onClick={() => void convertBodySelection(false)} title="sketch geometry that follows the body; a face gives its outline">convert</button>
            <button className="btn-small" data-testid="convert-body-construction" onClick={() => void convertBodySelection(true)} title="the same, as construction geometry">convert as construction</button>
            <button className="btn-small" onClick={() => toggleBodySelect(null, false)}>clear</button>
          </div>
        </>
      )}
    </div>
  );
}

function ConstraintRow({ c, ro, inSketch, sol }: { c: Constraint; ro: boolean; inSketch: boolean; sol: Feature['result'] extends infer R ? (R extends { sketch?: infer S } ? S : never) : never }) {
  const conflict = sol?.conflicting?.includes(c.name);
  const redundant = sol?.redundant?.includes(c.name);
  return (
    <tr className={`entity-row constraint-row ${conflict ? 'conflict' : ''} ${redundant ? 'redundant' : ''}`} data-testid={`constraint-${c.name}`}
        onMouseEnter={() => inSketch && setSketchHighlight([c.name, ...c.refs])} onMouseLeave={() => inSketch && setSketchHighlight([])}>
      <td className="entity-kind" title={c.kind}>{c.dimension ? c.kind : `${GLYPH[c.kind] ?? ''} ${c.kind}`}</td>
      <td className="entity-name">{c.name}</td>
      <td className="entity-fields">
        <span className="mono refs">{c.refs.join(', ')}{c.options?.along ? ` along ${String(c.options.along)}` : ''}</span>
        {c.dimension && (
          <span className="dim-field">
            <ExprInput text={c.value_text ?? String(c.value ?? '')} names={expressionNames()} disabled={ro} testId={`constraint-value-${c.name}`}
                       onCommit={(t) => setConstraintValue(c.name, t)} />
            {c.value_text && !/^-?\d+(\.\d+)?$/.test(c.value_text.trim()) && <span className="param-computed">= {c.value}</span>}
          </span>
        )}
        {conflict && <span className="badge err">conflict</span>}
        {redundant && <span className="badge warn">redundant</span>}
      </td>
      <td>{!ro && <button className="btn-small danger" onClick={() => deleteConstraint(c.name)} title="delete constraint">×</button>}</td>
    </tr>
  );
}

const NUMERIC: Record<string, string[]> = { circle: ['diameter'], rect: ['width', 'height'], slot: ['length', 'width', 'angle'], offset: ['distance'] };
const POINTS: Record<string, string[]> = { point: ['at'], line: ['start', 'end'], arc: ['center', 'start', 'end'], circle: ['at'], rect: ['at'], slot: ['at'] };

function EntityRow({ sketch, e, ro, inSketch }: { sketch: string; e: Entity; ro: boolean; inSketch: boolean }) {
  const selection = useStore((s) => s.sketchMode?.selection ?? null);
  const set = (kwarg: string, value: EditValue) => edit({ op: 'set_entity_argument', sketch, entity: e.name, kwarg, value });
  const del = () => edit({ op: 'delete_sketch_entity', sketch, entity: e.name });
  const fields = NUMERIC[e.kind] ?? [];
  const pts = POINTS[e.kind] ?? [];
  // inside the sketch the row follows the viewport's selection: lit when selected, a click selects, hovering lights the entity
  const selected = inSketch && !!selection?.some((r) => r === e.name || r.startsWith(`${e.name}.`));
  const ref = useRef<HTMLTableRowElement>(null);
  useEffect(() => { if (selected) ref.current?.scrollIntoView({ block: 'nearest' }); }, [selected]);
  const onClick = (ev: React.MouseEvent) => {
    if (!inSketch || (ev.target as HTMLElement).closest('input, select, button')) return;
    toggleSketchSelect(e.name, ev.shiftKey || ev.ctrlKey || ev.metaKey);
  };
  return (
    <tr ref={ref} className={`entity-row ${inSketch ? 'clickable' : ''} ${selected ? 'selected' : ''}`} data-testid={`entity-${e.name}`} onClick={onClick}
        onMouseEnter={() => inSketch && setSketchHighlight([e.name])} onMouseLeave={() => inSketch && setSketchHighlight([])}>
      <td className="entity-kind">{e.kind}</td>
      <td className="entity-name">{e.name}{e.construction ? ' (c)' : ''}</td>
      <td className="entity-fields">
        {fields.map((k) => <ExprField key={k} label={k} text={e.arg_texts?.[k] ?? String(e.args[k] ?? '')} disabled={ro} onCommit={(v) => set(k, v)} />)}
        {pts.map((k) => e.args[k] ? <PointField key={k} label={k} value={e.args[k] as [number, number]} disabled={ro} onCommit={(v) => set(k, v)} /> : null)}
        {e.kind === 'polygon' && <span className="panel-help">{(e.args.points as number[][]).length} points</span>}
        {e.kind === 'project' && <span className="mono refs">{e.arg_texts?.selector ?? 'converted'}</span>}
        {e.kind === 'offset' && (
          <>
            <span className="mono refs">of {(e.args.of as string[]).join(', ')}</span>
            <label className="field inline"><span>side</span>
              <select value={String(e.args.side ?? 'outside')} disabled={ro} onChange={(ev) => set('side', ev.target.value)}>
                {['outside', 'inside', 'left', 'right'].map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            </label>
            <label className="field inline"><span>corners</span>
              <select value={String(e.args.corners ?? 'sharp')} disabled={ro} onChange={(ev) => set('corners', ev.target.value)}>
                <option value="sharp">sharp</option><option value="round">round</option>
              </select>
            </label>
          </>
        )}
      </td>
      <td>{!ro && <button className="btn-small danger" onClick={del} title="delete entity">×</button>}</td>
    </tr>
  );
}

function PointField({ label, value, onCommit, disabled }: { label: string; value: [number, number]; onCommit: (v: [number, number]) => void; disabled?: boolean }) {
  const text = `${value[0]}, ${value[1]}`;
  const [val, setVal] = useState(text);
  useEffect(() => setVal(text), [text]);
  const commit = () => {
    const parts = val.split(',').map((s) => Number(s.trim()));
    if (parts.length === 2 && parts.every((n) => Number.isFinite(n)) && val.trim() !== text) onCommit([parts[0], parts[1]]);
  };
  return (
    <label className="field">
      <span>{label}</span>
      <input value={val} disabled={disabled} onChange={(e) => setVal(e.target.value)} onBlur={commit}
             onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }} />
    </label>
  );
}
