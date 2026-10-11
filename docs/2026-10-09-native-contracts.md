# Unity documentation native contracts v1

All seven tools return a closed, operation-specific envelope (`schemaVersion: 1`, `operation`, `ok`). Native DTOs are separate from useful direct text and existing renderer details. No hook restoration or persistence repair is performed.

## Bounds decided before wiring

| Family | Admitted facts | Bounds and overflow |
| --- | --- | --- |
| search/symbol | qualified pageId/docsetId/version and descriptive lookup rows; same-operation selected source status | 50 rows, 128 selected sources; identities 2048 characters exact (docset/version 256); descriptions 2048 characters; source queried/excluded/missing/failed; query-limited totals are not corpus totals |
| show | qualified page, sections, heading selection and producer truncation | 128 sections, text 50001 characters (existing 50000-character domain cap plus ellipsis); heading 2048; identity as above; section omissions counted |
| info | availability, version, whitelisted metadata counts/build date and bounded docset status | 128 docsets; no raw config, source paths, discovered paths or metadata dump in native output |
| validate | check kind/query, passed, resultCount, per-docset totals | 128 docsets, 128 checks per docset, query 2048; findings remain success, inability to inspect is failure |
| build_database/build_docset | observed source resolution/staging, ingestion, database staging/publication/config stages, counts, exact output DB and selector, producer-observed requested limit, source mode and limit policy | 16 fixed stage observations; DB reference 2048 exact; no source contents/argv/diagnostic dump; unknown effects after dispatch never authorize replay |

Compact DTO ceiling: 262144 UTF-8 bytes (resource/delivery safety). Direct presentation ceiling: 65536 UTF-8 bytes with explicit omission marker. Renderer details retain valid legacy shapes under a separate 262144-byte ceiling; oversized details are omitted, not replaced with a persisted DTO. Process stdout and stderr each have a 1048576-byte capture ceiling; pending stderr line 8192 bytes. Capture overflow terminates the process and returns failure, not empty data. Identity overflow fails the contract without clipping. Descriptive clipping and row omission are explicit. Existing lookup 50-row, validation 20-result and show 50000-character domain limits remain. Progress display is presentation-only.

## Outcomes

Completed zero-match queries succeed. Mixed usable sources succeed with partial coverage. All applicable sources missing/failed fail. Only search with positive valid routing evidence for all sources intentionally corpus-excluded succeeds without searched-absence evidence. Explicit missing/disabled selections and unknown routing fail. Legacy Python list/text clients retain their interface; native lookup mode is additive and performs no probes. SQL failures cannot be empty native rows.

Build stage records distinguish started/completed source resolution/staging and ingestion, database staging/publication and configuration. A failure after dispatch conservatively reports possible effects, retaining observed publication/configuration facts without readback/retry. Validation findings do not mean execution failed. A heading filter with no matching heading retains legacy all-section fallback and discloses it.

## Consumers

Codemode calls resolve the native value directly; check `ok` independently of rejection. `ctx.executeTool` returns an outcome wrapper, not the DTO itself. Host validation/block/throw/abort without a DTO may reject. Final SDK execution events can contain structured data; ordinary transcript results need not. Foreign hooks can remove/alter data and are not policed.

Use the returned exact `pageId` with its `docsetId` (or the same explicit database selector), never an unqualified ID from another database. Small whole-response printing is valid; selective output is optional.

`requestedLimit` is the observed requested cap, not a completeness guarantee. Core builds use a page limit; staged package ingestion has a source-dependent policy (for example XML ingestion is not universally capped by that flag). Native build success requires completed source, ingestion, database, publication and configuration observations. Row omissions aggregate admitted collection omissions; observed lookup row counts are only query-limited candidates, not all corpus matches.
