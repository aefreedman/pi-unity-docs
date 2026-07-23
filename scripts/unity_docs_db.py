#!/usr/bin/env python3
"""Build and query an agent-native SQLite cache for Unity offline docs.

This script intentionally uses only Python stdlib modules so the pi package can
run without Python dependency installation.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

# Pi's TypeScript extension reads this script's stdout/stderr as UTF-8. On
# Windows, redirected Python stdio can default to a legacy codepage such as
# cp1252, which turns curly quotes and ellipses into invalid UTF-8 bytes for
# Node and shows up in the TUI as replacement characters (�).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

DB_FILENAME = "unity_docs.sqlite"
CONFIG_PATH = Path.home() / ".pi" / "unity-docs" / "config.json"
SKIP_CLASSES = {
    "breadcrumbs",
    "nextprev",
    "scrolltofeedback",
    "suggest",
    "suggest-wrap",
    "suggest-form",
    "suggest-success",
    "suggest-failed",
    "loading",
    "lang-switcher",
    "version-number",
    "otherversionscontent",
    "search-form",
    "apisearch",
}
BLOCK_TAGS = {"p", "div", "li", "tr", "table", "ul", "ol", "pre", "blockquote", "figure", "figcaption"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


@dataclass
class ParsedSection:
    heading_path: str
    text: str


@dataclass
class SectionCandidate:
    has_h1: bool = False
    title: str = ""
    sections: list[ParsedSection] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(len(section.text) for section in self.sections)


class GenericHtmlMarkdownParser(HTMLParser):
    """Best-effort stdlib HTML-to-Markdown converter for package doc staging."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0
        self.pre_depth = 0
        self.code_depth = 0

    def append(self, value: str) -> None:
        if not self.skip_depth:
            self.out.append(value)

    def blank(self) -> None:
        self.append("\n\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg", "noscript", "iframe", "canvas"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "table", "blockquote"}:
            self.blank()
        elif tag == "br":
            self.append("\n")
        elif tag == "li":
            self.append("\n- ")
        elif tag in {"ul", "ol"}:
            self.blank()
        elif tag == "tr":
            self.append("\n")
        elif tag in {"td", "th"}:
            self.append(" | ")
        elif tag == "pre":
            self.pre_depth += 1
            self.append("\n\n```\n")
        elif tag == "code" and not self.pre_depth:
            self.code_depth += 1
            self.append("`")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg", "noscript", "iframe", "canvas"}:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.blank()
        elif tag in {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "blockquote"}:
            self.blank()
        elif tag == "li":
            self.append("\n")
        elif tag == "pre":
            if self.pre_depth:
                self.pre_depth -= 1
            self.append("\n```\n\n")
        elif tag == "code" and self.code_depth and not self.pre_depth:
            self.code_depth -= 1
            self.append("`")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.pre_depth:
            self.append(data)
            return
        text = re.sub(r"\s+", " ", html.unescape(data))
        if text.strip():
            self.append(text)
        elif self.out and not self.out[-1].endswith((" ", "\n")):
            self.append(" ")

    def markdown(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"\n\s+-\s+", "\n- ", text)
        text = text.strip()
        if not re.search(r"(?m)^#\s+", text):
            text = "# Documentation\n\n" + text
        return text + "\n"


