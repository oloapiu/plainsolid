export interface DocSummary { id: string; path: string; hash: string; name: string; kind?: string }

/** A model file or STEP file under the project, for choosers. */
export interface ProjectFile { path: string; kind: 'part' | 'assembly' | 'drawing' | 'step'; wrapper?: string }

export interface DocError { message: string; line: number | null; kind: string }

export interface Param {
  name: string; value: number; description: string; line: number | null;
  expression: string | null; read_only: boolean;
}

export interface Entity {
  name: string; kind: string; args: Record<string, unknown>; arg_texts?: Record<string, string>;
  construction: boolean; line: number | null;
}

export interface Constraint {
  name: string; kind: string; refs: string[]; value: number | null; options: Record<string, unknown>;
  line: number | null; value_text: string | null; dimension: boolean;
}

export interface ProjectedItem { kind: 'line' | 'circle' | 'arc' | 'point'; start?: [number, number]; end?: [number, number]; center?: [number, number]; radius?: number; at?: [number, number] }

export interface SketchSolution {
  coords: Record<string, Record<string, unknown>>; dof: number; rank: number; redundant: string[]; conflicting: string[];
  residual: number; free_entities: string[]; fully_constrained: boolean;
  free_variables?: Record<string, string[]>;  // per free entity, which of its variables are loose
  projected: Record<string, { name: string; items: ProjectedItem[] }>; ms: number;
}

export interface PlaneInfo {
  origin: [number, number, number]; x_dir: [number, number, number]; y_dir: [number, number, number];
  z_dir: [number, number, number]; size: number;
}

export interface FeatureResult {
  ok: boolean; skipped: boolean; error: DocError | null; warnings: string[];
  faces_created: number; seconds: number; sketch?: SketchSolution | null; plane?: PlaneInfo | null;
}

/** What a delete_feature op removes besides the feature itself (the server cascades). */
export interface Dependents { features: string[]; entities: number }

export interface Feature {
  name: string; kind: string; args: Record<string, unknown>; arg_texts: Record<string, string>;
  span: [number, number] | null; read_only: boolean; variable: string | null;
  entities: Entity[]; constraints?: Constraint[]; suppressed: boolean; result: FeatureResult | null;
  dependents?: Dependents;
}

export interface Instance {
  name: string; product: string; path: string; color: [number, number, number, number] | null;
  assembly: boolean; solids: number; children: Instance[];
  transform?: number[][] | null;   // 4x4 row-major world pose (instance features and imported nodes)
  file?: string | null;            // the part or STEP file an instance feature loaded
  kind?: string;                   // part | step
}

/** What the pose solver found for an assembly document. */
export interface AssemblySolution {
  dof: number; rank: number; variables: number; redundant: string[]; conflicting: string[]; free: string[];
  residual: number; iterations: number; ms: number; warnings: string[];
  poses: Record<string, { at: number[]; rotate: number[]; transform: number[][] }>;
}

export interface Tree {
  id: string; hash: string; revision: string; kind: string; meta: Record<string, unknown>; params: Param[];
  features: Feature[]; errors: DocError[]; path: string | null;
  names?: { params: string[]; dimensions: string[] };
  evaluation: { has_body: boolean; seconds: number; kind?: string; instances?: Instance[]; cached?: number; assembly?: AssemblySolution | null; drawing?: DrawingScene | null };
}

/** Feature kinds that add or remove material (what a pattern or mirror can repeat) and body kinds. */
export const TOOL_KINDS = ['extrude', 'cut', 'revolve', 'linear_pattern', 'circular_pattern', 'mirror'];
export const BODY_KINDS = [...TOOL_KINDS, 'fillet', 'chamfer', 'shell', 'import_step'];
/** Assembly documents: instance features and the mates between them. */
export const MATE_KINDS = ['fixed', 'coincident', 'concentric', 'distance', 'parallel', 'angle'];
export type MateKind = 'fixed' | 'coincident' | 'concentric' | 'distance' | 'parallel' | 'angle';

export interface BomRow {
  file: string; name: string; kind: string; count: number; instances: string[]; material: string | null;
  density: number | null; volume: number; mass: number | null; color: string | null;
}
export interface Bom { rows: BomRow[]; instances: number; mass: number | null; unit: string; unknown_mass: string[] }
export interface Interference { pairs: { a: string; b: string; volume: number }[]; count: number; unit: string }
/** The `summary` query of a part or assembly. */
export interface Summary {
  volume: number; area: number; bbox: { min: number[]; max: number[]; size: number[]; center: number[] };
  counts: { solids: number; faces: number; edges: number; vertices: number }; center_of_mass: number[]; mass: number | null;
}

export type EditValue = number | boolean | string | [number, number] | number[][] | { expr: string } | null;
export type { EditOp, JsonValue } from './operations';

export interface EditResult {
  changed: boolean; hash: string; diff?: string; solution?: SketchSolution | null;
  part?: string; instance?: string; instances?: Record<string, string>; repointed?: string[]; skipped?: string[];  // make_editable, explode_import
}

