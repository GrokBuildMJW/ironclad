/**
 * Command routing — a VERBATIM port of engine/commands.py:classify (+ HELP_TEXT).
 * input starting with "/" → command (local set handled here; everything else forwarded
 * to the orchestrator with the slash stripped, via /chat/stream). Bare exit/quit → leave.
 * Anything else → a turn. /doctor is LOCAL (GET /doctor, mirrors /health) — parity: commands.py LOCAL_COMMANDS.
 */
export type Kind = 'empty' | 'turn' | 'local' | 'server' | 'suggest';

export interface Classified {
  kind: Kind;
  name: string;
  payload: string;
}

/** A known slash command (MEM-16: single source of truth — drives classify, help, autocomplete). */
export interface FlagInfo {
  name: string;
  required: boolean;
  choices: string[];
  summary: string;
}

export interface Command {
  name: string;
  scope: 'local' | 'server';
  usage?: string; // e.g. "on|off", "<path>"
  desc: string;
  subcommands?: string[]; // #937: from /catalogue (#936) — powers argument autocomplete
  flags?: FlagInfo[]; //     #937: from /catalogue (#936) — flag names + choices for argument autocomplete
  arg?: boolean; //          #937: this completion inserts an argument token, not a `/verb` (see completionText)
  hidden?: boolean; //       #1264: a deprecated verb — still dispatchable if typed, but never advertised in autocomplete
}

