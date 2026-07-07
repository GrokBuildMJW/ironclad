import test from 'node:test';
import assert from 'node:assert/strict';
import {createVNode, createTextNode, appendChild, type VNode} from '../src/render/vnode.js';
import {createConfig, attachYoga, calculate, freeYoga} from '../src/render/layout.js';
import {Surface} from '../src/render/surface.js';
import {Palette} from '../src/render/palette.js';
import {paint, paintFixed} from '../src/render/paint.js';
import {Selection} from '../src/render/selection.js';

/** Lay out a vnode tree at the given width (height auto unless given). */
function layout(root: VNode, width: number, height?: number): void {
  attachYoga(root, createConfig());
  calculate(root, width, height);
}

/** Read a horizontal run of the surface as a string. */
function row(s: Surface, y: number, x0 = 0, x1 = s.width): string {
  let out = '';
  for (let x = x0; x < x1; x++) out += s.getChar(x, y);
  return out;
}

function text(props: Record<string, unknown>, value: string): VNode {
  const t = createVNode('ink-text', props);
  appendChild(t, createTextNode(value));
  return t;
}

test('paints text at the laid-out position', () => {
  const root = createVNode('ink-root');
  appendChild(root, text({}, 'hello'));
  layout(root, 20);
  const s = new Surface(20, 3);
  paint(root, s, new Palette());
  assert.equal(row(s, 0, 0, 5), 'hello');
  freeYoga(root);
});

test('applies text style through the palette (color + bold)', () => {
  const root = createVNode('ink-root');
  appendChild(root, text({color: 'red', bold: true}, 'hi'));
  layout(root, 10);
  const s = new Surface(10, 1);
  const pal = new Palette();
  paint(root, s, pal);
  const id = s.getStyle(0, 0);
  assert.ok(id > 0, 'non-default style id assigned');
  const st = pal.get(id);
  assert.equal(st.fg, 'red');
  assert.equal(st.bold, true);
  freeYoga(root);
});

test('nested Text segments keep their own styles', () => {
  const root = createVNode('ink-root');
  const outer = createVNode('ink-text', {color: 'red'});
  appendChild(outer, createTextNode('a'));
  const inner = createVNode('ink-text', {color: 'blue', bold: true});
  appendChild(inner, createTextNode('b'));
  appendChild(outer, inner);
  appendChild(root, outer);
  layout(root, 20);
  const s = new Surface(20, 1);
  const pal = new Palette();
  paint(root, s, pal);
  assert.equal(s.getChar(0, 0), 'a');
  assert.equal(pal.get(s.getStyle(0, 0)).fg, 'red', 'outer segment stays red');
  assert.equal(s.getChar(1, 0), 'b');
  assert.equal(pal.get(s.getStyle(1, 0)).fg, 'blue', 'inner segment is blue');
  assert.equal(pal.get(s.getStyle(1, 0)).bold, true, 'inner segment is bold');
  freeYoga(root);
});

test('a nested Text inherits the parent style for props it does not set', () => {
  const root = createVNode('ink-root');
  const outer = createVNode('ink-text', {color: 'red'});
  const inner = createVNode('ink-text', {bold: true}); // no color → inherits red
  appendChild(inner, createTextNode('x'));
  appendChild(outer, inner);
  appendChild(root, outer);
  layout(root, 20);
  const s = new Surface(20, 1);
  const pal = new Palette();
  paint(root, s, pal);
  const st = pal.get(s.getStyle(0, 0));
  assert.equal(st.fg, 'red', 'inherited color');
  assert.equal(st.bold, true, 'own bold');
  freeYoga(root);
});

test('parses embedded ANSI SGR in text content into styled runs', () => {
  const root = createVNode('ink-root');
  const t = createVNode('ink-text');
  // chalk/marked-terminal style: red 'hi', reset, then '!'
  appendChild(t, createTextNode('\x1b[31mhi\x1b[0m!'));
  appendChild(root, t);
  layout(root, 20);
  const s = new Surface(20, 1);
  const pal = new Palette();
  paint(root, s, pal);
  assert.equal(s.getChar(0, 0), 'h', 'no literal escape chars leak');
  assert.equal(s.getChar(1, 0), 'i');
  assert.equal(s.getChar(2, 0), '!');
  assert.equal(pal.get(s.getStyle(0, 0)).fg, 'ansi256:1', 'SGR red applied');
  assert.equal(pal.get(s.getStyle(2, 0)).fg, undefined, 'reset restores the default');
  freeYoga(root);
});

