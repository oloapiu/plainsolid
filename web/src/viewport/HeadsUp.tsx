// The heads-up bar in the viewport's bottom-right corner, clear of the tool panels at the top-left: camera views, the tools that act on the
// model, and the named views. It goes away with the sketch bar in sketch mode.
import { useState } from 'react';
import { useStore, setTool, setShowPlanes, setOrtho, saveNamedView, restoreNamedView, deleteNamedView, isAssembly } from '../state/store';
import { sceneRef } from './Viewport';
import type { ViewName } from './scene';

export function HeadsUp() {
  const tool = useStore((s) => s.tool);
  const section = useStore((s) => s.section);
  const named = useStore((s) => s.named);
  const tree = useStore((s) => s.tree);
  const showPlanes = useStore((s) => s.showPlanes);
  const ortho = useStore((s) => s.ortho);
  const [viewChoice, setViewChoice] = useState('');
  const view = (v: ViewName) => sceneRef.current?.setView(v);
  const onViewChoice = (v: string) => {
    setViewChoice('');
    if (v === '__save') {
      const name = window.prompt('Save the current camera, section, pins and visibility as view:', `view ${Object.keys(named).length + 1}`)?.trim();
      if (name) saveNamedView(name);
    } else if (v.startsWith('__del:')) {
      deleteNamedView(v.slice(6));
    } else if (v) restoreNamedView(v);
  };
  return (
    <div className="hud" data-testid="hud">
      <div className="btn-group">
        {(['iso', 'front', 'top', 'right'] as ViewName[]).map((v) => <button key={v} className="btn-small" onClick={() => view(v)} title={`${v} view (ctrl+1..7 for all)`}>{v}</button>)}
        <button className="btn-small" onClick={() => sceneRef.current?.fit()} title="fit (f)">fit</button>
        <button className={`btn-small ${ortho ? 'active' : ''}`} onClick={() => setOrtho(!ortho)} title="orthographic projection (sketches switch to it and back)" data-testid="toggle-ortho">ortho</button>
      </div>
      <div className="btn-group">
        <button className={`btn-small ${tool === 'section' || section ? 'active' : ''}`} onClick={() => setTool('section')} title="section plane" data-testid="tool-section">section</button>
        <button className={`btn-small ${tool === 'measure' ? 'active' : ''}`} onClick={() => setTool('measure')} title="measure entities" data-testid="tool-measure">measure</button>
        {isAssembly(tree) && <button className={`btn-small ${tool === 'move' ? 'active' : ''}`} onClick={() => setTool('move')} title="drag parts along what their mates leave free (m slide, r turn)" data-testid="tool-move">move</button>}
        <button className={`btn-small ${showPlanes ? 'active' : ''}`} onClick={() => setShowPlanes(!showPlanes)} title="show reference planes and the standard planes" data-testid="toggle-planes">planes</button>
      </div>
      <select className="views-select" value={viewChoice} onChange={(e) => onViewChoice(e.target.value)} title="named views" data-testid="views-select">
        <option value="">views…</option>
        <option value="__save">save current view…</option>
        {Object.keys(named).map((n) => <option key={n} value={n}>{n}</option>)}
        {Object.keys(named).length > 0 && <optgroup label="delete">{Object.keys(named).map((n) => <option key={`d${n}`} value={`__del:${n}`}>delete {n}</option>)}</optgroup>}
      </select>
    </div>
  );
}
