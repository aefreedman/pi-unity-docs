import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createSupervisedProcessRunner } from '../scripts/supervised-process.mjs';

function fixture() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  const signals = [];
  child.kill = s => { signals.push(s); return true; };
  return { child, signals };
}
test('capture ceiling stops delivery and terminates without retaining overflow', async () => {
  const { child, signals } = fixture();
  const chunks = [];
  const run = createSupervisedProcessRunner({ spawnImpl: () => child });
  const result = run('fake', [], { timeoutMs: 1000, maxStdoutBytes: 4, onStdout: b => chunks.push(b.toString()) });
  child.stdout.emit('data', Buffer.from('1234'));
  child.stdout.emit('data', Buffer.from('private overflow'));
  child.emit('close', 143);
  assert.deepEqual(chunks, ['1234']);
  assert.deepEqual(signals, ['SIGTERM']);
  assert.equal((await result).captureOverflow, true);
});
test('abort propagates into supervisor; already aborted does not spawn', async () => {
  const { child, signals } = fixture();
  let spawns = 0;
  const run = createSupervisedProcessRunner({ spawnImpl: () => { spawns++; return child; } });
  const controller = new AbortController();
  const result = run('fake', [], { timeoutMs: 1000, signal: controller.signal });
  controller.abort();
  child.emit('close', 143);
  assert.equal((await result).aborted, true);
  assert.deepEqual(signals, ['SIGTERM']);
  const before = await run('fake', [], { timeoutMs: 1000, signal: controller.signal });
  assert.equal(before.notDispatched, true);
  assert.equal(spawns, 1);
});
