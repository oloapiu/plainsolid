import { useEffect, useRef } from 'react';
import { Scene3D, clipPlane, type ViewName, type MeasureDrawing, type PlaneDrawing } from './scene';
import { useStore, pick, planePicked, hoverEntity, getState, cameraChanged, cameraApplied, fitApplied, measureText, measurePoints, leafPaths, instanceByPath, installedSectionMatches, entityTags, tagText, beginInstanceDrag, moveInstanceDrag, endInstanceDrag, setSceneCenter, openContextMenu, selectFace, selectEdge, itemOf, instanceFeatureOf, isAssembly } from '../state/store';
import { viewportMenu } from '../menu/entries';
import { MovePanel } from './MovePanel';
import { HeadsUp } from './HeadsUp';
import type { PlaneInfo } from '../api/types';

const STANDARD: Record<string, [number[], number[], number[]]> = {
  XY: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], XZ: [[1, 0, 0], [0, 0, 1], [0, -1, 0]], YZ: [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
};
function standardPlane(name: string, size: number): PlaneInfo {
  const [x, y, z] = STANDARD[name];
  return { origin: [0, 0, 0], x_dir: x as [number, number, number], y_dir: y as [number, number, number], z_dir: z as [number, number, number], size };
}
import { SketchOverlay } from '../sketch/SketchOverlay';
import { SectionPanel } from './SectionPanel';

export const sceneRef: { current: Scene3D | null } = { current: null };
export { planeFrame, frameFromInfo } from './scene';

