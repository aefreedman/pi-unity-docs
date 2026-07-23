import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { Text } from "@earendil-works/pi-tui";
import { fileURLToPath } from "node:url";
import path from "node:path";
import os from "node:os";
import fs from "node:fs";
import { selectPythonCommand } from "./scripts/python-selector.mjs";
import { runSupervisedProcess } from "./scripts/supervised-process.mjs";

const packageRoot = path.dirname(fileURLToPath(import.meta.url));
const scriptPath = path.join(packageRoot, "scripts", "unity_docs_db.py");
const pythonCommand = selectPythonCommand();

type ScriptResult = {
  stdout: string;
  stderr: string;
  exitCode: number | null;
  json?: unknown;
};

type TextToolResult = {
  content?: Array<{ type: string; text?: string }>;
  details?: unknown;
};

function shortenDisplay(value: string, max = 80): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= max) return normalized;
  return `${normalized.slice(0, max - 1)}…`;
}

function replaceTabs(value: string): string {
  return value.replace(/\t/g, "    ");
}

function trimTrailingEmptyLines(lines: string[]): string[] {
  let end = lines.length;
  while (end > 0 && lines[end - 1] === "") {
    end--;
  }
  return lines.slice(0, end);
}

function getTextOutput(result: TextToolResult): string {
  return result.content
    ?.filter((content) => content.type === "text" && typeof content.text === "string")
    .map((content) => content.text ?? "")
    .join("\n") ?? "";
}

function renderReadStyleCall(label: string, target: string | undefined, theme: any, context: any): Text {
  const text = (context.lastComponent as Text | undefined) ?? new Text("", 0, 0);
  const targetText = target ? theme.fg("accent", shortenDisplay(target)) : theme.fg("toolOutput", "...");
  text.setText(`${theme.fg("toolTitle", theme.bold(label))} ${targetText}`);
  return text;
}

function asRecord(value: unknown): Record<string, any> | undefined {
  return typeof value === "object" && value !== null ? value as Record<string, any> : undefined;
}

function asResultArray(value: unknown): Record<string, any>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, any> => typeof item === "object" && item !== null) : [];
}

function formatCount(value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  const numberValue = typeof value === "number" ? value : Number.parseInt(String(value), 10);
  return Number.isFinite(numberValue) ? numberValue.toLocaleString() : String(value);
}

function plural(count: number, singular: string, pluralText = `${singular}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? singular : pluralText}`;
}

function formatCollapsedSummary(kind: "info" | "search" | "symbol" | "show" | "build", result: TextToolResult): string | undefined {
  const details = asRecord(result.details);
  if (!details) return undefined;

  if (kind === "info") {
    const metadata = asRecord(details.metadata);
    if (!details.dbExists) return "database not found";
    const pages = formatCount(metadata?.pageCount);
    const sections = formatCount(metadata?.sectionCount);
    const symbols = formatCount(metadata?.symbolCount);
    return ["database ready", pages ? `${pages} pages` : undefined, sections ? `${sections} sections` : undefined, symbols ? `${symbols} symbols` : undefined]
      .filter(Boolean)
      .join("; ");
  }

  if (kind === "search" || kind === "symbol") {
    const results = asResultArray(details.results);
    if (results.length === 0) return kind === "search" ? "no results" : "no symbols found";
    const first = results[0];
    const page = String(first.pageId ?? first.page ?? "");
    const title = String(first.title ?? first.fullName ?? "");
    const heading = String(first.headingPath ?? "");
    const label = kind === "search" ? plural(results.length, "result") : plural(results.length, "symbol");
    const target = [page, title && title !== page ? title : undefined].filter(Boolean).join(" — ");
    return [label, target ? `top: ${target}` : undefined, heading ? `section: ${heading}` : undefined]
      .filter(Boolean)
      .join("; ");
  }

  if (kind === "show") {
    const page = asRecord(details.page);
    const sections = asResultArray(details.sections);
    const pageLabel = [page?.id, page?.title && page.title !== page.id ? page.title : undefined].filter(Boolean).join(" — ");
    return [pageLabel || "page loaded", plural(sections.length, "section"), details.truncated ? "truncated" : undefined]
      .filter(Boolean)
      .join("; ");
  }

  const pages = formatCount(details.pages);
  const sections = formatCount(details.sections);
  const symbols = formatCount(details.symbols);
  const elapsed = details.elapsedSeconds ? `${details.elapsedSeconds}s` : undefined;
  return ["build complete", pages ? `${pages} pages` : undefined, sections ? `${sections} sections` : undefined, symbols ? `${symbols} symbols` : undefined, elapsed]
    .filter(Boolean)
    .join("; ");
}

