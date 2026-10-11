import test from 'node:test';
import assert from 'node:assert/strict';
import { Value } from 'typebox/value';
import { createUnityDocsExtension } from '../index.ts';
import { project, outputSchema, boundedText, NativeFailure } from '../scripts/native-output.ts';
import { createScriptRunner } from '../scripts/script-runner.ts';

const row = { pageId: 'Package/Test', docsetId: 'tiny', version: '1.0', title: 'Test', snippet: 'hello', rank: 0, privateUnknown: 'ignored' };
const lookup = { ok: true, rows: [row], coverage: [{ docsetId: 'tiny', status: 'queried' }], observedRows: 1, queryLimited: true };
const stageFacts = ['source','ingestion','database','publication','configuration'].map(stage=>({stage,state:'completed'}));
const fixtures = {
  info: { dbExists: false, metadata: { pageCount: '0', unknown: 'private' }, docsets: { tiny: { enabled: false, dbExists: false, sourceExists: null } }, config: { secret: 'not native' } },
  search: lookup, symbol: { ...lookup, rows: [{ ...row, fullName: 'Test', signature: '' }] },
  show: { page: { ...row, id: row.pageId }, sections: [{ headingPath: 'Overview', text: 'hello', tokenEstimate: 0 }], truncated: false, headingSelection: 'fallback_all', selectedSections: 1 },
  validate: { passed: 0, total: 1, failed: 1, docsets: [{ docsetId: 'tiny', checkCount: 1, passed: false, checks: [{ kind: 'search', query: 'notfound', passed: false, resultCount: 0, topResult: null }] }] },
  build_database: { requestedLimit: 1, limitPolicy: 'page_limit', sourceMode: 'local_unity', dbPath: 'fixture/unity_docs.sqlite', version: '1.0', pages: 0, sections: 0, symbols: 0, elapsedSeconds: 0 },
  build_docset: { requestedLimit: 1, limitPolicy: 'source_dependent', sourceMode: 'explicit', dbPath: 'fixture/unity_docs.sqlite', docsetId: 'tiny', packageVersion: '1.0', pages: 0, sections: 0, symbols: 0, elapsedSeconds: 0 },
};
function toolsFor(runner) {
  const tools = new Map();
  createUnityDocsExtension(runner)({ registerTool: t => tools.set(t.name, t), registerCommand() {} });
  return tools;
}
for (const [operation, facts] of Object.entries(fixtures)) {
  test(`${operation}: registered adapter validates producer facts and executes once`, async () => {
    let calls = 0;
    const signal = new AbortController().signal;
    const runner = async (_command, args, opts) => {
      calls++;
      assert(args.includes('--native-observations'));
      assert.equal(opts.signal, signal);
      if (operation.startsWith('build_')) for (const stage of stageFacts) opts.onStderr(Buffer.from(`OBSERVATION ${JSON.stringify(stage)}\n`));
      opts.onStdout(Buffer.from(JSON.stringify(facts)));
      return { exitCode: 0 };
    };
    const tool = toolsFor(runner).get(`unity_docs_${operation}`);
    const result = await tool.execute('test', { query: 'hello', name: 'Test', page: 'Package/Test', sourcePath: 'fixture/source', dbDir: 'fixture/db', version: '1.0', limit: 1 }, signal, undefined, { cwd: 'fixture' });
    assert.equal(calls, 1);
    assert.equal(result.structuredContent.ok, true, JSON.stringify(result));
    assert(Value.Check(tool.outputSchema, result.structuredContent));
    assert(result.content[0].text.length > 0);
    assert(!Object.hasOwn(result, 'nativeFacts'));
    if (operation === 'info') {
      assert.equal(result.structuredContent.dbExists, false);
      assert.equal(result.structuredContent.docsets[0].sourceExists, null);
      assert(!JSON.stringify(result.structuredContent).includes('private'));
    }
    if (operation === 'validate') assert.equal(result.structuredContent.failed, 1);
    if (operation.startsWith('build_')) assert.equal(result.structuredContent.effects.stages.length, 5);
  });
}
test('malformed JSON and invalid row shape cannot become successful empty results', async () => {
  for (const stdout of ['not JSON private /secret', '{}', JSON.stringify({ ...lookup, rows: [null] })]) {
    let calls = 0;
    const tool = toolsFor(async (_c, _a, opts) => { calls++; opts.onStdout(Buffer.from(stdout)); return { exitCode: 0 }; }).get('unity_docs_search');
    const r = await tool.execute('id', { query: 'hello' }, undefined, undefined, {});
    assert.equal(calls, 1);
    assert.equal(r.isError, true);
    assert.equal(r.structuredContent.ok, false);
    assert(!r.content[0].text.includes('private'));
  }
});
test('partial, all-failed and all-excluded have distinct native and useful direct outcomes', async () => {
  for (const coverage of [
    [{ docsetId: 'tiny', status: 'queried' }, { docsetId: 'missing', status: 'missing' }],
    [{ docsetId: 'tiny', status: 'failed' }],
    [{ docsetId: 'tiny', status: 'excluded' }],
  ]) {
    const facts = { ...lookup, rows: [], observedRows: 0, coverage, ok: coverage[0].status !== 'failed' };
    const tool = toolsFor(async (_c, _a, o) => { o.onStdout(Buffer.from(JSON.stringify(facts))); return { exitCode: 0 }; }).get('unity_docs_search');
    const r = await tool.execute('id', { query: 'hello' }, undefined, undefined, {});
    assert.equal(r.structuredContent.ok, facts.ok);
    if (facts.ok) assert.match(r.content[0].text, /Coverage:/);
    if (coverage[0].status === 'excluded') assert.match(r.content[0].text, /no applicable source was searched/);
  }
});
test('exact identities, admitted-only guards, descriptive omission and byte bounds', () => {
  const dto = project('search', { ...lookup, rows: [{ ...row, pageId: 'x'.repeat(2048), snippet: 'y'.repeat(2049), unknownId: 'z'.repeat(10000) }] }, {});
  assert.equal(dto.rows[0].pageId.length, 2048);
  assert.equal(dto.omissions.fields, 1);
  assert.throws(() => project('search', { ...lookup, rows: [{ ...row, pageId: 'x'.repeat(2049) }] }, {}), NativeFailure);
  const omitted = project('search', { ...lookup, rows: [...Array(50).fill(row), { pageId: 'x'.repeat(10000) }], observedRows: 51 }, {});
  assert.equal(omitted.omissions.rows, 1);
  assert.equal(omitted.rows.length, 50);
  assert.throws(() => project('search', { ...lookup, rows: Array(50).fill({ ...row, title: 'Ã©'.repeat(2048), headingPath: 'Ã©'.repeat(2048), snippet: 'Ã©'.repeat(2048), summary: 'Ã©'.repeat(2048) }), observedRows: 50 }, {}), NativeFailure);
  const text = boundedText('Ã©'.repeat(65536));
  assert(Buffer.byteLength(text) <= 65536);
  assert.match(text, /presentation omitted/);
});
test('build stage failures, timeout/cancel and projection errors retain effects without replay', async () => {
  for (const terminal of [{ exitCode: 1 }, { exitCode: null, timedOut: true }, { exitCode: null, aborted: true }, { exitCode: 0 }]) {
    let calls = 0;
    const tools = toolsFor(async (_c, _a, opts) => {
      calls++;
      opts.onStderr(Buffer.from('OBSERVATION {"stage":"publication","state":"completed"}\n'));
      opts.onStdout(Buffer.from('{"dbPath":"' + 'x'.repeat(2049) + '"}'));
      return terminal;
    });
    const r = await tools.get('unity_docs_build_database').execute('id', { sourcePath: 'fixture/source', dbDir: 'fixture/out' }, undefined, undefined, {});
    assert.equal(calls, 1);
    assert.equal(r.isError, true);
    assert.equal(r.structuredContent.effects.possibleChanges, true);
    assert.deepEqual(r.structuredContent.effects.stages, [{ stage: 'publication', state: 'completed' }]);
    assert.equal(r.structuredContent.effects.replaySafe, false);
  }
  let calls = 0;
  const tool = toolsFor(async () => { calls++; throw new Error('must not dispatch'); }).get('unity_docs_build_docset');
  const refused = await tool.execute('id', { dbDir: 'fixture/out', llmsUrl: 'a', gitbookLlmsUrl: 'b' }, undefined, undefined, {});
  assert.equal(calls, 0);
  assert.equal(refused.structuredContent.effects.dispatched, false);
});
test('script seam preserves deliberate text clients and classifies JSON/capture errors safely', async () => {
  const script = createScriptRunner('fake', 'fake.py', async (_c, _a, o) => { o.onStdout(Buffer.from('plain text')); return { exitCode: 0 }; });
  assert.equal((await script(['show'])).stdout, 'plain text');
  await assert.rejects(script(['--json', 'show']), e => e.code === 'invalid_json');
  const overflow = createScriptRunner('fake', 'fake.py', async () => ({ exitCode: null, captureOverflow: true }));
  await assert.rejects(overflow(['--json', 'search']), e => e.code === 'capture_overflow');
});

test('split UTF-8 process chunks retain exact actionable identities', async () => {
  const payload = Buffer.from(JSON.stringify({ ...lookup, rows: [{ ...row, pageId: 'Package/é' }] }));
  const split = payload.indexOf(Buffer.from('é')) + 1;
  const script = createScriptRunner('fake', 'fake.py', async (_c, _a, o) => {
    o.onStdout(payload.subarray(0, split)); o.onStdout(payload.subarray(split)); return { exitCode: 0 };
  });
  const captured = await script(['--json', 'search'], 1000, undefined, undefined, true);
  assert.equal(project('search', captured.json, {}).rows[0].pageId, 'Package/é');
});
