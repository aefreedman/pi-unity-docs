---
name: using-unity-docs
description: Retrieve local Unity Manual and Scripting API documentation through the pi-unity-docs SQLite cache. Use when answering Unity API, Editor workflow, or Unity documentation questions where fast local docs lookup is useful.
---

# Using Unity Docs

Use this skill when the user asks about Unity APIs, Unity Manual behavior, Editor workflows, package features documented in the installed Unity docs, or version-specific Unity documentation.

## Preferred retrieval order

1. Use `unity_docs_symbol` first for API-like queries such as `Physics.Raycast`, `GameObject.AddComponent`, `UnityEngine.Rigidbody`, or `SerializeField`.
2. Use `unity_docs_search` for conceptual or workflow queries.
3. Use `unity_docs_show` only after finding the relevant page or section. Request the smallest useful section set.
4. If `unity_docs_info` reports no configured database, ask the user whether to configure/build one, or use `/unity-docs-configure` when interactive.

## Token efficiency rules

- Do not read raw Unity HTML unless the SQLite cache is missing or clearly wrong.
- Prefer section reads over full page reads.
- Keep `maxChars` low at first, then request more only if needed.
- For ScriptReference pages, usually request only `Declaration`, `Parameters`, `Returns`, `Description`, and `Examples` as needed.
- For Manual pages, request the heading sections directly related to the question.

## Typical tool flow

API question:

```text
unity_docs_symbol("UnityEngine.Physics.Raycast")
unity_docs_show("ScriptReference/Physics.Raycast", sections=["Declaration", "Parameters", "Returns", "Description"])
```

Concept question:

```text
unity_docs_search("URP render feature renderer feature", corpus="Manual")
unity_docs_show("Manual/<selected-page>", sections=["Overview", "Create a renderer feature"])
```

Status/configuration:

```text
unity_docs_info()
```

## Building the cache

Only build or rebuild when the user asks, or when `unity_docs_info` shows the cache is missing and the user approves.

Use the interactive pi command when available:

```text
/unity-docs-configure
```

Or use the tool/CLI with an explicit Unity documentation source and database install directory.