/** The command registry. `!<cmd>` (local shell, MEM-15) is separate — not a slash command. */
export const COMMANDS: readonly Command[] = [
  // local (handled client-side)
  {name: 'help', scope: 'local', desc: 'this help'},
  {name: 'tasks', scope: 'local', desc: 'TaskStore overview'},
  {name: 'pending', scope: 'local', desc: 'staged handovers for local code-agents'},
  {name: 'coders', scope: 'local', usage: '[use <id>|auto]', desc: 'which coding agents are bound/active (+ pin one at runtime)'},
  {name: 'work', scope: 'local', desc: 'run all open handovers ONCE locally (in parallel)'},
  {name: 'auto', scope: 'local', usage: 'on [N]|off', desc: 'full automation on/off — engine loop (watcher+autopilot+continuation) + local coder poller'},
  {name: 'health', scope: 'local', desc: 'server status'},
  {name: 'doctor', scope: 'local', desc: 'read-only preflight report (GET /doctor)'},
  {name: 'tool', scope: 'server', usage: '<name> <args|text>', desc: 'run a tool directly/deterministic, e.g. tool mpr_research <question>'},
  {name: 'reset', scope: 'local', desc: 'start clean — transcript + server context + summary (keeps long-term memory)'},
  {name: 'resume', scope: 'local', desc: 'restore the previous session (default start is fresh; or --resume)'},
  {name: 'update', scope: 'local', usage: '[pull]', desc: 'rebuild + reinstall the client from source (GX10_SRC), then restart'},
  {name: 'exit', scope: 'local', desc: 'quit'},
  {name: 'quit', scope: 'local', desc: 'quit'},
  // orchestrator (forwarded to the server)
  {name: 'status', scope: 'server', desc: 'status (model, perf, tasks, tools)'},
  {name: 'prompts', scope: 'server', desc: 'list the loaded prompt-library items (name, languages, description)'},
  {name: 'skills', scope: 'server', desc: 'list the loaded skills (playbooks + typed tools, incl. MPR)'},
  {name: 'config', scope: 'server', desc: 'active configuration'},
  {name: 'clear', scope: 'server', desc: "clear the orchestrator's context"},
  {name: 'context', scope: 'server', desc: 'show injected summary + retrieved block (diagnose)'},
  {name: 'rag', scope: 'server', usage: 'on|off', desc: 'toggle per-turn retrieval'},
  {name: 'read', scope: 'server', usage: '<file>', desc: 'read a file in the server workdir'},
  {name: 'ls', scope: 'server', usage: '[path]', desc: 'list a directory in the server workdir'},
  {name: 'write', scope: 'server', usage: '<path>', desc: 'write the last response to a file'},
  {name: 'cat', scope: 'server', usage: '<path>', desc: 'show a file in the server workdir'},
  {name: 'watcher', scope: 'server', usage: 'on|off', desc: 'deprecated alias for /auto on|off'},
  {name: 'autopilot', scope: 'server', usage: 'on|off', desc: 'autopilot'},
  {name: 'autoplan', scope: 'server', usage: 'on|off [N]', desc: 'auto-plan the next tasks'},
  {name: 'log-terminal', scope: 'server', usage: 'on|off', desc: 'live autopilot log window'},
  {name: 'initiative', scope: 'server', usage: 'new <name> | list | use <slug> | active | reconcile', desc: 'manage the initiative-centric vault (a deprecated alias for /project)', hidden: true},
  {name: 'project', scope: 'server', usage: 'list [--all] | new <name> [--path <dir>] | use <slug> | active | track new|use|list | delete <id> [--purge] | archive|unarchive <id>', desc: 'manage isolated projects (the guided setup command; /initiative is a deprecated alias)'},
  {name: 'switch', scope: 'server', usage: '<project_id>', desc: 'rebind the engine to a project (own paths + memory partition)'},
  {name: 'design', scope: 'server', usage: '--options [N]', desc: 'ask for 2..8 design proposal variants with pros/cons (default 2)'},
  {name: 'approve', scope: 'server', usage: 'design [<id>]', desc: "approve a design (bare /approve or /approve design [<proposal-id>])"},
  {name: 'board', scope: 'server', usage: '[slug]', desc: 'render the task board (pending/in_progress/done) to BOARD.md and show it'},
  {name: 'generate', scope: 'server', usage: '--domain <d> --case <c> --description <text> [--kind case|prompt] [--phase MVP|V1|V2|V3|out-of-scope] [--tier high|medium|low]', desc: 'scaffold a paved-road capability into the active project library'},
  // #952: complete the server-verb subset so it covers command_spec (guarded by check_ink_command_parity.py).
  // Missing these three permanently blinded the did-you-mean net to the epic's own worst-offender verbs.
  {name: 'lifecycle', scope: 'server', usage: 'gate [--slug <s>] [--tree <sha>] [--ledger <p>] [--stages tests,reviews,delivery]', desc: 'run the DELIVER-leg lifecycle-completeness gate'},
  {name: 'fork', scope: 'server', usage: 'list | [unit]', desc: 'list M5 architecture-fork MPR proposals'},
  {name: 'ace', scope: 'server', usage: 'warmup|eval|snapshot|versions|rollback|unlearn [--ledger <path>]', desc: 'ACE playbook ops (warm-start / efficiency diagnostic + local safety net)'},
  {name: 'quality', scope: 'server', usage: 'reset', desc: 'reset the output-quality breaker after a sustained-degradation staging hold'},
];

/** Handled on THIS (client) side — derived from the registry (no duplication). */
export const LOCAL_COMMANDS: ReadonlySet<string> = new Set(
  COMMANDS.filter((c) => c.scope === 'local').map((c) => c.name),
);

/** Autocomplete (MEM-16): commands whose name starts with `prefix` (prefix WITHOUT the leading
 *  '/'; empty → all). Drives the slash suggestion overlay. `extra` carries dynamic, server-fed
 *  entries (#149, the prompt catalogue) appended after the static set; a built-in command always
 *  wins on a name collision (the dynamic entry with that name is dropped — matching the server's
 *  dispatch order where a real command beats a prompt). */
export function completions(prefix: string, extra: readonly Command[] = []): readonly Command[] {
  const p = prefix.trim().toLowerCase();
  const builtin = new Set(COMMANDS.map((c) => c.name));
  const dyn = extra.filter((c) => !builtin.has(c.name));
  // #1264: a hidden (deprecated) built-in stays dispatchable if typed, but is never advertised here.
  return [...COMMANDS.filter((c) => !c.hidden), ...dyn].filter((c) => c.name.startsWith(p));
}

/** #937: argument autocomplete — once past the verb, complete its subcommands / flag names / flag-choices
 *  from the (server-fed) command-spec (each command carries structured `subcommands`+`flags`, #936).
 *  Deterministic + zero-cost (no model). Returns `arg`-marked items so {@link completionText} inserts the
 *  token into the line instead of replacing it with `/verb`. Empty while still typing the verb (so plain
 *  {@link completions} owns name completion) or for a verb with no spec. */
