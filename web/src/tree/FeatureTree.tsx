import { useEffect, useRef, useState } from 'react';
import {
  useStore, select, setUpto, enterSketch, isAssembly, isViewer, isDrawing, explodeImport, addSketchOn, openPlaneDialog, openFeatureDialog, selectorTarget, featureByName, getState, setError,
  setVisibility, setTransparency, leafPaths, setDrawingTool, addView, openDocument, modelDocPath, isolate, showAll, allLeafPaths, fmt, setTreeHover, itemOf, instanceFeatureOf,
  openContextMenu,
  type FeatureDialogKind,
} from '../state/store';
import { featureRowMenu } from '../menu/entries';

/** A right-click on a row selects it and opens its menu. */
function rowContext(e: React.MouseEvent, f: Feature) {
  e.preventDefault();
  select(f.name);
  openContextMenu(e.clientX, e.clientY, featureRowMenu(f), `${f.kind.replace('_', ' ')} ${f.name}`);
}
import { MATE_KINDS, VIEW_DIRECTIONS, type Feature, type Instance, type Tree } from '../api/types';
import { InstanceTree } from './InstanceTree';
import { sceneRef } from '../viewport/Viewport';

const ICONS: Record<string, string> = {
  sketch: '✎', extrude: '⬆', cut: '⬇', import_step: '⬚', plane: '▱', revolve: '⟲', fillet: '◜', chamfer: '◣', shell: '▢',
  linear_pattern: '⋯', circular_pattern: '✲', mirror: '⇔',
  instance: '▣', fixed: '⚓', coincident: '≡', concentric: '◎', distance: '↔', parallel: '∥', angle: '∠',
  view: '▭', dimension: '⟷', note: '❞',
};
const numText = (f: Feature, key: string): string => { const v = f.args[key]; return typeof v === 'number' ? fmt(v) : v == null ? '' : String(v); };
const basename = (p: unknown) => (String(p ?? '').split('/').pop() ?? '').split('#')[0];

/** The feature the viewport's hovered entity belongs to (a face or edge label in a part, an instance in an assembly). */
function useHoveredFeature(): string | null {
  const hover = useStore((s) => s.hover);
  const mesh = useStore((s) => s.mesh);
  const tree = useStore((s) => s.tree);
  if (!hover || !mesh) return null;
  if (isAssembly(tree)) { const item = itemOf(hover); return item ? (instanceFeatureOf(item.path)?.name ?? item.path.split('.')[0]) : null; }
  const labels = hover.kind === 'face' ? mesh.header.face_labels : hover.kind === 'edge' ? mesh.header.edge_labels : null;
  return labels ? labels[String(hover.id)] || null : null;
}

/** A row scrolls into view when it becomes the selected one. */
function useScrollWhenSelected(selected: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (selected) ref.current?.scrollIntoView({ block: 'nearest' }); }, [selected]);
  return ref;
}
/** A selector shortened for a tree row: `body.faces.top` reads "face of body". */
function shortRef(text: string | undefined): string {
  if (!text) return '';
  const m = /^([A-Za-z_][A-Za-z0-9_]*)\.(faces|edges|vertices)\b/.exec(text);
  if (m) return `${m[2].slice(0, -1)} of ${m[1]}`;
  return text.length > 22 ? `${text.slice(0, 21)}…` : text;
}

