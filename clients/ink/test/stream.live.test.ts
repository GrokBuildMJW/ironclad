/**
 * Live — chatStream streams a real turn from the Spark (GX10_LIVE_URL-gated).
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {Server} from '../src/net/server.js';
import {chatStream, type ToolFrame} from '../src/net/stream.js';

const LIVE = process.env['GX10_LIVE_URL'];

test('live — chatStream streams a turn from /chat/stream', {skip: !LIVE}, async () => {
  const srv = new Server(LIVE as string);
  const chunks: string[] = [];
  const frames: ToolFrame[] = [];
  await chatStream(srv, 'wer bist du? antworte in einem satz.', {
    onText: (s) => chunks.push(s),
    onTool: async (f) => {
      frames.push(f);
      // Phase-1 stub so the server's ToolBridge doesn't stall if a frame appears.
      await srv.req('POST', '/tool-result', {id: f.id, result: 'ERROR: bridge inactive (Phase 1)'});
    },
  });
  const text = chunks.join('');
  assert.ok(text.length > 0, 'received streamed text');
  assert.match(
    text,
    /Assistent|KI|Sprachmodell|Qwen|Ironclad|model/i,
    `unexpected answer: ${text.slice(0, 140)}`,
  );
});
