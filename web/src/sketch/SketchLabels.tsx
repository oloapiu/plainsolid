// HTML overlay for sketch mode: dimension labels (click to edit, drag to place) and
// constraint glyphs, positioned by projecting plane points to the screen.
import { useEffect, useState } from 'react';
import type { PlaneFrame, Scene3D } from '../viewport/scene';
import { useStore, setConstraintValue, setDimEditing, setSketchHighlight, deleteConstraint, expressionNames, sketchFeature } from '../state/store';
import { dimensionGeometry, refAnchor, GLYPH, fmtNum, type SketchModel, type Pt } from './model';
import { toWorld } from './draw';
import { ExprInput } from '../panel/ExprInput';

export type Placements = Record<string, Pt>;

export function SketchLabels({ scene, frame, model, placements, onPlace, tick }: {
  scene: Scene3D; frame: PlaneFrame; model: SketchModel; placements: Placements; onPlace: (name: string, p: Pt) => void; tick: number;
}) {
  const sm = useStore((s) => s.sketchMode);
  const feature = sketchFeature();
  const [, force] = useState(0);
  useEffect(() => { force((x) => x + 1); }, [tick]);
  if (!sm || !feature) return null;
  const project = (p: Pt) => scene.toScreen(toWorld(frame, p));
  const names = expressionNames();
  const glyphs: { key: string; x: number; y: number; text: string; refs: string[]; title: string; conflict: boolean; redundant: boolean }[] = [];
  const slots = new Map<string, number>();
  const dims: { c: (typeof model.constraints)[number]; x: number; y: number }[] = [];
  for (const c of model.constraints) {
    if (c.dimension) {
      const g = dimensionGeometry(model, c);
      if (!g) continue;
      const p = placements[c.name] ?? g.label;
      const [x, y] = project(p);
      dims.push({ c, x, y });
      continue;
    }
    const anchor = refAnchor(model, c.refs[0]);
    if (!anchor) continue;
    const [x, y] = project(anchor);
    const key = `${Math.round(x / 4)},${Math.round(y / 4)}`;
    const n = slots.get(key) ?? 0;
    slots.set(key, n + 1);
    glyphs.push({ key: c.name, x: x + 10 + n * 16, y: y - 12, text: GLYPH[c.kind] ?? c.kind[0], refs: c.refs, title: `${c.kind} ${c.name}: ${c.refs.join(', ')}`,
                  conflict: model.solution?.conflicting?.includes(c.name) ?? false, redundant: model.solution?.redundant?.includes(c.name) ?? false });
  }
  // while a drawing tool or a drag is active the labels must not intercept the pointer
  const passive = (sm.tool !== null && sm.tool !== 'dimension') || sm.dragging !== null || sm.dimPlacing !== null;
  return (
    <div className={`sketch-labels ${passive ? 'passive' : ''}`}>
      {glyphs.map((g) => (
        <span key={g.key} className={`glyph ${g.conflict ? 'conflict' : ''} ${g.redundant ? 'redundant' : ''}`} style={{ left: g.x, top: g.y }} title={g.title}
              onMouseEnter={() => setSketchHighlight(g.refs)} onMouseLeave={() => setSketchHighlight([])}
              onClick={(e) => { e.stopPropagation(); if (e.altKey || e.shiftKey) deleteConstraint(g.key); }}>{g.text}</span>
      ))}
      {dims.map(({ c, x, y }) => (
        <DimLabel key={c.name} name={c.name} kind={c.kind} x={x} y={y} text={c.value_text ?? fmtNum(c.value ?? 0)} value={c.value ?? 0}
                  editing={sm.dimEditing === c.name} names={names} refs={c.refs} scene={scene}
                  conflict={model.solution?.conflicting?.includes(c.name) ?? false} onPlace={(p) => onPlace(c.name, p)} />
      ))}
    </div>
  );
}

function DimLabel({ name, kind, x, y, text, value, editing, names, refs, scene, conflict, onPlace }: {
  name: string; kind: string; x: number; y: number; text: string; value: number; editing: boolean; names: string[]; refs: string[];
  scene: Scene3D; conflict: boolean; onPlace: (p: Pt) => void;
}) {
  const shown = kind === 'diameter' ? `Ø${fmtNum(value)}` : kind === 'radius' ? `R${fmtNum(value)}` : kind === 'angle' ? `${fmtNum(value)}°` : fmtNum(value);
  const isExpr = !/^-?\d+(\.\d+)?$/.test(text.trim());
  const onDown = (e: React.PointerEvent) => {
    if (editing) return;
    e.stopPropagation();
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture(e.pointerId);
    const start = [e.clientX, e.clientY];
    let dragged = false;
    const move = (ev: PointerEvent) => {
      if (!dragged && Math.hypot(ev.clientX - start[0], ev.clientY - start[1]) < 3) return;
      dragged = true;
      const p = scene.planePointAt(ev.clientX, ev.clientY);
      if (p) onPlace(p);
    };
    const up = (ev: PointerEvent) => {
      el.releasePointerCapture(ev.pointerId);
      el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up);
      if (!dragged) setDimEditing(name);
    };
    el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
  };
  return (
    <div className={`dim-label ${conflict ? 'conflict' : ''} ${editing ? 'editing' : ''}`} style={{ left: x, top: y }} data-testid={`dim-${name}`}
         onPointerDown={onDown} onMouseEnter={() => setSketchHighlight(refs)} onMouseLeave={() => setSketchHighlight([])} title={`${kind} ${name} = ${text}`}>
      {editing
        ? <ExprInput text={text} names={names} autoFocus testId={`dim-input-${name}`} onCommit={(t) => setConstraintValue(name, t)} onCancel={() => setDimEditing(null)} />
        : <span className="dim-text">{shown}{isExpr && <span className="dim-expr"> = {text}</span>}</span>}
    </div>
  );
}
