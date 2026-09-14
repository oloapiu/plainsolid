import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { getState, setSection, selectFace, addSketchOn, setShowPlanes, openPlaneDialog, setSketchSelection, openFeatureDialog, pick, select, edit, setOrtho, setSketchTool, useBodyInRelation, exitSketch, addInstance, addMate, fixInstance, fetchAssemblyQueries, selectorTarget, makeEditable, closeDocument, exportDocument, explodeImport, setDrawingTool, drawingPick, placeDimension, addNoteAt, moveDrawingItem, addView, openDocument, newDocument, setTool, setMoveMode, previewMate, clearPosePreview, beginInstanceDrag, moveInstanceDrag, endInstanceDrag, toggleBodySelect, convertBodySelection, previewFeature, clearFeaturePreview, setOverlay, setError } from './state/store';
import type { EntityKind } from './api/types';
import { sceneRef } from './viewport/Viewport';
import { frameFromInfo } from './viewport/scene';
import { toWorld } from './sketch/draw';
import { codeGoToLine } from './code/CodePane';
import './styles.css';

// hooks for the verification script and for debugging in the console: read-only state,
// the scene's clip count, and the section action (so a commit can be timed precisely)
(window as unknown as { __plainsolid: unknown }).__plainsolid = {
  getState,
  clipCount: () => sceneRef.current?.clipCount() ?? 0,
  planeCount: () => sceneRef.current?.planeCount() ?? 0,
  isOrtho: () => sceneRef.current?.isOrtho() ?? false,
  hasGhost: () => sceneRef.current?.hasGhostMesh() ?? false,
  pixelSize: () => sceneRef.current?.pixelSize() ?? null,
  /** Put the code pane's cursor on a line, as a click there would (the feature on that line gets selected). */
  codeCursorTo: (line: number) => codeGoToLine(line, false),
  /** Sketch-plane coordinates under a client position, in sketch mode. */
  planePointAt: (x: number, y: number) => sceneRef.current?.planePointAt(x, y) ?? null,
  actions: {
    setSection, selectFace, addSketchOn, setShowPlanes, openPlaneDialog, setSketchSelection, openFeatureDialog, select, edit, setOrtho, setSketchTool, exitSketch,
    addInstance, addMate, fixInstance, fetchAssemblyQueries, makeEditable, closeDocument, exportDocument, explodeImport,
    setDrawingTool, drawingPick, placeDimension, addNoteAt, moveDrawingItem, addView, openDocument, newDocument,
    setTool, setMoveMode, previewMate, clearPosePreview, beginInstanceDrag, endInstanceDrag, toggleBodySelect, convertBodySelection, previewFeature, clearFeaturePreview, setOverlay, setError,
    /** The reference point of an entity, the one a nearest() selector is written from. */
    entityCenter: (entity: { kind: EntityKind; id: number }) => sceneRef.current?.entityCenter(entity) ?? null,
    /** Drag the picked instance by a screen delta the way the viewport would. */
    dragInstance: (dx: number, dy: number) => { const scene = sceneRef.current; if (scene) moveInstanceDrag(dx, dy, scene.screenAxes()); },
    /** The selector expression a click on an entity would write (semantic when the identity map allows). */
    selectorFor: (kind: EntityKind, id: number) => { const c = sceneRef.current?.entityCenter({ kind, id }); return c ? selectorTarget({ kind, id }, c)?.expr ?? null : null; },
    /** Sketch mode: a modifier-click on a body edge or vertex, as the viewport would report it. */
    useBodyInRelation: (kind: EntityKind, id: number) => useBodyInRelation({ kind, id }, sceneRef.current?.entityCenter({ kind, id }) ?? null),
    /** Click an entity the way the viewport would: through the scene's current pick handler, so
     * whatever tool has taken it over (or handed it back) is exercised too. */
    pickEntity: (kind: EntityKind, id: number) => { const scene = sceneRef.current; if (scene) scene.onPick({ kind, id }); else pick({ kind, id }, null); },
  },
  /** Screen position (px in the canvas) of a sketch-plane point, in sketch mode. */
  sketchToScreen: (u: number, v: number) => {
    const scene = sceneRef.current, sm = getState().sketchMode;
    if (!scene || !sm) return null;
    const [x, y] = scene.toScreen(toWorld(frameFromInfo(sm.frame), [u, v]));
    const r = scene.renderer.domElement.getBoundingClientRect();
    return [x + r.left, y + r.top];
  },
};

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
