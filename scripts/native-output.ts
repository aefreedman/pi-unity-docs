import { Type } from "typebox";
import { Value } from "typebox/value";

export const DTO_BYTES = 262144;
export const PRESENTATION_BYTES = 65536;
const closed = (fields: any) => Type.Object(fields, { additionalProperties: false });
const str = (maxLength = 2048) => Type.String({ maxLength });
const count = Type.Integer({ minimum: 0 });
const optional = Type.Optional;
const nullable = (schema: any) => Type.Union([schema, Type.Null()]);
const identityFields = { pageId: str(), docsetId: str(256), version: optional(nullable(str(256))) };
const row = closed({ ...identityFields, title: optional(nullable(str())), headingPath: optional(nullable(str())), snippet: optional(nullable(str())), fullName: optional(nullable(str())), shortName: optional(nullable(str())), kind: optional(nullable(str())), signature: optional(nullable(str())), summary: optional(nullable(str())), requestedVersion: optional(nullable(str(256))), versionMatch: optional(nullable(str(256))) });
const source = closed({ docsetId: str(256), status: Type.Union([Type.Literal("queried"), Type.Literal("excluded"), Type.Literal("missing"), Type.Literal("failed")]) });
const stage = closed({ stage: Type.Union(["source", "ingestion", "database", "publication", "configuration", "source_configuration"].map(Type.Literal)), state: Type.Union([Type.Literal("started"), Type.Literal("completed")]) });
const effects = closed({ dispatched: Type.Boolean(), possibleChanges: Type.Boolean(), stages: Type.Array(stage, { maxItems: 16 }), replaySafe: Type.Literal(false) });
const metadata = closed(Object.fromEntries(["pageCount", "sectionCount", "symbolCount", "builtAt", "version", "docsetId", "docsetKind", "docsetTitle", "packageName", "packageVersion"].map(k => [k, optional(nullable(str()))])));
const status = closed({ docsetId: str(256), title: optional(nullable(str())), enabled: optional(Type.Boolean()), dbExists: Type.Boolean(), sourceExists: optional(nullable(Type.Boolean())), sourceRemote: optional(Type.Boolean()), metadata: optional(metadata) });
const check = closed({ kind: str(256), query: str(), passed: Type.Boolean(), resultCount: count });
const families: Record<string, any> = {
  search: { rows: Type.Array(row, { maxItems: 50 }), coverage: Type.Array(source, { maxItems: 128 }), completeness: Type.Union([Type.Literal("complete"), Type.Literal("partial"), Type.Literal("excluded")]), observedRows: count, queryLimited: Type.Literal(true) },
  symbol: { rows: Type.Array(row, { maxItems: 50 }), coverage: Type.Array(source, { maxItems: 128 }), completeness: Type.Union([Type.Literal("complete"), Type.Literal("partial")]), observedRows: count, queryLimited: Type.Literal(true) },
  show: { page: row, sections: Type.Array(closed({ headingPath: str(), text: str(50001), tokenEstimate: optional(count) }), { maxItems: 128 }), truncated: Type.Boolean(), headingSelection: Type.Union([Type.Literal("all"), Type.Literal("filtered"), Type.Literal("fallback_all")]), selectedSections: count },
  info: { dbExists: Type.Boolean(), metadata: optional(metadata), projectVersion: optional(str(256)), resolvedDocsVersion: optional(str(256)), versionMatch: optional(str(256)), docsets: Type.Array(status, { maxItems: 128 }) },
  validate: { passed: count, total: count, failed: count, docsets: Type.Array(closed({ docsetId: str(256), checkCount: count, passed: nullable(Type.Boolean()), checks: Type.Array(check, { maxItems: 128 }) }), { maxItems: 128 }) },
  build_database: { dbPath: str(), version: str(256), pages: count, sections: count, symbols: count, elapsedSeconds: Type.Number({ minimum: 0 }), requestedLimit: nullable(count), limitPolicy: Type.Union([Type.Literal("page_limit"), Type.Literal("source_dependent")]), sourceMode: str(256), sourceReadbackVerified: Type.Literal(false), effects },
  build_docset: { dbPath: str(), docsetId: str(256), packageVersion: optional(str(256)), pages: count, sections: count, symbols: count, elapsedSeconds: Type.Number({ minimum: 0 }), requestedLimit: nullable(count), limitPolicy: Type.Union([Type.Literal("page_limit"), Type.Literal("source_dependent")]), sourceMode: str(256), sourceReadbackVerified: Type.Literal(false), effects },
};
export function outputSchema(operation: string) {
  const base = { schemaVersion: Type.Literal(1), operation: Type.Literal(operation) };
  return Type.Union([
    closed({ ...base, ok: Type.Literal(true), ...families[operation], omissions: closed({ rows: count, fields: count }) }),
    closed({ ...base, ok: Type.Literal(false), error: closed({ code: str(256), message: str() }), effects: optional(effects), coverage: optional(Type.Array(source, { maxItems: 128 })) }),
  ]);
}
export class NativeFailure extends Error {
  constructor(public code: string, public stages: any[] = [], public dispatched = false) { super(`Unity documentation operation failed (${code}).`); }
}
function record(value: any) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new NativeFailure("invalid_shape");
  return value;
}
function array(value: any) {
  if (!Array.isArray(value)) throw new NativeFailure("invalid_shape");
  return value;
}
export function project(operation: string, input: any, params: any, stages: any[] = []) {
  const v = record(input);
  let fields = 0, rows = 0;
  const exact = (x: any, max = 2048) => {
    if (typeof x !== "string" || x.length > max || !x.length) throw new NativeFailure("invalid_identity", stages, operation.startsWith("build_"));
    return x;
  };
  const pick = (x: any, keys: string[], identities: string[] = []) => {
    record(x);
    const out: any = {};
    for (const k of keys) if (Object.hasOwn(x, k)) {
      const val = x[k];
      if (identities.includes(k)) out[k] = val === null && k !== "docsetId" ? null : exact(val, k.includes("Version") || k === "version" || k === "docsetId" ? 256 : 2048);
      else if (typeof val === "string" && val.length > 2048) { fields++; out[k] = val.slice(0, 2047) + "…"; }
      else out[k] = val;
    }
    return out;
  };
  const slice = (x: any, cap: number) => { const a = array(x); rows += Math.max(0, a.length - cap); return a.slice(0, cap); };
  const projectRow = (x: any, show = false) => {
    const r = pick(x, ["pageId", "docsetId", "version", "title", "headingPath", "snippet", "fullName", "shortName", "kind", "signature", "summary", "requestedVersion", "versionMatch"], ["pageId", "docsetId", "version", "requestedVersion", "fullName"]);
    if (show) r.pageId = exact(x.id);
    // Direct-db callers must keep that same selector; docsetId is producer metadata, not a global page identity.
    exact(r.pageId); exact(r.docsetId, 256);
    return r;
  };
  const projectMetadata = (x: any) => pick(x, ["pageCount", "sectionCount", "symbolCount", "builtAt", "version", "docsetId", "docsetKind", "docsetTitle", "packageName", "packageVersion"], ["version", "docsetId", "packageVersion"]);
  let data: any;
  if (operation === "search" || operation === "symbol") {
    const coverage = array(v.coverage).map(x => ({ docsetId: exact(record(x).docsetId, 256), status: x.status }));
    if (coverage.length > 128) throw new NativeFailure("selection_limit");
    if (typeof v.ok !== "boolean" || !coverage.length || coverage.some(x => !["queried", "excluded", "missing", "failed"].includes(x.status))) throw new NativeFailure("invalid_shape");
    const usable = coverage.some(x => x.status === "queried");
    const intentionallyExcluded = operation === "search" && coverage.every(x => x.status === "excluded");
    if (!v.ok || (!usable && !intentionallyExcluded)) return failure(operation, new NativeFailure("source_unavailable"), coverage);
    data = { rows: slice(v.rows, 50).map(x => projectRow(x)), coverage, completeness: coverage.every(x => x.status === "excluded") ? "excluded" : coverage.some(x => x.status === "missing" || x.status === "failed") ? "partial" : "complete", observedRows: v.observedRows, queryLimited: v.queryLimited };
    rows += Math.max(0, v.observedRows - v.rows.length);
  } else if (operation === "show") {
    data = { page: projectRow(v.page, true), sections: slice(v.sections, 128).map(x => pick(x, ["headingPath", "tokenEstimate"])), truncated: v.truncated, headingSelection: v.headingSelection, selectedSections: v.selectedSections };
    // Page text already obeys the canonical maxChars domain cap; do not apply descriptive clipping to it.
    data.sections.forEach((x: any, i: number) => { x.text = v.sections[i].text; });
  } else if (operation === "info") {
    data = { ...pick(v, ["dbExists", "projectVersion", "resolvedDocsVersion", "versionMatch"], ["projectVersion", "resolvedDocsVersion"]), docsets: slice(Object.entries(v.docsets ?? {}), 128).map(([id, x]: any) => ({ docsetId: exact(id, 256), ...pick(x, ["title", "enabled", "dbExists", "sourceExists", "sourceRemote"]), ...(x.metadata ? { metadata: projectMetadata(x.metadata) } : {}) })) };
    if (v.metadata) data.metadata = projectMetadata(v.metadata);
  } else if (operation === "validate") {
    data = { ...pick(v, ["passed", "total", "failed"]), docsets: slice(v.docsets, 128).map(x => ({ ...pick(x, ["docsetId", "checkCount", "passed"], ["docsetId"]), checks: slice(x.checks, 128).map(c => pick(c, ["kind", "query", "passed", "resultCount"])) })) };
  } else {
    if (!["source", "ingestion", "database", "publication", "configuration"].every(stage => stages.some(x => x.stage === stage && x.state === "completed"))) throw new NativeFailure("incomplete_build_receipt", stages, true);
    data = { ...pick(v, ["dbPath", ...(operation === "build_database" ? ["version"] : ["docsetId", "packageVersion"]), "pages", "sections", "symbols", "elapsedSeconds", "requestedLimit", "limitPolicy", "sourceMode"], ["dbPath", "version", "docsetId", "packageVersion"]), sourceReadbackVerified: false, effects: { dispatched: true, possibleChanges: true, stages, replaySafe: false } };
  }
  const dto = { schemaVersion: 1, operation, ok: true, ...data, omissions: { rows, fields } };
  if (!Value.Check(outputSchema(operation), dto) || Buffer.byteLength(JSON.stringify(dto)) > DTO_BYTES) throw new NativeFailure("contract_overflow_or_shape", stages, operation.startsWith("build_"));
  return dto;
}
export function failure(operation: string, error: unknown, coverage?: any[]) {
  const e = error instanceof NativeFailure ? error : new NativeFailure("operation_failed", [], operation.startsWith("build_"));
  const dto: any = { schemaVersion: 1, operation, ok: false, error: { code: e.code, message: e.message } };
  if (operation.startsWith("build_")) dto.effects = { dispatched: e.dispatched, possibleChanges: e.dispatched, stages: e.stages.slice(0, 16), replaySafe: false };
  if (coverage) dto.coverage = coverage;
  if (!Value.Check(outputSchema(operation), dto) || Buffer.byteLength(JSON.stringify(dto)) > DTO_BYTES) {
    delete dto.coverage;
    if (dto.effects) dto.effects.stages = [];
  }
  return dto;
}
export function boundedText(text: string) {
  if (Buffer.byteLength(text) <= PRESENTATION_BYTES) return text;
  const marker = "\n[presentation omitted: byte limit]";
  let end = Math.min(text.length, PRESENTATION_BYTES - Buffer.byteLength(marker));
  while (Buffer.byteLength(text.slice(0, end)) > PRESENTATION_BYTES - Buffer.byteLength(marker)) end--;
  if (end > 0 && /[\uD800-\uDBFF]/.test(text[end - 1])) end--;
  return text.slice(0, end) + marker;
}
