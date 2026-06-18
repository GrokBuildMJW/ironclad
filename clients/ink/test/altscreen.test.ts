import test from 'node:test';
import assert from 'node:assert/strict';
import {enterSequence, leaveSequence, AltScreen} from '../src/render/altscreen.js';

test('enterSequence enters alt buffer, hides cursor, enables mouse + paste', () => {
  const s = enterSequence();
  assert.ok(s.startsWith('\x1b[?1049h'), 'alt screen on first');
  assert.ok(s.includes('\x1b[?25l'), 'cursor hidden');
  assert.ok(s.includes('\x1b[?1000h') && s.includes('\x1b[?1002h') && s.includes('\x1b[?1006h'), 'mouse modes');
  assert.ok(s.includes('\x1b[?2004h'), 'bracketed paste');
});

test('leaveSequence is the exact reverse and leaves the alt screen last', () => {
  const s = leaveSequence();
  assert.ok(s.endsWith('\x1b[?1049l'), 'alt screen off LAST (restores main buffer + cursor)');
  assert.ok(s.includes('\x1b[?25h'), 'cursor shown again');
  assert.ok(s.includes('\x1b[?1000l') && s.includes('\x1b[?1002l') && s.includes('\x1b[?1006l'), 'mouse off');
  assert.ok(s.includes('\x1b[?2004l'), 'paste off');
  // SGR (1006) is disabled before the basic modes — reverse of enter order
  assert.ok(s.indexOf('\x1b[?1006l') < s.indexOf('\x1b[?1000l'), 'modes torn down in reverse');
});

test('every mode enabled on enter is disabled on leave', () => {
  const on = enterSequence();
  const off = leaveSequence();
  for (const mode of ['1049', '25', '1000', '1002', '1006', '2004']) {
    assert.ok(on.includes('?' + mode), `enter sets ?${mode}`);
    assert.ok(off.includes('?' + mode), `leave clears ?${mode}`);
  }
});

test('anyMotion uses 1003 instead of 1002', () => {
  const s = enterSequence({anyMotion: true});
  assert.ok(s.includes('\x1b[?1003h'), '1003 any-motion');
  assert.ok(!s.includes('\x1b[?1002h'), 'not 1002 drag');
});

test('options can suppress mouse / cursor / paste', () => {
  const s = enterSequence({mouse: false, hideCursor: false, bracketedPaste: false});
  assert.equal(s, '\x1b[?1049h', 'only the alt-screen switch remains');
});

test('AltScreen enter/leave are idempotent and track active state', () => {
  const out: string[] = [];
  const a = new AltScreen((d) => out.push(d));
  assert.equal(a.isActive, false);
  a.enter();
  a.enter(); // no-op
  assert.equal(a.isActive, true);
  assert.equal(out.length, 1, 'entered once');
  a.leave();
  a.leave(); // no-op
  assert.equal(a.isActive, false);
  assert.equal(out.length, 2, 'left once');
  assert.ok(out[0]?.startsWith('\x1b[?1049h'));
  assert.ok(out[1]?.endsWith('\x1b[?1049l'));
});
