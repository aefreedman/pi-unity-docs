import { spawn } from "node:child_process";

const DEFAULT_KILL_GRACE_MS = 1_000;

/**
 * Run a child with timeout escalation. Dependencies are injectable for tests.
 */
export function createSupervisedProcessRunner({
  spawnImpl = spawn,
  setTimeoutImpl = setTimeout,
  clearTimeoutImpl = clearTimeout,
  killGraceMs = DEFAULT_KILL_GRACE_MS,
} = {}) {
  return async function runSupervisedProcess(command, args, {
    timeoutMs,
    onStdout,
    onStderr,
    spawnOptions = {},
    signal,
    maxStdoutBytes = 1_048_576,
    maxStderrBytes = 1_048_576,
  }) {
    return await new Promise((resolve) => {
      let child;
      let resultSettled = false;
      let terminalSettled = false;
      let timedOut = false;
      let aborted = false;
      let captureOverflow = false;
      let stdoutBytes = 0;
      let stderrBytes = 0;
      let timeoutTimer;
      let killTimer;

      const clearTimers = () => {
        if (timeoutTimer !== undefined) clearTimeoutImpl(timeoutTimer);
        if (killTimer !== undefined) clearTimeoutImpl(killTimer);
      };
      const removeStreamListeners = () => {
        child?.stdout?.removeListener?.("data", handleStdout);
        child?.stderr?.removeListener?.("data", handleStderr);
      };
      const removeTerminalListeners = () => {
        child?.removeListener?.("error", handleError);
        child?.removeListener?.("close", handleClose);
      };
      const finishResult = (result) => {
        if (resultSettled) return;
        resultSettled = true;
        clearTimers();
        signal?.removeEventListener("abort", handleAbort);
        removeStreamListeners();
        if (terminalSettled) removeTerminalListeners();
        resolve({ ...result, ...(aborted ? { aborted: true } : {}), ...(captureOverflow ? { captureOverflow: true } : {}) });
      };
      const stop = () => {
        if (resultSettled || terminalSettled || killTimer !== undefined) return;
        try { child.kill("SIGTERM"); } catch {}
        killTimer = setTimeoutImpl(() => {
          if (resultSettled || terminalSettled) return;
          try { child.kill("SIGKILL"); } catch {}
          finishResult({ exitCode: null, timedOut });
        }, killGraceMs);
      };
      const handleAbort = () => { aborted = true; stop(); };
      const handleStdout = (chunk) => {
        stdoutBytes += Buffer.byteLength(chunk);
        if (stdoutBytes > maxStdoutBytes) { captureOverflow = true; stop(); return; }
        if (!captureOverflow) onStdout?.(chunk);
      };
      const handleStderr = (chunk) => {
        stderrBytes += Buffer.byteLength(chunk);
        if (stderrBytes > maxStderrBytes) { captureOverflow = true; stop(); return; }
        if (!captureOverflow) onStderr?.(chunk);
      };
      const handleError = (error) => {
        terminalSettled = true;
        if (!resultSettled) finishResult({ exitCode: null, timedOut, error });
        removeTerminalListeners();
      };
      const handleClose = (exitCode) => {
        terminalSettled = true;
        if (!resultSettled) finishResult({ exitCode, timedOut });
        removeTerminalListeners();
      };

      if (signal?.aborted) {
        aborted = true;
        terminalSettled = true;
        finishResult({ exitCode: null, timedOut: false, notDispatched: true });
        return;
      }
      try {
        child = spawnImpl(command, args, spawnOptions);
      } catch (error) {
        terminalSettled = true;
        finishResult({ exitCode: null, timedOut: false, error });
        return;
      }

      child.stdout?.on("data", handleStdout);
      child.stderr?.on("data", handleStderr);
      child.on("error", handleError);
      child.on("close", handleClose);
      signal?.addEventListener("abort", handleAbort, { once: true });
      if (signal?.aborted) handleAbort();
      timeoutTimer = setTimeoutImpl(() => {
        if (resultSettled || terminalSettled) return;
        timedOut = true;
        stop();
      }, timeoutMs);
    });
  };
}

export const runSupervisedProcess = createSupervisedProcessRunner();
