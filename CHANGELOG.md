# Changelog

## 0.2.0 - 2026-06-26

### Added

- Added package docset builds from GitBook `llms.txt`, public HTML pages, and C# XML documentation files.
- Added `unity_docs_validate` and `validate` CLI command for representative validation queries across configured docsets.
- Added source URL storage for package docs and `sourceUrl` fields in query results when available.
- Added exact-title/heading/slug search boost metadata via `exactScore`.

### Improved

- Improved package docset effectiveness for mirrored/staged documentation sources.
- Expanded package docset tests to cover staged HTML/XML builds, source URL propagation, and validation command execution.
