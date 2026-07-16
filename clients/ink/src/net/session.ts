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

// #1539: a stuck heartbeat/close must never wait out the full 600s request timeout on shutdown. On stop()
// the in-flight heartbeat is aborted immediately, and the closing request is bounded to this short deadline
// (a black-hole server then leaves its session to expire at the server-side TTL rather than hanging the CLI).
const SHUTDOWN_CLOSE_MS = 3000;

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
  // #1539: aborts a still-in-flight heartbeat/re-open when stop() runs, so the tick can never block shutdown
  // for the full request timeout. The closing requests use a fresh short deadline instead (see stop()).
  const ac = new AbortController();

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
      const ok = await srv.sessionHeartbeat(ac.signal);
      if (stopped) return;
      if (!ok) {
        // session lost server-side (restart / expiry) → try to re-open quietly
        await srv.sessionOpen(ac.signal).catch(() => undefined);
        if (stopped) {
          // a re-open that finished AFTER stop() must not leave a live server session (unsealed until TTL)
          await srv.sessionClose(AbortSignal.timeout(SHUTDOWN_CLOSE_MS)).catch(() => undefined);
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
      // #1539: abort any in-flight heartbeat/re-open so shutdown is not held for the full 600s request
      // timeout when the server is a black hole (the pending fetch socket would keep the Node process alive
      // long after the UI is gone). Then await the (now promptly-settling) tick FIRST so a re-open racing
      // with stop() runs its stopped-guarded compensating close before we return — the "server ends sealed"
      // guarantee still holds for a caller that `process.exit()`s right after `await stop()`.
      ac.abort();
      await inFlight.catch(() => undefined);
      // Close on a fresh SHORT deadline (not the aborted ac.signal): attempt the close but never block
      // shutdown beyond SHUTDOWN_CLOSE_MS; an unclosable session expires at the server-side TTL.
      await srv.sessionClose(AbortSignal.timeout(SHUTDOWN_CLOSE_MS)).catch(() => undefined);
    },
  };
}
