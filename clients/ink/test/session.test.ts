/**
 * Phase-d session handshake (INK-SESSION, #503) — hermetic, no server. A fake Server records the
 * lifecycle calls the helper makes so we can prove: the sealed profile opens + closes a session (the
 * pre-fix bug was ZERO callers), the open/token profile is a no-op, and every failure path is fail-soft.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {establishSession} from '../src/net/session.js';
import type {Server} from '../src/net/server.js';

interface Calls {
  open: number;
  heartbeat: number;
  close: number;
}

function fakeServer(opts: {
  health?: Record<string, unknown>;
  healthThrows?: unknown;
  openThrows?: unknown;
}): {srv: Server; calls: Calls} {
  const calls: Calls = {open: 0, heartbeat: 0, close: 0};
  const srv = {
    sessionId: null as string | null,
    async health() {
      if (opts.healthThrows) throw opts.healthThrows;
      return opts.health ?? {};
    },
    async sessionOpen() {
      calls.open++;
      if (opts.openThrows) throw opts.openThrows;
      this.sessionId = 'sess-abcdef123456';
      return {session_id: this.sessionId};
    },
    async sessionHeartbeat() {
      calls.heartbeat++;
      return true;
    },
    async sessionClose() {
      calls.close++;
      this.sessionId = null;
    },
  };
  return {srv: srv as unknown as Server, calls};
}

test('establishSession — open profile (no security.session) is a no-op', async () => {
  const {srv, calls} = fakeServer({health: {security: {}}});
  const h = await establishSession(srv);
  assert.equal(h.active, false);
  assert.equal(calls.open, 0);
  await h.stop(); // idempotent + does not close a session that was never opened
  assert.equal(calls.close, 0);
});

test('establishSession — sealed profile opens a session and closes it on stop()', async () => {
  const logs: string[] = [];
  const {srv, calls} = fakeServer({health: {security: {session: true, heartbeat_s: 30}}});
  const h = await establishSession(srv, (m) => logs.push(m));
  assert.equal(h.active, true);
  assert.equal(calls.open, 1);
  assert.equal((srv as unknown as {sessionId: string | null}).sessionId, 'sess-abcdef123456');
  assert.ok(logs.some((l) => l.includes('session opened')));
  await h.stop();
  assert.equal(calls.close, 1);
  await h.stop(); // idempotent: a second stop does not re-close
  assert.equal(calls.close, 1);
});

test('establishSession — unreachable server is fail-soft (no open, warns)', async () => {
  const logs: string[] = [];
  const {srv, calls} = fakeServer({healthThrows: new Error('ECONNREFUSED')});
  const h = await establishSession(srv, (m) => logs.push(m));
  assert.equal(h.active, false);
  assert.equal(calls.open, 0);
  assert.ok(logs.some((l) => l.includes('unreachable')));
});

test('establishSession — a 401 on open is fail-soft and hints at the token', async () => {
  const logs: string[] = [];
  const err = Object.assign(new Error('unauthorized'), {status: 401});
  const {srv, calls} = fakeServer({health: {security: {session: true}}, openThrows: err});
  const h = await establishSession(srv, (m) => logs.push(m));
  assert.equal(h.active, false); // open failed → no live session, but the client still runs
  assert.equal(calls.open, 1);
  assert.ok(logs.some((l) => l.includes('GX10_SERVER_TOKEN')));
  await h.stop();
  assert.equal(calls.close, 0); // nothing to close
});
