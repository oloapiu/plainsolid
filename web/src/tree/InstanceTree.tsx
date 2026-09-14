import { useEffect, useRef, useState } from 'react';
import type { Instance } from '../api/types';
import { useStore, selectItem, setVisibility, setTransparency, leafPaths, isolate, setTreeHover, itemOf, openContextMenu } from '../state/store';
import { viewerNodeMenu } from '../menu/entries';

export function InstanceTree({ roots }: { roots: Instance[] }) {
  return <div className="itree">{roots.map((r) => <Node key={r.path} inst={r} depth={0} />)}</div>;
}

function Node({ inst, depth }: { inst: Instance; depth: number }) {
  const selected = useStore((s) => s.selectedItem);
  const hover = useStore((s) => s.hover);
  const visibility = useStore((s) => s.visibility);
  const transparency = useStore((s) => s.transparency);
  const [open, setOpen] = useState(true);
  const leaves = leafPaths(inst);
  const visible = leaves.some((p) => visibility[p] ?? true);
  const transparent = leaves.length > 0 && leaves.every((p) => transparency[p] ?? false);
  const color = inst.color ? `rgb(${inst.color.slice(0, 3).map((c) => Math.round(c * 255)).join(',')})` : null;
  const toggleVisible = (v: boolean) => leaves.forEach((p) => setVisibility(p, v));
  const toggleTransparent = () => leaves.forEach((p) => setTransparency(p, !transparent));
  const isSelected = selected === inst.path;
  const hoveredPath = hover ? itemOf(hover)?.path ?? null : null;
  const isHovered = hoveredPath !== null && inst.children.length === 0 && hoveredPath === inst.path;
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (isSelected) ref.current?.scrollIntoView({ block: 'nearest' }); }, [isSelected]);
  return (
    <>
      <div ref={ref} className={`tree-row itree-row ${isSelected ? 'selected' : ''} ${isHovered ? 'hovered' : ''} ${visible ? '' : 'hidden-item'}`} style={{ paddingLeft: 8 + depth * 14 }}
           onClick={() => selectItem(inst.path)} data-testid={`instance-${inst.path}`} title={`${inst.path} · product ${inst.product}`}
           onContextMenu={(e) => { e.preventDefault(); selectItem(inst.path); openContextMenu(e.clientX, e.clientY, viewerNodeMenu(inst), inst.name); }}
           onMouseEnter={() => setTreeHover({ item: inst.path })} onMouseLeave={() => setTreeHover(null)}>
        {inst.children.length > 0
          ? <button className="itree-toggle" onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>{open ? '▾' : '▸'}</button>
          : <span className="itree-toggle" />}
        <input type="checkbox" className="itree-vis" checked={visible} title="visible · alt+click shows only this one"
               onClick={(e) => { e.stopPropagation(); if (e.altKey) { e.preventDefault(); isolate(leaves); } }}
               onChange={(e) => { if (!(e.nativeEvent as MouseEvent).altKey) toggleVisible(e.target.checked); }} data-testid={`vis-${inst.path}`} />
        <span className="swatch" style={{ background: color ?? 'transparent', borderStyle: color ? 'solid' : 'dashed' }} />
        <span className="tree-name">{inst.name}</span>
        <span className="tree-kind">{inst.children.length ? `asm ${inst.children.length}` : inst.solids !== 1 ? `${inst.solids} solids` : ''}</span>
        <button className={`itree-glass ${transparent ? 'on' : ''}`} title="transparent" onClick={(e) => { e.stopPropagation(); toggleTransparent(); }}>◐</button>
      </div>
      {open && inst.children.map((c) => <Node key={c.path} inst={c} depth={depth + 1} />)}
    </>
  );
}