function renderReadStyleResult(
  kind: "info" | "search" | "symbol" | "show" | "build",
  result: TextToolResult,
  options: { expanded?: boolean; isPartial?: boolean },
  theme: any,
  context: any,
): Text {
  const text = (context.lastComponent as Text | undefined) ?? new Text("", 0, 0);
  const output = getTextOutput(result);

  if (options.isPartial) {
    const lastLine = trimTrailingEmptyLines(output.split("\n")).at(-1) ?? "Running...";
    text.setText(theme.fg("warning", shortenDisplay(lastLine, 140)));
    return text;
  }

  if (!options.expanded && !context.isError) {
    const summary = formatCollapsedSummary(kind, result);
    text.setText(summary ? theme.fg("toolOutput", shortenDisplay(summary, 180)) + theme.fg("dim", " (expand to read)") : "");
    return text;
  }

  const renderedLines = trimTrailingEmptyLines(output.split("\n").map(replaceTabs));
  const maxLines = options.expanded ? renderedLines.length : 10;
  const displayLines = renderedLines.slice(0, maxLines);
  const remaining = renderedLines.length - maxLines;
  let rendered = displayLines.map((line) => theme.fg("toolOutput", line)).join("\n");
  if (rendered) rendered = `\n${rendered}`;
  if (remaining > 0) {
    rendered += theme.fg("muted", `\n... (${remaining} more lines, expand to show all)`);
  }
  text.setText(rendered);
  return text;
}

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
    return line.slice("PROGRESS ".length).trim() || null;
  }
}

async function runScript(args: string[], timeoutMs = 120_000, onProgress?: (message: string) => void): Promise<ScriptResult> {
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
    const result = await runSupervisedProcess(pythonCommand, [scriptPath, ...args], {
      timeoutMs,
      spawnOptions: {
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      },
      onStdout: (chunk) => { stdout += chunk.toString(); },
      onStderr: (chunk) => {
        const text = chunk.toString();
        stderr += text;
        stderrLineBuffer += text;
        const lines = stderrLineBuffer.split(/\r?\n/);
        stderrLineBuffer = lines.pop() ?? "";
        for (const line of lines) {
          const message = formatProgressLine(line.trim());
          if (message && onProgress) {
            lastProgressAt = Date.now();
            onProgress(message);
          }
        }
      },
    });

    if (result.error) throw result.error;
    if (result.timedOut) throw new Error(`Unity docs script timed out after ${Math.round(timeoutMs / 1000)}s.`);

    const finalProgress = formatProgressLine(stderrLineBuffer.trim());
    if (finalProgress && onProgress) onProgress(finalProgress);
    if (result.exitCode !== 0) {
      throw new Error([`Unity docs script failed with exit code ${result.exitCode}.`, stderr.trim(), stdout.trim()].filter(Boolean).join("\n"));
    }
    let parsed: unknown | undefined;
    if (stdout.trim()) {
      try {
        parsed = JSON.parse(stdout);
      } catch {
        // Text mode output is still useful for diagnostics.
      }
    }
    return { stdout, stderr, exitCode: result.exitCode, json: parsed };
  } finally {
    if (heartbeat) clearInterval(heartbeat);
  }
}

function asArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null) : [];
}

function truncate(value: string, max = 500): string {
  if (value.length <= max) return value;
  return `${value.slice(0, max)}…`;
}

function formatConfiguredDocsetHint(info: unknown): string {
  const docsets = (info as { docsets?: Record<string, { title?: string; enabled?: boolean; dbExists?: boolean }> } | undefined)?.docsets ?? {};
  const configured = Object.entries(docsets)
    .filter(([, docset]) => docset.enabled !== false)
    .map(([id, docset]) => `${id}${docset.title ? ` (${docset.title})` : ""}${docset.dbExists === false ? " [missing db]" : ""}`)
    .slice(0, 12);
  if (configured.length === 0) return "";
  return [
    "",
    "Configured docsets you can target explicitly:",
    ...configured.map((entry) => `- ${entry}`),
    "Tip: for package/plugin questions, retry with docset/docsets set to the relevant id (for example shapes, input-system, odin, dotween).",
  ].join("\n");
}

