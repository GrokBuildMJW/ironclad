/**
 * hittest — parse SGR mouse reports and map a cell to the node under it (R5).
 *
 * With SGR mouse mode (DEC 1006) the terminal reports `ESC [ < b ; col ; row M|m`: `b` encodes the
 * button + modifier + motion/wheel bits, `col`/`row` are 1-based, and the final `M`/`m` is press
 * vs release. `parseMouse` decodes that into a 0-based `MouseEvent`. `hitTest` walks the laid-out
 * tree in paint order and returns the **topmost** element whose cached rect contains the cell —
 * later-painted nodes (deeper children, later siblings) win, matching what the user sees on top.
 */
import type {GeomCache, Rect} from './geomcache.js';
import type {VNode} from './vnode.js';

export type MouseAction = 'down' | 'up' | 'move' | 'wheelUp' | 'wheelDown';
export type MouseButton = 'left' | 'middle' | 'right' | 'none';

export interface MouseEvent {
  x: number; // 0-based column
  y: number; // 0-based row
  action: MouseAction;
  button: MouseButton;
  shift: boolean;
  meta: boolean;
  ctrl: boolean;
}

const MOUSE_RE = /^\x1b\[<(\d+);(\d+);(\d+)([Mm])$/;

const BUTTONS: MouseButton[] = ['left', 'middle', 'right', 'none'];

/** Decode an SGR mouse report, or null if `data` is not one. */
export function parseMouse(data: string): MouseEvent | null {
  const m = MOUSE_RE.exec(data);
  if (!m) return null;
  const code = parseInt(m[1] ?? '', 10);
  const x = parseInt(m[2] ?? '', 10) - 1; // 1-based → 0-based
  const y = parseInt(m[3] ?? '', 10) - 1;
  const release = m[4] === 'm';

  const shift = Boolean(code & 4);
  const meta = Boolean(code & 8);
  const ctrl = Boolean(code & 16);

  let action: MouseAction;
  let button: MouseButton;
  if (code & 64) {
    // wheel: low bit 0 = up, 1 = down
    action = code & 1 ? 'wheelDown' : 'wheelUp';
    button = 'none';
  } else {
    button = BUTTONS[code & 3] ?? 'none';
    if (code & 32) action = 'move'; // motion/drag
    else action = release ? 'up' : 'down';
  }

  return {x, y, action, button, shift, meta, ctrl};
}

function contains(r: Rect, x: number, y: number): boolean {
  return x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h;
}

/**
 * Topmost element whose cached rect contains (x, y), or null. Pre-order traversal = paint order,
 * so the last containing node visited is the one drawn on top.
 */
export function hitTest(root: VNode, cache: GeomCache, x: number, y: number): VNode | null {
  let hit: VNode | null = null;
  const walk = (node: VNode): void => {
    const r = cache.get(node);
    if (r && contains(r, x, y)) hit = node;
    for (const child of node.children) {
      if (child.kind === 'element') walk(child);
    }
  };
  walk(root);
  return hit;
}
