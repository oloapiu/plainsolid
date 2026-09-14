import type { CompareReport, SectionSpec } from './types';

export interface MeshItem {
  name: string; path: string; color: [number, number, number, number] | null;
  faces: [number, number]; edges: [number, number]; vertices: [number, number]; triangles: [number, number];
}

export interface MeshHeader {
  version: number;
  ok?: boolean; error?: string | null; faces?: number;  // a preview mesh: what the candidate feature made
  sections: Record<string, { offset: number; count: number; dtype: string }>;
  face_ranges: [number, number][];
  edge_ranges: [number, number][];
  face_labels: Record<string, string>;
  edge_labels: Record<string, string>;
  face_tags?: Record<string, string[]>;   // identity-map tags per face: ":top", "rect1.left", ...
  edge_tags?: Record<string, string[]>;
  items: MeshItem[];
  section_faces: number[];
  vertices: number;
  bbox: [number[], number[]];
  triangles: number;
  hash?: string;
  revision?: string;
  upto?: string | null;
  section?: (SectionSpec & { origin?: number[] | null; normal?: number[] | null }) | null;
  compare?: CompareReport | null;   // an overlay mesh: items common, added, removed and the report
}

export interface ParsedMesh {
  header: MeshHeader;
  positions: Float32Array;
  normals: Float32Array;
  indices: Uint32Array;
  triangleFaceIds: Uint32Array;
  edgePositions: Float32Array;
  vertexPositions: Float32Array;
  faceCenters: Float32Array;   // the server's reference point per face (what nearest() measures from)
  edgeCenters: Float32Array;   // and per edge: its half-way point
}

const TYPED: Record<string, new (b: ArrayBuffer, o: number, n: number) => Float32Array | Uint32Array> = {
  float32: Float32Array, uint32: Uint32Array,
};

export function parseMesh(buffer: ArrayBuffer): ParsedMesh {
  const view = new DataView(buffer);
  const headerLen = view.getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, headerLen))) as MeshHeader;
  header.items ??= [];
  header.section_faces ??= [];
  header.face_tags ??= {};
  header.edge_tags ??= {};
  const dataStart = 4 + headerLen;
  const section = (name: string) => {
    const s = header.sections[name];
    if (!s) return new Float32Array(0);
    const Ctor = TYPED[s.dtype];
    // copy so the typed array is aligned even when the section offset is not a multiple of 4
    const src = new Uint8Array(buffer, dataStart + s.offset, s.count * 4);
    const copy = new Uint8Array(src.length);
    copy.set(src);
    return new Ctor(copy.buffer, 0, s.count);
  };
  return {
    header,
    positions: section('positions') as Float32Array,
    normals: section('normals') as Float32Array,
    indices: section('indices') as Uint32Array,
    triangleFaceIds: section('triangle_face_ids') as Uint32Array,
    edgePositions: section('edge_positions') as Float32Array,
    vertexPositions: section('vertex_positions') as Float32Array,
    faceCenters: section('face_centers') as Float32Array,
    edgeCenters: section('edge_centers') as Float32Array,
  };
}
