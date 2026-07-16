/**
 * A rolling, fixed-size tail buffer: retains only the last `maxBytes` of appended output so a process that
 * prints unbounded data can never OOM the client before its result is context-capped. Shared by the
 * coder-handover capture (agent/handover.ts) and the tool-bridge execute_command capture (tools/runTool.ts)
 * — the two sites previously diverged: handover was bounded (#1543) but collectChild retained full streams
 * and concatenated them at close, so a multi-GB printer crashed the Ink client (#1540 defect B).
 */

/** Default capture cap — a coder/command result is a short summary; anything larger is tail-truncated. */
export const MAX_CAPTURE_BYTES = 256 * 1024;

const TRUNCATED_MARKER = Buffer.from('…(truncated)…', 'utf-8');

export class BoundedTail {
  private tail: Buffer = Buffer.alloc(0);
  private truncated = false;

  constructor(private readonly maxBytes: number = MAX_CAPTURE_BYTES) {}

  append(chunk: Buffer): void {
    if (chunk.length >= this.maxBytes) {
      this.tail = chunk.subarray(chunk.length - this.maxBytes);
      this.truncated = true;
      return;
    }
    const overflow = this.tail.length + chunk.length - this.maxBytes;
    if (overflow > 0) {
      this.tail = Buffer.concat([this.tail.subarray(overflow), chunk], this.maxBytes);
      this.truncated = true;
    } else {
      this.tail = Buffer.concat([this.tail, chunk], this.tail.length + chunk.length);
    }
  }

  text(): string {
    const retained = this.truncated ? Buffer.concat([TRUNCATED_MARKER, this.tail]) : this.tail;
    return retained.toString('utf-8');
  }
}
