#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 naturalTuna — https://github.com/naturalTuna/pg_tune_ai
"""
index_check.py  —  인덱스 상태 점검 (카탈로그 기반)

기존 analyzer(플랜 분석)와 다른 갈래:
  특정 쿼리가 아니라 "DB 전체의 인덱스 상태"를 시스템 카탈로그로 점검한다.

점검 항목
--------
  1. 안 쓰는 인덱스    : idx_scan = 0 (제약용 PK/UNIQUE는 제외하고 표시)
  2. 거의 안 쓰는 인덱스: idx_scan 이 임계값 미만
  3. 중복 인덱스        : 완전히 같은 정의 / (a) 가 (a,b) 의 접두로 커버됨
                          (인덱스 종류·opclass·정렬·partial 조건·INCLUDE·표현식까지 비교)
  4. 큰 인덱스          : 인덱스 크기가 테이블 대비 과도 (추정). --exact-bloat 면 pgstatindex 로 실측
  5. INVALID 인덱스     : CREATE INDEX CONCURRENTLY 실패 잔재

  ⚠ HA: idx_scan 은 "이 노드" 통계다. 스탠바이에서만 쓰는 조회용 인덱스는 여기서 0 으로 보인다.
     스탠바이가 있으면 각 노드에서 따로 돌려서 합쳐 볼 것 (리포트에 경고 표시).

  ⚠ "삭제하라"가 아니라 "검토 대상"으로 제시. idx_scan=0 은 통계 리셋·가끔쓰는
     인덱스일 수 있으므로 stats_since(통계 수집 시작 시점)를 함께 보여준다.

사용법
-----
  python3 index_check.py --dsn "dbname=postgres"
  python3 index_check.py --dsn "dbname=postgres" --format json
  python3 index_check.py --dsn "dbname=postgres" --min-scans 50
"""

from __future__ import annotations
import argparse
import json
import sys
from dataclasses import dataclass, asdict

import pgexec


@dataclass
class IndexIssue:
    issue: str            # unused / rarely_used / duplicate / large_index / bloated / invalid
    severity: str         # high / medium / low / info
    index_name: str
    table_name: str
    message: str
    evidence: dict
    suggestion: str


def run_sql_json(dsn: str, sql: str):
    """쿼리 결과를 JSON 배열로 (json_agg)."""
    out = pgexec.run_psql(dsn, f"SELECT coalesce(json_agg(t), '[]') FROM ({sql}) t;")
    return json.loads(out.strip() or "[]")


# ---------------------------------------------------------------------------
# 카탈로그 조회 — 키 컬럼(표현식 포함)과 INCLUDE 컬럼을 분리해서 가져온다
# ---------------------------------------------------------------------------
INDEX_QUERY = """
SELECT
    s.schemaname                     AS schema_name,
    s.relname                        AS table_name,
    s.indexrelname                   AS index_name,
    format('%I.%I', s.schemaname, s.indexrelname) AS qual_index,
    s.idx_scan                       AS idx_scan,
    pg_relation_size(s.indexrelid)   AS index_bytes,
    pg_relation_size(s.relid)        AS table_bytes,
    i.indisunique                    AS is_unique,
    i.indisprimary                   AS is_primary,
    EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid) AS backs_constraint,
    i.indisvalid                     AS is_valid,
    am.amname                        AS am,
    pg_get_expr(i.indpred, i.indrelid, true) AS predicate,
    ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
            FROM generate_series(1, i.indnkeyatts) k ORDER BY k) AS key_defs,
    ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true)
            FROM generate_series(i.indnkeyatts + 1, i.indnatts) k ORDER BY k) AS include_defs,
    array_to_string(i.indclass::oid[], ',')     AS opclasses,
    array_to_string(i.indoption::int2[], ',')   AS options,
    array_to_string(i.indcollation::oid[], ',') AS collations,
    pg_get_indexdef(i.indexrelid)    AS indexdef
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
JOIN pg_class ci ON ci.oid = s.indexrelid
JOIN pg_am am ON am.oid = ci.relam
"""


