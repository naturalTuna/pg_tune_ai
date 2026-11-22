#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pgtune.py  —  규칙 기반 PostgreSQL 쿼리 튜닝 (AI 없음, 표준 라이브러리 + psql 만 사용)

  tune         ★ 전체: 진단 → 테이블 통계 → 인덱스 후보(hypopg) → 실행계획 대안(pg_hint_plan)
                       → 설정 실험(work_mem / jit) → 우선순위 추천
  analyze      진단만 (규칙 + 핫스팟 + 후보 목록)
  verify       인덱스 1개를 hypopg 로 검증
  hint         힌트 1개를 pg_hint_plan 으로 실험 (예: --hint "HashJoin(a b)")
  stats        쿼리에 쓰인 테이블 통계 / vacuum 상태
  index-check  DB 전체 인덱스 점검 (미사용 / 중복 / 비대)

  예)
    python3 pgtune.py tune --dsn "host=... dbname=app user=postgres" --sql "SELECT ..."
    python3 pgtune.py tune ... --analyze        # 실제 실행해서 실측 비교 (DML 은 ROLLBACK)
    python3 pgtune.py hint ... --hint "IndexScan(o)"

  필요 extension (대상 DB):
    hypopg       인덱스 가상 검증   CREATE EXTENSION hypopg;
    pg_hint_plan 실행계획 대안 실험 (LOAD 만, CREATE EXTENSION 불필요, superuser 계정)
