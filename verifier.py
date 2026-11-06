#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verifier.py  —  인덱스 후보를 hypopg로 가상 검증 (③ 단계)

하는 일
-------
  analyzer가 도출한 인덱스 후보(CREATE INDEX ...)를 받아서:
    1. 원본 쿼리 EXPLAIN → baseline cost
    2. hypopg_create_index 로 가상 인덱스 생성 (실제 빌드 X)
    3. 다시 EXPLAIN → 플래너가 가상 인덱스를 채택? cost 얼마로?
    4. hypopg_reset 으로 정리
  → "cost 19009 → 47, 채택됨" 같은 검증된 비교 결과 반환

  ⚠ 가상 인덱스는 EXPLAIN(ANALYZE 없이)으로만 검증 가능 — cost(추정) 비교.

사용법
-----
  python3 verifier.py --dsn "dbname=postgres" \
      --sql "SELECT * FROM orders WHERE customer_id = 12345" \
      --index "CREATE INDEX ON orders (customer_id)"

  # pgtune analyze 와 연계: analyzer가 뽑은 후보를 그대로 검증
  python3 pgtune.py analyze --dsn "dbname=postgres" --sql "..." --format json \
      | python3 verifier.py --dsn "dbname=postgres" --sql "..." --from-pgtune
"""

from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class VerifyResult:
    index_ddl: str
    baseline_cost: float        # 원본 총 cost
    indexed_cost: float         # 가상 인덱스 적용 후 총 cost
    adopted_by_planner: bool    # 플래너가 가상 인덱스를 실제로 채택했나
    scan_before: str            # 예: "Parallel Seq Scan"
    scan_after: str             # 예: "Bitmap Index Scan"
    improvement_pct: float      # 개선율 %


# ---------------------------------------------------------------------------
# psql 실행 헬퍼
# ---------------------------------------------------------------------------
def run_sql(dsn: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", dsn, "-At", "-X", "-q"],
        input=sql, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql 실패:\n{proc.stderr.strip()}")
    return proc.stdout


def explain_json(dsn: str, sql: str) -> dict:
    """EXPLAIN (ANALYZE 없이, FORMAT JSON). 가상 인덱스 검증용."""
    out = run_sql(dsn, f"EXPLAIN (FORMAT JSON) {sql};")
    s, e = out.find("["), out.rfind("]")
    if s == -1 or e == -1:
        raise RuntimeError(f"플랜 JSON 파싱 실패:\n{out}")
    return json.loads(out[s:e + 1])[0]["Plan"]


def explain_text(dsn: str, sql: str) -> str:
    """plan-only 텍스트(트리) EXPLAIN — 로그/표시용 ('일반 EXPLAIN' 모습)."""
    return run_sql(dsn, f"EXPLAIN {sql};").rstrip("\n")


def hypopg_available(dsn: str) -> bool:
    """대상 DB 에 hypopg extension 이 설치(CREATE EXTENSION)돼 있는지."""
    out = run_sql(dsn, "SELECT count(*) FROM pg_extension WHERE extname = 'hypopg';")
    return out.strip() == "1"


class HypopgMissing(RuntimeError):
    """hypopg 미설치 — 가상 인덱스 검증 자체가 불가능한 상태."""


HYPOPG_MISSING_MSG = ("hypopg extension 미설치 — 가상 인덱스 검증 불가. "
                      "대상 DB 에서 CREATE EXTENSION hypopg; 후 다시 실행하세요.")


def top_cost(plan: dict) -> float:
    """루트 노드의 Total Cost = 쿼리 전체 추정 비용."""
    return float(plan.get("Total Cost", 0.0))


# 가상 인덱스가 채택되면 노드 이름에 hypopg oid 마커가 박힌다: <14556>btree_...
_HYPO = re.compile(r"<\d+>")


def uses_hypothetical(plan: dict) -> bool:
    """플랜 트리 어디든 가상 인덱스를 참조하는 노드가 있는지."""
    if _HYPO.search(plan.get("Index Name", "")):
        return True
    return any(uses_hypothetical(c) for c in plan.get("Plans", []))


def main_scan(plan: dict) -> str:
    """플랜에서 가장 바깥의 의미 있는 스캔/노드 타입을 뽑는다 (표시용)."""
    skip = {"Gather", "Gather Merge", "Result", "Append", "Sort", "Limit"}
    node = plan
    while node.get("Node Type") in skip and node.get("Plans"):
        node = node["Plans"][0]
    return node.get("Node Type", "Unknown")


# ---------------------------------------------------------------------------
# 핵심: 후보 하나를 검증
# ---------------------------------------------------------------------------
def verify_candidate(dsn: str, sql: str, index_ddl: str,
                     capture: Optional[dict] = None) -> VerifyResult:
    # capture(dict)를 넘기면 before/after 원본 플랜을 담아준다 (로그 기록용).
    # 0) hypopg 확인 — 없으면 hypopg_create_index 가 실패한 채로 EXPLAIN 만 돌아
    #    "미채택(효과 없음)" 으로 잘못 판정되므로 먼저 막는다.
    if not hypopg_available(dsn):
        raise HypopgMissing(HYPOPG_MISSING_MSG)
    # 1) 원본
    before = explain_json(dsn, sql)
    base_cost = top_cost(before)
    scan_before = main_scan(before)
    # 로그용 텍스트 EXPLAIN(원본)은 capture 요청 시에만 추가로 받아온다
    before_text = explain_text(dsn, sql) if capture is not None else ""

    # 2) 가상 인덱스 생성 + 3) 재측정  (같은 세션이어야 가상인덱스가 유지됨)
    #    같은 세션에서 텍스트 EXPLAIN 과 JSON EXPLAIN 을 둘 다 받아온다.
    #    (after 텍스트는 가상 인덱스가 살아있는 이 세션 안에서만 뽑을 수 있음)
    dollar = f"$pgtune${index_ddl}$pgtune$"
    M_TXT, M_JSON = "__PGT_AFTER_TXT__", "__PGT_AFTER_JSON__"
    script = (
        "\\set ON_ERROR_STOP on\n"          # 가상 인덱스 생성 실패 시 즉시 오류로 처리
        f"SELECT hypopg_create_index({dollar});\n"
        f"SELECT '{M_TXT}';\n"
        f"EXPLAIN {sql};\n"
        f"SELECT '{M_JSON}';\n"
        f"EXPLAIN (FORMAT JSON) {sql};\n"
        f"SELECT hypopg_reset();\n"
    )
    out = run_sql(dsn, script)
    i_txt, i_json = out.rfind(M_TXT), out.rfind(M_JSON)
    if i_json == -1:
        raise RuntimeError(f"가상 인덱스 EXPLAIN 파싱 실패:\n{out}")
    json_part = out[i_json + len(M_JSON):]
    s, e = json_part.find("["), json_part.rfind("]")
    if s == -1 or e == -1:
        raise RuntimeError(f"가상 인덱스 EXPLAIN 파싱 실패:\n{out}")
    after = json.loads(json_part[s:e + 1])[0]["Plan"]
    after_text = (out[i_txt + len(M_TXT):i_json].strip("\n")
                  if (capture is not None and i_txt != -1) else "")

    indexed_cost = top_cost(after)
    adopted = uses_hypothetical(after)
    scan_after = main_scan(after)
    improvement = (1 - indexed_cost / base_cost) * 100 if base_cost else 0.0

    if capture is not None:
        capture["before_plan"] = before
        capture["after_plan"] = after
        capture["before_text"] = before_text
        capture["after_text"] = after_text

    return VerifyResult(
        index_ddl=index_ddl, baseline_cost=base_cost, indexed_cost=indexed_cost,
        adopted_by_planner=adopted, scan_before=scan_before, scan_after=scan_after,
        improvement_pct=round(improvement, 1),
    )


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
def format_result(r: VerifyResult) -> str:
    mark = "✅ 채택됨" if r.adopted_by_planner else "❌ 미채택 (효과 없음)"
    delta = (f"{r.improvement_pct:.1f}% 감소" if r.improvement_pct >= 0
             else f"{-r.improvement_pct:.1f}% 증가")
    lines = [
        f"후보: {r.index_ddl}",
        f"  플래너 판정: {mark}",
        f"  스캔 변화 : {r.scan_before}  →  {r.scan_after}",
        f"  cost      : {r.baseline_cost:,.0f}  →  {r.indexed_cost:,.0f}  ({delta})",
    ]
    if not r.adopted_by_planner:
        lines.append("  ※ 플래너가 가상 인덱스를 쓰지 않음 → 이 인덱스는 만들 가치 낮음.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="hypopg 기반 인덱스 후보 검증 (③)")
    ap.add_argument("--dsn", required=True, help='psql DSN, 예: "dbname=postgres"')
    ap.add_argument("--sql", required=True, help="검증 대상 쿼리")
    ap.add_argument("--index", help="검증할 CREATE INDEX 문 (단일 후보)")
    ap.add_argument("--from-pgtune", action="store_true",
                    help="stdin으로 analyze --format json 출력을 받아 후보 자동 추출")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    # 검증할 후보 목록 수집
    candidates = []
    if args.index:
        candidates.append(args.index)
    if args.from_pgtune:
        data = json.load(sys.stdin)
        candidates += [c["ddl"] for c in data.get("index_candidates", [])]
    if not candidates:
        ap.error("--index 또는 --from-pgtune 으로 후보를 지정하세요.")
        return 2

    try:
        results = [verify_candidate(args.dsn, args.sql, ddl) for ddl in candidates]
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))
    else:
        print("=== 인덱스 후보 검증 (hypopg) ===")
        for r in results:
            print(format_result(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())