class UnitySectionParser(HTMLParser):
    """Extract candidate div.section blocks and split them by headings."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[SectionCandidate] = []
        self._active: SectionCandidate | None = None
        self._section_depth = 0
        self._skip_depth = 0
        self._tag_stack: list[str] = []
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._heading_levels: dict[int, str] = {}
        self._current_heading = "Overview"
        self._current_parts: list[str] = []
        self._last_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        classes = set(attrs_dict.get("class", "").lower().split())

        if tag == "div" and "section" in classes and self._active is None:
            self._active = SectionCandidate()
            self._section_depth = 1
            self._skip_depth = 0
            self._tag_stack = [tag]
            self._heading_tag = None
            self._heading_parts = []
            self._heading_levels = {}
            self._current_heading = "Overview"
            self._current_parts = []
            self._last_text = ""
            return

        if self._active is None:
            return

        self._section_depth += 1
        self._tag_stack.append(tag)

        if self._skip_depth:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return

        if tag in {"script", "style", "nav", "form", "select", "button", "textarea"}:
            self._skip_depth = 1
            return

        if classes & SKIP_CLASSES:
            self._skip_depth = 1
            return

        if tag in HEADING_TAGS:
            self._flush_text()
            self._heading_tag = tag
            self._heading_parts = []
            return

        if tag == "br":
            self._append_text("\n")
        elif tag == "li":
            self._append_text("\n- ")
        elif tag == "tr":
            self._append_text("\n")
        elif tag in {"td", "th"}:
            if self._current_parts and not self._current_parts[-1].endswith(("\n", " | ")):
                self._append_text(" | ")
        elif tag == "code":
            self._append_text("`")

    def handle_endtag(self, tag: str) -> None:
        if self._active is None:
            return

        if self._skip_depth:
            self._skip_depth -= 1
        elif tag == self._heading_tag:
            self._finish_heading()
        elif tag == "code":
            self._append_text("`")
        elif tag in BLOCK_TAGS:
            self._append_text("\n")

        self._section_depth -= 1
        if self._tag_stack:
            self._tag_stack.pop()

        if self._section_depth <= 0:
            self._flush_text()
            self._normalize_candidate(self._active)
            self.candidates.append(self._active)
            self._active = None
            self._section_depth = 0

    def handle_data(self, data: str) -> None:
        if self._active is None or self._skip_depth:
            return
        if self._heading_tag:
            self._heading_parts.append(data)
            return
        self._append_text(data)

    def _append_text(self, value: str) -> None:
        if not value:
            return
        value = html.unescape(value)
        if not value:
            return
        self._current_parts.append(value)

    def _finish_heading(self) -> None:
        if self._active is None or not self._heading_tag:
            return
        raw = "".join(self._heading_parts)
        text = normalize_space(raw)
        level = int(self._heading_tag[1])
        self._heading_tag = None
        self._heading_parts = []
        if not text:
            return
        if level == 1:
            self._active.has_h1 = True
            self._active.title = text
            return
        for existing in list(self._heading_levels):
            if existing >= level:
                del self._heading_levels[existing]
        self._heading_levels[level] = text
        self._current_heading = " > ".join(self._heading_levels[k] for k in sorted(self._heading_levels)) or text

    def _flush_text(self) -> None:
        if self._active is None:
            return
        text = normalize_block("".join(self._current_parts))
        self._current_parts = []
        if not text:
            return
        if self._active.sections and self._active.sections[-1].heading_path == self._current_heading:
            prior = self._active.sections[-1].text
            self._active.sections[-1].text = normalize_block(prior + "\n" + text)
        else:
            self._active.sections.append(ParsedSection(self._current_heading, text))

    @staticmethod
    def _normalize_candidate(candidate: SectionCandidate) -> None:
        merged: list[ParsedSection] = []
        for section in candidate.sections:
            text = normalize_block(section.text)
            if not text:
                continue
            # Drop common Unity page furniture that can leak from content sections.
            if text.lower() in {"leave feedback", "suggest a change", "success!"}:
                continue
            if merged and merged[-1].heading_path == section.heading_path:
                merged[-1].text = normalize_block(merged[-1].text + "\n" + text)
            else:
                merged.append(ParsedSection(section.heading_path, text))
        candidate.sections = merged


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def normalize_block(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = value.strip(" \n\t")
    # Remove spaces introduced inside code fences/backtick pairs by HTML splitting.
    value = value.replace("` ", "`").replace(" `", "`")
    return value


def strip_yaml_quote(value: str) -> str:
    value = value.strip()
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        value = value[1:-1]
    return value.replace("''", "'").replace('\\"', '"')


def parse_xrefmap(path: Path) -> dict[str, list[dict[str, str]]]:
    by_href: dict[str, list[dict[str, str]]] = {}
    if not path.exists():
        return by_href
    current: dict[str, str] | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- uid:"):
            if current and current.get("href"):
                by_href.setdefault(current["href"], []).append(current)
            current = {"uid": strip_yaml_quote(line.split(":", 1)[1])}
            continue
        if current is None:
            continue
        if line.startswith("name:"):
            current["name"] = strip_yaml_quote(line.split(":", 1)[1])
        elif line.startswith("href:"):
            current["href"] = strip_yaml_quote(line.split(":", 1)[1])
    if current and current.get("href"):
        by_href.setdefault(current["href"], []).append(current)
    return by_href


def flatten_toc(node: dict[str, Any], trail: list[str] | None = None, out: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    if out is None:
        out = {}
    if trail is None:
        trail = []
    title = normalize_space(str(node.get("title") or ""))
    link = str(node.get("link") or "")
    next_trail = trail + ([title] if title and title.lower() not in {"root", "toc"} else [])
    if link and link != "null":
        out[link] = next_trail
    for child in node.get("children") or []:
        if isinstance(child, dict):
            flatten_toc(child, next_trail, out)
    return out


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def build_docdata_summary(index: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    titles: dict[str, str] = {}
    summaries: dict[str, str] = {}
    pages = index.get("pages") or []
    infos = index.get("info") or []
    for i, entry in enumerate(pages):
        if isinstance(entry, list) and len(entry) >= 2:
            slug, title = str(entry[0]), normalize_space(str(entry[1]))
            titles[slug] = title
    for info in infos:
        if not (isinstance(info, list) and len(info) >= 2):
            continue
        text = normalize_space(str(info[0]))
        try:
            page_index = int(info[1])
        except Exception:
            continue
        if page_index < 0 or page_index >= len(pages):
            continue
        page_entry = pages[page_index]
        if not (isinstance(page_entry, list) and page_entry):
            continue
        slug = str(page_entry[0])
        if not text or summaries.get(slug):
            continue
        if text == titles.get(slug):
            continue
        summaries[slug] = text
    return titles, summaries


def infer_version_from_source(source: Path) -> str:
    parts = list(source.resolve().parts)
    version_pattern = re.compile(r"^\d+\.\d+(?:\.(?:\d+|x).*)?$", flags=re.IGNORECASE)
    for part in reversed(parts):
        if version_pattern.match(part):
            return part
    return "unknown"


def default_db_dir(version: str) -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "pi" / "unity-docs" / version
    return Path.home() / ".pi" / "unity-docs" / version


def parse_unity_project_version_text(contents: str) -> str | None:
    match = re.search(r"^m_EditorVersion:\s*(\S+)\s*$", contents, flags=re.MULTILINE)
    return match.group(1) if match else None


def find_unity_project_root(start: Path) -> Path | None:
    current = start.expanduser().resolve()
    if current.is_file():
        current = current.parent
    while True:
        version_file = current / "ProjectSettings" / "ProjectVersion.txt"
        if version_file.exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def read_project_unity_version(project_path: str | None) -> str | None:
    if not project_path:
        return None
    project_root = find_unity_project_root(Path(project_path))
    if not project_root:
        raise SystemExit(f"Could not find a Unity project at or above: {project_path}")
    version_file = project_root / "ProjectSettings" / "ProjectVersion.txt"
    version = parse_unity_project_version_text(version_file.read_text(encoding="utf-8", errors="replace"))
    if not version:
        raise SystemExit(f"Could not parse Unity version from: {version_file}")
    return version


@dataclass(frozen=True)
class UnityVersionParts:
    raw: str
    major: int
    minor: int
    patch: int | None
    suffix: str = ""
    wildcard: bool = False


def parse_unity_version(value: str | None) -> UnityVersionParts | None:
    if not value:
        return None
    match = re.match(r"^(\d+)\.(\d+)(?:\.(x|\d+)(.*)?)?$", value.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    patch_token = match.group(3)
    wildcard = patch_token is None or patch_token.lower() == "x"
    patch = int(patch_token) if patch_token and patch_token.isdigit() else None
    return UnityVersionParts(
        raw=value.strip(),
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=patch,
        suffix=match.group(4) or "",
        wildcard=wildcard,
    )


def configured_or_project_version(args: argparse.Namespace, config: dict[str, Any] | None = None) -> str | None:
    if getattr(args, "version", None):
        return args.version
    project_version = read_project_unity_version(getattr(args, "project", None))
    if project_version:
        return project_version
    config = config if config is not None else load_config()
    return config.get("activeVersion")


def resolve_configured_unity_version(requested: str | None, databases: dict[str, Any]) -> tuple[str | None, str | None]:
    if not requested:
        return None, None
    if requested in databases:
        return requested, "exact"

    requested_parts = parse_unity_version(requested)
    if not requested_parts:
        return None, None

    same_line: list[tuple[str, UnityVersionParts]] = []
    for version in databases:
        parsed = parse_unity_version(str(version))
        if parsed and parsed.major == requested_parts.major and parsed.minor == requested_parts.minor:
            same_line.append((str(version), parsed))

    wildcard_matches = [(version, parsed) for version, parsed in same_line if parsed.wildcard]
    if wildcard_matches:
        # Prefer explicit x-style line docsets (6000.4.x) over bare minor labels (6000.4).
        wildcard_matches.sort(key=lambda item: (".x" in item[0].lower(), len(item[0])), reverse=True)
        return wildcard_matches[0][0], "minor-line"

    patch_matches = [(version, parsed) for version, parsed in same_line if parsed.patch is not None]
    if patch_matches:
        requested_patch = requested_parts.patch
        if requested_patch is None:
            patch_matches.sort(key=lambda item: item[1].patch or -1, reverse=True)
            return patch_matches[0][0], "nearest-patch"
        lower_or_equal = [item for item in patch_matches if (item[1].patch or -1) <= requested_patch]
        if lower_or_equal:
            lower_or_equal.sort(key=lambda item: item[1].patch or -1, reverse=True)
            return lower_or_equal[0][0], "nearest-patch"
        patch_matches.sort(key=lambda item: item[1].patch or 10**9)
        return patch_matches[0][0], "nearest-patch"

    return None, None


def resolve_configured_or_project_version(args: argparse.Namespace, config: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    requested = configured_or_project_version(args, config)
    resolved, match_kind = resolve_configured_unity_version(requested, config.get("databases") or {})
    return requested, resolved, match_kind


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def db_path_from_dir(db_dir: Path) -> Path:
    return db_dir / DB_FILENAME if db_dir.suffix.lower() != ".sqlite" else db_dir


def resolve_db_path(args: argparse.Namespace) -> Path:
    if getattr(args, "db", None):
        return Path(args.db).expanduser()
    if getattr(args, "db_dir", None):
        return db_path_from_dir(Path(args.db_dir).expanduser())
    config = load_config()
    requested_version, version, _match_kind = resolve_configured_or_project_version(args, config)
    databases = config.get("databases") or {}
    if version and version in databases and databases[version].get("dbPath"):
        return Path(databases[version]["dbPath"]).expanduser()
    if not getattr(args, "project", None) and config.get("dbPath"):
        return Path(config["dbPath"]).expanduser()
    if requested_version:
        raise SystemExit(f"No Unity docs database is configured for Unity {requested_version}. Build that version, add a same-minor fallback (for example {requested_version.rsplit('.', 1)[0]}.x), or pass --db/--db-dir.")
    raise SystemExit("No database configured. Run configure or pass --db/--db-dir.")


def resolve_source_path(args: argparse.Namespace) -> Path:
    if getattr(args, "source", None):
        return Path(args.source).expanduser()
    config = load_config()
    version = configured_or_project_version(args, config)
    databases = config.get("databases") or {}
    if version and version in databases and databases[version].get("sourcePath"):
        return Path(databases[version]["sourcePath"]).expanduser()
    if version:
        for candidate in discover_sources():
            if infer_version_from_source(candidate) == version:
                return candidate
    if not getattr(args, "project", None) and config.get("sourcePath"):
        return Path(config["sourcePath"]).expanduser()
    raise SystemExit("No Unity documentation source configured. Run configure or pass --source.")


def validate_source(source: Path) -> None:
    if not source.exists():
        raise SystemExit(f"Unity documentation source does not exist: {source}")
    missing = [name for name in ["Manual", "ScriptReference"] if not (source / name).is_dir()]
    if missing:
        raise SystemExit(f"Unity documentation source is missing {', '.join(missing)}: {source}")


def normalize_package_docs_source(source: Path) -> Path:
    source = source.expanduser()
    if source.name == "Documentation~":
        return source
    docs = source / "Documentation~"
    if docs.is_dir():
        return docs
    return source


def validate_package_source(source: Path) -> None:
    if not source.exists():
        raise SystemExit(f"Package documentation source does not exist: {source}")
    if not source.is_dir():
        raise SystemExit(f"Package documentation source is not a directory: {source}")
    if not any(source.rglob("*.md")):
        raise SystemExit(f"Package documentation source contains no Markdown files: {source}")


def fetch_text_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pi-unity-docs)"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")


def safe_slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.")
    return value[:180] or "page"


def markdown_source_url(raw_lines: list[str], frontmatter: dict[str, str]) -> str | None:
    for key in ["sourceUrl", "source_url", "source", "url"]:
        if frontmatter.get(key):
            return frontmatter[key]
    for line in raw_lines[:8]:
        match = re.search(r"<!--\s*Source:\s*(.*?)\s*-->", line, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(r"available as \[Markdown\]\((https?://[^)]+)\)", line, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def html_to_markdown(source_url: str, raw_html: str, title: str | None = None) -> str:
    parser = GenericHtmlMarkdownParser()
    parser.feed(raw_html)
    body = parser.markdown()
    if title and not body.startswith("# "):
        body = f"# {title}\n\n{body}"
    return f"<!-- Source: {source_url} -->\n\n{body}"


def split_markdown_by_heading(markdown: str, split_level: int) -> list[tuple[str, str]]:
    lines = markdown.splitlines()
    source_comment = ""
    if lines and lines[0].startswith("<!-- Source:"):
        source_comment = lines[0] + "\n\n"
        lines = lines[1:]
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and len(match.group(1)) == split_level:
            heading = clean_markdown_text(match.group(2)) or f"Section {len(starts) + 1}"
            starts.append((index, heading))
    if len(starts) <= 1:
        title = starts[0][1] if starts else "Documentation"
        return [(safe_slug(title).lower(), source_comment + "\n".join(lines).strip() + "\n")]
    chunks: list[tuple[str, str]] = []
    for i, (start, heading) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        chunk_lines = list(lines[start:end])
        chunk_lines[0] = "# " + heading
        chunks.append((f"{i + 1:02d}-{safe_slug(heading).lower()}", source_comment + "\n".join(chunk_lines).strip() + "\n"))
    return chunks


def stage_llms_manifest(llms_url: str, section: str | None, docs_dir: Path, limit: int | None = None) -> int:
    llms = fetch_text_url(llms_url)
    content = llms
    if section:
        pattern = rf"(?ms)^##\s+{re.escape(section)}\s*(.*?)(?=^##\s+|\Z)"
        match = re.search(pattern, llms)
        if not match:
            raise SystemExit(f"Could not find llms.txt section: {section}")
        content = match.group(1)
    urls: list[str] = []
    seen: set[str] = set()
    link_targets = re.findall(r"\]\(([^)\s]+)\)", content)
    link_targets.extend(re.findall(r"https?://[^)\s]+", content))
    for target in link_targets:
        url = urllib.parse.urljoin(llms_url, target.strip("<>"))
        if not urllib.parse.urlparse(url).path.lower().endswith(".md"):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    if limit is not None:
        urls = urls[:limit]
    base_path = urllib.parse.urlparse(llms_url).path.rsplit("/", 1)[0].strip("/")
    for index, url in enumerate(urls, 1):
        parsed = urllib.parse.urlparse(url)
        rel = parsed.path.strip("/")
        if base_path and rel.startswith(base_path + "/"):
            rel = rel[len(base_path) + 1:]
        target = docs_dir / f"{index:03d}-{safe_slug(urllib.parse.unquote(rel))}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        text = fetch_text_url(url)
        text = re.split(r"(?m)^---\s*\n\s*# Agent Instructions\s*$", text, maxsplit=1)[0].rstrip() + "\n"
        raw_lines = text.splitlines()
        frontmatter, _body_lines = parse_frontmatter(raw_lines)
        if markdown_source_url(raw_lines, frontmatter) is None:
            text = f"<!-- Source: {url} -->\n\n{text}"
        target.write_text(text, encoding="utf-8")
    return len(urls)


def stage_html_urls(urls: list[str], docs_dir: Path, split_level: int = 0, limit: int | None = None) -> int:
    count = 0
    for url in urls[:limit] if limit is not None else urls:
        raw = fetch_text_url(url)
        markdown = html_to_markdown(url, raw)
        if split_level:
            chunks = split_markdown_by_heading(markdown, split_level)
        else:
            slug = safe_slug(urllib.parse.urlparse(url).path.strip("/") or "index").lower()
            chunks = [(slug, markdown)]
        for slug, text in chunks:
            target = docs_dir / f"{slug}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            count += 1
    return count


def find_package_root(docs_source: Path) -> Path:
    if docs_source.name == "Documentation~":
        return docs_source.parent
    return docs_source


def load_package_metadata(package_root: Path) -> dict[str, Any]:
    package_json = package_root / "package.json"
    if not package_json.exists():
        return {}
    try:
        return json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def sanitize_docset_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip().lower()).strip("-.")
    return normalized or "package-docs"


def resolve_package_docs_source(source: str | None, project: str | None, package_name: str | None, package_version: str | None) -> tuple[Path, str, Path | None]:
    if source:
        docs = normalize_package_docs_source(Path(source))
        validate_package_source(docs)
        return docs, "explicit", None

    if not project or not package_name:
        raise SystemExit("Pass --source or pass both --project and --package-name.")

    project_path = Path(project).expanduser()
    if not project_path.exists():
        raise SystemExit(f"Unity project path does not exist: {project_path}")

    embedded = project_path / "Packages" / package_name / "Documentation~"
    if embedded.is_dir():
        validate_package_source(embedded)
        return embedded, "embedded-package", project_path

    package_cache = project_path / "Library" / "PackageCache"
    candidate_versions: list[str] = []
    if package_version:
        candidate_versions.append(package_version)

    lock_path = project_path / "Packages" / "packages-lock.json"
    if lock_path.exists():
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8", errors="replace"))
            version = ((lock.get("dependencies") or {}).get(package_name) or {}).get("version")
            if version and version not in candidate_versions:
                candidate_versions.append(str(version))
        except Exception:
            pass

    for version in candidate_versions:
        cached = package_cache / f"{package_name}@{version}" / "Documentation~"
        if cached.is_dir():
            validate_package_source(cached)
            return cached, "package-cache", project_path

    if package_cache.is_dir():
        matches = sorted(package_cache.glob(f"{package_name}@*/Documentation~"), key=lambda path: path.as_posix(), reverse=True)
        for match in matches:
            if match.is_dir():
                validate_package_source(match)
                return match, "package-cache", project_path

    raise SystemExit(f"Could not find Documentation~ for package '{package_name}' under project: {project_path}")


def discover_sources() -> list[Path]:
    roots: list[Path] = []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        roots.append(Path(program_files) / "Unity" / "Hub" / "Editor")
    roots.extend([
        Path("C:/Program Files/Unity/Hub/Editor"),
        Path("/Applications/Unity/Hub/Editor"),
        Path.home() / "Applications" / "Unity" / "Hub" / "Editor",
        Path.home() / "Unity" / "Hub" / "Editor",
        Path("/opt/Unity/Hub/Editor"),
        Path("/opt/unity/Hub/Editor"),
    ])
    found: list[Path] = []
    seen_roots: set[Path] = set()
    for root in roots:
        root = root.expanduser()
        if root in seen_roots or not root.exists():
            continue
        seen_roots.add(root)
        for child in root.iterdir():
            candidates = [
                child / "Editor" / "Data" / "Documentation" / "en",
                child / "Unity.app" / "Contents" / "Documentation" / "en",
                child / "Unity.app" / "Contents" / "Resources" / "Documentation" / "en",
            ]
            for candidate in candidates:
                if (candidate / "Manual").is_dir() and (candidate / "ScriptReference").is_dir():
                    found.append(candidate)
    return sorted(set(found), key=lambda p: p.as_posix(), reverse=True)


def parse_html_sections(path: Path) -> tuple[str, list[ParsedSection]]:
    parser = UnitySectionParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    candidates = [c for c in parser.candidates if c.has_h1 and c.total_chars > 0]
    if not candidates:
        candidates = [c for c in parser.candidates if c.total_chars > 0]
    if not candidates:
        return "", []
    best = max(candidates, key=lambda c: (c.has_h1, c.total_chars))
    return best.title, best.sections


def parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    if not lines or lines[0].strip() != "---":
        return {}, lines
    metadata: dict[str, str] = {}
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            for raw in lines[1:index]:
                if ":" not in raw:
                    continue
                key, value = raw.split(":", 1)
                metadata[key.strip()] = value.strip().strip('"\'')
            return metadata, lines[index + 1:]
    return {}, lines


def clean_markdown_text(value: str) -> str:
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\(([^)]*)\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\[[^\]]+\]", r"\1", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"__([^_]+)__", r"\1", value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("xref:", "")
    return normalize_space(value)


def parse_markdown_sections(path: Path) -> tuple[str, str | None, str | None, list[ParsedSection]]:
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    frontmatter, lines = parse_frontmatter(raw_lines)
    source_url = markdown_source_url(raw_lines, frontmatter)
    title = ""
    sections: list[ParsedSection] = []
    heading_stack: dict[int, str] = {}
    current_heading = "Overview"
    current_parts: list[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal current_parts
        text = "\n".join(part for part in current_parts if part).strip()
        if text:
            sections.append(ParsedSection(current_heading, text))
        current_parts = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            current_parts.append(stripped)
            continue
        if not in_fence:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
            if match:
                flush()
                level = len(match.group(1))
                heading = clean_markdown_text(match.group(2))
                if level == 1 and not title:
                    title = heading
                heading_stack[level] = heading
                for existing in list(heading_stack):
                    if existing > level:
                        del heading_stack[existing]
                current_heading = " > ".join(heading_stack[index] for index in sorted(heading_stack) if heading_stack[index]) or "Overview"
                continue
        cleaned = stripped if in_fence else clean_markdown_text(stripped)
        if cleaned:
            current_parts.append(cleaned)
    flush()

    if not title:
        title = clean_markdown_text(path.stem.replace("-", " ").replace("_", " ")) or path.stem
    return title, frontmatter.get("uid"), source_url, sections


def xml_element_to_text(element: Any) -> str:
    if element is None:
        return ""
    pieces: list[str] = []

    def walk(node: Any) -> None:
        if node.text:
            pieces.append(node.text)
        for child in node:
            tag = str(child.tag).lower()
            if tag in {"para", "br"}:
                pieces.append("\n")
            elif tag == "see":
                pieces.append(child.attrib.get("cref") or child.attrib.get("langword") or child.attrib.get("href") or "")
            elif tag == "paramref":
                pieces.append(child.attrib.get("name", ""))
            elif tag == "c":
                pieces.append("`")
                if child.text:
                    pieces.append(child.text)
                pieces.append("`")
            elif tag == "code":
                pieces.append("\n```csharp\n")
                if child.text:
                    pieces.append(child.text)
                pieces.append("\n```\n")
            else:
                walk(child)
            if child.tail:
                pieces.append(child.tail)

    walk(element)
    text = html.unescape("".join(pieces))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def xml_member_declaring_type(member_name: str) -> str:
    full = member_name[2:]
    base = full.split("(", 1)[0]
    if member_name.startswith("T:"):
        return base
    if ".#ctor" in base:
        return base.split(".#ctor", 1)[0]
    if ".#cctor" in base:
        return base.split(".#cctor", 1)[0]
    return base.rsplit(".", 1)[0] if "." in base else base


def xml_member_short_name(member_name: str) -> str:
    full = member_name[2:]
    base = full.split("(", 1)[0]
    if ".#ctor" in base:
        return base.rsplit(".", 1)[0].split(".")[-1]
    return base.rsplit(".", 1)[-1]


def stage_xml_docs(xml_sources: list[Path], docs_dir: Path) -> int:
    import xml.etree.ElementTree as ET

    api_dir = docs_dir / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    type_docs: dict[str, dict[str, Any]] = {}
    for xml_path in xml_sources:
        if not xml_path.exists():
            raise SystemExit(f"XML documentation file does not exist: {xml_path}")
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError as exc:
            raise SystemExit(f"Could not parse XML documentation file {xml_path}: {exc}") from exc
        members = root.find("members")
        if members is None:
            continue
        for member in members.findall("member"):
            name = member.attrib.get("name", "")
            if len(name) < 3 or name[1] != ":":
                continue
            declaring = xml_member_declaring_type(name)
            info = type_docs.setdefault(declaring, {"summary": "", "members": [], "sources": set()})
            info["sources"].add(str(xml_path))
            summary = xml_element_to_text(member.find("summary"))
            if name.startswith("T:"):
                info["summary"] = summary
                continue
            info["members"].append({
                "kind": name[0],
                "name": name,
                "short": xml_member_short_name(name),
                "summary": summary,
                "remarks": xml_element_to_text(member.find("remarks")),
                "returns": xml_element_to_text(member.find("returns")),
                "params": [(param.attrib.get("name", ""), xml_element_to_text(param)) for param in member.findall("param")],
                "source": str(xml_path),
            })
    count = 0
    for declaring, info in sorted(type_docs.items()):
        members = info.get("members") or []
        if not info.get("summary") and not members:
            continue
        sources = sorted(str(source) for source in info.get("sources", []))
        source_comment = f"<!-- Source: {Path(sources[0]).resolve().as_uri()} -->\n\n" if sources else ""
        lines = [f"# {declaring}", "", "Source: local XML documentation.", ""]
        if info.get("summary"):
            lines += [str(info["summary"]), ""]
        for member in sorted(members, key=lambda item: (item["short"], item["name"])):
            kind_label = {"M": "Method", "P": "Property", "F": "Field", "E": "Event"}.get(member["kind"], member["kind"])
            lines += [f"## {kind_label} {member['short']}", "", f"`{member['name']}`", ""]
            if member.get("summary"):
                lines += [member["summary"], ""]
            if member.get("params"):
                lines.append("Parameters:")
                for param_name, param_text in member["params"]:
                    lines.append(f"- `{param_name}`: {param_text}")
                lines.append("")
            if member.get("returns"):
                lines += ["Returns:", member["returns"], ""]
            if member.get("remarks"):
                lines += ["Remarks:", member["remarks"], ""]
        target = api_dir / f"{safe_slug(declaring).lower()}.md"
        target.write_text(source_comment + "\n".join(lines).strip() + "\n", encoding="utf-8")
        count += 1
    return count


def infer_kind(corpus: str, slug: str, title: str, sections: list[ParsedSection]) -> str:
    if corpus == "Manual":
        return "manual_page"
    lower = "\n".join(section.text[:300] for section in sections[:2]).lower()
    if " enum in " in lower or "enumeration" in lower:
        return "enum"
    if " struct in " in lower or "structure" in lower:
        return "struct"
    if " class in " in lower or "inherits from:" in lower:
        return "class"
    if slug.endswith("-ctor") or title.endswith("()"):
        return "constructor"
    if "." in title or "." in slug:
        if "Declaration" in {s.heading_path for s in sections}:
            return "member"
        return "api_member"
    return "api_page"


def extract_signature(sections: list[ParsedSection]) -> str:
    for section in sections:
        if "Declaration" in section.heading_path:
            lines = [normalize_space(line) for line in section.text.splitlines() if normalize_space(line)]
            for line in lines:
                if line.lower() != "declaration":
                    return line[:2000]
    return ""


def token_estimate(text: str) -> int:
    return max(1, int(len(text) / 4)) if text else 0


def format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;

        CREATE TABLE metadata(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE pages(
          id TEXT PRIMARY KEY,
          version TEXT NOT NULL,
          corpus TEXT NOT NULL,
          slug TEXT NOT NULL,
          title TEXT NOT NULL,
          kind TEXT NOT NULL,
          uid TEXT,
          source_path TEXT NOT NULL,
          source_url TEXT,
          url_path TEXT NOT NULL,
          breadcrumbs TEXT,
          summary TEXT
        );

        CREATE TABLE sections(
          id TEXT PRIMARY KEY,
          page_id TEXT NOT NULL REFERENCES pages(id),
          heading_path TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          text TEXT NOT NULL,
          token_estimate INTEGER NOT NULL
        );

        CREATE TABLE symbols(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          page_id TEXT NOT NULL REFERENCES pages(id),
          uid TEXT,
          full_name TEXT NOT NULL,
          short_name TEXT NOT NULL,
          kind TEXT,
          signature TEXT
        );

        CREATE VIRTUAL TABLE sections_fts USING fts5(
          section_id UNINDEXED,
          page_id UNINDEXED,
          title,
          heading_path,
          text,
          tokenize='unicode61'
        );

        CREATE INDEX idx_pages_slug ON pages(slug);
        CREATE INDEX idx_pages_title ON pages(title);
        CREATE INDEX idx_pages_corpus ON pages(corpus);
        CREATE INDEX idx_sections_page ON sections(page_id, ordinal);
        CREATE INDEX idx_symbols_full ON symbols(full_name);
        CREATE INDEX idx_symbols_short ON symbols(short_name);
        """
    )


