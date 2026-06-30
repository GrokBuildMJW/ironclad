/**
 * Phase-d session handshake — a port of engine/client.py:_establish_session (+ _heartbeat_loop).
 *
 * Under the `sealed` trust profile EVERY gated route requires an `X-Session-Id` header, so the client
 * must open a session before its first turn and keep it alive — otherwise each turn AND the 2s status
 * poll 401 (INK-SESSION, #503). On the `open`/`token` profile no session is needed and this is a no-op.
 * Fail-soft throughout: an unreachable server or a failed open returns a no-op handle (the UI surfaces
 * per-turn errors, same as the Python client) — it never throws.
 */
import type {Server} from './server.js';

export interface SessionHandle {
  /** True iff a Phase-d session was actually opened (sealed profile). */
  readonly active: boolean;
  /** Stop the heartbeat and close the session. Idempotent + fail-soft. */
  stop(): Promise<void>;
}

const NOOP: SessionHandle = {active: false, async stop() {}};

/**
 * GET /health; if `security.session` is set, open a session and keep it alive with a heartbeat interval
 * (re-open quietly on loss, mirroring `_heartbeat_loop`). Returns a handle whose `stop()` clears the
 * interval and closes the session. `log` receives the same status lines the Python client prints.
 */
export async function establishSession(
  srv: Server,
  log: (msg: string) => void = () => {},
): Promise<SessionHandle> {
  let health: Record<string, unknown>;
  try {
    health = await srv.health();
  } catch (e) {
    log(`  ⚠ server unreachable for handshake: ${String((e as Error).message ?? e)}`);
    return NOOP;
  }
  const sec = (health['security'] as Record<string, unknown> | undefined) ?? {};
  if (!sec['session']) return NOOP;
  const hb = Number(sec['heartbeat_s']) || 30; // seconds; default mirrors the Python client
  try {
    const res = await srv.sessionOpen();
    log(`  ✓ session opened (${String(res['session_id'] ?? '?').slice(0, 8)}…, heartbeat ${hb}s)`);
  } catch (e) {
    const status = (e as {status?: number}).status;
    const hint = status === 401 ? " — set GX10_SERVER_TOKEN to the server's deployment secret" : '';
    log(`  ✗ could not open a session${status ? ` (HTTP ${status})` : ''}${hint}`);
    return NOOP;
  }
  const timer = setInterval(() => {
    void srv.sessionHeartbeat().then((ok) => {
      // session lost server-side (restart / expiry) → try to re-open quietly
      if (!ok) void srv.sessionOpen().catch(() => undefined);
    });
  }, hb * 1000);
  timer.unref?.(); // the heartbeat must not, by itself, keep the process alive
  let stopped = false;
  return {
    active: true,
    async stop() {
      if (stopped) return;
      stopped = true;
      clearInterval(timer);
      await srv.sessionClose();
    },
  };
}
