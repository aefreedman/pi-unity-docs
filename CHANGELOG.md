# Changelog

## Unreleased

### Added

- Closed versioned native contracts for all seven documentation tools, with qualified references, bounded projections, source coverage and conservative build receipts.

### Fixed

- Reject malformed JSON/native shapes instead of reporting empty matches; observe native SQL/missing-source failures and intentional corpus exclusions without extra probes.
- Bound subprocess capture and safe diagnostics, propagate cancellation, and disclose heading-filter fallback and build publication/configuration stages.

### Changed

- Align offline and deterministic SDK validation with Pi 1.1.0; retain legacy Python/text interfaces and useful bounded direct presentation.

## 0.6.4 - 2026-09-29

### Changed

- Require Node >=22.19.0 for the Pi runtime and release runner.
- Align development and deterministic validation with Pi 0.99.1.
- Use locked dependency installation for release validation.

## 0.6.3 - 2026-09-21

### Changed

- Update Pi development dependencies and validation baseline to 0.87.0.

## 0.6.2 - 2026-09-20

### Changed

- Pin development validation dependencies to Pi 0.86.1 while retaining optional wildcard peers for Pi-provided runtime packages.

### Fixed

- Include JSON-compatible progress details in documentation build tool updates required by Pi 0.86.1.

## 0.6.1 - 2026-08-05

### Added

- Added guarded tag-triggered trusted publishing with npm source-commit reconciliation.

## 0.6.0 - 2026-08-05

### Added

- Added an explicit local HTML file input for package documentation docsets.
- Added a tag-triggered npm trusted-publishing workflow for releases after the manual bootstrap publication.

### Changed

- Limited remote documentation ingestion to bounded public HTTPS sources and same-origin `llms.txt` links.
- Reduced the npm artifact to runtime files and consumer documentation.
- Added npm installation guidance and public package metadata.

## 0.5.1 - 2026-07-24

### Changed

- Marked Pi-bundled core dependencies as optional peers so Pi git installs do not create redundant per-package `node_modules` directories.

## 0.5.0 - 2026-07-23

### Added

- Added generic `llms.txt` manifest inputs and a documented Unity CLI docset recipe while retaining the GitBook-named options as compatibility aliases.
- Added retrieval guidance that separates standalone Unity CLI documentation from versioned `com.unity.pipeline` command documentation.
- Added default validation queries for the `unity-cli` docset and versioned `com.unity.pipeline` package docsets.

### Fixed

- Preserve source-page URLs for Markdown downloaded through an `llms.txt` manifest and report remote docset sources without applying local filesystem existence semantics.

## 0.4.0 - 2026-07-09

### Added

- Added project-aware Unity documentation selection from `ProjectSettings/ProjectVersion.txt`, including exact, same-minor line, and nearest-patch matching across configured Unity databases.
- Exposed configured Unity databases as `unity-<version>` docsets for explicit multi-version queries.

### Changed

- Selected `python3` by default on macOS/Linux while retaining `python` on Windows and the `PI_UNITY_DOCS_PYTHON` override.
- Improved Unity docs setup to select among discovered documentation sources and default database directories by Unity version.
- Included version-match metadata in search, symbol, and page output when project-aware fallback selection is used.
- Bound Python tool timeouts with SIGTERM/SIGKILL escalation and cleanup.
- Added macOS CI coverage using Python 3 for tests and package validation.
- Made direct CLI examples and interactive source-path hints platform-appropriate on macOS, Windows, and Linux.

## 0.3.0 - 2026-06-27

### Changed

- Improved no-result search/symbol output with configured docset hints so agents can retry package/plugin queries against explicit docsets such as `shapes`.

## 0.2.1 - 2026-06-27

### Changed

- Clarified `using-unity-docs` package-docset guidance so agents explicitly inspect/search configured plugin docsets such as Shapes when package-specific documentation is relevant.

## 0.2.0 - 2026-06-26

### Added

- Added package docset builds from GitBook `llms.txt`, public HTML pages, and C# XML documentation files.
- Added `unity_docs_validate` and `validate` CLI command for representative validation queries across configured docsets.
- Added source URL storage for package docs and `sourceUrl` fields in query results when available.
- Added exact-title/heading/slug search boost metadata via `exactScore`.

### Improved

- Improved package docset effectiveness for mirrored/staged documentation sources.
- Expanded package docset tests to cover staged HTML/XML builds, source URL propagation, and validation command execution.
