import { useSyncExternalStore } from 'react';
import type { ParsedMesh } from '../api/mesh';
import type {
  Bom,
  CameraState,
  DocSummary,
  EntityKind,
  Interference,
  MeasureRef,
  MeasureResult,
  Overlay,
  PickedEntity,
  Pin,
  PlaneInfo,
  ProjectFile,
  SectionSpec,
  Summary,
  Tree,
  ViewSnapshot,
  ImportItem,
} from '../api/types';
import type { DimensionPlan } from '../sketch/model';

export type Plane = 'XY' | 'XZ' | 'YZ';
export type SketchTool = 'line' | 'circle' | 'arc' | 'rect' | 'slot' | 'polygon' | 'point' | 'dimension' | 'project' | 'offset' | null;
export type Tool = 'none' | 'section' | 'measure' | 'move';
export type MoveMode = 'translate' | 'rotate';
/** Instances shown at other poses than the mesh's: the mesh pose and the previewed one, by instance name. */
export type PosePreview = Record<string, { from: number[][]; to: number[][] }>;

export type Coords = Record<string, Record<string, unknown>>;

export interface SketchMode {
  sketch: string; plane: string; offset: number; tool: SketchTool;
  frame: PlaneInfo;               // the resolved plane the sketch lies on (from result.plane)
  construction: boolean;          // draw-as-construction mode: new entities get construction=True
  selection: string[];          // entity references (handles and curves) picked in the viewport
  bodySelection: PickedEntity[];  // body faces, edges and vertices picked for conversion
  hover: string | null;
  highlight: string[];          // references lit from the constraint panel
  preview: Coords | null;       // solved coordinates while dragging
  dragging: string | null;      // the reference being dragged
  locked: boolean;              // the last drag preview did not move the point
  solveMs: number | null;
  dimPlacing: DimensionPlan | null;  // dimension tool: waiting for a label placement click
  dimEditing: string | null;    // constraint name whose value field is open
}

export interface MeasureState { picks: MeasureRef[]; result: MeasureResult | null; pending: boolean }

/** A picked reference for a dialog field: body geometry as a selector expression, or a plane. */
export type PickTarget =
  | { kind: EntityKind; id: number; center: [number, number, number]; expr: string; label: string; owner: string }
  | { kind: 'plane'; name: string; expr: string; label: string; standard: boolean };

/** A dialog waiting for a viewport pick. */
export interface PickRequest { kinds: EntityKind[]; planes: boolean; hint: string; onPick: (t: PickTarget) => void; multi?: boolean }

export type PlaneForm = 'offset' | 'angle' | 'midplane' | 'through';
export type FeatureDialogKind = 'fillet' | 'chamfer' | 'shell' | 'revolve' | 'linear_pattern' | 'circular_pattern' | 'mirror' | 'instance' | 'mate' | 'extrude' | 'cut';
/** The "+ feature" dialog: what to add, and the sketch or feature it acts on (patterns, mirrors, revolves). */
export interface FeatureDialog { kind: FeatureDialogKind; target: string | null }
/** Assembly queries fetched on demand, remembered for the hash they were computed against. */
export interface AssemblyQueries { revision: string; bom: Bom | null; interference: Interference | null; pending: boolean }

export type DrawingTool = 'dimension' | 'note' | null;
/** One line of a context menu; a separator has only `sep`. */
export interface MenuEntry { label?: string; run?: () => void; disabled?: boolean; danger?: boolean; sep?: boolean; title?: string; id?: string }
export interface ContextMenuState { x: number; y: number; title?: string; entries: MenuEntry[] }
export type DimKind = 'auto' | 'distance' | 'diameter' | 'radius' | 'angle';
/** An edge picked on the sheet for a dimension: the selector the server gave the segment, its view, whether it is round. */
export interface DrawingPick { ref: string; view: string; round: boolean }

