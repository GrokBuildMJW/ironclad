/**
 * Streaming turn — a byte-exact port of engine/client.py:Server.chat_stream.
 *
 * POST /chat/stream with X-Local-Tools:1 + the auth/session headers; the response is plain
 * UTF-8 text interleaved with tool-bridge control frames. A SINGLE NUL (\x00) toggles
 * between text and frame mode (frame payload = "TR" + json{id,name,args}). Decoded
 * incrementally so a multi-byte char or a small frame split across reads is handled.
 *
 * The tool execution itself is injected (onTool): Phase 1 stubs it; Phase 2 wires the real
 * local tool-bridge (run the tool on the local fs, POST the result to /tool-result).
 */
import {HttpError, type Server} from './server.js';

const NUL = String.fromCharCode(0); // the \x00 frame delimiter (no raw null byte in source)

export interface ToolFrame {
  id: string;
  name: string;
  args: Record<string, unknown>;
  execCwd?: string; // #1317: server-shipped active-project exec cwd (mount) — where the bridged tool runs
}

export interface StreamHandlers {
  onText: (chunk: string) => void;
  onTool?: (frame: ToolFrame) => void | Promise<void>;
}

async function dispatchFrame(seg: string, onTool?: StreamHandlers['onTool']): Promise<void> {
  const json = seg.startsWith('TR') ? seg.slice(2) : seg;
  let payload: {id?: string; name?: string; args?: Record<string, unknown>; exec_cwd?: string};
  try {
    payload = JSON.parse(json) as typeof payload;
  } catch {
    return; // malformed frame — drop, never break the stream (parity with client.py)
  }
  if (!payload.id || !payload.name) return;
  if (onTool) {
    // #1317: carry the server-shipped exec cwd through, but only add the field when present so a frame
    // without it stays byte-identical to {id, name, args} (parity with the pre-#1317 stream).
    const toolFrame: ToolFrame = {id: payload.id, name: payload.name, args: payload.args ?? {}};
    if (payload.exec_cwd) toolFrame.execCwd = payload.exec_cwd;
    await onTool(toolFrame);
  }
}

/** A pure, testable feeder: push byte chunks; push `null` to flush at end-of-stream. */
export function createStreamParser(h: StreamHandlers): (chunk: Uint8Array | null) => Promise<void> {
  const dec = new TextDecoder('utf-8');
  let buf = '';
  let expectingFrame = false;
  return async (chunk: Uint8Array | null): Promise<void> => {
    if (chunk === null) {
      buf += dec.decode();
      if (buf && !expectingFrame) h.onText(buf);
      buf = '';
      return;
    }
    buf += dec.decode(chunk, {stream: true});
    let i: number;
    while ((i = buf.indexOf(NUL)) !== -1) {
      const seg = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (expectingFrame) await dispatchFrame(seg, h.onTool);
      else if (seg) h.onText(seg);
      expectingFrame = !expectingFrame;
    }
  };
}

// #954/#955: the server-side structured guided-input contract, rendered field-by-field by the client.
export interface GuideField {name: string; required: boolean; choices: string[]; default: string; type: string}
export interface NeedsGuide {command: string; subcommands: string[]; fields: GuideField[]; usage: string; canonical_echo: string}
export type ChatStreamReply =
  | {needs_confirm?: {command: string; tier: string; reason: string}; needs_guide?: NeedsGuide}
  | undefined;

/** #935/#1281: `--yes`/`--confirm` is the operator's confirmation for a destructive command — a standalone
 *  token in ANY position (not only trailing; `--yes --purge` slipped past the old `$`-anchored regex).
 *  Returns the message without the flag + whether it was present. */
export function stripConfirm(message: string): {msg: string; confirm: boolean} {
  const toks = message.split(/\s+/).filter((t) => t.length > 0);
  const kept = toks.filter((t) => t !== '--yes' && t !== '--confirm');
  return kept.length !== toks.length ? {msg: kept.join(' '), confirm: true} : {msg: message, confirm: false};
}

export async function chatStream(
  srv: Server,
  message: string,
  h: StreamHandlers,
  signal?: AbortSignal,
): Promise<ChatStreamReply> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Local-Tools': '1',
    ...srv.headers(),
  };
  // #935: uniform confirm affordance — a trailing `--yes`/`--confirm` on a destructive command IS the
  // confirmation (stripped here, sent as confirm=true). Keeps the flow input-free.
  // #935/#1281: `--yes`/`--confirm` (in ANY position) is the destructive-command confirmation.
  const {msg, confirm} = stripConfirm(message);
  // Combine the caller's cancel signal (Esc / Ctrl+C) with the request timeout, so aborting a turn
  // tears down the fetch + reader IMMEDIATELY instead of waiting on the server to stop generating.
  const timeout = AbortSignal.timeout(srv.timeoutMs);
  const sig = signal ? AbortSignal.any([signal, timeout]) : timeout;
  const res = await fetch(srv.base + '/chat/stream', {
    method: 'POST',
    headers,
    body: JSON.stringify({message: msg, confirm}),
    signal: sig,
  });
  if (!res.ok) throw new HttpError(res.status, `POST /chat/stream → HTTP ${res.status}`);
  // #935/#954: a destructive command → JSON {needs_confirm}; an explicit ?/--guide → JSON {needs_guide};
  // either way the server replies with JSON instead of a stream.
  if (res.headers.get('content-type')?.includes('application/json')) {
    return (await res.json()) as ChatStreamReply;
  }
  if (!res.body) throw new HttpError(0, 'POST /chat/stream → no response body');
  const feed = createStreamParser(h);
  const reader = res.body.getReader();
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    if (value) await feed(value);
  }
  await feed(null);
}
