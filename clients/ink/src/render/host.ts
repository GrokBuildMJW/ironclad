/**
 * host — the react-reconciler@0.33 HostConfig that bridges React onto our vnode tree (R3c).
 *
 * React mutations (create/append/insert/remove/update) are translated into vnode operations.
 * Each commit's prop set is split: `on*` functions go to `vnode.handlers` (via setHandler — no
 * dirty), everything else to `vnode.props` (via updateProps — dirty). `resetAfterCommit` fires
 * a per-container commit hook so the renderer (mount.ts) can re-layout/paint/flush. React 19
 * adds many required HostConfig methods (suspend/transition/priority) — all safe no-ops here.
 *
 * Signatures verified against @types/react-reconciler@0.33.
 */
import Reconciler from 'react-reconciler';
// Explicit `.js` so the EMITTED ESM resolves under Node (the bare subpath has no exports map);
// tsx/Bundler resolves it too. Node imports the named CJS exports via cjs-module-lexer.
import {ConcurrentRoot, DefaultEventPriority} from 'react-reconciler/constants.js';
import {createContext, type ReactNode} from 'react';
import {
  createVNode,
  createTextNode,
  appendChild as vAppend,
  insertBefore as vInsert,
  removeChild as vRemove,
  updateProps,
  setHandler,
  removeHandler,
  setText,
  type VNode,
  type TextNode,
  type ElementType,
  type Handler,
  type HostNode,
} from './vnode.js';

type Props = Record<string, unknown>;

/** Split a React prop bag into visual props and `on*` event handlers. */
function splitProps(raw: Props): {props: Props; handlers: Record<string, Handler>} {
  const props: Props = {};
  const handlers: Record<string, Handler> = {};
  for (const [k, v] of Object.entries(raw)) {
    if (k === 'children') continue;
    if (k.startsWith('on') && typeof v === 'function') handlers[k] = v as Handler;
    else props[k] = v;
  }
  return {props, handlers};
}

/** Per-container commit hook: resetAfterCommit → re-render. WeakMap so containers don't leak. */
const commitHooks = new WeakMap<VNode, () => void>();
const NO_CONTEXT: object = {};
let currentPriority: number = DefaultEventPriority;

const hostConfig = {
  supportsMutation: true,
  supportsPersistence: false,
  supportsHydration: false,
  isPrimaryRenderer: true,
  noTimeout: -1 as const,
  scheduleTimeout: setTimeout,
  cancelTimeout: clearTimeout,

  createInstance(type: string, raw: Props): VNode {
    const {props, handlers} = splitProps(raw);
    const node = createVNode(type as ElementType, props);
    Object.assign(node.handlers, handlers);
    return node;
  },
  createTextInstance(text: string): TextNode {
    return createTextNode(text);
  },
  appendInitialChild(parent: VNode, child: HostNode): void {
    vAppend(parent, child);
  },
  finalizeInitialChildren(): boolean {
    return false;
  },
  shouldSetTextContent(): boolean {
    return false;
  },
  getRootHostContext(): object {
    return NO_CONTEXT;
  },
  getChildHostContext(parent: object): object {
    return parent;
  },
  getPublicInstance(inst: VNode | TextNode): VNode | TextNode {
    return inst;
  },
  prepareForCommit(): null {
    return null;
  },
  resetAfterCommit(container: VNode): void {
    commitHooks.get(container)?.();
  },
  preparePortalMount(): void {},
  getInstanceFromNode(): null {
    return null;
  },
  beforeActiveInstanceBlur(): void {},
  afterActiveInstanceBlur(): void {},
  prepareScopeUpdate(): void {},
  getInstanceFromScope(): null {
    return null;
  },
  detachDeletedInstance(): void {},

  // ── mutation ──────────────────────────────────────────────────────────────
  appendChild(parent: VNode, child: HostNode): void {
    vAppend(parent, child);
  },
  appendChildToContainer(container: VNode, child: HostNode): void {
    vAppend(container, child);
  },
  insertBefore(parent: VNode, child: HostNode, before: HostNode): void {
    vInsert(parent, child, before);
  },
  insertInContainerBefore(container: VNode, child: HostNode, before: HostNode): void {
    vInsert(container, child, before);
  },
  removeChild(parent: VNode, child: HostNode): void {
    vRemove(parent, child);
  },
  removeChildFromContainer(container: VNode, child: HostNode): void {
    vRemove(container, child);
  },
  resetTextContent(): void {},
  commitTextUpdate(textInstance: TextNode, _old: string, next: string): void {
    setText(textInstance, next);
  },
  commitMount(): void {},
  commitUpdate(instance: VNode, _type: string, _prev: Props, nextRaw: Props): void {
    const {props, handlers} = splitProps(nextRaw);
    updateProps(instance, props);
    for (const k of Object.keys(instance.handlers)) if (!(k in handlers)) removeHandler(instance, k);
    for (const [k, fn] of Object.entries(handlers)) setHandler(instance, k, fn);
  },
  hideInstance(inst: VNode): void {
    updateProps(inst, {...inst.props, display: 'none'});
  },
  hideTextInstance(t: TextNode): void {
    setText(t, '');
  },
  unhideInstance(inst: VNode, raw: Props): void {
    updateProps(inst, splitProps(raw).props);
  },
  unhideTextInstance(t: TextNode, text: string): void {
    setText(t, text);
  },
  clearContainer(container: VNode): void {
    while (container.children.length) vRemove(container, container.children[0] as HostNode);
  },

  // ── React 19 required methods — safe no-ops / defaults ─────────────────────
  NotPendingTransition: null,
  HostTransitionContext: createContext<unknown>(null),
  resetFormInstance(): void {},
  requestPostPaintCallback(): void {},
  shouldAttemptEagerTransition(): boolean {
    return false;
  },
  trackSchedulerEvent(): void {},
  resolveEventType(): null {
    return null;
  },
  resolveEventTimeStamp(): number {
    return -1;
  },
  maySuspendCommit(): boolean {
    return false;
  },
  startSuspendingCommit(): void {},
  suspendInstance(): void {},
  waitForCommitToBeReady(): null {
    return null;
  },
  setCurrentUpdatePriority(p: number): void {
    currentPriority = p;
  },
  getCurrentUpdatePriority(): number {
    return currentPriority;
  },
  resolveUpdatePriority(): number {
    return currentPriority || DefaultEventPriority;
  },
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const reconciler = Reconciler(hostConfig as any);

/**
 * Flush a callback's React updates synchronously. Input handlers run outside React's batched
 * context, and a ConcurrentRoot would otherwise schedule their state updates asynchronously — so a
 * keystroke wouldn't redraw until a later tick. Wrapping input handling in this makes the UI update
 * immediately (and deterministically, for tests).
 */
export function flushSync<T>(fn: () => T): T {
  return reconciler.flushSyncFromReconciler(fn);
}

export interface Root {
  render(element: ReactNode): void;
  unmount(): void;
}

/** Create a React root bound to a container vnode. `commitHook` runs after each commit. */
export function createRoot(container: VNode, commitHook?: () => void): Root {
  if (commitHook) commitHooks.set(container, commitHook);
  const root = reconciler.createContainer(
    container,
    ConcurrentRoot,
    null,
    false,
    null,
    '',
    (e: Error) => {
      throw e;
    },
    () => {},
    () => {},
    () => {},
  );
  return {
    render(element: ReactNode): void {
      reconciler.flushSyncFromReconciler(() => reconciler.updateContainer(element, root, null, null));
    },
    unmount(): void {
      reconciler.flushSyncFromReconciler(() => reconciler.updateContainer(null, root, null, null));
    },
  };
}
