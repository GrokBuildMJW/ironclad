import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {shlexSplit, buildAgentArgv, resolveLaunch, runHandover, processOne, Pool, dispatchPending, reapCoders, authorizeLaunch, DEFAULT_AGENT_CMD, MAX_CAPTURE_BYTES, spawnAgent, readCapped, FEEDBACK_MAX_BYTES, type HandoverCfg} from '../src/agent/handover.js';
import {Server, type Json} from '../src/net/server.js';

const baseCfg: HandoverCfg = {
  claudeBinOverride: null,
  agentCmdOverride: null,
  claudeEffort: 'high',
  claudePermissionMode: 'acceptEdits',
};
const envelopeFor = (template: string) => ({
  enabled: true,
  allow_list: [{bin: '*', cmd_template: template}],
});

test('spawnAgent — retains only bounded stdout and stderr tails', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-tail-'));
  const bytes = MAX_CAPTURE_BYTES + 128 * 1024;
  const res = await spawnAgent(
    [process.execPath, '-e', `process.stdout.write('x'.repeat(${bytes})); process.stderr.write('y'.repeat(${bytes}))`],
    dir,
    process.env,
    5000,
  );

  assert.equal('enoent' in res, false);
  if ('enoent' in res) return;
  assert.match(res.stdout, /^…\(truncated\)…/);
  assert.ok(Buffer.byteLength(res.stdout, 'utf8') <= MAX_CAPTURE_BYTES + Buffer.byteLength('…(truncated)…'));
  assert.ok(res.stdout.endsWith('x'.repeat(100)));
  assert.match(res.stderr, /^…\(truncated\)…/);
  assert.ok(Buffer.byteLength(res.stderr, 'utf8') <= MAX_CAPTURE_BYTES + Buffer.byteLength('…(truncated)…'));
  assert.ok(res.stderr.endsWith('y'.repeat(100)));
});

test('runHandover — a sleeping coder times out with a plain task-failed signal (not agent-unavailable)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-timeout-'));
  const sleeper = join(dir, 'sleep.cjs');
  await fs.writeFile(sleeper, 'setTimeout(() => {}, 10_000);', 'utf8');
  const template = `{bin} ${sleeper.replace(/\\/g, '/')}`;
  const logs: string[] = [];

  const result = await runHandover({
    id: 'TIMEOUT1', agent: 'OPUS', handover: 'wait', timeout_s: 0.2,
    bin: process.execPath, cmd_template: template, tooling_envelope: envelopeFor(template),
  }, dir, baseCfg, (m) => logs.push(m));

  assert.deepEqual(result, {fb: null, meta: {exit_code: null, stderr: 'timeout'}});
  assert.ok(logs.some((m) => m.includes('code-agent TIMEOUT1 timed out after 0s — killed')));
});

test('runHandover — timeout kills a spawned descendant tree', {
  skip: process.platform === 'win32' ? 'POSIX process-group proof' : false,
}, async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-tree-'));
  const sentinel = join(dir, 'descendant-wrote');
  const ready = join(dir, 'descendant-started');
  const writer = join(dir, 'writer.cjs');
  const parent = join(dir, 'parent.cjs');
  await fs.writeFile(writer,
    "const fs=require('fs'); setTimeout(() => fs.writeFileSync(process.argv[2], 'survived'), 1800);",
    'utf8');
  await fs.writeFile(parent, [
    "const {spawn}=require('child_process'); const fs=require('fs');",
    "spawn(process.execPath, [process.argv[2], process.argv[3]], {stdio:'ignore'});",
    "fs.writeFileSync(process.argv[4], 'ready'); setTimeout(() => {}, 10_000);",
  ].join(''), 'utf8');
  const template = `{bin} ${parent.replace(/\\/g, '/')} ${writer.replace(/\\/g, '/')} ${sentinel.replace(/\\/g, '/')} ${ready.replace(/\\/g, '/')}`;

  const result = await runHandover({
    id: 'TIMEOUTTREE', agent: 'OPUS', handover: 'wait', timeout_s: 1,
    bin: process.execPath, cmd_template: template, tooling_envelope: envelopeFor(template),
  }, dir, baseCfg, () => {});

  assert.deepEqual(result, {fb: null, meta: {exit_code: null, stderr: 'timeout'}});
  await fs.access(ready);
  await delay(1000);
  await assert.rejects(fs.access(sentinel));
});

