import assert from "node:assert/strict";
import test from "node:test";

import { createSupervisedProcessRunner } from "../scripts/supervised-process.mjs";

class FakeEmitter {
  #listeners = new Map();

  on(event, listener) {
    this.#listeners.set(event, [...(this.#listeners.get(event) ?? []), listener]);
  }

  removeListener(event, listener) {
    this.#listeners.set(event, (this.#listeners.get(event) ?? []).filter((candidate) => candidate !== listener));
  }

  emit(event, ...args) {
    for (const listener of [...(this.#listeners.get(event) ?? [])]) listener(...args);
  }

  listenerCount(event) {
    return (this.#listeners.get(event) ?? []).length;
  }
}

function createFakeChild() {
  const child = new FakeEmitter();
  child.stdout = new FakeEmitter();
  child.stderr = new FakeEmitter();
  child.killed = false;
  child.signals = [];
  child.kill = (signal) => {
    child.killed = true; // Node sets this when SIGTERM is sent, not on close.
    child.signals.push(signal);
    return true;
  };
  return child;
}

function createFakeTimers() {
  const timers = [];
  return {
    setTimeout(callback, delay) {
      const timer = { callback, delay, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout(timer) {
      timer.cleared = true;
    },
    fire(delay) {
      const timer = timers.find((candidate) => candidate.delay === delay && !candidate.cleared);
      assert(timer, `Expected an active ${delay}ms timer.`);
      timer.callback();
    },
    timer(delay) {
      return timers.find((candidate) => candidate.delay === delay);
    },
  };
}

test("timeout escalates independent of ChildProcess.killed and settles without close", async () => {
  const child = createFakeChild();
  const timers = createFakeTimers();
  const run = createSupervisedProcessRunner({
    spawnImpl: () => child,
    setTimeoutImpl: timers.setTimeout,
    clearTimeoutImpl: timers.clearTimeout,
    killGraceMs: 2,
  });

  const result = run("python", ["tool.py"], { timeoutMs: 5 });
  timers.fire(5);
  assert.deepEqual(child.signals, ["SIGTERM"]);
  timers.fire(2);

  assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
  assert.deepEqual(await result, { exitCode: null, timedOut: true });
  assert.equal(child.listenerCount("error"), 1, "terminal error handling must remain until the child actually settles");
  child.emit("error", new Error("late child error after forced timeout"));
  assert.equal(child.listenerCount("error"), 0);
  assert.equal(child.listenerCount("close"), 0);
});

test("close clears pending escalation timers and stream listeners", async () => {
  const child = createFakeChild();
  const timers = createFakeTimers();
  const run = createSupervisedProcessRunner({
    spawnImpl: () => child,
    setTimeoutImpl: timers.setTimeout,
    clearTimeoutImpl: timers.clearTimeout,
    killGraceMs: 3,
  });

  const result = run("python", ["tool.py"], { timeoutMs: 7 });
  timers.fire(7);
  child.emit("close", 143);

  assert.deepEqual(await result, { exitCode: 143, timedOut: true });
  assert.equal(timers.timer(3)?.cleared, true);
  assert.equal(child.stdout.listenerCount("data"), 0);
});
