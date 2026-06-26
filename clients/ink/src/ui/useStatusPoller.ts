/**
 * Status poller — fetches /health + /tasks every ~2s into state for the footer. Returns
 * [status, setPerf] so the stream turn can push the latest [perf] line into the footer.
 *
 * §3b resilience:
 *  - **Coalescing:** a failed/missed poll keeps the last-known model/memory/tasks and only flips
 *    `connected:false` (the footer greys out instead of flickering/resetting). `pollStatus`
 *    returns `null` on any error to signal "keep previous state".
 *  - **Reconnect flush:** on a disconnected→connected transition, drain the tool-result retry
 *    buffer (a result dropped during a blip is delivered once the channel is back).
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
export async function pollStatus(srv: Server): Promise<StatusFields | null> {
  try {
    const h = await srv.health();
    const tasks = await srv.tasks();
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

/** Resolve the next connected flag + whether this poll is a reconnect (disconnected→connected),
 *  which is the signal to flush the tool-result buffer. `null` upd = a failed poll → disconnected. */
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
    wasConnected.current = false;
    const poll = async (): Promise<void> => {
      const upd = await pollStatus(srv);
      if (!live) return;
      const {connected, reconnected} = nextConnState(wasConnected.current, upd);
      wasConnected.current = connected;
      // coalesce: on a failed poll keep model/memory/tasks, just drop `connected`.
      setSt((s) => (upd === null ? {...s, connected: false} : {...s, ...upd}));
      if (reconnected) void flushToolResults(srv).catch(() => {}); // channel back → resend buffered results
    };
    void poll();
    const id = setInterval(() => void poll(), intervalMs);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [srv, intervalMs]);

  return [st, setPerf, setAgent, setSearch];
}
