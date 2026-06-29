#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pgtune.py  —  통합 진입점 (규칙엔진 기반 PostgreSQL 튜닝 도구)

세 모듈을 하나의 명령으로 묶는다:
  analyzer.py      (쿼리 진단 + 인덱스 후보)
  verifier.py     (hypopg 후보 검증)
  index_check.py  (DB 전체 인덱스 점검)

서브커맨드
---------
  analyze       쿼리 하나를 진단 (느린 노드 + 인덱스 후보)
  verify        인덱스 후보를 hypopg로 검증
  index-check   DB 전체 인덱스 상태 점검 (안 쓰는/중복/비대)
  tune          ★ analyze + 후보 자동 검증을 한 번에 (검증된 추천만 제시)

사용 예
------
  python3 pgtune.py tune        --dsn "dbname=postgres" --sql "SELECT ..."
  python3 pgtune.py analyze     --dsn "dbname=postgres" --sql "SELECT ..."
  python3 pgtune.py analyze     --plan-file plan.json
  python3 pgtune.py verify      --dsn "dbname=postgres" --sql "SELECT ..." --index "CREATE INDEX ..."
  python3 pgtune.py index-check --dsn "dbname=postgres"

  # 모든 서브커맨드에 --format json 사용 가능
"""

from __future__ import annotations
import argparse
import json
import sys

import analyzer
import verifier
import index_check
import tunelog
from pg_tune_ai import orchestrator


# ---------------------------------------------------------------------------
# 로그 본문 구성 헬퍼 (raw EXPLAIN 동봉용)
# ---------------------------------------------------------------------------
def _base_explain_block(dsn: str | None, sql: str | None, root: dict) -> str:
    """analyze/tune 용: 기본 플랜을 텍스트(트리) EXPLAIN 으로.
    DB 접속 정보가 없으면(plan-file 모드) JSON 덤프로 대체한다."""
    header = "=== EXPLAIN (기본 플랜, 텍스트) ==="
    if dsn and sql:
        try:
            return header + "\n" + analyzer.get_plan_text(dsn, sql, analyze=False)
        except Exception as e:
            return (f"{header}\n(텍스트 EXPLAIN 수집 실패: {e} — JSON 으로 대체)\n"
                    + _raw_explain_json(root))
    return header + " (plan-file 모드 → JSON)\n" + _raw_explain_json(root)


def _raw_explain_json(root: dict) -> str:
    """텍스트 EXPLAIN 을 못 받을 때의 폴백: 원본 EXPLAIN(JSON) 한 덩어리."""
    return ("--- EXPLAIN (raw, FORMAT JSON) ---\n"
            + json.dumps(root, ensure_ascii=False, indent=2))


def _verify_explain_block(capture: dict) -> str:
    """verify/tune 용: 가상 인덱스 적용 전/후 텍스트 EXPLAIN (before/after)."""
    parts = []
    if capture.get("before_text"):
        parts += ["--- before (원본) ---", capture["before_text"].rstrip()]
    elif capture.get("before_plan") is not None:
        parts += ["--- before (원본, JSON) ---",
                  json.dumps(capture["before_plan"], ensure_ascii=False, indent=2)]
    if capture.get("after_text"):
        parts += ["--- after (가상 인덱스 적용) ---", capture["after_text"].rstrip()]
    elif capture.get("after_plan") is not None:
        parts += ["--- after (가상 인덱스 적용, JSON) ---",
                  json.dumps(capture["after_plan"], ensure_ascii=False, indent=2)]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# analyze : 쿼리 진단 (analyzer)
# ---------------------------------------------------------------------------
def cmd_analyze(args) -> int:
    if args.plan_file:
        root = analyzer.load_plan_file(args.plan_file)
    elif args.dsn and args.sql:
        root = analyzer.get_plan_from_db(args.dsn, args.sql, analyze=not args.no_analyze)
    else:
        print("오류: --plan-file 또는 (--dsn 과 --sql)이 필요합니다.", file=sys.stderr)
        return 2
    res = analyzer.analyze_plan(root, dsn=getattr(args, "dsn", None))
    report = analyzer.format_report(res)   # "실행 …ms / 계획 …ms / === 진단 === …"

    print(json.dumps(res, ensure_ascii=False, indent=2) if args.format == "json"
          else report)

    # 항상 로그(진단). --no-log 면 생략.
    # 순서: 기본 EXPLAIN(텍스트) → 진단 리포트
    if not getattr(args, "no_log", False):
        base = _base_explain_block(getattr(args, "dsn", None),
                                   getattr(args, "sql", None), root)
        log_content = base + "\n\n" + report
        tunelog.write("analyze", args.sql or args.plan_file, log_content,
                      dsn=getattr(args, "dsn", None), sql=getattr(args, "sql", None))
    return 0


# ---------------------------------------------------------------------------
# verify : 후보 검증 (verifier)
# ---------------------------------------------------------------------------
def cmd_verify(args) -> int:
    log = not getattr(args, "no_log", False)
    capture: dict | None = {} if log else None
    results = [verifier.verify_candidate(args.dsn, args.sql, args.index, capture=capture)]
    if args.format == "json":
        from dataclasses import asdict
        print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))
    else:
        print("=== 인덱스 후보 검증 (hypopg) ===")
        for r in results:
            print(verifier.format_result(r))

    # 항상 로그(검증). 순서: 검증 결과 → 검증때 돌아간 EXPLAIN(before/after, 텍스트)
    if log:
        lines = ["=== 인덱스 후보 검증 (hypopg) ==="]
        lines += [verifier.format_result(r) for r in results]
        log_content = "\n".join(lines)
        if capture:
            log_content += ("\n\n=== 검증 EXPLAIN (hypopg before/after) ===\n"
                            + _verify_explain_block(capture))
        tunelog.write("verify", args.sql, log_content, dsn=args.dsn, sql=args.sql)
    return 0


# ---------------------------------------------------------------------------
# index-check : 인덱스 점검 (index_check)
# ---------------------------------------------------------------------------
def cmd_index_check(args) -> int:
    issues = index_check.diagnose(args.dsn, args.min_scans)
    if args.format == "json":
        from dataclasses import asdict
        print(json.dumps([asdict(i) for i in issues], ensure_ascii=False, indent=2))
    else:
        print(index_check.format_report(issues))
    return 0


# ---------------------------------------------------------------------------
# tune : 진단 → 후보 도출 → 자동 검증 (전체 파이프라인 한 번에)
# ---------------------------------------------------------------------------
def _tune_text_report(res: dict, verified: list) -> str:
    """tune 사람용 통합 리포트 텍스트를 구성 (출력/로그 공용)."""
    lines = [analyzer.format_report(res), "", "=== 검증된 추천 (hypopg) ==="]
    if not verified:
        lines.append("  검증할 인덱스 후보가 없습니다. (인덱스로 개선 어려운 쿼리이거나, "
                     "설정/구조 튜닝이 필요한 경우)")
    else:
        adopted = [v for v in verified if v.adopted_by_planner and v.improvement_pct > 0]
        for v in verified:
            lines.append(verifier.format_result(v))
        if adopted:
            lines += ["", "→ 실제 적용 추천:"]
            for v in adopted:
                lines.append(f"   {v.index_ddl};   (cost {v.baseline_cost:,.0f} → "
                             f"{v.indexed_cost:,.0f}, {v.improvement_pct:.1f}% 감소)")
        else:
            lines += ["", "→ 플래너가 채택하는 후보가 없습니다. 인덱스보다 설정/구조 검토 필요."]
    return "\n".join(lines)


def cmd_tune(args) -> int:
    log = not getattr(args, "no_log", False)
    # 1) 진단
    root = analyzer.get_plan_from_db(args.dsn, args.sql, analyze=not args.no_analyze)
    res = analyzer.analyze_plan(root, dsn=getattr(args, "dsn", None))

    # 2) 후보를 각각 hypopg로 검증 (검증 EXPLAIN 은 capture 로 수집; 로그 시에만)
    verified, captures = [], []
    for cand in res["index_candidates"]:
        cap: dict | None = {} if log else None
        verified.append(verifier.verify_candidate(args.dsn, args.sql, cand["ddl"], capture=cap))
        captures.append(cap)

    # 사람용 통합 리포트는 항상 구성 (출력 또는 로그용) — 진단 + 검증된 추천
    report = _tune_text_report(res, verified)

    # 3) 출력
    if args.format == "json":
        from dataclasses import asdict
        print(json.dumps({
            "diagnosis": res,
            "verified_recommendations": [asdict(v) for v in verified],
        }, ensure_ascii=False, indent=2))
    else:
        print(report)

    # 항상 로그(튜닝).
    # 순서: 기본 EXPLAIN(텍스트) → 진단 → 검증 → 검증때 돌아간 EXPLAIN(후보별 before/after)
    if log:
        parts = [_base_explain_block(args.dsn, args.sql, root), "", report]
        verify_blocks = []
        for i, cap in enumerate(captures, 1):
            if cap:
                verify_blocks.append(f"--- 후보 {i} 검증 ---\n" + _verify_explain_block(cap))
        if verify_blocks:
            parts += ["", "=== 검증 EXPLAIN (후보별 hypopg before/after) ===", ""]
            parts.append("\n\n".join(verify_blocks))
        tunelog.write("tune", args.sql, "\n".join(parts), dsn=args.dsn, sql=args.sql)
    return 0


# ---------------------------------------------------------------------------
# ai : 진단 → LLM 제안 → 검증 (pg_tune_ai 파이프라인)
# ---------------------------------------------------------------------------
def cmd_ai(args) -> int:
    log = not getattr(args, "no_log", False)
    result = orchestrator.run(
        args.dsn, args.sql,
        model=args.model, host=args.host,
        analyze=not args.no_analyze,
        verify=not args.no_verify,
        timeout=args.timeout,
    )
    report = orchestrator.format_report(result)

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(report)

    # 항상 로그(ai). 순서: 진단+LLM 제안+검증 리포트 → 프롬프트에 들어간 EXPLAIN
    if log:
        parts = [report]
        if result.get("plan_text"):
            parts += ["", "=== 프롬프트에 사용된 EXPLAIN (텍스트) ===", result["plan_text"]]
        tunelog.write("ai", args.sql, "\n".join(parts), dsn=args.dsn, sql=args.sql)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pgtune", description="규칙엔진 기반 PostgreSQL 튜닝 도구 (통합 진입점)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_fmt(sp):
        sp.add_argument("--format", choices=["text", "json"], default="text")

    def add_nolog(sp):
        sp.add_argument("--no-log", action="store_true",
                        help="로그를 남기지 않음 (기본은 pgtune_log/ 에 자동 기록)")

    # analyze
    a = sub.add_parser("analyze", help="쿼리 진단 (느린 노드 + 인덱스 후보)")
    a.add_argument("--dsn")
    a.add_argument("--sql")
    a.add_argument("--plan-file")
    a.add_argument("--no-analyze", action="store_true")
    add_fmt(a)
    add_nolog(a)
    a.set_defaults(func=cmd_analyze)

    # verify
    v = sub.add_parser("verify", help="인덱스 후보를 hypopg로 검증")
    v.add_argument("--dsn", required=True)
    v.add_argument("--sql", required=True)
    v.add_argument("--index", required=True)
    add_fmt(v)
    add_nolog(v)
    v.set_defaults(func=cmd_verify)

    # index-check
    c = sub.add_parser("index-check", help="DB 전체 인덱스 점검")
    c.add_argument("--dsn", required=True)
    c.add_argument("--min-scans", type=int, default=50)
    add_fmt(c)
    c.set_defaults(func=cmd_index_check)

    # tune (통합 파이프라인)
    t = sub.add_parser("tune", help="진단 + 후보 자동 검증 (한 번에)")
    t.add_argument("--dsn", required=True)
    t.add_argument("--sql", required=True)
    t.add_argument("--no-analyze", action="store_true")
    add_fmt(t)
    add_nolog(t)
    t.set_defaults(func=cmd_tune)

    # ai (LLM 파이프라인: 진단 → LLM 제안 → 검증)
    ai = sub.add_parser("ai", help="진단 + LLM 제안 + 검증 (pg_tune_ai)")
    ai.add_argument("--dsn", required=True)
    ai.add_argument("--sql", required=True)
    ai.add_argument("--model", default=orchestrator.llm_client.DEFAULT_MODEL,
                    help=f"Ollama 모델 (기본 {orchestrator.llm_client.DEFAULT_MODEL})")
    ai.add_argument("--host", default=orchestrator.llm_client.DEFAULT_HOST,
                    help=f"Ollama 호스트 (기본 {orchestrator.llm_client.DEFAULT_HOST})")
    ai.add_argument("--no-analyze", action="store_true",
                    help="ANALYZE 없이 추정만 (빠름/안전)")
    ai.add_argument("--no-verify", action="store_true",
                    help="LLM 제안을 검증 없이 그대로 출력 (권장 안 함)")
    ai.add_argument("--timeout", type=int, default=180,
                    help="LLM 응답 대기 초 (기본 180)")
    add_fmt(ai)
    add_nolog(ai)
    ai.set_defaults(func=cmd_ai)

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())