/** The one value or reference that defines a feature, for its tree row; the kind stays in the tooltip. */
export function summaryOf(f: Feature): string {
  const a = f.args, t = f.arg_texts;
  const extent = () => (a.upto ? `up to ${shortRef(t.upto)}` : a.through ? 'through' : numText(f, 'depth')) + (a.symmetric ? ' sym' : '');
  switch (f.kind) {
    case 'sketch': return `on ${shortRef(t.on ?? String(a.on ?? ''))}${a.offset ? ` + ${numText(f, 'offset')}` : ''} · ${f.entities.length} entit${f.entities.length === 1 ? 'y' : 'ies'}`;
    case 'extrude': return `${String(a.sketch ?? '')} · ${extent()}${a.op === 'cut' ? ' cut' : ''}`;
    case 'cut': return `${String(a.sketch ?? '')} · ${extent()}`;
    case 'revolve': return `${String(a.sketch ?? '')} · ${numText(f, 'angle') || '360'}°`;
    case 'fillet': return `R${numText(f, 'radius')}`;
    case 'chamfer': return `${numText(f, 'distance')}${a.distance2 != null ? ` × ${numText(f, 'distance2')}` : ''}`;
    case 'shell': return `${numText(f, 'thickness')} thick${a.outward ? ' out' : ''}`;
    case 'plane': return t.between ? 'midplane' : t.through ? 'through 3 points' : t.about ? `${shortRef(t.base ?? String(a.base ?? ''))} ∠ ${numText(f, 'angle')}°`
      : `${shortRef(t.base ?? String(a.base ?? ''))}${a.offset ? ` + ${numText(f, 'offset')}` : ''}`;
    case 'linear_pattern': return `${numText(f, 'count')} × ${numText(f, 'spacing')}${a.count2 != null ? ` · ${numText(f, 'count2')} × ${numText(f, 'spacing2')}` : ''}`;
    case 'circular_pattern': return `${numText(f, 'count')} × ${numText(f, 'angle') || '360'}°`;
    case 'mirror': return `about ${shortRef(t.about ?? String(a.about ?? 'YZ'))}`;
    case 'import_step': case 'instance': return basename(a.path);
    case 'fixed': return String(a.instance ?? '');
    case 'coincident': case 'concentric': case 'distance': case 'parallel': case 'angle':
      return `${f.kind}${a.value != null ? ` ${numText(f, 'value')}${f.kind === 'angle' ? '°' : ''}` : ''}${a.flip ? ' flipped' : ''}`;
    default: return f.kind.replace('_', ' ');
  }
}

const ADDABLE: { kind: FeatureDialogKind; label: string; title: string }[] = [
  { kind: 'fillet', label: 'fillet', title: 'round edges picked in the viewport' },
  { kind: 'chamfer', label: 'chamfer', title: 'bevel edges picked in the viewport' },
  { kind: 'shell', label: 'shell', title: 'hollow the body, removing picked faces' },
  { kind: 'mirror', label: 'mirror body', title: 'mirror the whole body across a plane' },
];

