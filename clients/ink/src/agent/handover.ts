/**
 * Local handover / code-agent execution ≙ client.py:381-468 (`_run_handover`,
 * `_process_one`, `dispatch_pending`) + the `/work` and `/auto` REPL handlers.
 *
 * The server NEVER launches code-agents (its queue consumer no-ops); this client pulls
 * `/pending`, runs each handover LOCALLY with `claude --print` (cwd = --codedir), reads the
 * feedback file the agent wrote, and POSTs it to `/feedback`. Concurrency is a bounded
 * async pool (= max parallel agents). The "no-polling" memory forbids polling background-task
 * completion; `await`-ing a child's exit (and the spec'd /auto 5s poller) is the contract.
 */
import {spawn} from 'node:child_process';
import {promises as fs} from 'node:fs';
import * as fsSync from 'node:fs';
import path from 'node:path';
import type {Server, Json} from '../net/server.js';

type Item = Record<string, unknown>;
const str = (v: unknown, d = ''): string => (v === undefined || v === null ? d : String(v));

/** The Claude-default launch template — used only when neither an explicit client override nor the
 *  server's per-agent spec supplies one (≙ client.py DEFAULT_AGENT_CMD). */
export const DEFAULT_AGENT_CMD =
  '{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}';
const DEFAULT_BIN = 'claude';
/** #455: how much of a code-agent's stderr to upload for the server-side exhausted classifier. */
const STDERR_TAIL_CHARS = 4000;

export interface HandoverCfg {
  // #449/INK-HANDOVER-1 (#503): an EXPLICIT client-side override (GX10_CLAUDE_BIN / GX10_AGENT_CMD or
  // the config file) — the documented single-agent BYO path — beats the server's per-agent spec; null
  // (unset) ⇒ the server spec wins, then the built-in default. Mirrors client.py's *_OVERRIDE precedence.
  claudeBinOverride: string | null;
  agentCmdOverride: string | null;
  claudeEffort: string; // default effort when the item omits it
  claudePermissionMode: string; // default permission when the item omits it
}

/** First non-empty string (mirrors Python `a or b or c`; '' is falsy). */
function firstNonEmpty(...vals: Array<string | null | undefined>): string {
  for (const v of vals) if (v) return v;
  return '';
}

/** The fully-resolved per-agent launch spec (INK-HANDOVER-1, #503). PURE + testable: the server ships
 *  the full spec per `/pending` item (bin/cmd_template/model/effort/permission + mcp/mcp_env); the client
 *  is a thin renderer. Precedence per field mirrors client.py `_run_handover`: an explicit client override
 *  > the server's item spec > the built-in default; `mcp`/`mcp_env` come straight from the item. */
export interface LaunchSpec {
  bin: string;
  model: string;
  effort: string;
  permission: string;
  template: string;
  mcp: string;
  mcpEnv: Record<string, string>;
}

type ToolingEnvelopePolicy = {enabled?: boolean; allow_list?: Array<{bin?: string; cmd_template?: string}>};

function normalizeTemplate(v: unknown): string {
  return str(v).replaceAll('{mcp}', '').replace(/[ \t\n\r\f\v]+/g, ' ').replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, '');
}

function isBareCommand(v: string): boolean {
  return !!v && !v.includes('/') && !v.includes('\\');
}

function binIdentity(v: unknown): string {
  const s = str(v).trim();
  if (!s) return '';
  if (isBareCommand(s)) return path.basename(s);
  const expanded = expandPath(s);
  try {
    return fsSync.realpathSync.native(expanded);
  } catch {
    return path.resolve(expanded);
  }
}

function expandPath(s: string): string {
  let out = s.replace(/^~(?=$|[/\\])/, process.env.HOME || process.env.USERPROFILE || '~');
  out = out.replace(/\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([^}]+)\}/g, (m, a, b) => {
    const key = a || b;
    return process.env[key] ?? m;
  });
  return out;
}

function globMatch(text: string, pat: string): boolean {
  let rx = '^';
  for (let i = 0; i < pat.length; i++) {
    const c = pat[i] as string;
    if (c === '*') rx += '.*';
    else if (c === '?') rx += '.';
    else rx += c.replace(/[.[\]+^${}()|\\]/g, '\\$&');
  }
  rx += '$';
  return new RegExp(rx).test(text);
}

function binMatches(candidate: string, allowed: string): boolean {
  const a = expandPath(allowed.trim());
  if (!candidate || !a) return false;
  if (/[?*]/.test(a)) return globMatch(candidate, a);
  if (!isBareCommand(a)) return candidate === binIdentity(a);
  const aid = binIdentity(a);
  return candidate === aid || path.basename(candidate) === path.basename(aid);
}

