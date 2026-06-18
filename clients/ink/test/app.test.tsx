import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToString} from '../src/render/ink-compat.js';
import {App} from '../src/ui/App.js';
import {Server} from '../src/net/server.js';

// Unreachable server → App still renders its header immediately (fetch resolves to the
// "unreachable" note asynchronously); this test only needs the initial frame. Rendered on OUR
// renderer (the client now mounts through ink-compat, not Stock Ink).
const srv = (): Server => new Server('http://127.0.0.1:1', {timeoutMs: 500});

test('App renders the Ironclad header + status footer on the custom renderer', () => {
  const {frame, unmount} = renderToString(<App srv={srv()} codedir="." maxAgents={3} />, 80, 24);
  const f = frame();
  assert.match(f, /Ironclad/, 'brand shown');
  assert.match(f, /model/, 'status footer shown');
  unmount();
});
