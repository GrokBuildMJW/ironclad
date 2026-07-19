/**
 * ink-compat — the Ink-shaped public API backed by our custom renderer (R8).
 *
 * This is the seam that lets the existing components (App/Footer/InputBox/WorkingLine) run
 * UNCHANGED on our renderer: they import `Box`/`Text`/`Static`/`useInput`/`useApp`/`useStdout`
 * and get our versions. `Box`/`Text` render the same `ink-box`/`ink-text` host primitives our
 * reconciler + layout + paint understand; the hooks come from our RenderContext; `render` is our
 * `mount`. Replacing Ink's `render()` is the whole point — so the alternate screen + app scrollback
 * + selection are on by default (decision a), unlike Ink.
 */
import {createElement, type ReactNode} from 'react';
import {mount, type MountOptions, type Instance} from './mount.js';
import {createVNode} from './vnode.js';
import {Surface, WIDE_CONT} from './surface.js';
import {Palette} from './palette.js';
import {paint} from './paint.js';
import {createConfig, attachYoga, calculate, freeYoga} from './layout.js';
import {createRoot} from './host.js';
import {RenderContext, createRenderContext, type Key} from './hooks.js';
import {FocusContext, FocusManager} from './focus.js';

export {useInput, useApp, useStdout, useStdin, type Key} from './hooks.js';
export {useFocus, useFocusManager} from './focus.js';
export {mount, type Instance, type MountOptions} from './mount.js';

type BoxProps = Record<string, unknown> & {children?: ReactNode};
type TextProps = Record<string, unknown> & {children?: ReactNode};

/**
 * Flexbox container — forwards every style prop to the `ink-box` host primitive. Defaults
 * `flexDirection` to `row` to match Ink (Yoga's own default is `column`), so components that rely
 * on Ink's row-by-default Box lay out correctly.
 */
export function Box({children, ...props}: BoxProps): React.ReactElement {
  return createElement('ink-box', {flexDirection: 'row', ...props}, children);
}

/** Styled text — forwards color/bold/etc. to the `ink-text` host primitive. */
export function Text({children, ...props}: TextProps): React.ReactElement {
  return createElement('ink-text', props, children);
}

export interface StaticProps<T> {
  items: readonly T[];
  children: (item: T, index: number) => ReactNode;
  style?: Record<string, unknown>;
}

/**
 * Static — renders a stable list of items as a column. Ink commits these once into native
 * scrollback; on the alternate screen there is no native scrollback, so they live in the tree and
 * the app ScrollBox scrolls them. Same authoring contract: `items` + a per-item render function.
 */
export function Static<T>({items, children, style}: StaticProps<T>): React.ReactElement {
  return createElement(
    'ink-box',
    {flexDirection: 'column', flexShrink: 0, ...style},
    items.map((item, i) => children(item, i)),
  );
}

/** Flexible gap that pushes siblings apart. */
export function Spacer(): React.ReactElement {
  return createElement('ink-box', {flexGrow: 1});
}

/** One or more line breaks inside a Text flow. */
export function Newline({count = 1}: {count?: number}): React.ReactElement {
  return createElement('ink-text', null, '\n'.repeat(Math.max(1, count)));
}

/** Ink-compatible `render(element, options)` → mounts on our renderer. */
export function render(element: ReactNode, options: MountOptions = {}): Instance {
  return mount(element, options);
}

// ── test helper (Ink-testing-library's render(...).lastFrame() equivalent) ──────────────────

const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>]/g;

/** Strip ANSI control sequences, leaving the visible characters. */
export function stripAnsi(s: string): string {
  return s.replace(ANSI, '');
}

/** Read a painted Surface back as visible text (spaces preserved, wide-glyph tails skipped). */
function surfaceToText(s: Surface): string {
  const lines: string[] = [];
  for (let y = 0; y < s.height; y++) {
    let line = '';
    for (let x = 0; x < s.width; x++) {
      if (s.getFlag(x, y) & WIDE_CONT) continue;
      line += s.getChar(x, y);
    }
    lines.push(line.replace(/\s+$/, ''));
  }
  while (lines.length && lines[lines.length - 1] === '') lines.pop();
  return lines.join('\n');
}

/**
 * Render `element` and read back the visible frame for assertions. Unlike scraping the ANSI byte
 * stream (which omits unchanged blanks the diff skips), this paints the committed tree into a
 * Surface and reads the cells — so spaces and layout are faithful, like a terminal would show.
 */
export function renderToString(
  element: ReactNode,
  columns = 80,
  rows = 24,
): {frame: () => string; input: (input: string, key: Key) => void; unmount: () => void} {
  const container = createVNode('ink-root');
  const palette = new Palette();
  const config = createConfig();
  const focusManager = new FocusManager();
  const stdout = {columns, rows, write: () => true} as unknown as NodeJS.WriteStream;
  const bridge = createRenderContext({stdout, exit: () => {}});
  const surface = new Surface(columns, rows);
  let frameText = '';

  const renderFrame = (): void => {
    freeYoga(container);
    attachYoga(container, config);
    calculate(container, columns);
    paint(container, surface, palette); // paint clears the surface itself
    frameText = surfaceToText(surface);
  };

  const wrapped = createElement(
    RenderContext.Provider,
    {value: bridge.value},
    createElement(FocusContext.Provider, {value: focusManager}, element),
  );
  const root = createRoot(container, renderFrame);
  root.render(wrapped);

  return {
    frame: () => frameText,
    input: (input, key) => bridge.emit(input, key),
    unmount: () => {
      root.unmount();
      freeYoga(container);
    },
  };
}
