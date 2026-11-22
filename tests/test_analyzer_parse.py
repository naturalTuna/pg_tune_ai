# -*- coding: utf-8 -*-
"""analyzer 조건식 파싱 / 규칙 단위 테스트 (DB 불필요)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyzer as a  # noqa: E402


class FilterParseTest(unittest.TestCase):
    def test_varchar_cast_not_column_text(self):
        # 예전 버전은 여기서 컬럼을 'text' 로 뽑아 CREATE INDEX ON t (text) 를 만들었음
        self.assertEqual(a.extract_filter_columns("((status)::text = 'A'::text)"), ["status"])

    def test_quoted_and_mixed_case(self):
        self.assertEqual(a.extract_filter_columns('((amt > 5) AND ("Region" = 3))'), ["amt", "Region"])
        self.assertEqual(a.equality_columns('((amt > 5) AND ("Region" = 3))'), ["Region"])

    def test_any_is_equality(self):
        self.assertEqual(a.equality_columns("(o.customer_id = ANY ('{1,2}'::integer[]))"), ["customer_id"])

    def test_join_cond_not_equality(self):
        self.assertEqual(a.equality_columns("(o.cust_id = c.id)"), [])

    def test_multiword_type_cast(self):
        f = "((created_at)::timestamp without time zone >= '2025-01-01 00:00:00'::timestamp without time zone)"
        self.assertEqual(a.extract_filter_columns(f), ["created_at"])

    def test_literal_content_ignored(self):
        self.assertEqual(a.extract_filter_columns("((note)::text = 'x = 1 AND y > 2'::text)"), ["note"])

    def test_sort_keys(self):
        self.assertEqual(a.sort_key_columns(['o."CreatedAt" DESC', "o.id"], "o"), ["CreatedAt DESC", "id"])
        self.assertEqual(a.sort_key_columns(["lower(o.name)"], "o"), [])

    def test_candidate_ddl_quoted(self):
        f = a.Finding("seq_scan_inefficient", "medium", "Seq Scan", '"Sales".orders', "", {
            "filter": '(("Region" = 3) AND (amt > 5))', "removed": 1000}, "")
        c = a.derive_index_candidates([f])[0]
        self.assertEqual(c.ddl, 'CREATE INDEX ON "Sales".orders ("Region", amt)')

    def test_validate_candidates_drops_unknown_columns(self):
        c = a.IndexCandidate("orders", ["text", "status"], "r", "CREATE INDEX ON orders (text, status)")
        out = a.validate_candidates([c], {"orders": {"status": "character varying(10)"}})
        self.assertEqual(out[0].ddl, "CREATE INDEX ON orders (status)")


class CandidateExtraTest(unittest.TestCase):
    def test_expression_index_candidate(self):
        f = a.Finding("function_on_column", "medium", "Seq Scan", "orders", "", {
            "function": "date", "column": "created_at", "filter": "x"}, "")
        c = a.derive_index_candidates([f])
        self.assertEqual([x.ddl for x in c], ["CREATE INDEX ON orders ((date(created_at)))"])
        self.assertEqual(a.validate_candidates(c, {"orders": {"id": "integer"}})[0].ddl,
                         "CREATE INDEX ON orders ((date(created_at)))")

    def test_recommended_indexes_dedupe_prefix(self):
        import pgtune
        from verifier import VerifyResult
        def v(ddl, pct):
            return VerifyResult(ddl, 100.0, 100.0 * (1 - pct / 100), True, "Seq Scan", "Index Scan", pct)
        rec = pgtune.recommended_indexes([v('CREATE INDEX ON orders ("Region")', 99.0),
                                          v('CREATE INDEX ON orders ("Region", amt)', 99.4),
                                          v("CREATE INDEX ON customers (grade)", 80.0)])
        self.assertEqual([r.index_ddl for r in rec],
                         ['CREATE INDEX ON orders ("Region", amt)', "CREATE INDEX ON customers (grade)"])


class RuleTest(unittest.TestCase):
    def test_function_on_column_rhs_not_flagged(self):
        node = {"Node Type": "Seq Scan", "Relation Name": "t", "Filter": "((name)::text = upper('abc'::text))"}
        self.assertIsNone(a.rule_function_on_column(node, None, {}))

    def test_function_on_column_lhs_flagged(self):
        node = {"Node Type": "Seq Scan", "Relation Name": "t",
                "Filter": "(date(created_at) = '2025-01-01'::date)"}
        f = a.rule_function_on_column(node, None, {})
        self.assertEqual((f.evidence["function"], f.evidence["column"]), ("date", "created_at"))

    def test_varchar_cast_not_implicit_cast_finding(self):
        node = {"Node Type": "Seq Scan", "Relation Name": "t", "Schema": "public",
                "Filter": "((status)::text = 'A'::text)"}
        ctx = {"columns": {"t": {"status": "character varying(10)"}}}
        self.assertIsNone(a.rule_implicit_cast(node, None, ctx))
        ctx = {"columns": {"t": {"status": "integer"}}}
        self.assertIsNotNone(a.rule_implicit_cast(node, None, ctx))

    def test_hint_injection_rejected(self):
        with self.assertRaises(Exception):
            a.explain_script("SELECT 1", False, "", hint="SeqScan(t) */ ; DROP TABLE t; /*")


if __name__ == "__main__":
    unittest.main()
