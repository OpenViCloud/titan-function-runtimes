// Unit tests of what is only Node.js's: how a handler module is found, and which values are a response. The HTTP and
// cron behaviour is in tests/contract/contract_test.py, which runs the same cases against every runtime.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { loadHandler, toResponse } from './adapter.mjs';

function codeDir(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'titan-node-'));
  for (const [name, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, name)), { recursive: true });
    fs.writeFileSync(path.join(dir, name), text);
  }
  return dir;
}

test('an ES module, a CommonJS module and a nested module are found', async () => {
  const dir = codeDir({
    'index.mjs': 'export const handler = async () => "esm";',
    'cjs.cjs': 'module.exports.handler = () => "cjs";',
    'plain.js': 'exports.handler = () => "plain-js";',
    'sub/deep.mjs': 'export const go = () => "deep";',
  });
  assert.equal(await (await loadHandler('index.handler', dir))(), 'esm');
  assert.equal(await (await loadHandler('cjs.handler', dir))(), 'cjs');
  assert.equal(await (await loadHandler('plain.handler', dir))(), 'plain-js');
  assert.equal(await (await loadHandler('sub/deep.go', dir))(), 'deep');
});

test('a module that does not exist, has no export, or exports a non-function is refused', async () => {
  const dir = codeDir({ 'index.mjs': 'export const value = 1;' });
  for (const spec of ['nomodule.handler', 'index.nohandler', 'index.value', 'handler', '.handler', 'index.']) {
    await assert.rejects(loadHandler(spec, dir), undefined, spec);
  }
});

test('a module outside the code dir is not loaded through ..', async () => {
  const dir = codeDir({ 'index.mjs': 'export const handler = () => 1;' });
  const inner = path.join(dir, 'sub');
  fs.mkdirSync(inner);
  await assert.rejects(loadHandler('../index.handler', inner), /no module|outside/);
});

test('a string is a 200 text/plain; an object needs a statusCode from 100 to 599', () => {
  assert.deepEqual(toResponse('é'), { status: 200, headers: [['content-type', 'text/plain; charset=utf-8']], body: Buffer.from('é') });
  assert.equal(toResponse({ statusCode: 204 }).body.length, 0);
  for (const bad of [undefined, null, 5, [], {}, { statusCode: '200' }, { statusCode: 200.5 }, { statusCode: 99 }, { statusCode: 600 },
    { statusCode: 200, headers: [] }, { statusCode: 200, headers: { a: 1 } }, { statusCode: 200, headers: { 'a b': 'x' } },
    { statusCode: 200, headers: { a: 'x\ny' } }, { statusCode: 200, body: Buffer.from('x') }, { statusCode: 200, isBase64Encoded: true, body: '***' }]) {
    assert.throws(() => toResponse(bad), /./, JSON.stringify(bad));
  }
});

test('framing headers are dropped in any case; base64 only when the flag is exactly true', () => {
  assert.deepEqual(toResponse({ statusCode: 200, headers: { 'Content-Length': '9', 'TRANSFER-ENCODING': 'x', 'X-Ok': '1' } }).headers, [['X-Ok', '1']]);
  assert.deepEqual(toResponse({ statusCode: 200, isBase64Encoded: true, body: 'AP8=' }).body, Buffer.from([0, 255]));
  assert.deepEqual(toResponse({ statusCode: 200, isBase64Encoded: 'yes', body: 'AP8=' }).body, Buffer.from('AP8='));
});