test('parses truecolor + reset SGR and never renders the escape literally', () => {
  const root = createVNode('ink-root');
  const t = createVNode('ink-text');
  appendChild(t, createTextNode('\x1b[38;2;255;0;0mX\x1b[0m'));
  appendChild(root, t);
  layout(root, 20);
  const s = new Surface(20, 1);
  const pal = new Palette();
  paint(root, s, pal);
  assert.equal(s.getChar(0, 0), 'X');
  assert.equal(pal.get(s.getStyle(0, 0)).fg, '#ff0000', 'truecolor parsed');
  assert.equal(s.getChar(1, 0), ' ', 'trailing reset consumed, nothing literal');
  freeYoga(root);
});

test('keeps a trailing space so adjacent row segments stay separated', () => {
  const root = createVNode('ink-root');
  const rowBox = createVNode('ink-box', {flexDirection: 'row'});
  appendChild(rowBox, text({}, 'a ')); // trailing space is intentional inline spacing
  appendChild(rowBox, text({}, 'b'));
  appendChild(root, rowBox);
  layout(root, 20);
  const s = new Surface(20, 1);
  paint(root, s, new Palette());
  assert.equal(row(s, 0, 0, 3), 'a b', 'trailing space preserved between segments');
  freeYoga(root);
});

test('an open-sided box renders clean rules without corner glyphs', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box', {borderStyle: 'single', borderLeft: false, borderRight: false, width: 6, height: 3});
  appendChild(root, box);
  layout(root, 20);
  const s = new Surface(20, 4);
  paint(root, s, new Palette());
  assert.equal(row(s, 0, 0, 6), '──────', 'top is a clean rule, no corners');
  assert.equal(row(s, 2, 0, 6), '──────', 'bottom is a clean rule');
  assert.equal(s.getChar(0, 1), ' ', 'no left side drawn');
  assert.equal(s.getChar(5, 1), ' ', 'no right side drawn');
  freeYoga(root);
});

test('strokes a single border around a fixed box', () => {
  const root = createVNode('ink-root');
  appendChild(root, createVNode('ink-box', {borderStyle: 'single', width: 6, height: 3}));
  layout(root, 20);
  const s = new Surface(20, 5);
  paint(root, s, new Palette());
  assert.equal(row(s, 0, 0, 6), '┌────┐');
  assert.equal(s.getChar(0, 1), '│');
  assert.equal(s.getChar(5, 1), '│');
  assert.equal(row(s, 2, 0, 6), '└────┘');
  freeYoga(root);
});

test('honors all borderStyle variants', () => {
  const corners: Record<string, string> = {round: '╭', double: '╔', bold: '┏', classic: '+'};
  for (const [style, tl] of Object.entries(corners)) {
    const root = createVNode('ink-root');
    appendChild(root, createVNode('ink-box', {borderStyle: style, width: 4, height: 3}));
    layout(root, 10);
    const s = new Surface(10, 4);
    paint(root, s, new Palette());
    assert.equal(s.getChar(0, 0), tl, `${style} top-left corner`);
    freeYoga(root);
  }
});

test('fills backgroundColor and composites text onto it', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box', {backgroundColor: 'blue', width: 5, height: 2});
  appendChild(box, text({}, 'ab'));
  appendChild(root, box);
  layout(root, 10);
  const s = new Surface(10, 3);
  const pal = new Palette();
  paint(root, s, pal);
  // a blank cell inside the box carries the blue background
  const blank = s.getStyle(4, 1);
  assert.ok(blank > 0 && pal.get(blank).bg === 'blue', 'background filled');
  // the text drew on top AND inherited the blue background (Ink-faithful composite)
  assert.equal(s.getChar(0, 0), 'a');
  const onText = s.getStyle(0, 0);
  assert.equal(pal.get(onText).bg, 'blue', 'text inherits the box background');
  freeYoga(root);
});