async function buildNoResultDocsetHint(params: { db?: string; dbDir?: string; version?: string; projectPath?: string; profile?: string; docset?: string }): Promise<string> {
  try {
    return formatConfiguredDocsetHint((await runScript(["--json", "info", ...buildInfoArgs(params)], 30_000)).json);
  } catch {
    return "";
  }
}

function formatSearchResults(results: Record<string, unknown>[], noResultHint = ""): string {
  if (results.length === 0) return `No Unity documentation results found.${noResultHint}`;
  return results.map((result, index) => {
    const pageId = String(result.pageId ?? "");
    const title = String(result.title ?? "");
    const heading = String(result.headingPath ?? "");
    const snippet = String(result.snippet ?? "").replace(/\s+/g, " ").trim();
    const docset = String(result.docsetTitle ?? result.docsetId ?? "");
    const versionNote = result.versionMatch && result.versionMatch !== "exact" ? ` [${String(result.versionMatch)} for ${String(result.requestedVersion ?? "requested project version")}]` : "";
    return [
      `${index + 1}. ${pageId} — ${title}${docset ? ` [${docset}]` : ""}${versionNote}`,
      heading ? `   Section: ${heading}` : undefined,
      snippet ? `   ${truncate(snippet, 700)}` : undefined,
    ].filter(Boolean).join("\n");
  }).join("\n");
}

function formatSymbolResults(results: Record<string, unknown>[], noResultHint = ""): string {
  if (results.length === 0) return `No Unity API symbols found.${noResultHint}`;
  return results.map((result, index) => {
    const fullName = String(result.fullName ?? result.title ?? "");
    const pageId = String(result.pageId ?? "");
    const kind = String(result.kind ?? "");
    const signature = String(result.signature ?? "");
    const summary = String(result.summary ?? "");
    const docset = String(result.docsetTitle ?? result.docsetId ?? "");
    const versionNote = result.versionMatch && result.versionMatch !== "exact" ? ` [${String(result.versionMatch)} for ${String(result.requestedVersion ?? "requested project version")}]` : "";
    return [
      `${index + 1}. ${fullName}${kind ? ` [${kind}]` : ""}${docset ? ` [${docset}]` : ""}${versionNote}`,
      pageId ? `   Page: ${pageId}` : undefined,
      signature ? `   Signature: ${truncate(signature, 900)}` : undefined,
      summary ? `   Summary: ${truncate(summary, 500)}` : undefined,
    ].filter(Boolean).join("\n");
  }).join("\n");
}

function formatShowResult(value: unknown): string {
  const result = value as { page?: Record<string, unknown>; sections?: Record<string, unknown>[]; truncated?: boolean };
  const page = result.page ?? {};
  const sections = result.sections ?? [];
  const docset = String(page.docsetTitle ?? page.docsetId ?? "");
  const versionNote = page.versionMatch && page.versionMatch !== "exact" ? ` [${String(page.versionMatch)} for ${String(page.requestedVersion ?? "requested project version")}]` : "";
  const lines = [`# ${String(page.id ?? "")} — ${String(page.title ?? "")}${docset ? ` [${docset}]` : ""}${versionNote}`];
  if (page.summary) lines.push(`Summary: ${String(page.summary)}`);
  for (const section of sections) {
    lines.push(`\n## ${String(section.headingPath ?? "")}`);
    lines.push(String(section.text ?? ""));
  }
  if (result.truncated) lines.push("\n[truncated]");
  return lines.join("\n");
}

function inferUnityVersionFromSource(sourcePath: string): string {
  const normalized = sourcePath.replace(/\\/g, "/");
  const versionPattern = /^\d+\.\d+(?:\.(?:\d+|x).*)?$/i;
  const parts = normalized.split("/").filter(Boolean).reverse();
  return parts.find((part) => versionPattern.test(part)) ?? "unknown";
}

function defaultDbDir(version: string): string {
  const localAppData = process.env.LOCALAPPDATA;
  if (localAppData) return path.join(localAppData, "pi", "unity-docs", version);
  return path.join(os.homedir(), ".pi", "unity-docs", version);
}

function defaultUnityDocsSourceHint(platform: NodeJS.Platform = process.platform): string {
  if (platform === "darwin") {
    return "/Applications/Unity/Hub/Editor/<version>/Unity.app/Contents/Documentation/en";
  }
  if (platform === "win32") {
    return "C:/Program Files/Unity/Hub/Editor/<version>/Editor/Data/Documentation/en";
  }
  return path.join(os.homedir(), "Unity", "Hub", "Editor", "<version>", "Editor", "Data", "Documentation", "en");
}