export function FeatureTree() {
  const tree = useStore((s) => s.tree);
  const selected = useStore((s) => s.selected);
  const upto = useStore((s) => s.upto);
  const sketchMode = useStore((s) => s.sketchMode);
  const selectedFace = useStore((s) => s.selectedFace);
  const featureDialog = useStore((s) => s.featureDialog);
  const [planeMenu, setPlaneMenu] = useState(false);
  const [featureMenu, setFeatureMenu] = useState(false);
  const hovered = useHoveredFeature();
  if (!tree) return <div className="panel-empty">no document</div>;
  if (isViewer(tree)) return <ViewerTree tree={tree} />;
  if (isAssembly(tree)) return <AssemblyTree tree={tree} selected={selected} />;
  if (isDrawing(tree)) return <DrawingTree tree={tree} selected={selected} />;

  const rollbackIndex = upto ? tree.features.findIndex((f) => f.name === upto) : tree.features.length - 1;
  const selectedFeature = featureByName(selected);
  const planeFeatures = tree.features.filter((f) => f.kind === 'plane' && f.variable && f.result?.plane);

  // + sketch: on the selected face, on the selected plane feature, or from a menu of planes
  const newSketch = async () => {
    setFeatureMenu(false);
    const scene = sceneRef.current;
    if (selectedFace !== null && scene) {
      const center = scene.entityCenter({ kind: 'face', id: selectedFace });
      const target = center ? selectorTarget({ kind: 'face', id: selectedFace }, center) : null;
      if (target) { await addSketchOn(target); return; }
      setError('the selected face has no owning body feature'); return;
    }
    if (selectedFeature?.kind === 'plane' && selectedFeature.variable && selectedFeature.result?.plane) {
      await addSketchOn({ kind: 'plane', name: selectedFeature.name, expr: selectedFeature.variable, label: `plane ${selectedFeature.name}`, standard: false });
      return;
    }
    setPlaneMenu(!planeMenu);
  };
  const pickPlane = async (name: string) => {
    setPlaneMenu(false);
    if (['XY', 'XZ', 'YZ'].includes(name)) { await addSketchOn(name as 'XY' | 'XZ' | 'YZ'); return; }
    const f = featureByName(name);
    if (f?.variable) await addSketchOn({ kind: 'plane', name, expr: f.variable, label: `plane ${name}`, standard: false });
  };
  const sketchTitle = selectedFace !== null ? `new sketch on the selected face (face ${selectedFace})`
    : selectedFeature?.kind === 'plane' ? `new sketch on ${selectedFeature.name}` : 'new sketch: choose a plane';
  const openFeature = (kind: FeatureDialogKind) => {
    setFeatureMenu(false);
    openFeatureDialog(featureDialog?.kind === kind ? null : kind, null);
  };

  const title = String(tree.meta.name ?? tree.path?.split('/').pop() ?? 'document');
  return (
    <div className="tree">
      <div className="tree-header">
        <span className="mode-chip" data-testid="mode-chip">part</span>
        <span className="tree-title" title={tree.path ?? title}>{title}</span>
      </div>
      <div className="tree-actions">
        <button className="btn-small" onClick={() => { setFeatureMenu(false); openPlaneDialog(getState().planeDialog ? null : 'offset'); }} title="new reference plane" data-testid="new-plane">+ plane</button>
        <button className="btn-small" onClick={newSketch} title={sketchTitle} data-testid="new-sketch">+ sketch</button>
        <button className={`btn-small ${featureMenu ? 'active' : ''}`} onClick={() => { setPlaneMenu(false); setFeatureMenu(!featureMenu); }} title="fillet, chamfer, shell or mirror the body" data-testid="new-feature">+ feature</button>
      </div>
      {(selectedFace !== null || selectedFeature?.kind === 'plane') && (
        <div className="tree-caption" data-testid="sketch-target">next sketch goes on {selectedFace !== null ? `face ${selectedFace}` : selectedFeature?.name}</div>
      )}
      {planeMenu && (
        <div className="plane-menu" data-testid="sketch-plane-menu">
          <span className="panel-help">sketch on</span>
          {['XY', 'XZ', 'YZ'].map((n) => <button key={n} className="btn-small" onClick={() => pickPlane(n)} data-testid={`sketch-on-${n}`}>{n}</button>)}
          {planeFeatures.map((f) => <button key={f.name} className="btn-small" onClick={() => pickPlane(f.name)} data-testid={`sketch-on-${f.name}`}>{f.name}</button>)}
          <span className="panel-help">or select a face or a plane first</span>
        </div>
      )}
      {featureMenu && (
        <div className="feature-menu" data-testid="feature-menu">
          {ADDABLE.map((a) => <button key={a.kind} className="btn-small" onClick={() => openFeature(a.kind)} title={a.title} data-testid={`add-${a.kind}`}>{a.label}</button>)}
          <span className="panel-help">patterns, mirrors and revolves start from the feature or sketch they act on</span>
        </div>
      )}
      {tree.errors.map((e, i) => (
        <div key={i} className="tree-error" title={e.message}>⚠ line {e.line ?? '?'}: {e.message}</div>
      ))}
      {tree.features.map((f, i) => (
        <Row key={f.name} f={f} i={i} selected={f.name === selected} rolledBack={i > rollbackIndex} hovered={hovered === f.name}
             inSketch={sketchMode?.sketch === f.name} isRollbackEnd={i === rollbackIndex && upto !== null} />
      ))}
      {upto && <button className="btn-small tree-showall" onClick={() => setUpto(null)}>show all features</button>}
      {tree.features.length === 0 && <div className="panel-empty">no features yet: add a sketch</div>}
    </div>
  );
}