def build_database(source: Path, db_path: Path, version: str, force: bool = False, limit: int | None = None, write_config: bool = True, progress: bool = False) -> dict[str, Any]:
    validate_source(source)
    if db_path.exists() and not force:
        raise SystemExit(f"Database already exists. Pass --force to replace: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="unity_docs_", suffix=".sqlite", dir=str(db_path.parent))
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    counts = {"pages": 0, "sections": 0, "symbols": 0}
    corpora = ["Manual", "ScriptReference"]
    html_files_by_corpus: dict[str, list[Path]] = {}
    remaining = limit
    for corpus in corpora:
        files = sorted((source / corpus).rglob("*.html"))
        if remaining is not None:
            files = files[:max(0, remaining)]
            remaining -= len(files)
        html_files_by_corpus[corpus] = files
    total_pages = sum(len(files) for files in html_files_by_corpus.values())
    last_progress_at = 0.0
    last_progress_pages = -1

    def emit_progress(stage: str, corpus: str | None = None, force_emit: bool = False) -> None:
        nonlocal last_progress_at, last_progress_pages
        if not progress:
            return
        now = time.time()
        if not force_emit and now - last_progress_at < 5 and counts["pages"] - last_progress_pages < 500:
            return
        last_progress_at = now
        last_progress_pages = counts["pages"]
        percent = round((counts["pages"] / total_pages) * 100, 1) if total_pages else 100.0
        message = {
            "stage": stage,
            "corpus": corpus,
            "pages": counts["pages"],
            "totalPages": total_pages,
            "percent": percent,
            "sections": counts["sections"],
            "symbols": counts["symbols"],
            "elapsed": format_elapsed(now - started),
        }
        print("PROGRESS " + json.dumps(message), file=sys.stderr, flush=True)

    try:
        emit_progress("starting", force_emit=True)
        conn = sqlite3.connect(tmp_path)
        create_schema(conn)
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("version", version))
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("sourcePath", str(source)))
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("docsetId", sanitize_docset_id(f"unity-{version}")))
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("docsetKind", "unity"))
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("docsetTitle", f"Unity {version}"))
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", ("builtAt", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

        for corpus in corpora:
            corpus_root = source / corpus
            doc_index = load_json(corpus_root / "docdata" / "index.json")
            toc = load_json(corpus_root / "docdata" / "toc.json")
            titles, summaries = build_docdata_summary(doc_index)
            breadcrumbs_by_link = flatten_toc(toc) if toc else {}
            xrefs = parse_xrefmap(corpus_root / "xrefmap.yml")
            html_files = html_files_by_corpus[corpus]
            emit_progress(f"processing {corpus}", corpus, force_emit=True)
            for html_path in html_files:
                rel = html_path.relative_to(corpus_root).as_posix()
                slug = rel[:-5]
                page_id = f"{corpus}/{slug}"
                parsed_title, sections = parse_html_sections(html_path)
                title = titles.get(slug) or (xrefs.get(rel, [{}])[0].get("name") if xrefs.get(rel) else "") or parsed_title or slug
                summary = summaries.get(slug) or (sections[0].text[:500] if sections else "")
                kind = infer_kind(corpus, slug, title, sections)
                refs = xrefs.get(rel, [])
                uid = refs[0].get("uid") if refs else None
                breadcrumbs = " > ".join(breadcrumbs_by_link.get(slug, []))
                signature = extract_signature(sections)

                conn.execute(
                    "INSERT INTO pages(id, version, corpus, slug, title, kind, uid, source_path, source_url, url_path, breadcrumbs, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (page_id, version, corpus, slug, title, kind, uid, str(html_path), None, f"{corpus}/{rel}", breadcrumbs, summary),
                )
                counts["pages"] += 1
                emit_progress(f"processing {corpus}", corpus)

                if not sections and summary:
                    sections = [ParsedSection("Overview", summary)]
                for ordinal, section in enumerate(sections):
                    sid = f"{page_id}#{ordinal:03d}"
                    conn.execute(
                        "INSERT INTO sections(id, page_id, heading_path, ordinal, text, token_estimate) VALUES (?, ?, ?, ?, ?, ?)",
                        (sid, page_id, section.heading_path, ordinal, section.text, token_estimate(section.text)),
                    )
                    conn.execute(
                        "INSERT INTO sections_fts(section_id, page_id, title, heading_path, text) VALUES (?, ?, ?, ?, ?)",
                        (sid, page_id, title, section.heading_path, section.text),
                    )
                    counts["sections"] += 1

                if corpus == "ScriptReference":
                    symbol_rows: list[tuple[str | None, str, str, str, str]] = []
                    for ref in refs:
                        full_name = ref.get("uid") or title
                        short_name = ref.get("name") or title.split(".")[-1]
                        symbol_rows.append((ref.get("uid"), full_name, short_name, kind, signature))
                    if not symbol_rows:
                        symbol_rows.append((uid, title, title.split(".")[-1], kind, signature))
                    # Useful aliases for exact lookup.
                    if title and all(row[1] != title for row in symbol_rows):
                        symbol_rows.append((uid, title, title.split(".")[-1], kind, signature))
                    if slug and all(row[1] != slug for row in symbol_rows):
                        symbol_rows.append((uid, slug, slug.split(".")[-1], kind, signature))
                    for row in symbol_rows:
                        conn.execute(
                            "INSERT INTO symbols(page_id, uid, full_name, short_name, kind, signature) VALUES (?, ?, ?, ?, ?, ?)",
                            (page_id, *row),
                        )
                        counts["symbols"] += 1
            conn.commit()

        conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", ("pageCount", str(counts["pages"])))
        conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", ("sectionCount", str(counts["sections"])))
        conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", ("symbolCount", str(counts["symbols"])))
        conn.commit()
        emit_progress("finalizing database", force_emit=True)
        conn.execute("VACUUM")
        conn.close()
        shutil.move(str(tmp_path), str(db_path))
        emit_progress("complete", force_emit=True)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    elapsed = time.time() - started
    if write_config:
        update_config(version, source, db_path)
    return {"dbPath": str(db_path), "sourcePath": str(source), "version": version, "elapsedSeconds": round(elapsed, 2), **counts}


