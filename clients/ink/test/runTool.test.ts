import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import type {ChildProcess} from 'node:child_process';
import {setTimeout as delay} from 'node:timers/promises';
import {
  isBestEffortTeardown,
  runOperatorShell,
  runTool,
  shellGuard,
  winPowershellArgs,
} from '../src/tools/runTool.js';
import {waitForChildExit} from '../src/tools/procTree.js';
import {MAX_CAPTURE_BYTES} from '../src/tools/boundedTail.js';
import {withSandboxShim} from './sandboxFixture.js';

async function tmp(): Promise<string> {
  return fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-tool-'));
}

async function processTreeCommand(d: string): Promise<{command: string; ready: string; sentinel: string}> {
  const sentinel = path.join(d, 'descendant-wrote');
  const ready = path.join(d, 'descendant-started');
  const writer = path.join(d, 'writer.mjs');
  await fs.writeFile(writer, [
    "import {writeFileSync} from 'node:fs';",
    'setTimeout(() => writeFileSync(process.argv[2], \'survived\'), 1800);',
    '',
  ].join('\n'), 'utf-8');
  const parent = path.join(d, 'parent.mjs');
  await fs.writeFile(parent, [
    "import {spawn} from 'node:child_process';",
    "import {writeFileSync} from 'node:fs';",
    'spawn(process.execPath, [process.argv[2], process.argv[3]], {stdio: \'ignore\'});',
    "writeFileSync(process.argv[4], 'ready');",
    'setTimeout(() => {}, 10_000);',
    '',
  ].join('\n'), 'utf-8');
  const command = [process.execPath, parent, writer, sentinel, ready].map((v) => JSON.stringify(v)).join(' ');
  return {command, ready, sentinel};
}

async function exists(p: string): Promise<boolean> {
  return fs.stat(p).then(() => true).catch(() => false);
}

function fakeChild(exitCode: number | null = null): ChildProcess {
  return Object.assign(new EventEmitter(), {exitCode, signalCode: null}) as ChildProcess;
}

test('#1500 isBestEffortTeardown identifies only firejail', () => {
  assert.equal(isBestEffortTeardown('firejail'), true);
  for (const backend of ['bwrap', '', 'auto']) assert.equal(isBestEffortTeardown(backend), false);
});

test('#1500 waitForChildExit resolves immediately for an already-exited child', async () => {
  const child = fakeChild(0);
  await waitForChildExit(child, 1000);
  assert.equal(child.listenerCount('exit'), 0);
  assert.equal(child.listenerCount('close'), 0);
});

test('#1500 waitForChildExit resolves on exit and clears its listeners', async () => {
  const child = fakeChild();
  const waiting = waitForChildExit(child, 1000);
  child.emit('exit', 0, null);
  await waiting;
  assert.equal(child.listenerCount('exit'), 0);
  assert.equal(child.listenerCount('close'), 0);
});

test('#1500 waitForChildExit has a bounded fallback when a child never exits', async () => {
  const child = fakeChild();
  const started = Date.now();
  await waitForChildExit(child, 20);
  assert.ok(Date.now() - started < 1000, 'bounded wait did not resolve promptly');
  assert.equal(child.listenerCount('exit'), 0);
  assert.equal(child.listenerCount('close'), 0);
});

test('write_file then read_file round-trips with exact OK string', async () => {
  const d = await tmp();
  const p = path.join(d, 'note.txt');
  const w = await runTool('write_file', {path: p, content: 'hello welt'});
  assert.equal(w, `OK: Written 10 chars to ${p}`);
  assert.equal(await runTool('read_file', {path: p}), 'hello welt');
  await fs.rm(d, {recursive: true, force: true});
});

