import {promises as fs} from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/** Install a deterministic test-only bwrap interface shim on PATH for one positive model-command call.
 * Production still resolves the host's real bwrap/firejail; this helper is imported only by tests. */
export async function withSandboxShim<T>(fn: () => Promise<T>): Promise<T> {
  const shimDir = await fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-bwrap-shim-'));
  const shim = path.join(shimDir, 'bwrap');
  await fs.writeFile(shim, [
    '#!/bin/sh',
    // #1489: record the FULL argv (the sandbox flags before `--`) so a test can assert the hardening
    // flags are actually passed; then strip to `--` and exec the real command as before.
    '[ -n "$IRONCLAD_BWRAP_ARGV_LOG" ] && printf \'%s\\n\' "$*" > "$IRONCLAD_BWRAP_ARGV_LOG"',
    'while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done',
    '[ "$#" -gt 0 ] && shift',
    'exec "$@"',
    '',
  ].join('\n'), 'utf-8');
  await fs.chmod(shim, 0o755);
  const oldPath = process.env.PATH ?? '';
  process.env.PATH = shimDir + path.delimiter + oldPath;
  try {
    return await fn();
  } finally {
    process.env.PATH = oldPath;
    await fs.rm(shimDir, {recursive: true, force: true});
  }
}