function findAncestorUnityProjectSync(startDir?: string): string | undefined {
  if (!startDir) return undefined;
  let current = path.resolve(startDir);
  try {
    if (fs.existsSync(current) && fs.statSync(current).isFile()) current = path.dirname(current);
  } catch {
    return undefined;
  }
  while (true) {
    if (fs.existsSync(path.join(current, "ProjectSettings", "ProjectVersion.txt"))) return current;
    const parent = path.dirname(current);
    if (parent === current) return undefined;
    current = parent;
  }
}

function hasExplicitDocsSelector(params: { db?: string; dbDir?: string; version?: string; projectPath?: string; docset?: string; docsets?: string[] | string }): boolean {
  return Boolean(params.db || params.dbDir || params.version || params.projectPath || params.docset || (Array.isArray(params.docsets) ? params.docsets.length : params.docsets));
}

function buildCommonDbArgs(params: { db?: string; dbDir?: string; version?: string; projectPath?: string; profile?: string; docset?: string; docsets?: string[] | string }, cwd?: string): string[] {
  const args: string[] = [];
  if (!hasExplicitDocsSelector(params)) {
    const projectRoot = findAncestorUnityProjectSync(cwd);
    if (projectRoot) args.push("--project", projectRoot);
  }
  if (params.db) args.push("--db", params.db);
  if (params.dbDir) args.push("--db-dir", params.dbDir);
  if (params.version) args.push("--version", params.version);
  if (params.projectPath) args.push("--project", params.projectPath);
  if (params.profile) args.push("--profile", params.profile);
  if (params.docset) args.push("--docset", params.docset);
  if (params.docsets) args.push("--docsets", Array.isArray(params.docsets) ? params.docsets.join(",") : params.docsets);
  return args;
}

function buildInfoArgs(params: { db?: string; dbDir?: string; version?: string; projectPath?: string; profile?: string; docset?: string }, cwd?: string): string[] {
  const args: string[] = [];
  if (!hasExplicitDocsSelector(params)) {
    const projectRoot = findAncestorUnityProjectSync(cwd);
    if (projectRoot) args.push("--project", projectRoot);
  }
  if (params.db) args.push("--db", params.db);
  if (params.dbDir) args.push("--db-dir", params.dbDir);
  if (params.version) args.push("--version", params.version);
  if (params.projectPath) args.push("--project", params.projectPath);
  if (params.profile) args.push("--profile", params.profile);
  if (params.docset) args.push("--docset", params.docset);
  return args;
}