function Row({ f, i, selected, rolledBack, inSketch, isRollbackEnd, hovered }:
  { f: Feature; i: number; selected: boolean; rolledBack: boolean; inSketch: boolean; isRollbackEnd: boolean; hovered: boolean }) {
  const r = f.result;
  const ref = useScrollWhenSelected(selected);
  const cls = ['tree-row', selected ? 'selected' : '', hovered ? 'hovered' : '', rolledBack ? 'rolled-back' : '', r && !r.ok ? 'failed' : '', inSketch ? 'in-sketch' : '', f.suppressed ? 'suppressed' : '', isRollbackEnd ? 'rollback-end' : ''].join(' ');
  return (
    <div ref={ref} className={cls} onClick={() => select(f.name)} onDoubleClick={() => { if (f.kind === 'sketch' && !f.read_only) enterSketch(f); }}
         onContextMenu={(e) => rowContext(e, f)}
         onMouseEnter={() => setTreeHover({ feature: f.name })} onMouseLeave={() => setTreeHover(null)}
         data-testid={`feature-${f.name}`} title={f.span ? `lines ${f.span[0]}–${f.span[1]}` : 'generated'}>
      <span className="tree-icon" title={f.kind.replace('_', ' ')}>{ICONS[f.kind] ?? '•'}</span>
      <span className="tree-name">{f.name}</span>
      <span className="tree-kind" title={f.kind.replace('_', ' ')}>{summaryOf(f)}</span>
      {f.read_only && <span className="badge" title="created in a loop or function; edit in the code">ro</span>}
      {f.suppressed && <span className="badge" title="suppressed: skipped by the evaluator">off</span>}
      {r?.error && <span className="badge err" title={`${r.error.message}${r.error.line ? ` (line ${r.error.line})` : ''}`}>!</span>}
      {r && r.warnings.length > 0 && <span className="badge warn" title={r.warnings.join('\n')}>⚠</span>}
      <button className={`tree-rollback ${isRollbackEnd ? 'here' : ''}`} title={isRollbackEnd ? 'the model is rolled back to here' : 'roll back: show the model as it is after this feature'}
              onClick={(e) => { e.stopPropagation(); setUpto(f.name); }}>{isRollbackEnd ? '⤒' : '⤓'}</button>
      <span className="tree-index">{i + 1}</span>
    </div>
  );
}

// ---- assemblies: instances, mates and review imports in file order --------------------

const swatchOf = (color: [number, number, number, number] | null | undefined) =>
  color ? `rgb(${color.slice(0, 3).map((c) => Math.round(c * 255)).join(',')})` : null;

