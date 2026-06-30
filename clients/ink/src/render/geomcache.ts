/**
 * geomcache — per-node absolute geometry (R4, pattern §4).
 *
 * Hit-testing (mouse → component) needs each element's rectangle in absolute screen coordinates.
 * Yoga stores geometry *relative to the parent*, so `build()` walks the laid-out tree once,
 * accumulating offsets, and records every element's absolute `{x,y,w,h}`.
 *
 * (The contamination-tracking that fed the partial-repaint `blit` path was removed with that unwired
 * subsystem — #503 INK-R-2; the renderer full-repaints dirty subtrees.)
 */
import type {VNode} from './vnode.js';

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Minimal geometry view of a Yoga node (keeps this module decoupled + testable). */
interface YogaGeom {
  getComputedLeft(): number;
  getComputedTop(): number;
  getComputedWidth(): number;
  getComputedHeight(): number;
}

export class GeomCache {
  private rects = new Map<VNode, Rect>();

  set(node: VNode, rect: Rect): void {
    this.rects.set(node, rect);
  }

  get(node: VNode): Rect | undefined {
    return this.rects.get(node);
  }

  has(node: VNode): boolean {
    return this.rects.has(node);
  }

  delete(node: VNode): void {
    this.rects.delete(node);
  }

  /** Drop all cached geometry (e.g. on a full rebuild). */
  clear(): void {
    this.rects.clear();
  }

  /**
   * Walk a laid-out tree and (re)record every element's absolute rect. Clears prior rects first
   * (positions change with each layout). Nodes without a Yoga node (not yet laid out) are skipped
   * along with their subtree.
   */
  build(root: VNode): void {
    this.rects.clear();
    const walk = (node: VNode, ax: number, ay: number): void => {
      const yn = node.yoga as YogaGeom | null;
      if (!yn) return;
      const w = Math.round(yn.getComputedWidth());
      const h = Math.round(yn.getComputedHeight());
      const x = ax + Math.round(yn.getComputedLeft());
      const y = ay + Math.round(yn.getComputedTop());
      this.rects.set(node, {x, y, w, h});
      for (const child of node.children) {
        if (child.kind === 'element') walk(child, x, y);
      }
    };
    walk(root, 0, 0);
  }
}
