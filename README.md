# pi-unity-docs

Pi package for fast, token-efficient retrieval from local Unity offline documentation.

The package builds a local SQLite FTS5 database from an installed Unity documentation folder such as:

```text
C:\Program Files\Unity\Hub\Editor\6000.4.7f1\Editor\Data\Documentation\en
```

It does not copy raw HTML and does not generate Markdown/JSONL. The Unity install remains the source of truth; the generated database is a rebuildable cache.

The package can also build separate package/plugin documentation docsets from Unity package `Documentation~` folders. Package docsets are managed separately from the core Unity docs cache and can be searched alongside it.

## Install in pi

From this checkout:

```bash
pi install <path-to-pi-unity-docs>
```

Or run pi temporarily with:

```bash
pi -e <path-to-pi-unity-docs>
```

## Configure and build

Interactive configuration from pi:

```text
/unity-docs-configure
```

The command asks for:

- Unity documentation source directory
- database install directory
- Unity version label
- whether to build immediately

Non-interactive CLI:

```bash
python scripts/unity_docs_db.py configure \
  --source "C:/Program Files/Unity/Hub/Editor/6000.4.7f1/Editor/Data/Documentation/en" \
  --db-dir "$LOCALAPPDATA/pi/unity-docs/6000.4.7f1" \
  --version 6000.4.7f1 \
  --yes

python scripts/unity_docs_db.py build \
  --source "C:/Program Files/Unity/Hub/Editor/6000.4.7f1/Editor/Data/Documentation/en" \
  --db-dir "$LOCALAPPDATA/pi/unity-docs/6000.4.7f1" \
  --version 6000.4.7f1 \
  --force \
  --progress
```

Configuration is stored at:

```text
~/.pi/unity-docs/config.json
```

The generated database is named `unity_docs.sqlite` inside the selected database directory.

## Tools exposed to pi

- `unity_docs_info` — show configuration and database status.
- `unity_docs_search` — full-text search over section-level Unity docs.
- `unity_docs_symbol` — exact/near-exact API symbol lookup.
- `unity_docs_show` — retrieve compact page sections.
- `unity_docs_build_database` — build/rebuild the core Unity docs cache when explicitly requested.
- `unity_docs_build_docset` — build/rebuild a package/plugin `Documentation~` docset when explicitly requested.

## Direct CLI usage

```bash
python scripts/unity_docs_db.py info
python scripts/unity_docs_db.py build --source "<Unity Documentation/en>" --db-dir "<db-dir>" --force --progress
python scripts/unity_docs_db.py search "Physics.Raycast layerMask trigger" --limit 8
python scripts/unity_docs_db.py symbol "UnityEngine.Physics.Raycast"
python scripts/unity_docs_db.py show "ScriptReference/Physics.Raycast" --sections Declaration,Parameters,Returns,Description --max-chars 6000
```

Build a package docset from an explicit package docs source:

```bash
python scripts/unity_docs_db.py build-docset \
  --source "<package-root-or-Documentation~>" \
  --db-dir "<docset-db-dir>" \
  --docset-id "<docset-id>" \
  --force
```

Build a package docset from a Unity project's embedded packages or package cache:

```bash
python scripts/unity_docs_db.py build-docset \
  --project "<unity-project-path>" \
  --package-name "<package-name>" \
  --db-dir "<docset-db-dir>" \
  --force
```

For project package resolution, embedded packages are checked before `Library/PackageCache`. If `Packages/packages-lock.json` contains a resolved version, that version is preferred before falling back to matching package-cache folders.

Add `--json` to query commands for machine-readable output. Build progress is emitted to stderr with `--progress`, so JSON stdout remains parseable.

## Notes

- Requires Python 3.10+ and SQLite with FTS5 enabled. No third-party Python packages are required.
- Build time depends on disk speed. ScriptReference contains tens of thousands of pages.
- The database can be deleted at any time and rebuilt from the Unity install.
