/**
 * Force chalk / marked-terminal to emit ANSI colour even when *this* process's stdout doesn't look
 * like a TTY (MEM-20). `renderMarkdown` produces an intermediate ANSI string that OUR renderer then
 * parses and repaints onto the real terminal — so the colour (and the cli-highlight syntax
 * highlighting marked-terminal bundles) must NOT be suppressed by chalk's stdout sniffing of this
 * intermediate. Respects `NO_COLOR` and an explicit `FORCE_COLOR`. Imported BEFORE marked-terminal
 * so chalk reads it at init.
 */
if (!process.env['NO_COLOR'] && !process.env['FORCE_COLOR']) {
  process.env['FORCE_COLOR'] = '1';
}