/** The mate kinds the "+ mate" button offers; the dialog picks the references. */
export function AssemblyTree({ tree, selected }: { tree: Tree; selected: string | null }) {
  const featureDialog = useStore((s) => s.featureDialog);
  const visibility = useStore((s) => s.visibility);
  const transparency = useStore((s) => s.transparency);
  const title = String(tree.meta.name ?? tree.path?.split('/').pop() ?? 'document');
  const roots = new Map((tree.evaluation.instances ?? []).map((r) => [r.name, r]));
  const sol = tree.evaluation.assembly ?? null;
  const fixed = new Set(tree.features.filter((f) => f.kind === 'fixed' && !f.suppressed && f.result?.ok).map((f) => String(f.args.instance)));
  const open = (kind: FeatureDialogKind) => openFeatureDialog(featureDialog?.kind === kind ? null : kind, null);
  const instances = tree.features.filter((f) => f.kind === 'instance');
  const [info, setInfo] = useState(false);
  const hidden = allLeafPaths().some((p) => visibility[p] === false);
  const hovered = useHoveredFeature();
  const status = !sol ? null : sol.conflicting.length ? { cls: 'bad', text: `${sol.conflicting.length} unsatisfied` }
    : sol.dof === 0 ? { cls: 'ok', text: 'fully constrained' } : { cls: 'free', text: `${sol.free.length} free` };
  return (
    <div className="tree">
      <div className="tree-header">
        <span className="mode-chip" data-testid="mode-chip">assembly</span>
        <span className="tree-title" title={tree.path ?? title}>{title}</span>
        {sol && status && (
          <span className={`asm-status ${status.cls}`} data-testid="assembly-status">
            {status.text}
            <button className="info" onClick={() => setInfo(!info)} title="degrees of freedom, free instances, redundant and unsatisfied mates" data-testid="assembly-status-info">i</button>
          </span>
        )}
      </div>
      {info && sol && (
        <div className="asm-status-pop" data-testid="assembly-status-pop">
          <div>{sol.dof} degree{sol.dof === 1 ? '' : 's'} of freedom over {sol.variables / 6} free instance{sol.variables === 6 ? '' : 's'}</div>
          {sol.free.length > 0 && <div>under-constrained: <span className="mono">{sol.free.join(', ')}</span></div>}
          {sol.redundant.length > 0 && <div>redundant mates: <span className="mono">{sol.redundant.join(', ')}</span></div>}
          {sol.conflicting.length > 0 && <div>not satisfied: <span className="mono">{sol.conflicting.join(', ')}</span></div>}
          {sol.warnings.map((w, i) => <div key={i}>{w}</div>)}
          <div className="panel-help">solved in {sol.ms.toFixed(0)} ms</div>
        </div>
      )}
      <div className="tree-actions">
        <button className={`btn-small ${featureDialog?.kind === 'instance' ? 'active' : ''}`} onClick={() => open('instance')} title="add an instance of a part file or a vendor STEP file" data-testid="new-instance">+ instance</button>
        <button className={`btn-small ${featureDialog?.kind === 'mate' ? 'active' : ''}`} onClick={() => open('mate')} title="mate two instances: pick two references in the viewport" data-testid="new-mate" disabled={instances.length < 2}>+ mate</button>
        {hidden && <button className="btn-small" onClick={showAll} title="show every hidden instance" data-testid="show-all">show all</button>}
      </div>
      {tree.errors.map((e, i) => <div key={i} className="tree-error" title={e.message}>⚠ line {e.line ?? '?'}: {e.message}</div>)}
      {tree.features.map((f, i) => {
        const r = f.result;
        const isInst = f.kind === 'instance';
        // instances pointing into one STEP file are grouped by where they sit in that file
        const fragment = isInst ? (String(f.args.path).split('#')[1] ?? '') : '';
        const group = fragment.includes('.') ? fragment.slice(0, fragment.lastIndexOf('.')) : '';
        const prevInst = tree.features.slice(0, i).reverse().find((x) => x.kind === 'instance');
        const prevFragment = prevInst ? (String(prevInst.args.path).split('#')[1] ?? '') : '';
        const prevGroup = prevFragment.includes('.') ? prevFragment.slice(0, prevFragment.lastIndexOf('.')) : '';
        const depth = group ? group.split('.').length : 0;
        const root = isInst ? roots.get(f.name) : f.kind === 'import_step' ? roots.get(f.name) : undefined;
        const leaves = root ? leafPaths(root) : [];
        const visible = leaves.length === 0 || leaves.some((p) => visibility[p] ?? true);
        const transparent = leaves.length > 0 && leaves.every((p) => transparency[p] ?? false);
        const cls = ['tree-row', f.name === selected ? 'selected' : '', hovered === f.name ? 'hovered' : '', r && !r.ok ? 'failed' : '', f.suppressed ? 'suppressed' : '', visible ? '' : 'hidden-item'].join(' ');
        const isMate = MATE_KINDS.includes(f.kind);
        const refs = isMate ? [f.arg_texts.a, f.arg_texts.b].filter(Boolean).join(' ↔ ') : '';
        return (
          <div key={f.name}>
            {isInst && group && group !== prevGroup && (
              <div className="tree-row tree-group" style={{ paddingLeft: 8 + (depth - 1) * 14 }} title={`bodies of ${group} in ${String(f.args.path).split('#')[0]}`}>
                <span className="tree-icon">⬚</span><span className="tree-name">{group.split('.').pop()}</span><span className="tree-kind">{group}</span>
              </div>
            )}
            <AsmRow selected={f.name === selected} cls={cls} onClick={() => select(f.name)} onContext={(e) => rowContext(e, f)} testId={`feature-${f.name}`} style={isInst && depth ? { paddingLeft: 8 + depth * 14 } : undefined}
                    hover={isInst || f.kind === 'import_step' ? { feature: f.name } : null}
                    title={isMate ? refs || f.arg_texts.instance : f.span ? `lines ${f.span[0]}–${f.span[1]}` : 'generated'}>
              <span className="tree-icon">{ICONS[f.kind] ?? '•'}</span>
              {isInst && <span className="swatch" style={{ background: swatchOf(root?.color) ?? 'transparent', borderStyle: root?.color ? 'solid' : 'dashed' }} />}
              <span className="tree-name">{f.name}</span>
              <span className="tree-kind" title={isInst ? `${root?.kind ?? 'instance'} · ${String(f.args.path)}` : f.kind.replace('_', ' ')}>{summaryOf(f)}</span>
              {isInst && fixed.has(f.name) && <span className="badge" title="fixed: anchored where it is">fixed</span>}
              {isInst && sol?.free.includes(f.name) && <span className="badge warn" title="under-constrained: mates leave it free to move">free</span>}
              {f.read_only && <span className="badge" title="created in a loop or function; edit in the code">ro</span>}
              {f.suppressed && <span className="badge" title="suppressed: skipped by the evaluator">off</span>}
              {r?.error && <span className="badge err" title={`${r.error.message}${r.error.line ? ` (line ${r.error.line})` : ''}`}>!</span>}
              {r && r.ok && r.warnings.length > 0 && <span className="badge warn" title={r.warnings.join('\n')}>⚠</span>}
              {root && (
                <>
                  <input type="checkbox" className="itree-vis" checked={visible} title="visible · alt+click shows only this one"
                         onClick={(e) => { e.stopPropagation(); if (e.altKey) { e.preventDefault(); isolate(leaves); } }}
                         onChange={(e) => { if (!(e.nativeEvent as MouseEvent).altKey) leaves.forEach((p) => setVisibility(p, e.target.checked)); }} data-testid={`vis-${f.name}`} />
                  <button className={`itree-glass ${transparent ? 'on' : ''}`} title="transparent" onClick={(e) => { e.stopPropagation(); leaves.forEach((p) => setTransparency(p, !transparent)); }}>◐</button>
                </>
              )}
              <span className="tree-index">{i + 1}</span>
            </AsmRow>
            {f.kind === 'import_step' && root && root.children.length > 0 && <InstanceTree roots={[root as Instance]} />}
          </div>
        );
      })}
      {tree.features.length === 0 && <div className="panel-empty">no instances yet: add a part file or a STEP file</div>}
    </div>
  );
}

