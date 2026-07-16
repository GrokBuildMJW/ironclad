import test from 'node:test';
import assert from 'node:assert/strict';
import {basename, join} from 'node:path';
import {defaultExec, updatePlan, validateSrcDir, runUpdate, type Exec} from '../src/tools/update.js';

test('#1522 defaultExec force-kills the tree and settles on timeout even if the child ignores SIGTERM', async () => {
  const started = Date.now();
  const nodeCommand = process.platform === 'win32' ? basename(process.execPath) : process.execPath;
  const {code, out} = await defaultExec(
    nodeCommand,
    ['-e', "process.stdout.write('ready'); process.on('SIGTERM',()=>{}); setInterval(()=>{}, 1e9)"],
    {timeoutMs: 300},
  );
  assert.equal(code, 1);
  assert.equal(out, 'ready', 'the real child must start and hold its stdout pipe open');
  assert.ok(Date.now() - started < 3000, 'defaultExec must settle within the drain bound, not hang');
});

test('updatePlan — build + install from <src>/clients/ink, git pull only when asked', () => {
  const ink = join('/repo', 'clients', 'ink');
  const plain = updatePlan('/repo', false);
  assert.deepEqual(plain, [
    {label: 'build', command: 'npm', args: ['--prefix', ink, 'run', 'build']},
    {label: 'install -g', command: 'npm', args: ['install', '-g', ink]},
  ]);

  const withPull = updatePlan('/repo', true);
  assert.deepEqual(withPull, [
    {label: 'git pull', command: 'git', args: ['-C', '/repo', 'pull', '--ff-only']},
    ...plain,
  ]);
  assert.ok(withPull.every((step) => !/["&]/u.test(step.command) && step.args.every((arg) => !/["&]/u.test(arg))));
});

test('validateSrcDir — accepts normal paths and rejects shell/cmd metacharacters', () => {
  assert.equal(validateSrcDir(String.raw`C:\Program Files (x86)\repo`), null);
  assert.equal(validateSrcDir('/home/u/x'), null);
  // a legitimate apostrophe path must NOT be refused (harmless under argv-no-shell + cmd.exe) — #1496 review
  assert.equal(validateSrcDir(String.raw`C:\Users\O'Brien\repo`), null);
  assert.equal(validateSrcDir("/home/O'Brien/repo"), null);

  for (const unsafe of [String.raw`C:\x" & calc & "`, 'a|b', 'a`b', '$(calc)', 'a;b', 'a\nb']) {
    assert.match(validateSrcDir(unsafe) ?? '', /shell\/cmd metacharacter/u, unsafe);
  }
});

test('runUpdate — refuses an unsafe source path without executing a step', async () => {
  const seen: Array<{command: string; args: string[]}> = [];
  const exec: Exec = async (command, args) => {
    seen.push({command, args});
    return {code: 0, out: ''};
  };

  const {ok, log} = await runUpdate(String.raw`C:\x" & calc & "`, true, exec);
  assert.equal(ok, false);
  assert.deepEqual(seen, []);
  assert.deepEqual(log, ['✗ /update refused: the source path contains a shell/cmd metacharacter']);
});

test('runUpdate — runs every step in order, reports ok + restart note', async () => {
  const seen: Array<{command: string; args: string[]}> = [];
  const exec: Exec = async (command, args) => {
    seen.push({command, args});
    return {code: 0, out: ''};
  };
  const {ok, log} = await runUpdate('/repo', true, exec);
  assert.equal(ok, true);
  assert.deepEqual(
    seen,
    updatePlan('/repo', true).map(({command, args}) => ({command, args})),
    'git pull + build + install all ran in order',
  );
  assert.ok(log.some((l) => /restart ironclad/.test(l)), 'asks for a restart');
});

test('runUpdate — stops at the first failure (no later steps), ok=false', async () => {
  const seen: Array<{command: string; args: string[]}> = [];
  const exec: Exec = async (command, args) => {
    seen.push({command, args});
    return args.includes('build') ? {code: 1, out: 'tsc error'} : {code: 0, out: ''};
  };
  const {ok, log} = await runUpdate('/repo', false, exec);
  assert.equal(ok, false);
  assert.equal(seen.length, 1, 'build failed → install never ran');
  assert.ok(log.some((l) => /FAILED/.test(l)));
  assert.ok(log.some((l) => /tsc error/.test(l)), 'surfaces the failing output');
  assert.ok(!log.some((l) => /restart ironclad/.test(l)), 'no restart note on failure');
});
