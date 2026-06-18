/**
 * Slash-command autocomplete — pure key→action core (MEM-16(2)).
 *
 * The App renders a suggestion list (CommandMenu) whenever the input begins with `/` and has
 * matching `completions(...)`. This module owns the *behaviour* (which key does what, selection
 * wrap, the visible window for long lists) so it is unit-testable without a renderer. The App only
 * holds the selection index + applies the returned action; it never reimplements the logic.
 *
 * NB: named `menuModel` (not `commandMenu`) so it never case-collides with `CommandMenu.tsx` (the
 * view) on case-insensitive filesystems like Windows.
 */
import type {Key} from '../render/hooks.js';
import type {Command} from '../commands.js';

/** Max suggestions shown at once — longer lists (e.g. bare `/`) window around the selection. */
export const MENU_MAX_VISIBLE = 8;

export type MenuAction =
  | {type: 'none'} // not a menu key (or menu closed) → let normal input handling proceed
  | {type: 'move'; sel: number} // selection changed (↑/↓, wraps)
  | {type: 'complete'; cmd: Command} // Tab → fill the highlighted command
  | {type: 'close'}; // Esc → dismiss the menu until the buffer changes

/** Clamp a selection index into `[0, n-1]` (0 when empty). */
export function clampSel(sel: number, n: number): number {
  if (n <= 0) return 0;
  return Math.max(0, Math.min(sel, n - 1));
}

/**
 * Map a keypress to a menu action. `items` is the current completion list — empty means the menu
 * is closed, so every key is `none` (normal input). Selection wraps. Tab completes the highlight;
 * Esc closes. Anything else is `none` so typing/Enter/Backspace fall through unchanged.
 */
export function menuKey(sel: number, items: readonly Command[], key: Key): MenuAction {
  const n = items.length;
  if (n === 0) return {type: 'none'};
  const cur = clampSel(sel, n);
  if (key.upArrow) return {type: 'move', sel: (cur - 1 + n) % n};
  if (key.downArrow) return {type: 'move', sel: (cur + 1) % n};
  if (key.tab) return {type: 'complete', cmd: items[cur]!};
  if (key.escape) return {type: 'close'};
  return {type: 'none'};
}

/** Completing fills `/<name>` plus a trailing space when the command takes an argument (usage), so
 *  the caret is ready for it; no-arg commands get no trailing space. */
export function completionText(cmd: Command): string {
  return `/${cmd.name}${cmd.usage ? ' ' : ''}`;
}

/** The visible slice of a (possibly long) list, kept centred on `sel`. Returns the slice and its
 *  offset into the full list so the caller can highlight the right row + count what's hidden. */
export function menuWindow<T>(items: readonly T[], sel: number, max: number = MENU_MAX_VISIBLE): {slice: T[]; offset: number} {
  if (items.length <= max) return {slice: [...items], offset: 0};
  const s = clampSel(sel, items.length);
  const offset = Math.max(0, Math.min(s - Math.floor(max / 2), items.length - max));
  return {slice: items.slice(offset, offset + max), offset};
}
