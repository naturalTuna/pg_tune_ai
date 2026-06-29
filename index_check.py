#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
index_check.py  —  인덱스 상태 점검 (카탈로그 기반)

기존 analyzer(플랜 분석)와 다른 갈래:
  특정 쿼리가 아니라 "DB 전체의 인덱스 상태"를 시스템 카탈로그로 점검한다.

점검 항목
--------
  1. 안 쓰는 인덱스    : idx_scan = 0 (제약용 PK/UNIQUE는 제외하고 표시)
  2. 거의 안 쓰는 인덱스: idx_scan 이 임계값 미만
  3. 중복 인덱스        : (a) 가 (a,b) 의 접두로 완전히 커버됨
  4. 비대한 인덱스      : 인덱스 크기가 테이블 대비 과도

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
import subprocess
import sys
from dataclasses import dataclass, asdict


@dataclass
class IndexIssue:
    issue: str            # unused / rarely_used / duplicate / bloated
    severity: str         # high / medium / low / info
    index_name: str
    table_name: str
    message: str
    evidence: dict
    suggestion: str


def run_sql_json(dsn: str, sql: str):
    """쿼리를 JSON으로 받아 파싱 (row_to_json 집계 사용)."""
    wrapped = f"SELECT coalesce(json_agg(t), '[]') FROM ({sql}) t;"
    proc = subprocess.run(
        ["psql", dsn, "-At", "-X", "-q", "-c", wrapped],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql 실패:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout.strip() or "[]")


# ---------------------------------------------------------------------------
# 카탈로그 조회
# ---------------------------------------------------------------------------
# 인덱스별 사용통계 + 크기 + 제약 여부 + 컬럼 목록
INDEX_QUERY = """
SELECT
    s.relname               AS table_name,
    s.indexrelname          AS index_name,
    s.idx_scan              AS idx_scan,
    pg_relation_size(s.indexrelid)  AS index_bytes,
    pg_relation_size(s.relid)       AS table_bytes,
    (i.indisprimary OR i.indisunique) AS is_constraint,
    i.indisvalid            AS is_valid,
    (
      SELECT string_agg(a.attname, ',' ORDER BY k.ord)
      FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
      WHERE k.attnum <> 0
    )                       AS columns
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
"""


def fetch_stats_reset(dsn: str) -> str:
    """통계 리셋 시각은 DB 단위 정보 → pg_stat_database 에서 가져온다."""
    try:
        rows = run_sql_json(
            dsn,
            "SELECT stats_reset::text AS stats_reset FROM pg_stat_database "
            "WHERE datname = current_database()",
        )
        return rows[0]["stats_reset"] if rows and rows[0]["stats_reset"] else "불명"
    except Exception:
        return "불명"


def fetch_indexes(dsn: str):
    return run_sql_json(dsn, INDEX_QUERY)


# ---------------------------------------------------------------------------
# 점검 규칙
# ---------------------------------------------------------------------------
def check_unused(idx: dict, stats_since: str = "불명") -> IndexIssue | None:
    if idx["idx_scan"] != 0 or idx["is_constraint"]:
        return None  # 사용됨 또는 제약용(PK/UNIQUE)은 제외
    mb = round(idx["index_bytes"] / 1024 / 1024, 1)
    return IndexIssue(
        issue="unused", severity="medium",
        index_name=idx["index_name"], table_name=idx["table_name"],
        message=f"한 번도 사용 안 됨 (idx_scan=0, {mb} MB 차지)",
        evidence={"idx_scan": 0, "index_mb": mb, "stats_since": stats_since,
                  "columns": idx["columns"]},
        suggestion="검토 후 삭제 고려. 단 가끔 쓰는(월말 배치 등) 인덱스이거나 "
                   f"통계 수집 시작({stats_since})이 최근이면 아직 판단 이르니 확인할 것.",
    )


def check_rarely_used(idx: dict, min_scans: int) -> IndexIssue | None:
    if idx["is_constraint"] or idx["idx_scan"] == 0:
        return None  # 0은 check_unused 담당
    if idx["idx_scan"] >= min_scans:
        return None
    mb = round(idx["index_bytes"] / 1024 / 1024, 1)
    return IndexIssue(
        issue="rarely_used", severity="low",
        index_name=idx["index_name"], table_name=idx["table_name"],
        message=f"거의 안 쓰임 (idx_scan={idx['idx_scan']}, 임계 {min_scans} 미만)",
        evidence={"idx_scan": idx["idx_scan"], "index_mb": mb,
                  "columns": idx["columns"]},
        suggestion="비용 대비 효용 검토. 쓰기 부담을 상쇄할 만큼 조회에 기여하는지 확인.",
    )