def fetch_stats_reset(dsn: str) -> str:
    try:
        rows = run_sql_json(dsn, "SELECT stats_reset::text AS stats_reset FROM pg_stat_database "
                                 "WHERE datname = current_database()")
        return rows[0]["stats_reset"] if rows and rows[0]["stats_reset"] else "불명"
    except Exception:
        return "불명"


def fetch_ha_state(dsn: str) -> dict:
    """{in_recovery, replicas} — 스탠바이가 있으면 idx_scan 해석 주의."""
    try:
        rows = run_sql_json(dsn, "SELECT pg_is_in_recovery() AS in_recovery, "
                                 "(SELECT count(*) FROM pg_stat_replication) AS replicas")
        return rows[0]
    except Exception:
        return {"in_recovery": False, "replicas": 0}


def fetch_indexes(dsn: str):
    rows = run_sql_json(dsn, INDEX_QUERY)
    for r in rows:
        r["is_constraint"] = bool(r["is_primary"] or r["is_unique"] or r["backs_constraint"])
        r["columns"] = ",".join(r["key_defs"]) + (" INCLUDE " + ",".join(r["include_defs"])
                                                  if r["include_defs"] else "")
    return rows


# ---------------------------------------------------------------------------
# 점검 규칙
# ---------------------------------------------------------------------------
def _mb(b) -> float:
    return round((b or 0) / 1024 / 1024, 1)


def check_unused(idx: dict, stats_since: str = "불명", ha: dict | None = None) -> IndexIssue | None:
    if idx["idx_scan"] != 0 or idx["is_constraint"]:
        return None
    ha = ha or {}
    note = ""
    if ha.get("replicas") or ha.get("in_recovery"):
        note = (" ⚠ HA 구성: 이 값은 이 노드 통계만 반영 — 스탠바이에서 조회용으로 쓰이는 인덱스일 수 있으니 "
                "모든 노드의 pg_stat_user_indexes 를 확인하기 전에는 삭제 금지.")
    return IndexIssue(
        "unused", "medium", idx["index_name"], idx["table_name"],
        f"한 번도 사용 안 됨 (idx_scan=0, {_mb(idx['index_bytes'])} MB 차지)",
        {"idx_scan": 0, "index_mb": _mb(idx["index_bytes"]), "stats_since": stats_since,
         "columns": idx["columns"], "ha": ha},
        "검토 후 삭제 고려. 가끔 쓰는(월말 배치 등) 인덱스이거나 "
        f"통계 수집 시작({stats_since})이 최근이면 아직 판단 이름." + note)


def check_rarely_used(idx: dict, min_scans: int) -> IndexIssue | None:
    if idx["is_constraint"] or idx["idx_scan"] == 0 or idx["idx_scan"] >= min_scans:
        return None
    return IndexIssue(
        "rarely_used", "low", idx["index_name"], idx["table_name"],
        f"거의 안 쓰임 (idx_scan={idx['idx_scan']}, 임계 {min_scans} 미만)",
        {"idx_scan": idx["idx_scan"], "index_mb": _mb(idx["index_bytes"]), "columns": idx["columns"]},
        "비용 대비 효용 검토. 쓰기 부담을 상쇄할 만큼 조회에 기여하는지 확인.")


def check_invalid(idx: dict) -> IndexIssue | None:
    if idx["is_valid"]:
        return None
    return IndexIssue(
        "invalid", "high", idx["index_name"], idx["table_name"],
        "INVALID 인덱스 (CREATE INDEX CONCURRENTLY 실패 잔재 등) — 조회에 안 쓰이면서 쓰기 비용만 발생",
        {"indexdef": idx["indexdef"]},
        f"DROP INDEX CONCURRENTLY {idx['qual_index']}; 후 다시 생성 (또는 REINDEX INDEX CONCURRENTLY).")


