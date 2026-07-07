/**
 * Claude-Code-style tool-call phrasing (#1167 step 2, epic #1144).
 *
 * Maps the engine's `<Kind>(<arg>)` label to a human summary — the present-progressive action while the tool
 * runs (`Running 1 shell command…`), the past-tense summary once done (`Ran 1 shell command`, `Read 70
 * lines`) — plus the one-line detail shown collapsed (`$ <cmd>` / `<path>`) and the exact header shown when
 * expanded (`Bash(<cmd>)`). Pure + testable; the <ToolCall> component renders it.
 */
export interface ToolMeta {
  summary: string; // the `●` line (collapsed): action phrase
  detail: string; // the `⎿` line (collapsed): the command / target, '' if none
  header: string; // the `●` line (expanded): the exact `Kind(arg)` label
}

const VERBS: Record<string, {running: string; done: string; shell?: boolean}> = {
  Bash: {running: 'Running 1 shell command…', done: 'Ran 1 shell command', shell: true},
  Read: {running: 'Reading 1 file…', done: 'Read 1 file'},
  Write: {running: 'Writing 1 file…', done: 'Wrote 1 file'},
  List: {running: 'Listing 1 directory…', done: 'Listed 1 directory'},
  Search: {running: 'Searching…', done: 'Searched'},
  Issue: {running: 'Creating 1 issue…', done: 'Created 1 issue'},
};

export function toolMeta(label: string, done: boolean, lineCount: number, shell = 'Bash'): ToolMeta {
  const m = /^(\w+)\((.*)\)$/.exec(label);
  const kind = m ? (m[1] ?? '') : label;
  const arg = m ? (m[2] ?? '') : '';
  const v = VERBS[kind];
  let summary: string;
  if (!v) {
    summary = done ? label : `${label}…`;
  } else if (kind === 'Read' && done && lineCount > 0) {
    summary = `Read ${lineCount} line${lineCount === 1 ? '' : 's'}`;
  } else {
    summary = done ? v.done : v.running;
  }
  // Expanded header: a shell command uses the client's ACTUAL shell (PowerShell on Windows), not a hardcoded
  // "Bash" — the engine labels every `execute_command` "Bash", but the local tool-bridge runs the platform
  // shell (runTool.ts spawns `powershell` on win32).
  const header = v?.shell ? `${shell}(${arg})` : label;
  return {summary, detail: v?.shell ? `$ ${arg}` : arg, header};
}
