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
import path from 'node:path';
import type {Server, Json} from '../net/server.js';

export const MODEL_BY_AGENT: Record<string, string> = {
  OPUS: 'claude-opus-4-8',
  SONNET: 'claude-sonnet-4-6',
};

export interface HandoverCfg {
  claudeBin: string;
  claudeEffort: string;
  claudePermissionMode: string;
  agentCmd: string;
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

/** ≙ _build_agent_argv: shlex-split the template, then substitute placeholders PER TOKEN so
 *  `{prompt}` (with spaces) stays exactly one argv element. Unknown `{x}` are left as-is. */
export function buildAgentArgv(
  template: string,
  subs: {bin: string; model: string; effort: string; permission: string; prompt: string},
): string[] {
  const map: Record<string, string> = {...subs};
  return shlexSplit(template).map((tok) => {
    const bare = tok.slice(1, -1);
    if (tok.startsWith('{') && tok.endsWith('}') && bare in map) return map[bare] as string;
    let t = tok;
    for (const [k, v] of Object.entries(map)) t = t.replaceAll('{' + k + '}', v);
    return t;
  });
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

type Item = Record<string, unknown>;
const str = (v: unknown, d = ''): string => (v === undefined || v === null ? d : String(v));

/** Spawn the code-agent; resolve {enoent:true} if the binary is missing, else {rc}. */
function spawnAgent(argv: string[], codedir: string): Promise<{rc: number} | {enoent: true}> {
  return new Promise((resolve) => {
    let done = false;
    const fin = (r: {rc: number} | {enoent: true}): void => {
      if (!done) {
        done = true;
        resolve(r);
      }
    };
    const child = spawn(argv[0] as string, argv.slice(1), {
      cwd: codedir,
      stdio: ['ignore', 'inherit', 'inherit'],
      env: {...process.env, PYTHONIOENCODING: 'utf-8'},
    });
    child.on('error', (e) => fin((e as NodeJS.ErrnoException).code === 'ENOENT' ? {enoent: true} : {rc: 1}));
    child.on('close', (code) => fin({rc: code ?? 0}));
  });
}

/** ≙ _run_handover: materialise the handover, run claude --print locally, read the feedback. */
export async function runHandover(
  item: Item,
  codedir: string,
  cfg: HandoverCfg,
  log: (m: string) => void,
): Promise<string | null> {
  const tid = str(item['id']);
  const agent = str(item['agent'], 'OPUS').toUpperCase();
  const hoName = str(item['handover_file']) || `${tid}_${agent}.md`;
  const hoText = str(item['handover']);

  // Local agent scratch is kept OUT of the project root: a hidden .ironclad/agent/ drop zone
  // (the handover round-trip is HTTP-mediated, so this path is independent of the server's vorhaben).
  const hoDir = path.join(codedir, '.ironclad', 'agent', 'handovers');
  await fs.mkdir(hoDir, {recursive: true});
  await fs.writeFile(path.join(hoDir, hoName), hoText, 'utf-8');

  const model = str(item['model']) || MODEL_BY_AGENT[agent] || 'claude-opus-4-8';
  const effort = str(item['effort']) || cfg.claudeEffort;
  const fbName = `${tid}_${agent}-feedback.md`;
  const prompt =
    `Autonomously read and complete the handover at .ironclad/agent/handovers/${hoName}. ` +
    `Follow any agent guide in this repo (e.g. AGENTS.md / CLAUDE.md). When done, write a ` +
    `short result summary to .ironclad/agent/feedback/${fbName}.`;

  const argv = buildAgentArgv(cfg.agentCmd, {
    bin: cfg.claudeBin,
    model,
    effort,
    permission: cfg.claudePermissionMode,
    prompt,
  });
  log(`  → code-agent (local): ${tid} (${agent}, ${model}, effort=${effort})  cwd=${codedir}`);
  const res = await spawnAgent(argv, codedir);
  if ('enoent' in res) {
    log(`  ✗ code-agent binary '${argv[0] ?? cfg.claudeBin}' not found (set GX10_CLAUDE_BIN / GX10_AGENT_CMD) — handover ${tid} skipped`);
    return null;
  }
  const fbPath = path.join(codedir, '.ironclad', 'agent', 'feedback', fbName);
  try {
    return await fs.readFile(fbPath, 'utf-8');
  } catch {
    log(`  ⚠ claude exited (exit ${res.rc}) without a feedback file ${fbName}`);
    return null;
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
    const fb = await runHandover(item, codedir, cfg, log);
    if (fb) {
      const res = await srv.feedback({task_id: tid, agent, content: fb});
      log(`  ✓ feedback uploaded: ${tid} → ${str((res as Json)['feedback_file'])}`);
      return true;
    }
    log(`  ⚠ ${tid}: no feedback produced — will retry on the next poll`);
  } catch (e) {
    log(`  ✗ ${tid}: code-agent failed: ${e instanceof Error ? e.message : String(e)}`);
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
