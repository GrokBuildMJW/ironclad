/**
 * Phase-d session handshake (INK-SESSION, #503) — hermetic, no server. A fake Server records the
 * lifecycle calls the helper makes so we can prove: the sealed profile opens + closes a session (the
 * original bug was ZERO callers), heartbeats/reopens stay serial and cannot survive stop(), the open/token
 * profile is a no-op, and every failure path is fail-soft.
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
  openWait?: (call: number) => Promise<void> | void;
  heartbeat?: () => Promise<boolean> | boolean;
  onClose?: (call: number) => void;
}): {srv: Server; calls: Calls; state: {liveSessions: number}} {
  const calls: Calls = {open: 0, heartbeat: 0, close: 0};
  const state = {liveSessions: 0};
  const srv = {
    sessionId: null as string | null,
    async health() {
      if (opts.healthThrows) throw opts.healthThrows;
      return opts.health ?? {};
    },
    async sessionOpen() {
      calls.open++;
      if (opts.openThrows) throw opts.openThrows;
      await opts.openWait?.(calls.open);
      this.sessionId = 'sess-abcdef123456';
      state.liveSessions++;
      return {session_id: this.sessionId};
    },
    async sessionHeartbeat() {
      calls.heartbeat++;
      return (await opts.heartbeat?.()) ?? true;
    },
    async sessionClose() {
      calls.close++;
      this.sessionId = null;
      state.liveSessions = Math.max(0, state.liveSessions - 1);
      opts.onClose?.(calls.close);
    },
  };
  return {srv: srv as unknown as Server, calls, state};
}

function deferred<T>(): {promise: Promise<T>; resolve: (value: T) => void} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return {promise, resolve};
}

async function expectSignal<T>(promise: Promise<T>, label: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`timed out waiting for ${label}`)), 500);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
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

test('establishSession — slow heartbeats are serial and never overlap', async () => {
  const starts = [deferred<void>(), deferred<void>(), deferred<void>()] as const;
  const releases = [deferred<void>(), deferred<void>(), deferred<void>()] as const;
  let inFlight = 0;
  let maxInFlight = 0;
  let heartbeat = 0;
  const {srv} = fakeServer({
    health: {security: {session: true, heartbeat_s: 0.01}},
    async heartbeat() {
      const current = heartbeat++;
      inFlight++;
      maxInFlight = Math.max(maxInFlight, inFlight);
      starts[current]?.resolve(undefined);
      await releases[current]?.promise;
      inFlight--;
      return true;
    },
  });
  const h = await establishSession(srv);

  await expectSignal(starts[0].promise, 'first heartbeat');
  await new Promise((resolve) => setTimeout(resolve, 25)); // longer than the 10ms heartbeat interval
  assert.equal(maxInFlight, 1);
  releases[0].resolve(undefined);
  await expectSignal(starts[1].promise, 'second heartbeat');
  releases[1].resolve(undefined);
  await expectSignal(starts[2].promise, 'third heartbeat');
  releases[2].resolve(undefined);
  await h.stop();

  assert.equal(maxInFlight, 1);
});

test('establishSession — stop closes a session reopened by an already-started tick', async () => {
  const heartbeatStarted = deferred<void>();
  const heartbeatRelease = deferred<void>();
  const reopenStarted = deferred<void>();
  const reopenRelease = deferred<void>();
  const postReopenClose = deferred<void>();
  const {srv, calls, state} = fakeServer({
    health: {security: {session: true, heartbeat_s: 0.01}},
    async heartbeat() {
      heartbeatStarted.resolve(undefined);
      await heartbeatRelease.promise;
      return false;
    },
    async openWait(call) {
      if (call !== 2) return;
      reopenStarted.resolve(undefined);
      await reopenRelease.promise;
    },
    onClose(call) {
      if (call === 2) postReopenClose.resolve(undefined);
    },
  });
  const h = await establishSession(srv);

  await expectSignal(heartbeatStarted.promise, 'heartbeat');
  heartbeatRelease.resolve(undefined);
  await expectSignal(reopenStarted.promise, 'reopen');
  const stopping = h.stop();          // stop() now awaits the in-flight tick (its compensating close)
  reopenRelease.resolve(undefined);   // let the racing re-open complete so the tick can close it
  await stopping;                     // returns only AFTER the reopened session is closed (F1 guarantee)
  await expectSignal(postReopenClose.promise, 'post-reopen close');

  assert.equal(calls.open, 2);
  assert.equal(calls.close, 2);
  assert.equal(state.liveSessions, 0);
  assert.equal(calls.open - calls.close, 0);
});

test('establishSession — a heartbeat resolving after stop does not reopen', async () => {
  const heartbeatStarted = deferred<void>();
  const heartbeatRelease = deferred<void>();
  const heartbeatReturned = deferred<void>();
  const {srv, calls} = fakeServer({
    health: {security: {session: true, heartbeat_s: 0.01}},
    async heartbeat() {
      heartbeatStarted.resolve(undefined);
      await heartbeatRelease.promise;
      heartbeatReturned.resolve(undefined);
      return false;
    },
  });
  const h = await establishSession(srv);

  await expectSignal(heartbeatStarted.promise, 'heartbeat');
  const stopping = h.stop();          // stop() awaits the in-flight heartbeat tick
  heartbeatRelease.resolve(undefined);
  await expectSignal(heartbeatReturned.promise, 'late heartbeat result');
  await stopping;                     // the late heartbeat resolved AFTER stop() → the stopped guard skips reopen
  await new Promise<void>((resolve) => setImmediate(resolve));

  assert.equal(calls.open, 1);
});