test('runTool resolves relative paths + runs execute_command in the shipped baseCwd (#1317)', async () => {
  const base = await tmp();
  // a relative write/read resolves UNDER base (the server-shipped active project), not process.cwd()
  const w = await runTool('write_file', {path: 'sub/f.txt', content: 'hi'}, base);
  assert.equal(w, 'OK: Written 2 chars to sub/f.txt');
  assert.equal(await fs.readFile(path.join(base, 'sub', 'f.txt'), 'utf-8'), 'hi');
  assert.equal(await runTool('read_file', {path: 'sub/f.txt'}, base), 'hi');
  // execute_command runs with cwd=base → a relative shell redirect lands under base, not process.cwd()
  if (process.platform === 'win32') {
    assert.match(await runTool('execute_command', {command: 'echo cwdtest > marker.txt'}, base),
                 /refused.*Windows.*fails closed/);
    await runOperatorShell('echo cwdtest > marker.txt', base);
  } else {
    await withSandboxShim(() => runTool('execute_command', {command: 'echo cwdtest > marker.txt'}, base));
  }
  const there = await fs.stat(path.join(base, 'marker.txt')).then(() => true).catch(() => false);
  assert.ok(there, 'execute_command ran in the shipped baseCwd');
  await fs.rm(base, {recursive: true, force: true});
});

test('read-only tools fall back to a contained project root while writes stay at the code root (#1615)', async () => {
  const root = await tmp();
  const base = path.join(root, 'src');
  const vault = path.join(root, 'vault', 'demo');
  await fs.mkdir(base, {recursive: true});
  await fs.mkdir(vault, {recursive: true});
  await fs.writeFile(path.join(vault, 'handover.md'), 'vault sibling', 'utf-8');
  const relativeVault = path.join('vault', 'demo');

  assert.equal(
    await runTool('read_file', {path: path.join(relativeVault, 'handover.md')}, base, root),
    'vault sibling',
  );
  assert.match(await runTool('list_directory', {path: relativeVault}, base, root), /\[F\] handover\.md/);

  await runTool('write_file', {path: path.join('vault', 'written.md'), content: 'code-root write'}, base, root);
  assert.equal(await fs.readFile(path.join(base, 'vault', 'written.md'), 'utf-8'), 'code-root write');
  assert.equal(await exists(path.join(root, 'vault', 'written.md')), false);

  const outside = path.join(root, '..', `ironclad-outside-${path.basename(root)}.txt`);
  await fs.writeFile(outside, 'outside secret', 'utf-8');
  const traversal = path.join('vault', '..', '..', path.basename(outside));
  const refused = await runTool('read_file', {path: traversal}, base, root);
  assert.match(refused, /^ERROR: Not found:/);
  assert.doesNotMatch(refused, /outside secret/);

  await fs.rm(outside, {force: true});
  await fs.rm(root, {recursive: true, force: true});
});

test('runTool falls back to process.cwd() when baseCwd is missing on this host; absolute paths unaffected (#1317)', async () => {
  const d = await tmp();
  const ghost = path.join(d, 'does-not-exist');
  const p = path.join(d, 'x.txt'); // an ABSOLUTE path is unaffected by base either way
  const w = await runTool('write_file', {path: p, content: 'ok'}, ghost);
  assert.equal(w, `OK: Written 2 chars to ${p}`);
  assert.equal(await fs.readFile(p, 'utf-8'), 'ok');
  await fs.rm(d, {recursive: true, force: true});
});

test('read_file missing → ERROR: Not found with the raw path', async () => {
  const d = await tmp();
  const p = path.join(d, 'nope.txt');
  assert.equal(await runTool('read_file', {path: p}), `ERROR: Not found: ${p}`);
  await fs.rm(d, {recursive: true, force: true});
});

test('read_file caps >24000 chars head 16000 + marker + tail 8000', async () => {
  const d = await tmp();
  const p = path.join(d, 'big.txt');
  const content = 'a'.repeat(25000); // omitted = 25000 - 16000 - 8000 = 1000
  await runTool('write_file', {path: p, content});
  const out = await runTool('read_file', {path: p});
  assert.ok(out.startsWith('a'.repeat(16000)), 'head 16000');
  assert.ok(out.endsWith('a'.repeat(8000)), 'tail 8000');
  assert.ok(
    out.includes('[Ironclad: 1000 chars omitted — file 25000 chars, capped at 24000.'),
    'exact cap marker',
  );
  await fs.rm(d, {recursive: true, force: true});
});

test('read_file refuses a sparse file above the byte bound before reading it', async () => {
  const d = await tmp();
  const p = path.join(d, 'sparse.txt');
  const fh = await fs.open(p, 'w');
  await fh.truncate(16 * 1024 * 1024 + 1);   // #1488: the byte cap is the 16 MiB allocation ceiling
  await fh.close();
  const out = await runTool('read_file', {path: p});
  assert.match(out, /^ERROR: read_file refused: file too large — 16777217 bytes, cap 16777216 bytes$/);
  await fs.rm(d, {recursive: true, force: true});
});

