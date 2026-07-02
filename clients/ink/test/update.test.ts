import test from 'node:test';
import assert from 'node:assert/strict';
import {updatePlan, runUpdate, type Exec} from '../src/tools/update.js';

test('updatePlan — build + install from <src>/clients/ink, git pull only when asked', () => {
  const plain = updatePlan('/repo', false);
  assert.deepEqual(plain.map((s) => s.label), ['build', 'install -g']);
  for (const s of plain) assert.match(s.command, /clients[\\/]ink/, 'targets the ink package');
  assert.match(plain[1]!.command, /install -g/);

  const withPull = updatePlan('/repo', true);
  assert.deepEqual(withPull.map((s) => s.label), ['git pull', 'build', 'install -g']);
  assert.match(withPull[0]!.command, /git -C "\/repo" pull/);
});

test('runUpdate — runs every step in order, reports ok + restart note', async () => {
  const seen: string[] = [];
  const exec: Exec = async (command) => {
    seen.push(command);
    return {code: 0, out: ''};
  };
  const {ok, log} = await runUpdate('/repo', true, exec);
  assert.equal(ok, true);
  assert.equal(seen.length, 3, 'git pull + build + install all ran');
  assert.match(seen[0]!, /git .* pull/);
  assert.ok(log.some((l) => /restart ironclad/.test(l)), 'asks for a restart');
});

test('runUpdate — stops at the first failure (no later steps), ok=false', async () => {
  const seen: string[] = [];
  const exec: Exec = async (command) => {
    seen.push(command);
    return command.includes('run build') ? {code: 1, out: 'tsc error'} : {code: 0, out: ''};
  };
  const {ok, log} = await runUpdate('/repo', false, exec);
  assert.equal(ok, false);
  assert.equal(seen.length, 1, 'build failed → install never ran');
  assert.ok(log.some((l) => /FAILED/.test(l)));
  assert.ok(log.some((l) => /tsc error/.test(l)), 'surfaces the failing output');
  assert.ok(!log.some((l) => /restart ironclad/.test(l)), 'no restart note on failure');
});
