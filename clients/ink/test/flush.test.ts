import test from 'node:test';
import assert from 'node:assert/strict';
import {renderPatches, withSync, BSU, ESU} from '../src/render/flush.js';
import {Palette} from '../src/render/palette.js';
import {WIDE, WIDE_CONT} from '../src/render/surface.js';
import type {Patch} from '../src/render/diff.js';

const ESC = '\x1b';

test('withSync — wraps non-empty body in BSU/ESU; empty stays empty', () => {
  assert.equal(withSync(''), '');
  assert.equal(withSync('X'), BSU + 'X' + ESU);
  assert.equal(BSU, `${ESC}[?2026h`);
  assert.equal(ESU, `${ESC}[?2026l`);
});

test('renderPatches — empty → no body', () => {
  assert.deepEqual(renderPatches([], new Palette()), {body: '', row: 0, col: 0, style: 0});
});

test('renderPatches — single run: horizontal move + glyphs, no style change', () => {
  const pal = new Palette();
  const patches: Patch[] = [{y: 0, x: 2, cells: [{cp: 0x41, style: 0, flag: 0}, {cp: 0x42, style: 0, flag: 0}]}];
  const r = renderPatches(patches, pal);
  assert.equal(r.body, `${ESC}[3GAB`); // col 3 (x=2, 1-based), then "AB"
  assert.equal(r.col, 4);
});

test('renderPatches — style transition then reset at frame end', () => {
  const pal = new Palette();
  const red = pal.intern({fg: 'red'});
  const patches: Patch[] = [{y: 0, x: 0, cells: [{cp: 0x41, style: red, flag: 0}]}];
  const r = renderPatches(patches, pal);
  // x=0 → no G move; transition 0→red, 'A', then transition red→0 at end
  assert.equal(r.body, pal.transition(0, red) + 'A' + pal.transition(red, 0));
});

test('renderPatches — wide glyph: lead written, continuation skipped, advances 2 cols', () => {
  const pal = new Palette();
  const patches: Patch[] = [
    {y: 0, x: 0, cells: [{cp: 0x4e16, style: 0, flag: WIDE}, {cp: 0, style: 0, flag: WIDE_CONT}]},
  ];
  const r = renderPatches(patches, pal);
  assert.equal(r.body, '世');
  assert.equal(r.col, 2);
});

test('renderPatches — multi-row uses a relative vertical move + column reset', () => {
  const pal = new Palette();
  const patches: Patch[] = [
    {y: 0, x: 0, cells: [{cp: 0x41, style: 0, flag: 0}]},
    {y: 2, x: 0, cells: [{cp: 0x42, style: 0, flag: 0}]},
  ];
  const r = renderPatches(patches, pal);
  // 'A' advances cursor to col 1; next patch at (2,0): down 2 rows, back to col 1, 'B'
  assert.equal(r.body, `A${ESC}[2B${ESC}[1GB`);
});
