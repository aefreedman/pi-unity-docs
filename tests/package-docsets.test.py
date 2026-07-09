import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "unity_docs_db.py"
INDEX_TS = ROOT / "index.ts"


def run(args, home):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    result = subprocess.run([sys.executable, str(SCRIPT), "--json", *args], text=True, capture_output=True, env=env)
    if result.returncode != 0:
        raise AssertionError(f"command failed: {args}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return json.loads(result.stdout)


def write_unity_source(root: Path, version: str, marker: str) -> Path:
    source = root / version / "Editor" / "Data" / "Documentation" / "en"
    for corpus in ["Manual", "ScriptReference"]:
        corpus_root = source / corpus
        corpus_root.mkdir(parents=True)
        (corpus_root / "Example.html").write_text(f"""
<html><body><div class=\"section\">
<h1>{corpus} Example</h1>
<h2>Overview</h2>
<p>{marker} documentation for Unity {version}.</p>
</div></body></html>
""", encoding="utf-8")
    return source


def write_unity_project(root: Path, version: str) -> Path:
    project = root / f"Project-{version}"
    (project / "ProjectSettings").mkdir(parents=True)
    (project / "Assets").mkdir()
    (project / "ProjectSettings" / "ProjectVersion.txt").write_text(f"m_EditorVersion: {version}\n", encoding="utf-8")
    return project


def write_package(root: Path, name="com.example.input", version="1.2.3", title="Example Input") -> Path:
    docs = root / "Documentation~"
    docs.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({
        "name": name,
        "displayName": title,
        "version": version,
        "unity": "6000.0",
    }), encoding="utf-8")
    (docs / "Actions.md").write_text("""---
uid: example-actions
---
# Input Actions

PlayerInput can use input actions and bindings.

## Rebinding

Interactive rebinding lets players change controls.
""", encoding="utf-8")
    (docs / "Gamepad.md").write_text("""# Gamepad Support

Gamepad controls include sticks, buttons, and triggers.
""", encoding="utf-8")
    return docs


