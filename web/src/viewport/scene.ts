import * as THREE from 'three';
import type { MeshItem, ParsedMesh } from '../api/mesh';
import type { CameraState, PickedEntity, PlaneInfo, SectionSpec } from '../api/types';
import type { Plane } from '../state/store';

export type ViewName = 'front' | 'back' | 'left' | 'right' | 'top' | 'bottom' | 'iso';

export interface Highlights {
  selectedFace: number | null; hover: PickedEntity | null; featureFaces: Set<number>;
  picked?: PickedEntity[];   // entities a dialog has picked: faces tinted, edges and vertices drawn
  selectedEdge?: number | null;
  hoverFaces?: Set<number>;  // the faces of the tree row under the mouse
}

export interface PlaneFrame { origin: THREE.Vector3; x: THREE.Vector3; y: THREE.Vector3; n: THREE.Vector3; plane: THREE.Plane }

export interface MeasureDrawing { points: number[][]; text: string; pinned: boolean }

const BASE = new THREE.Color(0xb8c4d0);
const HOVER = new THREE.Color(0xd4e2ee);
const FEATURE = new THREE.Color(0x7fb2e6);
const PICK = new THREE.Color(0xff8c1a);
const CAP = new THREE.Color(0xd94f3d);
const EDGE_HOVER = 0x7ff0ff;
const PICK_COLOR = 0xff8c1a;
const MEASURE = 0xffb14a;
const PIN = 0xffd27a;

/** Plane normals follow build123d: XY is +Z, XZ is -Y, YZ is +X. */
export const PLANE_NORMALS: Record<Plane, [number, number, number]> = { XY: [0, 0, 1], XZ: [0, -1, 0], YZ: [1, 0, 0] };

