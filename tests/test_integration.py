# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 naturalTuna — https://github.com/naturalTuna/pg_tune_ai
"""실제 PostgreSQL 통합 테스트.

  PGTUNE_TEST_DSN="host=/tmp port=5432 user=postgres dbname=postgres" python3 -m unittest tests.test_integration
  (없으면 건너뜀. pgtune_it 스키마를 만들고 끝나면 지움. hypopg 가 있으면 verify 테스트도 수행)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import analyzer  # noqa: E402
import index_check  # noqa: E402
import pgexec  # noqa: E402
import sqlguard  # noqa: E402
import tunelog  # noqa: E402
import verifier  # noqa: E402
from pg_tune_ai import parser as ai_parser  # noqa: E402
from pg_tune_ai import proposal_verifier as pv  # noqa: E402

DSN = os.environ.get("PGTUNE_TEST_DSN")


def q(sql, **v):
    return pgexec.run_psql(DSN, sql, v).strip()


@unittest.skipUnless(DSN, "PGTUNE_TEST_DSN 미설정")
class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        q("""
DROP SCHEMA IF EXISTS pgtune_it CASCADE;
CREATE SCHEMA pgtune_it;
SET search_path = pgtune_it;
CREATE TABLE orders (id int PRIMARY KEY, status varchar(10), amt int, "Region" int, note text);
INSERT INTO orders SELECT g, CASE WHEN g % 100 = 0 THEN 'A' ELSE 'B' END, g % 1000, g % 50, 'n' || g
  FROM generate_series(1, 200000) g;
