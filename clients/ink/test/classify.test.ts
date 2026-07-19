import test from 'node:test';
import assert from 'node:assert/strict';
import {classify, LOCAL_COMMANDS, COMMANDS, completions, HELP_TEXT, resolveCommand, argCompletions, type Command} from '../src/commands.js';

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
  // DOCTOR (#503): /doctor is LOCAL now (GET /doctor, mirrors /health) — it is NOT forwarded to a turn.
  assert.deepEqual(classify('/doctor'), {kind: 'local', name: 'doctor', payload: 'doctor'});
});

test('classify — server slash commands (forwarded, slash already stripped in payload)', () => {
  assert.deepEqual(classify('/status'), {kind: 'server', name: 'status', payload: 'status'});
  assert.deepEqual(classify('/ls src'), {kind: 'server', name: 'ls', payload: 'ls src'});
  assert.deepEqual(classify('/design --options 2'), {kind: 'server', name: 'design', payload: 'design --options 2'});
});

test('MEM-16: registry derives LOCAL_COMMANDS + powers completions', () => {
  // the registry still covers exactly the historical local set
  for (const c of ['tasks', 'pending', 'work', 'auto', 'health', 'doctor', 'help', 'reset', 'resume', 'exit', 'quit']) {
    assert.ok(LOCAL_COMMANDS.has(c), `${c} should be local`);
  }
  // completions filter by prefix (no leading slash)
  const res = completions('res').map((c) => c.name);
  assert.deepEqual(res, ['reset', 'resume']);
  assert.equal(completions('').length, COMMANDS.filter((c) => !c.hidden).length); // empty → all visible (#1264: hidden excluded)
  assert.ok(completions('stat').some((c) => c.name === 'status' && c.scope === 'server'));
  assert.ok(completions('des').some((c) => c.name === 'design' && c.scope === 'server'));
  assert.equal(completions('zzz').length, 0); // no match
  const tool = COMMANDS.find((c) => c.name === 'tool');
  assert.equal(tool?.usage, '<name> <args|text>');
  assert.equal(tool?.desc, 'run a tool directly/deterministic, e.g. tool mpr_research <question>');
  // #1617: static cold-start guidance mirrors the implemented engine verbs and never teaches removed --type.
  const initiative = COMMANDS.find((c) => c.name === 'initiative');
  const project = COMMANDS.find((c) => c.name === 'project');
  assert.equal(initiative?.usage, 'new <name> | list | use <slug> | active | reconcile');
  assert.equal(project?.usage, 'list [--all] | new <name> [--path <dir>] | use <slug> | active | track new|use|list | delete <id> [--purge] | archive|unarchive <id>');
  assert.ok(!initiative?.usage?.includes('--type'));
  assert.ok(!project?.usage?.includes('--type'));
});

test('#452: /coders is a local command + offered in completions', () => {
  assert.deepEqual(classify('/coders'), {kind: 'local', name: 'coders', payload: 'coders'});
  assert.ok(LOCAL_COMMANDS.has('coders'), 'coders should be local');
  assert.ok(
    completions('cod').some((c) => c.name === 'coders' && c.scope === 'local'),
    'coders should appear in slash autocomplete',
  );
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

test('#934 classify — alias expands to the canonical command', () => {
  assert.deepEqual(classify('/cfg'), {kind: 'server', name: 'config', payload: 'config'});
});

test('#934 classify — a typo becomes a suggestion, never forwarded (no billed turn)', () => {
  const c = classify('/confog rag on');
  assert.equal(c.kind, 'suggest');
  assert.equal(c.name, 'config');
});

test('#934 classify — a prompt-name / unknown token still forwards (server resolves prompts)', () => {
  assert.equal(classify('/code-review diff=x').kind, 'server');
});

test('#934 classify — a destructive-verb prefix suggests, never auto-runs', () => {
  const c = classify('/proj list');   // proj → project (destructive) → suggest, not auto
  assert.equal(c.kind, 'suggest');
  assert.equal(c.name, 'project');
});

test('#934 resolveCommand — exact / alias / prefix / suggest / unknown', () => {
  const known = ['config', 'config get', 'status', 'project'];
  const A = {lg: 'lifecycle gate'};
  const unsafe = new Set(['project']);
  assert.deepEqual(resolveCommand('config', known, A, unsafe), {kind: 'exact', value: 'config'});
  assert.deepEqual(resolveCommand('lg', known, A, unsafe), {kind: 'alias', value: 'lifecycle gate'});
  assert.deepEqual(resolveCommand('stat', known, A, unsafe), {kind: 'prefix', value: 'status'});
  assert.deepEqual(resolveCommand('proj', known, A, unsafe), {kind: 'suggest', value: 'project'});
  assert.equal(resolveCommand('confog', known, A, unsafe).kind, 'suggest');
  assert.deepEqual(resolveCommand('zzzzz', known, A, unsafe), {kind: 'unknown', value: ''});
});

test('#937 argCompletions — subcommands / flags / choices from the spec', () => {
  const LC: Command = {
    name: 'lifecycle', scope: 'server', desc: 'lifecycle',
    subcommands: ['gate'],
    flags: [
      {name: '--slug', required: false, choices: [], summary: 'the slug'},
      {name: '--stages', required: false, choices: ['tests', 'reviews', 'delivery'], summary: 'stages'},
    ],
  };
  const names = (b: string): string[] => argCompletions(b, [LC]).map((c) => c.name);
  assert.deepEqual(names('/lifecycle'), []);                       // still on the verb → name completion owns it
  assert.deepEqual(names('/lifecycle '), ['gate']);                // first-arg slot → subcommand
  assert.deepEqual(names('/lifecycle ga'), ['gate']);              // typing the subcommand
  assert.deepEqual(names('/lifecycle gate --'), ['--slug', '--stages']);           // flag names
  assert.deepEqual(names('/lifecycle gate --st'), ['--stages']);   // flag prefix
  assert.deepEqual(names('/lifecycle gate --stages '), ['tests', 'reviews', 'delivery']); // choices
  assert.deepEqual(names('/lifecycle gate --stages te'), ['tests']);               // choice prefix
  assert.ok(argCompletions('/lifecycle gate --', [LC]).every((c) => c.arg === true)); // all arg-marked
  assert.deepEqual(names('/unknownverb foo'), []);                 // unknown verb → nothing
});

test('#952 classify — did-you-mean now covers the worst-offender verbs (lifecycle/fork/ace)', () => {
  // before #952 these were absent from the static COMMANDS server subset, so a typo forwarded (a billed turn)
  const lc = classify('/lifecyle gate');   // a real typo (NOT a prefix) → edit-distance 1 to 'lifecycle'
  assert.equal(lc.kind, 'suggest');
  assert.equal(lc.name, 'lifecycle');
  const fk = classify('/frk');             // edit-distance 1 to 'fork', not a prefix
  assert.equal(fk.kind, 'suggest');
  assert.equal(fk.name, 'fork');
  // correctly-typed still forwards to the server verbatim
  assert.equal(classify('/lifecycle gate').kind, 'server');
  assert.equal(classify('/ace eval').kind, 'server');
});