def check_large(idx: dict, ratio: float = 0.5) -> IndexIssue | None:
    tb, ib = idx["table_bytes"], idx["index_bytes"]
    if tb == 0 or ib < 10 * 1024 * 1024 or ib < tb * ratio:
        return None
    return IndexIssue(
        "large_index", "low", idx["index_name"], idx["table_name"],
        f"인덱스가 테이블 대비 큼 ({_mb(ib)} MB / 테이블 {_mb(tb)} MB) — 크기 비율 기준 추정",
        {"index_mb": _mb(ib), "table_mb": _mb(tb), "ratio": round(ib / tb, 2), "columns": idx["columns"]},
        "블로트인지 컬럼이 많은 인덱스인지 구분 필요. --exact-bloat (pgstattuple 의 pgstatindex, "
        "인덱스 전체를 읽으므로 한가한 시간에) 로 실측 후 REINDEX INDEX CONCURRENTLY 검토.")


def check_bloat_exact(dsn: str, idx: dict, min_density: float = 50.0) -> IndexIssue | None:
    if idx["am"] != "btree" or idx["index_bytes"] < 10 * 1024 * 1024:
        return None
    rows = pgexec.run_psql(dsn, "SELECT avg_leaf_density, leaf_fragmentation FROM pgstatindex(:'i');",
                           {"i": idx["qual_index"]}).strip()
    if not rows or "|" not in rows:
        return None
    dens, frag = (float(x) if x not in ("", "NaN") else 0.0 for x in rows.split("|"))
    if dens >= min_density:
        return None
    return IndexIssue(
        "bloated", "medium", idx["index_name"], idx["table_name"],
        f"리프 밀도 {dens:.0f}% (기준 {min_density:.0f}% 미만), 단편화 {frag:.0f}% — 실측(pgstatindex)",
        {"avg_leaf_density": dens, "leaf_fragmentation": frag, "index_mb": _mb(idx["index_bytes"])},
        f"REINDEX INDEX CONCURRENTLY {idx['qual_index']};")


def _sig(idx: dict) -> tuple:
    """키 위치별 (정의, opclass, 옵션, collation) — 같은 위치끼리 비교."""
    def arr(s):
        return [x for x in (s or "").split(",") if x != ""]
    ops, opt, col = arr(idx["opclasses"]), arr(idx["options"]), arr(idx["collations"])
    n = len(idx["key_defs"])
    return tuple((idx["key_defs"][k], ops[k] if k < len(ops) else "", opt[k] if k < len(opt) else "",
                  col[k] if k < len(col) else "") for k in range(n))