export function argCompletions(buffer: string, extra: readonly Command[] = []): readonly Command[] {
  if (!buffer.startsWith('/')) return [];
  const trailing = /\s$/.test(buffer);
  const toks = buffer.slice(1).trim().split(/\s+/).filter(Boolean);  // real tokens (no trailing empties)
  if (toks.length === 0) return [];
  if (toks.length === 1 && !trailing) return [];                     // still typing the verb → name completion
  const verb = (toks[0] ?? '').toLowerCase();
  // #952: the /catalogue entry (carries structured subcommands/flags) must win over the static COMMANDS
  // fallback (which has none) — otherwise the static verb shadows the catalogue and arg-autocomplete dies.
  const cmd = [...extra, ...COMMANDS].find((c) => c.name === verb);
  if (!cmd) return [];
  const cur = trailing ? '' : (toks[toks.length - 1] ?? '');         // token being typed ('' right after a space)
  const prev = trailing ? (toks[toks.length - 1] ?? '') : (toks[toks.length - 2] ?? '');
  const mk = (name: string, desc: string): Command => ({name, scope: cmd.scope, desc, arg: true});
  // a flag with choices was just named → offer its values
  const pf = cmd.flags?.find((f) => f.name === prev && f.choices.length);
  if (pf) return pf.choices.filter((c) => c.startsWith(cur)).map((c) => mk(c, `${prev} value`));
  // a flag name is being typed
  if (cur.startsWith('-')) {
    return (cmd.flags ?? []).filter((f) => f.name.startsWith(cur))
      .map((f) => mk(f.name, f.summary + (f.required ? ' (required)' : '')));
  }
  // the first argument slot (verb + space, or typing the first arg) + the verb has subcommands
  const argIndex = toks.length - 1 + (trailing ? 1 : 0);             // 1 = the first argument position
  if (argIndex === 1 && cmd.subcommands?.length) {
    return cmd.subcommands.filter((s) => s.startsWith(cur)).map((s) => mk(s, `${verb} ${s}`));
  }
  return [];
}

/** Map a server `/catalogue` snapshot into dynamic completion entries.
 *  - **commands** (#931): the server-command completions are GENERATED from the live command-spec
 *    (`_catalogue_snapshot["commands"]`), so verbs the static list misses (e.g. lifecycle/fork/ace)
 *    still surface. Client-only (local) verbs are skipped; a name colliding with a built-in is dropped
 *    by {@link completions} (built-in wins). When `/catalogue` is unavailable (cold-start, or a
 *    token/sealed fetch that fails) the static {@link COMMANDS} list is the fallback.
 *  - **prompts** (#149/#148): directly invocable as `/<name>`. Skills are discoverable via `/skills`
 *    but not bare-slash invocable, so they are intentionally left out (no dead completions). */
export function catalogueToCommands(cat: {
  prompts?: Array<{name?: unknown; description?: unknown; languages?: unknown}>;
  commands?: Array<{name?: unknown; usage?: unknown; summary?: unknown; tier?: unknown;
                    subcommands?: unknown; flags?: unknown}>;
}): Command[] {
  const out: Command[] = [];
  for (const c of cat?.commands ?? []) {
    const name = typeof c?.name === 'string' ? c.name : '';
    if (!name || LOCAL_COMMANDS.has(name)) continue;
    const usage = typeof c?.usage === 'string' ? c.usage : '';
    const summary = typeof c?.summary === 'string' ? c.summary : '';
    // #937: retain the structured spec so argCompletions can complete subcommands / flags / choices
    const subcommands = Array.isArray(c?.subcommands)
      ? (c.subcommands as unknown[]).filter((x): x is string => typeof x === 'string') : [];
    const flags = Array.isArray(c?.flags)
      ? (c.flags as Array<Record<string, unknown>>).map((f) => ({
          name: typeof f?.name === 'string' ? f.name : '',
          required: f?.required === true,
          choices: Array.isArray(f?.choices) ? (f.choices as unknown[]).filter((x): x is string => typeof x === 'string') : [],
          summary: typeof f?.summary === 'string' ? f.summary : '',
        })).filter((f) => f.name)
      : [];
    out.push({name, scope: 'server', ...(usage ? {usage} : {}), desc: summary,
              ...(subcommands.length ? {subcommands} : {}), ...(flags.length ? {flags} : {})});
  }
  for (const p of cat?.prompts ?? []) {
    const name = typeof p?.name === 'string' ? p.name : '';
    if (!name) continue;
    const langs = Array.isArray(p?.languages)
      ? (p.languages as unknown[]).filter((x): x is string => typeof x === 'string').join(',')
      : '';
    const desc = typeof p?.description === 'string' ? p.description : '';
    out.push({
      name,
      scope: 'server',
      desc: `prompt${langs ? ' · ' + langs : ''}${desc ? ' — ' + desc : ''}`,
    });
  }
  return out;
}