def build_package_docset(
    source: Path,
    db_path: Path,
    docset_id: str,
    title: str | None = None,
    package_name: str | None = None,
    package_version: str | None = None,
    force: bool = False,
    limit: int | None = None,
    write_config: bool = True,
    progress: bool = False,
    source_kind: str = "explicit",
    project_path: Path | None = None,
) -> dict[str, Any]:
    source = normalize_package_docs_source(source)
    validate_package_source(source)
    if db_path.exists() and not force:
        raise SystemExit(f"Database already exists. Pass --force to replace: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    package_root = find_package_root(source)
    metadata = load_package_metadata(package_root)
    package_name = package_name or metadata.get("name") or docset_id
    package_version = package_version or metadata.get("version") or "unknown"
    title = title or metadata.get("displayName") or metadata.get("name") or docset_id
    docset_id = sanitize_docset_id(docset_id or package_name or title)

    started = time.time()
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="unity_docs_package_", suffix=".sqlite", dir=str(db_path.parent))
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    counts = {"pages": 0, "sections": 0, "symbols": 0}
    md_files = sorted(path for path in source.rglob("*.md") if path.is_file())
    if limit is not None:
        md_files = md_files[:limit]
    total_pages = len(md_files)
    last_progress_at = 0.0
    last_progress_pages = -1

    def emit_progress(stage: str, force_emit: bool = False) -> None:
        nonlocal last_progress_at, last_progress_pages
        if not progress:
            return
        now = time.time()
        if not force_emit and now - last_progress_at < 5 and counts["pages"] - last_progress_pages < 100:
            return
        last_progress_at = now
        last_progress_pages = counts["pages"]
        percent = round((counts["pages"] / total_pages) * 100, 1) if total_pages else 100.0
        message = {
            "stage": stage,
            "corpus": "Package",
            "pages": counts["pages"],
            "totalPages": total_pages,
            "percent": percent,
            "sections": counts["sections"],
            "symbols": counts["symbols"],
            "elapsed": format_elapsed(now - started),
        }
        print("PROGRESS " + json.dumps(message), file=sys.stderr, flush=True)

    try:
        emit_progress("starting", force_emit=True)
        conn = sqlite3.connect(tmp_path)
        create_schema(conn)
        metadata_rows = {
            "version": package_version,
            "sourcePath": str(source),
            "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "docsetId": docset_id,
            "docsetKind": "package",
            "docsetTitle": title,
            "packageName": package_name,
            "packageVersion": package_version,
            "sourceKind": source_kind,
        }
        if project_path:
            metadata_rows["projectPath"] = str(project_path)
        for key, value in metadata_rows.items():
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (key, str(value)))

        emit_progress("processing Package", force_emit=True)
        for md_path in md_files:
            rel = md_path.relative_to(source).as_posix()
            slug = rel.rsplit(".", 1)[0]
            page_id = f"Package/{slug}"
            page_title, uid, source_url, sections = parse_markdown_sections(md_path)
            summary = sections[0].text[:500] if sections else ""
            conn.execute(
                "INSERT INTO pages(id, version, corpus, slug, title, kind, uid, source_path, source_url, url_path, breadcrumbs, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (page_id, package_version, "Package", slug, page_title, "package_page", uid, str(md_path), source_url, f"Package/{rel}", title, summary),
            )
            counts["pages"] += 1
            emit_progress("processing Package")
            if not sections and summary:
                sections = [ParsedSection("Overview", summary)]
            for ordinal, section in enumerate(sections):
                sid = f"{page_id}#{ordinal:03d}"
                conn.execute(
                    "INSERT INTO sections(id, page_id, heading_path, ordinal, text, token_estimate) VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, page_id, section.heading_path, ordinal, section.text, token_estimate(section.text)),
                )
                conn.execute(
                    "INSERT INTO sections_fts(section_id, page_id, title, heading_path, text) VALUES (?, ?, ?, ?, ?)",
                    (sid, page_id, page_title, section.heading_path, section.text),
                )
                counts["sections"] += 1

            symbol_candidates = [candidate for candidate in [uid, page_title, slug] if candidate]
            for section in sections:
                heading = section.heading_path.split(" > ")[-1]
                if heading and heading not in symbol_candidates:
                    symbol_candidates.append(heading)
            for candidate in symbol_candidates:
                conn.execute(
                    "INSERT INTO symbols(page_id, uid, full_name, short_name, kind, signature) VALUES (?, ?, ?, ?, ?, ?)",
                    (page_id, uid, candidate, candidate.split(".")[-1], "package_page", ""),
                )
                counts["symbols"] += 1
        conn.commit()
        conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", ("pageCount", str(counts["pages"])))
        conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", ("sectionCount", str(counts["sections"])))
        conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", ("symbolCount", str(counts["symbols"])))
        conn.commit()
        emit_progress("finalizing database", force_emit=True)
        conn.execute("VACUUM")
        conn.close()
        shutil.move(str(tmp_path), str(db_path))
        emit_progress("complete", force_emit=True)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    elapsed = time.time() - started
    if write_config:
        update_docset_config(docset_id, {
            "kind": "package",
            "title": title,
            "packageName": package_name,
            "packageVersion": package_version,
            "sourcePath": str(source),
            "sourceKind": source_kind,
            "projectPath": str(project_path) if project_path else None,
            "dbPath": str(db_path),
            "priority": 50,
            "enabled": True,
        })
    return {"dbPath": str(db_path), "sourcePath": str(source), "docsetId": docset_id, "docsetKind": "package", "title": title, "packageName": package_name, "packageVersion": package_version, "sourceKind": source_kind, "elapsedSeconds": round(elapsed, 2), **counts}


