/**
 * Tool-bridge passthrough ≙ client.py:_run_passthrough_tool (client.py:236-252).
 *
 * A `TR{json}` frame arrived on the stream → run the tool LOCALLY and POST the result to
 * /tool-result. Two parity points:
 *  - a tool error never breaks the stream (runTool already returns "ERROR: …"; the extra
 *    try/catch is the belt-and-braces equivalent of the Python `except Exception → ERROR`);
 *  - the result POST swallows ANY transport error (Python catches `urllib.error.URLError`,
 *    of which HTTPError is a subclass) — so a 410 Gone (server-side bridge already timed
 *    out/cancelled) or a dropped connection is silently ignored, never crashing the turn.
 */
import {runTool} from './runTool.js';
import type {Server} from '../net/server.js';
import type {ToolFrame} from '../net/stream.js';

export async function runPassthroughTool(srv: Server, frame: ToolFrame): Promise<void> {
  let result: string;
  try {
    result = await runTool(frame.name, frame.args);
  } catch (e) {
    result = `ERROR: ${e instanceof Error ? e.message : String(e)}`;
  }
  try {
    await srv.req('POST', '/tool-result', {id: frame.id, result});
  } catch {
    /* URLError parity: any HTTP/network error on the result POST is swallowed. */
  }
}