// #1047: ranged / pattern read_file (mirrors gx10.py `_read_file_ranged`) ──────
test('read_file start/end returns only that 1-based inclusive line range', async () => {
  const d = await tmp();
  const p = path.join(d, 'n.txt');
  const content = Array.from({length: 50}, (_, i) => `L${i + 1}`).join('\n');
  await runTool('write_file', {path: p, content});
  assert.equal(await runTool('read_file', {path: p, start: 5, end: 7}), '[Ironclad: lines 5-7 of 50]\nL5\nL6\nL7');
  await fs.rm(d, {recursive: true, force: true});
});

test('read_file bad range falls back to the normal read (no crash)', async () => {
  const d = await tmp();
  const p = path.join(d, 'n.txt');
  await runTool('write_file', {path: p, content: Array.from({length: 20}, (_, i) => `L${i + 1}`).join('\n')});
  const out = await runTool('read_file', {path: p, start: 999}); // past EOF → fall back
  assert.ok(out.startsWith('L1') && !out.includes('[Ironclad: lines'));
  await fs.rm(d, {recursive: true, force: true});
});

test('read_file pattern reads a window of lines around the first match', async () => {
  const d = await tmp();
  const p = path.join(d, 'n.txt');
  await runTool('write_file', {path: p, content: Array.from({length: 50}, (_, i) => `L${i + 1}`).join('\n')});
  const out = await runTool('read_file', {path: p, pattern: '^L25$'});
  assert.ok(out.startsWith('[Ironclad: lines 5-45 of 50]'), 'a ±20-line window around the match');
  assert.ok(out.includes('L25'));
  await fs.rm(d, {recursive: true, force: true});
});

test('read_file no pattern match falls back to the normal read', async () => {
  const d = await tmp();
  const p = path.join(d, 'n.txt');
  await runTool('write_file', {path: p, content: 'L1\nL2\nL3'});
  assert.equal(await runTool('read_file', {path: p, pattern: 'ZZZ-NOPE'}), 'L1\nL2\nL3');
  await fs.rm(d, {recursive: true, force: true});
});

test('read_file max_chars caps the returned slice', async () => {
  const d = await tmp();
  const p = path.join(d, 'n.txt');
  await runTool('write_file', {path: p, content: 'X'.repeat(1000) + '\n' + 'Y'.repeat(1000)});
  const out = await runTool('read_file', {path: p, start: 1, end: 2, max_chars: 200});
  assert.ok(out.includes('omitted from the slice — capped at 200'));
  await fs.rm(d, {recursive: true, force: true});
});

test('read_file cap marker re-steers to search_files (not findstr)', async () => {
  const d = await tmp();
  const p = path.join(d, 'big.txt');
  await runTool('write_file', {path: p, content: 'a'.repeat(25000)});
  const out = await runTool('read_file', {path: p});
  assert.ok(out.includes('use search_files to locate the relevant lines, then read only those.'));
  assert.ok(!out.includes('findstr'));
  await fs.rm(d, {recursive: true, force: true});
});

test('write_file content.length is the char count (counts JS string length)', async () => {
  const d = await tmp();
  const p = path.join(d, 'u.txt');
  const w = await runTool('write_file', {path: p, content: 'äöü'}); // 3 code points
  assert.equal(w, `OK: Written 3 chars to ${p}`);
  await fs.rm(d, {recursive: true, force: true});
});

test('move_file / copy_file / delete_file / create_directory exact strings', async () => {
  const d = await tmp();
  const a = path.join(d, 'a.txt');
  const b = path.join(d, 'sub', 'b.txt');
  await runTool('write_file', {path: a, content: 'x'});
  assert.equal(await runTool('move_file', {source: a, destination: b}), `OK: Moved ${a} → ${b}`);
  const c = path.join(d, 'c.txt');
  assert.equal(await runTool('copy_file', {source: b, destination: c}), `OK: Copied ${b} → ${c}`);
  assert.equal(await runTool('delete_file', {path: c}), `OK: Deleted ${c}`);
  const nd = path.join(d, 'newdir');
  assert.equal(await runTool('create_directory', {path: nd}), `OK: Created ${nd}`);
  await fs.rm(d, {recursive: true, force: true});
});