// #934: short aliases (alias -> canonical command) + the destructive/costly verbs that must never
// auto-resolve from a bare prefix. Mirrors engine.command_spec.ALIASES / unsafe_first_words() — kept in
// sync by the #939 parity guard. English-only.
export const ALIASES: Readonly<Record<string, string>> = {
  lg: 'lifecycle gate', cfg: 'config', keys: 'config keys',
  cfgget: 'config get', cfgset: 'config set', pj: 'project', gen: 'generate',
};
const UNSAFE: ReadonlySet<string> = new Set(['project', 'auto', 'autoplan', 'ace', 'generate', 'tool', 'design']);

function _editDistance(a: string, b: string): number {
  if (a === b) return 0;
  let prev = Array.from({length: b.length + 1}, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++)
      cur.push(Math.min(prev[j]! + 1, cur[j - 1]! + 1, prev[j - 1]! + (a[i - 1] === b[j - 1] ? 0 : 1)));
    prev = cur;
  }
  return prev[b.length]!;
}

/** #934: deterministic, zero-cost resolution of a leading command token (no model). Mirrors
 *  engine.command_spec.resolve_command — exact / alias / non-destructive-unique-prefix / did-you-mean. */
export function resolveCommand(
  token: string,
  knownVerbs: readonly string[],
  aliases: Readonly<Record<string, string>>,
  unsafe: ReadonlySet<string>,
): {kind: 'exact' | 'alias' | 'prefix' | 'suggest' | 'unknown'; value: string} {
  const t = token.trim().toLowerCase();
  if (!t) return {kind: 'unknown', value: ''};
  if (Object.prototype.hasOwnProperty.call(aliases, t)) return {kind: 'alias', value: aliases[t]!};
  const firsts = new Set(knownVerbs.map((v) => v.split(/\s+/)[0]!));
  if (firsts.has(t)) return {kind: 'exact', value: t};
  const pref = [...firsts].filter((f) => f.startsWith(t)).sort();
  if (pref.length === 1) return {kind: unsafe.has(pref[0]!) ? 'suggest' : 'prefix', value: pref[0]!};
  const cand = [...firsts].sort((x, y) => _editDistance(t, x) - _editDistance(t, y) || (x < y ? -1 : 1));
  if (cand.length && _editDistance(t, cand[0]!) <= 2) return {kind: 'suggest', value: cand[0]!};
  return {kind: 'unknown', value: ''};
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
  // #934: alias / non-destructive-unique-prefix / did-you-mean (zero-cost, no turn). An exact/unknown token
  // forwards verbatim as before (so a /<prompt-name> still reaches the server's prompt resolver).
  const r = resolveCommand(name, COMMANDS.map((c) => c.name), ALIASES, UNSAFE);
  if (r.kind === 'alias') return classify('/' + r.value + body.slice(name.length));
  if (r.kind === 'prefix') {
    const payload = r.value + body.slice(name.length);
    return LOCAL_COMMANDS.has(r.value) ? {kind: 'local', name: r.value, payload} : {kind: 'server', name: r.value, payload};
  }
  if (r.kind === 'suggest') return {kind: 'suggest', name: r.value, payload: body};
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
