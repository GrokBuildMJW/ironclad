import test from 'node:test';
import assert from 'node:assert/strict';
import {classify, LOCAL_COMMANDS, COMMANDS, completions, HELP_TEXT} from '../src/commands.js';

test('classify — empty / whitespace', () => {
  assert.deepEqual(classify(''), {kind: 'empty', name: '', payload: ''});
  assert.deepEqual(classify('   '), {kind: 'empty', name: '', payload: ''});
  assert.deepEqual(classify('/'), {kind: 'empty', name: '', payload: ''});
  assert.deepEqual(classify('/   '), {kind: 'empty', name: '', payload: ''});
});

test('classify — bare exit/quit → local exit (both names normalise to "exit")', () => {
  assert.deepEqual(classify('exit'), {kind: 'local', name: 'exit', payload: 'exit'});
  assert.deepEqual(classify('QUIT'), {kind: 'local', name: 'exit', payload: 'quit'});
});

test('classify — plain text → turn (verbatim payload, trimmed)', () => {
  assert.deepEqual(classify('wer bist du?'), {kind: 'turn', name: '', payload: 'wer bist du?'});
  assert.deepEqual(classify('  hello world  '), {kind: 'turn', name: '', payload: 'hello world'});
});

test('classify — local slash commands', () => {
  for (const c of LOCAL_COMMANDS) {
    if (c === 'exit' || c === 'quit') continue; // handled by the bare branch
    const r = classify(`/${c}`);
    assert.equal(r.kind, 'local', `/${c} should be local`);
    assert.equal(r.name, c);
    assert.equal(r.payload, c);
  }
  assert.deepEqual(classify('/auto on'), {kind: 'local', name: 'auto', payload: 'auto on'});
});

test('classify — server slash commands (forwarded, slash already stripped in payload)', () => {
  assert.deepEqual(classify('/status'), {kind: 'server', name: 'status', payload: 'status'});
  assert.deepEqual(classify('/ls src'), {kind: 'server', name: 'ls', payload: 'ls src'});
  // no local /doctor — must forward as a server command
  assert.deepEqual(classify('/doctor'), {kind: 'server', name: 'doctor', payload: 'doctor'});
});

test('MEM-16: registry derives LOCAL_COMMANDS + powers completions', () => {
  // the registry still covers exactly the historical local set
  for (const c of ['tasks', 'pending', 'work', 'auto', 'health', 'help', 'reset', 'resume', 'exit', 'quit']) {
    assert.ok(LOCAL_COMMANDS.has(c), `${c} should be local`);
  }
  // completions filter by prefix (no leading slash)
  const res = completions('res').map((c) => c.name);
  assert.deepEqual(res, ['reset', 'resume']);
  assert.equal(completions('').length, COMMANDS.length); // empty → all
  assert.ok(completions('stat').some((c) => c.name === 'status' && c.scope === 'server'));
  assert.equal(completions('zzz').length, 0); // no match
});

test('#147: /prompts + /skills are server discovery commands, in completions + help', () => {
  // forwarded to the orchestrator (slash stripped), like /status
  assert.deepEqual(classify('/prompts'), {kind: 'server', name: 'prompts', payload: 'prompts'});
  assert.deepEqual(classify('/skills'), {kind: 'server', name: 'skills', payload: 'skills'});
  // autocomplete offers them
  assert.ok(completions('prompt').some((c) => c.name === 'prompts' && c.scope === 'server'));
  assert.ok(completions('skill').some((c) => c.name === 'skills' && c.scope === 'server'));
  // /help advertises them (HELP_TEXT is generated from COMMANDS)
  assert.ok(HELP_TEXT.includes('/prompts') && HELP_TEXT.includes('/skills'));
  // they are repeatable commands, never a persisted turn
  assert.notEqual(classify('/prompts').kind, 'turn');
  assert.notEqual(classify('/skills').kind, 'turn');
});

test('classify — MEM-15: !cmd → local shell (payload = command, not a turn)', () => {
  assert.deepEqual(classify('!git status'), {kind: 'local', name: 'sh', payload: 'git status'});
  assert.deepEqual(classify('  !ls -la  '), {kind: 'local', name: 'sh', payload: 'ls -la'});
  assert.deepEqual(classify('!'), {kind: 'empty', name: '', payload: ''});      // bare ! → nothing
  assert.notEqual(classify('!git status').kind, 'turn');                        // never a persisted turn
  assert.equal(classify('git status').kind, 'turn');                            // without ! → normal turn
});

test('classify — MEM-11: only conversational input is a turn (persisted); commands are not', () => {
  // real conversation → kept (persisted + rolled into the summary)
  assert.equal(classify('was hängt von X ab?').kind, 'turn');
  assert.equal(classify('implement a retry').kind, 'turn');
  // ephemeral / repeatable commands → NOT a turn → App.streamTurn won't record them
  for (const cmd of ['/status', '/clear', '/config', '/ls', '/ls src', '/health', '/tasks', '/help']) {
    assert.notEqual(classify(cmd).kind, 'turn', `${cmd} must not be a persisted turn`);
  }
});