/** An assembly tree row: scrolls into view when selected, lights its instance in the viewport when hovered. */
function AsmRow({ selected, cls, onClick, onContext, testId, style, title, hover, children }: {
  selected: boolean; cls: string; onClick: () => void; onContext: (e: React.MouseEvent) => void; testId: string; style?: React.CSSProperties; title: string;
  hover: { feature?: string; item?: string } | null; children: React.ReactNode;
}) {
  const ref = useScrollWhenSelected(selected);
  return (
    <div ref={ref} className={cls} onClick={onClick} onContextMenu={onContext} data-testid={testId} style={style} title={title}
         onMouseEnter={() => { if (hover) setTreeHover(hover); }} onMouseLeave={() => { if (hover) setTreeHover(null); }}>
      {children}
    </div>
  );
}

/** A STEP file opened for review: its hierarchy, and one action that turns it into an assembly. */
function ViewerTree({ tree }: { tree: Tree }) {
  const [confirm, setConfirm] = useState(false);
  const visibility = useStore((s) => s.visibility);
  const hidden = allLeafPaths().some((p) => visibility[p] === false);
  const title = String(tree.meta.name ?? tree.path?.split('/').pop() ?? 'document');
  const imports = tree.features.filter((f) => f.kind === 'import_step');
  const roots = tree.evaluation.instances ?? [];
  const bodies = roots.reduce((n, r) => n + leafPaths(r).length, 0);
  const failed = tree.features.filter((f) => f.result && !f.result.ok);
  const run = async () => { setConfirm(false); for (const f of imports) await explodeImport(f.name); };
  return (
    <div className="tree">
      <div className="tree-header">
        <span className="mode-chip" data-testid="mode-chip">STEP viewer</span>
        <span className="tree-title" title={tree.path ?? title}>{title}</span>
      </div>
      <div className="tree-actions">
        <span className="delete-wrap">
          <button className="btn-small" onClick={() => setConfirm(!confirm)} title="turn the file's bodies into instances that can be moved, mated and edited" data-testid="make-editable-viewer">make editable</button>
          {confirm && (
            <div className="popover left" data-testid="make-editable-confirm">
              <div>The {bodies} bodies of this file become instances of an assembly, posed as the file has them and still read from the STEP file. Bodies that are not solids are left out. Then "edit part" on a body gives it a part file.</div>
              <div className="btn-row">
                <button className="btn-small" onClick={run} data-testid="make-editable-ok">make editable</button>
                <button className="btn-small" onClick={() => setConfirm(false)}>keep viewing</button>
              </div>
            </div>
          )}
        </span>
        {hidden && <button className="btn-small" onClick={showAll} title="show every hidden body" data-testid="show-all">show all</button>}
        <span className="panel-help">section, measure and snapshot work as they are</span>
      </div>
      {tree.errors.map((e, i) => <div key={i} className="tree-error" title={e.message}>⚠ line {e.line ?? '?'}: {e.message}</div>)}
      {failed.map((f) => <div key={f.name} className="tree-error" title={f.result?.error?.message}>⚠ {f.name}: {f.result?.error?.message}</div>)}
      <InstanceTree roots={roots} />
      {roots.length === 0 && <div className="panel-empty">no instances</div>}
    </div>
  );
}

