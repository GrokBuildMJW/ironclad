/**
 * #1193/#1195 (epic #1144): the client tool-bridge carries the SAME deterministic
 * `N directories, M files` count on a shell listing as the engine (gx10.py:5144) — computed from
 * the FILESYSTEM, not by parsing output. Mirrors ack/tests/test_listing_count.py, plus the
 * bridged execute_command integration the Python suite covers engine-side.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  directoryCountHeader,
  directoryEntryNames,
  fmtCount,
  joinLikePython,
  listingCountHeaderForCommand,
  shlexSplit,
} from '../src/tools/listingCount.js';
import {runTool} from '../src/tools/runTool.js';

/** ≙ test_listing_count._mk: a temp dir with n dirs + m files. */
async function mk(nDirs: number, nFiles: number): Promise<string> {
  const d = await fs.mkdtemp(path.join(os.tmpdir(), 'ironclad-count-'));
  for (let i = 0; i < nDirs; i++) await fs.mkdir(path.join(d, `d${i}`));
  for (let i = 0; i < nFiles; i++) await fs.writeFile(path.join(d, `f${i}.txt`), '');
  return d;
}

test('directory count header comes from the fs', async () => {
  const d = await mk(2, 3);
  assert.equal(await directoryCountHeader(d), '2 directories, 3 files');
  assert.equal(await directoryCountHeader(path.join(d, 'missing')), null);
  await fs.rm(d, {recursive: true, force: true});
});

test('count header singular/plural', async () => {
  assert.equal(fmtCount(1, 1), '1 directory, 1 file');
  assert.equal(fmtCount(0, 0), '0 directories, 0 files');
  const d = await mk(1, 1);
  assert.equal(await directoryCountHeader(d), '1 directory, 1 file');
  const empty = await mk(0, 0); // through the FILESYSTEM, ≙ the Python (0,0) case
  assert.equal(await directoryCountHeader(empty), '0 directories, 0 files');
  await fs.rm(d, {recursive: true, force: true});
  await fs.rm(empty, {recursive: true, force: true});
});

test('a listing command gets the fs count', async () => {
  const d = (await mk(2, 3)).replace(/\\/g, '/'); // forward slashes are shlex-safe on Windows
  assert.equal(await listingCountHeaderForCommand(`cd ${d} && ls -la`), '2 directories, 3 files');
  assert.equal(await listingCountHeaderForCommand(`cd ${d} && Get-ChildItem`), '2 directories, 3 files');
  assert.equal(await listingCountHeaderForCommand(`cd ${d} && ls`), '2 directories, 3 files');
  assert.equal(await listingCountHeaderForCommand(`ls -la ${d}`), '2 directories, 3 files');
  await fs.rm(d, {recursive: true, force: true});
});

test('a quoted path with a space resolves (shlex parity)', async () => {
  const base = await mk(0, 0);
  const spaced = path.join(base, 'My Dir');
  await fs.mkdir(spaced);
  await fs.mkdir(path.join(spaced, 'sub'));
  await fs.writeFile(path.join(spaced, 'a.txt'), '');
  const posix = spaced.replace(/\\/g, '/');
  assert.equal(await listingCountHeaderForCommand(`cd "${posix}" && ls -la`), '1 directory, 1 file');
  assert.equal(await listingCountHeaderForCommand(`ls "${posix}"`), '1 directory, 1 file');
  assert.throws(() => shlexSplit('cd "unbalanced'), /No closing quotation/);
  await fs.rm(base, {recursive: true, force: true});
});

test('ambiguous commands get no header', async () => {
  for (const bad of [
    'ls -la | grep x', //     pipe
    'ls -R', //               recursive
    'ls -la > out.txt', //    redirect
    'echo hi', //             not a listing
    'ls a b', //              >1 path
    'cat file', //            not a listing
    'ls *.txt', //            glob
    'ls -la; rm x', //        command chain
    'ls -la && ls && ls', //  >1 &&
    'Get-ChildItem -Recurse', // PowerShell recursive
    'gci -recurse', //         recursive, lowercase (PowerShell is case-insensitive)
    'Get-ChildItem -recurse', // recursive, lowercase long form
    'gci -r', //               recursive short
    'Get-ChildItem -Exclude Real', // value-taking PS param → ambiguous target
    'gci -Filter *.txt', //    value-taking PS param
    'ls\u00a0-la', //          NBSP is NOT shlex whitespace — one token, not a listing verb
  ]) {
    assert.equal(await listingCountHeaderForCommand(bad), null, bad);
  }
  assert.deepEqual(shlexSplit('ls\u00a0-la'), ['ls\u00a0-la']); // ≙ shlex.whitespace = ' \t\r\n' only
});

