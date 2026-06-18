/**
 * Status poller — fetches /health + /tasks every ~2s into state for the footer. Returns
 * [status, setPerf] so the stream turn can push the latest [perf] line into the footer.
 */
import {useEffect, useState} from 'react';
import type {Server} from '../net/server.js';

export interface StatusState {
  model: string;
  connected: boolean;
  watcher: boolean;
  autopilot: boolean;
  pending: number;
  inProgress: number;
  done: number;
  perf: string;
}

export const EMPTY_STATUS: StatusState = {
  model: '—',
  connected: false,
  watcher: false,
  autopilot: false,
  pending: 0,
  inProgress: 0,
  done: 0,
  perf: '',
};

export function useStatusPoller(srv: Server, intervalMs = 2000): [StatusState, (perf: string) => void] {
  const [st, setSt] = useState<StatusState>(EMPTY_STATUS);
  const setPerf = (perf: string): void => setSt((s) => ({...s, perf}));

  useEffect(() => {
    let live = true;
    const poll = async (): Promise<void> => {
      try {
        const h = await srv.health();
        const tasks = await srv.tasks();
        const c = (s: string): number => tasks.filter((t) => t['status'] === s).length;
        if (live) {
          setSt((s) => ({
            ...s,
            connected: Boolean(h['ok']),
            model: String(h['model'] ?? '—'),
            watcher: Boolean(h['watcher']),
            autopilot: Boolean(h['autopilot']),
            pending: c('pending'),
            inProgress: c('in_progress'),
            done: c('done'),
          }));
        }
      } catch {
        if (live) setSt((s) => ({...s, connected: false}));
      }
    };
    void poll();
    const id = setInterval(() => void poll(), intervalMs);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [srv, intervalMs]);

  return [st, setPerf];
}
