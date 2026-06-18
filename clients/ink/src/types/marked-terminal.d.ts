// Minimal type shim — marked-terminal ships no .d.ts. We only use markedTerminal() as a
// marked extension (the render is exercised by Spike A at runtime).
declare module 'marked-terminal' {
  import type {MarkedExtension} from 'marked';
  export function markedTerminal(
    options?: Record<string, unknown>,
    highlightOptions?: Record<string, unknown>,
  ): MarkedExtension;
}
