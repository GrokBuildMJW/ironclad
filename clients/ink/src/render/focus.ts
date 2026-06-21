/**
 * focus — keyboard focus order + the useFocus / useFocusManager hooks (R5).
 *
 * The FocusManager keeps focusable ids in registration order (≈ mount order ≈ visual order, Ink's
 * model), tracks the active one, and cycles with Tab / Shift+Tab. `useFocus` registers a component
 * as focusable and re-renders it when focus moves; `useFocusManager` exposes the controls. The
 * actual Tab keypress is turned into focusNext/Previous by `handleFocusKey`, which R7 wires into the
 * input bridge. Pure manager logic is React-free so it is fully unit-testable.
 */
import {createContext, useContext, useEffect, useId, useMemo, useReducer} from 'react';
import type {Key} from './hooks.js';

interface Focusable {
  id: string;
  isActive: boolean;
}

export class FocusManager {
  private focusables: Focusable[] = [];
  private activeId: string | null = null;
  private enabled = true;
  private listeners = new Set<() => void>();

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private emit(): void {
    for (const l of [...this.listeners]) l();
  }

  /** Add a focusable (idempotent). The first active focusable auto-focuses. */
  register(id: string, isActive = true): void {
    const existing = this.focusables.find((f) => f.id === id);
    if (existing) existing.isActive = isActive;
    else this.focusables.push({id, isActive});
    if (this.activeId === null && isActive && this.enabled) {
      this.activeId = id;
      this.emit();
    }
  }

  unregister(id: string): void {
    this.focusables = this.focusables.filter((f) => f.id !== id);
    if (this.activeId === id) {
      this.activeId = null;
      this.step(1); // move focus to the next survivor (or null)
    }
  }

  setActive(id: string, isActive: boolean): void {
    const f = this.focusables.find((x) => x.id === id);
    if (f) f.isActive = isActive;
  }

  isFocused(id: string): boolean {
    return this.enabled && this.activeId === id;
  }

  get active(): string | null {
    return this.enabled ? this.activeId : null;
  }

  /** True when at least one active focusable is registered — Tab only navigates focus then. */
  hasFocusables(): boolean {
    return this.enabled && this.focusables.some((f) => f.isActive);
  }

  focus(id: string): void {
    if (this.focusables.some((f) => f.id === id && f.isActive)) {
      this.activeId = id;
      this.emit();
    }
  }

  enable(): void {
    if (this.enabled) return;
    this.enabled = true;
    this.emit();
  }

  disable(): void {
    if (!this.enabled) return;
    this.enabled = false;
    this.emit();
  }

  focusNext(): void {
    this.step(1);
  }

  focusPrevious(): void {
    this.step(-1);
  }

  private step(dir: 1 | -1): void {
    const items = this.focusables.filter((f) => f.isActive);
    if (items.length === 0) {
      this.activeId = null;
      this.emit();
      return;
    }
    const idx = items.findIndex((f) => f.id === this.activeId);
    const next = idx === -1 ? (dir > 0 ? 0 : items.length - 1) : (idx + dir + items.length) % items.length;
    this.activeId = items[next]?.id ?? null;
    this.emit();
  }
}

export const FocusContext = createContext<FocusManager | null>(null);

export interface UseFocusOptions {
  id?: string;
  autoFocus?: boolean;
  isActive?: boolean;
}

/** Register the component as focusable; returns whether it currently holds focus. */
export function useFocus(options: UseFocusOptions = {}): {isFocused: boolean} {
  const manager = useContext(FocusContext);
  const generatedId = useId();
  const id = options.id ?? generatedId;
  const isActive = options.isActive ?? true;
  const autoFocus = options.autoFocus ?? false;
  const [, force] = useReducer((c: number) => c + 1, 0);

  useEffect(() => {
    if (!manager) return;
    manager.register(id, isActive);
    if (autoFocus) manager.focus(id);
    const unsubscribe = manager.subscribe(force);
    return () => {
      unsubscribe();
      manager.unregister(id);
    };
    // autoFocus is intentionally mount-only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manager, id, isActive]);

  return {isFocused: manager ? manager.isFocused(id) : false};
}

/** Focus controls for imperative navigation. */
export function useFocusManager(): {
  focusNext: () => void;
  focusPrevious: () => void;
  focus: (id: string) => void;
  enableFocus: () => void;
  disableFocus: () => void;
} {
  const manager = useContext(FocusContext);
  return useMemo(
    () => ({
      focusNext: () => manager?.focusNext(),
      focusPrevious: () => manager?.focusPrevious(),
      focus: (id: string) => manager?.focus(id),
      enableFocus: () => manager?.enable(),
      disableFocus: () => manager?.disable(),
    }),
    [manager],
  );
}

/** Turn a Tab / Shift+Tab keypress into a focus move. Returns true if it consumed the key.
 *
 * Only consumes Tab when there is actually something to focus (>=1 active focusable). Without that
 * guard the manager swallowed EVERY Tab even with zero focusables, so it never reached the app's
 * useInput — and the slash-command menu's Tab-completion silently did nothing (#17). When the app
 * registers no focusables (the current TUI), Tab now falls through to useInput. */
export function handleFocusKey(manager: FocusManager, key: Key): boolean {
  if (key.tab && !key.ctrl && !key.meta && manager.hasFocusables()) {
    if (key.shift) manager.focusPrevious();
    else manager.focusNext();
    return true;
  }
  return false;
}
