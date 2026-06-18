/**
 * Tool-bridge passthrough ≙ client.py:_run_passthrough_tool (client.py:236-252).
 *
 * A `TR{json}` frame arrived on the stream → run the tool LOCALLY and POST the result to
 * /tool-result. Two parity points:
 *  - a tool error never breaks the stream (runTool already returns "ERROR: …"; the extra
 *    try/catch is the belt-and-braces equivalent of the Python `except Exception → ERROR`);
 *  - the result POST never crashes the turn. §3b: instead of swallowing a transient transport
 *    error, it goes through a retry buffer — a dropped connection / 5xx is buffered and resent on
 *    the next contact (so the server-side ToolBridge isn't left stalled), while a permanent 4xx
 *    (e.g. 410 Gone) is still dropped as a stale result.
 */
import {runTool} from './runTool.js';
import {ToolResultBuffer} from '../net/retryBuffer.js';
import type {Server} from '../net/server.js';
import type {ToolFrame} from '../net/stream.js';

const _results = new ToolResultBuffer();

/** Resend any tool-results buffered from an earlier transient failure (call on reconnect). */
export function flushToolResults(srv: Server): Promise<void> {
  return _results.flush(srv);
}

/** Number of tool-results currently waiting to be resent (for status/diagnostics/tests). */
export function pendingToolResults(): number {
  return _results.size;
}

export async function runPassthroughTool(srv: Server, frame: ToolFrame): Promise<void> {
  let result: string;
  try {
    result = await runTool(frame.name, frame.args);
  } catch (e) {
    result = `ERROR: ${e instanceof Error ? e.message : String(e)}`;
  }
  // buffer-on-transient-fail + drain-first; never throws into the turn.
  await _results.send(srv, {id: frame.id, result});
}
