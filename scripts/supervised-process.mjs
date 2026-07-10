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
  }) {
    return await new Promise((resolve) => {
      let child;
      let resultSettled = false;
      let terminalSettled = false;
      let timedOut = false;
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
        removeStreamListeners();
        if (terminalSettled) removeTerminalListeners();
        resolve(result);
      };
      const handleStdout = (chunk) => onStdout?.(chunk);
      const handleStderr = (chunk) => onStderr?.(chunk);
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
      timeoutTimer = setTimeoutImpl(() => {
        if (resultSettled || terminalSettled) return;
        timedOut = true;
        try {
          child.kill("SIGTERM");
        } catch {
          // The terminal event or grace deadline will complete the operation.
        }
        killTimer = setTimeoutImpl(() => {
          if (resultSettled || terminalSettled) return;
          try {
            child.kill("SIGKILL");
          } catch {
            // The child may have exited between the timeout and escalation.
          }
          // Bound the caller, but retain terminal listeners until close/error so
          // a late child-process error cannot become an unhandled EventEmitter error.
          finishResult({ exitCode: null, timedOut });
        }, killGraceMs);
      }, timeoutMs);
    });
  };
}

export const runSupervisedProcess = createSupervisedProcessRunner();
