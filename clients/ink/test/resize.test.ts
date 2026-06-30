import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {terminalSize, watchResize, clearScreen} from '../src/render/resize.js';

function fakeStdout(columns?: number, rows?: number): NodeJS.WriteStream {
  const e = new EventEmitter() as unknown as NodeJS.WriteStream & EventEmitter;
  (e as {columns?: number}).columns = columns as number;
  (e as {rows?: number}).rows = rows as number;
  return e as unknown as NodeJS.WriteStream;
}

test('terminalSize reads columns/rows with 80x24 fallback', () => {
  assert.deepEqual(terminalSize(fakeStdout(120, 40)), {columns: 120, rows: 40});
  assert.deepEqual(terminalSize(fakeStdout(undefined, undefined)), {columns: 80, rows: 24});
});

test('watchResize fires with the new size and can unsubscribe', () => {
  const out = fakeStdout(80, 24);
  const sizes: Array<[number, number]> = [];
  const off = watchResize(out, (s) => sizes.push([s.columns, s.rows]));
  (out as {columns: number}).columns = 100;
  (out as {rows: number}).rows = 30;
  (out as unknown as EventEmitter).emit('resize');
  assert.deepEqual(sizes, [[100, 30]]);
  off();
  (out as unknown as EventEmitter).emit('resize');
  assert.deepEqual(sizes, [[100, 30]], 'no more events after unsubscribe');
});

test('clearScreen wipes and homes', () => {
  assert.equal(clearScreen(), '\x1b[2J\x1b[H');
});
