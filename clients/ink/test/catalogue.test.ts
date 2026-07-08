import test from 'node:test';
import assert from 'node:assert/strict';
import {completions, catalogueToCommands, COMMANDS, type Command} from '../src/commands.js';
import {Server} from '../src/net/server.js';

test('#149 catalogueToCommands maps prompts to /<name> entries and skips skills', () => {
  const cmds = catalogueToCommands({
    prompts: [
      {name: 'code-review', description: 'review a diff', languages: ['en', 'de']},
      {name: 'commit-message', description: 'a commit msg', languages: ['en']},
    ],
    // skills are in the snapshot but must NOT become bare-slash completions (they aren't /<name>-invocable)
    skills: [{name: 'mpr_research', kind: 'tool', description: 'mpr'}],
  } as never);
  assert.deepEqual(cmds.map((c) => c.name), ['code-review', 'commit-message']);
  assert.ok(cmds.every((c) => c.scope === 'server'));
  assert.ok(cmds[0]!.desc.includes('en,de') && cmds[0]!.desc.startsWith('prompt'));
});

test('#149 catalogueToCommands tolerates malformed/missing input', () => {
  assert.deepEqual(catalogueToCommands({} as never), []);
  assert.deepEqual(
    catalogueToCommands({prompts: [{}, {name: 123}, {name: 'ok'}]} as never).map((c) => c.name),
    ['ok'],
  );
});

test('#149 completions merges static + dynamic; a built-in command wins on a name collision', () => {
  const extra: Command[] = [
    {name: 'code-review', scope: 'server', desc: 'prompt · en'},
    {name: 'status', scope: 'server', desc: 'collides with the built-in command'},
  ];
  const names = completions('', extra).map((c) => c.name);
  assert.ok(names.includes('code-review')); // dynamic prompt offered
  assert.equal(names.filter((n) => n === 'status').length, 1); // command wins, dynamic dropped, no dup
  // prefix filter applies to the dynamic entries too
  assert.deepEqual(completions('code-', extra).map((c) => c.name), ['code-review']);
});

test('#149 completions with no extra is the static set minus hidden (back-compat)', () => {
  const visible = COMMANDS.filter((c) => !c.hidden);
  assert.equal(completions('').length, visible.length);
});

test('#1264 a deprecated/hidden verb is never advertised in autocomplete but stays in the registry', () => {
  // hidden from the completion dropdown (no name, no prefix match)...
  assert.ok(!completions('').some((c) => c.name === 'initiative'));
  assert.ok(!completions('init').some((c) => c.name === 'initiative'));
  // ...yet it remains a real static entry — dispatchable if typed, and it satisfies the ink↔spec parity
  // coverage guard (which requires every spec verb to be present in COMMANDS).
  assert.ok(COMMANDS.some((c) => c.name === 'initiative' && c.hidden));
});

test('#149 Server.catalogue() GETs /catalogue and shapes prompts/skills', async () => {
  const s = new Server('http://h:8100');
  let seen = '';
  // override the low-level req to avoid real network
  (s as unknown as {req: (m: string, p: string) => Promise<unknown>}).req = async (m, p) => {
    seen = `${m} ${p}`;
    return {prompts: [{name: 'x'}], skills: [{name: 'y', kind: 'tool'}]};
  };
  const cat = await s.catalogue();
  assert.equal(seen, 'GET /catalogue');
  assert.deepEqual(cat.prompts, [{name: 'x'}]);
  assert.deepEqual(cat.skills, [{name: 'y', kind: 'tool'}]);
});

test('#149 Server.catalogue() defaults missing arrays to []', async () => {
  const s = new Server('http://h:8100');
  (s as unknown as {req: () => Promise<unknown>}).req = async () => ({});
  const cat = await s.catalogue();
  assert.deepEqual(cat, {prompts: [], skills: [], commands: []});   // #931: + commands
});

test('#931 catalogueToCommands generates server commands from the spec + skips local verbs', () => {
  const cmds = catalogueToCommands({
    commands: [
      {name: 'lifecycle', usage: 'gate --tree', summary: 'run the DELIVER gate', tier: 'mutating'},
      {name: 'help', usage: '', summary: 'server help', tier: 'read_only'},   // local verb → skipped
    ],
    prompts: [{name: 'code-review', description: 'review', languages: ['en']}],
  });
  const byName = new Map(cmds.map((c) => [c.name, c]));
  // the missing-from-static server verb is generated, with server scope + usage + desc
  const lc = byName.get('lifecycle') as Command;
  assert.equal(lc.scope, 'server');
  assert.equal(lc.usage, 'gate --tree');
  assert.equal(lc.desc, 'run the DELIVER gate');
  assert.ok(!byName.has('help'));                 // client-only (local) verb is not injected
  assert.ok(byName.has('code-review'));           // prompts still injected
});

test('#931 catalogueToCommands tolerates a missing commands array', () => {
  assert.deepEqual(catalogueToCommands({prompts: []}), []);
});