test('NoSelect mask: borders are chrome, text content is selectable', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box', {borderStyle: 'single', width: 6, height: 3});
  appendChild(box, text({}, 'x'));
  appendChild(root, box);
  layout(root, 10);
  const s = new Surface(10, 4);
  const mask = paint(root, s, new Palette());
  const at = (x: number, y: number): number => mask[y * s.width + x] ?? 0;
  assert.equal(at(0, 0), 1, 'top-left border is NoSelect');
  assert.equal(at(1, 0), 1, 'top edge is NoSelect');
  assert.equal(at(0, 1), 1, 'left edge is NoSelect');
  // the text 'x' sits at the content origin (1,1) and must stay selectable
  assert.equal(s.getChar(1, 1), 'x');
  assert.equal(at(1, 1), 0, 'text content is selectable');
  freeYoga(root);
});

test('selectable={false} excludes a whole subtree from copy', () => {
  const root = createVNode('ink-root');
  const chrome = createVNode('ink-box', {selectable: false});
  appendChild(chrome, text({}, 'footer'));
  appendChild(root, chrome);
  layout(root, 10);
  const s = new Surface(10, 1);
  const mask = paint(root, s, new Palette());
  for (let x = 0; x < 6; x++) assert.equal(mask[x] ?? 0, 1, `footer col ${x} NoSelect`);
  freeYoga(root);
});

test('wide (CJK) glyphs occupy two columns', () => {
  const root = createVNode('ink-root');
  appendChild(root, text({}, '世a'));
  layout(root, 10);
  const s = new Surface(10, 1);
  paint(root, s, new Palette());
  assert.equal(s.getChar(0, 0), '世');
  assert.equal(s.getFlag(0, 0), 1, 'lead WIDE flag');
  assert.equal(s.getFlag(1, 0), 2, 'WIDE_CONT flag');
  assert.equal(s.getChar(2, 0), 'a', 'narrow glyph follows the wide pair');
  freeYoga(root);
});

test('wraps long text to the available width', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box', {width: 5});
  appendChild(box, text({}, 'one two three'));
  appendChild(root, box);
  layout(root, 20);
  const s = new Surface(20, 5);
  paint(root, s, new Palette());
  assert.equal(row(s, 0, 0, 5).trimEnd(), 'one');
  assert.equal(row(s, 1, 0, 5).trimEnd(), 'two');
  assert.equal(row(s, 2, 0, 5).trimEnd(), 'three');
  freeYoga(root);
});

test('clips text to an explicit height on the text node', () => {
  const root = createVNode('ink-root');
  appendChild(root, text({width: 5, height: 1}, 'one two three'));
  layout(root, 20);
  const s = new Surface(20, 5);
  paint(root, s, new Palette());
  assert.equal(row(s, 0, 0, 5).trimEnd(), 'one');
  assert.equal(row(s, 1, 0, 5).trim(), '', 'second wrapped line clipped by height:1');
  freeYoga(root);
});

test('yOffset scrolls content up so the bottom rows stay visible', () => {
  const root = createVNode('ink-root'); // default column → children stack vertically
  for (let i = 0; i < 5; i++) appendChild(root, text({}, 'L' + i)); // L0..L4 = 5 rows
  layout(root, 20);
  const s = new Surface(20, 3); // only 3 rows tall
  paint(root, s, new Palette(), 2); // scroll up by 2 → show L2, L3, L4
  assert.equal(row(s, 0, 0, 2), 'L2');
  assert.equal(row(s, 1, 0, 2), 'L3');
  assert.equal(row(s, 2, 0, 2), 'L4', 'bottom rows visible, top scrolled off');
  freeYoga(root);
});

test('paint fills the softWrap mask and selection re-joins the wrapped logical line', () => {
  const root = createVNode('ink-root');
  const box = createVNode('ink-box', {width: 6}); // narrow → a long word breaks across rows
  appendChild(box, text({}, 'superlongword'));
  appendChild(root, box);
  layout(root, 6);
  const s = new Surface(6, 4);
  const softWrap = new Uint8Array(s.height);
  paint(root, s, new Palette(), 0, softWrap);
  assert.equal(softWrap[0], 0, 'first row is not a continuation');
  assert.equal(softWrap[1], 1, 'wrapped row is a soft-wrap continuation');
  assert.equal(softWrap[2], 1, 'wrapped row is a soft-wrap continuation');

  const sel = new Selection();
  sel.begin(0, 0);
  sel.extend(5, 2);
  assert.equal(sel.extractText(s, {softWrap}), 'superlongword', 'wrapped word re-joined on copy');
});

