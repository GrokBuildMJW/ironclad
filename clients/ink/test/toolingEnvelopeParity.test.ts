import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {authorizeLaunch} from '../src/agent/handover.js';

type Vector = {
  name: string;
  bin: string;
  cmd_template: string;
  allow_list?: Array<{bin?: string; cmd_template?: string}>;
  policy?: unknown;
  expected_authorized: boolean;
};

const here = path.dirname(fileURLToPath(import.meta.url));
// This ../../../core/ack path depends on export_core.py rewriting the core/ prefix to root in the published tree.
const vectorsPath = path.resolve(here, '../../../ack/tooling_envelope_vectors.json');

function policyFor(v: Vector): {enabled?: boolean; allow_list?: Array<{bin?: string; cmd_template?: string}>} | null | undefined {
  if ('policy' in v) return v.policy as ReturnType<typeof policyFor>;
  return {enabled: true, allow_list: v.allow_list ?? []};
}

test('authorizeLaunch matches the shared tooling-envelope vector corpus', () => {
  process.env.IRONCLAD_TE_BIN = 'claude';
  process.env.IRONCLAD_TE_NESTED = '$IRONCLAD_TE_BIN';
  delete process.env.IRONCLAD_TE_UNDEFINED;

  const vectors = JSON.parse(readFileSync(vectorsPath, 'utf8')) as Vector[];
  assert.ok(vectors.length > 0);
  for (const v of vectors) {
    const refusal = authorizeLaunch(v.bin, v.cmd_template, policyFor(v) as Parameters<typeof authorizeLaunch>[2]);
    assert.equal(refusal === null, v.expected_authorized, v.name);
  }
});

test('shared vector corpus is loaded from core/ack', () => {
  assert.ok(vectorsPath.endsWith(path.normalize('ack/tooling_envelope_vectors.json')));
});
