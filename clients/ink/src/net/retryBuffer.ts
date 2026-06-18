/**
 * Tool-result retry buffer (§3b client resilience).
 *
 * A `POST /tool-result` that fails on a **transient** error (dropped connection, timeout, 5xx)
 * is buffered and resent on the next contact, so a brief network blip mid-turn doesn't leave the
 * server-side ToolBridge stalled waiting for a result. A **permanent** rejection (HTTP 4xx — e.g.
 * 410 Gone: the server bridge already timed out / the turn moved on) is dropped, NOT retried
 * (parity with the old "swallow URLError" behaviour for genuinely-stale results).
 *
 * Bounded (a long outage can't grow it without limit) and fail-soft (never throws into the turn).
 */
import {HttpError} from './server.js';
import type {Server} from './server.js';

export interface PendingResult {
  id: string;
  result: string;
}

export class ToolResultBuffer {
  private q: PendingResult[] = [];

  constructor(private readonly max = 100) {}

  get size(): number {
    return this.q.length;
  }

  /** Transient = worth retrying (network/timeout/5xx). HTTP 4xx = permanent → drop. */
  private static transient(e: unknown): boolean {
    if (e instanceof HttpError) return e.status >= 500;
    return true; // fetch rejected (network/abort/timeout) → transient
  }

  private enqueue(p: PendingResult): void {
    this.q.push(p);
    if (this.q.length > this.max) this.q.shift(); // bounded: drop the oldest
  }

  /** Resend all buffered results in order. Stops at the first still-transient failure (keeps the
   *  rest for later, never hammers a down server); permanent failures are dropped. Never throws. */
  async flush(srv: Server): Promise<void> {
    while (this.q.length) {
      const p = this.q[0]!;
      try {
        await srv.req('POST', '/tool-result', {id: p.id, result: p.result});
        this.q.shift(); // delivered → drop
      } catch (e) {
        if (ToolResultBuffer.transient(e)) return; // still down → keep order, try again later
        this.q.shift(); // permanent (stale) → drop this one, continue draining
      }
    }
  }

  /** Drain any buffered results first (preserves ordering), then POST *p*; on a transient failure
   *  buffer *p* for a later flush. Never throws — a result POST must never break the turn. */
  async send(srv: Server, p: PendingResult): Promise<void> {
    await this.flush(srv);
    try {
      await srv.req('POST', '/tool-result', {id: p.id, result: p.result});
    } catch (e) {
      if (ToolResultBuffer.transient(e)) this.enqueue(p);
      // permanent → drop (the server won't accept this stale result)
    }
  }
}