CREATE TABLE victim (id int);
INSERT INTO victim SELECT generate_series(1, 10);
CREATE TABLE ic (a int, b int, c text);
CREATE INDEX ic_expr ON ic (a, lower(c));
CREATE INDEX ic_partial ON ic (a) WHERE b > 0;
CREATE INDEX ic_dup1 ON ic (b);
CREATE INDEX ic_dup2 ON ic (b);
CREATE INDEX ic_gin_like ON ic USING hash (b);
ANALYZE orders; ANALYZE victim;
""")
        cls.hypopg = q("SELECT count(*) FROM pg_available_extensions WHERE name='hypopg'") == "1"
        if cls.hypopg:
            q("CREATE EXTENSION IF NOT EXISTS hypopg")
        cls.dsn = DSN + " options='-c search_path=pgtune_it'"

    @classmethod
    def tearDownClass(cls):
        q("DROP SCHEMA IF EXISTS pgtune_it CASCADE")

    def victim_count(self):
        return int(q("SELECT count(*) FROM pgtune_it.victim"))

    # --- 데이터 보호 -------------------------------------------------------
    def test_analyze_cte_delete_not_committed(self):
        sql = "WITH d AS (DELETE FROM victim WHERE id <= 5 RETURNING *) SELECT count(*) FROM d"
        with self.assertRaises(RuntimeError) as cm:        # READ ONLY 트랜잭션 → 에러
            analyzer.explain_json(self.dsn, sql, analyze=True)
        self.assertIn("read-only", str(cm.exception))
        self.assertEqual(self.victim_count(), 10)

    def test_analyze_dml_rolled_back(self):
        root = analyzer.explain_json(self.dsn, "/* c */ DELETE FROM victim WHERE id <= 5", analyze=True)
        self.assertIn("Plan", root)
        self.assertEqual(self.victim_count(), 10)

    def test_multi_statement_rejected(self):
        with self.assertRaises(sqlguard.SqlGuardError):
            analyzer.explain_json(self.dsn, "SELECT 1; DROP TABLE victim", analyze=False)
        self.assertEqual(self.victim_count(), 10)

    def test_llm_rewrite_injection_blocked(self):
        raw = json.dumps({"proposals": [
            {"type": "rewrite", "sql": "SELECT * FROM orders WHERE status='A'; COMMIT; DROP TABLE victim",
             "rationale": "x"},
            {"type": "config", "set": {"work_mem": "64MB'; COMMIT; CREATE TABLE pwned(); --"},
             "rationale": "y"},
            {"type": "index", "ddl": "CREATE INDEX ON orders (status); DROP TABLE victim", "rationale": "z"},
            {"type": "config", "set": {"work_mem": "64MB"}, "rationale": "ok"},
            {"type": "rewrite", "sql": "SELECT id FROM orders WHERE status = 'A'", "rationale": "ok"}]})
        props, warns = ai_parser.parse(raw)
        self.assertEqual([p.type for p in props], ["config", "rewrite"])
        self.assertEqual(len([w for w in warns if "보안 검사 실패" in w]), 3)
        # 파서를 우회해서 직접 넘겨도 검증 단계에서 다시 막힘
        bad = [ai_parser.Proposal("rewrite", "x", sql="SELECT 1; COMMIT; DROP TABLE victim"),
               ai_parser.Proposal("config", "y", set={"work_mem": "1MB'; COMMIT; CREATE TABLE pwned(); --"})]
        res = pv.verify_all(self.dsn, "SELECT * FROM orders WHERE status='A'", bad + props)
        self.assertEqual([r.ok for r in res], [False, False, True, True])
        self.assertEqual(self.victim_count(), 10)
        self.assertEqual(q("SELECT to_regclass('pwned') IS NULL AND to_regclass('pgtune_it.pwned') IS NULL"), "t")

    def test_index_ddl_injection_blocked(self):
        with self.assertRaises(sqlguard.SqlGuardError):
            verifier.verify_candidate(self.dsn, "SELECT 1",
                                      "CREATE INDEX ON orders ((1)) $pgtune$); DROP TABLE victim; --")
        self.assertEqual(self.victim_count(), 10)

    # --- 정확도 -----------------------------------------------------------
    def test_varchar_candidate_column(self):
        root = analyzer.explain_json(self.dsn, "SELECT * FROM orders WHERE status = 'A'", analyze=True)
        res = analyzer.analyze_plan(root, self.dsn)
        ddls = [c["ddl"] for c in res["index_candidates"]]
        self.assertIn("CREATE INDEX ON pgtune_it.orders (status)", ddls)
        self.assertFalse(any("(text" in d for d in ddls))
        self.assertFalse(any(f["rule"] == "implicit_cast_on_column" for f in res["findings"]))

    def test_mixed_case_column_candidate(self):
        root = analyzer.explain_json(self.dsn, 'SELECT * FROM orders WHERE amt > 990 AND "Region" = 3',
                                     analyze=True)
        res = analyzer.analyze_plan(root, self.dsn)
        ddls = [c["ddl"] for c in res["index_candidates"]]
        self.assertIn('CREATE INDEX ON pgtune_it.orders ("Region", amt)', ddls)

    def test_hypopg_verify_quoted(self):
        if not self.hypopg:
            self.skipTest("hypopg 없음")
        r = verifier.verify_candidate(self.dsn, 'SELECT * FROM orders WHERE "Region" = 3 AND amt = 7',
                                      'CREATE INDEX ON pgtune_it.orders ("Region", amt)')
        self.assertTrue(r.adopted_by_planner)
        self.assertGreater(r.improvement_pct, 50)

    # --- index-check ------------------------------------------------------
    def test_index_check_duplicates(self):
        issues = [i for i in index_check.diagnose(DSN, 50) if i.table_name == "ic" and i.issue == "duplicate"]
        names = {i.index_name for i in issues}
        self.assertEqual(names, {"ic_dup1"})            # 완전 중복 1개만
        self.assertNotIn("ic_partial", names)            # partial 은 중복 아님
        self.assertNotIn("ic_expr", names)               # 표현식 인덱스

    # --- 비밀번호 노출 -----------------------------------------------------
    def test_password_masked_in_log_and_not_in_argv(self):
        d = tempfile.mkdtemp()
        old = os.getcwd()
        os.chdir(d)
        try:
            path = tunelog.write("analyze", "SELECT 1", "x", dsn="host=h password=topsecret", echo=False)
            with open(path, encoding="utf-8") as f:
                body = f.read()
            self.assertNotIn("topsecret", body)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        finally:
            os.chdir(old)
        clean, env = pgexec._env("host=h password=topsecret dbname=x")
        self.assertNotIn("topsecret", clean)
        self.assertEqual(env["PGPASSWORD"], "topsecret")

    def test_cli_tune_runs(self):
        out = subprocess.run([sys.executable, os.path.join(ROOT, "pgtune.py"), "--statement-timeout", "60s",
                              "tune", "--dsn", self.dsn, "--sql", "SELECT * FROM orders WHERE status = 'A'",
                              "--no-log", "--no-hint"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("추천", out.stdout)


if __name__ == "__main__":
    unittest.main()
