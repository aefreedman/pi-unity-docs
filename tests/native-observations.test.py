"""Offline producer observations: prebuilt SQLite plus disposable build stage injections."""
import argparse
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from importlib.util import spec_from_file_location, module_from_spec
import sys

spec = spec_from_file_location("unity_native", Path(__file__).resolve().parents[1] / "scripts/unity_docs_db.py")
m = module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class Observations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "tiny.sqlite"
        conn = sqlite3.connect(self.db)
        m.create_schema(conn)
        conn.execute("INSERT INTO metadata VALUES ('docsetId', 'tiny')")
        conn.execute("INSERT INTO metadata VALUES ('docsetKind', 'package')")
        conn.execute("INSERT INTO metadata VALUES ('version', '1.0')")
        conn.execute("INSERT INTO pages(id, version, corpus, slug, title, summary, kind, source_path, url_path) VALUES ('Package/Test','1.0','Package','Test','Test','summary','package_page','fixture.md','Package/Test.md')")
        conn.execute("INSERT INTO sections VALUES ('s','Package/Test','Overview',0,'hello world',2)")
        conn.execute("INSERT INTO sections_fts VALUES ('s','Package/Test','Test','Overview','hello world')")
        conn.commit()
        conn.close()
        self.native = patch.object(m, "NATIVE_OBSERVATIONS", True)
        self.native.start()

    def tearDown(self):
        self.native.stop()
        self.temp.cleanup()

    def args(self, **kw):
        return argparse.Namespace(query="hello", name="Test", limit=8, corpus=None, **kw)

    def lookup(self, selections, symbol=False, corpus=None, query="hello"):
        with patch.object(m, "resolve_query_docsets", return_value=selections):
            args = self.args()
            args.corpus, args.query = corpus, query
            return m.native_lookup(args, symbol)

    def source(self, id="tiny", db=None, kind="package"):
        return (id, db or self.db, {"docsetId": id, "docsetKind": kind})

    def test_completed_zero_and_matching(self):
        matched = self.lookup([self.source()])
        self.assertTrue(matched["ok"])
        self.assertEqual(matched["rows"][0]["pageId"], "Package/Test")
        empty = self.lookup([self.source()], query="notfound")
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["rows"], [])
        self.assertEqual(empty["coverage"][0]["status"], "queried")

    def test_exclusion_is_search_only_and_missing_precedes_routing(self):
        excluded = self.lookup([self.source()], corpus="Manual")
        self.assertTrue(excluded["ok"])
        self.assertEqual(excluded["coverage"][0]["status"], "excluded")
        missing = self.lookup([self.source(db=self.root / "absent")], corpus="Manual")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["coverage"][0]["status"], "missing")
        symbols = self.lookup([self.source()], symbol=True)
        self.assertTrue(symbols["ok"])
        self.assertEqual(symbols["coverage"][0]["status"], "queried")
        self.assertFalse(self.lookup([self.source(db=self.root / "absent")], symbol=True)["ok"])

    def test_sql_all_failed_and_mixed(self):
        bad = self.root / "bad.sqlite"
        sqlite3.connect(bad).close()
        failed = self.lookup([self.source(db=bad)])
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["coverage"][0]["status"], "failed")
        mixed = self.lookup([self.source(), self.source("broken", bad)])
        self.assertTrue(mixed["ok"])
        self.assertEqual([x["status"] for x in mixed["coverage"]], ["queried", "failed"])
        self.assertEqual(len(mixed["rows"]), 1)
        self.assertFalse(self.lookup([self.source(db=bad)], symbol=True)["ok"])

    def test_symbol_connections_close_on_success_and_sql_failure(self):
        bad = self.root / "bad.sqlite"
        sqlite3.connect(bad).close()
        for db, name, fails in [(self.db, "Test", False), (self.db, "Missing.Name", False), (bad, "Test", True)]:
            with self.subTest(db=db.name, name=name):
                conn = m.ensure_db(db)
                with patch.object(m, "ensure_db", return_value=conn):
                    if fails:
                        with self.assertRaises(sqlite3.OperationalError):
                            m.symbol_db(db, name, 8)
                    else:
                        m.symbol_db(db, name, 8)
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")

    def test_explicit_missing_disabled_and_resolution_failures(self):
        args = self.args(db=None, db_dir=None, version=None, project=None, profile=None, docsets="good,disabled", docset=None)
        cfg = {"docsets": {"good": {"dbPath": str(self.db)}, "disabled": {"dbPath": str(self.db), "enabled": False}}, "profiles": {"default": ["good", "disabled"]}}
        with patch.object(m, "load_config", return_value=cfg):
            with self.assertRaises(ValueError):
                m.resolve_query_docsets(args)
            args.docsets = ""
            with self.assertRaises(ValueError):
                m.resolve_query_docsets(args)
            args.docsets = "unknown"
            with self.assertRaises(ValueError):
                m.resolve_query_docsets(args)
        with patch.object(m, "resolve_query_docsets", side_effect=SystemExit("resolution")):
            with self.assertRaises(SystemExit):
                m.native_lookup(args)

    def test_unresolved_explicit_profile_never_queries_or_claims_exclusion(self):
        cfg = {"docsets": {"tiny": {"dbPath": str(self.db), "kind": "package"}}}
        for corpus in [None, "Manual"]:
            args = self.args(profile="does-not-exist")
            args.corpus = corpus
            with self.subTest(corpus=corpus), patch.object(m, "load_config", return_value=cfg) as load, patch.object(m, "search_db") as search, patch.object(m, "symbol_db") as symbol:
                with self.assertRaisesRegex(ValueError, "^unresolved_profile$"):
                    m.native_lookup(args)
                self.assertEqual(load.call_count, 1)
                search.assert_not_called()
                symbol.assert_not_called()
        with patch.object(m, "load_config", return_value=cfg), patch.object(m, "NATIVE_OBSERVATIONS", False):
            args.corpus = None
            self.assertEqual(m.search_docsets(args)[0]["pageId"], "Package/Test")
            args.corpus = "Manual"
            self.assertEqual(m.search_docsets(args), [])

    def test_unresolved_configured_profile_precedes_core_or_legacy_fallback(self):
        configs = [
            {"docsets": {"tiny": {"dbPath": str(self.db), "kind": "package"}}, "activeProfile": "missing"},
            {"databases": {"1.0": {"dbPath": str(self.db)}}, "activeVersion": "1.0", "activeProfile": "missing"},
            {"dbPath": str(self.db), "activeProfile": "missing"},
            {"docsets": {"tiny": {"dbPath": str(self.db)}}, "profiles": {"default": None}},
        ]
        for cfg in configs:
            with self.subTest(config=cfg), patch.object(m, "load_config", return_value=cfg):
                with self.assertRaisesRegex(ValueError, "^unresolved_profile$"):
                    m.native_lookup(self.args())
                with patch.object(m, "NATIVE_OBSERVATIONS", False):
                    self.assertEqual(len(m.resolve_query_docsets(self.args())), 1)
        with patch.object(m, "load_config", return_value={"dbPath": str(self.db)}):
            with self.assertRaisesRegex(ValueError, "^unresolved_profile$"):
                m.native_lookup(self.args(profile="missing"))

    def test_valid_profiles_and_unconfigured_default_keep_legacy_selection(self):
        docsets = {"tiny": {"dbPath": str(self.db), "kind": "package"}, "other": {"dbPath": str(self.db), "kind": "package"}}
        cases = [
            ({"docsets": docsets}, None, ["tiny", "other"]),
            ({"docsets": docsets, "profiles": {"unselected": ["other"]}}, None, ["tiny", "other"]),
            ({"docsets": docsets, "profiles": {"default": ["tiny"]}}, None, ["tiny"]),
            ({"docsets": docsets, "profiles": {"chosen": ["tiny"]}, "activeProfile": "chosen"}, None, ["tiny"]),
            ({"docsets": docsets, "profiles": {"chosen": ["tiny"], "default": ["other"]}, "activeProfile": "missing"}, "chosen", ["tiny"]),
            ({"docsets": docsets, "profiles": {"default": []}}, None, ["tiny", "other"]),
            ({"dbPath": str(self.db)}, None, ["tiny"]),
        ]
        for cfg, profile, expected in cases:
            args = self.args(profile=profile)
            with self.subTest(config=cfg, profile=profile), patch.object(m, "load_config", return_value=cfg):
                for native in [True, False]:
                    with patch.object(m, "NATIVE_OBSERVATIONS", native):
                        self.assertEqual([id for id, _db, _docset in m.resolve_query_docsets(args)], expected)
                with patch.object(m, "search_db", wraps=m.search_db) as search:
                    result = m.native_lookup(args)
                    self.assertTrue(result["ok"])
                    self.assertEqual(search.call_count, len(expected))
                    self.assertTrue(all(x["status"] == "queried" for x in result["coverage"]))

    def test_docset_and_direct_db_selectors_ignore_irrelevant_profiles(self):
        cfg = {"docsets": {"tiny": {"dbPath": str(self.db), "kind": "package"}}, "databases": {"1.0": {"dbPath": str(self.db)}}, "activeProfile": "missing"}
        selectors = [
            {"docset": "tiny"}, {"docsets": "tiny"},
            {"db": str(self.db)}, {"db_dir": str(self.db)}, {"version": "1.0"},
            {"db": str(self.db), "docset": "unselected-missing"},
        ]
        for selector in selectors:
            args = self.args(profile="also-missing", **selector)
            with self.subTest(selector=selector), patch.object(m, "load_config", return_value=cfg):
                native_selection = m.resolve_query_docsets(args)
                with patch.object(m, "NATIVE_OBSERVATIONS", False):
                    legacy_selection = m.resolve_query_docsets(args)
                    self.assertEqual([(id, db) for id, db, _docset in legacy_selection], [(id, db) for id, db, _docset in native_selection])
                result = m.native_lookup(args)
                self.assertTrue(result["ok"])
                self.assertEqual(result["coverage"][0]["status"], "queried")

    def test_show_fallback_and_ambiguity(self):
        result = m.show_db(self.db, "Package/Test", ["notfound"], 500, {"docsetId": "tiny"})
        self.assertEqual(result["headingSelection"], "fallback_all")
        self.assertEqual(result["selectedSections"], 1)
        args = argparse.Namespace(page="Package/Test", docset=None, db=None, db_dir=None, version=None, max_chars=500)
        with patch.object(m, "resolve_query_docsets", return_value=[self.source(), self.source("other")]):
            with self.assertRaises(SystemExit):
                m.show_docsets(args, None)

    def test_validation_findings_and_sql_failure(self):
        args = self.args()
        source = self.source()
        source[2]["validationQueries"] = [{"kind": "search", "query": "notfound"}]
        with patch.object(m, "resolve_query_docsets", return_value=[source]):
            result = m.validate_docsets(args)
        self.assertEqual((result["passed"], result["total"], result["failed"]), (0, 1, 1))
        with patch.object(m, "resolve_query_docsets", return_value=[source]), patch.object(m, "search_db", side_effect=sqlite3.OperationalError("private")):
            with self.assertRaises(sqlite3.OperationalError):
                m.validate_docsets(args)

    def test_build_publication_before_configuration_failure(self):
        # Only a tiny disposable source/output; no user cache/config is touched.
        source = self.root / "source"
        for corpus in ["Manual", "ScriptReference"]:
            (source / corpus).mkdir(parents=True)
        output = self.root / "built.sqlite"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(m, "update_config", side_effect=OSError("private config")):
            with self.assertRaises(OSError):
                m.build_database(source, output, "1.0")
        self.assertTrue(output.exists())
        lines = stderr.getvalue()
        self.assertIn('"stage": "publication", "state": "completed"', lines)
        self.assertIn('"stage": "configuration", "state": "started"', lines)
        self.assertNotIn('"stage": "configuration", "state": "completed"', lines)

    def test_build_move_failure_and_pre_dispatch_refusal(self):
        source = self.root / "pkg" / "Documentation~"
        source.mkdir(parents=True)
        (source / "Test.md").write_text("# Test\nhello", encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(m.shutil, "move", side_effect=OSError("move")):
            with self.assertRaises(OSError):
                m.build_package_docset(source, self.root / "out.sqlite", "test", write_config=False)
        self.assertIn('"stage": "publication", "state": "started"', stderr.getvalue())
        self.assertNotIn('"stage": "publication", "state": "completed"', stderr.getvalue())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                m.build_package_docset(source, self.db, "test")
        self.assertNotIn('"stage": "database"', stderr.getvalue())
        self.assertNotIn('"stage": "publication"', stderr.getvalue())

    def test_package_configuration_and_final_source_label_failure(self):
        source = self.root / "pkg" / "Documentation~"
        source.mkdir(parents=True)
        (source / "Test.md").write_text("# Test hello", encoding="utf-8")
        output = self.root / "package.sqlite"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(m, "update_docset_config", side_effect=OSError("configuration")):
            with self.assertRaises(OSError):
                m.build_package_docset(source, output, "test")
        self.assertTrue(output.exists())
        self.assertIn('"stage": "publication", "state": "completed"', stderr.getvalue())
        self.assertIn('"stage": "configuration", "state": "started"', stderr.getvalue())
        args = m.build_parser().parse_args(["build-docset", "--docset-id", "test", "--html-file", "fixture.html", "--db-dir", str(self.root / "out")])
        stderr = io.StringIO()
        def fake_build(*a, **kw):
            m.observe_stage("publication", "completed")
            m.observe_stage("configuration", "completed")
            return {"dbPath": "fixture", "sourcePath": "staged"}
        with contextlib.redirect_stderr(stderr), patch.object(m, "stage_html_files"), patch.object(m, "build_package_docset", side_effect=fake_build), patch.object(m, "load_config", return_value={"docsets": {"test": {}}}), patch.object(m, "save_config", side_effect=OSError("final config")):
            with self.assertRaises(OSError):
                m.cmd_build_docset(args)
        self.assertIn('"stage": "source_configuration", "state": "started"', stderr.getvalue())
        self.assertNotIn('"stage": "source_configuration", "state": "completed"', stderr.getvalue())

    def test_native_limit_facts_are_additive(self):
        args = m.build_parser().parse_args(["--native-observations", "--json", "build", "--source", "fixture", "--db-dir", str(self.root / "out"), "--limit", "1"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), patch.object(m, "build_database", return_value={"pages": 0}):
            m.cmd_build(args)
        import json
        facts = json.loads(output.getvalue())
        self.assertEqual(facts["requestedLimit"], 1)
        self.assertEqual(facts["limitPolicy"], "page_limit")
        self.assertEqual(facts["sourceMode"], "local_unity")

    def test_staged_source_failure_does_not_claim_publication(self):
        args = m.build_parser().parse_args(["build-docset", "--html-url", "https://example.invalid", "--db-dir", str(self.root / "output")])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(m, "stage_html_urls", side_effect=ValueError("injected: no network")):
            with self.assertRaises(ValueError):
                m.cmd_build_docset(args)
        self.assertIn('"stage": "source", "state": "started"', stderr.getvalue())
        self.assertNotIn('"stage": "publication"', stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
