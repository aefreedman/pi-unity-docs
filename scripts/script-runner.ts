import { StringDecoder } from "node:string_decoder";
import { runSupervisedProcess } from "./supervised-process.mjs";
import { NativeFailure } from "./native-output.ts";
export type ScriptResult = { stdout: string; stderr: string; exitCode: number | null; json?: unknown; stages: Array<{ stage: string; state: string }> };
function formatProgressLine(line: string): string | null {
  if (!line.startsWith("PROGRESS ")) return null;
  try {
    const payload = JSON.parse(line.slice("PROGRESS ".length)) as {
      stage?: string;
      corpus?: string | null;
      pages?: number;
      totalPages?: number;
      percent?: number;
      sections?: number;
      symbols?: number;
      elapsed?: string;
    };
    const total = payload.totalPages ?? 0;
    const pages = payload.pages ?? 0;
    const percent = typeof payload.percent === "number" ? `${payload.percent.toFixed(1)}%` : "?%";
    const progress = total > 0 ? `${pages}/${total} pages (${percent})` : `${pages} pages`;
    const corpus = payload.corpus ? ` ${payload.corpus}` : "";
    return `Unity docs build:${corpus} ${payload.stage ?? "running"} — ${progress}, ${payload.sections ?? 0} sections, ${payload.symbols ?? 0} symbols, elapsed ${payload.elapsed ?? "?"}`;
  } catch {
    return null;
  }
}

export function createScriptRunner(pythonCommand: string, scriptPath: string, processRunner = runSupervisedProcess) {
return async function runScript(args: string[], timeoutMs = 120_000, onProgress?: (message: string) => void, signal?: AbortSignal, native = false): Promise<ScriptResult> {
  const stages: Array<{ stage: string; state: string }> = [];
  if (native) args = ["--native-observations", ...args];
  const stdoutDecoder = new StringDecoder("utf8");
  const stderrDecoder = new StringDecoder("utf8");
  let stdout = "";
  let stderr = "";
  let stderrLineBuffer = "";
  let lastProgressAt = Date.now();
  const heartbeat = onProgress
    ? setInterval(() => {
        if (Date.now() - lastProgressAt >= 15_000) {
          lastProgressAt = Date.now();
          onProgress("Unity docs build still running...");
        }
      }, 15_000)
    : undefined;

  try {
    const result = await processRunner(pythonCommand, [scriptPath, ...args], {
      timeoutMs,
      signal,
      spawnOptions: {
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      },
      onStdout: (chunk) => { stdout += stdoutDecoder.write(chunk); },
      onStderr: (chunk) => {
        const text = stderrDecoder.write(chunk);
        stderr += text;
        stderrLineBuffer += text;
        const lines = stderrLineBuffer.split(/\r?\n/);
        stderrLineBuffer = (lines.pop() ?? "").slice(0, 8192);
        for (const line of lines) {
          if (native && line.startsWith("OBSERVATION ")) {
            try {
              const fact = JSON.parse(line.slice(12));
              if (["source", "ingestion", "database", "publication", "configuration", "source_configuration"].includes(fact.stage) && ["started", "completed"].includes(fact.state) && stages.length < 16) stages.push({ stage: fact.stage, state: fact.state });
            } catch {}
          }
          const message = formatProgressLine(line.trim());
          if (message && onProgress) {
            lastProgressAt = Date.now();
            onProgress(message);
          }
        }
      },
    });

    stdout += stdoutDecoder.end();
    const finalStderr = stderrDecoder.end();
    stderr += finalStderr;
    stderrLineBuffer += finalStderr;
    const dispatched = !result.notDispatched;
    if (result.error) throw new NativeFailure("process_error", stages, true);
    if (result.aborted) throw new NativeFailure("cancelled", stages, dispatched);
    if (result.captureOverflow) throw new NativeFailure("capture_overflow", stages, true);
    if (result.timedOut) throw new NativeFailure("timeout", stages, true);
    if (result.exitCode !== 0) throw new NativeFailure("process_exit", stages, true);
    const finalProgress = formatProgressLine(stderrLineBuffer.trim());
    if (finalProgress && onProgress) onProgress(finalProgress);
    let parsed: unknown | undefined;
    if (stdout.trim()) {
      try {
        parsed = JSON.parse(stdout);
      } catch {
        if (args.includes("--json")) throw new NativeFailure("invalid_json", stages, true);
      }
    }
    if (args.includes("--json") && parsed === undefined) throw new NativeFailure("invalid_json", stages, true);
    return { stdout, stderr, exitCode: result.exitCode, json: parsed, stages };
  } finally {
    if (heartbeat) clearInterval(heartbeat);
  }
};
}
