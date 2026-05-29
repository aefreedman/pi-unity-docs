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
    for index, part in enumerate(parts):
        if part.lower() == "editor" and index > 0:
            return parts[index - 1]
    return "unknown"


def default_db_dir(version: str) -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "pi" / "unity-docs" / version
    return Path.home() / ".pi" / "unity-docs" / version


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
    version = getattr(args, "version", None) or config.get("activeVersion")
    databases = config.get("databases") or {}
    if version and version in databases and databases[version].get("dbPath"):
        return Path(databases[version]["dbPath"]).expanduser()
    if config.get("dbPath"):
        return Path(config["dbPath"]).expanduser()
    raise SystemExit("No database configured. Run configure or pass --db/--db-dir.")


def resolve_source_path(args: argparse.Namespace) -> Path:
    if getattr(args, "source", None):
        return Path(args.source).expanduser()
    config = load_config()
    version = getattr(args, "version", None) or config.get("activeVersion")
    databases = config.get("databases") or {}
    if version and version in databases and databases[version].get("sourcePath"):
        return Path(databases[version]["sourcePath"]).expanduser()
    if config.get("sourcePath"):
        return Path(config["sourcePath"]).expanduser()
    raise SystemExit("No Unity documentation source configured. Run configure or pass --source.")


def validate_source(source: Path) -> None:
    if not source.exists():
        raise SystemExit(f"Unity documentation source does not exist: {source}")
    missing = [name for name in ["Manual", "ScriptReference"] if not (source / name).is_dir()]
    if missing:
        raise SystemExit(f"Unity documentation source is missing {', '.join(missing)}: {source}")


def discover_sources() -> list[Path]:
    roots: list[Path] = []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        roots.append(Path(program_files) / "Unity" / "Hub" / "Editor")
    roots.append(Path("C:/Program Files/Unity/Hub/Editor"))
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for child in root.iterdir():
            candidate = child / "Editor" / "Data" / "Documentation" / "en"
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
                    "INSERT INTO pages(id, version, corpus, slug, title, kind, uid, source_path, url_path, breadcrumbs, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (page_id, version, corpus, slug, title, kind, uid, str(html_path), f"{corpus}/{rel}", breadcrumbs, summary),
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


def update_config(version: str, source: Path, db_path: Path) -> None:
    config = load_config()
    config.setdefault("databases", {})[version] = {
        "sourcePath": str(source),
        "dbPath": str(db_path),
    }
    config["activeVersion"] = version
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
    return {key: row[key] for key in row.keys()}


