# Changelog

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