export function authorizeLaunch(bin: string, template: string, policy: ToolingEnvelopePolicy | null | undefined): string | null {
  if (policy === undefined) return null;
  if (policy === null) return 'tooling envelope refused malformed policy';
  if (policy.enabled !== true) {
    if (policy.enabled === false) return null;
    return 'tooling envelope refused malformed policy';
  }
  if (!Array.isArray(policy.allow_list)) return 'tooling envelope refused malformed policy';
  const candidateBin = binIdentity(bin);
  const candidateTemplate = normalizeTemplate(template);
  if (!candidateBin || !candidateTemplate) return 'tooling envelope refused malformed coder command';
  for (const e of policy.allow_list) {
    if (binMatches(candidateBin, str(e.bin)) && candidateTemplate === normalizeTemplate(e.cmd_template)) return null;
  }
  return 'tooling envelope refused unauthorized coder command';
}

export function resolveLaunch(item: Item, cfg: HandoverCfg): LaunchSpec {
  const me = item['mcp_env'];
  const mcpEnv: Record<string, string> = {};
  if (me && typeof me === 'object' && !Array.isArray(me)) {
    for (const [k, v] of Object.entries(me as Record<string, unknown>)) mcpEnv[String(k)] = String(v);
  }
  return {
    bin: firstNonEmpty(cfg.claudeBinOverride, str(item['bin']), DEFAULT_BIN),
    // model fallback matches client.py exactly (`item['model'] or 'claude-opus-4-8'`) — the server ships
    // the per-agent model, so no client-side agent→model table (a thin renderer keeps no such table).
    model: firstNonEmpty(str(item['model']), 'claude-opus-4-8'),
    effort: firstNonEmpty(str(item['effort']), cfg.claudeEffort),
    permission: firstNonEmpty(str(item['permission']), cfg.claudePermissionMode),
    template: firstNonEmpty(cfg.agentCmdOverride, str(item['cmd_template']), DEFAULT_AGENT_CMD),
    mcp: str(item['mcp']),
    mcpEnv,
  };
}

/** POSIX shlex.split — whitespace splits tokens; single/double quotes and backslash group. */
export function shlexSplit(s: string): string[] {
  const out: string[] = [];
  let cur = '';
  let started = false;
  let inSingle = false;
  let inDouble = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i] as string;
    if (inSingle) {
      if (c === "'") inSingle = false;
      else cur += c;
    } else if (inDouble) {
      if (c === '"') inDouble = false;
      else if (c === '\\' && (s[i + 1] === '"' || s[i + 1] === '\\')) cur += s[++i];
      else cur += c;
    } else if (c === "'") {
      inSingle = true;
      started = true;
    } else if (c === '"') {
      inDouble = true;
      started = true;
    } else if (c === '\\') {
      cur += s[++i] ?? '';
      started = true;
    } else if (c === ' ' || c === '\t' || c === '\n') {
      if (started) {
        out.push(cur);
        cur = '';
        started = false;
      }
    } else {
      cur += c;
      started = true;
    }
  }
  if (started) out.push(cur);
  return out;
}

/** ≙ commands.build_agent_argv: shlex-split the template, then substitute placeholders PER TOKEN so
 *  `{prompt}` (with spaces) stays exactly one argv element. `{feedback}` (#443) is the deterministic
 *  result-capture path; `{mcp}` (#480) is a MULTI-token placeholder that expands (via shlex) to 0+ args
 *  (the gated read-only Memory MCP config under the sealed profile, else nothing). Unknown `{x}` are
 *  left as-is. (INK-HANDOVER-1, #503: the client previously dropped {feedback}/{mcp} — left literal.) */
export function buildAgentArgv(
  template: string,
  subs: {bin: string; model: string; effort: string; permission: string; prompt: string;
         feedback?: string; mcp?: string},
): string[] {
  const map: Record<string, string> = {
    bin: subs.bin, model: subs.model, effort: subs.effort,
    permission: subs.permission, prompt: subs.prompt, feedback: subs.feedback ?? '',
  };
  const mcp = subs.mcp ?? '';
  const argv: string[] = [];
  for (const tok of shlexSplit(template)) {
    if (tok === '{mcp}') {
      argv.push(...shlexSplit(mcp)); // #480: multi-token — empty mcp ⇒ no args
    } else if (tok.startsWith('{') && tok.endsWith('}') && tok.slice(1, -1) in map) {
      argv.push(map[tok.slice(1, -1)] as string);
    } else {
      let t = tok;
      for (const [k, v] of Object.entries(map)) t = t.replaceAll('{' + k + '}', v);
      argv.push(t);
    }
  }
  return argv;
}