def main():
    index_text = INDEX_TS.read_text(encoding="utf-8")
    assert "formatConfiguredDocsetHint" in index_text
    assert "Configured docsets you can target explicitly" in index_text
    assert "buildInfoArgs" in index_text

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        home.mkdir()

        old_source = write_unity_source(tmp_path / "unity-sources", "2022.3.18f1", "oldmarker")
        line_source = write_unity_source(tmp_path / "unity-sources", "2022.3.x", "linemarker")
        nearest_source = write_unity_source(tmp_path / "unity-sources", "2021.3.16f1", "nearestmarker")
        new_source = write_unity_source(tmp_path / "unity-sources", "6000.5.2f1", "newmarker")
        old_project = write_unity_project(tmp_path, "2022.3.18f1")
        line_project = write_unity_project(tmp_path, "2022.3.19f1")
        nearest_project = write_unity_project(tmp_path, "2021.3.18f1")
        run(["build", "--source", str(old_source), "--db-dir", str(tmp_path / "unity-old-db"), "--version", "2022.3.18f1", "--force"], home)
        run(["build", "--source", str(line_source), "--db-dir", str(tmp_path / "unity-line-db"), "--version", "2022.3.x", "--force"], home)
        run(["build", "--source", str(nearest_source), "--db-dir", str(tmp_path / "unity-nearest-db"), "--version", "2021.3.16f1", "--force"], home)
        run(["build", "--source", str(new_source), "--db-dir", str(tmp_path / "unity-new-db"), "--version", "6000.5.2f1", "--force"], home)

        multi_info = run(["info", "--project", str(old_project)], home)
        assert multi_info["projectVersion"] == "2022.3.18f1"
        assert "unity-2022.3.18f1" in multi_info["docsets"]
        assert "unity-2022.3.x" in multi_info["docsets"]
        assert "unity-6000.5.2f1" in multi_info["docsets"]
        project_search = run(["search", "oldmarker", "--project", str(old_project), "--limit", "5"], home)
        assert project_search and project_search[0]["docsetId"] == "unity-2022.3.18f1"
        assert project_search[0]["versionMatch"] == "exact"
        line_search = run(["search", "linemarker", "--project", str(line_project), "--limit", "5"], home)
        assert line_search and line_search[0]["docsetId"] == "unity-2022.3.x"
        assert line_search[0]["requestedVersion"] == "2022.3.19f1"
        assert line_search[0]["versionMatch"] == "minor-line"
        nearest_search = run(["search", "nearestmarker", "--project", str(nearest_project), "--limit", "5"], home)
        assert nearest_search and nearest_search[0]["docsetId"] == "unity-2021.3.16f1"
        assert nearest_search[0]["versionMatch"] == "nearest-patch"
        explicit_old = run(["search", "oldmarker", "--docset", "unity-2022.3.18f1", "--limit", "5"], home)
        assert explicit_old and explicit_old[0]["docsetId"] == "unity-2022.3.18f1"
        default_new = run(["search", "newmarker", "--limit", "5"], home)
        assert default_new and default_new[0]["docsetId"] == "unity-6000.5.2f1"

        package_root = tmp_path / "pkg"
        docs = write_package(package_root)
        db_dir = tmp_path / "db-explicit"
        built = run(["build-docset", "--source", str(docs), "--db-dir", str(db_dir), "--docset-id", "example-input", "--force"], home)
        assert built["docsetId"] == "example-input"
        assert built["packageName"] == "com.example.input"
        assert built["sourceKind"] == "explicit"

        search = run(["search", "PlayerInput", "--db-dir", str(db_dir), "--limit", "5"], home)
        assert search, "expected search results"
        assert search[0]["docsetId"] == "example-input"
        assert search[0]["docsetKind"] == "package"
        assert "exactScore" in search[0]

        shown = run(["show", "Package/Actions", "--db-dir", str(db_dir)], home)
        assert shown["page"]["title"] == "Input Actions"
        assert shown["page"]["docsetId"] == "example-input"

        profile_search = run(["search", "rebinding", "--docset", "example-input", "--limit", "5"], home)
        assert profile_search and profile_search[0]["docsetId"] == "example-input"

        project = tmp_path / "UnityProject"
        cached_root = project / "Library" / "PackageCache" / "com.example.cache@2.0.0"
        write_package(cached_root, name="com.example.cache", version="2.0.0", title="Cached Package")
        lock_dir = project / "Packages"
        lock_dir.mkdir(parents=True)
        (lock_dir / "packages-lock.json").write_text(json.dumps({
            "dependencies": {
                "com.example.cache": {"version": "2.0.0"}
            }
        }), encoding="utf-8")
        cache_db_dir = tmp_path / "db-cache"
        cache_built = run(["build-docset", "--project", str(project), "--package-name", "com.example.cache", "--db-dir", str(cache_db_dir), "--force"], home)
        assert cache_built["sourceKind"] == "package-cache"
        assert cache_built["packageVersion"] == "2.0.0"

        info = run(["info"], home)
        assert info["docsets"]["com.example.cache"]["sourceExists"] is True
        assert info["docsets"]["com.example.cache"]["dbExists"] is True

        html_source = tmp_path / "docs.html"
        html_source.write_text("<h1>HTML Docs</h1><h2>Tweening</h2><p>DOMove and SetEase examples.</p>", encoding="utf-8")
        xml_source = tmp_path / "Api.xml"
        xml_source.write_text("""<?xml version=\"1.0\"?>
<doc><members>
  <member name=\"T:Example.TweenApi\"><summary>Example tween API.</summary></member>
  <member name=\"M:Example.TweenApi.DOMove(System.Single)\"><summary>Moves a target.</summary><param name=\"duration\">Duration in seconds.</param></member>
</members></doc>
""", encoding="utf-8")
        staged_db_dir = tmp_path / "db-staged"
        staged = run([
            "build-docset",
            "--docset-id", "staged-docs",
            "--package-name", "com.example.staged",
            "--html-url", html_source.as_uri(),
            "--html-split-level", "2",
            "--xml-doc", str(xml_source),
            "--db-dir", str(staged_db_dir),
            "--force",
        ], home)
        assert staged["docsetId"] == "staged-docs"
        staged_search = run(["search", "DOMove SetEase", "--db-dir", str(staged_db_dir), "--limit", "5"], home)
        assert staged_search and staged_search[0].get("sourceUrl") == html_source.as_uri()
        staged_symbol = run(["symbol", "DOMove", "--db-dir", str(staged_db_dir), "--limit", "5"], home)
        assert staged_symbol and "DOMove" in staged_symbol[0]["fullName"]
        assert staged_symbol[0].get("sourceUrl") == xml_source.resolve().as_uri()

        validation = run(["validate", "--docset", "example-input", "--limit", "5"], home)
        assert validation["total"] == 0 or validation["failed"] == 0

    print("PASS: package docsets tests succeeded")


if __name__ == "__main__":
    main()
