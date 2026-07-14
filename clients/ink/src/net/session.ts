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
 * GET /health; if `security.session` is set, open a session and keep it alive with a serial heartbeat loop
 * (re-open quietly on loss, mirroring `_heartbeat_loop`). Returns a handle whose `stop()` clears the
 * scheduled heartbeat and closes the session. `log` receives the same status lines the Python client prints.
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
  let stopped = false;
  let timer: NodeJS.Timeout | undefined;
  let inFlight: Promise<void> = Promise.resolve(); // the currently-running tick, so stop() can await it

  const schedule = (): void => {
    if (stopped) return;
    timer = setTimeout(() => {
      inFlight = tick();
    }, hb * 1000);
    timer.unref?.(); // the heartbeat must not, by itself, keep the process alive
  };

  const tick = async (): Promise<void> => {
    if (stopped) return;
    try {
      const ok = await srv.sessionHeartbeat();
      if (stopped) return;
      if (!ok) {
        // session lost server-side (restart / expiry) → try to re-open quietly
        await srv.sessionOpen().catch(() => undefined);
        if (stopped) {
          // a re-open that finished AFTER stop() must not leave a live server session (unsealed until TTL)
          await srv.sessionClose().catch(() => undefined);
          return;
        }
      }
    } catch {
      // sessionHeartbeat is contracted fail-soft (returns false, never throws); guard anyway so a surprise
      // error can never permanently kill the heartbeat loop (which would silently stale the session).
      if (stopped) return;
    }
    schedule();
  };

  schedule();
  return {
    active: true,
    async stop() {
      if (stopped) return;
      stopped = true;
      if (timer) clearTimeout(timer);
      // await the in-flight tick FIRST so a re-open racing with stop() runs its stopped-guarded compensating
      // close before we return — the "server ends sealed" guarantee then holds even for a caller that
      // `process.exit()`s right after `await stop()` (it is not left to a natural event-loop drain).
      await inFlight.catch(() => undefined);
      await srv.sessionClose();
    },
  };
}