test('spawnAgent — a signal-terminated coder whose close is deferred is not misclassified as timedOut (#1538)', {
  skip: process.platform === 'win32' ? 'POSIX signal + inherited-pipe race' : false,
}, async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-sig-'));
  const coder = join(dir, 'coder.cjs');
  // Spawn a grandchild that INHERITS our stdout (fd 1) and holds it open ~900ms — this defers spawnAgent's
  // `close` event. Then SIGTERM ourselves at ~150ms: signalCode='SIGTERM' + exitCode=null while `close`
  // is still pending. The 500ms timer fires in that window. Pre-#1538 the guard only checked exitCode, so
  // it declared a timeout and killed the tree; now it steps aside and `close` resolves the real result.
  await fs.writeFile(coder, [
    "const {spawn} = require('child_process');",
    "spawn(process.execPath, ['-e', 'setTimeout(() => {}, 900)'], {stdio: ['ignore', 1, 'ignore']});",
    "setTimeout(() => process.kill(process.pid, 'SIGTERM'), 150);",
  ].join('\n'), 'utf8');

  const res = await spawnAgent([process.execPath, coder], dir, process.env, 500);
  assert.equal('enoent' in res, false);
  if ('enoent' in res) return;
  assert.notEqual(res.timedOut, true);   // the real signal exit won — NOT a false timeout that discards feedback
});

