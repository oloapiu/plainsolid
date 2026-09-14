import { state } from './core';

// Shared request lifetime: a tab session survives failed navigation, but every
// successful switch invalidates its replies, including when revisiting the same file.

export let documentEpoch = 0;
const lanes = new Set<RequestLane>();

export function invalidateDocumentRequests(): number {
  documentEpoch++;
  for (const lane of lanes) lane.cancel();
  return documentEpoch;
}

export function requestScope(geometry = false) {
  const epoch = documentEpoch, id = state.docId, revision = state.revision, upto = state.upto;
  return () => epoch === documentEpoch && id === state.docId &&
    (!geometry || (revision === state.revision && upto === state.upto));
}

/** One latest-wins request stream. Aborting saves work where supported; the
 * scope check still protects callers whose transport cannot be cancelled. */
export class RequestLane {
  private controller: AbortController | null = null;
  constructor() { lanes.add(this); }
  cancel() { this.controller?.abort(); this.controller = null; }
  start(geometry = false) {
    this.cancel();
    const controller = this.controller = new AbortController();
    const scope = requestScope(geometry);
    return { signal: controller.signal, current: () => this.controller === controller && !controller.signal.aborted && scope() };
  }
}

/** Latest-wins on top of a lane: one call runs at a time. A call made meanwhile waits, and a
 * newer waiting call replaces it, the replaced one answering `skipped`. Field changes faster
 * than the server answers thus cost one request each way, never a backlog of obsolete work. */
export class Coalesced<A extends unknown[], R> {
  readonly lane = new RequestLane();
  private busy = false;
  private waiting: { args: A; resolve: (r: R) => void } | null = null;
  constructor(private readonly fn: (lane: RequestLane, ...args: A) => Promise<R>, private readonly skipped: R) {}
  call(...args: A): Promise<R> {
    return new Promise<R>((resolve) => {
      if (this.busy) { this.waiting?.resolve(this.skipped); this.waiting = { args, resolve }; return; }
      void this.run(args, resolve);
    });
  }
  /** Abort what is in flight and drop what is waiting. */
  cancel() {
    this.lane.cancel();
    this.waiting?.resolve(this.skipped);
    this.waiting = null;
  }
  private async run(args: A, resolve: (r: R) => void) {
    this.busy = true;
    try { resolve(await this.fn(this.lane, ...args)); }
    finally {
      this.busy = false;
      const next = this.waiting;
      this.waiting = null;
      if (next) void this.run(next.args, next.resolve);
    }
  }
}
