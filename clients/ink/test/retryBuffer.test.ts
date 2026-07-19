import test from 'node:test';
import assert from 'node:assert/strict';
import {ToolResultBuffer} from '../src/net/retryBuffer.js';
import {HttpError, type Server} from '../src/net/server.js';

// A minimal stand-in for Server.req — records POST bodies and fails per `mode`.
class FakeSrv {
  calls: Array<{id: string; result: string}> = [];
  mode: 'ok' | 'transient' | 'http' = 'ok';
  status = 500;
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  async req(_m: string, _p: string, body?: Record<string, unknown>): Promise<Record<string, unknown>> {
    this.calls.push({id: String(body?.['id']), result: String(body?.['result'])});
    if (this.mode === 'transient') throw new Error('ECONNREFUSED');
    if (this.mode === 'http') throw new HttpError(this.status, 'x');
    return {};
  }
}
const asSrv = (f: FakeSrv): Server => f as unknown as Server;

test('send: success does not buffer', async () => {
  const b = new ToolResultBuffer();
  const s = new FakeSrv();
  await b.send(asSrv(s), {id: '1', result: 'r'});
  assert.equal(b.size, 0);
  assert.equal(s.calls.length, 1);
});

test('transient failure buffers, then flush resends on recovery', async () => {
  const b = new ToolResultBuffer();
  const s = new FakeSrv();
  s.mode = 'transient';
  await b.send(asSrv(s), {id: '1', result: 'r'});
  assert.equal(b.size, 1); // buffered, not lost
  s.mode = 'ok';
  s.calls = [];
  await b.flush(asSrv(s));
  assert.equal(b.size, 0); // delivered
  assert.deepEqual(s.calls, [{id: '1', result: 'r'}]);
});

test('permanent 4xx (e.g. 410 Gone) is dropped, not buffered', async () => {
  const b = new ToolResultBuffer();
  const s = new FakeSrv();
  s.mode = 'http';
  s.status = 410;
  await b.send(asSrv(s), {id: '1', result: 'r'});
  assert.equal(b.size, 0); // stale result → dropped, never retried
});

test('5xx is transient (buffered), 4xx is permanent (dropped)', async () => {
  const b = new ToolResultBuffer();
  const s = new FakeSrv();
  s.mode = 'http';
  s.status = 503;
  await b.send(asSrv(s), {id: '1', result: 'r'});
  assert.equal(b.size, 1); // 5xx → buffered
  s.status = 400;
  await b.flush(asSrv(s)); // first item now 400 → permanent → dropped
  assert.equal(b.size, 0);
});

test('401 (recoverable mid-turn session expiry) is transient — retained, then delivered on recovery (#1573)', async () => {
  const b = new ToolResultBuffer();
  const s = new FakeSrv();
  s.mode = 'http';
  s.status = 401;
  await b.send(asSrv(s), {id: '1', result: 'r'});
  assert.equal(b.size, 1); // 401 → transient (a sealed session expired mid-turn) → BUFFERED, not dropped
  s.mode = 'ok';           // the poller/heartbeat reopens the session
  await b.flush(asSrv(s));
  assert.equal(b.size, 0); // the retained tool result now delivers with the fresh X-Session-Id
});

test('403 is transient like 401 (#1573)', async () => {
  const b = new ToolResultBuffer();
  const s = new FakeSrv();
  s.mode = 'http';
  s.status = 403;
  await b.send(asSrv(s), {id: '1', result: 'r'});
  assert.equal(b.size, 1); // 403 is also a recoverable session-auth failure → buffered
});

test('send drains the buffer first, preserving order', async () => {
  const b = new ToolResultBuffer();
  const s = new FakeSrv();
  s.mode = 'transient';
  await b.send(asSrv(s), {id: '1', result: 'a'}); // buffered
  assert.equal(b.size, 1);
  s.mode = 'ok';
  s.calls = [];
  await b.send(asSrv(s), {id: '2', result: 'b'}); // drains #1, then sends #2
  assert.equal(b.size, 0);
  assert.deepEqual(s.calls.map((c) => c.id), ['1', '2']); // older result first
});

test('buffer is bounded — overflow drops the oldest', async () => {
  const b = new ToolResultBuffer(2);
  const s = new FakeSrv();
  s.mode = 'transient';
  await b.send(asSrv(s), {id: '1', result: 'a'});
  await b.send(asSrv(s), {id: '2', result: 'b'});
  await b.send(asSrv(s), {id: '3', result: 'c'}); // > max(2) → oldest (#1) dropped
  assert.equal(b.size, 2);
  s.mode = 'ok';
  s.calls = [];
  await b.flush(asSrv(s));
  assert.deepEqual(s.calls.map((c) => c.id), ['2', '3']);
});

test('flush stops at the first still-transient item (keeps the rest in order)', async () => {
  const b = new ToolResultBuffer();
  const s = new FakeSrv();
  s.mode = 'transient';
  await b.send(asSrv(s), {id: '1', result: 'a'});
  await b.send(asSrv(s), {id: '2', result: 'b'});
  assert.equal(b.size, 2);
  await b.flush(asSrv(s)); // still down → nothing delivered, order kept
  assert.equal(b.size, 2);
});

test('#1490 concurrent flushes never drop an unsent result (re-entrancy guard)', async () => {
  // #1490 flushes on EVERY connected poll, so a pending flush can overlap the next poll's flush. Without the
  // guard, both read q[0] then both shift() — the 2nd shift removes an item that was never sent. The deferred
  // request forces that overlap without depending on a wall-clock delay.
  let markStarted!: () => void;
  let release!: () => void;
  const started = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  class SlowSrv {
    calls: Array<{id: string; result: string}> = [];
    async req(_m: string, _p: string, body?: Record<string, unknown>): Promise<Record<string, unknown>> {
      markStarted();
      await pending;
      this.calls.push({id: String(body?.['id']), result: String(body?.['result'])});
      return {};
    }
  }
  const b = new ToolResultBuffer();
  const down = new FakeSrv();
  down.mode = 'transient';
  await b.send(asSrv(down), {id: '1', result: 'a'}); // buffered
  await b.send(asSrv(down), {id: '2', result: 'b'}); // buffered
  assert.equal(b.size, 2);

  const slow = new SlowSrv();
  const srv = slow as unknown as Server;
  const first = b.flush(srv);
  await started;
  const second = b.flush(srv);
  release();
  await Promise.all([first, second]);

  assert.equal(b.size, 0, 'both results delivered, none dropped');
  assert.deepEqual(slow.calls, [{id: '1', result: 'a'}, {id: '2', result: 'b'}],
    'each result sent exactly once, in order (the 2nd flush no-op\'d instead of racing shift())');
});