def update_config(version: str, source: Path, db_path: Path) -> None:
    config = load_config()
    config.setdefault("databases", {})[version] = {
        "sourcePath": str(source),
        "dbPath": str(db_path),
    }
    config["activeVersion"] = version
    save_config(config)


def update_docset_config(docset_id: str, docset: dict[str, Any]) -> None:
    config = load_config()
    clean_docset = {key: value for key, value in docset.items() if value is not None}
    config.setdefault("docsets", {})[docset_id] = clean_docset
    profiles = config.setdefault("profiles", {})
    default_profile = profiles.setdefault("default", [])
    if docset_id not in default_profile:
        default_profile.append(docset_id)
    config.setdefault("activeProfile", "default")
    save_config(config)


def ensure_db(conn_path: Path) -> sqlite3.Connection:
    if not conn_path.exists():
        raise SystemExit(f"Unity docs database does not exist: {conn_path}")
    conn = sqlite3.connect(conn_path)
    conn.row_factory = sqlite3.Row
    return conn


def fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]+", query.lower())
    if not terms:
        return '""'
    return " ".join(f'"{term}"*' for term in terms[:12])


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    if "source_url" in data and data.get("source_url") is not None and "sourceUrl" not in data:
        data["sourceUrl"] = data.get("source_url")
    return data


def table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def page_source_url_select(conn: sqlite3.Connection, alias: str = "p") -> str:
    return f"{alias}.source_url AS sourceUrl" if table_has_column(conn, "pages", "source_url") else "NULL AS sourceUrl"


def read_db_metadata(db_path: Path) -> dict[str, str]:
    if not db_path.exists():
        return {}
    conn = ensure_db(db_path)
    try:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata")}
    finally:
        conn.close()


def annotate_docset(item: dict[str, Any], docset: dict[str, Any]) -> dict[str, Any]:
    if not docset:
        return item
    annotated = dict(item)
    for key in ["docsetId", "docsetTitle", "docsetKind", "packageName", "packageVersion", "version", "requestedVersion", "versionMatch"]:
        if docset.get(key) is not None:
            annotated[key] = docset.get(key)
    return annotated


def docset_from_db_metadata(db_path: Path, fallback_id: str | None = None) -> dict[str, Any]:
    metadata = read_db_metadata(db_path)
    docset_id = metadata.get("docsetId") or fallback_id
    title = metadata.get("docsetTitle") or metadata.get("packageName") or metadata.get("version") or docset_id
    return {
        "docsetId": docset_id,
        "docsetTitle": title,
        "docsetKind": metadata.get("docsetKind") or ("package" if metadata.get("packageName") else "unity"),
        "packageName": metadata.get("packageName"),
        "packageVersion": metadata.get("packageVersion"),
    }


def has_direct_db_selector(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "db", None) or getattr(args, "db_dir", None) or getattr(args, "version", None))