def check_bloated(idx: dict, ratio: float = 0.5) -> IndexIssue | None:
    tb, ib = idx["table_bytes"], idx["index_bytes"]
    if tb == 0 or ib < 10 * 1024 * 1024:  # 10MB 미만은 무시
        return None
    if ib < tb * ratio:
        return None
    return IndexIssue(
        issue="bloated", severity="low",
        index_name=idx["index_name"], table_name=idx["table_name"],
        message=f"인덱스가 테이블 대비 큼 ({round(ib/1024/1024,1)} MB / "
                f"테이블 {round(tb/1024/1024,1)} MB)",
        evidence={"index_mb": round(ib/1024/1024, 1),
                  "table_mb": round(tb/1024/1024, 1),
                  "ratio": round(ib/tb, 2)},
        suggestion="블로트 가능성. REINDEX 검토(운영 시 CONCURRENTLY). "
                   "또는 컬럼 수가 많은 인덱스인지 확인.",
    )


def check_duplicates(indexes: list) -> list:
    """같은 테이블에서 한 인덱스의 컬럼이 다른 인덱스의 접두(prefix)면 중복."""
    issues = []
    by_table: dict = {}
    for idx in indexes:
        by_table.setdefault(idx["table_name"], []).append(idx)

    for table, idxs in by_table.items():
        for a in idxs:
            if a["is_constraint"] or not a["columns"]:
                continue
            a_cols = a["columns"].split(",")
            for b in idxs:
                if a["index_name"] == b["index_name"] or not b["columns"]:
                    continue
                b_cols = b["columns"].split(",")
                # a 의 컬럼들이 b 의 앞부분과 정확히 일치하면 a 는 b 로 커버됨
                if len(a_cols) < len(b_cols) and b_cols[:len(a_cols)] == a_cols:
                    issues.append(IndexIssue(
                        issue="duplicate", severity="medium",
                        index_name=a["index_name"], table_name=table,
                        message=f"({a['columns']}) 는 ({b['columns']}) 에 의해 커버됨",
                        evidence={"redundant": a["columns"],
                                  "covered_by": b["columns"],
                                  "covering_index": b["index_name"]},
                        suggestion=f"{a['index_name']} 는 {b['index_name']} 로 대체 "
                                   "가능. 다른 쿼리가 단독으로 쓰지 않는지 확인 후 삭제 검토.",
                    ))
    return issues


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def diagnose(dsn: str, min_scans: int) -> list:
    indexes = fetch_indexes(dsn)
    stats_since = fetch_stats_reset(dsn)
    issues = []
    for idx in indexes:
        for fn in (lambda i: check_unused(i, stats_since),
                   lambda i: check_rarely_used(i, min_scans),
                   check_bloated):
            r = fn(idx)
            if r:
                issues.append(r)
    issues += check_duplicates(indexes)
    issues.sort(key=lambda x: SEVERITY_ORDER.get(x.severity, 9))
    return issues


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
SEV = {"high": "[!!]", "medium": "[! ]", "low": "[  ]", "info": "[i ]"}
LABEL = {"unused": "안 쓰는 인덱스", "rarely_used": "거의 안 쓰는 인덱스",
         "duplicate": "중복 인덱스", "bloated": "비대한 인덱스"}


def format_report(issues: list) -> str:
    if not issues:
        return "=== 인덱스 점검 ===\n  특이사항 없음. 인덱스 상태 양호."
    out = ["=== 인덱스 점검 ==="]
    for x in issues:
        out += [f"{SEV.get(x.severity,'[  ]')} {LABEL.get(x.issue,x.issue)} "
                f"@{x.table_name}.{x.index_name}",
                f"      {x.message}",
                f"      → {x.suggestion}"]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="인덱스 상태 점검 (카탈로그 기반)")
    ap.add_argument("--dsn", required=True, help='psql DSN, 예: "dbname=postgres"')
    ap.add_argument("--min-scans", type=int, default=50,
                    help="이 횟수 미만 사용은 '거의 안 씀'으로 표시 (기본 50)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    try:
        issues = diagnose(args.dsn, args.min_scans)
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    print(json.dumps([asdict(i) for i in issues], ensure_ascii=False, indent=2)
          if args.format == "json" else format_report(issues))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())