test('copy_file missing source → ERROR: Source not found', async () => {
  const d = await tmp();
  const src = path.join(d, 'ghost.txt');
  const dst = path.join(d, 'out.txt');
  assert.equal(await runTool('copy_file', {source: src, destination: dst}), `ERROR: Source not found: ${src}`);
  await fs.rm(d, {recursive: true, force: true});
});

test('list_directory empty → (empty)', async () => {
  const d = await tmp();
  assert.equal(await runTool('list_directory', {path: d}), '(empty)');
  await fs.rm(d, {recursive: true, force: true});
});

test('list_directory sorts dirs first then name, [D]/[F] labels', async () => {
  const d = await tmp();
  await runTool('write_file', {path: path.join(d, 'zeta.txt'), content: '1'});
  await runTool('write_file', {path: path.join(d, 'alpha.txt'), content: '1'});
  await runTool('create_directory', {path: path.join(d, 'mid')});
  const out = await runTool('list_directory', {path: d});
  assert.equal(out, '1 directory, 2 files\n[D] mid\n[F] alpha.txt\n[F] zeta.txt'); // #1183 count header
  await fs.rm(d, {recursive: true, force: true});
});

test('execute_command captures output; unknown tool errors', async () => {
  if (process.platform === 'win32') {
    assert.match(await runTool('execute_command', {command: 'Write-Output hello'}), /refused.*Windows/);
    assert.equal(await runOperatorShell('Write-Output hello'), 'hello');
  } else {
    const d = await tmp();
    assert.equal(await withSandboxShim(() => runTool('execute_command', {command: 'printf hello'})), 'hello');
    await fs.rm(d, {recursive: true, force: true});
  }
  assert.equal(await runTool('totally_unknown', {}), 'ERROR: Unknown tool: totally_unknown');
});

test('#1489 execute_command timeout kills a spawned descendant tree', {
  skip: process.platform === 'win32',
}, async () => {
  const d = await tmp();
  try {
    const {command, ready, sentinel} = await processTreeCommand(d);
    const out = await withSandboxShim(() => runTool('execute_command', {command, timeout: 1}));
    assert.equal(out, 'ERROR: Timeout after 1s');
    assert.equal(await exists(ready), true, 'the descendant was spawned before the timeout');
    await delay(1100);
    assert.equal(await exists(sentinel), false, 'a descendant survived the timed-out process group');
  } finally {
    await fs.rm(d, {recursive: true, force: true});
  }
});

test('#1540 execute_command that exits at the deadline with a deferred close returns its output, not a false timeout', {
  skip: process.platform === 'win32',
}, async () => {
  const d = await tmp();
  try {
    const script = path.join(d, 'boundary.cjs');
    // exit (~100ms) << timeout (1s, integer — pyInt truncates a float to 0) << grandchild lifetime (~2.5s):
    // the grandchild inherits stdout (fd 1) and holds it open, deferring this process's `close` past the timer,
    // so at the deadline exitCode is set but `close` has not fired — exactly the race the guard must handle.
    await fs.writeFile(script, [
      "const {spawn} = require('child_process');",
      "process.stdout.write('BOUNDARY-OK\\n');",
      "spawn(process.execPath, ['-e', 'setTimeout(() => {}, 2500)'], {stdio: ['ignore', 1, 'ignore']});",
      "setTimeout(() => process.exit(0), 100);",
    ].join('\n'), 'utf8');
    const out = await withSandboxShim(() =>
      runTool('execute_command', {command: `${process.execPath} ${script}`, timeout: 1}));
    assert.match(out, /BOUNDARY-OK/);      // the real buffered output survived (pre-fix: false 'ERROR: Timeout')
    assert.doesNotMatch(out, /Timeout/);
  } finally {
    await fs.rm(d, {recursive: true, force: true});
  }
});

