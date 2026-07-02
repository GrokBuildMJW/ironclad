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
import {exec as cpExec} from 'node:child_process';
import {join} from 'node:path';

export interface UpdateStep {
  label: string;
  command: string;
}

/** The shell steps to rebuild+reinstall from `srcDir` (the repo root). Pure → unit-testable. */
export function updatePlan(srcDir: string, pull: boolean): UpdateStep[] {
  const ink = join(srcDir, 'clients', 'ink');
  const steps: UpdateStep[] = [];
  if (pull) steps.push({label: 'git pull', command: `git -C "${srcDir}" pull --ff-only`});
  steps.push({label: 'build', command: `npm --prefix "${ink}" run build`});
  steps.push({label: 'install -g', command: `npm install -g "${ink}"`});
  return steps;
}

/** Injectable command runner — resolves with the exit code + combined stdout/stderr. */
export type Exec = (command: string) => Promise<{code: number; out: string}>;

const defaultExec: Exec = (command) =>
  new Promise((resolve) => {
    cpExec(command, {timeout: 300_000, maxBuffer: 16 * 1024 * 1024, windowsHide: true}, (err, stdout, stderr) => {
      const out = `${stdout ?? ''}${stderr ?? ''}`.trim();
      const code = err ? (typeof (err as {code?: unknown}).code === 'number' ? (err as {code: number}).code : 1) : 0;
      resolve({code, out});
    });
  });

/** Run the update steps in order; stop at the first failure. Never throws — returns a log + ok. */
export async function runUpdate(
  srcDir: string,
  pull: boolean,
  exec: Exec = defaultExec,
): Promise<{ok: boolean; log: string[]}> {
  const log: string[] = [];
  for (const step of updatePlan(srcDir, pull)) {
    const {code, out} = await exec(step.command);
    log.push(`• ${step.label}: ${code === 0 ? 'ok' : `FAILED (exit ${code})`}`);
    if (out) log.push(out);
    if (code !== 0) return {ok: false, log};
  }
  log.push('✓ updated — restart ironclad so the new version takes effect.');
  return {ok: true, log};
}
