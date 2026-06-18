/**
 * diff — compares two Surfaces cell-by-cell within the NEW frame's damage box and emits
 * change runs ("Patches"). The damage box bounds the comparison: cells outside it were not
 * written this frame, so they are unchanged by construction. Contiguous changed cells in a
 * row become one Patch; a gap closes the run.
 *
 * Both surfaces normally share dimensions (the front/back pair). If they differ (e.g. right
 * after a resize before the back buffer is re-sized), every cell in the damage box is treated
 * as changed → a full repaint of that region, which is the safe behaviour.
 */
import type {Surface} from './surface.js';

export interface PatchCell {
  cp: number; // Unicode code point (0 on a wide-glyph continuation cell)
  style: number; // Palette id
  flag: number; // WIDE / WIDE_CONT
}

export interface Patch {
  y: number;
  x: number;
  cells: PatchCell[];
}

export function diff(prev: Surface, next: Surface): Patch[] {
  const out: Patch[] = [];
  const d = next.damage;
  if (!d) return out;
  const w = next.width;
  const dimsDiffer = prev.width !== next.width || prev.height !== next.height;

  for (let y = d.minY; y <= d.maxY; y++) {
    let run: PatchCell[] | null = null;
    let runX = 0;
    const base = y * w;
    for (let x = d.minX; x <= d.maxX; x++) {
      const i = base + x;
      const changed =
        dimsDiffer ||
        next.code[i] !== prev.code[i] ||
        next.style[i] !== prev.style[i] ||
        next.flags[i] !== prev.flags[i];
      if (changed) {
        if (!run) {
          run = [];
          runX = x;
        }
        run.push({cp: next.code[i] ?? 32, style: next.style[i] ?? 0, flag: next.flags[i] ?? 0});
      } else if (run) {
        out.push({y, x: runX, cells: run});
        run = null;
      }
    }
    if (run) out.push({y, x: runX, cells: run});
  }
  return out;
}
