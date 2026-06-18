/**
 * geomcache — per-node absolute geometry + contamination tracking (R4, pattern §4).
 *
 * Blit needs to know each subtree's rectangle in absolute screen coordinates to copy its cells
 * from the front buffer. Yoga stores geometry *relative to the parent*, so `build()` walks the
 * laid-out tree once, accumulating offsets, and records every element's absolute `{x,y,w,h}`.
 *
 * `contaminate(node)` marks a subtree whose front-buffer cells were overdrawn by a later sibling
 * or an absolutely-positioned overlay — those cells no longer represent the node, so it must be
 * repainted, not blitted. Contamination is per-frame; the renderer resets it each pass.
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
  private contaminated = new Set<VNode>();

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
    this.contaminated.delete(node);
  }

  /** Drop all cached geometry and contamination (e.g. on a full rebuild). */
  clear(): void {
    this.rects.clear();
    this.contaminated.clear();
  }

  /** Mark a node whose front-buffer region was overdrawn — it cannot be safely blitted. */
  contaminate(node: VNode): void {
    this.contaminated.add(node);
  }

  isContaminated(node: VNode): boolean {
    return this.contaminated.has(node);
  }

  /** Clear contamination for the next frame, keeping cached rects. */
  resetContamination(): void {
    this.contaminated.clear();
  }

  /**
   * Walk a laid-out tree and (re)record every element's absolute rect. Clears prior rects and
   * contamination first (positions change with each layout). Nodes without a Yoga node (not yet
   * laid out) are skipped along with their subtree.
   */
  build(root: VNode): void {
    this.rects.clear();
    this.contaminated.clear();
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
