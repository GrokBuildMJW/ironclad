/**
 * Status poller — fetches /health + /tasks every ~2s into state for the footer. Returns
 * [status, setPerf] so the stream turn can push the latest [perf] line into the footer.
 *
 * §3b resilience:
 *  - **Coalescing:** a failed/missed poll keeps the last-known model/memory/tasks and only flips
 *    `connected:false` (the footer greys out instead of flickering/resetting). `pollStatus`
 *    returns `null` on any error to signal "keep previous state".
 *  - **Connected flush:** on every connected poll, drain the tool-result retry buffer (a result
 *    dropped during a blip is delivered within one poll interval even without a reconnect edge).
 */
import {useEffect, useRef, useState} from 'react';
import {flushToolResults} from '../tools/bridge.js';
import type {Server} from '../net/server.js';

export interface StatusState {
  model: string;
  connected: boolean;
  memory: string; // /health.memory (Cold/Mem0): 'up' (reachable) · 'down' (configured, unreachable) · 'off'
  warm: string; // #385 /health.warm (Warm/Valkey): 'up' · 'down' (configured, unreachable) · 'off' (none)
  watcher: boolean;
  autopilot: boolean;
  pending: number;
  inProgress: number;
  done: number;
  perf: string;
  agent: string; // #453: which coder was last routed (pushed from the stream, like perf)
  search: string; // epic #505 S9: last web-search summary (n + ms), pushed from the stream
}

export const EMPTY_STATUS: StatusState = {
  model: '—',
  connected: false,
  memory: 'off',
  warm: 'off',
  watcher: false,
  autopilot: false,
  pending: 0,
  inProgress: 0,
  done: 0,
  perf: '',
  agent: '',
  search: '',
};

/** The status fields derived from one /health + /tasks fetch (perf + agent + search are pushed
 *  separately from the stream, not polled). */
export type StatusFields = Omit<StatusState, 'perf' | 'agent' | 'search'>;

/** One poll: map /health + /tasks into status fields, or `null` on ANY error (→ caller keeps the
 *  previous state and only marks it disconnected — the coalescing guarantee). Never throws. */
export async function pollStatus(srv: Server, signal?: AbortSignal): Promise<StatusFields | null> {
  try {
    const h = await srv.health(signal);
    const tasks = await srv.tasks(signal);
    const c = (s: string): number => tasks.filter((t) => t['status'] === s).length;
    return {
      connected: Boolean(h['ok']),
      model: String(h['model'] ?? '—'),
      memory: String(h['memory'] ?? 'off'),
      warm: String(h['warm'] ?? 'off'),
      watcher: Boolean(h['watcher']),
      autopilot: Boolean(h['autopilot']),
      pending: c('pending'),
      inProgress: c('in_progress'),
      done: c('done'),
    };
  } catch {
    return null;
  }
}

/** Resolve the next connected flag + whether this poll is a reconnect (disconnected→connected).
 *  `null` upd = a failed poll → disconnected. */
export function nextConnState(prev: boolean, upd: StatusFields | null): {connected: boolean; reconnected: boolean} {
  const now = upd === null ? false : Boolean(upd.connected);
  return {connected: now, reconnected: now && !prev};
}

export function useStatusPoller(
  srv: Server,
  intervalMs = 2000,
): [StatusState, (perf: string) => void, (agent: string) => void, (search: string) => void] {
  const [st, setSt] = useState<StatusState>(EMPTY_STATUS);
  const wasConnected = useRef(false);
  const setPerf = (perf: string): void => setSt((s) => ({...s, perf}));
  const setAgent = (agent: string): void => setSt((s) => ({...s, agent})); // #453
  const setSearch = (search: string): void => setSt((s) => ({...s, search})); // epic #505 S9

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // #1542: aborts the in-flight health/tasks fetch on teardown so a poll stuck against a black-hole server
    // never lingers on its 600s request timeout, retaining a socket after the component is gone.
    const ac = new AbortController();
    wasConnected.current = false;
    const poll = async (): Promise<void> => {
      const upd = await pollStatus(srv, ac.signal);
      if (!live) return;
      const {connected} = nextConnState(wasConnected.current, upd);
      wasConnected.current = connected;
      // coalesce: on a failed poll keep model/memory/tasks, just drop `connected`.
      setSt((s) => (upd === null ? {...s, connected: false} : {...s, ...upd}));
      if (connected) void flushToolResults(srv).catch(() => {}); // channel up → drain buffered results; the 2s poll is the bounded retry timer
    };
    // #1542: a SELF-SCHEDULING loop — the next poll is armed only AFTER the current one settles, so at most
    // one health/tasks request is ever in flight and completions can never land out of order. (The old fixed
    // `setInterval` fired every intervalMs regardless, so a slow server that pended each request for the 600s
    // timeout accreted hundreds of simultaneous fetches/sockets, and a late result could clobber newer state.)
    const tick = async (): Promise<void> => {
      await poll();
      if (!live) return;
      timer = setTimeout(() => void tick(), intervalMs);
    };
    void tick();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
      ac.abort();
    };
  }, [srv, intervalMs]);

  return [st, setPerf, setAgent, setSearch];
}