export type WsEvent = { event: 'hello' | 'changed' | 'external' | 'dependency'; doc: string; hash: string; revision: string };

// ---- sections, measurements, views ---------------------------------------------

export type SectionPlane = 'XY' | 'XZ' | 'YZ';
export interface SectionSpec { plane: SectionPlane; offset: number; flip: boolean }

export type EntityKind = 'face' | 'edge' | 'vertex';
export interface PickedEntity { kind: EntityKind; id: number }
export type MeasureRef = { face: number } | { edge: number } | { vertex: number } | { point: [number, number, number] };

export interface MeasureInfo {
  kind: string; id?: number; item?: string; label?: string; type?: string;
  area?: number; length?: number; center?: number[]; normal?: number[]; axis?: number[]; axis_point?: number[];
  direction?: number[]; radius?: number; diameter?: number; start?: number[]; end?: number[]; position?: number[];
}

export interface MeasureResult {
  a: MeasureInfo; b?: MeasureInfo; distance?: number; closest?: [number[], number[]]; delta?: number[];
  angle?: number; axis_distance?: number; center_distance?: number;
}

export interface CameraState { position: [number, number, number]; target: [number, number, number]; up: [number, number, number]; ortho?: boolean }

export interface Pin {
  a: MeasureRef; b: MeasureRef | null; text: string; points: number[][]; result: MeasureResult;
}

export interface ViewSnapshot {
  camera: CameraState | null; section: SectionSpec | null; pins: Pin[];
  visibility: Record<string, boolean>; transparency: Record<string, boolean>;
}

export interface ViewsState extends ViewSnapshot {
  version: number; named: Record<string, ViewSnapshot>; warning?: string;
}

// ---- drawings: the sheet as one 2D scene in sheet millimetres, y up from the bottom-left corner

/** A line (p = x0 y0 x1 y1), circle (c, r), arc (c, r, a = start and end degrees counter-clockwise) or polyline (p). */
export interface DwgSeg { k: 'l' | 'c' | 'a' | 'p'; p?: number[]; c?: [number, number]; r?: number; a?: [number, number]; ref?: string }
export interface DwgText { at: [number, number]; text: string; size: number; anchor: 'start' | 'middle' | 'end'; angle?: number; layer?: string }
export interface DwgTrace { section: string; letter: string; line: number[]; lines: number[][]; arrows: number[][]; texts: DwgText[] }
export interface DwgView {
  name: string; direction: string; at: [number, number]; scale: number; section: string | null; offset: number; flip: boolean;
  hidden_lines: boolean; bbox: [number, number, number, number]; label: string | null; label_at: [number, number] | null;
  visible: DwgSeg[]; hidden: DwgSeg[]; hatch: DwgSeg[]; traces: DwgTrace[];
}
export interface DwgDimension {
  name: string; view: string; kind: string; value: number; text: string; lines: number[][]; arrows: number[][]; arc: number[] | null;
  label: { at: [number, number]; text: string; angle: number; anchor: 'start' | 'middle' | 'end' } | null; at: [number, number]; refs: string[];
}
export interface DwgNote { name: string; text: string; at: [number, number]; size: number; view: string | null }
export interface DrawingScene {
  sheet: { size: string; width: number; height: number; scale: number };
  model: { path: string; name: string | null; kind: string | null; error: string | null; planes?: string[]; meta?: Record<string, string> };
  frame: { lines: number[][]; texts: DwgText[] };
  views: DwgView[]; dimensions: DwgDimension[]; notes: DwgNote[];
}
export const VIEW_DIRECTIONS = ['front', 'top', 'right', 'left', 'back', 'bottom', 'iso'];
export const DIMENSION_KINDS = ['distance', 'diameter', 'radius', 'angle'];

// ---- previews: poses without writing
export type PoseMap = Record<string, { at: number[]; rotate: number[]; transform: number[][] }>;
export interface PreviewResult {
  ok: boolean; hash: string; error?: string; errors?: DocError[]; results?: Record<string, FeatureResult & { name: string }>;
  poses: PoseMap; assembly?: AssemblySolution | null; flip?: boolean; rotation?: number; conflicting?: string[];
}
export interface DragResult { moved: boolean; reason?: string; poses: PoseMap; conflicting?: string[]; dof?: number }

/** What `compare` says: this document's geometry against another's (or its own earlier version). */
export interface CompareRegion { volume: number; min: number[]; max: number[]; size: number[]; center: number[] }
export interface CompareReport {
  base: { volume: number; min: number[]; max: number[]; size: number[]; center: number[] };
  other: { volume: number; min: number[]; max: number[]; size: number[]; center: number[] };
  common: number;
  added: { volume: number; count: number; regions: CompareRegion[] };
  removed: { volume: number; count: number; regions: CompareRegion[] };
  change: number;
  same: boolean;
  unit: string;
}
/** What the viewport compares the document with: another file, or this file at a git revision. */
export interface Overlay { other?: string; rev?: string }
