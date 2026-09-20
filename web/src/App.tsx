import React, { useEffect, useState } from 'react';
import { Viewport, sceneRef } from './viewport/Viewport';
import { FeatureTree } from './tree/FeatureTree';
import { CodePane } from './code/CodePane';
import { PropertyPanel } from './panel/PropertyPanel';
import {
  useStore, start, useDocument, undo, redo, setStatus, setError, setTool, clearMeasure,
  dismissViewsWarning, isAssembly, isDrawing, fmt, requestPick,
  closeDocument, exportDocument, setMoveMode,
  setOverlay, requestDelete, select, openFeatureDialog, openPlaneDialog, setDrawingTool, setDeleteConfirm,
  requestImport, quitServer,
} from './state/store';
import { DrawingSheet } from './drawing/DrawingSheet';
import { Menu } from './Menu';
import { Help } from './Help';
import { ContextMenu } from './menu/ContextMenu';
import { OpenMenu, NewMenu, ImportDialog } from './FilesMenu';
import type { ImportItem } from './api/types';

const readNumber = (key: string, fallback: number) => { try { return Number(localStorage.getItem(key)) || fallback; } catch { return fallback; } };
/** The viewport never gets narrower than this; the side panels give way first. */
const VIEWPORT_MIN = 420;
/** A collapsed side panel keeps this much: its reopen button. */
const SIDE_STRIP = 18;

const readFlag = (key: string, fallback: boolean) => { try { const v = localStorage.getItem(key); return v === null ? fallback : v === '1'; } catch { return fallback; } };
const remember = (key: string, value: string) => { try { localStorage.setItem(key, value); } catch { /* fine */ } };

