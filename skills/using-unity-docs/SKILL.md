---
name: using-unity-docs
description: Retrieve supplemental Unity addon, asset, plugin, or package documentation from configured local docsets, or consult a local Unity Manual and Scripting API cache for explicitly offline work.
---

# Using Supplemental Unity Docs

Use [Unity's official agent index](https://docs.unity.com/llms.txt) and version-matched official pages first for the Engine, Editor, Scripting API, and officially documented packages. Use installed CLI help and the exact project's advertised `unity command --project-path <exact-project-copy>` catalog for command syntax. Do not configure a core cache for routine Unity questions.

## Look up supplemental material

1. Identify the addon, asset, plugin, or package and its relevant version. For a Unity package, check the project's resolved version or embedded package metadata.
2. If the configured docset is unknown, call `unity_docs_info`. Select the matching docset explicitly with `docset` or `docsets`; do not assume a broad search covers it.
3. Use `unity_docs_symbol` for an API symbol or `unity_docs_search` for a topic. Pass the same docset to `unity_docs_show` and request only the relevant sections. For example, search for `rounded rectangle` with `docset: "shapes"`, then read the selected page with that same docset.
4. If no suitable docset is configured, use the publisher's documentation where available. Build a docset only when explicitly asked; `unity_docs_build_docset` accepts package `Documentation~`, external documentation sources, or an exact project path and package name.

## Explicit offline core lookup

For a configured local Unity Manual or Scripting API cache, pass `projectPath`, a version, or an explicit core docset. Project-aware core selection can fall back within the same major/minor line; check `requestedVersion` and `versionMatch`. For an explicitly requested core cache build, use `/unity-docs-configure` or `unity_docs_build_database`.

For structured results, output limits, partial coverage, and build uncertainty, see the [public contract reference](../../docs/2026-10-09-native-contracts.md). A missing result in a partial search is not evidence of absence.