test('reapCoders kills + unclaims an in-flight coder before session close (#1541)', {
  // reap works on all platforms (killProcessTree uses taskkill /T on Windows, process-group SIGTERM on POSIX)
}, async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-reap-'));
  const sleeper = join(dir, 'sleep.cjs');
  await fs.writeFile(sleeper, 'setTimeout(() => {}, 30_000);', 'utf8'); // never finishes on its own
  const template = `{bin} ${sleeper.replace(/\\/g, '/')}`;
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input) => {
    const p = new URL(String(input)).pathname;
    calls.push(p);
    const payload = p === '/pending'
      ? {pending: [{id: 'REAP1', agent: 'OPUS', handover: 'long', bin: process.execPath,
                    cmd_template: template, tooling_envelope: envelopeFor(template)}]}
      : {ok: true, status: 'pending'};
    return new Response(JSON.stringify(payload), {status: 200});
  }) as typeof fetch;
  try {
    const srv = new Server('http://engine.test');
    const jobs = await dispatchPending(srv, dir, baseCfg, new Pool(1), new Set(), () => {});
    assert.equal(jobs.length, 1); // the long-running coder is in flight
    const started = Date.now();
    await reapCoders(4000);       // the exit path kills the child + awaits its /unclaim (re-kills across the race)
    // PROMPT proves the reap actually KILLED the coder (its sleeper is 30s) rather than the /unclaim assertion
    // passing on natural completion; reapCoders returns only once no job is in flight (or the deadline hits).
    assert.ok(Date.now() - started < 3000, `reap must be prompt, took ${Date.now() - started}ms`);
    // the reaped coder RELEASED its task (POST /unclaim) instead of being left stuck in_progress after close
    assert.ok(calls.includes('/unclaim'), `expected /unclaim, saw ${calls.join(',')}`);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('shlexSplit — whitespace splits; single/double quotes group', () => {
  assert.deepEqual(shlexSplit('a b  c'), ['a', 'b', 'c']);
  assert.deepEqual(shlexSplit('a "b c" d'), ['a', 'b c', 'd']);
  assert.deepEqual(shlexSplit("x 'y z'"), ['x', 'y z']);
});

test('buildAgentArgv — {prompt} stays ONE argv element despite spaces', () => {
  const argv = buildAgentArgv(
    '{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}',
    {bin: 'claude', model: 'claude-opus-4-8', effort: 'high', permission: 'acceptEdits', prompt: 'do the thing with spaces'},
  );
  assert.deepEqual(argv, [
    'claude', '--model', 'claude-opus-4-8', '--effort', 'high',
    '--permission-mode', 'acceptEdits', '--print', 'do the thing with spaces',
  ]);
});

test('buildAgentArgv — unknown {x} left as-is; embedded placeholder substituted', () => {
  assert.deepEqual(
    buildAgentArgv('{bin} --flag={effort} {unknown}', {bin: 'c', model: 'm', effort: 'high', permission: 'p', prompt: 'pr'}),
    ['c', '--flag=high', '{unknown}'],
  );
});

test('buildAgentArgv — {feedback} substituted, {mcp} expands to multiple args (INK-HANDOVER-1)', () => {
  const argv = buildAgentArgv('{bin} {mcp} -o {feedback} --print {prompt}', {
    bin: 'codex', model: 'm', effort: 'high', permission: 'p', prompt: 'do it',
    feedback: '.ironclad/agent/feedback/x-output.md', mcp: '--mcp-config /tmp/m.json --foo bar',
  });
  assert.deepEqual(argv, [
    'codex', '--mcp-config', '/tmp/m.json', '--foo', 'bar',
    '-o', '.ironclad/agent/feedback/x-output.md', '--print', 'do it',
  ]);
});

test('buildAgentArgv — empty {mcp} contributes no args (INK-HANDOVER-1)', () => {
  assert.deepEqual(
    buildAgentArgv('{bin} {mcp} --print {prompt}', {bin: 'claude', model: 'm', effort: 'e', permission: 'p', prompt: 'go'}),
    ['claude', '--print', 'go'],
  );
});

test('resolveLaunch — the server per-agent spec drives bin/template/model/effort/permission/mcp (INK-HANDOVER-1)', () => {
  const spec = resolveLaunch(
    {agent: 'SONNET', bin: '/srv/bin/codex', cmd_template: '{bin} run {prompt}', model: 'srv-model',
     effort: 'low', permission: 'plan', permission_bypass: true, mcp: '--mcp x', mcp_env: {TOK: 'v'}},
    baseCfg,
  );
  assert.equal(spec.bin, '/srv/bin/codex');
  assert.equal(spec.template, '{bin} run {prompt}');
  assert.equal(spec.model, 'srv-model');
  assert.equal(spec.effort, 'low');
  assert.equal(spec.permission, 'plan');
  assert.equal(spec.permissionBypass, true);
  assert.equal(spec.mcp, '--mcp x');
  assert.deepEqual(spec.mcpEnv, {TOK: 'v'});
});

test('resolveLaunch — an explicit client override beats the item; defaults fill the rest (INK-HANDOVER-1)', () => {
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: '/usr/local/bin/claude', agentCmdOverride: '{bin} --print {prompt}'};
  const spec = resolveLaunch({agent: 'OPUS', bin: '/srv/ignored', cmd_template: '{bin} ignored'}, cfg);
  assert.equal(spec.bin, '/usr/local/bin/claude'); // explicit override wins over the item spec
  assert.equal(spec.template, '{bin} --print {prompt}');
  assert.equal(spec.model, 'claude-opus-4-8'); // item omits model → OPUS default
  assert.equal(spec.effort, 'high'); // cfg default
  assert.equal(spec.permission, 'acceptEdits'); // cfg default
  assert.equal(spec.permissionBypass, false);
  assert.deepEqual(spec.mcpEnv, {});
});

test('runHandover — permission bypass without per-agent capability is refused before spawn', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-permission-'));
  const result = await runHandover(
    {id: 'TPERM', agent: 'OPUS', handover: 'do x', permission: 'bypassPermissions'},
    dir, baseCfg, () => {},
  );
  assert.equal(result.fb, null);
  assert.match(result.meta.stderr ?? '', /capabilities\.permission_bypass=true/);
});

