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

/** A known slash command (MEM-16: single source of truth — drives classify, help, autocomplete). */
export interface Command {
  name: string;
  scope: 'local' | 'server';
  usage?: string; // e.g. "on|off", "<path>"
  desc: string;
}

/** The command registry. `!<cmd>` (local shell, MEM-15) is separate — not a slash command. */
export const COMMANDS: readonly Command[] = [
  // local (handled client-side)
  {name: 'help', scope: 'local', desc: 'this help'},
  {name: 'tasks', scope: 'local', desc: 'TaskStore overview'},
  {name: 'pending', scope: 'local', desc: 'staged handovers for local code-agents'},
  {name: 'work', scope: 'local', desc: 'run all open handovers ONCE locally (in parallel)'},
  {name: 'auto', scope: 'local', usage: 'on|off', desc: 'background poller for handovers'},
  {name: 'health', scope: 'local', desc: 'server status'},
  {name: 'reset', scope: 'local', desc: 'start clean — transcript + server context + summary (keeps long-term memory)'},
  {name: 'resume', scope: 'local', desc: 'restore the previous session (default start is fresh; or --resume)'},
  {name: 'update', scope: 'local', usage: '[pull]', desc: 'rebuild + reinstall the client from source (GX10_SRC), then restart'},
  {name: 'exit', scope: 'local', desc: 'quit'},
  {name: 'quit', scope: 'local', desc: 'quit'},
  // orchestrator (forwarded to the server)
  {name: 'status', scope: 'server', desc: 'status (model, perf, tasks, tools)'},
  {name: 'config', scope: 'server', desc: 'active configuration'},
  {name: 'clear', scope: 'server', desc: "clear the orchestrator's context"},
  {name: 'context', scope: 'server', desc: 'show injected summary + retrieved block (diagnose)'},
  {name: 'rag', scope: 'server', usage: 'on|off', desc: 'toggle per-turn retrieval'},
  {name: 'read', scope: 'server', usage: '<path>', desc: 'read a file in the server workdir'},
  {name: 'ls', scope: 'server', usage: '[path]', desc: 'list a directory in the server workdir'},
  {name: 'write', scope: 'server', usage: '<path>', desc: 'write the last response to a file'},
  {name: 'cat', scope: 'server', usage: '<path>', desc: 'show a file in the server workdir'},
  {name: 'watcher', scope: 'server', usage: 'on|off', desc: 'auto-advance (reconciler)'},
  {name: 'autopilot', scope: 'server', usage: 'on|off', desc: 'autopilot'},
  {name: 'autoplan', scope: 'server', usage: 'on|off [N]', desc: 'auto-plan the next tasks'},
  {name: 'log-terminal', scope: 'server', usage: 'on|off', desc: 'live autopilot log window'},
  {name: 'doctor', scope: 'server', desc: 'read-only preflight report'},
];

/** Handled on THIS (client) side — derived from the registry (no duplication). */
export const LOCAL_COMMANDS: ReadonlySet<string> = new Set(
  COMMANDS.filter((c) => c.scope === 'local').map((c) => c.name),
);

/** Autocomplete (MEM-16): commands whose name starts with `prefix` (prefix WITHOUT the leading
 *  '/'; empty → all). Drives the slash suggestion overlay. */
export function completions(prefix: string): readonly Command[] {
  const p = prefix.trim().toLowerCase();
  return COMMANDS.filter((c) => c.name.startsWith(p));
}

export function classify(line: string): Classified {
  const s = line.trim();
  if (!s) return {kind: 'empty', name: '', payload: ''};
  const low = s.toLowerCase();
  if (low === 'exit' || low === 'quit') return {kind: 'local', name: 'exit', payload: low};
  // MEM-15: `!cmd` → run a shell command LOCALLY (no orchestrator turn). payload = the command.
  if (s.startsWith('!')) {
    const cmd = s.slice(1).trim();
    return cmd ? {kind: 'local', name: 'sh', payload: cmd} : {kind: 'empty', name: '', payload: ''};
  }
  if (!s.startsWith('/')) return {kind: 'turn', name: '', payload: s};
  const body = s.slice(1).trim();
  if (!body) return {kind: 'empty', name: '', payload: ''};
  const name = (body.split(/\s+/)[0] ?? '').toLowerCase();
  if (LOCAL_COMMANDS.has(name)) return {kind: 'local', name, payload: body};
  return {kind: 'server', name, payload: body};
}

/** Render one command as a help line: "/name usage   desc". */
function _helpLine(c: Command): string {
  const lhs = `/${c.name}${c.usage ? ' ' + c.usage : ''}`;
  return `    ${lhs.padEnd(20)} ${c.desc}`;
}

/** Generated from COMMANDS (MEM-16) — no hand-maintained duplicate of the command list. */
export const HELP_TEXT = [
  'Commands (with a / prefix) — plain text without / is sent to the orchestrator as a turn.',
  '  !<cmd>  runs a shell command LOCALLY (PowerShell on Windows, in the codedir), e.g. !git status',
  '',
  '  local (client):',
  ...COMMANDS.filter((c) => c.scope === 'local').map(_helpLine),
  '',
  '  orchestrator (server):',
  ...COMMANDS.filter((c) => c.scope === 'server').map(_helpLine),
].join('\n');