def check_duplicates(indexes: list) -> list:
    issues, reported = [], set()
    by_table: dict = {}
    for idx in indexes:
        if idx["is_valid"]:
            by_table.setdefault((idx["schema_name"], idx["table_name"]), []).append(idx)

    for (_, table), idxs in by_table.items():
        for a in idxs:
            sa = _sig(a)
            for b in idxs:
                if a is b or a["index_name"] in reported:
                    continue
                if a["am"] != b["am"] or (a["predicate"] or "") != (b["predicate"] or ""):
                    continue
                sb = _sig(b)
                same_keys = sa == sb
                prefix = (a["am"] == "btree" and len(sa) < len(sb) and sb[:len(sa)] == sa)
                if not (same_keys or prefix):
                    continue
                # a 의 INCLUDE 컬럼을 b 가 (키 또는 INCLUDE 로) 다 갖고 있어야 대체 가능
                b_cols = set(b["key_defs"]) | set(b["include_defs"])
                if not set(a["include_defs"]) <= b_cols:
                    continue
                if same_keys:
                    if set(a["include_defs"]) != set(b["include_defs"]):
                        # INCLUDE 만 다르면 더 작은 쪽이 중복
                        if not set(a["include_defs"]) < set(b["include_defs"]):
                            continue
                    elif a["is_constraint"] and not b["is_constraint"]:
                        continue            # 완전 동일 → 제약을 받치지 않는 쪽만 보고
                    elif a["is_constraint"] == b["is_constraint"] and a["index_name"] > b["index_name"]:
                        continue            # 둘 다 같은 조건이면 이름순 뒤쪽 하나만 보고
                if a["is_constraint"]:
                    continue                # PK/UNIQUE/제약용은 삭제 대상으로 제안하지 않음
                if same_keys and set(a["include_defs"]) == set(b["include_defs"]):
                    kind = "완전히 같은 정의"
                elif same_keys:
                    kind = "키가 같고 INCLUDE 컬럼만 더 적음"
                else:
                    kind = "접두 키로 커버됨"
                issues.append(IndexIssue(
                    "duplicate", "medium", a["index_name"], table,
                    f"({a['columns']}) 는 {b['index_name']} ({b['columns']}) 와 {kind}",
                    {"redundant": a["columns"], "covered_by": b["columns"], "covering_index": b["index_name"],
                     "kind": "exact" if same_keys else "prefix", "am": a["am"],
                     "predicate": a["predicate"]},
                    f"{a['index_name']} 는 {b['index_name']} 로 대체 가능. "
                    + ("" if same_keys else "다른 쿼리가 짧은 인덱스의 크기 이점에 의존하지 않는지 확인 후 ")
                    + f"DROP INDEX CONCURRENTLY {a['qual_index']}; 검토."))
                reported.add(a["index_name"])
    return issues


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def pgstattuple_available(dsn: str) -> bool:
    try:
        return pgexec.run_psql(dsn, "SELECT count(*) FROM pg_extension WHERE extname='pgstattuple';").strip() == "1"
    except Exception:
        return False


def diagnose(dsn: str, min_scans: int, exact_bloat: bool = False) -> list:
    indexes = fetch_indexes(dsn)
    stats_since = fetch_stats_reset(dsn)
    ha = fetch_ha_state(dsn)
    exact = exact_bloat and pgstattuple_available(dsn)
    issues = []
    for idx in indexes:
        for r in (check_invalid(idx), check_unused(idx, stats_since, ha), check_rarely_used(idx, min_scans)):
            if r:
                issues.append(r)
        r = check_bloat_exact(dsn, idx) if exact else check_large(idx)
        if r:
            issues.append(r)
    issues += check_duplicates(indexes)
    issues.sort(key=lambda x: SEVERITY_ORDER.get(x.severity, 9))
    return issues


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
SEV = {"high": "[!!]", "medium": "[! ]", "low": "[  ]", "info": "[i ]"}
LABEL = {"unused": "안 쓰는 인덱스", "rarely_used": "거의 안 쓰는 인덱스", "duplicate": "중복 인덱스",
         "large_index": "큰 인덱스(추정)", "bloated": "블로트 인덱스(실측)", "invalid": "INVALID 인덱스"}


def format_report(issues: list) -> str:
    if not issues:
        return "=== 인덱스 점검 ===\n  특이사항 없음. 인덱스 상태 양호."
    out = ["=== 인덱스 점검 ==="]
    for x in issues:
        out += [f"{SEV.get(x.severity, '[  ]')} {LABEL.get(x.issue, x.issue)} @{x.table_name}.{x.index_name}",
                f"      {x.message}", f"      → {x.suggestion}"]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="인덱스 상태 점검 (카탈로그 기반)")
    ap.add_argument("--dsn", required=True, help='psql DSN, 예: "dbname=postgres"')
    ap.add_argument("--min-scans", type=int, default=50,
                    help="이 횟수 미만 사용은 '거의 안 씀'으로 표시 (기본 50)")
    ap.add_argument("--exact-bloat", action="store_true",
                    help="pgstattuple 의 pgstatindex 로 블로트 실측 (인덱스 전체를 읽음)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()
    try:
        issues = diagnose(args.dsn, args.min_scans, args.exact_bloat)
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    print(json.dumps([asdict(i) for i in issues], ensure_ascii=False, indent=2)
          if args.format == "json" else format_report(issues))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