export function App() {
  const docs = useStore((s) => s.docs);
  const docId = useStore((s) => s.docId);
  const status = useStore((s) => s.status);
  const error = useStore((s) => s.error);
  const hash = useStore((s) => s.hash);
  const tool = useStore((s) => s.tool);
  const sketchMode = useStore((s) => s.sketchMode);
  const section = useStore((s) => s.section);
  const sectionPending = useStore((s) => s.sectionPending);
  const sectionSeconds = useStore((s) => s.sectionSeconds);
  const viewsWarning = useStore((s) => s.viewsWarning);
  const tree = useStore((s) => s.tree);
  const pickRequest = useStore((s) => s.pickRequest);
  const selected = useStore((s) => s.selected);
  const selectedFace = useStore((s) => s.selectedFace);
  const selectedEdge = useStore((s) => s.selectedEdge);
  const selectedItem = useStore((s) => s.selectedItem);
  const featureDialog = useStore((s) => s.featureDialog);
  const planeDialog = useStore((s) => s.planeDialog);
  const drawingTool = useStore((s) => s.drawingTool);
  const moveMode = useStore((s) => s.moveMode);
  const measurePicks = useStore((s) => s.measure.picks.length);
  const deleteConfirm = useStore((s) => s.deleteConfirm);
  const codeReveal = useStore((s) => s.codeReveal);
  const overlay = useStore((s) => s.overlay);
  const [help, setHelp] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const [openSignal, setOpenSignal] = useState(0);
  // the side panels: draggable widths and collapse toggles, remembered per browser; on a small
  // window the panels give way so the viewport keeps VIEWPORT_MIN
  const [leftWidth, setLeftWidth] = useState(() => readNumber('plainsolid.leftWidth', 250));
  const [rightWidth, setRightWidth] = useState(() => readNumber('plainsolid.rightWidth', 300));
  const [leftOpen, setLeftOpen] = useState(() => readFlag('plainsolid.leftOpen', true));
  const [rightOpen, setRightOpen] = useState(() => readFlag('plainsolid.rightOpen', true));
  const [codeOpen, setCodeOpen] = useState(() => readFlag('plainsolid.codeOpen', true));
  const [codeHeight, setCodeHeight] = useState(() => readNumber('plainsolid.codeHeight', 220));
  const clampSide = (side: 'left' | 'right', w: number) => {
    const other = side === 'left' ? (rightOpen ? rightWidth : SIDE_STRIP) : (leftOpen ? leftWidth : SIDE_STRIP);
    const room = window.innerWidth - other - VIEWPORT_MIN - 10;
    const [lo, hi] = side === 'left' ? [170, 480] : [220, 720];
    return Math.max(lo, Math.min(hi, room, Math.round(w)));
  };
  const startResize = (side: 'left' | 'right') => (e: React.PointerEvent<HTMLDivElement>) => {
    const x0 = e.clientX, w0 = side === 'left' ? leftWidth : rightWidth;
    const el = e.currentTarget;
    el.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => {
      const w = side === 'left' ? w0 + (ev.clientX - x0) : w0 - (ev.clientX - x0);
      (side === 'left' ? setLeftWidth : setRightWidth)(clampSide(side, w));
    };
    const up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
  };
  useEffect(() => {
    const fit = () => { setLeftWidth((w) => clampSide('left', w)); setRightWidth((w) => clampSide('right', w)); };
    fit();
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  }, [leftOpen, rightOpen, leftWidth, rightWidth]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => remember('plainsolid.leftWidth', String(leftWidth)), [leftWidth]);
  useEffect(() => remember('plainsolid.rightWidth', String(rightWidth)), [rightWidth]);
  useEffect(() => remember('plainsolid.leftOpen', leftOpen ? '1' : '0'), [leftOpen]);
  useEffect(() => remember('plainsolid.rightOpen', rightOpen ? '1' : '0'), [rightOpen]);
  useEffect(() => remember('plainsolid.codeOpen', codeOpen ? '1' : '0'), [codeOpen]);
  useEffect(() => remember('plainsolid.codeHeight', String(codeHeight)), [codeHeight]);
  useEffect(() => { if (codeReveal) setCodeOpen(true); }, [codeReveal]);

  useEffect(() => { start(); }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const mod = e.ctrlKey || e.metaKey;
      // the tab and open keys work from anywhere; the rest stay out of text fields and the code pane
      if (mod && (e.key === 'Tab' || e.key === ']' || e.key === '[')) {
        e.preventDefault();
        if (docs.length > 1) { const i = docs.findIndex((d) => d.id === docId); const step = e.key === '[' || (e.key === 'Tab' && e.shiftKey) ? -1 : 1; void useDocument(docs[(i + step + docs.length) % docs.length].id); }
        return;
      }
      if (mod && e.key.toLowerCase() === 'o') { e.preventDefault(); setOpenSignal((n) => n + 1); return; }
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.closest('.cm-editor'))) return;
      if (mod && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
      if (mod && e.key === '`') { e.preventDefault(); setCodeOpen((o) => !o); return; }
      if (e.key === '?' && !mod) { setHelp((h) => !h); return; }
      if (e.key === 'Escape') {
        if (help) { setHelp(false); return; }
        if (deleteConfirm) { setDeleteConfirm(null); return; }
        if (pickRequest) { requestPick(null); return; }
        if (tool === 'measure') { clearMeasure(); return; }
        if (tool === 'move') { setTool('move'); return; }
        if (sketchMode) return;  // the sketch overlay owns escape inside a sketch
        if (featureDialog) { openFeatureDialog(null); return; }
        if (planeDialog) { openPlaneDialog(null); return; }
        if (drawingTool) { setDrawingTool(null); return; }
        if (tool === 'section') { setTool('section'); return; }
        if (selected || selectedFace !== null || selectedEdge !== null || selectedItem) select(null);
        return;
      }
      if ((e.key === 'Delete' || e.key === 'Backspace') && !mod && !sketchMode) { e.preventDefault(); requestDelete(); return; }
      if (!mod && isAssembly(tree) && !sketchMode && (e.key === 'm' || e.key === 'r')) { e.preventDefault(); setMoveMode(e.key === 'm' ? 'translate' : 'rotate'); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [tool, pickRequest, tree, sketchMode, help, featureDialog, planeDialog, drawingTool, selected, selectedFace, selectedEdge, selectedItem, deleteConfirm, docs, docId]);

  const drawing = isDrawing(tree);
  // a STEP file dropped anywhere on the window is offered to the import dialog, which copies it in
  const onDragOver = (e: React.DragEvent) => { if (Array.from(e.dataTransfer.types).includes('Files')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } };
  const onDrop = (e: React.DragEvent) => {
    const dropped = Array.from(e.dataTransfer.files);
    if (!dropped.length) return;
    e.preventDefault();
    const items: ImportItem[] = [], rejected: string[] = [];
    for (const f of dropped) {
      const dot = f.name.lastIndexOf('.');
      const suffix = dot > 0 ? f.name.slice(dot) : '';
      if (/^\.(step|stp)$/i.test(suffix)) items.push({ name: f.name.slice(0, dot), suffix, file: f });
      else rejected.push(f.name);
    }
    if (rejected.length) setError(`only STEP files are imported, not ${rejected.join(', ')}`);
    requestImport(items);
  };
  const exportStep = () => {
    const name = String(tree?.meta.name ?? 'model');
    if (drawing) {
      const target = window.prompt('Export the drawing to (.pdf, .dxf or .svg, relative to the document):', `${name}.pdf`);
      const fmt_ = target?.trim().split('.').pop()?.toLowerCase();
      if (target && (fmt_ === 'pdf' || fmt_ === 'dxf' || fmt_ === 'svg')) void exportDocument(fmt_, target);
      else if (target) setError('the drawing exports to .pdf, .dxf or .svg');
      return;
    }
    const target = window.prompt('Export STEP to (relative to the document):', `${name}.step`);
    if (target) void exportDocument('step', target);
  };
  const snapshot = async () => {
    const scene = sceneRef.current;
    if (!scene) return;
    try {
      const blob = await scene.snapshot();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `plainsolid-${Date.now()}.png`; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      let copied = false;
      try {
        if (navigator.clipboard && 'write' in navigator.clipboard && typeof ClipboardItem !== 'undefined') {
          await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
          copied = true;
        }
      } catch { /* clipboard not permitted */ }
      setStatus(copied ? 'snapshot saved and copied to the clipboard' : 'snapshot saved');
    } catch (e) { setError((e as Error).message); }
  };

  // what the active tool wants next, else what a click or a right-click would do here
  const hint = pickRequest ? `${pickRequest.hint} · esc cancels`
    : tool === 'measure' ? (measurePicks === 0 ? 'measure: click a face, an edge or a vertex' : measurePicks === 1 ? 'measure: click a second entity, or pin' : 'measure: two entities · a third click starts over')
    : tool === 'section' ? 'section: drag the slider or type an offset · flip · clear · right-click a face sets the offset to it'
    : tool === 'move' ? (moveMode === 'translate' ? 'move: drag a part to slide it · r turns' : 'move: drag a part to turn it · m slides')
    : sketchMode ? 'sketch: esc stops the tool · d dimension · e convert · x construction switch · shift held draws without snapping · right-click for relations and edits'
    : drawing ? 'drag a view, dimension or note to move it · scroll zooms · right-click for actions'
    : selectedEdge !== null ? 'edge selected · right-click: fillet, chamfer, plane, measure · delete removes its feature'
    : selectedFace !== null ? 'face selected · right-click: sketch here, plane, normal to (ctrl+0)'
    : 'click a face or an edge · right-click for actions · ? lists the keys';
  const rows = `36px ${viewsWarning ? '24px ' : ''}1fr ${codeOpen ? Math.min(600, Math.max(120, codeHeight)) : 24}px 22px`;

  return (
    <div className="app" style={{ gridTemplateRows: rows }} onDragOver={onDragOver} onDrop={onDrop}>
      <div className="toolbar">
        <span className="brand">plainsolid</span>
        <div className="doc-tabs" data-testid="doc-tabs">
          {docs.map((d) => (
            <span key={d.id} className={`doc-tab ${d.id === docId ? 'active' : ''}`} onClick={() => useDocument(d.id)}
                  onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); void closeDocument(d.id); } }}
                  title={`${d.path}${d.kind ? ` · ${d.kind}` : ''} · middle-click closes`} data-testid={`doc-tab-${d.name}`}>
              <span className="doc-tab-name">{d.name}</span>
              <button className="doc-tab-close" title="close" onClick={(e) => { e.stopPropagation(); closeDocument(d.id); }}>×</button>
            </span>
          ))}
          {docs.length === 0 && <span className="doc-tab empty">no document</span>}
        </div>
        <OpenMenu compare={compareOpen} onCompareDone={() => setCompareOpen(false)} openSignal={openSignal} />
        <NewMenu />
        <ImportDialog />
        <span className="spacer" />
        <div className="btn-group">
          <button className="btn-small btn-glyph" onClick={undo} title="undo (ctrl+z)" data-testid="undo">↶</button>
          <button className="btn-small btn-glyph" onClick={redo} title="redo (ctrl+shift+z)" data-testid="redo">↷</button>
        </div>
        <Menu label="file" title="snapshot, export, compare, quit" testId="file-menu">
          {tree && !drawing && <button className="btn-small" type="button" onClick={snapshot} title="save the viewport as PNG and copy it" data-testid="snapshot">snapshot</button>}
          {tree && <button className="btn-small" type="button" onClick={exportStep} data-testid="export-step"
            title={drawing ? 'write the sheet to a PDF, DXF or SVG file' : 'write the part, or the posed assembly with its instance names and colours, to a STEP file'}>{drawing ? 'export…' : 'export STEP'}</button>}
          {tree && !drawing && !overlay && <button className="btn-small" type="button" data-testid="compare-file"
            title="overlay another document: what this one adds is green, what it lacks red"
            onClick={() => setCompareOpen(true)}>compare with…</button>}
          {tree && !drawing && !overlay && <button className="btn-small" type="button" data-testid="compare-head"
            title="overlay this file as last committed to git" onClick={() => void setOverlay({ rev: 'HEAD' })}>compare with last commit</button>}
          {tree && overlay && <button className="btn-small" type="button" data-testid="compare-stop" onClick={() => void setOverlay(null)}>stop comparing</button>}
          <button className="btn-small danger" type="button" data-testid="quit-server" title="stop the server; every tab of the app loses it until plainsolid serve runs again"
            onClick={() => void quitServer()}>quit server</button>
        </Menu>
      </div>
      {viewsWarning && <div className="views-warning">{viewsWarning} <button className="btn-small" onClick={dismissViewsWarning}>ok</button></div>}
      <div className="main" style={{ gridTemplateColumns: `${leftOpen ? `${leftWidth}px 5px` : `${SIDE_STRIP}px 0px`} 1fr ${rightOpen ? `5px ${rightWidth}px` : `0px ${SIDE_STRIP}px`}` }}>
        <div className={`left ${leftOpen ? '' : 'collapsed'}`}>
          <button className="side-toggle" onClick={() => setLeftOpen((o) => !o)} title={leftOpen ? 'hide the tree' : 'show the tree'} data-testid="left-toggle">{leftOpen ? '◂' : '▸'}</button>
          {leftOpen && <FeatureTree />}
        </div>
        <div className={`splitter ${leftOpen ? '' : 'off'}`} onPointerDown={startResize('left')} title="drag to resize the tree" data-testid="left-splitter" />
        {drawing ? <DrawingSheet /> : <Viewport />}
        <div className={`splitter ${rightOpen ? '' : 'off'}`} onPointerDown={startResize('right')} title="drag to resize the panel" data-testid="right-splitter" />
        <div className={`right ${rightOpen ? '' : 'collapsed'}`}>
          <button className="side-toggle" onClick={() => setRightOpen((o) => !o)} title={rightOpen ? 'hide the panel' : 'show the panel'} data-testid="right-toggle">{rightOpen ? '▸' : '◂'}</button>
          {rightOpen && <PropertyPanel />}
        </div>
        {error && (
          <div className="error-banner" data-testid="error-banner" role="alert">
            <span>{error}</span>
            <button className="doc-tab-close" onClick={() => setError(null)} title="dismiss">×</button>
          </div>
        )}
      </div>
      <CodePane open={codeOpen} onToggle={() => setCodeOpen((o) => !o)} onResize={(h) => setCodeHeight(Math.min(600, Math.max(120, h)))} />
      <div className="statusbar">
        <span data-testid="status-text">{status}</span>
        <span className="spacer" />
        {section && (
          <span className="status-section" data-testid="status-section">
            {sectionPending ? 'sectioning…' : `section ${section.plane} ${section.offset}${section.flip ? ' flipped' : ''}${sectionSeconds !== null ? ` · ${fmt(sectionSeconds)} s` : ''}`}
          </span>
        )}
        <span className="status-hash" aria-hidden="true">{hash}</span>
        <span className="status-hint" data-testid="status-hint">{hint}</span>
        <button className="btn-small status-help-btn" onClick={() => setHelp(!help)} title="the keys (?)" data-testid="help-toggle">?</button>
      </div>
      {help && <Help onClose={() => setHelp(false)} />}
      <ContextMenu />
    </div>
  );
}