test('a hard line break (\\n) is NOT marked as a soft-wrap continuation', () => {
  const root = createVNode('ink-root');
  appendChild(root, text({}, 'a\nb')); // two paragraphs → a hard break, not a wrap
  layout(root, 20);
  const s = new Surface(20, 3);
  const softWrap = new Uint8Array(s.height);
  paint(root, s, new Palette(), 0, softWrap);
  assert.equal(softWrap[1], 0, 'the second line is a real line break, kept separate on copy');
  freeYoga(root);
});

test('a cursor-flagged text declares the hardware cursor just after its last character', () => {
  const root = createVNode('ink-root');
  const t = createVNode('ink-text', {cursor: true});
  appendChild(t, createTextNode('> hi'));
  appendChild(root, t);
  layout(root, 20);
  const s = new Surface(20, 1);
  const cursor = {x: 0, y: 0, set: false};
  paint(root, s, new Palette(), 0, undefined, cursor);
  assert.equal(cursor.set, true, 'cursor declared');
  assert.equal(cursor.x, 4, 'just after "> hi" (4 cells)');
  assert.equal(cursor.y, 0);
  freeYoga(root);
});

test('no hardware cursor is declared without a cursor-flagged text', () => {
  const root = createVNode('ink-root');
  appendChild(root, text({}, 'plain'));
  layout(root, 20);
  const s = new Surface(20, 1);
  const cursor = {x: 0, y: 0, set: false};
  paint(root, s, new Palette(), 0, undefined, cursor);
  assert.equal(cursor.set, false);
  freeYoga(root);
});

test('does not paint a display:none subtree', () => {
  const root = createVNode('ink-root');
  appendChild(root, text({}, 'visible'));
  const hidden = createVNode('ink-box', {display: 'none'});
  appendChild(hidden, text({}, 'gone'));
  appendChild(root, hidden);
  layout(root, 20);
  const s = new Surface(20, 3);
  paint(root, s, new Palette());
  assert.equal(row(s, 0, 0, 7), 'visible');
  // nothing from the hidden subtree anywhere
  for (let y = 0; y < 3; y++) assert.ok(!row(s, y).includes('gone'), `row ${y} has no hidden text`);
  freeYoga(root);
});

// #1148: the input+footer stay PINNED at the viewport bottom while the transcript scrolls behind them —
// exactly the mount stamp (paint scrolled, then clearRows + paintFixed the footer at the bottom).
test('#1148 paintFixed pins the footer subtree at the viewport bottom regardless of scroll', () => {
  const root = createVNode('ink-root');
  const app = createVNode('ink-box', {flexDirection: 'column', flexGrow: 1});
  const scroll = createVNode('ink-box', {flexDirection: 'column', flexGrow: 1});
  for (let i = 0; i < 20; i++) appendChild(scroll, text({}, `line${i}`)); // taller than the viewport
  const footer = createVNode('ink-box', {flexDirection: 'column'});
  appendChild(footer, text({}, 'INPUT'));
  appendChild(app, scroll);
  appendChild(app, footer); // footer is the LAST element child (mount identifies it by that)
  appendChild(root, app);

  const H = 8; // viewport height
  layout(root, 20); // natural height → the tree is ~21 rows, taller than H
  const s = new Surface(20, H);
  // scrolled to the TOP (yOffset 0): the footer's laid-out row (~20) is far below the 8-row surface
  paint(root, s, new Palette(), 0);
  assert.ok(!row(s, H - 1).includes('INPUT'), 'without the stamp the footer is off-screen when scrolled up');

  // the mount stamp: reserve the bottom footer rows + paint the footer there, fixed
  const fh = Math.round((footer.yoga as {getComputedHeight(): number}).getComputedHeight());
  s.clearRows(H - fh, H);
  paintFixed(footer, s, new Palette(), 0, H - fh, new Uint8Array(20 * H));

  assert.match(row(s, H - 1), /INPUT/, 'the input is pinned on the bottom row');
  assert.match(row(s, 0), /line0/, 'the transcript still shows at the top (scrolled up), behind the pin');
  freeYoga(root);
});