/** Bounded async pool — at most `max` concurrent jobs (≙ ThreadPoolExecutor max_workers). */
export class Pool {
  private active = 0;
  private readonly waiters: Array<() => void> = [];
  constructor(private readonly max: number) {}
  async run<T>(fn: () => Promise<T>): Promise<T> {
    if (this.active >= this.max) await new Promise<void>((r) => this.waiters.push(r));
    this.active++;
    try {
      return await fn();
    } finally {
      this.active--;
      this.waiters.shift()?.();
    }
  }
}

/** The run signal reported back to the server (#455 / INK-HANDOVER-2): exit code + a stderr tail so a
 *  budget/quota-exhausted run is classified `agent-unavailable` (trip the breaker + fail over) instead
 *  of retrying the same agent forever. `enoent` ⇒ the binary was missing (exit_code null). */
type SpawnResult = {enoent: true} | {rc: number | null; stdout: string; stderr: string};

/** Spawn the code-agent; capture stdout/stderr so output stays under the Ink renderer's control. */
function spawnAgent(argv: string[], codedir: string, env: NodeJS.ProcessEnv): Promise<SpawnResult> {
  return new Promise((resolve) => {
    let done = false;
    const outChunks: Buffer[] = [];
    const errChunks: Buffer[] = [];
    const fin = (r: SpawnResult): void => {
      if (!done) {
        done = true;
        resolve(r);
      }
    };
    // stdout/stderr are piped so a local code-agent cannot write directly into the Ink-owned terminal.
    const child = spawn(argv[0] as string, argv.slice(1), {
      cwd: codedir,
      stdio: ['ignore', 'pipe', 'pipe'],
      env,
    });
    child.stdout?.on('data', (d: Buffer) => {
      outChunks.push(d);
    });
    child.stderr?.on('data', (d: Buffer) => {
      errChunks.push(d);
    });
    child.on('error', (e) => {
      const stdout = Buffer.concat(outChunks).toString('utf-8');
      const stderr = Buffer.concat(errChunks).toString('utf-8');
      fin((e as NodeJS.ErrnoException).code === 'ENOENT' ? {enoent: true} : {rc: 1, stdout, stderr});
    });
    child.on('close', (code) => {
      const stdout = Buffer.concat(outChunks).toString('utf-8');
      const stderr = Buffer.concat(errChunks).toString('utf-8');
      fin({rc: code, stdout, stderr});
    });
  });
}

/** The handover run result: the feedback text (null if none) + the run signal meta (#455). */
export interface RunResult {
  fb: string | null;
  meta: {exit_code: number | null; stderr: string};
}

/** ≙ _run_handover: materialise the handover, run the per-agent code-agent locally, read the feedback,
 *  and ALWAYS return the run signal (#455). Threads the full server-shipped per-agent spec (INK-HANDOVER-1)
 *  and falls back to the {feedback} captured final message when no feedback file is written (#443). */