test('runHandover — coder prompt requires the first-line completion status contract', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-prompt-'));
  const writer = join(dir, 'capture-prompt.cjs');
  await fs.writeFile(writer, "require('fs').writeFileSync(process.argv[2], process.argv[3]);", 'utf8');
  const cfg: HandoverCfg = {
    ...baseCfg,
    claudeBinOverride: process.execPath,
    agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')} {feedback} {prompt}`,
  };

  const result = await runHandover(
    {id: 'TSTATUS', agent: 'OPUS', handover: 'do x', tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)},
    dir, cfg, () => {},
  );

  assert.match(result.fb ?? '', /The FIRST line of that file must be `status: done`/);
  assert.match(result.fb ?? '', /`status: blocked`/);
  assert.match(result.fb ?? '', /`status: clarification_needed`/);
});

test('authorizeLaunch — legacy disabled policy refuses', () => {
  assert.match(authorizeLaunch('anything', '{bin} {prompt}', {enabled: false, allow_list: []}) ?? '', /malformed policy/);
});

test('authorizeLaunch — absent policy refuses like Python', () => {
  assert.match(authorizeLaunch('anything', '{bin} {prompt}', undefined) ?? '', /malformed policy/);
  assert.match(authorizeLaunch('anything', '{bin} {prompt}', null) ?? '', /malformed policy/);
  assert.match(authorizeLaunch('anything', '{bin} {prompt}', {}) ?? '', /malformed policy/);
});

test('authorizeLaunch — exact template and basename authorize', () => {
  assert.equal(authorizeLaunch('/opt/bin/claude', '{bin} --print {prompt}', {
    enabled: true,
    allow_list: [{bin: 'claude', cmd_template: '{bin} --print {prompt}'}],
  }), null);
});

test('authorizeLaunch — pinned path refuses same-named impostor', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-pin-'));
  const trustedDir = join(dir, 'trusted');
  const attackerDir = join(dir, 'attacker');
  await fs.mkdir(trustedDir);
  await fs.mkdir(attackerDir);
  const trusted = join(trustedDir, process.platform === 'win32' ? 'claude.cmd' : 'claude');
  const attacker = join(attackerDir, process.platform === 'win32' ? 'claude.cmd' : 'claude');
  await fs.writeFile(trusted, '', 'utf8');
  await fs.writeFile(attacker, '', 'utf8');
  assert.equal(authorizeLaunch(trusted, '{bin} --print {prompt}', {
    enabled: true,
    allow_list: [{bin: trusted, cmd_template: '{bin} --print {prompt}'}],
  }), null);
  assert.match(authorizeLaunch(attacker, '{bin} --print {prompt}', {
    enabled: true,
    allow_list: [{bin: trusted, cmd_template: '{bin} --print {prompt}'}],
  }) ?? '', /unauthorized coder command/);
});

test('authorizeLaunch — expands portable environment paths and treats brackets literally', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-glob-'));
  const trusted = join(dir, 'claude-a');
  await fs.writeFile(trusted, '', 'utf8');
  process.env.INK_TE_DIR = dir;
  assert.equal(authorizeLaunch(trusted, '{bin} --print {prompt}', {
    enabled: true,
    allow_list: [{bin: '$INK_TE_DIR/claude-a', cmd_template: '{bin} --print {prompt}'}],
  }), null);
  assert.match(authorizeLaunch(trusted, '{bin} --print {prompt}', {
    enabled: true,
    allow_list: [{bin: '*claude-[ab]', cmd_template: '{bin} --print {prompt}'}],
  }) ?? '', /unauthorized coder command/);
  assert.match(authorizeLaunch(trusted, '{bin} --print {prompt}', {
    enabled: true,
    allow_list: [{bin: '*claude-[!a]', cmd_template: '{bin} --print {prompt}'}],
  }) ?? '', /unauthorized coder command/);
});

