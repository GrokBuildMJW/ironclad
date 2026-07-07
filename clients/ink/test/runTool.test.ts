import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {runTool, winPowershellArgs, shellGuard} from '../src/tools/runTool.js';

async function tmp(): Promise<string> {
  return fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-tool-'));
}

test('write_file then read_file round-trips with exact OK string', async () => {
  const d = await tmp();
  const p = path.join(d, 'note.txt');
  const w = await runTool('write_file', {path: p, content: 'hello welt'});
  assert.equal(w, `OK: Written 10 chars to ${p}`);
  assert.equal(await runTool('read_file', {path: p}), 'hello welt');
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
  const cmd = process.platform === 'win32' ? 'Write-Output hello' : 'printf hello';
  assert.equal(await runTool('execute_command', {command: cmd}), 'hello');
  assert.equal(await runTool('totally_unknown', {}), 'ERROR: Unknown tool: totally_unknown');
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