test('#1540 execute_command output is bounded — a high-volume printer cannot exhaust client memory', {
  skip: process.platform === 'win32',
}, async () => {
  const d = await tmp();
  try {
    const script = path.join(d, 'printer.cjs');
    const bytes = MAX_CAPTURE_BYTES + 200 * 1024;
    await fs.writeFile(script, `process.stdout.write('x'.repeat(${bytes}));`, 'utf8');
    const out = await runOperatorShell(`${process.execPath} ${script}`);
    assert.ok(Buffer.byteLength(out, 'utf8') <= MAX_CAPTURE_BYTES + 64,
      `output must be tail-capped, was ${Buffer.byteLength(out, 'utf8')} bytes`);
    assert.match(out, /^…\(truncated\)…/); // the rolling tail keeps only the last MAX_CAPTURE_BYTES + a marker
  } finally {
    await fs.rm(d, {recursive: true, force: true});
  }
});

test('#1489 execute_command abort kills a spawned descendant tree', {
  skip: process.platform === 'win32',
}, async () => {
  const d = await tmp();
  try {
    const {command, ready, sentinel} = await processTreeCommand(d);
    const ac = new AbortController();
    const running = withSandboxShim(() => runTool(
      'execute_command', {command, timeout: 10}, undefined, undefined, 'auto', ac.signal,
    ));
    const deadline = Date.now() + 5000;
    while (!(await exists(ready)) && Date.now() < deadline) await delay(10);
    ac.abort();
    assert.equal(await running, 'ERROR: cancelled');
    assert.equal(await exists(ready), true, 'the descendant was spawned before the abort');
    await delay(1900);
    assert.equal(await exists(sentinel), false, 'a descendant survived abort of the process group');
  } finally {
    await fs.rm(d, {recursive: true, force: true});
  }
});

test('#1489 execSandboxed passes the hardening flags to the bwrap backend', {
  skip: process.platform === 'win32',
}, async () => {
  // The shim strips the sandbox flags before exec, so behavioural tests never see them. Capture the
  // full argv the backend was invoked with and assert the isolation + tree-kill flags are actually present
  // (parity with the Python test_sandbox.py string assertion; runTool.ts had zero coverage of this argv).
  const d = await tmp();
  const log = path.join(d, 'bwrap-argv');
  process.env.IRONCLAD_BWRAP_ARGV_LOG = log;
  try {
    await withSandboxShim(() => runTool('execute_command', {command: 'printf ok'}));
    const argv = await fs.readFile(log, 'utf-8');
    for (const flag of ['--die-with-parent', '--unshare-pid', '--unshare-net', '--dev-bind', '--proc']) {
      assert.ok(argv.includes(flag), `bwrap argv is missing ${flag}: ${argv}`);
    }
  } finally {
    delete process.env.IRONCLAD_BWRAP_ARGV_LOG;
    await fs.rm(d, {recursive: true, force: true});
  }
});

test('#459 winPowershellArgs hardens WriteProgress + preserves the command', () => {
  const a = winPowershellArgs('Get-Date');
  // -Command payload prepends $ProgressPreference='SilentlyContinue' so a progress bar can never draw into
  // the renderer-owned conhost (the #447 scaling break); the original command still runs after it.
  assert.deepEqual(a.slice(0, 3), ['-NoProfile', '-NonInteractive', '-Command']);
  const payload = a[3] ?? '';
  assert.ok(payload.startsWith("$ProgressPreference='SilentlyContinue'; "));
  assert.ok(payload.endsWith('Get-Date'));
});

test('#459 shellGuard blocks remote/unbounded commands (parity with Python _shell_guard)', () => {
  // covers the local Ink paths (bridged execute_command + the /sh escape hatch) that bypass the server guard
  for (const c of ['curl https://x', 'wget http://y', 'iex (irm http://z)', 'Invoke-WebRequest https://a',
    'Start-Sleep 99', 'while ($true) { ping x }', 'Get-Content app.log -Wait', 'ping -t host']) {
    assert.notEqual(shellGuard(c), null, `should block: ${c}`);
  }
});

test('#459 shellGuard allows normal commands incl. fetch tokens in args', () => {
  for (const c of ['Get-Date', 'git status', 'ls -la', "Select-String 'wget' app.log",
    'Get-Content curl.txt', 'git clone https://github.com/u/curl']) {
    assert.equal(shellGuard(c), null, `should allow: ${c}`);
  }
});

test('#459 execute_command refuses a blocked command before running it', async () => {
  const out = await runTool('execute_command', {command: 'curl https://evil'});
  assert.ok(out.startsWith('BLOCKED'));
});