export function Viewport() {
  const ref = useRef<HTMLDivElement>(null);
  const mesh = useStore((s) => s.mesh);
  const selected = useStore((s) => s.selected);
  const selectedItem = useStore((s) => s.selectedItem);
  const selectedFace = useStore((s) => s.selectedFace);
  const selectedEdge = useStore((s) => s.selectedEdge);
  const ghostStyle = useStore((s) => s.ghostStyle);
  const bodySelection = useStore((s) => s.sketchMode?.bodySelection ?? null);
  const hover = useStore((s) => s.hover);
  const sketchMode = useStore((s) => s.sketchMode);
  const loading = useStore((s) => s.loading);
  const tool = useStore((s) => s.tool);
  const section = useStore((s) => s.section);
  const previewOffset = useStore((s) => s.previewOffset);
  const sectionPending = useStore((s) => s.sectionPending);
  const visibility = useStore((s) => s.visibility);
  const transparency = useStore((s) => s.transparency);
  const measure = useStore((s) => s.measure);
  const pins = useStore((s) => s.pins);
  const cameraToApply = useStore((s) => s.cameraToApply);
  const tree = useStore((s) => s.tree);
  const showPlanes = useStore((s) => s.showPlanes);
  const pickRequest = useStore((s) => s.pickRequest);
  const dialogPicks = useStore((s) => s.dialogPicks);
  const ortho = useStore((s) => s.ortho);
  const ghostMesh = useStore((s) => s.ghostMesh);
  const moveMode = useStore((s) => s.moveMode);
  const posePreview = useStore((s) => s.posePreview);
  const busy = useStore((s) => s.busy);
  const treeHover = useStore((s) => s.treeHover);
  const refHighlight = useStore((s) => s.refHighlight);
  const firstFit = useRef(true);

  useEffect(() => {
    if (!ref.current) return;
    const scene = new Scene3D(ref.current);
    sceneRef.current = scene;
    scene.onPick = (ent) => pick(ent, ent ? scene.entityCenter(ent) : null);
    scene.onHover = (ent) => hoverEntity(ent);
    scene.onPlanePick = (name) => planePicked(name);
    scene.onCameraChange = (c) => cameraChanged(c);
    scene.onDragStart = (ent) => beginInstanceDrag(ent);
    scene.onDragMove = (dx, dy) => moveInstanceDrag(dx, dy, scene.screenAxes());
    scene.onDragEnd = () => { void endInstanceDrag(); };
    // a triad tip looks from that side of the axis; the tip the camera is already on flips to the other side
    scene.onTriadClick = (axis, towards) => scene.setView(axis === 'x' ? (towards ? 'left' : 'right') : axis === 'y' ? (towards ? 'front' : 'back') : towards ? 'bottom' : 'top');
    // a right-click selects what it lands on (unless a dialog is picking) and opens the menu for it
    scene.onContextMenu = (e, ent) => {
      const st = getState();
      if (ent && !st.pickRequest && st.tool !== 'measure') { if (ent.kind === 'edge') selectEdge(ent.id); else if (ent.kind === 'face') selectFace(ent.id); }
      const center = ent ? scene.entityCenter(ent) : null;
      let title = ent ? `${ent.kind} ${ent.id}` : st.tree?.kind === 'assembly' ? 'assembly' : 'part';
      if (ent && st.mesh) {
        const label = ent.kind === 'face' ? st.mesh.header.face_labels[String(ent.id)] : ent.kind === 'edge' ? st.mesh.header.edge_labels[String(ent.id)] : '';
        const itm = isAssembly(st.tree) ? itemOf(ent) : null;
        const owner = itm ? (instanceFeatureOf(itm.path)?.name ?? itm.path) : label;
        if (owner) title = `${ent.kind} ${ent.id} of ${owner}`;
      }
      openContextMenu(e.clientX, e.clientY, viewportMenu(ent, center, scene), title);
    };
    setSceneCenter((ent) => scene.entityCenter(ent));
    return () => { scene.dispose(); sceneRef.current = null; setSceneCenter(null); };
  }, []);

  // the move tool takes left drags that start on an instance
  useEffect(() => {
    const scene = sceneRef.current;
    if (scene) scene.dragTool = tool === 'move' && tree?.kind === 'assembly' && !sketchMode ? moveMode : null;
  }, [tool, moveMode, tree, sketchMode]);
  useEffect(() => { sceneRef.current?.setPosePreview(posePreview); }, [posePreview, mesh]);

  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    scene.setMesh(mesh, true);
    if (mesh) {
      const cam = getState().cameraToApply;
      if (cam) { scene.setCamera(cam); cameraApplied(); fitApplied(); firstFit.current = false; }
      else if (firstFit.current || getState().fitPending) { scene.fit(); fitApplied(); firstFit.current = false; }  // no saved camera: show the document whole
    }
  }, [mesh]);

  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene || !cameraToApply || !mesh) return;
    scene.setCamera(cameraToApply);
    cameraApplied();
  }, [cameraToApply, mesh]);

  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene || !mesh) return;
    const featureFaces = new Set<number>();
    if (selected && tree?.kind !== 'assembly') for (const [id, label] of Object.entries(mesh.header.face_labels)) if (label === selected) featureFaces.add(Number(id));
    if (selected && tree?.kind === 'assembly') {
      for (const it of mesh.header.items) if (it.path === selected || it.path.startsWith(`${selected}.`)) for (let f = it.faces[0]; f < it.faces[0] + it.faces[1]; f++) featureFaces.add(f);
    }
    if (selectedItem) {
      const inst = instanceByPath(selectedItem);
      const paths = new Set(inst ? leafPaths(inst) : [selectedItem]);
      for (const it of mesh.header.items) if (paths.has(it.path)) for (let f = it.faces[0]; f < it.faces[0] + it.faces[1]; f++) featureFaces.add(f);
    }
    // the tree row under the mouse: a feature's faces, an instance's items, or a viewer node's bodies
    const hoverFaces = new Set<number>();
    const addItem = (it: { faces: [number, number] }) => { for (let f = it.faces[0]; f < it.faces[0] + it.faces[1]; f++) hoverFaces.add(f); };
    if (treeHover?.feature && tree?.kind !== 'assembly') for (const [id, label] of Object.entries(mesh.header.face_labels)) if (label === treeHover.feature) hoverFaces.add(Number(id));
    if (treeHover?.feature && tree?.kind === 'assembly') for (const it of mesh.header.items) if (it.path === treeHover.feature || it.path.startsWith(`${treeHover.feature}.`)) addItem(it);
    if (treeHover?.item) {
      const inst = instanceByPath(treeHover.item);
      const paths = new Set(inst ? leafPaths(inst) : [treeHover.item]);
      for (const it of mesh.header.items) if (paths.has(it.path)) addItem(it);
    }
    scene.setHighlights({ selectedFace, selectedEdge, hover, featureFaces, hoverFaces, picked: [...dialogPicks, ...(bodySelection ?? []), ...refHighlight] });
  }, [mesh, selected, selectedItem, selectedFace, selectedEdge, hover, dialogPicks, bodySelection, tree, treeHover, refHighlight]);

  useEffect(() => { sceneRef.current?.setItemState(visibility, transparency); }, [visibility, transparency, mesh]);
  useEffect(() => { sceneRef.current?.setOrtho(ortho); }, [ortho]);
  useEffect(() => { sceneRef.current?.setGhostMesh(ghostMesh, ghostStyle); }, [ghostMesh, ghostStyle, mesh]);
  useEffect(() => {
    // edges pick always (those of the face under the cursor); vertices too for measuring, dialog fields
    // that take them, and body relations inside a sketch
    const needAll = tool === 'measure' || !!sketchMode || (pickRequest ? pickRequest.kinds.includes('vertex') : false);
    sceneRef.current?.setPickMode(needAll ? 'all' : 'edges');
  }, [tool, pickRequest, sketchMode]);

  // reference planes: every plane feature, the selected one lit, plus the standard planes when shown
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const size = Math.max(20, scene.modelSize() * 0.6);
    const list: PlaneDrawing[] = [];
    const planes = (tree?.features ?? []).filter((f) => f.kind === 'plane' && f.result?.plane);
    for (const f of planes) if (showPlanes || f.name === selected) list.push({ name: f.name, info: f.result!.plane!, selected: f.name === selected, standard: false });
    if (showPlanes) for (const n of ['XY', 'XZ', 'YZ']) list.push({ name: n, info: standardPlane(n, size), selected: false, standard: true });
    scene.setPlanes(list);
    scene.planesPickable = showPlanes || !!pickRequest?.planes;
  }, [tree, showPlanes, selected, mesh, pickRequest]);

  // GPU clip whenever the installed mesh is not yet the wanted section: while the slider
  // drags, and from a commit until the server's capped mesh arrives. Once the capped mesh is
  // installed the clip goes away (a clip at the same plane would fight with the caps).
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const active = !!section && (previewOffset !== null || !installedSectionMatches());
    scene.setClipPlane(active && section ? clipPlane(section, previewOffset ?? section.offset) : null);
  }, [section, previewOffset, mesh]);

  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const list: MeasureDrawing[] = pins.map((p) => ({ points: p.points, text: p.text, pinned: true }));
    if (measure.result) list.push({ points: measurePoints(measure.result), text: measureText(measure.result), pinned: false });
    scene.setMeasurements(list);
  }, [pins, measure.result]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.closest('.cm-editor'))) return;
      const scene = sceneRef.current;
      if (!scene) return;
      const views: ViewName[] = ['front', 'back', 'left', 'right', 'top', 'bottom', 'iso'];
      if ((e.ctrlKey || e.metaKey) && e.key >= '1' && e.key <= '7') {
        e.preventDefault(); scene.setView(views[Number(e.key) - 1]);
      } else if ((e.ctrlKey || e.metaKey) && e.key === '0') {
        // normal to: the sketch plane inside a sketch, the selected face outside
        e.preventDefault();
        const st = getState();
        if (st.sketchMode) scene.normalTo();
        else if (st.selectedFace !== null) scene.lookAtFace(st.selectedFace);
      } else if (e.key === 'f' && !e.ctrlKey && !e.metaKey) { scene.fit(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const hoverLabel = hover && mesh ? (hover.kind === 'face' ? mesh.header.face_labels[String(hover.id)] : hover.kind === 'edge' ? mesh.header.edge_labels[String(hover.id)] : '') : '';
  const hoverTags = hover && mesh ? entityTags(hover).map(tagText).join(' ') : '';
  return (
    <div className="viewport" ref={ref} data-testid="viewport">
      {sketchMode && <SketchOverlay />}
      {!sketchMode && <HeadsUp />}
      {tool === 'section' && !sketchMode && <SectionPanel />}
      {tool === 'move' && !sketchMode && <MovePanel />}
      {tool === 'measure' && !sketchMode && (
        <div className="tool-hint" data-testid="measure-hint">
          measure: {measure.picks.length === 0 ? 'click a face, edge or vertex' : measure.picks.length === 1 ? 'click a second entity, or pin the description' : 'two entities picked'} · esc clears
        </div>
      )}
      {pickRequest && !sketchMode && (
        <div className="tool-hint pick-hint" data-testid="pick-hint">{pickRequest.hint} · esc cancels</div>
      )}
      {(busy > 0 || loading || sectionPending) && <div className="busy-line" data-testid="busy-line" />}
      {loading && <div className="viewport-badge">evaluating…</div>}
      {!loading && sectionPending && <div className="viewport-badge">sectioning…</div>}
      {hover !== null && mesh && !sketchMode && (
        <div className="viewport-hover">{hover.kind} {hover.id} · {hoverLabel || '—'}{hoverTags ? ` · ${hoverTags}` : ''}</div>
      )}
    </div>
  );
}
