import {spawn, type ChildProcess} from 'node:child_process';

/** Resolve once *child* exits/closes or the bound expires; never wait indefinitely. */
export function waitForChildExit(child: ChildProcess, timeoutMs: number): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    let settled = false;
    const settle = (): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener('exit', settle);
      child.removeListener('close', settle);
      resolve();
    };
    const timer = setTimeout(settle, Math.max(0, timeoutMs));
    child.once('exit', settle);
    child.once('close', settle);
    // Close the check/listener race if the child exited between the first check and listener registration.
    if (child.exitCode !== null || child.signalCode !== null) settle();
  });
}

/** Best-effort whole-tree termination shared by every Ink subprocess lane. */
export async function killProcessTree(child: ChildProcess): Promise<void> {
  child.stdin?.destroy();
  child.stdout?.destroy();
  child.stderr?.destroy();
  if (child.pid === undefined) return;
  if (process.platform === 'win32') {
    await new Promise<void>((resolve) => {
      const killer = spawn('taskkill', ['/F', '/T', '/PID', String(child.pid)], {stdio: 'ignore'});
      killer.once('error', () => {
        try { child.kill('SIGKILL'); } catch { /* process already exited */ }
        resolve();
      });
      killer.once('close', (code) => {
        if (code !== 0) {
          try { child.kill('SIGKILL'); } catch { /* process already exited */ }
        }
        resolve();
      });
    });
  } else {
    try {
      process.kill(-child.pid, 'SIGKILL');
    } catch {
      try { child.kill('SIGKILL'); } catch { /* process already exited */ }
    }
  }
  child.unref();
}
