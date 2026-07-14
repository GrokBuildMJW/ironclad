import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {renderToString} from '../src/render/ink-compat.js';
import {flushToolResults, pendingToolResults, runPassthroughTool} from '../src/tools/bridge.js';
import {pollStatus, nextConnState, useStatusPoller, type StatusFields} from '../src/ui/useStatusPoller.js';
import type {Server, Json} from '../src/net/server.js';

function srvOf(health: () => Promise<Json>, tasks: () => Promise<Json[]>): Server {
  return {health, tasks} as unknown as Server;
}

test('pollStatus maps /health + /tasks into status fields', async () => {
  const s = srvOf(
    async () => ({ok: true, model: 'qwen', memory: 'up', warm: 'down', watcher: true, autopilot: false}),
    async () => [{status: 'pending'}, {status: 'pending'}, {status: 'done'}],
  );
  const f = await pollStatus(s);
  assert.ok(f);
  assert.equal(f.connected, true);
  assert.equal(f.model, 'qwen');
  assert.equal(f.memory, 'up');
  assert.equal(f.warm, 'down');                          // #385: Warm tier mapped separately from Cold
  assert.equal(f.watcher, true);
  assert.equal(f.pending, 2);
  assert.equal(f.done, 1);
});

test('pollStatus returns null on error → caller keeps previous state (coalesce)', async () => {
  const down = srvOf(async () => { throw new Error('health down'); }, async () => []);
  assert.equal(await pollStatus(down), null);
  const tasksDown = srvOf(async () => ({ok: true}), async () => { throw new Error('tasks down'); });
  assert.equal(await pollStatus(tasksDown), null);
});

test('pollStatus defaults memory to off when /health omits it', async () => {
  const s = srvOf(async () => ({ok: true, model: 'm'}), async () => []);
  const f = await pollStatus(s);
  assert.equal(f?.memory, 'off');
});

test('nextConnState detects a reconnect (disconnected→connected)', () => {
  const up = {connected: true} as StatusFields;
  assert.deepEqual(nextConnState(false, up), {connected: true, reconnected: true});   // reconnect edge (flush now keys on `connected`, #1490)
  assert.deepEqual(nextConnState(true, up), {connected: true, reconnected: false});   // stayed up
  assert.deepEqual(nextConnState(true, null), {connected: false, reconnected: false}); // dropped
  assert.deepEqual(nextConnState(false, null), {connected: false, reconnected: false}); // stayed down
});

test('nextConnState: a poll reporting connected:false is not a reconnect', () => {
  assert.deepEqual(nextConnState(false, {connected: false} as StatusFields), {
    connected: false,
    reconnected: false,
  });
});

test('a buffered tool result drains on the next connected poll without a reconnect edge', async () => {
  let healthCalls = 0;
  let postAttempts = 0;
  let failNextPost = false;
  let observedConnected = false;
  const srv = {
    health: async () => {
      healthCalls += 1;
      return {ok: true, model: 'm'};
    },
    tasks: async () => [],
    req: async () => {
      postAttempts += 1;
      if (failNextPost) {
        failNextPost = false;
        throw new Error('temporary network failure');
      }
      return {};
    },
  } as unknown as Server;
  const waitFor = async (predicate: () => boolean): Promise<void> => {
    const deadline = Date.now() + 1000;
    while (!predicate()) {
      if (Date.now() >= deadline) assert.fail('timed out waiting for status poll');
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
  };
  const Probe = (): null => {
    const [status] = useStatusPoller(srv, 25);
    observedConnected = status.connected;
    return null;
  };
  const mounted = renderToString(React.createElement(Probe), 20, 1);
  try {
    await waitFor(() => observedConnected); // initial poll completed: connected + reconnect edge
    failNextPost = true;
    await runPassthroughTool(srv, {id: 'buffered', name: 'totally_unknown', args: {}});
    assert.equal(pendingToolResults(), 1);
    await waitFor(() => healthCalls >= 2 && pendingToolResults() === 0);
    assert.equal(postAttempts, 2, 'the still-connected poll retried the buffered result');
  } finally {
    mounted.unmount();
    await flushToolResults(srv);
  }
});
