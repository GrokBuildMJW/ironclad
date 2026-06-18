/**
 * Command routing — a VERBATIM port of engine/commands.py:classify (+ HELP_TEXT).
 * input starting with "/" → command (local set handled here; everything else forwarded
 * to the orchestrator with the slash stripped, via /chat/stream). Bare exit/quit → leave.
 * Anything else → a turn. NOTE: no local "/doctor" (parity: commands.py LOCAL_COMMANDS).
 */
export type Kind = 'empty' | 'turn' | 'local' | 'server';

export interface Classified {
  kind: Kind;
  name: string;
  payload: string;
}

/** Handled on THIS (client) side. Matches commands.py LOCAL_COMMANDS exactly. */
export const LOCAL_COMMANDS: ReadonlySet<string> = new Set([
  'tasks', 'pending', 'work', 'auto', 'health', 'help', 'exit', 'quit',
]);

export function classify(line: string): Classified {
  const s = line.trim();
  if (!s) return {kind: 'empty', name: '', payload: ''};
  const low = s.toLowerCase();
  if (low === 'exit' || low === 'quit') return {kind: 'local', name: 'exit', payload: low};
  if (!s.startsWith('/')) return {kind: 'turn', name: '', payload: s};
  const body = s.slice(1).trim();
  if (!body) return {kind: 'empty', name: '', payload: ''};
  const name = (body.split(/\s+/)[0] ?? '').toLowerCase();
  if (LOCAL_COMMANDS.has(name)) return {kind: 'local', name, payload: body};
  return {kind: 'server', name, payload: body};
}

export const HELP_TEXT = `Commands (with a / prefix) — plain text without / is sent to the orchestrator as a turn:

  local (client):
    /help              this help
    /tasks             TaskStore overview
    /pending           staged handovers for local code-agents
    /work              run all open handovers ONCE locally (in parallel)
    /auto on|off       background poller for handovers
    /health            server status
    exit               quit

  orchestrator (server):
    /status            status (model, perf, tasks, tools)
    /config            active configuration
    /clear             clear the orchestrator's context
    /read <path>       read a file in the server workdir
    /ls [path]         list a directory in the server workdir
    /watcher on|off    auto-advance (reconciler)
    /autopilot on|off  autopilot
    /autoplan on|off [N]
    (more: /write, /cat, /log-terminal)`;