export async function runHandover(
  item: Item,
  codedir: string,
  cfg: HandoverCfg,
  log: (m: string) => void,
): Promise<RunResult> {
  const tid = str(item['id']);
  const agent = str(item['agent'], 'OPUS').toUpperCase();
  const hoName = str(item['handover_file']) || `${tid}_${agent}.md`;
  const hoText = str(item['handover']);

  // Local agent scratch is kept OUT of the product tree: a hidden .ironclad/agent/ drop zone under the
  // client's codedir (the handover round-trip is HTTP-mediated, independent of the server's initiative).
  const hoDir = path.join(codedir, '.ironclad', 'agent', 'handovers');
  await fs.mkdir(hoDir, {recursive: true});
  const hoPath = path.join(hoDir, hoName);
  await fs.writeFile(hoPath, hoText, 'utf-8');

  const spec = resolveLaunch(item, cfg); // INK-HANDOVER-1 (#503): bin/template/model/effort/permission/mcp
  const fbName = `${tid}_${agent}-feedback.md`;
  const capName = `${tid}_${agent}-output.md`; // #443: deterministic {feedback} capture path
  // #1307: BUILD PRODUCT CODE in the active project's code root — the server ships it per `/pending`
  // item (`cwd` = the engine's exec cwd for the active project = <project-root>/<code_subdir>). Honour it
  // ONLY when it is a real directory on THIS host: in a remote/sealed topology the client does not share
  // the server's filesystem, so that absolute path won't exist — fall back to the client's own `codedir`
  // (today's behaviour, byte-identical for that topology), as we also do when no `cwd` is shipped (older
  // engine). This closes the isolation escape where a coder launched after an in-session `/switch` spawned
  // in the client's stale startup `codedir` and wrote one project's code into another project's tree.
  const shippedCwd = str(item['cwd']);
  let launchCwd = codedir;
  if (shippedCwd) {
    try {
      if ((await fs.stat(shippedCwd)).isDirectory()) launchCwd = shippedCwd;
    } catch {
      /* shipped cwd not present on this host (remote/sealed) → keep codedir */
    }
  }

  // #443 (review F-1): unlink BOTH result paths before launching so a stale file from a prior failed
  // attempt can never be read as THIS run's result (the scratch dir persists across re-runs).
  const fbDir = path.join(codedir, '.ironclad', 'agent', 'feedback');
  await fs.mkdir(fbDir, {recursive: true});
  const fbPath = path.join(fbDir, fbName);
  const capPath = path.join(fbDir, capName);
  await fs.rm(fbPath, {force: true});
  await fs.rm(capPath, {force: true});

  // #1307: the coder's scratch stays under `codedir` but the launch cwd is the project code root, so the
  // handover-in / feedback-out paths handed to the coder are ABSOLUTE — resolved independently of its cwd,
  // and the client reads the feedback back from the same place regardless of where the product tree lives.
  const prompt =
    `Autonomously read and complete the handover at ${hoPath}. ` +
    `Follow any agent guide in this repo (e.g. AGENTS.md / CLAUDE.md). When done, write a ` +
    `short result summary to ${fbPath}.`;

  const argv = buildAgentArgv(spec.template, {
    bin: spec.bin,
    model: spec.model,
    effort: spec.effort,
    permission: spec.permission,
    prompt,
    feedback: capPath, // #443 capture; absolute so it is independent of the coder's cwd (#1307)
    mcp: spec.mcp,
  });
  const refusal = authorizeLaunch(spec.bin, spec.template, item['tooling_envelope'] as ToolingEnvelopePolicy | undefined);
  if (refusal) {
    log(`  ✗ ${refusal} — handover ${tid} skipped`);
    return {fb: null, meta: {exit_code: null, stderr: refusal}};
  }

  log(`  → code-agent (local): ${tid} (${agent}, ${spec.model}, effort=${spec.effort})  cwd=${launchCwd}`);
  // #480: the spawned MCP inherits the memory connection from the agent's env — it travels here, NEVER
  // on the MCP JSON-RPC wire (secret-free). Empty mcp_env under open/token ⇒ byte-identical launch.
  const env: NodeJS.ProcessEnv = {...process.env, PYTHONIOENCODING: 'utf-8', ...spec.mcpEnv};
  const res = await spawnAgent(argv, launchCwd, env);
  if ('enoent' in res) {
    log(`  ✗ code-agent binary '${argv[0] ?? spec.bin}' not found (set GX10_CLAUDE_BIN / GX10_AGENT_CMD) — handover ${tid} skipped`);
    return {fb: null, meta: {exit_code: null, stderr: 'binary-not-found'}};
  }
  const logDir = path.join(codedir, '.ironclad', 'agent', 'logs');
  const logPath = path.join(logDir, `${tid}_${agent}.log`);
  try {
    await fs.mkdir(logDir, {recursive: true});
    await fs.writeFile(
      logPath,
      `# ${tid} ${agent} (exit ${res.rc})\n\n## stdout\n${res.stdout}\n\n## stderr\n${res.stderr}\n`,
      'utf-8',
    );
  } catch {
    /* fail-soft — the handover result path is more important than diagnostic logging */
  }
  if (res.stderr.trim()) {
    log(`  ⓘ ${agent} stderr (${res.stderr.length} chars) -> ${logPath}`);
    const tail = res.stderr.trim().split(/\r?\n/).slice(-2).join(' | ').slice(-200);
    if (tail) log(`     ${tail}`);
  }
  const meta = {exit_code: res.rc, stderr: res.stderr.slice(-STDERR_TAIL_CHARS)};

  try {
    return {fb: await fs.readFile(fbPath, 'utf-8'), meta};
  } catch {
    /* no feedback file — fall through to the {feedback} captured final message */
  }
  // #443 hybrid fallback: the agent didn't write the feedback file — use its captured final message.
  try {
    const cap = await fs.readFile(capPath, 'utf-8');
    if (cap.trim()) {
      log(`  ⓘ no feedback file ${fbName}; using the captured final message ${capName}`);
      return {fb: cap, meta};
    }
  } catch {
    /* no capture either */
  }
  if (res.stdout.trim()) {
    log(`  ⓘ no feedback file — captured ${res.stdout.length} chars from stdout`);
    return {fb: res.stdout, meta};
  }
  log(`  ⚠ agent exited (exit ${res.rc}) without a feedback file ${fbName} or a captured message`);
  return {fb: null, meta};
}

