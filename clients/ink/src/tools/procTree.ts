import {spawn, type ChildProcess} from 'node:child_process';

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
