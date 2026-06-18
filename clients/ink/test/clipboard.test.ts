import test from 'node:test';
import assert from 'node:assert/strict';
import {osc52, osClipboardCommands, osPasteCommands, Clipboard} from '../src/render/clipboard.js';

test('osc52 wraps base64 of the UTF-8 text in the OSC 52 escape', () => {
  assert.equal(osc52('hi'), '\x1b]52;c;aGk=\x07'); // base64('hi') = aGk=
  const u = osc52('héllo');
  assert.ok(u.startsWith('\x1b]52;c;') && u.endsWith('\x07'));
  assert.equal(u.slice('\x1b]52;c;'.length, -1), Buffer.from('héllo', 'utf8').toString('base64'));
});

test('osClipboardCommands picks the right native tool per platform', () => {
  assert.deepEqual(osClipboardCommands('win32'), [{command: 'clip', args: []}]);
  assert.deepEqual(osClipboardCommands('darwin'), [{command: 'pbcopy', args: []}]);
  const linux = osClipboardCommands('linux');
  assert.deepEqual(
    linux.map((c) => c.command),
    ['wl-copy', 'xclip', 'xsel'],
  );
});

test('osPasteCommands picks the right native reader per platform', () => {
  assert.equal(osPasteCommands('win32')[0]?.command, 'powershell');
  assert.equal(osPasteCommands('darwin')[0]?.command, 'pbpaste');
  assert.deepEqual(
    osPasteCommands('linux').map((c) => c.command),
    ['wl-paste', 'xclip', 'xsel'],
  );
});

test('Clipboard.copy emits OSC 52 and runs the native fallback', () => {
  const writes: string[] = [];
  const osCalls: Array<[string, string]> = [];
  const cb = new Clipboard((d) => writes.push(d), {
    platform: 'linux',
    osCopy: (text, platform) => osCalls.push([text, platform]),
  });
  cb.copy('select me');
  assert.equal(writes.length, 1);
  assert.equal(writes[0], osc52('select me'));
  assert.deepEqual(osCalls, [['select me', 'linux']]);
});

test('osc52 can be disabled (native only)', () => {
  const writes: string[] = [];
  let osCalled = false;
  new Clipboard((d) => writes.push(d), {osc52: false, osCopy: () => (osCalled = true), platform: 'darwin'}).copy('x');
  assert.equal(writes.length, 0, 'no OSC 52 written');
  assert.equal(osCalled, true);
});

test('native fallback can be disabled (OSC 52 only)', () => {
  const writes: string[] = [];
  let osCalled = false;
  new Clipboard((d) => writes.push(d), {osFallback: false, osCopy: () => (osCalled = true)}).copy('x');
  assert.equal(writes.length, 1, 'OSC 52 still written');
  assert.equal(osCalled, false, 'native fallback skipped');
});
