/**
 * dispatch — route a mouse event through the vnode path (capture → target → bubble) (R5).
 *
 * hittest gives the topmost node under the pointer; dispatch walks the ancestor chain root→target
 * firing `on<Event>Capture` handlers, then target→root firing `on<Event>` handlers, stopping early
 * if a handler calls `stopPropagation()` — the familiar DOM model, so component handlers compose.
 * A press+release on the same node synthesizes an `onClick`.
 */
import {hitTest, type MouseEvent, type MouseAction, type MouseButton} from './hittest.js';
import type {GeomCache} from './geomcache.js';
import type {VNode} from './vnode.js';

/** Raw action → the bubbling handler prop components carry. This is the GENERIC dispatcher contract
 *  (any host can route any action to a component handler). The chat client's mount.ts intentionally
 *  consumes the wheel itself to scroll the ScrollBox and returns BEFORE dispatching, so `onWheel` is
 *  not delivered to components there by design (#503 INK-R-4) — the mapping stays for hosts that do
 *  want component-level wheel handling (it is covered by dispatch.test.ts). */
const HANDLER_FOR: Record<MouseAction, string> = {
  down: 'onMouseDown',
  up: 'onMouseUp',
  move: 'onMouseMove',
  wheelUp: 'onWheel',
  wheelDown: 'onWheel',
};

export interface DispatchEvent {
  type: MouseAction | 'click';
  x: number;
  y: number;
  button: MouseButton;
  shift: boolean;
  meta: boolean;
  ctrl: boolean;
  /** The node the event originated on (topmost under the pointer). */
  target: VNode;
  /** The node whose handler is currently running. */
  currentTarget: VNode;
  propagationStopped: boolean;
  stopPropagation: () => void;
}

function makeEvent(ev: MouseEvent, target: VNode, type: DispatchEvent['type']): DispatchEvent {
  const e: DispatchEvent = {
    type,
    x: ev.x,
    y: ev.y,
    button: ev.button,
    shift: ev.shift,
    meta: ev.meta,
    ctrl: ev.ctrl,
    target,
    currentTarget: target,
    propagationStopped: false,
    stopPropagation(): void {
      e.propagationStopped = true;
    },
  };
  return e;
}

/** Ancestor chain [target, …, root] via parent links. */
export function pathToRoot(node: VNode): VNode[] {
  const path: VNode[] = [];
  let n: VNode | null = node;
  while (n) {
    path.push(n);
    n = n.parent;
  }
  return path;
}

/**
 * Run a single event through a precomputed path ([target,…,root]): capture (root→target) then
 * bubble (target→root). `stopPropagation()` in either phase halts the remaining handlers.
 */
export function dispatchTo(path: VNode[], event: DispatchEvent, handlerName: string): void {
  const captureName = handlerName + 'Capture';
  for (let i = path.length - 1; i >= 0; i--) {
    if (event.propagationStopped) return;
    const node = path[i];
    if (!node) continue;
    event.currentTarget = node;
    const h = node.handlers[captureName];
    if (typeof h === 'function') h(event);
  }
  for (let i = 0; i < path.length; i++) {
    if (event.propagationStopped) return;
    const node = path[i];
    if (!node) continue;
    event.currentTarget = node;
    const h = node.handlers[handlerName];
    if (typeof h === 'function') h(event);
  }
}

/**
 * Stateful mouse router: hit-tests against the live geomcache, dispatches the raw event, and
 * synthesizes `onClick` when a press and release land on the same node.
 */
export class MouseDispatcher {
  private downTarget: VNode | null = null;

  constructor(
    private readonly root: VNode,
    private readonly cache: GeomCache,
  ) {}

  /** Route one mouse event; returns the hit target (or null). */
  handle(ev: MouseEvent): VNode | null {
    const target = hitTest(this.root, this.cache, ev.x, ev.y);
    if (target) {
      dispatchTo(pathToRoot(target), makeEvent(ev, target, ev.action), HANDLER_FOR[ev.action]);
    }
    if (ev.action === 'down') {
      this.downTarget = target;
    } else if (ev.action === 'up') {
      if (target && target === this.downTarget) {
        dispatchTo(pathToRoot(target), makeEvent(ev, target, 'click'), 'onClick');
      }
      this.downTarget = null;
    }
    return target;
  }
}
