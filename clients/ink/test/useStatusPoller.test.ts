import test from 'node:test';
import assert from 'node:assert/strict';
import {pollStatus, nextConnState, type StatusFields} from '../src/ui/useStatusPoller.js';
import type {Server, Json} from '../src/net/server.js';

function srvOf(health: () => Promise<Json>, tasks: () => Promise<Json[]>): Server {
  return {health, tasks} as unknown as Server;
}

test('pollStatus maps /health + /tasks into status fields', async () => {
  const s = srvOf(
    async () => ({ok: true, model: 'qwen', memory: 'up', watcher: true, autopilot: false}),
    async () => [{status: 'pending'}, {status: 'pending'}, {status: 'done'}],
  );
  const f = await pollStatus(s);
  assert.ok(f);
  assert.equal(f.connected, true);
  assert.equal(f.model, 'qwen');
  assert.equal(f.memory, 'up');
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
  assert.deepEqual(nextConnState(false, up), {connected: true, reconnected: true});   // reconnect → flush
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
