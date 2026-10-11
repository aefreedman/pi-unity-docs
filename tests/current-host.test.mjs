// Deterministic in-process SDK fixture: real package file loader and prebuilt temp SQLite.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { Type } from 'typebox';
import { VERSION, createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager, createCodemodeExtension } from '@earendil-works/pi-coding-agent';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { selectPythonCommand } from '../scripts/python-selector.mjs';

const root = path.resolve('.');
const runtimeVersion = VERSION;

test('current Pi host: direct/native/nested/error/hook/transcript and final-event serialization', async () => {
  assert.equal(runtimeVersion, '1.1.0');
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'unity-docs-host-'));
  let session;
  try {
    const db = path.join(tmp, 'tiny.sqlite');
    const buildFixture = spawnSync(selectPythonCommand(), ['-c', `
import importlib.util,sys,sqlite3
spec=importlib.util.spec_from_file_location('host_db',sys.argv[1]); m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
c=sqlite3.connect(sys.argv[2]);m.create_schema(c)
c.executemany('INSERT INTO metadata VALUES (?,?)',[('docsetId','tiny'),('docsetKind','package'),('version','1.0'),('pageCount','1')])
c.execute("INSERT INTO pages(id,version,corpus,slug,title,kind,source_path,url_path,summary) VALUES ('Package/Test','1.0','Package','Test','Test','package_page','fixture.md','Package/Test.md','hello')")
c.execute("INSERT INTO sections VALUES ('s','Package/Test','Overview',0,'hello world',2)")
c.execute("INSERT INTO sections_fts VALUES ('s','Package/Test','Test','Overview','hello world')")
c.execute("INSERT INTO symbols(page_id,full_name,short_name,kind,signature) VALUES ('Package/Test','Test','Test','package_page','')")
c.commit();c.close()
`, path.join(root, 'scripts/unity_docs_db.py'), db], { encoding: 'utf8' });
    assert.equal(buildFixture.status, 0, buildFixture.stderr);
    const settingsManager = SettingsManager.inMemory({ defaultTools: ['unity_docs_search', 'unity_docs_symbol', 'unity_docs_show', 'unity_docs_info', 'unity_docs_validate', 'codemode', 'nested_consumer'], compaction: { enabled: false }, retry: { enabled: false } });
    const modelRuntime = await ModelRuntime.create({ authPath: path.join(tmp, 'auth.json'), modelsPath: null, modelsStorePath: path.join(tmp, 'models.json'), refreshOnCreate: false, allowModelNetwork: false });
    const queue = [];
    const model = { id: 'scripted', name: 'scripted', provider: 'local-fixture', api: 'fixture-api', baseUrl: 'https://fixture.invalid', reasoning: false, input: ['text'], contextWindow: 100000, maxTokens: 1000, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
    const streamSimple = (currentModel, _context, options) => {
      const stream = createAssistantMessageEventStream();
      queueMicrotask(async () => {
        const call = queue.shift();
        const message = { role: 'assistant', provider: currentModel.provider, model: currentModel.id, api: currentModel.api, timestamp: Date.now(), content: [], stopReason: call ? 'toolUse' : 'stop', usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
        await options?.onPayload?.({ fixture: true }, currentModel);
        await options?.onResponse?.({ headers: new Headers() }, currentModel);
        stream.push({ type: 'start', partial: message });
        if (call) {
          const block = { type: 'toolCall', id: `fixture-${Date.now()}`, name: call.name, arguments: call.args };
          message.content.push(block);
          stream.push({ type: 'toolcall_start', contentIndex: 0, partial: message });
          stream.push({ type: 'toolcall_end', contentIndex: 0, toolCall: block, partial: message });
        } else {
          message.content.push({ type: 'text', text: 'done' });
          stream.push({ type: 'text_start', contentIndex: 0, partial: message });
          stream.push({ type: 'text_delta', contentIndex: 0, delta: 'done', partial: message });
          stream.push({ type: 'text_end', contentIndex: 0, content: 'done', partial: message });
        }
        stream.push({ type: 'done', reason: message.stopReason, message });
        stream.end(message);
      });
      return stream;
    };
    let hookMode = 'none';
    let nestedOutcome;
    const resourceLoader = new DefaultResourceLoader({
      cwd: tmp, agentDir: tmp, settingsManager, additionalExtensionPaths: [path.join(root, 'index.ts')], noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      extensionFactories: [
        createCodemodeExtension({ mode: 'on' }),
        pi => {
          pi.registerProvider('local-fixture', { api: model.api, baseUrl: model.baseUrl, apiKey: 'fixture-only', models: [model], streamSimple });
          pi.registerTool({ name: 'nested_consumer', label: 'Nested', description: 'Offline fixture consumer', parameters: Type.Object({ missing: Type.Optional(Type.Boolean()) }), async execute(_id, _args, signal, _update, ctx) {
            nestedOutcome = await ctx.executeTool('unity_docs_search', { db: _args.missing ? path.join(tmp, 'missing.sqlite') : db, query: 'hello' }, { signal });
            return { content: [{ type: 'text', text: 'nested observed' }], details: undefined };
          } });
          pi.on('tool_call', event => event.toolName === 'unity_docs_search' && hookMode === 'block' ? { block: true, reason: 'fixture blocked' } : undefined);
          pi.on('tool_result', event => {
            if (event.toolName !== 'unity_docs_search') return;
            if (hookMode === 'drop') return { content: [{ type: 'text', text: 'fixture redacted' }] };
            if (hookMode === 'alter') return { structuredContent: { fixtureAltered: true } };
          });
        },
      ],
    });
    await resourceLoader.reload();
    assert.deepEqual(resourceLoader.getExtensions().errors, []);
    ({ session } = await createAgentSession({ cwd: tmp, agentDir: tmp, modelRuntime, model, settingsManager, resourceLoader, sessionManager: SessionManager.inMemory(tmp) }));
    await session.bindExtensions({});
    const events = [];
    session.subscribe(event => { if (event.type === 'tool_execution_end') { JSON.stringify(event); events.push(event); } });
    async function run(name, args) {
      const start = events.length;
      queue.push({ name, args });
      await session.prompt('Run the next offline fixture call.');
      return events.slice(start).find(event => event.toolName === name);
    }
    const direct = await run('unity_docs_search', { db, query: 'hello' });
    assert.equal(direct.result.structuredContent?.ok, true, JSON.stringify(direct));
    assert.match(direct.result.content[0].text, /Package\/Test/);
    assert.match(direct.result.content[0].text, /Coverage: complete/);
    const info = await run('unity_docs_info', { db });
    assert.equal(info.result.structuredContent.dbExists, true);
    assert.match(info.result.content[0].text, /dbExists/);
    const symbol = await run('unity_docs_symbol', { db, name: 'Test' });
    assert.equal(symbol.result.structuredContent.rows[0].fullName, 'Test');
    const validation = await run('unity_docs_validate', { db });
    assert.equal(validation.result.structuredContent.ok, true);
    assert.equal(validation.result.structuredContent.total, 0);
    const show = await run('unity_docs_show', { db, page: 'Package/Test', sections: ['unmatched'] });
    assert.equal(show.result.structuredContent.headingSelection, 'fallback_all');
    assert.match(show.result.content[0].text, /hello world/);
    const selective = await run('codemode', { code: `const r = await tools.unity_docs_search({db:${JSON.stringify(db)}, query:'hello'}); if (!r.ok) throw Error(r.error.code); text(r.rows.map(x=>({page:x.pageId, docset:x.docsetId})));` });
    assert.equal(selective.isError, false);
    assert.match(selective.result.content.map(c=>c.text??'').join(' '), /Package\/Test/, JSON.stringify(selective));
    assert(!selective.result.content.map(c=>c.text??'').join(' ').includes('hello world'));
    await run('nested_consumer', {});
    assert.equal(nestedOutcome.isError, false);
    assert.equal(nestedOutcome.result.structuredContent.ok, true);
    await run('nested_consumer', { missing: true });
    assert.equal(nestedOutcome.isError, true);
    assert.equal(nestedOutcome.result.structuredContent.ok, false);
    const dataFailure = await run('codemode', { code: `const r=await tools.unity_docs_search({db:${JSON.stringify(path.join(tmp, 'missing.sqlite'))}, query:'hello'}); text({ok:r.ok, code:r.error.code});` });
    assert.equal(dataFailure.isError, false);
    assert.match(dataFailure.result.content.map(c=>c.text??'').join(' '), /"ok": false|"ok":false/);
    hookMode = 'block';
    const rejected = await run('codemode', { code: `try { await tools.unity_docs_search({db:${JSON.stringify(db)},query:'hello'}); throw Error('unexpected'); } catch(e) { text('rejected:'+e.message); }` });
    assert.match(rejected.result.content.map(c=>c.text??'').join(' '), /rejected:.*fixture blocked/s);
    hookMode = 'drop';
    const dropped = await run('unity_docs_search', { db, query: 'hello' });
    assert.equal(dropped.result.structuredContent, undefined);
    assert.equal(dropped.result.content[0].text, 'fixture redacted');
    hookMode = 'alter';
    const altered = await run('unity_docs_search', { db, query: 'hello' });
    assert.deepEqual(altered.result.structuredContent, { fixtureAltered: true });
    const persisted = session.sessionManager.getBranch().filter(e => e.type === 'message' && e.message.role === 'toolResult');
    assert(persisted.length > 0);
    assert(persisted.every(e => e.message.structuredContent === undefined));
    console.log(`Validated Pi ${runtimeVersion}: ${events.length} final execution events serialized; no RPC/CLI wire claim.`);
  } finally {
    session?.dispose();
    await fs.rm(tmp, { recursive: true, force: true });
  }
});