test('authorizeLaunch — undefined environment variables stay literal and refuse like Python expandvars', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-env-lit-'));
  const trusted = join(dir, 'claude-a');
  await fs.writeFile(trusted, '', 'utf8');
  delete process.env.INK_TE_UNDEFINED_BIN_DIR;

  assert.match(authorizeLaunch(trusted, '{bin} --print {prompt}', {
    enabled: true,
    allow_list: [{bin: '$INK_TE_UNDEFINED_BIN_DIR/claude-a', cmd_template: '{bin} --print {prompt}'}],
  }) ?? '', /unauthorized coder command/);
  assert.match(authorizeLaunch(trusted, '{bin} --print {prompt}', {
    enabled: true,
    allow_list: [{bin: '${INK_TE_UNDEFINED_BIN_DIR}/claude-a', cmd_template: '{bin} --print {prompt}'}],
  }) ?? '', /unauthorized coder command/);
  assert.match(authorizeLaunch(trusted, '{bin} --print {prompt}', {
    enabled: true,
    allow_list: [{bin: '%INK_TE_UNDEFINED_BIN_DIR%/claude-a', cmd_template: '{bin} --print {prompt}'}],
  }) ?? '', /unauthorized coder command/);
});

test('authorizeLaunch — unauthorized template is refused fail-closed', () => {
  assert.match(authorizeLaunch('python', '{bin} wrapper.py {prompt}', {
    enabled: true,
    allow_list: [{bin: 'claude', cmd_template: '{bin} --print {prompt}'}],
  }) ?? '', /unauthorized coder command/);
});

test('processOne — ALWAYS reports the run signal even when the binary is missing (INK-HANDOVER-2)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'agent-unavailable'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, agentCmdOverride: '{bin} {prompt}'};
  const item = {id: 'T1', agent: 'OPUS', handover: 'do x', bin: 'definitely-no-such-binary-xyz123', tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)};
  const claimed = new Set(['T1']);
  const logs: string[] = [];
  const ok = await processOne(srv, item, dir, cfg, claimed, m => logs.push(m));
  assert.equal(ok, false);
  assert.equal(calls.length, 1); // the run signal is POSTed despite no feedback (the #455 breaker is reachable)
  assert.equal(calls[0]?.['content'], '');
  assert.equal(calls[0]?.['exit_code'], null);
  assert.equal(calls[0]?.['stderr'], 'binary-not-found');
  assert.equal(claimed.has('T1'), false); // un-claimed for retry/failover
  // An older server object has neither method: both signal failures are logged and remain fail-soft.
  assert.ok(logs.some(m => m.includes('/claim failed (continuing)')));
  assert.ok(logs.some(m => m.includes('/unclaim failed (continuing)')));
});