// ---- drawings: views, dimensions and notes in file order --------------------------------

/** A drawing: its model, the view menu, the dimension and note tools, and one row per feature. */
function DrawingTree({ tree, selected }: { tree: Tree; selected: string | null }) {
  const tool = useStore((s) => s.drawingTool);
  const [viewMenu, setViewMenu] = useState(false);
  const scene = tree.evaluation.drawing ?? null;
  const title = String(tree.meta.name ?? tree.path?.split('/').pop() ?? 'document');
  const modelPath = String(tree.meta.of ?? '');
  const toggle = (t: 'dimension' | 'note') => { setViewMenu(false); setDrawingTool(tool === t ? null : t); };
  const addSection = async () => {
    setViewMenu(false);
    const known = ['XY', 'XZ', 'YZ', ...(scene?.model.planes ?? [])];
    const answer = window.prompt(`Section plane and offset (one of ${known.join(', ')}, then a distance along its normal):`, 'XZ 0');
    if (!answer) return;
    const [plane, off] = answer.trim().split(/[\s,]+/);
    if (!plane) return;
    await addView('section', { plane, offset: Number(off) || 0 });
  };
  return (
    <div className="tree">
      <div className="tree-header">
        <span className="mode-chip" data-testid="mode-chip">drawing</span>
        <span className="tree-title" title={tree.path ?? title}>{title}</span>
      </div>
      <div className="tree-actions">
        <button className={`btn-small ${viewMenu ? 'active' : ''}`} onClick={() => { setDrawingTool(null); setViewMenu(!viewMenu); }} title="add a view of the model" data-testid="new-view">+ view</button>
        <button className={`btn-small ${tool === 'dimension' ? 'active' : ''}`} onClick={() => toggle('dimension')} title="dimension: pick one or two edges on the sheet, then click where the dimension goes" data-testid="new-dimension">+ dimension</button>
        <button className={`btn-small ${tool === 'note' ? 'active' : ''}`} onClick={() => toggle('note')} title="note: click where the text goes" data-testid="new-note">+ note</button>
      </div>
      <div className="tree-caption" data-testid="drawing-model">
        of <span className="mono">{modelPath}</span>{scene?.model.kind ? ` · ${scene.model.kind}` : ''}
        {modelDocPath() && <button className="btn-small" onClick={() => { const p = modelDocPath(); if (p) void openDocument(p); }} title="open the model in a tab; the drawing follows its edits" data-testid="open-model">open model</button>}
      </div>
      {viewMenu && (
        <div className="plane-menu" data-testid="view-menu">
          <span className="panel-help">view from</span>
          {VIEW_DIRECTIONS.map((d) => <button key={d} className="btn-small" onClick={() => { setViewMenu(false); void addView(d); }} data-testid={`view-${d}`}>{d}</button>)}
          <button className="btn-small" onClick={addSection} title="cut the model with a plane and look at the cut face" data-testid="view-section">section…</button>
        </div>
      )}
      {scene?.model.error && <div className="tree-error" title={scene.model.error}>⚠ {scene.model.error}</div>}
      {tree.errors.map((e, i) => <div key={i} className="tree-error" title={e.message}>⚠ line {e.line ?? '?'}: {e.message}</div>)}
      {tree.features.map((f, i) => {
        const r = f.result;
        const dim = scene?.dimensions.find((d) => d.name === f.name);
        const detail = f.kind === 'view' ? (f.args.section ? `section ${String(f.args.section)}` : String(f.args.direction ?? '')) : f.kind === 'dimension' ? (dim ? `${dim.kind} ${dim.text}` : 'dimension') : 'note';
        const cls = ['tree-row', f.name === selected ? 'selected' : '', r && !r.ok ? 'failed' : '', f.suppressed ? 'suppressed' : ''].join(' ');
        return (
          <div key={f.name} className={cls} onClick={() => select(f.name)} onContextMenu={(e) => rowContext(e, f)} data-testid={`feature-${f.name}`} title={f.span ? `lines ${f.span[0]}–${f.span[1]}` : 'generated'}>
            <span className="tree-icon">{ICONS[f.kind] ?? '•'}</span>
            <span className="tree-name">{f.name}</span>
            <span className="tree-kind">{detail}</span>
            {f.read_only && <span className="badge" title="created in a loop or function; edit in the code">ro</span>}
            {f.suppressed && <span className="badge" title="suppressed: skipped by the evaluator">off</span>}
            {r?.error && <span className="badge err" title={`${r.error.message}${r.error.line ? ` (line ${r.error.line})` : ''}`}>!</span>}
            {r && r.ok && r.warnings.length > 0 && <span className="badge warn" title={r.warnings.join('\n')}>⚠</span>}
            <span className="tree-index">{i + 1}</span>
          </div>
        );
      })}
      {tree.features.length === 0 && <div className="panel-empty">no views yet: add one</div>}
    </div>
  );
}