"""

from __future__ import annotations
import argparse
import json
import sys
from dataclasses import asdict

import analyzer
import verifier
import hint_verifier
import stats_check
import index_check
import tunelog
import pgexec
from pg_tune_ai import orchestrator


# ---------------------------------------------------------------------------
def _base_text(dsn, sql) -> str:
    try:
        return "=== EXPLAIN (기본 플랜) ===\n" + analyzer.get_plan_text(dsn, sql, analyze=False)
    except Exception as e:
        return f"=== EXPLAIN (기본 플랜) ===\n(수집 실패: {e})"


def _verify_indexes(dsn, sql, cands):
    verified, failed = [], []
    for c in cands:
        try:
            verified.append(verifier.verify_candidate(dsn, sql, c["ddl"]))
        except verifier.HypopgMissing as e:
            failed = [(x["ddl"], str(e)) for x in cands[len(verified):]]
            break
        except RuntimeError as e:
            failed.append((c["ddl"], str(e).splitlines()[-1] if str(e) else "오류"))
    return verified, failed


def _summary(res, stats, verified, exps) -> list:
    """우선순위 추천: 근본 원인(인덱스/쿼리/통계) → 설정 → 힌트(최후 수단)."""
    recs = []
    for v in sorted([v for v in verified if v.adopted_by_planner and v.improvement_pct > 5],
                    key=lambda v: -v.improvement_pct):
        recs.append(("인덱스", f"{v.index_ddl};  (cost {v.improvement_pct:.0f}% 감소, "
                                f"{v.scan_before} → {v.scan_after})"))
    rewrite = {"function_on_column", "implicit_cast_on_column", "like_leading_wildcard",
               "correlated_subquery", "or_condition_seq_scan"}
    for f in res["findings"]:
        if f["rule"] in rewrite:
            recs.append(("쿼리 수정", f"{f['message']} → {f['suggestion']}"))
    seen = set()
    for s in stats:
        for a in s["actions"]:
            if a not in seen:
                seen.add(a)
                recs.append(("통계", f"{a}  ({'; '.join(s['issues'])})"))
    if any(f["rule"] == "bad_estimate" for f in res["findings"]) and not seen:
        recs.append(("통계", "추정 행수 오차가 큼 — 조건 컬럼들 간 상관관계면 "
                            "CREATE STATISTICS ... (dependencies, ndistinct) ON col1, col2 FROM tbl;"))
    for r in exps.get("results", []):
        if r.get("kind") == "setting" and str(r.get("verdict", "")).startswith("✅"):
            recs.append(("설정", f"{r['label']}  — {r['verdict']}  (세션 / 해당 쿼리 단위 적용 권장)"))
    for r in exps.get("results", []):
        if r.get("kind") == "hint" and str(r.get("verdict", "")).startswith(("✅", "추정 비용 감소")):
            recs.append(("힌트(최후 수단)",
                         f"/*+ {r['label']} */ — {r['verdict']}. 위의 인덱스/통계 조치로 해결 안 될 때만 "
                         f"쿼리 앞에 주석으로 적용 (pg_hint_plan 이 shared_preload_libraries 에 있어야 함)"))
    return recs


def _tune_report(res, stats, verified, failed, exps, analyze) -> str:
    L = [analyzer.format_report(res), "", stats_check.format_report(stats), "",
         "=== 인덱스 후보 검증 (hypopg) ==="]
    if failed:
        for ddl, why in failed:
            L += [f"후보: {ddl}", f"  ⚠ 검증 불가: {why}"]
    if verified:
        L += [verifier.format_result(v) for v in verified]
    elif not failed:
        L.append("  검증할 인덱스 후보 없음")
    L += ["", hint_verifier.format_results(exps, analyze), "", "=== 추천 (우선순위) ==="]
    recs = _summary(res, stats, verified, exps)
    if not recs:
        L.append("  뚜렷한 개선 항목 없음 — 이미 적절한 플랜이거나, 데이터량/하드웨어 문제일 수 있음.")
    for i, (cat, txt) in enumerate(recs, 1):
        L.append(f"  {i}. [{cat}] {txt}")
    if not analyze:
        L += ["", "※ 추정 플랜 기준입니다. 실제 실행 시간으로 비교하려면 --analyze (쿼리가 실제 실행됨)."]
    return "\n".join(L)


# ---------------------------------------------------------------------------
def cmd_tune(a) -> int:
    analyze = a.analyze
    if analyze:
        # 첫 실행은 캐시가 비어 느릴 수 있음 → 1회 예열 후 기준 측정 (실험들과 같은 조건)
        analyzer.explain_json(a.dsn, a.sql, True)
    root = analyzer.explain_json(a.dsn, a.sql, analyze)
    res = analyzer.analyze_plan(root, dsn=a.dsn)
    stats = stats_check.check(a.dsn, res["relations"]) if not a.no_stats else []
    verified, failed = ([], []) if a.no_hypo else _verify_indexes(a.dsn, a.sql, res["index_candidates"])
    exps = hint_verifier.run_all(a.dsn, a.sql, res, root, analyze, use_hint=not a.no_hint)
    report = _tune_report(res, stats, verified, failed, exps, analyze)
    if a.format == "json":
        print(json.dumps({"diagnosis": res, "stats": stats,
                          "index_verified": [asdict(v) for v in verified],
                          "index_failed": failed, "experiments": exps},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(report)
    if not a.no_log:
        tunelog.write("tune", a.sql, _base_text(a.dsn, a.sql) + "\n\n" + report, dsn=a.dsn, sql=a.sql)
    return 0


def cmd_analyze(a) -> int:
    if a.plan_file:
        root = analyzer.load_plan_file(a.plan_file)
    elif a.dsn and a.sql:
        root = analyzer.explain_json(a.dsn, a.sql, a.analyze)
    else:
        print("오류: --plan-file 또는 (--dsn 과 --sql) 이 필요합니다.", file=sys.stderr)
        return 2
    res = analyzer.analyze_plan(root, dsn=a.dsn)
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.format == "json" else analyzer.format_report(res))
    return 0


def cmd_verify(a) -> int:
    r = verifier.verify_candidate(a.dsn, a.sql, a.index)
    print(json.dumps(asdict(r), ensure_ascii=False, indent=2) if a.format == "json"
          else "=== 인덱스 후보 검증 (hypopg) ===\n" + verifier.format_result(r))
    return 0


def cmd_hint(a) -> int:
    ok, msg = hint_verifier.hint_available(a.dsn)
    if not ok:
        print(f"pg_hint_plan 사용 불가: {msg}", file=sys.stderr)
        return 1
    base = analyzer.explain_json(a.dsn, a.sql, a.analyze)
    r = hint_verifier.run_hint(a.dsn, a.sql, {"hint": a.hint, "target": "사용자 지정 힌트",
                                               "expect": a.expect or ""}, base, a.analyze)
    out = {"hint_available": True, "results": [asdict(r)]}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str) if a.format == "json"
          else hint_verifier.format_results(out, a.analyze))
    return 0


def cmd_stats(a) -> int:
    root = analyzer.explain_json(a.dsn, a.sql, False)
    rels = sorted({analyzer.qual_rel(n) for n in analyzer.walk(root["Plan"]) if analyzer.qual_rel(n)})
    st = stats_check.check(a.dsn, rels)
    print(json.dumps(st, ensure_ascii=False, indent=2) if a.format == "json" else stats_check.format_report(st))
    return 0


def cmd_index_check(a) -> int:
    issues = index_check.diagnose(a.dsn, a.min_scans, a.exact_bloat)
    print(json.dumps([asdict(i) for i in issues], ensure_ascii=False, indent=2) if a.format == "json"
          else index_check.format_report(issues))
    return 0


# ---------------------------------------------------------------------------
# ai : 진단 → 로컬 LLM(Ollama) 제안 → 검증 (선택 기능)
# ---------------------------------------------------------------------------
def cmd_ai(args) -> int:
    log = not getattr(args, "no_log", False)
    result = orchestrator.run(
        args.dsn, args.sql,
        model=args.model, host=args.host,
        analyze=args.analyze,
        verify=not args.no_verify,
        timeout=args.timeout,
    )
    report = orchestrator.format_report(result)

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(report)

    # 항상 로그(ai). 순서: 진단+LLM 제안+검증 리포트 → 프롬프트 EXPLAIN → 제안별 before/after
    if log:
        parts = [report]
        if result.get("plan_text"):
            parts += ["", "=== 프롬프트에 사용된 EXPLAIN (원본 플랜) ===", result["plan_text"]]
        # 검증된 제안별 적용 전/후 EXPLAIN
        ver = result.get("verified", [])
        blocks = []
        for i, v in enumerate(ver, 1):
            eb, ea = v.get("explain_before", ""), v.get("explain_after", "")
            if not eb and not ea:
                continue
            b = [f"--- 제안 {i} [{v.get('type')}] {v.get('detail','')} ---"]
            if eb:
                b += ["[before]", eb]
            if ea:
                b += ["[after]", ea]
            blocks.append("\n".join(b))
        if blocks:
            parts += ["", "=== 제안별 적용 전/후 EXPLAIN ===", ""]
            parts.append("\n\n".join(blocks))
        tunelog.write("ai", args.sql, "\n".join(parts), dsn=args.dsn, sql=args.sql)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="pgtune", description="규칙 기반 PostgreSQL 쿼리 튜닝")
    p.add_argument("--statement-timeout", help="모든 쿼리 statement_timeout (기본 5min, 예: 30s)")
    p.add_argument("--lock-timeout", help="lock_timeout (기본 5s)")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, need_sql=True, need_dsn=True):
        sp.add_argument("--dsn", required=need_dsn)
        if need_sql:
            sp.add_argument("--sql", required=True)
        sp.add_argument("--format", choices=["text", "json"], default="text")

    def analyze_opt(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--analyze", action="store_true", help="실제 실행해서 실측 비교 (DML 은 ROLLBACK)")
        g.add_argument("--no-analyze", action="store_false", dest="analyze", help="추정 플랜만 (기본)")

    t = sub.add_parser("tune", help="전체 튜닝 (진단 + 통계 + hypopg + pg_hint_plan + 설정 실험)")
    common(t); analyze_opt(t)
    t.add_argument("--no-hint", action="store_true", help="pg_hint_plan 실험 생략")
    t.add_argument("--no-hypo", action="store_true", help="hypopg 인덱스 검증 생략")
    t.add_argument("--no-stats", action="store_true", help="테이블 통계 점검 생략")
    t.add_argument("--no-log", action="store_true")
    t.set_defaults(func=cmd_tune)

    an = sub.add_parser("analyze", help="진단만")
    an.add_argument("--dsn"); an.add_argument("--sql"); an.add_argument("--plan-file")
    an.add_argument("--format", choices=["text", "json"], default="text"); analyze_opt(an)
    an.set_defaults(func=cmd_analyze)

    v = sub.add_parser("verify", help="인덱스 1개 hypopg 검증")
    common(v); v.add_argument("--index", required=True); v.set_defaults(func=cmd_verify)

    h = sub.add_parser("hint", help="힌트 1개 pg_hint_plan 실험")
    common(h); analyze_opt(h)
    h.add_argument("--hint", required=True, help='예: "HashJoin(o c)", "IndexScan(o)", "Leading(c o)"')
    h.add_argument("--expect", help="적용 확인용 노드 타입 (예: \"Hash Join\")")
    h.set_defaults(func=cmd_hint)

    s = sub.add_parser("stats", help="쿼리 테이블 통계 / vacuum 상태")
    common(s); s.set_defaults(func=cmd_stats)

    c = sub.add_parser("index-check", help="DB 전체 인덱스 점검")
    common(c, need_sql=False); c.add_argument("--min-scans", type=int, default=50)
    c.add_argument("--exact-bloat", action="store_true", help="pgstatindex 로 블로트 실측 (인덱스 전체 읽음)")
    c.set_defaults(func=cmd_index_check)
    ai = sub.add_parser("ai", help="진단 + 로컬 LLM(Ollama) 제안 + 검증 (선택 기능)")
    ai.add_argument("--dsn", required=True)
    ai.add_argument("--sql", required=True)
    ai.add_argument("--model", default=orchestrator.llm_client.DEFAULT_MODEL)
    ai.add_argument("--host", default=orchestrator.llm_client.DEFAULT_HOST)
    ai.add_argument("--no-verify", action="store_true", help="LLM 제안을 검증 없이 출력 (권장 안 함)")
    ai.add_argument("--timeout", type=int, default=180, help="LLM 응답 대기 초")
    ai.add_argument("--format", choices=["text", "json"], default="text")
    ai.add_argument("--no-log", action="store_true")
    analyze_opt(ai)
    ai.set_defaults(func=cmd_ai)
    return p


def main() -> int:
    args = build_parser().parse_args()
    pgexec.set_timeouts(args.statement_timeout, args.lock_timeout)
    try:
        return args.func(args)
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