export function planeFrame(plane: Plane, offset: number): PlaneFrame {
  const axes: Record<Plane, [number[], number[], number[]]> = {
    XY: [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
    XZ: [[1, 0, 0], [0, 0, 1], [0, -1, 0]],
    YZ: [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
  };
  const [x, y, n] = axes[plane].map((a) => new THREE.Vector3(...a));
  const origin = n.clone().multiplyScalar(offset);
  const p = new THREE.Plane().setFromNormalAndCoplanarPoint(n, origin);
  return { origin, x, y, n, plane: p };
}

/** A frame from the server's resolved plane (a sketch on a face, a plane feature, or a standard plane). */
export function frameFromInfo(info: PlaneInfo): PlaneFrame {
  const origin = new THREE.Vector3(...info.origin);
  const x = new THREE.Vector3(...info.x_dir).normalize();
  const n = new THREE.Vector3(...info.z_dir).normalize();
  const y = new THREE.Vector3(...info.y_dir).normalize();
  const p = new THREE.Plane().setFromNormalAndCoplanarPoint(n, origin);
  return { origin, x, y, n, plane: p };
}

export interface PlaneDrawing { name: string; info: PlaneInfo; selected: boolean; standard: boolean }

const TRIAD_AXES: ['x' | 'y' | 'z', THREE.Vector3, number][] = [
  ['x', new THREE.Vector3(1, 0, 0), 0xe5534b], ['y', new THREE.Vector3(0, 1, 0), 0x6cc07a], ['z', new THREE.Vector3(0, 0, 1), 0x7fb2e6],
];

/** A letter on a transparent square, for the triad's axis labels. */
function glyphSprite(text: string, color: number): THREE.Sprite {
  const c = document.createElement('canvas');
  c.width = 64; c.height = 64;
  const ctx = c.getContext('2d')!;
  ctx.font = 'bold 44px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillStyle = `#${color.toString(16).padStart(6, '0')}`;
  ctx.fillText(text, 32, 34);
  const tex = new THREE.CanvasTexture(c);
  return new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
}

/** A three.js clipping plane that removes the material on the normal side (or the other side with flip). */
export function clipPlane(spec: SectionSpec, offset = spec.offset): THREE.Plane {
  const n = new THREE.Vector3(...PLANE_NORMALS[spec.plane]);
  const origin = n.clone().multiplyScalar(offset);
  // three.js keeps the half-space where dot(normal, p) + constant >= 0
  const keepDir = spec.flip ? n.clone() : n.clone().negate();
  return new THREE.Plane().setFromNormalAndCoplanarPoint(keepDir, origin);
}

interface ItemObjects { item: MeshItem; mesh: THREE.Mesh; edges: THREE.LineSegments; segEdge: Uint32Array; material: THREE.MeshStandardMaterial }

export class Scene3D {
  readonly renderer: THREE.WebGLRenderer;
  readonly scene = new THREE.Scene();
  /** The perspective camera drives every camera motion; the orthographic one mirrors it when active. */
  readonly camera: THREE.PerspectiveCamera;
  readonly ortho: THREE.OrthographicCamera;
  private useOrtho = false;
  private ghostObj: THREE.Mesh | null = null;
  readonly target = new THREE.Vector3();
  readonly overlay = new THREE.Group();
  private items: ItemObjects[] = [];
  private axes: THREE.AxesHelper | null = null;
  private axesWanted = false;
  // the corner triad: three axes in a tiny scene of their own that turns with the camera, drawn bottom-left
  private triadScene = new THREE.Scene();
  private triadCamera = new THREE.OrthographicCamera(-1.7, 1.7, 1.7, -1.7, 0.1, 20);
  private triadPx = 84;
  /** A click on a triad tip: the axis, and whether the camera already looks along it from its positive side. */
  onTriadClick: (axis: 'x' | 'y' | 'z', towardsCamera: boolean) => void = () => {};
  /** A right-click without a drag: the entity under the cursor (null on empty space), for a context menu. */
  onContextMenu: (e: PointerEvent, entity: PickedEntity | null) => void = () => {};
  onPlaneContext: (u: number, v: number, e: PointerEvent, body: PickedEntity | null) => void = () => {};
  private mesh: ParsedMesh | null = null;
  private faceOfVertex: Float32Array | null = null;
  private colorAttr: THREE.BufferAttribute | null = null;
  private vertexPoints: THREE.Points | null = null;
  private hoverObj: THREE.Object3D | null = null;
  private measureGroup = new THREE.Group();
  private labels: THREE.Sprite[] = [];
  private raycaster = new THREE.Raycaster();
  private needsRender = true;
  private size = 100;
  private sketchFrame: PlaneFrame | null = null;
  private hl: Highlights = { selectedFace: null, hover: null, featureFaces: new Set(), picked: [] };
  private pickedObjs: THREE.Object3D[] = [];
  private observer: ResizeObserver;
  private disposed = false;
  private meshVisible = true;
  private ghost = false;
  /** faces: faces only; edges: faces and the edges of the face under the cursor; all: every edge and the vertices. */
  private pickMode: 'faces' | 'edges' | 'all' = 'edges';
  private selEdgeObj: THREE.Object3D | null = null;
  private previewing = false;
  private visibility: Record<string, boolean> = {};
  private transparency: Record<string, boolean> = {};
  private clip: THREE.Plane | null = null;
  private capFaces = new Set<number>();
  private planeGroup = new THREE.Group();
  private planeMeshes: THREE.Mesh[] = [];
  /** When true, a click on a displayed reference plane reports it through onPlanePick. */
  planesPickable = false;

  onPick: (entity: PickedEntity | null) => void = () => {};
  onHover: (entity: PickedEntity | null) => void = () => {};
  onCameraChange: (camera: CameraState) => void = () => {};
  /** Sketch mode: a click on the plane; `body` is the body entity under the cursor when a modifier was held. */
  onPlaneClick: (u: number, v: number, ev: PointerEvent, body: PickedEntity | null) => void = () => {};
  onPlaneMove: (u: number, v: number, body: PickedEntity | null) => void = () => {};
  onPlaneDoubleClick: () => void = () => {};
  /** Sketch mode: a left button press on the plane. Return true to take the drag (no orbit/pan). */
  onPlaneDown: (u: number, v: number, ev: PointerEvent) => boolean = () => false;
  onPlaneDrag: (u: number, v: number) => void = () => {};
  onPlaneUp: (u: number, v: number) => void = () => {};
  /** A click on a displayed reference plane square: the plane feature name, or XY/XZ/YZ. */
  onPlanePick: (name: string) => void = () => {};
  /** Sketch mode: when true, hover and click pick the (ghosted) body instead of the plane. */
  sketchPickBody = false;
  /** Sketch mode: when true (select mode), a left drag on empty plane orbits instead of doing nothing. */
  sketchOrbitFree = false;
  /** Assemblies: when set, a left drag that starts on an instance moves it instead of orbiting. */
  dragTool: 'translate' | 'rotate' | null = null;
  /** The drag begins on this entity; return false to let the drag orbit instead. */
  onDragStart: (entity: PickedEntity) => boolean = () => false;
  onDragMove: (dx: number, dy: number) => void = () => {};
  onDragEnd: () => void = () => {};

  constructor(readonly container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.localClippingEnabled = true;
    this.renderer.domElement.style.display = 'block';
    this.renderer.domElement.tabIndex = 0;
    container.appendChild(this.renderer.domElement);
    this.scene.background = new THREE.Color(0x2a2e33);
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.1, 10000);
    this.camera.up.set(0, 0, 1);
    this.scene.add(this.camera);
    this.ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 10000);
    this.ortho.up.set(0, 0, 1);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x3a3f45, 0.85));
    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(1, 1.5, 2.5);
    this.camera.add(key);
    this.scene.add(this.overlay);
    this.scene.add(this.measureGroup);
    this.scene.add(this.planeGroup);
    this.buildTriad();
    this.camera.position.set(150, -180, 130);
    this.camera.lookAt(this.target);
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(container);
    this.resize();
    this.bind();
    this.loop();
  }

  // ---- data -------------------------------------------------------------------

  setMesh(mesh: ParsedMesh | null, keepCamera = true) {
    this.clearMesh();
    this.mesh = mesh;
    if (!mesh || mesh.indices.length === 0) { this.updateHelpers([[-50, -50, 0], [50, 50, 0]]); this.requestRender(); return; }
    const nv = mesh.positions.length / 3;
    const posAttr = new THREE.BufferAttribute(mesh.positions, 3);
    const norAttr = new THREE.BufferAttribute(mesh.normals, 3);
    this.colorAttr = new THREE.BufferAttribute(new Float32Array(nv * 3), 3);
    const faceId = new Float32Array(nv);
    for (let t = 0; t < mesh.triangleFaceIds.length; t++)
      for (let k = 0; k < 3; k++) faceId[mesh.indices[3 * t + k]] = mesh.triangleFaceIds[t];
    this.faceOfVertex = faceId;
    this.capFaces = new Set(mesh.header.section_faces);
    const items = mesh.header.items.length ? mesh.header.items : [{
      name: 'body', path: '', color: null, faces: [0, mesh.header.face_ranges.length] as [number, number],
      edges: [0, mesh.header.edge_ranges.length] as [number, number], vertices: [0, mesh.vertexPositions.length / 3] as [number, number],
      triangles: [0, mesh.indices.length / 3] as [number, number],
    }];
    const ep = mesh.edgePositions;
    for (const item of items) {
      const [t0, tn] = item.triangles;
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', posAttr);
      geom.setAttribute('normal', norAttr);
      geom.setAttribute('color', this.colorAttr);
      geom.setIndex(new THREE.BufferAttribute(mesh.indices.subarray(t0 * 3, (t0 + tn) * 3), 1));
      geom.computeBoundingSphere();
      const material = new THREE.MeshStandardMaterial({
        vertexColors: true, metalness: 0.05, roughness: 0.65, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
        side: THREE.DoubleSide,
      });
      const m = new THREE.Mesh(geom, material);
      m.userData.path = item.path;
      // edges of this item as segment pairs, remembering the edge id per segment
      const segs: number[] = [];
      const segEdge: number[] = [];
      const [e0, en] = item.edges;
      for (let e = e0; e < e0 + en; e++) {
        const [start, count] = mesh.header.edge_ranges[e];
        for (let i = 0; i < count - 1; i++) {
          const a = (start + i) * 3, b = (start + i + 1) * 3;
          segs.push(ep[a], ep[a + 1], ep[a + 2], ep[b], ep[b + 1], ep[b + 2]);
          segEdge.push(e);
        }
      }
      const eg = new THREE.BufferGeometry();
      eg.setAttribute('position', new THREE.BufferAttribute(new Float32Array(segs), 3));
      const edges = new THREE.LineSegments(eg, new THREE.LineBasicMaterial({ color: 0x14171a }));
      this.scene.add(m);
      this.scene.add(edges);
      this.items.push({ item, mesh: m, edges, segEdge: new Uint32Array(segEdge), material });
    }
    if (mesh.vertexPositions.length) {
      const vg = new THREE.BufferGeometry();
      vg.setAttribute('position', new THREE.BufferAttribute(mesh.vertexPositions, 3));
      this.vertexPoints = new THREE.Points(vg, new THREE.PointsMaterial({ color: 0x9fd3ff, size: 5, sizeAttenuation: false, depthTest: true }));
      this.vertexPoints.visible = this.pickMode === 'all' && this.meshVisible;
      this.scene.add(this.vertexPoints);
    }
    this.updateHelpers(mesh.header.bbox);
    this.applyItemState();
    this.applyClip();
    this.applyColors();
    if (this.ghost) this.setGhost(true);
    if (!keepCamera) this.fit();
    this.requestRender();
  }

  private clearMesh() {
    for (const it of this.items) {
      this.scene.remove(it.mesh); this.scene.remove(it.edges);
      it.mesh.geometry.dispose(); it.material.dispose();
      it.edges.geometry.dispose(); (it.edges.material as THREE.Material).dispose();
    }
    this.items = [];
    if (this.vertexPoints) { this.scene.remove(this.vertexPoints); this.vertexPoints.geometry.dispose(); this.vertexPoints = null; }
    this.faceOfVertex = null; this.colorAttr = null;
    this.setHoverObject(null);
  }

  modelSize(): number { return this.size; }

  // ---- the corner triad ---------------------------------------------------------------

  private buildTriad() {
    for (const [name, dir, color] of TRIAD_AXES) {
      const g = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), dir]);
      this.triadScene.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color })));
      const label = glyphSprite(name.toUpperCase(), color);
      label.position.copy(dir).multiplyScalar(1.35);
      label.scale.set(0.6, 0.6, 1);
      this.triadScene.add(label);
    }
  }

  private placeTriadCamera() {
    const dir = this.camera.position.clone().sub(this.target).normalize();
    if (dir.lengthSq() === 0) dir.set(1, -1, 1).normalize();
    this.triadCamera.position.copy(dir).multiplyScalar(6);
    this.triadCamera.up.copy(this.camera.up);
    this.triadCamera.lookAt(0, 0, 0);
    this.triadCamera.updateMatrixWorld();
  }

  /** Pixels from the canvas's bottom-left corner, or null outside the triad's square. */
  private inTriad(e: MouseEvent): [number, number] | null {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const x = e.clientX - rect.left, y = rect.bottom - e.clientY;
    return x >= 0 && x <= this.triadPx && y >= 0 && y <= this.triadPx ? [x, y] : null;
  }

  private triadHit(e: MouseEvent): { axis: 'x' | 'y' | 'z'; towards: boolean } | null {
    const at = this.inTriad(e);
    if (!at) return null;
    this.placeTriadCamera();
    const dir = this.camera.position.clone().sub(this.target).normalize();
    let best: { axis: 'x' | 'y' | 'z'; towards: boolean; d: number } | null = null;
    for (const [axis, v] of TRIAD_AXES) {
      const p = v.clone().multiplyScalar(1.35).project(this.triadCamera);
      const d = Math.hypot(((p.x + 1) / 2) * this.triadPx - at[0], ((p.y + 1) / 2) * this.triadPx - at[1]);
      if (d <= 16 && (!best || d < best.d)) best = { axis, towards: v.dot(dir) > 0.999, d };
    }
    return best;
  }

  /** Look straight at a face along its average normal, keeping the zoom (ctrl+0 outside a sketch). */
  lookAtFace(face: number) {
    if (!this.mesh || !this.faceOfVertex) return;
    const n = new THREE.Vector3();
    const nv = this.mesh.normals;
    for (let i = 0; i < this.faceOfVertex.length; i++) if (this.faceOfVertex[i] === face) n.add(new THREE.Vector3(nv[i * 3], nv[i * 3 + 1], nv[i * 3 + 2]));
    if (n.lengthSq() < 1e-9) return;
    n.normalize();
    const c = this.entityCenter({ kind: 'face', id: face });
    const origin = c ? new THREE.Vector3(c[0], c[1], c[2]) : this.bboxCenter();
    // y is +Z projected onto the face, the way sketches on walls read; a horizontal face keeps +Y up
    const up = Math.abs(n.z) > 0.99 ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(0, 0, 1);
    const y = up.clone().sub(n.clone().multiplyScalar(up.dot(n))).normalize();
    const x = new THREE.Vector3().crossVectors(y, n).normalize();
    this.lookAtPlane({ origin, x, y, n, plane: new THREE.Plane().setFromNormalAndCoplanarPoint(n, origin) }, true);
  }
  hasMesh(): boolean { return this.items.length > 0; }

  setGhost(on: boolean) {
    this.ghost = on;
    this.applyItemState();
    this.requestRender();
  }

  /** A second body drawn faint and unpickable: in sketch mode, the finished part behind
   * the pre-sketch body, so a feature made from the sketch shows while sketching goes on. */
  setGhostMesh(mesh: ParsedMesh | null, style: 'ghost' | 'preview' = 'ghost') {
    if (this.ghostObj) {
      this.scene.remove(this.ghostObj);
      this.ghostObj.geometry.dispose();
      (this.ghostObj.material as THREE.Material).dispose();
      this.ghostObj = null;
    }
    if (mesh && mesh.indices.length) {
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.BufferAttribute(mesh.positions, 3));
      geom.setAttribute('normal', new THREE.BufferAttribute(mesh.normals, 3));
      geom.setIndex(new THREE.BufferAttribute(mesh.indices, 1));
      // a preview draws the candidate result nearly solid over the current body, which fades back
      const preview = style === 'preview';
      const material = new THREE.MeshStandardMaterial({
        color: preview ? 0x8fc3f0 : 0x7fb2e6, transparent: true, opacity: preview ? 0.6 : 0.16, depthWrite: preview, side: THREE.DoubleSide, metalness: 0.05, roughness: 0.7,
      });
      this.ghostObj = new THREE.Mesh(geom, material);
      this.ghostObj.renderOrder = 1;
      this.scene.add(this.ghostObj);
    }
    const previewing = !!mesh && style === 'preview';
    if (previewing !== this.previewing) { this.previewing = previewing; this.applyItemState(); }
    this.requestRender();
  }

  hasGhostMesh(): boolean { return this.ghostObj !== null; }

  // ---- orthographic projection ------------------------------------------------------

  setOrtho(on: boolean) {
    if (this.useOrtho === on) return;
    this.useOrtho = on;
    this.syncOrtho();
    for (const l of this.labels) this.scaleLabel(l);
    this.requestRender();
    this.cameraMoved();
  }

  isOrtho(): boolean { return this.useOrtho; }

  /** The camera to render and pick with. */
  private active(): THREE.Camera {
    if (this.useOrtho) this.syncOrtho();
    return this.useOrtho ? this.ortho : this.camera;
  }

  /** Mirror the perspective camera: same place and direction, a frustum as wide as the
   * perspective view is at the orbit target, so orbit, pan and zoom keep working unchanged. */
  private syncOrtho() {
    const o = this.ortho, c = this.camera;
    o.position.copy(c.position);
    o.quaternion.copy(c.quaternion);
    o.up.copy(c.up);
    const dist = c.position.distanceTo(this.target) || this.size;
    const halfH = dist * Math.tan((c.fov * Math.PI) / 360);
    const halfW = halfH * c.aspect;
    o.left = -halfW; o.right = halfW; o.top = halfH; o.bottom = -halfH;
    o.near = c.near; o.far = c.far;
    o.updateProjectionMatrix();
    o.updateMatrixWorld();
  }

  setMeshVisible(v: boolean) {
    this.meshVisible = v;
    this.applyItemState();
    this.requestRender();
  }

  setItemState(visibility: Record<string, boolean>, transparency: Record<string, boolean>) {
    this.visibility = visibility;
    this.transparency = transparency;
    this.applyItemState();
    this.requestRender();
  }

  private applyItemState() {
    for (const it of this.items) {
      const visible = this.meshVisible && (this.visibility[it.item.path] ?? true);
      const transparent = this.ghost || this.previewing || (this.transparency[it.item.path] ?? false);
      it.mesh.visible = visible;
      it.edges.visible = visible;
      it.material.transparent = transparent;
      it.material.opacity = this.ghost ? 0.35 : this.previewing ? 0.22 : transparent ? 0.3 : 1;
      it.material.depthWrite = !transparent;
      it.material.needsUpdate = true;
      const em = it.edges.material as THREE.LineBasicMaterial;
      em.transparent = transparent; em.opacity = transparent ? 0.35 : 1;
    }
    if (this.vertexPoints) this.vertexPoints.visible = this.pickMode === 'all' && this.meshVisible;
  }

  setPickMode(mode: 'faces' | 'edges' | 'all') {
    this.pickMode = mode;
    this.applyItemState();
    this.setHoverObject(null);
    this.requestRender();
  }

  // ---- section preview ----------------------------------------------------------

  setClipPlane(plane: THREE.Plane | null) {
    this.clip = plane;
    this.applyClip();
    this.requestRender();
  }

  /** Number of active clipping planes (0 or 1); for verification and debugging. */
  clipCount(): number { return this.clip ? 1 : 0; }

  private applyClip() {
    const planes = this.clip ? [this.clip] : [];
    for (const it of this.items) {
      it.material.clippingPlanes = planes;
      it.material.needsUpdate = true;
      const em = it.edges.material as THREE.LineBasicMaterial;
      em.clippingPlanes = planes; em.needsUpdate = true;
    }
    if (this.vertexPoints) (this.vertexPoints.material as THREE.PointsMaterial).clippingPlanes = planes;
  }

  private updateHelpers(bbox: [number[], number[]] | number[][]) {
    const [lo, hi] = bbox;
    const extent = Math.max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], Math.abs(lo[0]), Math.abs(hi[0]), Math.abs(lo[1]), Math.abs(hi[1]), 10);
    this.size = Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) || 100;
    const gridSize = Math.ceil((extent * 2.4) / 10) * 10;
    if (this.axes) this.scene.remove(this.axes);
    // no floor grid: the axis triad shows only with the standard planes, and never behind a square-on sketch
    this.axes = new THREE.AxesHelper(Math.max(gridSize * 0.15, 5));
    this.axes.visible = this.axesWanted && this.sketchFrame === null;
    this.scene.add(this.axes);
  }

  // ---- colours and highlights -------------------------------------------------------

  setHighlights(hl: Highlights) {
    this.hl = hl;
    this.applyColors();
    this.applyHoverObject();
    this.applyPicked();
    this.requestRender();
  }

  private applyPicked() {
    for (const o of this.pickedObjs) this.overlay.remove(o);
    this.pickedObjs = [];
    if (!this.mesh) return;
    for (const p of this.hl.picked ?? []) {
      let obj: THREE.Object3D | null = null;
      if (p.kind === 'edge') obj = this.edgeLine(p.id, PICK.getHex());
      else if (p.kind === 'vertex') { const v = this.vertexPosition(p.id); obj = v ? this.marker(v, PICK.getHex(), 10) : null; }
      if (obj) { this.overlay.add(obj); this.pickedObjs.push(obj); }
    }
  }

  private edgeLine(id: number, color: number): THREE.Line | null {
    if (!this.mesh) return null;
    const [start, count] = this.mesh.header.edge_ranges[id] ?? [0, 0];
    if (!count) return null;
    const pts = new Float32Array(this.mesh.edgePositions.buffer, this.mesh.edgePositions.byteOffset + start * 12, count * 3);
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pts), 3));
    const line = new THREE.Line(g, new THREE.LineBasicMaterial({ color, depthTest: false, linewidth: 2 }));
    line.renderOrder = 5;
    return line;
  }

  private applyColors() {
    if (!this.colorAttr || !this.faceOfVertex) return;
    const col = this.colorAttr;
    const fv = this.faceOfVertex;
    const { selectedFace, hover, featureFaces } = this.hl;
    const pickedFaces = new Set((this.hl.picked ?? []).filter((p) => p.kind === 'face').map((p) => p.id));
    const hoverFace = hover?.kind === 'face' ? hover.id : null;
    const faceBase = new Map<number, THREE.Color>();
    for (const it of this.items) {
      const c = it.item.color ? new THREE.Color(it.item.color[0], it.item.color[1], it.item.color[2]) : BASE;
      const [f0, fn] = it.item.faces;
      for (let f = f0; f < f0 + fn; f++) faceBase.set(f, c);
    }
    const tint = (c: THREE.Color) => c.clone().lerp(FEATURE, 0.55);
    for (let i = 0; i < fv.length; i++) {
      const f = fv[i];
      const base = this.capFaces.has(f) ? CAP : faceBase.get(f) ?? BASE;
      const c = f === selectedFace || pickedFaces.has(f) ? PICK : f === hoverFace ? (this.capFaces.has(f) ? base.clone().lerp(HOVER, 0.4) : HOVER)
        : this.hl.hoverFaces?.has(f) ? base.clone().lerp(HOVER, 0.5) : featureFaces.has(f) ? tint(base) : base;
      col.setXYZ(i, c.r, c.g, c.b);
    }
    col.needsUpdate = true;
  }

  private setHoverObject(obj: THREE.Object3D | null) {
    if (this.hoverObj) { this.overlay.remove(this.hoverObj); }
    this.hoverObj = obj;
    if (obj) this.overlay.add(obj);
  }

  private applyHoverObject() {
    if (this.selEdgeObj) { this.overlay.remove(this.selEdgeObj); this.selEdgeObj = null; }
    const se = this.hl.selectedEdge;
    if (se !== null && se !== undefined && this.mesh) { this.selEdgeObj = this.edgeLine(se, PICK_COLOR); if (this.selEdgeObj) this.overlay.add(this.selEdgeObj); }
    const h = this.hl.hover;
    if (!h || h.kind === 'face' || !this.mesh) { this.setHoverObject(null); return; }
    if (h.kind === 'edge') {
      this.setHoverObject(this.edgeLine(h.id, EDGE_HOVER));
    } else {
      const p = this.vertexPosition(h.id);
      this.setHoverObject(p ? this.marker(p, EDGE_HOVER, 10) : null);
    }
  }

  private vertexPosition(id: number): THREE.Vector3 | null {
    if (!this.mesh || id * 3 + 2 >= this.mesh.vertexPositions.length) return null;
    const v = this.mesh.vertexPositions;
    return new THREE.Vector3(v[id * 3], v[id * 3 + 1], v[id * 3 + 2]);
  }

  private marker(p: THREE.Vector3, color: number, size: number): THREE.Points {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array([p.x, p.y, p.z]), 3));
    const pts = new THREE.Points(g, new THREE.PointsMaterial({ color, size, sizeAttenuation: false, depthTest: false }));
    pts.renderOrder = 6;
    return pts;
  }

  // ---- measurements: lines, markers and label sprites (all in the WebGL frame) ------------

  /** Reference planes as translucent squares with a name label; standard planes small and faint. */
  setPlanes(list: PlaneDrawing[]) {
    this.axesWanted = list.some((p) => p.standard);
    if (this.axes) this.axes.visible = this.axesWanted && this.sketchFrame === null;
    this.labels = this.labels.filter((l) => l.parent !== this.planeGroup);
    for (const child of [...this.planeGroup.children]) {
      this.planeGroup.remove(child);
      child.traverse((o) => {
        const anyO = o as THREE.Mesh;
        if (anyO.geometry) anyO.geometry.dispose();
        const mat = (anyO as unknown as { material?: THREE.Material }).material;
        if (mat) { const sm = mat as THREE.SpriteMaterial; sm.map?.dispose(); mat.dispose(); }
      });
    }
    this.planeMeshes = [];
    for (const pl of list) {
      const frame = frameFromInfo(pl.info);
      const size = pl.standard ? Math.max(pl.info.size * 0.45, 10) : pl.info.size;
      const color = pl.selected ? 0xff8c1a : pl.standard ? 0x8fa3b8 : 0x7fb2e6;
      const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: pl.selected ? 0.22 : pl.standard ? 0.05 : 0.12,
                                                side: THREE.DoubleSide, depthWrite: false });
      const mesh = new THREE.Mesh(new THREE.PlaneGeometry(size, size), mat);
      const basis = new THREE.Matrix4().makeBasis(frame.x, frame.y, frame.n);
      mesh.setRotationFromMatrix(basis);
      mesh.position.copy(frame.origin);
      mesh.userData.plane = pl.name;
      mesh.renderOrder = 5;
      this.planeGroup.add(mesh);
      this.planeMeshes.push(mesh);
      const h = size / 2;
      const corners = [[-h, -h], [h, -h], [h, h], [-h, h], [-h, -h]].map(([u, v]) =>
        frame.origin.clone().addScaledVector(frame.x, u).addScaledVector(frame.y, v));
      const outline = new THREE.Line(new THREE.BufferGeometry().setFromPoints(corners),
        new THREE.LineBasicMaterial({ color, transparent: true, opacity: pl.selected ? 0.9 : pl.standard ? 0.25 : 0.6 }));
      outline.renderOrder = 6;
      this.planeGroup.add(outline);
      if (!pl.standard || pl.selected) {
        const label = this.makeLabel(pl.name, pl.selected, pl.standard ? '#8fa3b8' : '#7fb2e6');
        label.position.copy(frame.origin.clone().addScaledVector(frame.x, h * 0.85).addScaledVector(frame.y, h * 0.85));
        this.planeGroup.add(label);
        this.labels.push(label);
      }
    }
    this.requestRender();
  }

  planeCount(): number { return this.planeMeshes.length; }

  /** The displayed plane under a mouse event, when planes are pickable and nearer than the body. */
  private pickPlane(e: MouseEvent, maxDistance = Infinity): string | null {
    if (!this.planesPickable || !this.planeMeshes.length) return null;
    this.raycaster.setFromCamera(this.ndc(e), this.active());
    const hits = this.raycaster.intersectObjects(this.planeMeshes, false);
    const hit = hits.find((h) => h.distance <= maxDistance + 1e-6);
    return hit ? String(hit.object.userData.plane) : null;
  }

  setMeasurements(list: MeasureDrawing[]) {
    for (const child of [...this.measureGroup.children]) {
      this.measureGroup.remove(child);
      child.traverse((o) => {
        const anyO = o as THREE.Mesh;
        if (anyO.geometry) anyO.geometry.dispose();
        const mat = (anyO as unknown as { material?: THREE.Material }).material;
        if (mat) { const sm = mat as THREE.SpriteMaterial; sm.map?.dispose(); mat.dispose(); }
      });
    }
    this.labels = this.labels.filter((l) => l.parent === this.planeGroup);
    for (const m of list) {
      const color = m.pinned ? PIN : MEASURE;
      const pts = m.points.map((p) => new THREE.Vector3(p[0], p[1], p[2]));
      if (pts.length >= 2) {
        const g = new THREE.BufferGeometry().setFromPoints(pts);
        const line = new THREE.Line(g, new THREE.LineBasicMaterial({ color, depthTest: false }));
        line.renderOrder = 7;
        this.measureGroup.add(line);
      }
      for (const p of pts) this.measureGroup.add(this.marker(p, color, 8));
      const mid = pts.length >= 2 ? pts[0].clone().add(pts[1]).multiplyScalar(0.5) : pts[0];
      if (mid) {
        const label = this.makeLabel(m.text, m.pinned);
        label.position.copy(mid);
        this.measureGroup.add(label);
        this.labels.push(label);
      }
    }
    this.requestRender();
  }

  private makeLabel(text: string, pinned: boolean, stroke?: string): THREE.Sprite {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d')!;
    const scale = 2;
    ctx.font = `${13 * scale}px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`;
    const w = Math.ceil(ctx.measureText(text).width + 16 * scale), h = 24 * scale;
    canvas.width = w; canvas.height = h;
    ctx.font = `${13 * scale}px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`;
    ctx.fillStyle = 'rgba(20, 23, 27, 0.88)';
    ctx.strokeStyle = stroke ?? (pinned ? '#ffd27a' : '#ffb14a');
    ctx.lineWidth = 2 * scale;
    const r = 5 * scale;
    ctx.beginPath();
    ctx.roundRect(ctx.lineWidth / 2, ctx.lineWidth / 2, w - ctx.lineWidth, h - ctx.lineWidth, r);
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#f2f5f7';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, 8 * scale, h / 2 + 1);
    const tex = new THREE.CanvasTexture(canvas);
    tex.minFilter = THREE.LinearFilter;
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, sizeAttenuation: false, transparent: true }));
    sprite.userData.aspect = w / h;
    sprite.center.set(0.5, -0.35);
    sprite.renderOrder = 8;
    this.scaleLabel(sprite);
    return sprite;
  }

  private scaleLabel(sprite: THREE.Sprite) {
    // with sizeAttenuation off, a sprite's projected height in pixels is scale / tan(fov/2) * H / 2
    const hPx = 22;
    const H = this.renderer.domElement.clientHeight || 600;
    if (this.useOrtho) this.syncOrtho();
    const s = this.useOrtho ? (hPx * (this.ortho.top - this.ortho.bottom)) / H : (hPx * 2 * Math.tan((this.camera.fov * Math.PI) / 360)) / H;
    sprite.scale.set(s * (sprite.userData.aspect as number), s, 1);
  }

  // ---- camera -----------------------------------------------------------------

  bboxCenter(): THREE.Vector3 {
    const bb = this.mesh?.header.bbox;
    if (!bb) return new THREE.Vector3();
    return new THREE.Vector3((bb[0][0] + bb[1][0]) / 2, (bb[0][1] + bb[1][1]) / 2, (bb[0][2] + bb[1][2]) / 2);
  }

  getCamera(): CameraState {
    const p = this.camera.position, t = this.target, u = this.camera.up;
    return { position: [p.x, p.y, p.z], target: [t.x, t.y, t.z], up: [u.x, u.y, u.z], ortho: this.useOrtho };
  }

  setCamera(c: CameraState) {
    this.camera.position.set(...c.position);
    this.target.set(...c.target);
    this.camera.up.set(...c.up);
    this.camera.lookAt(this.target);
    this.updateNearFar();
    this.requestRender();
  }

  private updateNearFar() {
    this.camera.near = Math.max(this.size / 500, 0.01);
    this.camera.far = this.size * 50 + 1000;
    this.camera.updateProjectionMatrix();
  }

  private cameraMoved() { this.onCameraChange(this.getCamera()); }

  fit() {
    if (this.sketchFrame && this.sketchBox && !this.mesh?.header.face_ranges.length) { this.fitSketch(); return; }  // no body: the sketch
    const center = this.bboxCenter();
    const dir = this.camera.position.clone().sub(this.target).normalize();
    if (dir.lengthSq() === 0) dir.set(1, -1.2, 0.9).normalize();
    const dist = (this.size * 0.6) / Math.tan((this.camera.fov * Math.PI) / 360) + this.size * 0.1;
    this.target.copy(center);
    this.camera.position.copy(center).add(dir.multiplyScalar(dist));
    this.updateNearFar();
    this.camera.lookAt(this.target);
    this.requestRender();
    this.cameraMoved();
  }

  setView(view: ViewName) {
    const dirs: Record<ViewName, [number, number, number]> = {
      front: [0, -1, 0], back: [0, 1, 0], left: [-1, 0, 0], right: [1, 0, 0],
      top: [0, 0, 1], bottom: [0, 0, -1], iso: [1, -1.2, 0.9],
    };
    const d = new THREE.Vector3(...dirs[view]).normalize();
    const dist = this.camera.position.distanceTo(this.target) || this.size * 2;
    this.camera.up.set(0, 0, 1);
    if (view === 'top') this.camera.up.set(0, 1, 0);
    if (view === 'bottom') this.camera.up.set(0, -1, 0);
    this.camera.position.copy(this.target).add(d.multiplyScalar(dist));
    this.camera.lookAt(this.target);
    this.fit();
  }

  /** Face a plane square on. With keepDistance the view keeps its current zoom and target. */
  lookAtPlane(frame: PlaneFrame, keepDistance = false) {
    const center = keepDistance ? this.target.clone() : this.bboxCenter();
    const onPlane = frame.plane.projectPoint(center, new THREE.Vector3());
    const dist = keepDistance ? (this.camera.position.distanceTo(this.target) || this.size)
      : (this.size * 0.6) / Math.tan((this.camera.fov * Math.PI) / 360) + this.size * 0.1;
    this.target.copy(onPlane);
    this.camera.up.copy(frame.y);
    this.camera.position.copy(onPlane).add(frame.n.clone().multiplyScalar(dist));
    this.camera.lookAt(this.target);
    this.requestRender();
    this.cameraMoved();
  }

  /** Sketch mode: the sketch's own extents in plane coordinates (u0, v0, u1, v1), which fit uses
   * when there is no body to fit to. */
  sketchBox: [number, number, number, number] | null = null;

  /** Sketch mode: look square on at the sketch's extents, filling the view with them. */
  fitSketch() {
    const frame = this.sketchFrame, box = this.sketchBox;
    if (!frame || !box) return;
    const [u0, v0, u1, v1] = box;
    const size = Math.max(Math.hypot(u1 - u0, v1 - v0), 1);
    const center = frame.origin.clone().add(frame.x.clone().multiplyScalar((u0 + u1) / 2)).add(frame.y.clone().multiplyScalar((v0 + v1) / 2));
    const dist = (size * 0.6) / Math.tan((this.camera.fov * Math.PI) / 360) + size * 0.1;
    this.size = Math.max(this.size, size);
    this.target.copy(center);
    this.camera.up.copy(frame.y);
    this.camera.position.copy(center).add(frame.n.clone().multiplyScalar(dist));
    this.updateNearFar();
    this.camera.lookAt(this.target);
    this.requestRender();
    this.cameraMoved();
  }

  /** Sketch mode: back to looking straight at the sketch plane, keeping the zoom. */
  normalTo() {
    if (this.sketchFrame) this.lookAtPlane(this.sketchFrame, true);
  }

  setSketchFrame(frame: PlaneFrame | null) {
    this.sketchFrame = frame;
    if (!frame) this.sketchBox = null;
    if (frame) this.lookAtPlane(frame);
    else this.camera.up.set(0, 0, 1);
    // the axis triad is noise behind a square-on sketch
    if (this.axes) this.axes.visible = this.axesWanted && !frame;
    this.requestRender();
  }

  // ---- interaction ------------------------------------------------------------

  private bind() {
    const el = this.renderer.domElement;
    let button = -1, sx = 0, sy = 0, lx = 0, ly = 0, dragging = false, shift = false, alt = false, planeDrag = false, instDrag = false;
    el.addEventListener('contextmenu', (e) => e.preventDefault());
    el.addEventListener('pointerdown', (e) => {
      el.focus();
      button = e.button; sx = lx = e.clientX; sy = ly = e.clientY; dragging = false; shift = e.shiftKey; alt = e.altKey; planeDrag = false; instDrag = false;
      el.setPointerCapture(e.pointerId);
      if (button === 0 && !shift && !alt && this.sketchFrame && !this.sketchPickBody) {
        const uv = this.planePoint(e);
        if (uv && this.onPlaneDown(uv[0], uv[1], e)) planeDrag = true;
      }
      if (button === 0 && !shift && !alt && !this.sketchFrame && this.dragTool) {
        const ent = this.pickEntity(e);
        if (ent && this.onDragStart(ent)) instDrag = true;
      }
    });
    el.addEventListener('pointermove', (e) => {
      if (button < 0) { this.hover(e); return; }
      const dx = e.clientX - lx, dy = e.clientY - ly;
      lx = e.clientX; ly = e.clientY;
      if (!dragging && Math.hypot(e.clientX - sx, e.clientY - sy) > 3) dragging = true;
      if (!dragging) return;
      if (instDrag) { this.onDragMove(dx, dy); return; }
      if (planeDrag) { const uv = this.planePoint(e); if (uv) this.onPlaneDrag(uv[0], uv[1]); return; }
      const pan = button === 2 || (button === 1 && shift) || (button === 0 && shift && this.sketchFrame !== null);
      // in a sketch a left press that grabbed a handle drags it; any other left drag orbits (a trackpad has
      // no middle button), as do the middle button and alt+drag; clicks still draw with the active tool
      const orbit = !pan && (button === 1 || (button === 0 && (!this.sketchFrame || alt || this.sketchOrbitFree || !planeDrag)));
      if (pan) this.pan(dx, dy);
      else if (orbit) this.orbit(dx, dy);
    });
    el.addEventListener('pointerup', (e) => {
      const wasDrag = dragging;
      const b = button;
      const wasPlaneDrag = planeDrag;
      const wasInstDrag = instDrag;
      button = -1; dragging = false; planeDrag = false; instDrag = false;
      el.releasePointerCapture(e.pointerId);
      if (wasInstDrag) { this.onDragEnd(); if (!wasDrag && b === 0) this.click(e); return; }
      if (wasPlaneDrag && wasDrag) { const uv = this.planePoint(e); if (uv) this.onPlaneUp(uv[0], uv[1]); return; }
      if (!wasDrag && b === 0) this.click(e);
      else if (!wasDrag && b === 2) this.context(e);
      else if (wasDrag) this.cameraMoved();
    });
    el.addEventListener('pointerleave', () => { this.onHover(null); });
    el.addEventListener('dblclick', () => { if (this.sketchFrame) this.onPlaneDoubleClick(); });
    el.addEventListener('wheel', (e) => { e.preventDefault(); this.zoomAt(e); this.cameraMoved(); }, { passive: false });
  }

  private ndc(e: MouseEvent): THREE.Vector2 {
    const r = this.renderer.domElement.getBoundingClientRect();
    return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  }

  private visibleMeshes(): THREE.Mesh[] { return this.items.filter((it) => it.mesh.visible).map((it) => it.mesh); }

  private worldPerPixel(dist: number): number {
    const h = this.renderer.domElement.clientHeight || 1;
    if (this.useOrtho) { this.syncOrtho(); return (this.ortho.top - this.ortho.bottom) / h; }
    return (2 * dist * Math.tan((this.camera.fov * Math.PI) / 360)) / h;
  }

  private pickEntity(e: MouseEvent): PickedEntity | null {
    if (!this.faceOfVertex || !this.meshVisible || !this.mesh || this.inTriad(e)) return null;
    const ndc = this.ndc(e);
    const cam = this.active();
    this.raycaster.setFromCamera(ndc, cam);
    const faceHits = this.raycaster.intersectObjects(this.visibleMeshes(), false);
    const faceHit = faceHits.find((h) => !this.clip || this.clip.distanceToPoint(h.point) >= -1e-6) ?? null;
    const faceDist = faceHit ? faceHit.distance : Infinity;
    if (this.pickMode === 'faces') return faceHit?.face ? { kind: 'face', id: this.faceOfVertex[faceHit.face.a] } : null;
    const hitItem = faceHit ? this.items.find((it) => it.mesh === faceHit.object) ?? null : null;
    if (this.pickMode === 'edges' && !hitItem) return null;

    const rect = this.renderer.domElement.getBoundingClientRect();
    const px = (p: THREE.Vector3) => {
      const v = p.clone().project(cam);
      return new THREE.Vector2(((v.x + 1) / 2) * rect.width, ((1 - v.y) / 2) * rect.height);
    };
    const cursor = new THREE.Vector2(e.clientX - rect.left, e.clientY - rect.top);
    const eps = Math.max(this.size * 0.003, 0.05);
    const occluded = (p: THREE.Vector3) => {
      // a ray from the camera through the point (from the near plane for the orthographic camera)
      const v = p.clone().project(cam);
      const rc = new THREE.Raycaster();
      rc.setFromCamera(new THREE.Vector2(v.x, v.y), cam);
      rc.far = rc.ray.origin.distanceTo(p) - eps;
      return rc.intersectObjects(this.visibleMeshes(), false).some((h) => !this.clip || this.clip.distanceToPoint(h.point) >= -1e-6);
    };
    // vertices first: nearest to the cursor within 9 px, not occluded
    let best: { kind: 'vertex' | 'edge'; id: number; d: number } | null = null;
    const vp = this.mesh.vertexPositions;
    for (const it of this.pickMode === 'all' ? this.items : []) {
      if (!it.mesh.visible) continue;
      const [v0, vn] = it.item.vertices;
      for (let i = v0; i < v0 + vn; i++) {
        const p = new THREE.Vector3(vp[i * 3], vp[i * 3 + 1], vp[i * 3 + 2]);
        if (this.clip && this.clip.distanceToPoint(p) < -1e-6) continue;
        const d = px(p).distanceTo(cursor);
        if (d <= 9 && (!best || d < best.d) && !occluded(p)) best = { kind: 'vertex', id: i, d };
      }
    }
    if (best) return { kind: 'vertex', id: best.id };
    // edges: raycast the segments with a world threshold matching 7 px at the target distance
    const dist = faceHit ? faceHit.distance : this.camera.position.distanceTo(this.target);
    this.raycaster.params.Line.threshold = 7 * this.worldPerPixel(dist);
    // in the default mode only the edges of the item under the cursor are tested, so a big assembly stays quick
    const edgeObjs = (this.pickMode === 'all' ? this.items : hitItem ? [hitItem] : []).filter((it) => it.edges.visible).map((it) => it.edges);
    const hits = this.raycaster.intersectObjects(edgeObjs, false);
    for (const h of hits) {
      if (h.distance > faceDist + eps) continue;  // behind the surface the cursor is on
      if (this.clip && this.clip.distanceToPoint(h.point) < -1e-6) continue;
      const d = px(h.point).distanceTo(cursor);
      if (d > 8) continue;
      const it = this.items.find((x) => x.edges === h.object);
      const seg = Math.floor((h.index ?? 0) / 2);
      if (it && it.segEdge[seg] !== undefined && (!best || d < best.d)) best = { kind: 'edge', id: it.segEdge[seg], d };
    }
    if (best) return best;
    return faceHit?.face ? { kind: 'face', id: this.faceOfVertex[faceHit.face.a] } : null;
  }

  private planePoint(e: MouseEvent): [number, number] | null {
    if (!this.sketchFrame) return null;
    this.raycaster.setFromCamera(this.ndc(e), this.active());
    const p = this.raycaster.ray.intersectPlane(this.sketchFrame.plane, new THREE.Vector3());
    if (!p) return null;
    const d = p.sub(this.sketchFrame.origin);
    return [d.dot(this.sketchFrame.x), d.dot(this.sketchFrame.y)];
  }

  private hoverPending = false;
  private hover(e: PointerEvent) {
    if (this.hoverPending) return;
    this.hoverPending = true;
    requestAnimationFrame(() => {
      this.hoverPending = false;
      if (this.sketchFrame && !this.sketchPickBody) {
        const uv = this.planePoint(e);
        if (uv) this.onPlaneMove(uv[0], uv[1], this.pickEntity(e));
        return;
      }
      this.onHover(this.pickEntity(e));
    });
  }

  private context(e: PointerEvent) {
    if (this.inTriad(e)) return;
    if (this.sketchFrame && !this.sketchPickBody) {
      const uv = this.planePoint(e);
      if (uv) this.onPlaneContext(uv[0], uv[1], e, this.pickEntity(e));
      return;
    }
    this.onContextMenu(e, this.pickEntity(e));
  }

  private click(e: PointerEvent) {
    const tip = this.triadHit(e);
    if (tip) { this.onTriadClick(tip.axis, tip.towards); return; }
    if (this.sketchFrame && !this.sketchPickBody) {
      const uv = this.planePoint(e);
      if (uv) this.onPlaneClick(uv[0], uv[1], e, this.pickEntity(e));
      return;
    }
    if (this.planesPickable) {
      // a plane square nearer than the body under the cursor wins the click
      this.raycaster.setFromCamera(this.ndc(e), this.active());
      const faceHit = this.raycaster.intersectObjects(this.visibleMeshes(), false)[0];
      const plane = this.pickPlane(e, faceHit ? faceHit.distance : Infinity);
      if (plane) { this.onPlanePick(plane); return; }
    }
    this.onPick(this.pickEntity(e));
  }

  /** Sketch-plane coordinates under a client position, or null outside sketch mode. */
  planePointAt(clientX: number, clientY: number): [number, number] | null {
    return this.planePoint({ clientX, clientY } as MouseEvent);
  }

  /** World units per screen pixel at the sketch plane (or the orbit target). */
  pixelSize(): number {
    const p = this.sketchFrame ? this.sketchFrame.plane.projectPoint(this.target, new THREE.Vector3()) : this.target;
    return this.worldPerPixel(this.camera.position.distanceTo(p));
  }

  /** Screen position (px, relative to the canvas) of a world point. */
  toScreen(p: THREE.Vector3): [number, number] {
    const v = p.clone().project(this.active());
    const el = this.renderer.domElement;
    return [((v.x + 1) / 2) * el.clientWidth, ((1 - v.y) / 2) * el.clientHeight];
  }

  /** Centroid of a picked entity, for `nearest()` selectors. */
  /** The reference point of an entity: the one the server's nearest() measures from (a face's
   * centre, an edge's half-way point), taken from the mesh when it carries them. Without them a
   * full circle's average would be its centre, nowhere near the curve, and a nearest() written
   * from it could name another edge. */
  entityCenter(entity: PickedEntity): [number, number, number] | null {
    const m = this.mesh;
    if (!m) return null;
    const at = (arr: Float32Array, i: number): [number, number, number] => [arr[i * 3], arr[i * 3 + 1], arr[i * 3 + 2]];
    if (entity.kind === 'vertex') return entity.id * 3 + 2 < m.vertexPositions.length ? at(m.vertexPositions, entity.id) : null;
    if (entity.kind === 'edge') {
      if (entity.id * 3 + 2 < m.edgeCenters.length) return at(m.edgeCenters, entity.id);
      // fallback: the point half-way along the polyline
      const [start, count] = m.header.edge_ranges[entity.id] ?? [0, 0];
      if (count < 1) return null;
      if (count === 1) return at(m.edgePositions, start);
      const seg: number[] = [];
      let total = 0;
      for (let i = start; i < start + count - 1; i++) { const a = at(m.edgePositions, i), b = at(m.edgePositions, i + 1); const d = Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]); seg.push(d); total += d; }
      let half = total / 2;
      for (let i = 0; i < seg.length; i++) {
        if (half <= seg[i] || i === seg.length - 1) { const a = at(m.edgePositions, start + i), b = at(m.edgePositions, start + i + 1); const t = seg[i] ? half / seg[i] : 0; return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]; }
        half -= seg[i];
      }
      return null;
    }
    if (entity.id * 3 + 2 < m.faceCenters.length) return at(m.faceCenters, entity.id);
    // fallback: the area-weighted centroid of the triangles
    const [t0, tn] = m.header.face_ranges[entity.id] ?? [0, 0];
    const acc = [0, 0, 0]; let area = 0;
    for (let t = t0; t < t0 + tn; t++) {
      const a = at(m.positions, m.indices[3 * t]), b = at(m.positions, m.indices[3 * t + 1]), c = at(m.positions, m.indices[3 * t + 2]);
      const ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2], vx = c[0] - a[0], vy = c[1] - a[1], vz = c[2] - a[2];
      const w = Math.hypot(uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx) / 2;
      acc[0] += (a[0] + b[0] + c[0]) / 3 * w; acc[1] += (a[1] + b[1] + c[1]) / 3 * w; acc[2] += (a[2] + b[2] + c[2]) / 3 * w; area += w;
    }
    if (!area) return null;
    return [acc[0] / area, acc[1] / area, acc[2] / area];
  }

  /** Trackball orbit: a horizontal drag turns about the screen's vertical axis, a vertical drag
   * about its horizontal axis, without limit; the up vector turns along, so the model can be
   * rolled right over. The standard views and "normal to" set the up vector straight again. */
  private orbit(dx: number, dy: number) {
    const off = this.camera.position.clone().sub(this.target);
    const right = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 0).normalize();
    const up = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 1).normalize();
    const q = new THREE.Quaternion().setFromAxisAngle(up, -dx * 0.008)
      .multiply(new THREE.Quaternion().setFromAxisAngle(right, -dy * 0.008));
    off.applyQuaternion(q);
    this.camera.up.copy(up).applyQuaternion(q);
    this.camera.position.copy(this.target).add(off);
    this.camera.lookAt(this.target);
    this.requestRender();
  }

  /** Screen axes and the world size of a pixel at the orbit target, for dragging parts. */
  screenAxes(): { right: [number, number, number]; up: [number, number, number]; worldPerPixel: number } {
    const right = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 0).normalize();
    const up = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 1).normalize();
    return { right: [right.x, right.y, right.z], up: [up.x, up.y, up.z], worldPerPixel: this.worldPerPixel(this.camera.position.distanceTo(this.target)) };
  }

  /** Show instances at other poses without a new mesh: each item whose instance (the first
   * segment of its path) is listed moves by `to · from⁻¹`; null puts everything back. */
  setPosePreview(preview: Record<string, { from: number[][]; to: number[][] }> | null) {
    for (const it of this.items) {
      const inst = it.item.path.split('.')[0];
      const p = preview?.[inst];
      for (const obj of [it.mesh, it.edges]) {
        if (p) {
          const from = new THREE.Matrix4().set(...(p.from.flat() as [number, number, number, number, number, number, number, number, number, number, number, number, number, number, number, number]));
          const to = new THREE.Matrix4().set(...(p.to.flat() as [number, number, number, number, number, number, number, number, number, number, number, number, number, number, number, number]));
          obj.matrixAutoUpdate = false;
          obj.matrix.copy(to.multiply(from.invert()));
          obj.matrixWorldNeedsUpdate = true;
        } else if (!obj.matrixAutoUpdate) {
          obj.matrixAutoUpdate = true;
          obj.matrix.identity();
          obj.position.set(0, 0, 0); obj.quaternion.identity(); obj.scale.set(1, 1, 1);
          obj.matrixWorldNeedsUpdate = true;
        }
      }
    }
    this.requestRender();
  }

  private pan(dx: number, dy: number) {
    const dist = this.camera.position.distanceTo(this.target);
    const scale = this.worldPerPixel(dist);
    const right = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 0);
    const up = new THREE.Vector3().setFromMatrixColumn(this.camera.matrixWorld, 1);
    const move = right.multiplyScalar(-dx * scale).add(up.multiplyScalar(dy * scale));
    this.camera.position.add(move);
    this.target.add(move);
    this.requestRender();
  }

  private zoomAt(e: WheelEvent) {
    const factor = Math.exp(Math.sign(e.deltaY) * 0.12);
    this.raycaster.setFromCamera(this.ndc(e), this.active());
    const viewDir = this.camera.getWorldDirection(new THREE.Vector3());
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(viewDir, this.target);
    const p = this.raycaster.ray.intersectPlane(plane, new THREE.Vector3()) ?? this.target.clone();
    this.camera.position.sub(p).multiplyScalar(factor).add(p);
    this.target.sub(p).multiplyScalar(factor).add(p);
    this.requestRender();
  }

  // ---- rendering --------------------------------------------------------------

  requestRender() { this.needsRender = true; }

  private resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = '100%';
    this.renderer.domElement.style.height = '100%';
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    for (const l of this.labels) this.scaleLabel(l);
    this.requestRender();
  }

  private loop = () => {
    if (this.disposed) return;
    if (this.needsRender) { this.needsRender = false; this.renderAll(); }
    requestAnimationFrame(this.loop);
  };

  /** The scene, then the triad in the bottom-left corner turned like the camera. */
  private renderAll() {
    const r = this.renderer;
    r.setScissorTest(false);
    r.render(this.scene, this.active());
    const s = this.triadPx;
    this.placeTriadCamera();
    r.autoClear = false;
    r.setViewport(0, 0, s, s);
    r.setScissor(0, 0, s, s);
    r.setScissorTest(true);
    r.render(this.triadScene, this.triadCamera);
    r.setScissorTest(false);
    r.setViewport(0, 0, this.container.clientWidth || 1, this.container.clientHeight || 1);
    r.autoClear = true;
  }

  async snapshot(): Promise<Blob> {
    this.renderAll();
    return new Promise((resolve, reject) =>
      this.renderer.domElement.toBlob((b) => (b ? resolve(b) : reject(new Error('snapshot failed'))), 'image/png'));
  }

  dispose() {
    this.disposed = true;
    this.observer.disconnect();
    this.clearMesh();
    this.setMeasurements([]);
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
