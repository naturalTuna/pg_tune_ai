# -*- coding: utf-8 -*-
"""sqlguard 단위 테스트 (DB 불필요)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlguard as g  # noqa: E402


class SplitTest(unittest.TestCase):
    def test_quotes_comments_dollar(self):
        sql = "select ';' as a -- c;\n; select $$x;y$$; /* a; /* nested; */ */ select E'\\';'"
        self.assertEqual(len(g.split_statements(sql)), 3)

    def test_single_ok_trailing_semicolon(self):
        self.assertEqual(g.single_statement("SELECT 1;  "), "SELECT 1")

    def test_multi_rejected(self):
        for s in ("SELECT 1; DROP TABLE t", "SELECT 1;COMMIT;DELETE FROM t",
                  "SELECT '1'';' ; SELECT 2"):
            with self.assertRaises(g.SqlGuardError):
                g.single_statement(s)

    def test_meta_command_rejected(self):
        with self.assertRaises(g.SqlGuardError):
            g.single_statement("SELECT 1 \\! rm -rf /")

    def test_unterminated(self):
        for s in ("SELECT 'abc", "SELECT $$abc", "SELECT /* x"):
            with self.assertRaises(g.SqlGuardError):
                g.single_statement(s)


class KeywordTest(unittest.TestCase):
    def test_first_keyword(self):
        self.assertEqual(g.first_keyword("/* x */ ( select 1)"), "SELECT")
        self.assertEqual(g.first_keyword("-- c\n  delete from t"), "DELETE")
        self.assertTrue(g.is_dml("  update t set a=1"))
        self.assertFalse(g.is_dml("WITH d AS (DELETE FROM t RETURNING *) SELECT 1"))

    def test_readonly(self):
        g.readonly_query('select "update", "delete" from t where a = \'drop\'')
        for s in ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
                  "SELECT nextval('s')", "SELECT * FROM t FOR UPDATE",
                  "DELETE FROM t", "SELECT pg_terminate_backend(1)",
                  "SELECT * FROM t; COMMIT; DROP TABLE x"):
            with self.assertRaises(g.SqlGuardError, msg=s):
                g.readonly_query(s)


class GucTest(unittest.TestCase):
    def test_whitelist(self):
        self.assertEqual(g.guc_setting("work_mem", "'256MB'"), ("work_mem", "256MB"))
        self.assertEqual(g.guc_setting("enable_seqscan", "off"), ("enable_seqscan", "off"))
        for k, v in (("work_mem", "64MB'; COMMIT; DROP TABLE x; --"),
                     ("shared_preload_libraries", "evil"), ("archive_command", "rm"),
                     ("work_mem = 1; drop", "1")):
            with self.assertRaises(g.SqlGuardError):
                g.guc_setting(k, v)


class IndexDdlTest(unittest.TestCase):
    def test_ok(self):
        for d in ("CREATE INDEX ON orders (status)", 'CREATE INDEX ON s."T" ("A", b DESC)',
                  "create index idx1 on t using gin (c gin_trgm_ops)",
                  "CREATE INDEX ON t (a) INCLUDE (b) WHERE b > 0", "CREATE INDEX ON t ((lower(c)))"):
            g.index_ddl(d)

    def test_bad(self):
        for d in ("CREATE INDEX ON t (a); DROP TABLE t", "DROP TABLE t", "CREATE TABLE x(a int)",
                  "CREATE INDEX ON t ((SELECT 1))", "CREATE INDEX ON t (a) WHERE a IN (SELECT 1)"):
            with self.assertRaises(g.SqlGuardError, msg=d):
                g.index_ddl(d)


class IdentDsnTest(unittest.TestCase):
    def test_quote(self):
        self.assertEqual(g.quote_ident("abc"), "abc")
        self.assertEqual(g.quote_ident("Region"), '"Region"')
        self.assertEqual(g.quote_ident("order"), '"order"')
        self.assertEqual(g.quote_ident('a"b'), '"a""b"')
        self.assertEqual(g.qualify("public", "t"), "t")
        self.assertEqual(g.qualify("Sales", "t"), '"Sales".t')

    def test_password(self):
        self.assertEqual(g.mask_dsn("host=x password=secret dbname=d"), "host=x password=**** dbname=d")
        self.assertEqual(g.mask_dsn("postgresql://u:secret@h/db"), "postgresql://u:****@h/db")
        dsn, pw = g.split_password("host=x password='a b\\'c' dbname=d")
        self.assertEqual(pw, "a b'c")
        self.assertNotIn("password", dsn)
        dsn, pw = g.split_password("postgresql://u:p%40ss@h:5432/db")
        self.assertEqual((dsn, pw), ("postgresql://u@h:5432/db", "p@ss"))


if __name__ == "__main__":
    unittest.main()
