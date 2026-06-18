/**
 * Spike A (GATE) — does marked-terminal ANSI render correctly inside an Ink <Text> with
 * Yoga layout? Asserts: markdown content shows, the footer stays the LAST line (no drift),
 * and no visible line overflows the box width. Pass → Phase 1; fail → fallback renderer.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {Box, Text} from 'ink';
import {render} from 'ink-testing-library';
import {renderMarkdown} from '../src/markdown.js';

const ESC = String.fromCharCode(27);
const ANSI = new RegExp(ESC + '\\[[0-9;]*m', 'g');
const strip = (s: string): string => s.replace(ANSI, '');

const MD = `# Titel

Ein **fetter** Satz mit deutlich längerem Text, der über die schmale Box-Breite hinausgeht und sauber umbrechen soll, ohne die Spalte zu sprengen.

- erstes Element
- zweites Element

\`\`\`js
const x = 1;
\`\`\`
`;

function Spike({md, width}: {md: string; width: number}): React.ReactElement {
  return (
    <Box flexDirection="column" width={width}>
      <Text>{renderMarkdown(md, width - 2)}</Text>
      <Text dimColor>◆FOOTER◆</Text>
    </Box>
  );
}

test('Spike A — narrow box: content renders, footer pinned last, width respected', () => {
  const W = 44;
  const {lastFrame, unmount} = render(<Spike md={MD} width={W} />);
  const frame = lastFrame() ?? '';
  const visible = frame.split('\n').map(strip);

  assert.match(frame, /Titel/, 'heading rendered');
  assert.match(frame, /erstes Element/, 'list item 1 rendered');
  assert.match(frame, /zweites Element/, 'list item 2 rendered');
  assert.match(frame, /const x = 1/, 'code block rendered');

  const nonEmpty = visible.filter((l) => l.trim().length > 0);
  assert.match(nonEmpty.at(-1) ?? '', /◆FOOTER◆/, 'footer is the last non-empty line (no drift)');

  for (const l of visible) {
    assert.ok([...l].length <= W, `line within ${W} cols: ${JSON.stringify(l)} (len ${[...l].length})`);
  }
  unmount();
});

test('Spike A — wide box (100 cols): footer still last, no drift', () => {
  const {lastFrame, unmount} = render(<Spike md={MD} width={100} />);
  const visible = (lastFrame() ?? '').split('\n').map(strip);
  const nonEmpty = visible.filter((l) => l.trim().length > 0);
  assert.match(nonEmpty.at(-1) ?? '', /◆FOOTER◆/, 'footer last at 100 cols');
  unmount();
});