test('a symlink/junction to a directory is followed (old Dirent parity limit closed)', async () => {
  const base = await mk(0, 1); // one plain file
  await fs.mkdir(path.join(base, 'real'));
  // 'junction' works unprivileged on Windows; the type argument is ignored on POSIX (plain symlink)
  await fs.symlink(path.join(base, 'real'), path.join(base, 'link'), 'junction');
  const names = await directoryEntryNames(base);
  assert.ok(names);
  assert.deepEqual([...names.dirs].sort(), ['link', 'real']); // ≙ Python Path.is_dir() follows links
  assert.deepEqual(names.files, ['f0.txt']);
  assert.equal(await directoryCountHeader(base), '2 directories, 1 file');
  await fs.rm(base, {recursive: true, force: true});
});

test('a Windows drive-relative operand passes through like ntpath.join (pure, every OS)', () => {
  // ntpath.join('F:\\base', 'C:temp') → 'C:temp' (resolves on the C: drive's own cwd);
  // path.win32.join would glue an unreadable 'F:\\base\\C:temp' and silently lose the header.
  // Pure + platform-injected so the count is identical on every CI platform (#939 guard).
  assert.equal(joinLikePython('F:\\base', 'C:temp', 'win32'), 'C:temp');
  assert.notEqual(joinLikePython('F:\\base', 'temp', 'win32'), 'temp'); // a plain name is still joined
  assert.notEqual(joinLikePython('/base', 'C:temp', 'linux'), 'C:temp'); // POSIX: C:temp is a normal name
});

test('bridged execute_command prepends the header on a real listing', async () => {
  const d = await mk(2, 3);
  const cwd = process.cwd();
  try {
    process.chdir(d);
    // the shell that actually runs differs per platform/install — pick a listing verb both resolve
    const cmd = process.platform === 'win32' ? 'Get-ChildItem' : 'ls -la';
    const out = await runTool('execute_command', {command: cmd});
    assert.ok(out.startsWith('2 directories, 3 files\n'), `missing header: ${out.slice(0, 80)}`);
    // #1202: line 2 is the machine AnswerData the SERVER renders into the localized `Answer:` reply
    const line2 = out.split('\n')[1] ?? '';
    assert.ok(line2.startsWith('AnswerData: '), `missing AnswerData: ${line2.slice(0, 60)}`);
    const data = JSON.parse(line2.slice('AnswerData: '.length)) as {dirs: string[]; files: string[]};
    assert.deepEqual([...data.dirs].sort(), ['d0', 'd1']);
    assert.deepEqual([...data.files].sort(), ['f0.txt', 'f1.txt', 'f2.txt']);
  } finally {
    process.chdir(cwd);
  }
  await fs.rm(d, {recursive: true, force: true});
});

test('bridged listing ships the machine AnswerData only up to the transport cap (mirrors the engine)', async () => {
  const big = await mk(0, 201); // > LIST_DIR_HARD_CAP (200)
  const cwd = process.cwd();
  try {
    process.chdir(big);
    const cmd = process.platform === 'win32' ? 'Get-ChildItem' : 'ls -la';
    const out = await runTool('execute_command', {command: cmd});
    assert.ok(out.startsWith('0 directories, 201 files\n'), `header: ${out.slice(0, 40)}`);
    assert.ok(!out.includes('AnswerData:'), 'over-cap must ship the header only (no name-list JSON)');
  } finally {
    process.chdir(cwd);
  }
  await fs.rm(big, {recursive: true, force: true});
});

test('list_directory uses ONE snapshot: count, markers and order agree (symlink followed)', async () => {
  const base = await mk(0, 1); // f0.txt
  await fs.mkdir(path.join(base, 'zdir'));
  await fs.symlink(path.join(base, 'zdir'), path.join(base, 'link'), 'junction');
  const out = await runTool('list_directory', {path: base});
  const lines = out.split('\n');
  assert.equal(lines[0], '2 directories, 1 file'); // link + zdir followed as dirs, from one snapshot
  // dirs (case-insensitive) before files, ≙ the engine: [D] link, [D] zdir, [F] f0.txt
  assert.deepEqual(lines.slice(1), ['[D] link', '[D] zdir', '[F] f0.txt']);
  await fs.rm(base, {recursive: true, force: true});
});

test('a failed or non-listing command gets no header', async () => {
  const d = await mk(2, 3);
  const cwd = process.cwd();
  try {
    process.chdir(d);
    const hi = await runTool('execute_command', {command: 'echo hi'});
    assert.equal(hi, 'hi'); // non-listing success — untouched
    const bad = await runTool('execute_command', {command: 'ls --definitely-not-a-flag'});
    assert.ok(!bad.startsWith('2 directories'), `header on a FAILED listing: ${bad.slice(0, 80)}`);
  } finally {
    process.chdir(cwd);
  }
  await fs.rm(d, {recursive: true, force: true});
});

test('a silent success keeps the (exit 0, no output) placeholder', async () => {
  const out = await runTool('execute_command', {command: 'cd .'});
  assert.equal(out, '(exit 0, no output)');
});