/** #1300: drop the per-task agent scratch (handover drop + feedback + capture) after a SUCCESSFUL
 *  upload — the server-side `.work/archive/` history is the durable record; the client copies are
 *  transport materialization and would otherwise accumulate per task. Fail-soft: a cleanup hiccup
 *  never un-does a successful run. A FAILED run keeps its scratch for diagnosis + retry. */
async function cleanupAgentScratch(codedir: string, item: Item): Promise<void> {
  const tid = str(item['id']);
  const agent = str(item['agent'], 'OPUS').toUpperCase();
  const hoName = str(item['handover_file']) || `${tid}_${agent}.md`;
  const base = path.join(codedir, '.ironclad', 'agent');
  for (const p of [
    path.join(base, 'handovers', hoName),
    path.join(base, 'feedback', `${tid}_${agent}-feedback.md`),
    path.join(base, 'feedback', `${tid}_${agent}-output.md`),
    path.join(base, 'logs', `${tid}_${agent}.log`),
  ]) {
    try {
      await fs.rm(p, {force: true});
    } catch {
      /* fail-soft — never let cleanup mask a successful upload */
    }
  }
}

/** ≙ _process_one: run the handover, upload feedback; un-claim on any failure for retry. */
export async function processOne(
  srv: Server,
  item: Item,
  codedir: string,
  cfg: HandoverCfg,
  claimed: Set<string>,
  log: (m: string) => void,
): Promise<boolean> {
  const tid = str(item['id']);
  const agent = str(item['agent'], 'OPUS').toUpperCase();
  try {
    const {fb, meta} = await runHandover(item, codedir, cfg, log);
    // INK-HANDOVER-2 (#503): ALWAYS report the run signal (even with no feedback) so the server can
    // classify a budget-exhausted run → trip the #455 breaker + fail over on the next poll, instead of
    // retrying the same out-of-budget agent forever. Mirrors client.py _process_one.
    const res = await srv.feedback({
      task_id: tid,
      agent,
      content: fb ?? '',
      exit_code: meta.exit_code,
      stderr: meta.stderr,
    });
    const clsRaw = (res as Json)['classification'];
    const cls = typeof clsRaw === 'string' ? clsRaw : null;
    if (cls === 'ok-feedback' || (cls === null && fb)) {
      log(`  ✓ feedback uploaded: ${tid} → ${str((res as Json)['feedback_file'])}`);
      await cleanupAgentScratch(codedir, item); // #1300: the scratch is done its job
      return true;
    }
    if (cls === 'agent-unavailable') {
      log(`  ⚠ ${tid}: ${agent} unavailable (budget/quota) → failing over to a peer on the next poll`);
    } else {
      log(`  ⚠ ${tid}: no feedback produced — will retry on the next poll`);
    }
  } catch (e) {
    log(`  ✗ ${tid}: upload/code-agent failed: ${e instanceof Error ? e.message : String(e)}`);
  }
  claimed.delete(tid);
  return false;
}

/** ≙ dispatch_pending: pull /pending, submit every UNclaimed handover to the pool. NON-blocking. */
export async function dispatchPending(
  srv: Server,
  codedir: string,
  cfg: HandoverCfg,
  pool: Pool,
  claimed: Set<string>,
  log: (m: string) => void,
): Promise<Array<Promise<boolean>>> {
  let pending: Json[];
  try {
    pending = await srv.pending();
  } catch (e) {
    log(`  ✗ /pending unreachable: ${e instanceof Error ? e.message : String(e)}`);
    return [];
  }
  const jobs: Array<Promise<boolean>> = [];
  for (const item of pending) {
    const tid = str(item['id']);
    if (!tid || claimed.has(tid)) continue;
    claimed.add(tid); // claim immediately → no double-launch on an overlapping poll
    jobs.push(pool.run(() => processOne(srv, item, codedir, cfg, claimed, log)));
  }
  return jobs;
}