def search_db(db_path: Path, query: str, limit: int, corpus: str | None = None) -> list[dict[str, Any]]:
    conn = ensure_db(db_path)
    match = fts_query(query)
    params: list[Any] = [match]
    corpus_sql = ""
    if corpus:
        corpus_sql = " AND p.corpus = ?"
        params.append(corpus)
    params.append(limit)
    sql = f"""
        SELECT s.id AS sectionId, s.page_id AS pageId, p.title, p.corpus, p.kind, p.slug,
               p.breadcrumbs, s.heading_path AS headingPath,
               snippet(sections_fts, 4, '[', ']', ' … ', 24) AS snippet,
               bm25(sections_fts) AS rank
        FROM sections_fts
        JOIN sections s ON s.id = sections_fts.section_id
        JOIN pages p ON p.id = s.page_id
        WHERE sections_fts MATCH ? {corpus_sql}
        ORDER BY rank
        LIMIT ?
    """
    try:
        rows = [row_dict(row) for row in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def symbol_db(db_path: Path, name: str, limit: int) -> list[dict[str, Any]]:
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
                   p.id AS pageId, p.title, p.corpus, p.slug, p.summary
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
        for row in conn.execute(
            "SELECT id AS pageId, title, corpus, slug, kind, uid, summary FROM pages WHERE title = ? LIMIT ?",
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
            return rows[:limit]
    append_page_rows(clean)
    if rows:
        conn.close()
        return rows[:limit]
    append_symbol_rows("sym.short_name = ?", (clean,))
    if short != clean:
        append_symbol_rows("sym.short_name = ?", (short,))
    if len(rows) < limit:
        append_symbol_rows("sym.full_name GLOB ?", (f"*{clean}*",))

    conn.close()
    return rows[:limit]


def resolve_page(conn: sqlite3.Connection, page: str) -> sqlite3.Row | None:
    clean = page.strip().replace("\\", "/")
    candidates = [clean]
    if clean.endswith(".html"):
        candidates.append(clean[:-5])
    if not clean.startswith(("Manual/", "ScriptReference/")):
        candidates.extend([f"Manual/{clean}", f"ScriptReference/{clean}"])
    for candidate in candidates:
        row = conn.execute("SELECT * FROM pages WHERE id = ? OR slug = ? OR url_path = ? LIMIT 1", (candidate, candidate, candidate)).fetchone()
        if row:
            return row
    row = conn.execute("SELECT * FROM pages WHERE title = ? COLLATE NOCASE LIMIT 1", (clean,)).fetchone()
    if row:
        return row
    return conn.execute("SELECT * FROM pages WHERE title LIKE ? OR slug LIKE ? LIMIT 1", (f"%{clean}%", f"%{clean}%")).fetchone()


def show_db(db_path: Path, page: str, sections_filter: list[str] | None, max_chars: int) -> dict[str, Any]:
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
    result = {"page": row_dict(page_row), "sections": output_sections, "truncated": truncated}
    conn.close()
    return result


def info_db(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    db_path: Path | None = None
    try:
        db_path = resolve_db_path(args)
    except SystemExit:
        db_path = None
    discovered = [str(path) for path in discover_sources()] if getattr(args, "discover", False) else []
    info: dict[str, Any] = {
        "configPath": str(CONFIG_PATH),
        "config": config,
        "activeDbPath": str(db_path) if db_path else None,
        "dbExists": bool(db_path and db_path.exists()),
        "discoveredSources": discovered,
    }
    if db_path and db_path.exists():
        conn = ensure_db(db_path)
        metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata")}
        info["metadata"] = metadata
        conn.close()
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
    version = args.version or infer_version_from_source(source)
    db_path = resolve_db_path(args) if (args.db or args.db_dir) else db_path_from_dir(default_db_dir(version))
    if not (args.db or args.db_dir):
        # Preserve configured dbPath when available.
        try:
            db_path = resolve_db_path(args)
        except SystemExit:
            pass
    result = build_database(source, db_path, version, force=args.force, limit=args.limit, write_config=not args.no_config, progress=args.progress)
    print_json_or_text(result, args.json)


def cmd_search(args: argparse.Namespace) -> None:
    data = search_db(resolve_db_path(args), args.query, args.limit, args.corpus)
    print_json_or_text(data, args.json)


def cmd_symbol(args: argparse.Namespace) -> None:
    data = symbol_db(resolve_db_path(args), args.name, args.limit)
    print_json_or_text(data, args.json)


def cmd_show(args: argparse.Namespace) -> None:
    sections = [s.strip() for s in args.sections.split(",") if s.strip()] if args.sections else None
    data = show_db(resolve_db_path(args), args.page, sections, args.max_chars)
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


def cmd_info(args: argparse.Namespace) -> None:
    print_json_or_text(info_db(args), args.json)


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
    build.add_argument("--force", action="store_true", help="Replace an existing database.")
    build.add_argument("--limit", type=int, help="Debug: only process this many pages total.")
    build.add_argument("--no-config", action="store_true", help="Do not update ~/.pi/unity-docs/config.json after building.")
    build.add_argument("--progress", action="store_true", help="Emit progress JSON lines to stderr while building.")
    build.set_defaults(func=cmd_build)

    info = sub.add_parser("info", help="Show configuration and database status.")
    add_json_flag(info)
    info.add_argument("--db", help="Explicit SQLite database path.")
    info.add_argument("--db-dir", help="Directory containing unity_docs.sqlite.")
    info.add_argument("--version", help="Unity version label from config.")
    info.add_argument("--discover", action="store_true", help="Include discovered Unity documentation sources.")
    info.set_defaults(func=cmd_info)

    search = sub.add_parser("search", help="Search section-level Unity docs.")
    add_json_flag(search)
    search.add_argument("query")
    search.add_argument("--db", help="Explicit SQLite database path.")
    search.add_argument("--db-dir", help="Directory containing unity_docs.sqlite.")
    search.add_argument("--version", help="Unity version label from config.")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--corpus", choices=["Manual", "ScriptReference"], help="Limit search to a corpus.")
    search.set_defaults(func=cmd_search)

    symbol = sub.add_parser("symbol", help="Look up a Unity API symbol.")
    add_json_flag(symbol)
    symbol.add_argument("name")
    symbol.add_argument("--db", help="Explicit SQLite database path.")
    symbol.add_argument("--db-dir", help="Directory containing unity_docs.sqlite.")
    symbol.add_argument("--version", help="Unity version label from config.")
    symbol.add_argument("--limit", type=int, default=8)
    symbol.set_defaults(func=cmd_symbol)

    show = sub.add_parser("show", help="Show selected sections from a Unity docs page.")
    add_json_flag(show)
    show.add_argument("page")
    show.add_argument("--db", help="Explicit SQLite database path.")
    show.add_argument("--db-dir", help="Directory containing unity_docs.sqlite.")
    show.add_argument("--version", help="Unity version label from config.")
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
