/**
 * hooks — the app-facing React hooks our renderer provides (R3e).
 *
 * When we replace Ink's `render()` we also replace Ink's hook context, so components keep
 * calling `useInput` / `useApp` / `useStdout` / `useStdin` unchanged. They read a single
 * `RenderContext` that the mount layer (R7) provides; the input-dispatch layer (R5) pushes
 * parsed keypresses through the producer-side bridge created here, which fans them out to
 * every active `useInput` subscriber.
 *
 * Separation of concerns: this module owns the *contract* (context shape, the Ink-compatible
 * `Key` type, subscription) — not key parsing (keys.ts) nor terminal I/O wiring (mount.ts).
 */
import {createContext, useContext, useEffect, useMemo, useRef} from 'react';

/** Ink-compatible key descriptor passed to `useInput` handlers. */
export interface Key {
  upArrow: boolean;
  downArrow: boolean;
  leftArrow: boolean;
  rightArrow: boolean;
  pageDown: boolean;
  pageUp: boolean;
  return: boolean;
  escape: boolean;
  ctrl: boolean;
  shift: boolean;
  tab: boolean;
  backspace: boolean;
  delete: boolean;
  meta: boolean;
  /** True when `input` is a bracketed paste (one chunk, markers stripped) rather than typed — lets the
   * app compress a multi-line paste to a `[Pasted #N +L lines]` placeholder (#438). */
  paste: boolean;
}

export type InputHandler = (input: string, key: Key) => void;

/** A fresh all-false Key with optional overrides (used by keys.ts and tests). */
export function emptyKey(over: Partial<Key> = {}): Key {
  return {
    upArrow: false, downArrow: false, leftArrow: false, rightArrow: false,
    pageDown: false, pageUp: false, return: false, escape: false,
    ctrl: false, shift: false, tab: false, backspace: false, delete: false, meta: false,
    paste: false,
    ...over,
  };
}

/** What the hooks read. Provided by the mount layer via `RenderContext.Provider`. */
export interface RenderContextValue {
  exit: (error?: Error) => void;
  stdout: NodeJS.WriteStream;
  stdin: NodeJS.ReadStream | undefined;
  setRawMode: (on: boolean) => void;
  isRawModeSupported: boolean;
  /** Subscribe to input; returns an unsubscribe fn. */
  subscribeInput: (handler: InputHandler) => () => void;
}

export const RenderContext = createContext<RenderContextValue | null>(null);

function useRenderContext(): RenderContextValue {
  const c = useContext(RenderContext);
  if (!c) {
    throw new Error('Ironclad render hooks must be used inside the renderer (mount.ts provides RenderContext).');
  }
  return c;
}

/** Ink-compatible: `useApp()` → `{ exit }` to unmount the app (optionally with an error). */
export function useApp(): {exit: (error?: Error) => void} {
  const {exit} = useRenderContext();
  return useMemo(() => ({exit}), [exit]);
}

/** Ink-compatible: `useStdout()` → the output stream plus a `write` helper. */
export function useStdout(): {stdout: NodeJS.WriteStream; write: (data: string) => void} {
  const {stdout} = useRenderContext();
  return useMemo(() => ({stdout, write: (data: string) => void stdout.write(data)}), [stdout]);
}

/** Ink-compatible: `useStdin()` → the input stream and raw-mode controls. */
export function useStdin(): {
  stdin: NodeJS.ReadStream | undefined;
  setRawMode: (on: boolean) => void;
  isRawModeSupported: boolean;
} {
  const {stdin, setRawMode, isRawModeSupported} = useRenderContext();
  return useMemo(() => ({stdin, setRawMode, isRawModeSupported}), [stdin, setRawMode, isRawModeSupported]);
}

export interface InputOptions {
  /** When false, the handler is detached (and this component stops holding raw mode). */
  isActive?: boolean;
}

/**
 * Ink-compatible: `useInput(handler, { isActive })`. While active, raw mode is held and every
 * parsed keypress is delivered as `(input, key)`. The handler is kept in a ref so changing it
 * each render does not churn the subscription.
 */
export function useInput(handler: InputHandler, options: InputOptions = {}): void {
  const {subscribeInput, setRawMode, isRawModeSupported} = useRenderContext();
  const isActive = options.isActive ?? true;

  // latest-ref: refs are made for this; avoids re-subscribing when only the handler identity changes
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    if (!isActive) return;
    if (isRawModeSupported) setRawMode(true);
    const unsubscribe = subscribeInput((input, key) => handlerRef.current(input, key));
    return () => {
      unsubscribe();
      if (isRawModeSupported) setRawMode(false);
    };
  }, [isActive, subscribeInput, setRawMode, isRawModeSupported]);
}

/** Producer side: the context value plus the emitter the dispatch layer drives. */
export interface InputBridge {
  value: RenderContextValue;
  /** Deliver a parsed keypress to all active subscribers (called by dispatch.ts/mount.ts). */
  emit: (input: string, key: Key) => void;
  /** Number of active `useInput` subscribers (lets the dispatcher idle raw mode when zero). */
  subscriberCount: () => number;
}

export interface RenderContextInit {
  stdout: NodeJS.WriteStream;
  exit: (error?: Error) => void;
  stdin?: NodeJS.ReadStream;
  setRawMode?: (on: boolean) => void;
  isRawModeSupported?: boolean;
}

/**
 * Build the `RenderContextValue` and its input bridge. The mount layer feeds real
 * stdin/stdout + an exit fn; the dispatch layer calls `bridge.emit(...)` per keypress.
 */
export function createRenderContext(init: RenderContextInit): InputBridge {
  const subscribers = new Set<InputHandler>();
  const isRawModeSupported = init.isRawModeSupported ?? Boolean(init.stdin?.isTTY);
  const setRawMode =
    init.setRawMode ??
    ((on: boolean): void => {
      // Node typings mark setRawMode optional on ReadStream; guard for non-TTY pipes.
      init.stdin?.setRawMode?.(on);
    });

  const value: RenderContextValue = {
    exit: init.exit,
    stdout: init.stdout,
    stdin: init.stdin,
    setRawMode,
    isRawModeSupported,
    subscribeInput: (handler) => {
      subscribers.add(handler);
      return () => {
        subscribers.delete(handler);
      };
    },
  };

  return {
    value,
    emit: (input, key) => {
      for (const handler of [...subscribers]) handler(input, key);
    },
    subscriberCount: () => subscribers.size,
  };
}