export default function (pi: ExtensionAPI) {
  pi.registerCommand("unity-docs-configure", {
    description: "Configure and optionally build the local Unity documentation database.",
    handler: async (_args, ctx) => {
      if (!ctx.hasUI) {
        ctx.ui.notify("/unity-docs-configure requires an interactive UI. Use scripts/unity_docs_db.py configure instead.", "error");
        return;
      }

      let discoveredSources: string[] = [];
      try {
        const info = await runScript(["--json", "info", "--discover"], 30_000);
        discoveredSources = (info.json as { discoveredSources?: string[] } | undefined)?.discoveredSources ?? [];
      } catch {
        discoveredSources = [];
      }

      const manualChoice = "Enter a Unity documentation path manually...";
      let sourcePath = "";
      if (discoveredSources.length > 0) {
        const labels = discoveredSources.map((source) => `${inferUnityVersionFromSource(source)} — ${source}`);
        const selected = await ctx.ui.select("Select Unity documentation source", [...labels, manualChoice]);
        if (!selected) {
          ctx.ui.notify("Unity docs configuration cancelled.", "info");
          return;
        }
        if (selected !== manualChoice) {
          const selectedIndex = labels.indexOf(selected);
          sourcePath = discoveredSources[selectedIndex] ?? "";
        }
      }

      if (!sourcePath) {
        sourcePath = await ctx.ui.input("Unity documentation source directory", discoveredSources[0] || defaultUnityDocsSourceHint()) ?? "";
        if (!sourcePath) {
          ctx.ui.notify("Unity docs configuration cancelled.", "info");
          return;
        }
      }

      const inferredVersion = inferUnityVersionFromSource(sourcePath);
      let version = inferredVersion;
      if (!version || version === "unknown") {
        version = await ctx.ui.input("Unity version label", inferredVersion || "6000.0.0f1") ?? "";
        if (!version) {
          ctx.ui.notify("Unity docs configuration cancelled.", "info");
          return;
        }
      }

      const dbDir = defaultDbDir(version);

      const configured = await runScript(["--json", "configure", "--source", sourcePath, "--db-dir", dbDir, "--version", version, "--yes"], 60_000);
      const buildNow = await ctx.ui.confirm("Build Unity docs database now?", "This may take several minutes for the full ScriptReference.");
      if (!buildNow) {
        ctx.ui.notify(`Configured Unity docs database. Build later with unity_docs_build_database or scripts/unity_docs_db.py build.\n${configured.stdout.trim()}`, "info");
        return;
      }

      ctx.ui.notify("Building Unity docs database. This may take several minutes...", "info");
      const built = await runScript(
        ["--json", "build", "--source", sourcePath, "--db-dir", dbDir, "--version", version, "--force", "--progress"],
        3_600_000,
        (message) => ctx.ui.setStatus("unity-docs", message),
      );
      ctx.ui.setStatus("unity-docs", "Unity docs build complete");
      ctx.ui.notify(`Unity docs database built.\n${built.stdout.trim()}`, "info");
    },
  });

  pi.registerTool({
    name: "unity_docs_info",
    label: "Unity Docs Info",
    description: "Show local Unity documentation database configuration and status.",
    promptSnippet: "Show Unity documentation cache configuration and database status.",
    promptGuidelines: ["Use unity_docs_info when the Unity docs cache status or configured database path is unknown."],
    parameters: Type.Object({
      discover: Type.Optional(Type.Boolean({ description: "Include discovered Unity Documentation/en source directories." })),
      db: Type.Optional(Type.String({ description: "Explicit SQLite database path." })),
      dbDir: Type.Optional(Type.String({ description: "Directory containing unity_docs.sqlite." })),
      version: Type.Optional(Type.String({ description: "Unity version label from config." })),
      projectPath: Type.Optional(Type.String({ description: "Unity project path used to select the matching docs version from ProjectSettings/ProjectVersion.txt." })),
      profile: Type.Optional(Type.String({ description: "Documentation profile from config." })),
      docset: Type.Optional(Type.String({ description: "Documentation docset id from config." })),
    }),
    renderCall(args, theme, context) {
      return renderReadStyleCall("unity docs info", args.projectPath ?? args.version ?? args.dbDir ?? args.db ?? "configured database", theme, context);
    },
    renderResult(result, options, theme, context) {
      return renderReadStyleResult("info", result, options, theme, context);
    },
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const args = ["--json", "info", ...buildInfoArgs(params, ctx?.cwd)];
      if (params.discover) args.push("--discover");
      const result = await runScript(args, 60_000);
      return {
        content: [{ type: "text", text: JSON.stringify(result.json, null, 2) }],
        details: result.json as object,
      };
    },
  });

  pi.registerTool({
    name: "unity_docs_search",
    label: "Unity Docs Search",
    description: "Search the local Unity Manual and ScriptReference SQLite FTS cache and return compact section snippets.",
    promptSnippet: "Search local Unity documentation by terms and return compact section snippets.",
    promptGuidelines: [
      "Use unity_docs_search for Unity Manual or Scripting API documentation lookups when exact symbol lookup is insufficient.",
      "For token efficiency, call unity_docs_show only for the most relevant result sections after unity_docs_search.",
    ],
    parameters: Type.Object({
      query: Type.String({ description: "Search terms, for example 'Physics.Raycast layerMask trigger'." }),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50, default: 8 })),
      corpus: Type.Optional(Type.Union([Type.Literal("Manual"), Type.Literal("ScriptReference"), Type.Literal("Package")], { description: "Optional corpus filter." })),
      db: Type.Optional(Type.String({ description: "Explicit SQLite database path." })),
      dbDir: Type.Optional(Type.String({ description: "Directory containing unity_docs.sqlite." })),
      version: Type.Optional(Type.String({ description: "Unity version label from config." })),
      projectPath: Type.Optional(Type.String({ description: "Unity project path used to select the matching docs version from ProjectSettings/ProjectVersion.txt." })),
      profile: Type.Optional(Type.String({ description: "Documentation profile from config." })),
      docset: Type.Optional(Type.String({ description: "Documentation docset id from config." })),
      docsets: Type.Optional(Type.Array(Type.String(), { description: "Documentation docset ids from config." })),
    }),
    renderCall(args, theme, context) {
      return renderReadStyleCall("unity docs search", args.query, theme, context);
    },
    renderResult(result, options, theme, context) {
      return renderReadStyleResult("search", result, options, theme, context);
    },
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const args = ["--json", "search", params.query, "--limit", String(params.limit ?? 8), ...buildCommonDbArgs(params, ctx?.cwd)];
      if (params.corpus) args.push("--corpus", params.corpus);
      const result = await runScript(args, 120_000);
      const rows = asArray(result.json);
      const hint = rows.length === 0 ? await buildNoResultDocsetHint(params) : "";
      return {
        content: [{ type: "text", text: formatSearchResults(rows, hint) }],
        details: { results: rows, noResultHint: hint || undefined },
      };
    },
  });

  pi.registerTool({
    name: "unity_docs_symbol",
    label: "Unity Docs Symbol",
    description: "Look up Unity Scripting API pages by exact or near-exact symbol name.",
    promptSnippet: "Look up Unity Scripting API symbols such as UnityEngine.Physics.Raycast.",
    promptGuidelines: ["Use unity_docs_symbol before unity_docs_search for API-like Unity queries such as Physics.Raycast or GameObject.AddComponent."],
    parameters: Type.Object({
      name: Type.String({ description: "API symbol, for example 'UnityEngine.Physics.Raycast' or 'Physics.Raycast'." }),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50, default: 8 })),
      db: Type.Optional(Type.String({ description: "Explicit SQLite database path." })),
      dbDir: Type.Optional(Type.String({ description: "Directory containing unity_docs.sqlite." })),
      version: Type.Optional(Type.String({ description: "Unity version label from config." })),
      projectPath: Type.Optional(Type.String({ description: "Unity project path used to select the matching docs version from ProjectSettings/ProjectVersion.txt." })),
      profile: Type.Optional(Type.String({ description: "Documentation profile from config." })),
      docset: Type.Optional(Type.String({ description: "Documentation docset id from config." })),
      docsets: Type.Optional(Type.Array(Type.String(), { description: "Documentation docset ids from config." })),
    }),
    renderCall(args, theme, context) {
      return renderReadStyleCall("unity docs symbol", args.name, theme, context);
    },
    renderResult(result, options, theme, context) {
      return renderReadStyleResult("symbol", result, options, theme, context);
    },
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const result = await runScript(["--json", "symbol", params.name, "--limit", String(params.limit ?? 8), ...buildCommonDbArgs(params, ctx?.cwd)], 120_000);
      const rows = asArray(result.json);
      const hint = rows.length === 0 ? await buildNoResultDocsetHint(params) : "";
      return {
        content: [{ type: "text", text: formatSymbolResults(rows, hint) }],
        details: { results: rows, noResultHint: hint || undefined },
      };
    },
  });

  pi.registerTool({
    name: "unity_docs_show",
    label: "Unity Docs Show",
    description: "Retrieve compact sections from a local Unity documentation page.",
    promptSnippet: "Show selected sections from a local Unity documentation page.",
    promptGuidelines: ["Use unity_docs_show after unity_docs_symbol or unity_docs_search, requesting only the sections needed to answer the user."],
    parameters: Type.Object({
      page: Type.String({ description: "Page id, slug, title, or url path, for example 'ScriptReference/Physics.Raycast'." }),
      sections: Type.Optional(Type.Array(Type.String(), { description: "Optional heading filters, for example ['Declaration','Parameters','Description']." })),
      maxChars: Type.Optional(Type.Integer({ minimum: 500, maximum: 50000, default: 6000 })),
      db: Type.Optional(Type.String({ description: "Explicit SQLite database path." })),
      dbDir: Type.Optional(Type.String({ description: "Directory containing unity_docs.sqlite." })),
      version: Type.Optional(Type.String({ description: "Unity version label from config." })),
      projectPath: Type.Optional(Type.String({ description: "Unity project path used to select the matching docs version from ProjectSettings/ProjectVersion.txt." })),
      profile: Type.Optional(Type.String({ description: "Documentation profile from config." })),
      docset: Type.Optional(Type.String({ description: "Documentation docset id from config." })),
      docsets: Type.Optional(Type.Array(Type.String(), { description: "Documentation docset ids from config." })),
    }),
    renderCall(args, theme, context) {
      return renderReadStyleCall("unity docs show", args.page, theme, context);
    },
    renderResult(result, options, theme, context) {
      return renderReadStyleResult("show", result, options, theme, context);
    },
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const args = ["--json", "show", params.page, "--max-chars", String(params.maxChars ?? 6000), ...buildCommonDbArgs(params, ctx?.cwd)];
      if (params.sections?.length) args.push("--sections", params.sections.join(","));
      const result = await runScript(args, 120_000);
      return {
        content: [{ type: "text", text: formatShowResult(result.json) }],
        details: result.json as object,
      };
    },
  });

  pi.registerTool({
    name: "unity_docs_validate",
    label: "Unity Docs Validate",
    description: "Run representative validation queries against configured Unity documentation docsets.",
    promptSnippet: "Validate configured Unity documentation docsets with representative searches.",
    parameters: Type.Object({
      db: Type.Optional(Type.String({ description: "Explicit SQLite database path." })),
      dbDir: Type.Optional(Type.String({ description: "Directory containing unity_docs.sqlite." })),
      version: Type.Optional(Type.String({ description: "Unity version label from config." })),
      projectPath: Type.Optional(Type.String({ description: "Unity project path used to select the matching docs version from ProjectSettings/ProjectVersion.txt." })),
      profile: Type.Optional(Type.String({ description: "Documentation profile from config." })),
      docset: Type.Optional(Type.String({ description: "Documentation docset id from config." })),
      docsets: Type.Optional(Type.Array(Type.String(), { description: "Documentation docset ids from config." })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20, description: "Maximum results per validation query." })),
    }),
    renderCall(args, theme, context) {
      return renderReadStyleCall("unity docs validate", args.docset ?? args.profile ?? "configured docsets", theme, context);
    },
    renderResult(result, _options, theme, context) {
      const text = (context.lastComponent as Text | undefined) ?? new Text("", 0, 0);
      const details = asRecord(result.details);
      const summary = details ? `${details.passed ?? 0}/${details.total ?? 0} checks passed` : getTextOutput(result);
      text.setText(theme.fg("toolOutput", summary));
      return text;
    },
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const args = ["--json", "validate", "--limit", String(params.limit ?? 5), ...buildCommonDbArgs(params, ctx?.cwd)];
      const result = await runScript(args, 300_000);
      return {
        content: [{ type: "text", text: `Unity docs validation complete.\n${JSON.stringify(result.json, null, 2)}` }],
        details: result.json as object,
      };
    },
  });

  pi.registerTool({
    name: "unity_docs_build_docset",
    label: "Unity Docs Build Docset",
    description: "Build or rebuild a local SQLite documentation cache from package Documentation~, an llms.txt manifest, public HTML, or C# XML docs.",
    promptSnippet: "Build or rebuild a Unity package or external documentation docset.",
    promptGuidelines: ["Use unity_docs_build_docset only when the user explicitly asks to build or rebuild package/plugin documentation."],
    parameters: Type.Object({
      docsetId: Type.Optional(Type.String({ description: "Docset id to register. Defaults to packageName." })),
      sourcePath: Type.Optional(Type.String({ description: "Package Documentation~ source directory, or package root containing Documentation~." })),
      projectPath: Type.Optional(Type.String({ description: "Unity project path used to resolve Packages or Library/PackageCache." })),
      packageName: Type.Optional(Type.String({ description: "Unity package name to resolve from a project or record in metadata." })),
      packageVersion: Type.Optional(Type.String({ description: "Optional package version to resolve from PackageCache or record in metadata." })),
      title: Type.Optional(Type.String({ description: "Human-readable docset title." })),
      llmsUrl: Type.Optional(Type.String({ description: "llms.txt URL to mirror into a temporary Markdown source before building." })),
      llmsSection: Type.Optional(Type.String({ description: "Optional llms.txt section heading to mirror." })),
      gitbookLlmsUrl: Type.Optional(Type.String({ description: "Compatibility alias for llmsUrl." })),
      gitbookSection: Type.Optional(Type.String({ description: "Compatibility alias for llmsSection." })),
      htmlUrls: Type.Optional(Type.Array(Type.String(), { description: "Public HTML documentation URLs to convert to Markdown before building." })),
      htmlSplitLevel: Type.Optional(Type.Integer({ minimum: 1, maximum: 6, description: "Split converted HTML pages into Markdown pages at this heading level." })),
      xmlDocs: Type.Optional(Type.Array(Type.String(), { description: "C# XML documentation files to convert to Markdown API pages before building." })),
      dbDir: Type.String({ description: "Directory where unity_docs.sqlite should be installed." }),
      force: Type.Optional(Type.Boolean({ default: false, description: "Replace an existing database." })),
      limit: Type.Optional(Type.Integer({ minimum: 1, description: "Debug/testing: process only this many Markdown or mirrored pages." })),
    }),
    renderCall(args, theme, context) {
      return renderReadStyleCall("unity docs build docset", args.docsetId ?? args.packageName ?? args.sourcePath ?? args.projectPath, theme, context);
    },
    renderResult(result, options, theme, context) {
      return renderReadStyleResult("build", result, options, theme, context);
    },
    async execute(_toolCallId, params, _signal, onUpdate) {
      const args = ["--json", "build-docset", "--db-dir", params.dbDir, "--progress"];
      if (params.docsetId) args.push("--docset-id", params.docsetId);
      if (params.sourcePath) args.push("--source", params.sourcePath);
      if (params.projectPath) args.push("--project", params.projectPath);
      if (params.packageName) args.push("--package-name", params.packageName);
      if (params.packageVersion) args.push("--package-version", params.packageVersion);
      if (params.title) args.push("--title", params.title);
      if (params.llmsUrl && params.gitbookLlmsUrl) throw new Error("Pass only one of llmsUrl or gitbookLlmsUrl.");
      if (params.llmsSection && params.gitbookSection) throw new Error("Pass only one of llmsSection or gitbookSection.");
      const llmsUrl = params.llmsUrl ?? params.gitbookLlmsUrl;
      const llmsSection = params.llmsSection ?? params.gitbookSection;
      if (llmsUrl) args.push("--llms-url", llmsUrl);
      if (llmsSection) args.push("--llms-section", llmsSection);
      for (const url of params.htmlUrls ?? []) args.push("--html-url", url);
      if (params.htmlSplitLevel) args.push("--html-split-level", String(params.htmlSplitLevel));
      for (const xmlDoc of params.xmlDocs ?? []) args.push("--xml-doc", xmlDoc);
      if (params.force) args.push("--force");
      if (params.limit) args.push("--limit", String(params.limit));
      onUpdate?.({ content: [{ type: "text", text: "Building Unity package documentation docset..." }] });
      const result = await runScript(args, 3_600_000, (message) => {
        onUpdate?.({ content: [{ type: "text", text: message }] });
      });
      return {
        content: [{ type: "text", text: `Unity package documentation docset build complete.\n${JSON.stringify(result.json, null, 2)}` }],
        details: result.json as object,
      };
    },
  });

  pi.registerTool({
    name: "unity_docs_build_database",
    label: "Unity Docs Build Database",
    description: "Build or rebuild the local SQLite Unity documentation cache. Use only when explicitly requested by the user.",
    promptSnippet: "Build or rebuild the SQLite Unity documentation cache from an installed Unity docs directory.",
    promptGuidelines: ["Use unity_docs_build_database only when the user explicitly asks to build or rebuild the Unity docs cache."],
    parameters: Type.Object({
      sourcePath: Type.String({ description: "Unity Documentation/en source directory." }),
      dbDir: Type.String({ description: "Directory where unity_docs.sqlite should be installed." }),
      version: Type.Optional(Type.String({ description: "Unity version label. Inferred from source path when omitted." })),
      force: Type.Optional(Type.Boolean({ default: false, description: "Replace an existing database." })),
      limit: Type.Optional(Type.Integer({ minimum: 1, description: "Debug/testing: process only this many pages." })),
    }),
    renderCall(args, theme, context) {
      return renderReadStyleCall("unity docs build", args.version ?? args.dbDir, theme, context);
    },
    renderResult(result, options, theme, context) {
      return renderReadStyleResult("build", result, options, theme, context);
    },
    async execute(_toolCallId, params, _signal, onUpdate) {
      const version = params.version ?? inferUnityVersionFromSource(params.sourcePath);
      const args = ["--json", "build", "--source", params.sourcePath, "--db-dir", params.dbDir, "--version", version, "--progress"];
      if (params.force) args.push("--force");
      if (params.limit) args.push("--limit", String(params.limit));
      onUpdate?.({ content: [{ type: "text", text: "Building Unity docs database. This can take several minutes..." }] });
      const result = await runScript(args, 3_600_000, (message) => {
        onUpdate?.({ content: [{ type: "text", text: message }] });
      });
      return {
        content: [{ type: "text", text: `Unity docs database build complete.\n${JSON.stringify(result.json, null, 2)}` }],
        details: result.json as object,
      };
    },
  });
}