test('processOne — POSTs /claim before spawn', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const marker = join(dir, 'claimed.marker');
  const checker = join(dir, 'claim-check.cjs');
  await fs.writeFile(checker,
    "const fs=require('fs'); if(!fs.existsSync(process.argv[2])) process.exit(9); fs.writeFileSync(process.argv[3], 'CLAIMED');",
    'utf8');
  const calls: Array<{path: string; body: Json}> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    const path = new URL(String(input)).pathname;
    const body = JSON.parse(String(init?.body ?? '{}')) as Json;
    calls.push({path, body});
    if (path === '/claim') await fs.writeFile(marker, 'yes', 'utf8');
    const payload = path === '/feedback'
      ? {ok: true, classification: 'ok-feedback', feedback_file: 'fb.md'}
      : {ok: true, status: path === '/claim' ? 'in_progress' : 'pending'};
    return new Response(JSON.stringify(payload), {status: 200});
  }) as typeof fetch;
  try {
    const srv = new Server('http://engine.test');
    const cfg: HandoverCfg = {
      ...baseCfg,
      claudeBinOverride: process.execPath,
      agentCmdOverride: `{bin} ${checker.replace(/\\/g, '/')} ${marker.replace(/\\/g, '/')} {feedback}`,
    };
    const claimed = new Set(['T1455']);
    const ok = await processOne(
      srv, {id: 'T1455', agent: 'OPUS', handover: 'claim first', tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)},
      dir, cfg, claimed, () => {});
    assert.equal(ok, true); // the child exits 9 unless the mocked /claim completed before spawn
    assert.deepEqual(calls.map(c => c.path), ['/claim', '/feedback']);
    assert.deepEqual(calls[0]?.body, {task_id: 'T1455', agent: 'OPUS'});
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('processOne — POSTs /unclaim after coder failure', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const calls: Array<{path: string; body: Json}> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    const path = new URL(String(input)).pathname;
    const body = JSON.parse(String(init?.body ?? '{}')) as Json;
    calls.push({path, body});
    const payload = path === '/feedback'
      ? {ok: true, classification: 'agent-failed'}
      : {ok: true, status: path === '/claim' ? 'in_progress' : 'pending'};
    return new Response(JSON.stringify(payload), {status: 200});
  }) as typeof fetch;
  try {
    const srv = new Server('http://engine.test');
    const cfg: HandoverCfg = {...baseCfg, agentCmdOverride: '{bin} {prompt}'};
    const claimed = new Set(['T1456']);
    const ok = await processOne(srv, {
      id: 'T1456', agent: 'OPUS', handover: 'fail', bin: 'definitely-no-such-binary-1455',
      tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD),
    }, dir, cfg, claimed, () => {});
    assert.equal(ok, false);
    assert.deepEqual(calls.map(c => c.path), ['/claim', '/feedback', '/unclaim']);
    assert.deepEqual(calls[2]?.body, {task_id: 'T1456'});
    assert.equal(claimed.has('T1456'), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('processOne — a nonzero exit with stderr and no feedback still reports the run signal (#455 failover)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const failer = join(dir, 'fail.cjs');
  await fs.writeFile(failer, "process.stderr.write('boom: quota exceeded'); process.exit(7);", 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'agent-unavailable'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${failer.replace(/\\/g, '/')}`};
  const claimed = new Set(['T3']);
  const ok = await processOne(srv, {id: 'T3', agent: 'OPUS', handover: 'do z', tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)}, dir, cfg, claimed, () => {});
  assert.equal(ok, false);
  assert.equal(calls.length, 1); // the #455 breaker path: budget-exhausted run is reported, not silently retried
  assert.equal(calls[0]?.['content'], '');
  assert.equal(calls[0]?.['exit_code'], 7);
  assert.match(String(calls[0]?.['stderr']), /quota exceeded/);
  assert.equal(claimed.has('T3'), false);
});

test('processOne — uploads the captured final message via the {feedback} fallback (ok path)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const writer = join(dir, 'writer.cjs');
  await fs.writeFile(writer, "require('fs').writeFileSync(process.argv[2], 'CAPTURED');", 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as unknown as Server;
  // bin = this node; template runs the writer with the {feedback} capture path as its arg
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')} {feedback}`};
  const claimed = new Set(['T2']);
  const ok = await processOne(srv, {id: 'T2', agent: 'OPUS', handover: 'do y', tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)}, dir, cfg, claimed, () => {});
  assert.equal(ok, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.['content'], 'CAPTURED'); // the {feedback} capture is read when no feedback file is written
  assert.equal(calls[0]?.['exit_code'], 0);
  // #1300: after a SUCCESSFUL upload the agent scratch is gone (handover drop + feedback + capture)
  const scratch = join(dir, '.ironclad', 'agent');
  await assert.rejects(fs.access(join(scratch, 'handovers', 'T2_OPUS.md')));
  await assert.rejects(fs.access(join(scratch, 'feedback', 'T2_OPUS-output.md')));
  await assert.rejects(fs.access(join(scratch, 'feedback', 'T2_OPUS-feedback.md')));
  await assert.rejects(fs.access(join(scratch, 'logs', 'T2_OPUS.log')));
});

