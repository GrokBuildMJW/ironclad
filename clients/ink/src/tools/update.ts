/**
 * /update (MEM-17) — rebuild + reinstall the global `ironclad` client from source, so the user
 * never has to leave the CLI to run `cd clients/ink && npm run build && npm install -g .`.
 *
 * The running Node process can't hot-swap its own code, so this only stages the new build; the user
 * restarts `ironclad` to pick it up. A globally-installed package doesn't know where its source repo
 * is, so the path comes from config (GX10_SRC / srcDir = the repo root). The steps are pure
 * (`updatePlan`) for testability; `runUpdate` executes them in order, fail-soft, stopping at the
 * first failure.
 */
import {spawn, type ChildProcess, type SpawnOptions} from 'node:child_process';
import {join} from 'node:path';

export interface UpdateStep {
  label: string;
  command: string;
  args: string[];
}

/** The executable + argv steps to rebuild+reinstall from `srcDir` (the repo root). */
export function updatePlan(srcDir: string, pull: boolean): UpdateStep[] {
  const ink = join(srcDir, 'clients', 'ink');
  const steps: UpdateStep[] = [];
  if (pull) steps.push({label: 'git pull', command: 'git', args: ['-C', srcDir, 'pull', '--ff-only']});
  steps.push({label: 'build', command: 'npm', args: ['--prefix', ink, 'run', 'build']});
  steps.push({label: 'install -g', command: 'npm', args: ['install', '-g', ink]});
  return steps;
}

/** Return why `srcDir` is unsafe for the Windows command wrapper, or null when it is safe. Only true
 *  shell/cmd operators are rejected — NOT the apostrophe (a legitimate path component, e.g. an `O'Brien`
 *  home dir, that is harmless under POSIX argv-no-shell and is not special to cmd.exe). */
export function validateSrcDir(srcDir: string): string | null {
  return /["`$&|;<>^%!\n\r]/u.test(srcDir)
    ? 'the source path contains a shell/cmd metacharacter'
    : null;
}

/** Injectable command runner — resolves with the exit code + combined stdout/stderr. */
export type Exec = (command: string, args: string[]) => Promise<{code: number; out: string}>;

const TIMEOUT_MS = 300_000;
const MAX_BUFFER = 16 * 1024 * 1024;

const defaultExec: Exec = (command, args) =>
  new Promise((resolve) => {
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let forcedCode: number | null = null;
    let settled = false;
    let timer: NodeJS.Timeout | undefined;

    const finish = (code: number, error = ''): void => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      const out = `${Buffer.concat(stdout).toString('utf8')}${Buffer.concat(stderr).toString('utf8')}${error}`.trim();
      resolve({code, out});
    };

    try {
      const options: SpawnOptions = {stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true};
      const child: ChildProcess = process.platform === 'win32'
        ? spawn('cmd.exe', ['/d', '/s', '/c', command, ...args], options)
        : spawn(command, args, options);

      const capture = (target: Buffer[], chunk: Buffer, buffered: number): number => {
        const remaining = MAX_BUFFER - buffered;
        if (remaining > 0) {
          const kept = chunk.length > remaining ? chunk.subarray(0, remaining) : chunk;
          target.push(kept);
          buffered += kept.length;
        }
        if (chunk.length > remaining && forcedCode === null) {
          forcedCode = 1;
          child.kill();
        }
        return buffered;
      };

      child.stdout?.on('data', (chunk: Buffer) => {
        stdoutBytes = capture(stdout, chunk, stdoutBytes);
      });
      child.stderr?.on('data', (chunk: Buffer) => {
        stderrBytes = capture(stderr, chunk, stderrBytes);
      });
      child.on('error', (error) => finish(1, error.message));
      child.on('close', (code) => finish(forcedCode ?? code ?? 1));
      timer = setTimeout(() => {
        forcedCode = 1;
        child.kill();
      }, TIMEOUT_MS);
    } catch (error) {
      finish(1, error instanceof Error ? error.message : String(error));
    }
  });

/** Run the update steps in order; stop at the first failure. Never throws — returns a log + ok. */
export async function runUpdate(
  srcDir: string,
  pull: boolean,
  exec: Exec = defaultExec,
): Promise<{ok: boolean; log: string[]}> {
  const validationError = validateSrcDir(srcDir);
  if (validationError) return {ok: false, log: [`✗ /update refused: ${validationError}`]};

  const log: string[] = [];
  for (const step of updatePlan(srcDir, pull)) {
    const {code, out} = await exec(step.command, step.args);
    log.push(`• ${step.label}: ${code === 0 ? 'ok' : `FAILED (exit ${code})`}`);
    if (out) log.push(out);
    if (code !== 0) return {ok: false, log};
  }
  log.push('✓ updated — restart ironclad so the new version takes effect.');
  return {ok: true, log};
}