export interface State {
  docs: DocSummary[];
  docId: string | null;
  tree: Tree | null;
  source: string;
  hash: string;
  revision: string;
  sourceHash: string;
  saveState: 'saved' | 'unsaved' | 'saving' | 'error' | 'conflict';
  closingDoc: string | null;
  mesh: ParsedMesh | null;
  upto: string | null;
  selected: string | null;      // feature name
  selectedFace: number | null;
  selectedEdge: number | null;
  selectedItem: string | null;  // instance path (assemblies)
  hover: PickedEntity | null;
  status: string;
  error: string | null;
  loading: boolean;
  codeDirty: boolean;           // user is editing the code pane
  sourceDoc: string | null;     // the document `source` belongs to
  sourceBase: string;           // the text the pane last loaded from the server (what a dirty edit started from)
  externalPending: boolean;     // file changed while codeDirty
  sketchMode: SketchMode | null;
  // review tools: sections, measurements, views and overlays
  tool: Tool;
  section: SectionSpec | null;      // wanted section; the mesh header says which section is installed
  previewOffset: number | null;     // while dragging the slider: GPU clip at this offset
  plainMesh: ParsedMesh | null;     // last uncut mesh for the current hash and upto (slider previews clip this)
  sectionPending: boolean;          // a capped-section fetch is in flight
  sectionSeconds: number | null;    // duration of the last capped-section fetch
  sectionInstalls: number;          // how many capped-section meshes have been installed (for verification)
  overlay: Overlay | null;          // comparing with another file or a git revision: the mesh is common/added/removed
  visibility: Record<string, boolean>;
  transparency: Record<string, boolean>;
  measure: MeasureState;
  pins: Pin[];
  named: Record<string, ViewSnapshot>;
  camera: CameraState | null;       // last known camera, from the scene
  cameraToApply: CameraState | null; // set on load or view restore; the viewport consumes it
  fitPending: boolean;              // a document arrived without a saved camera: fit its first mesh
  viewsWarning: string | null;
  showPlanes: boolean;              // draw reference planes and the standard planes
  ortho: boolean;                   // orthographic projection
  orthoBeforeSketch: boolean | null; // the projection to restore when the sketch closes
  ghostMesh: ParsedMesh | null;     // sketch mode: the finished body, drawn faint behind the pre-sketch body; or a feature preview
  ghostStyle: 'ghost' | 'preview';
  planeDialog: PlaneForm | null;    // the "+ plane" dialog is open in the property panel
  featureDialog: FeatureDialog | null;  // a fillet, chamfer, shell, revolve, pattern or mirror dialog is open
  dialogPicks: PickedEntity[];      // entities a dialog has picked so far, lit in the viewport
  pickRequest: PickRequest | null;  // a dialog field is waiting for a viewport pick
  queries: AssemblyQueries | null;  // bill of materials and interference of the current assembly
  files: ProjectFile[];             // model and STEP files under the project, for choosers
  filesRoot: string;                // the project directory those paths are relative to
  imports: ImportItem[];            // STEP files waiting for the import dialog; the first one shows
  moveMode: MoveMode;               // the move tool: slide or turn the dragged instance
  posePreview: PosePreview | null;  // assemblies: instances drawn at previewed poses (a drag, a mate being added)
  previewNote: string | null;       // what the preview says: constrained, free, or conflicting
  drawingTool: DrawingTool;         // drawings: picking edges for a dimension, or placing a note
  drawingPicks: DrawingPick[];      // the edges picked so far for a dimension
  dimKind: DimKind;                 // what the next dimension measures; auto infers from the picks
  busy: number;                     // requests in flight (edits, previews, solves, measurements): the viewport shows a line
  deleteConfirm: string | null;     // a feature whose delete needs confirming (the server would cascade)
  treeHover: { feature?: string; item?: string } | null;  // the tree row under the mouse: its geometry lights in the viewport
  refHighlight: PickedEntity[];     // what a reference under the mouse in the panel picks, lit in the viewport
  contextMenu: ContextMenuState | null;  // the right-click menu, entries computed where the click landed
  codeReveal: number;               // bumps when something wants the code pane shown
  summary: { revision: string; data: Summary } | null;        // the part overview's numbers, for the hash they were computed against
  version: number;              // bumps on every state change, for effects
}

const initial: State = {
  docs: [], docId: null, tree: null, source: '', hash: '', revision: '', sourceHash: '', saveState: 'saved', closingDoc: null, mesh: null, upto: null,
  selected: null, selectedFace: null, selectedEdge: null, selectedItem: null, hover: null, status: 'starting', error: null,
  loading: false, codeDirty: false, sourceDoc: null, sourceBase: '', externalPending: false, sketchMode: null,
  tool: 'none', section: null, previewOffset: null, plainMesh: null, sectionPending: false, sectionSeconds: null,
  sectionInstalls: 0, overlay: null, visibility: {}, transparency: {},
  measure: { picks: [], result: null, pending: false }, pins: [], named: {}, camera: null, cameraToApply: null, fitPending: false,
  viewsWarning: null, showPlanes: false, ortho: false, orthoBeforeSketch: null, ghostMesh: null, ghostStyle: 'ghost',
  planeDialog: null, featureDialog: null, dialogPicks: [], pickRequest: null, queries: null, files: [], filesRoot: '', imports: [],
  moveMode: 'translate', posePreview: null, previewNote: null, drawingTool: null, drawingPicks: [], dimKind: 'auto', busy: 0, deleteConfirm: null, treeHover: null, summary: null, refHighlight: [], contextMenu: null, codeReveal: 0, version: 0,
};

export let state: State = initial;
const listeners = new Set<() => void>();

export function set(patch: Partial<State>, notify = true) {
  state = { ...state, ...patch, version: state.version + 1 };
  if (notify) listeners.forEach((l) => l());
}

/** Count a request while it is in flight, so the viewport can show that something is happening. */
export async function withBusy<T>(fn: () => Promise<T>): Promise<T> {
  set({ busy: state.busy + 1 });
  try { return await fn(); } finally { set({ busy: Math.max(0, state.busy - 1) }); }
}

export function getState() { return state; }
export function useStore<T>(selector: (s: State) => T): T {
  return useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, () => selector(state));
}

export const isAssembly = (tree: Tree | null) => tree?.kind === 'assembly';
export const isDrawing = (tree: Tree | null) => tree?.kind === 'drawing';
/** A STEP viewer: an assembly document made only of imports, what opening a STEP file writes. */
export const isViewer = (tree: Tree | null) => !!tree && tree.kind === 'assembly' && tree.features.length > 0 && tree.features.every((f) => f.kind === 'import_step');