test('processOne — a coder that emits its result ONLY to stdout is CAPTURED (not leaked to the terminal) and used (#1406)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const writer = join(dir, 'stdout-only.cjs');
  await fs.writeFile(writer, "process.stdout.write('STDOUT_ONLY_RESULT'); process.exit(0);", 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')}`};
  const claimed = new Set(['T1406']);
  const stdoutWrites: string[] = [];
  const originalWrite = process.stdout.write;
  process.stdout.write = ((chunk: string | Uint8Array, ...args: unknown[]) => {
    stdoutWrites.push(Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk));
    void args;
    return true;
  }) as typeof process.stdout.write;
  try {
    const ok = await processOne(srv, {id: 'T1406', agent: 'OPUS', handover: 'do stdout', tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)}, dir, cfg, claimed, () => {});
    assert.equal(ok, true);
  } finally {
    process.stdout.write = originalWrite;
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.['content'], 'STDOUT_ONLY_RESULT');
  assert.deepEqual(stdoutWrites, []);
});

test('processOne — coder stdout and stderr are written to a per-task log; client sees only stderr summary (#1406)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const writer = join(dir, 'noisy.cjs');
  await fs.writeFile(
    writer,
    [
      "process.stdout.write('CODER_STDOUT_FULL');",
      "process.stderr.write('first raw stderr line that must not be dumped\\nsecond diagnostic line\\nfinal diagnostic tail');",
      "process.exit(3);",
    ].join(''),
    'utf8',
  );
  const srv = {
    async feedback(): Promise<Json> {
      return {classification: 'task-failed'};
    },
  } as unknown as Server;
  const logs: string[] = [];
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')}`};
  const ok = await processOne(srv, {id: 'T1406LOG', agent: 'OPUS', handover: 'do noisy', tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)}, dir, cfg, new Set(['T1406LOG']), (m) => logs.push(m));
  assert.equal(ok, false);

  const coderLog = join(dir, '.ironclad', 'agent', 'logs', 'T1406LOG_OPUS.log');
  const logText = await fs.readFile(coderLog, 'utf8');
  assert.match(logText, /^# T1406LOG OPUS \(exit 3\)/);
  assert.match(logText, /## stdout\nCODER_STDOUT_FULL/);
  assert.match(logText, /## stderr\nfirst raw stderr line that must not be dumped\nsecond diagnostic line\nfinal diagnostic tail/);

  assert.ok(logs.some((m) => m.includes('OPUS stderr (')));
  assert.ok(logs.some((m) => m.includes(coderLog)));
  assert.ok(logs.some((m) => m.includes('second diagnostic line | final diagnostic tail')));
  assert.equal(logs.some((m) => m.includes('first raw stderr line that must not be dumped')), false);
  assert.equal(logs.some((m) => m.includes('CODER_STDOUT_FULL')), false);
});

test('processOne — a FAILED run keeps its scratch for diagnosis + retry (#1300)', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-'));
  const failer = join(dir, 'fail.cjs');
  await fs.writeFile(failer, "process.exit(1);", 'utf8');
  const srv = {
    async feedback(): Promise<Json> {
      return {classification: 'task-failed'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath, agentCmdOverride: `{bin} ${failer.replace(/\\/g, '/')}`};
  const ok = await processOne(srv, {id: 'T4', agent: 'OPUS', handover: 'do w', tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)}, dir, cfg, new Set(['T4']), () => {});
  assert.equal(ok, false);
  // the handover drop survives a failed run — the retry re-reads it and the operator can inspect it
  await fs.access(join(dir, '.ironclad', 'agent', 'handovers', 'T4_OPUS.md'));
});

test('runHandover — the coder is launched in the server-shipped project cwd, not the client codedir (#1307)', async () => {
  const codedir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-cd-'));   // the client's stale startup dir (scratch lives here)
  const projDir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-proj-')); // the active project's code root (server-shipped)
  const writer = join(codedir, 'pwd.cjs');
  // the writer records the CHILD's actual cwd into the {feedback} capture path → surfaces as the feedback
  await fs.writeFile(writer, 'require("fs").writeFileSync(process.argv[2], process.cwd());', 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath,
    agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')} {feedback}`};
  const item = {id: 'P1', agent: 'OPUS', handover: 'build it', cwd: projDir, tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)};
  const ok = await processOne(srv, item, codedir, cfg, new Set(['P1']), () => {});
  assert.equal(ok, true);
  const reported = await fs.realpath(String(calls[0]?.['content']).trim());
  assert.equal(reported, await fs.realpath(projDir)); // launched IN the project code root…
  assert.notEqual(reported, await fs.realpath(codedir)); // …NOT the client's codedir
  // the product tree stays clean: no agent scratch was created under the project code root
  await assert.rejects(fs.access(join(projDir, '.ironclad', 'agent', 'handovers', 'P1_OPUS.md')));
});

test('runHandover — falls back to the client codedir when the server ships no cwd (#1307 back-compat)', async () => {
  const codedir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-fb-'));
  const writer = join(codedir, 'pwd.cjs');
  await fs.writeFile(writer, 'require("fs").writeFileSync(process.argv[2], process.cwd());', 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath,
    agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')} {feedback}`};
  const ok = await processOne(srv, {id: 'P2', agent: 'OPUS', handover: 'x', tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)}, codedir, cfg, new Set(['P2']), () => {});
  assert.equal(ok, true);
  assert.equal(await fs.realpath(String(calls[0]?.['content']).trim()), await fs.realpath(codedir));
});

test('runHandover — falls back to codedir when the shipped cwd does not exist on this host (#1307 remote/sealed)', async () => {
  const codedir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-rs-'));
  const writer = join(codedir, 'pwd.cjs');
  await fs.writeFile(writer, 'require("fs").writeFileSync(process.argv[2], process.cwd());', 'utf8');
  const calls: Json[] = [];
  const srv = {
    async feedback(body: Json): Promise<Json> {
      calls.push(body);
      return {classification: 'ok-feedback', feedback_file: 'fb.md'};
    },
  } as unknown as Server;
  const cfg: HandoverCfg = {...baseCfg, claudeBinOverride: process.execPath,
    agentCmdOverride: `{bin} ${writer.replace(/\\/g, '/')} {feedback}`};
  const ghost = join(codedir, 'does-not-exist-on-this-host'); // shipped by the server but absent here
  const item = {id: 'P3', agent: 'OPUS', handover: 'x', cwd: ghost, tooling_envelope: envelopeFor(cfg.agentCmdOverride ?? DEFAULT_AGENT_CMD)};
  const ok = await processOne(srv, item, codedir, cfg, new Set(['P3']), () => {});
  assert.equal(ok, true);
  assert.equal(await fs.realpath(String(calls[0]?.['content']).trim()), await fs.realpath(codedir));
});

test('Pool — caps concurrency at max', async () => {
  const pool = new Pool(2);
  let active = 0;
  let peak = 0;
  const job = (): Promise<boolean> =>
    pool.run(async () => {
      active++;
      peak = Math.max(peak, active);
      await new Promise((r) => setTimeout(r, 20));
      active--;
      return true;
    });
  await Promise.all([job(), job(), job(), job(), job()]);
  assert.ok(peak <= 2, `peak concurrency ${peak} must be ≤ 2`);
});

test('#1543 readCapped bounds a giant coder result file', async () => {
  const dir = await fs.mkdtemp(join(tmpdir(), 'ink-ho-cap-'));
  const p = join(dir, 'big.md');
  await fs.writeFile(p, 'status: done\n' + 'x'.repeat(FEEDBACK_MAX_BYTES * 2));
  const out = await readCapped(p);
  assert.equal(out.length, FEEDBACK_MAX_BYTES);   // truncated to the cap, not the full 2x
  assert.ok(out.startsWith('status: done'));      // the first line (pipeline signal) is kept
  await fs.rm(dir, {recursive: true, force: true});
});