def split_docset_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def core_unity_docsets(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    docsets: dict[str, dict[str, Any]] = {}
    databases = config.get("databases") or {}
    for version, database in databases.items():
        if not isinstance(database, dict) or not database.get("dbPath"):
            continue
        docset_id = sanitize_docset_id(f"unity-{version}")
        docsets[docset_id] = {
            "kind": "unity",
            "title": f"Unity {version}",
            "version": version,
            "sourcePath": database.get("sourcePath"),
            "dbPath": database.get("dbPath"),
            "priority": 100,
            "enabled": True,
        }
    return docsets


def resolve_query_docsets(args: argparse.Namespace) -> list[tuple[str, Path, dict[str, Any]]]:
    if has_direct_db_selector(args):
        db_path = resolve_db_path(args)
        config = load_config()
        requested_version, resolved_version, match_kind = resolve_configured_or_project_version(args, config)
        if not (getattr(args, "db", None) or getattr(args, "db_dir", None)) and resolved_version:
            docset_id = sanitize_docset_id(f"unity-{resolved_version}")
            docset = {
                "docsetId": docset_id,
                "docsetTitle": f"Unity {resolved_version}",
                "docsetKind": "unity",
                "version": resolved_version,
                "requestedVersion": requested_version,
                "versionMatch": match_kind,
            }
        else:
            docset = docset_from_db_metadata(db_path)
        return [(docset.get("docsetId") or "direct", db_path, docset)]

    config = load_config()
    docsets = core_unity_docsets(config)
    docsets.update(dict(config.get("docsets") or {}))
    project_version = read_project_unity_version(getattr(args, "project", None))
    requested_core_version = project_version or config.get("activeVersion")
    selected_core_version, selected_match_kind = resolve_configured_unity_version(requested_core_version, config.get("databases") or {})
    selected_core_id = sanitize_docset_id(f"unity-{selected_core_version}") if selected_core_version else ""
    if not docsets:
        db_path = resolve_db_path(args)
        docset = docset_from_db_metadata(db_path)
        return [(docset.get("docsetId") or "legacy", db_path, docset)]

    requested = split_docset_ids(getattr(args, "docsets", None))
    if getattr(args, "docset", None):
        requested.insert(0, args.docset)
    explicit_requested = bool(requested)
    if project_version and not explicit_requested and selected_core_id not in docsets:
        raise SystemExit(f"No Unity docs database is configured for project Unity {project_version}. Build that version, add a same-minor fallback (for example {project_version.rsplit('.', 1)[0]}.x), or pass an explicit --docset/--docsets selector.")
    if not requested:
        profile_name = getattr(args, "profile", None) or config.get("activeProfile") or "default"
        profiles = config.get("profiles") or {}
        requested = list(profiles.get(profile_name) or [])
        if selected_core_id and selected_core_id not in requested:
            requested.insert(0, selected_core_id)
    if not requested:
        requested = [key for key, value in sorted(docsets.items(), key=lambda item: int((item[1] or {}).get("priority", 0)), reverse=True) if (value or {}).get("enabled", True)]

    resolved: list[tuple[str, Path, dict[str, Any]]] = []
    for docset_id in requested:
        raw = docsets.get(docset_id)
        if not raw or raw.get("enabled", True) is False:
            continue
        db_path_value = raw.get("dbPath")
        if not db_path_value:
            continue
        db_path = Path(db_path_value).expanduser()
        docset = {
            "docsetId": docset_id,
            "docsetTitle": raw.get("title") or docset_id,
            "docsetKind": raw.get("kind") or "unity",
            "packageName": raw.get("packageName"),
            "packageVersion": raw.get("packageVersion"),
            "version": raw.get("version"),
            "requestedVersion": requested_core_version if docset_id == selected_core_id else None,
            "versionMatch": selected_match_kind if docset_id == selected_core_id else None,
            "priority": raw.get("priority", 0),
            "validationQueries": raw.get("validationQueries"),
        }
        resolved.append((docset_id, db_path, docset))
    if not resolved:
        raise SystemExit("No enabled documentation docsets are configured for this query.")
    return resolved


def search_db(db_path: Path, query: str, limit: int, corpus: str | None = None, docset: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    conn = ensure_db(db_path)
    match = fts_query(query)
    corpus_sql = ""
    query_lower = normalize_space(query).lower()
    like_query = f"%{query_lower}%"
    source_url_sql = page_source_url_select(conn, "p")
    params: list[Any] = [query_lower, like_query, query_lower, like_query, like_query, match]
    if corpus:
        corpus_sql = " AND p.corpus = ?"
        params.append(corpus)
    params.append(limit)
    sql = f"""
        SELECT s.id AS sectionId, s.page_id AS pageId, p.title, p.corpus, p.kind, p.slug,
               p.breadcrumbs, {source_url_sql}, s.heading_path AS headingPath,
               snippet(sections_fts, 4, '[', ']', ' … ', 24) AS snippet,
               bm25(sections_fts) AS rank,
               CASE
                 WHEN lower(p.title) = ? THEN 100
                 WHEN lower(p.title) LIKE ? THEN 80
                 WHEN lower(s.heading_path) = ? THEN 70
                 WHEN lower(s.heading_path) LIKE ? THEN 55
                 WHEN lower(p.slug) LIKE ? THEN 40
                 ELSE 0
               END AS exactScore
        FROM sections_fts
        JOIN sections s ON s.id = sections_fts.section_id
        JOIN pages p ON p.id = s.page_id
        WHERE sections_fts MATCH ? {corpus_sql}
        ORDER BY exactScore DESC, rank
        LIMIT ?
    """
    try:
        rows = [row_dict(row) for row in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [annotate_docset(row, docset or {}) for row in rows]


def search_docsets(args: argparse.Namespace) -> list[dict[str, Any]]:
    docsets = resolve_query_docsets(args)
    merged: list[dict[str, Any]] = []
    per_docset_limit = max(args.limit, 5)
    for _docset_id, db_path, docset in docsets:
        if not db_path.exists():
            continue
        docset_kind = str(docset.get("docsetKind") or "unity")
        if args.corpus in {"Manual", "ScriptReference"} and docset_kind == "package":
            continue
        if args.corpus == "Package" and docset_kind != "package":
            continue
        for row in search_db(db_path, args.query, per_docset_limit, args.corpus, docset):
            row["docsetPriority"] = docset.get("priority", 0)
            merged.append(row)
    merged.sort(key=lambda row: (-int(row.get("exactScore") or 0), -int(row.get("docsetPriority") or 0), float(row.get("rank") or 0)))
    return merged[:args.limit]


def symbol_db(db_path: Path, name: str, limit: int, docset: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    conn = ensure_db(db_path)
    clean = normalize_space(name)
    short = clean.split(".")[-1]
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def append_symbol_rows(where_sql: str, params: tuple[Any, ...]) -> None:
        nonlocal rows
        if len(rows) >= limit:
            return
        query_limit = max(limit * 3, 12)
        for row in conn.execute(
            f"""
            SELECT sym.full_name AS fullName, sym.short_name AS shortName, sym.uid, sym.kind, sym.signature,
                   p.id AS pageId, p.title, p.corpus, p.slug, p.summary, {page_source_url_select(conn, "p")}
            FROM symbols sym
            JOIN pages p ON p.id = sym.page_id
            WHERE {where_sql}
            ORDER BY length(sym.full_name)
            LIMIT ?
            """,
            (*params, query_limit),
        ):
            item = row_dict(row)
            key = (item["pageId"], item.get("signature") or item.get("fullName") or "")
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
            if len(rows) >= limit:
                return

    def append_page_rows(title: str) -> None:
        if len(rows) >= limit:
            return
        source_url_sql = page_source_url_select(conn, "pages")
        for row in conn.execute(
            f"SELECT id AS pageId, title, corpus, slug, kind, uid, summary, {source_url_sql} FROM pages WHERE title = ? LIMIT ?",
            (title, limit),
        ):
            page = row_dict(row)
            key = (page["pageId"], page.get("uid") or page.get("title") or "")
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "fullName": page.get("uid") or page.get("title"),
                "shortName": page.get("title"),
                "uid": page.get("uid"),
                "kind": page.get("kind"),
                "signature": "",
                **page,
            })
            if len(rows) >= limit:
                return

    # Keep exact/index-friendly lookups first. A single OR with LIKE forced a
    # large scan on the 100k-row symbol table; staged queries are materially
    # faster and keep exact API pages ahead of fuzzy matches.
    append_symbol_rows("sym.full_name = ?", (clean,))
    if "." in clean:
        append_symbol_rows("sym.full_name GLOB ?", (f"{clean}(*",))
        append_symbol_rows("sym.full_name GLOB ?", (f"{clean}",))
        append_symbol_rows("sym.full_name GLOB ?", (f"*.{clean}(*",))
        append_symbol_rows("sym.full_name GLOB ?", (f"*.{clean}",))
        if rows:
            conn.close()
            return [annotate_docset(row, docset or {}) for row in rows[:limit]]
    append_page_rows(clean)
    if rows:
        conn.close()
        return [annotate_docset(row, docset or {}) for row in rows[:limit]]
    append_symbol_rows("sym.short_name = ?", (clean,))
    if short != clean:
        append_symbol_rows("sym.short_name = ?", (short,))
    if len(rows) < limit:
        append_symbol_rows("sym.full_name GLOB ?", (f"*{clean}*",))

    conn.close()
    return [annotate_docset(row, docset or {}) for row in rows[:limit]]


def symbol_docsets(args: argparse.Namespace) -> list[dict[str, Any]]:
    docsets = resolve_query_docsets(args)
    merged: list[dict[str, Any]] = []
    for _docset_id, db_path, docset in docsets:
        if not db_path.exists():
            continue
        for row in symbol_db(db_path, args.name, max(args.limit, 5), docset):
            exact = str(row.get("fullName") or "").lower() == args.name.lower() or str(row.get("shortName") or "").lower() == args.name.lower()
            row["docsetPriority"] = docset.get("priority", 0)
            row["exactMatch"] = exact
            merged.append(row)
    merged.sort(key=lambda row: (not row.get("exactMatch"), -int(row.get("docsetPriority") or 0), len(str(row.get("fullName") or ""))))
    return merged[:args.limit]


def resolve_page(conn: sqlite3.Connection, page: str) -> sqlite3.Row | None:
    clean = page.strip().replace("\\", "/")
    candidates = [clean]
    if clean.endswith(".html"):
        candidates.append(clean[:-5])
    if not clean.startswith(("Manual/", "ScriptReference/", "Package/")):
        candidates.extend([f"Manual/{clean}", f"ScriptReference/{clean}", f"Package/{clean}"])
    for candidate in candidates:
        row = conn.execute("SELECT * FROM pages WHERE id = ? OR slug = ? OR url_path = ? LIMIT 1", (candidate, candidate, candidate)).fetchone()
        if row:
            return row
    row = conn.execute("SELECT * FROM pages WHERE title = ? COLLATE NOCASE LIMIT 1", (clean,)).fetchone()
    if row:
        return row
    return conn.execute("SELECT * FROM pages WHERE title LIKE ? OR slug LIKE ? LIMIT 1", (f"%{clean}%", f"%{clean}%")).fetchone()


def show_db(db_path: Path, page: str, sections_filter: list[str] | None, max_chars: int, docset: dict[str, Any] | None = None) -> dict[str, Any]:
    conn = ensure_db(db_path)
    page_row = resolve_page(conn, page)
    if not page_row:
        conn.close()
        raise SystemExit(f"No Unity documentation page matched: {page}")
    section_rows = [row_dict(row) for row in conn.execute("SELECT * FROM sections WHERE page_id = ? ORDER BY ordinal", (page_row["id"],))]
    if sections_filter:
        wanted = [s.lower() for s in sections_filter]
        filtered = [row for row in section_rows if any(w in row["heading_path"].lower() for w in wanted)]
        if filtered:
            section_rows = filtered
    remaining = max_chars
    output_sections: list[dict[str, Any]] = []
    truncated = False
    for row in section_rows:
        text = row["text"]
        if remaining <= 0:
            truncated = True
            break
        if len(text) > remaining:
            text = text[: max(0, remaining)] + "…"
            truncated = True
        remaining -= len(text)
        output_sections.append({"headingPath": row["heading_path"], "text": text, "tokenEstimate": row["token_estimate"]})
    page_data = annotate_docset(row_dict(page_row), docset or {})
    result = {"page": page_data, "sections": output_sections, "truncated": truncated}
    conn.close()
    return result


def show_docsets(args: argparse.Namespace, sections_filter: list[str] | None) -> dict[str, Any]:
    page = args.page
    explicit_docset = getattr(args, "docset", None)
    if not explicit_docset and ":" in page:
        possible_docset, possible_page = page.split(":", 1)
        config = load_config()
        known_docsets = core_unity_docsets(config)
        known_docsets.update(config.get("docsets") or {})
        if possible_docset in known_docsets:
            explicit_docset = possible_docset
            page = possible_page
    if explicit_docset:
        args = argparse.Namespace(**{**vars(args), "docset": explicit_docset, "docsets": None, "page": page})
    if has_direct_db_selector(args):
        db_path = resolve_db_path(args)
        return show_db(db_path, page, sections_filter, args.max_chars, docset_from_db_metadata(db_path))

    matches: list[dict[str, Any]] = []
    missing: list[str] = []
    for docset_id, db_path, docset in resolve_query_docsets(args):
        if not db_path.exists():
            continue
        try:
            matches.append(show_db(db_path, page, sections_filter, args.max_chars, docset))
        except SystemExit:
            missing.append(docset_id)
    if not matches:
        raise SystemExit(f"No documentation page matched: {page}")
    if len(matches) > 1 and not explicit_docset:
        labels = ", ".join(str(match["page"].get("docsetId")) for match in matches)
        raise SystemExit(f"Multiple documentation pages matched '{page}'. Pass --docset. Matches: {labels}")
    return matches[0]


def info_db(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    db_path: Path | None = None
    try:
        db_path = resolve_db_path(args)
    except SystemExit:
        db_path = None
    discovered = [str(path) for path in discover_sources()] if getattr(args, "discover", False) else []
    project_version = read_project_unity_version(getattr(args, "project", None))
    info: dict[str, Any] = {
        "configPath": str(CONFIG_PATH),
        "config": config,
        "activeDbPath": str(db_path) if db_path else None,
        "dbExists": bool(db_path and db_path.exists()),
        "discoveredSources": discovered,
    }
    requested_info_version = project_version or config.get("activeVersion")
    resolved_info_version, info_match_kind = resolve_configured_unity_version(requested_info_version, config.get("databases") or {})
    if project_version:
        info["projectVersion"] = project_version
    if resolved_info_version:
        info["resolvedDocsVersion"] = resolved_info_version
        info["versionMatch"] = info_match_kind
    if db_path and db_path.exists():
        conn = ensure_db(db_path)
        metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata")}
        info["metadata"] = metadata
        conn.close()
    docset_status: dict[str, Any] = {}
    configured_docsets = core_unity_docsets(config)
    configured_docsets.update(dict(config.get("docsets") or {}))
    for docset_id, docset in configured_docsets.items():
        db_path_value = docset.get("dbPath")
        source_path_value = docset.get("sourcePath")
        docset_db = Path(db_path_value).expanduser() if db_path_value else None
        source_is_remote = bool(source_path_value and str(source_path_value).startswith(("http://", "https://")))
        docset_source = Path(source_path_value).expanduser() if source_path_value and not source_is_remote else None
        status: dict[str, Any] = {
            **docset,
            "dbExists": bool(docset_db and docset_db.exists()),
            "sourceExists": None if source_is_remote else bool(docset_source and docset_source.exists()),
            "sourceRemote": source_is_remote,
        }
        if docset_db and docset_db.exists():
            status["metadata"] = read_db_metadata(docset_db)
        docset_status[docset_id] = status
    if docset_status:
        info["docsets"] = docset_status
    return info


def print_json_or_text(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if isinstance(data, list):
        for index, item in enumerate(data, 1):
            title = item.get("title") or item.get("fullName") or item.get("pageId")
            page = item.get("pageId", "")
            heading = item.get("headingPath")
            snippet = item.get("snippet") or item.get("summary") or item.get("signature") or ""
            print(f"{index}. {page} — {title}")
            if heading:
                print(f"   Section: {heading}")
            if snippet:
                print(f"   {normalize_space(snippet)}")
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_configure(args: argparse.Namespace) -> None:
    source = Path(args.source).expanduser() if args.source else None
    if not source:
        discovered = discover_sources()
        source = discovered[0] if discovered else None
    if not source:
        raise SystemExit("Pass --source; no Unity documentation source was discovered.")
    validate_source(source)
    version = args.version or infer_version_from_source(source)
    db_dir = Path(args.db_dir).expanduser() if args.db_dir else default_db_dir(version)
    db_path = db_path_from_dir(db_dir)
    if not args.yes:
        print(f"Source: {source}")
        print(f"Version: {version}")
        print(f"Database: {db_path}")
        answer = input("Write this configuration? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise SystemExit("Cancelled.")
    update_config(version, source, db_path)
    print_json_or_text({"configured": True, "version": version, "sourcePath": str(source), "dbPath": str(db_path), "configPath": str(CONFIG_PATH)}, args.json)


def cmd_build(args: argparse.Namespace) -> None:
    source = resolve_source_path(args)
    version = configured_or_project_version(args) or infer_version_from_source(source)
    db_path = resolve_db_path(args) if (args.db or args.db_dir) else db_path_from_dir(default_db_dir(version))
    if not (args.db or args.db_dir):
        # Preserve configured exact-version dbPath when available, but do not
        # build an exact patch version into a same-line fallback DB.
        config = load_config()
        configured_db = (config.get("databases") or {}).get(version, {})
        if isinstance(configured_db, dict) and configured_db.get("dbPath"):
            db_path = Path(configured_db["dbPath"]).expanduser()
    result = build_database(source, db_path, version, force=args.force, limit=args.limit, write_config=not args.no_config, progress=args.progress)
    print_json_or_text(result, args.json)


def cmd_search(args: argparse.Namespace) -> None:
    data = search_docsets(args)
    print_json_or_text(data, args.json)


def cmd_symbol(args: argparse.Namespace) -> None:
    data = symbol_docsets(args)
    print_json_or_text(data, args.json)


def cmd_show(args: argparse.Namespace) -> None:
    sections = [s.strip() for s in args.sections.split(",") if s.strip()] if args.sections else None
    data = show_docsets(args, sections)
    if args.json:
        print_json_or_text(data, True)
        return
    page = data["page"]
    print(f"# {page['id']} — {page['title']}")
    if page.get("summary"):
        print(f"Summary: {page['summary']}")
    for section in data["sections"]:
        print(f"\n## {section['headingPath']}\n{section['text']}")
    if data.get("truncated"):
        print("\n[truncated]")


DEFAULT_VALIDATION_QUERIES: dict[str, list[dict[str, Any]]] = {
    "unity": [
        {"kind": "symbol", "query": "UnityEngine.Physics.Raycast", "expect": ["Physics.Raycast"]},
        {"kind": "search", "query": "Input.GetKeyDown", "expect": ["Input"]},
    ],
    "input-system": [
        {"kind": "search", "query": "PlayerInput input actions", "expect": ["PlayerInput"]},
        {"kind": "search", "query": "rebinding", "expect": ["rebinding"]},
    ],
    "unitask": [
        {"kind": "search", "query": "UniTask async await", "expect": ["UniTask"]},
    ],
    "test-framework": [
        {"kind": "search", "query": "Unity Test Framework", "expect": ["Test"]},
    ],
    "ui-test-framework": [
        {"kind": "search", "query": "click visual element", "expect": ["Click"]},
    ],
    "text-animator": [
        {"kind": "search", "query": "typewriter wait input", "expect": ["Wait"]},
        {"kind": "search", "query": "Yarn Spinner", "expect": ["Yarn"]},
    ],
    "yarn-spinner": [
        {"kind": "search", "query": "Unity Yarn Project", "expect": ["Yarn"]},
        {"kind": "search", "query": "DialogueRunner command handler", "expect": ["AddCommandHandler"]},
    ],
    "shapes": [
        {"kind": "search", "query": "Draw Line Vector3 start end", "expect": ["Line"]},
        {"kind": "search", "query": "Draw Polyline path thickness", "expect": ["Polyline"]},
    ],
    "dotween": [
        {"kind": "search", "query": "DOMove SetEase Sequence", "expect": ["SetEase"]},
        {"kind": "symbol", "query": "DORotateQuaternion", "expect": ["DORotateQuaternion"]},
    ],
    "odin": [
        {"kind": "symbol", "query": "ShowIfAttribute", "expect": ["ShowIfAttribute"]},
        {"kind": "search", "query": "custom value drawer", "expect": ["Value Drawer"]},
    ],
    "unity-cli": [
        {"kind": "search", "query": "install modules editor", "expect": ["install", "modules"]},
        {"kind": "search", "query": "exit code 130 cancellation", "expect": ["130"]},
    ],
    "unity-pipeline": [
        {"kind": "search", "query": "recompile status domain reload", "expect": ["recompile", "status"]},
        {"kind": "search", "query": "run_tests async_tests test_status", "expect": ["run_tests", "test"]},
    ],
}


def validation_queries_for_docset(docset_id: str, docset: dict[str, Any]) -> list[dict[str, Any]]:
    configured = docset.get("validationQueries")
    if isinstance(configured, list):
        return [item for item in configured if isinstance(item, dict) and item.get("query")]
    if docset_id in DEFAULT_VALIDATION_QUERIES:
        return DEFAULT_VALIDATION_QUERIES[docset_id]
    if docset.get("packageName") == "com.unity.pipeline":
        return DEFAULT_VALIDATION_QUERIES["unity-pipeline"]
    if docset.get("docsetKind") == "unity" or docset.get("kind") == "unity":
        return DEFAULT_VALIDATION_QUERIES["unity"]
    return []


def result_text_for_validation(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows[:5]:
        parts.extend(str(row.get(key) or "") for key in ["fullName", "shortName", "title", "pageId", "headingPath", "snippet", "summary"])
    return "\n".join(parts).lower()


def validate_docsets(args: argparse.Namespace) -> dict[str, Any]:
    resolved = resolve_query_docsets(args)
    docset_results: list[dict[str, Any]] = []
    total = 0
    passed = 0
    for docset_id, db_path, docset in resolved:
        queries = validation_queries_for_docset(docset_id, docset)
        checks: list[dict[str, Any]] = []
        for check in queries:
            total += 1
            kind = str(check.get("kind") or "search")
            query = str(check["query"])
            expect = [str(value).lower() for value in check.get("expect") or []]
            if kind == "symbol":
                rows = symbol_db(db_path, query, args.limit, docset)
            else:
                rows = search_db(db_path, query, args.limit, None, docset)
            haystack = result_text_for_validation(rows)
            ok = bool(rows) and all(expected in haystack for expected in expect)
            if ok:
                passed += 1
            checks.append({
                "kind": kind,
                "query": query,
                "expect": check.get("expect") or [],
                "passed": ok,
                "resultCount": len(rows),
                "topResult": rows[0] if rows else None,
            })
        docset_results.append({
            "docsetId": docset_id,
            "docsetTitle": docset.get("docsetTitle"),
            "dbPath": str(db_path),
            "checkCount": len(checks),
            "passed": all(check["passed"] for check in checks) if checks else None,
            "checks": checks,
        })
    return {"passed": passed, "total": total, "failed": total - passed, "docsets": docset_results}


def cmd_validate(args: argparse.Namespace) -> None:
    print_json_or_text(validate_docsets(args), args.json)


def cmd_info(args: argparse.Namespace) -> None:
    print_json_or_text(info_db(args), args.json)


def cmd_build_docset(args: argparse.Namespace) -> None:
    llms_url = args.llms_url
    llms_section = args.llms_section
    ingesting = bool(llms_url or args.html_url or args.xml_doc)
    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
    try:
        source_label: str | None = None
        if ingesting:
            source_label = args.source or llms_url or (args.html_url[0] if args.html_url else None) or (args.xml_doc[0] if args.xml_doc else None)
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="unity_docs_docset_stage_")
            stage_root = Path(temp_dir_obj.name)
            docs_source = stage_root / "Documentation~"
            docs_source.mkdir(parents=True, exist_ok=True)
            source_kind = "staged"
            project_path = None
            metadata: dict[str, Any] = {}
            if args.source:
                base_source, base_kind, project_path = resolve_package_docs_source(args.source, args.project, args.package_name, args.package_version)
                source_kind = f"{base_kind}+staged"
                package_root = find_package_root(base_source)
                metadata = load_package_metadata(package_root)
                shutil.copytree(base_source, docs_source, dirs_exist_ok=True)
            if llms_url:
                stage_llms_manifest(llms_url, llms_section, docs_source, args.limit)
            if args.html_url:
                stage_html_urls(args.html_url, docs_source, args.html_split_level or 0, args.limit)
            if args.xml_doc:
                stage_xml_docs([Path(path).expanduser() for path in args.xml_doc], docs_source)
            package_name = args.package_name or metadata.get("name") or args.docset_id or "package-docs"
            package_version = args.package_version or metadata.get("version") or "unknown"
            package_json = {
                "name": package_name,
                "displayName": args.title or metadata.get("displayName") or package_name,
                "version": package_version,
                "documentationUrl": llms_url or (args.html_url[0] if args.html_url else metadata.get("documentationUrl")),
            }
            (stage_root / "package.json").write_text(json.dumps(package_json, indent=2), encoding="utf-8")
        else:
            docs_source, source_kind, project_path = resolve_package_docs_source(args.source, args.project, args.package_name, args.package_version)
            package_root = find_package_root(docs_source)
            metadata = load_package_metadata(package_root)
            package_name = args.package_name or metadata.get("name") or package_root.name.split("@")[0]
            package_version = args.package_version

        docset_id = sanitize_docset_id(args.docset_id or package_name)
        db_path = resolve_db_path(args) if (args.db or args.db_dir) else db_path_from_dir(default_db_dir(docset_id))
        result = build_package_docset(
            docs_source,
            db_path,
            docset_id=docset_id,
            title=args.title,
            package_name=package_name,
            package_version=package_version,
            force=args.force,
            limit=None if ingesting else args.limit,
            write_config=not args.no_config,
            progress=args.progress,
            source_kind=source_kind,
            project_path=project_path,
        )
        if ingesting and source_label:
            result["sourcePath"] = source_label
            if source_label.startswith(("http://", "https://")):
                result["sourceUrl"] = source_label
        if ingesting and not args.no_config and source_label:
            config = load_config()
            docset_config = (config.get("docsets") or {}).get(docset_id)
            if isinstance(docset_config, dict):
                docset_config["sourcePath"] = source_label
                if source_label.startswith(("http://", "https://")):
                    docset_config["sourceUrl"] = source_label
                save_config(config)
        print_json_or_text(result, args.json)
    finally:
        if temp_dir_obj is not None:
            temp_dir_obj.cleanup()


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and query a SQLite cache for Unity offline documentation.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser("configure", help="Write Unity docs cache configuration.")
    add_json_flag(configure)
    configure.add_argument("--source", help="Unity Documentation/en source directory.")
    configure.add_argument("--db-dir", help="Directory where unity_docs.sqlite should be installed.")
    configure.add_argument("--version", help="Unity version label.")
    configure.add_argument("--yes", action="store_true", help="Do not prompt before writing configuration.")
    configure.set_defaults(func=cmd_configure)

    build = sub.add_parser("build", help="Build or rebuild the SQLite cache.")
    add_json_flag(build)
    build.add_argument("--source", help="Unity Documentation/en source directory.")
    build.add_argument("--db", help="Explicit SQLite database path.")
    build.add_argument("--db-dir", help="Directory where unity_docs.sqlite should be installed.")
    build.add_argument("--version", help="Unity version label.")
    build.add_argument("--project", help="Unity project path used to infer the documentation version from ProjectSettings/ProjectVersion.txt.")
    build.add_argument("--force", action="store_true", help="Replace an existing database.")
    build.add_argument("--limit", type=int, help="Debug: only process this many pages total.")
    build.add_argument("--no-config", action="store_true", help="Do not update ~/.pi/unity-docs/config.json after building.")
    build.add_argument("--progress", action="store_true", help="Emit progress JSON lines to stderr while building.")
    build.set_defaults(func=cmd_build)

    build_docset = sub.add_parser("build-docset", help="Build or rebuild a package documentation docset.")
    add_json_flag(build_docset)
    build_docset.add_argument("--source", help="Package Documentation~ source directory or package root containing Documentation~.")
    build_docset.add_argument("--project", help="Unity project path used to resolve embedded packages or Library/PackageCache packages.")
    build_docset.add_argument("--package-name", help="Unity package name to resolve from a project or record in metadata.")
    build_docset.add_argument("--package-version", help="Optional package version to resolve from PackageCache or record in metadata.")
    build_docset.add_argument("--docset-id", help="Docset id to register. Defaults to the package name.")
    build_docset.add_argument("--title", help="Human-readable docset title. Defaults to package displayName/name.")
    build_docset.add_argument("--db", help="Explicit SQLite database path.")
    build_docset.add_argument("--db-dir", help="Directory where unity_docs.sqlite should be installed.")
    build_docset.add_argument("--force", action="store_true", help="Replace an existing database.")
    build_docset.add_argument("--limit", type=int, help="Debug: only process this many Markdown pages.")
    build_docset.add_argument("--no-config", action="store_true", help="Do not update ~/.pi/unity-docs/config.json after building.")
    build_docset.add_argument("--progress", action="store_true", help="Emit progress JSON lines to stderr while building.")
    llms_url_group = build_docset.add_mutually_exclusive_group()
    llms_url_group.add_argument("--llms-url", dest="llms_url", help="llms.txt URL to mirror into a temporary Markdown docset source before building.")
    llms_url_group.add_argument("--gitbook-llms-url", dest="llms_url", help="Compatibility alias for --llms-url.")
    llms_section_group = build_docset.add_mutually_exclusive_group()
    llms_section_group.add_argument("--llms-section", dest="llms_section", help="Optional llms.txt section heading to mirror.")
    llms_section_group.add_argument("--gitbook-section", dest="llms_section", help="Compatibility alias for --llms-section.")
    build_docset.add_argument("--html-url", action="append", help="Public HTML documentation URL to convert to Markdown before building. May be passed multiple times.")
    build_docset.add_argument("--html-split-level", type=int, choices=range(1, 7), metavar="1-6", help="Split converted HTML pages into Markdown pages at this heading level.")
    build_docset.add_argument("--xml-doc", action="append", help="C# XML documentation file to convert to Markdown API pages before building. May be passed multiple times.")
    build_docset.set_defaults(func=cmd_build_docset)

    info = sub.add_parser("info", help="Show configuration and database status.")
    add_json_flag(info)
    info.add_argument("--db", help="Explicit SQLite database path.")
    info.add_argument("--db-dir", help="Directory containing unity_docs.sqlite.")
    info.add_argument("--version", help="Unity version label from config.")
    info.add_argument("--project", help="Unity project path used to select the matching Unity documentation version.")
    info.add_argument("--profile", help="Documentation profile to inspect.")
    info.add_argument("--docset", help="Documentation docset id to inspect.")
    info.add_argument("--discover", action="store_true", help="Include discovered Unity documentation sources.")
    info.set_defaults(func=cmd_info)

    validate = sub.add_parser("validate", help="Run representative validation queries against configured docsets.")
    add_json_flag(validate)
    validate.add_argument("--db", help="Explicit SQLite database path.")
    validate.add_argument("--db-dir", help="Directory containing unity_docs.sqlite.")
    validate.add_argument("--version", help="Unity version label from config.")
    validate.add_argument("--project", help="Unity project path used to select the matching Unity documentation version.")
    validate.add_argument("--profile", help="Documentation profile to validate.")
    validate.add_argument("--docset", help="Documentation docset id to validate.")
    validate.add_argument("--docsets", help="Comma-separated documentation docset ids to validate.")
    validate.add_argument("--limit", type=int, default=5)
    validate.set_defaults(func=cmd_validate)

    search = sub.add_parser("search", help="Search section-level Unity docs.")
    add_json_flag(search)
    search.add_argument("query")
    search.add_argument("--db", help="Explicit SQLite database path.")
    search.add_argument("--db-dir", help="Directory containing unity_docs.sqlite.")
    search.add_argument("--version", help="Unity version label from config.")
    search.add_argument("--project", help="Unity project path used to select the matching Unity documentation version.")
    search.add_argument("--profile", help="Documentation profile to search.")
    search.add_argument("--docset", help="Documentation docset id to search.")
    search.add_argument("--docsets", help="Comma-separated documentation docset ids to search.")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--corpus", choices=["Manual", "ScriptReference", "Package"], help="Limit search to a corpus.")
    search.set_defaults(func=cmd_search)

    symbol = sub.add_parser("symbol", help="Look up a Unity API symbol.")
    add_json_flag(symbol)
    symbol.add_argument("name")
    symbol.add_argument("--db", help="Explicit SQLite database path.")
    symbol.add_argument("--db-dir", help="Directory containing unity_docs.sqlite.")
    symbol.add_argument("--version", help="Unity version label from config.")
    symbol.add_argument("--project", help="Unity project path used to select the matching Unity documentation version.")
    symbol.add_argument("--profile", help="Documentation profile to search.")
    symbol.add_argument("--docset", help="Documentation docset id to search.")
    symbol.add_argument("--docsets", help="Comma-separated documentation docset ids to search.")
    symbol.add_argument("--limit", type=int, default=8)
    symbol.set_defaults(func=cmd_symbol)

    show = sub.add_parser("show", help="Show selected sections from a Unity docs page.")
    add_json_flag(show)
    show.add_argument("page")
    show.add_argument("--db", help="Explicit SQLite database path.")
    show.add_argument("--db-dir", help="Directory containing unity_docs.sqlite.")
    show.add_argument("--version", help="Unity version label from config.")
    show.add_argument("--project", help="Unity project path used to select the matching Unity documentation version.")
    show.add_argument("--profile", help="Documentation profile to search.")
    show.add_argument("--docset", help="Documentation docset id to search.")
    show.add_argument("--docsets", help="Comma-separated documentation docset ids to search.")
    show.add_argument("--sections", help="Comma-separated heading filters.")
    show.add_argument("--max-chars", type=int, default=6000)
    show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
