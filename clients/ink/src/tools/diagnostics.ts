/** Process-level operator diagnostics: a configurable sink + once-only dedup, so a deep-in-the-stack
 *  advisory (e.g. a best-effort sandbox backend) reaches the operator without threading a callback
 *  through the hot tool path. Mirrors the Python module-flag + logger.warning pattern (gx10.py). */
let _sink: (message: string) => void = () => {};
const _emitted = new Set<string>();

/** Wire the operator-visible sink once at app startup (App.tsx → the TUI status line). */
export function setDiagnosticSink(sink: (message: string) => void): void {
  _sink = sink;
}

/** Emit *message* to the sink the FIRST time *key* is seen this process; later calls with the same key are dropped. */
export function emitDiagnosticOnce(key: string, message: string): void {
  if (_emitted.has(key)) return;
  _emitted.add(key);
  _sink(message);
}

/** Test hook: reset the once-only set + the sink to the default no-op. */
export function resetDiagnosticsForTest(): void {
  _emitted.clear();
  _sink = () => {};
}
