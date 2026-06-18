import test from 'node:test';
import assert from 'node:assert/strict';
import {runPassthroughTool} from '../src/tools/bridge.js';
import {HttpError, type Server, type Json} from '../src/net/server.js';
import type {ToolFrame} from '../src/net/stream.js';

/** Minimal Server stub exposing only `req` (the only method runPassthroughTool touches). */
function fakeServer(reqImpl: (method: string, path: string, body?: Json) => Promise<Json>): Server {
  return {req: reqImpl} as unknown as Server;
}

test('runs the tool and posts {id, result} to /tool-result', async () => {
  const posted: Array<{method: string; path: string; body?: Json}> = [];
  const srv = fakeServer(async (method, path, body) => {
    posted.push({method, path, body});
    return {};
  });
  // "totally_unknown" is a deterministic tool path (no fs needed): runTool → ERROR string.
  const frame: ToolFrame = {id: 'abc', name: 'totally_unknown', args: {}};
  await runPassthroughTool(srv, frame);
  assert.equal(posted.length, 1);
  assert.equal(posted[0]?.method, 'POST');
  assert.equal(posted[0]?.path, '/tool-result');
  assert.deepEqual(posted[0]?.body, {id: 'abc', result: 'ERROR: Unknown tool: totally_unknown'});
});

test('swallows a 410 Gone (HttpError) on the result POST — never throws', async () => {
  const srv = fakeServer(async () => {
    throw new HttpError(410, 'gone');
  });
  await runPassthroughTool(srv, {id: 'x', name: 'totally_unknown', args: {}});
  assert.ok(true, 'returned without throwing');
});

test('swallows a network error on the result POST — never throws', async () => {
  const srv = fakeServer(async () => {
    throw new Error('ECONNREFUSED');
  });
  await runPassthroughTool(srv, {id: 'y', name: 'totally_unknown', args: {}});
  assert.ok(true, 'returned without throwing');
});
