/**
 * vnode — the host tree our custom reconciler builds (R3a).
 *
 * Each element node carries its visual `props` (style/attributes), its event `handlers` kept
 * SEPARATE from props, its `children`, a `dirty` flag, and a slot for the Yoga node (assigned
 * later by layout.ts; null here so this module is testable without the Yoga WASM init).
 *
 * Why handlers are separate (concept-spec §3): a React handler's identity changes on every
 * render unless wrapped in useCallback. If handlers lived in `props`, every render would mark
 * the node dirty → a full repaint. Storing them apart lets the reconciler update a handler
 * without dirtying the node. `markDirty` propagates UP to the root but never into sibling
 * subtrees — so a deep change repaints its path, not the whole tree.
 */

export type ElementType = 'ink-root' | 'ink-box' | 'ink-text';
export type Handler = (...args: unknown[]) => void;

export interface TextNode {
  kind: 'text';
  value: string;
  parent: VNode | null;
}

export interface VNode {
  kind: 'element';
  type: ElementType;
  props: Record<string, unknown>;
  handlers: Record<string, Handler>;
  children: HostNode[];
  parent: VNode | null;
  dirty: boolean;
  yoga: unknown | null; // YogaNode, assigned by layout.ts
}

export type HostNode = VNode | TextNode;

export function createVNode(type: ElementType, props: Record<string, unknown> = {}): VNode {
  return {kind: 'element', type, props, handlers: {}, children: [], parent: null, dirty: true, yoga: null};
}

export function createTextNode(value: string): TextNode {
  return {kind: 'text', value, parent: null};
}

/** Mark a node dirty and propagate the flag up to the root. Siblings are untouched. */
export function markDirty(node: HostNode): void {
  let n: HostNode | null = node;
  while (n) {
    if (n.kind === 'element') n.dirty = true;
    n = n.parent;
  }
}

/** Clear a single node's dirty flag (done during render once it's been drawn). */
export function clearDirty(node: VNode): void {
  node.dirty = false;
}

export function appendChild(parent: VNode, child: HostNode): void {
  if (child.parent) removeChild(child.parent, child);
  child.parent = parent;
  parent.children.push(child);
  markDirty(parent);
}

export function insertBefore(parent: VNode, child: HostNode, before: HostNode): void {
  if (child.parent) removeChild(child.parent, child);
  const i = parent.children.indexOf(before);
  child.parent = parent;
  if (i < 0) parent.children.push(child);
  else parent.children.splice(i, 0, child);
  markDirty(parent);
}

export function removeChild(parent: VNode, child: HostNode): void {
  const i = parent.children.indexOf(child);
  if (i >= 0) parent.children.splice(i, 1);
  child.parent = null;
  markDirty(parent);
}

/** Replace an element's visual props and mark it dirty (a prop change is a visual change). */
export function updateProps(node: VNode, props: Record<string, unknown>): void {
  node.props = props;
  markDirty(node);
}

/** Set/remove an event handler WITHOUT dirtying the node (handler identity churns per render). */
export function setHandler(node: VNode, name: string, fn: Handler): void {
  node.handlers[name] = fn;
}

export function removeHandler(node: VNode, name: string): void {
  delete node.handlers[name];
}

/** Update a text node's value and dirty its parent (text is a visual change). */
export function setText(node: TextNode, value: string): void {
  node.value = value;
  if (node.parent) markDirty(node.parent);
}
