#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orchestrator.py — pg_tune_ai 전체 파이프라인 실행

흐름:
  analyzer(진단)         → findings + 실행계획
    → prompt(조립)        → LLM 프롬프트
    → llm_client(호출)    → LLM 원시 응답(JSON 문자열)
    → parser(구조화)      → proposals
    → proposal_verifier   → 검증된 proposals (hypopg/EXPLAIN)
    → 결과(dict)

설계 원칙
  - LLM 은 DB 에 접근하지 않는다 (prompt/llm_client/parser 단계는 텍스트만).
  - DB 접근은 진단(analyzer)과 검증(proposal_verifier)에서만 일어난다.
  - LLM 제안은 반드시 검증을 거친 뒤 사용자에게 제시된다.
"""

from __future__ import annotations
import json
from dataclasses import asdict

import analyzer
from pg_tune_ai import prompt as ai_prompt
from pg_tune_ai import llm_client
from pg_tune_ai import parser as ai_parser
from pg_tune_ai import proposal_verifier


def run(dsn: str, sql: str, *,
        model: str = llm_client.DEFAULT_MODEL,
        host: str = llm_client.DEFAULT_HOST,
        analyze: bool = True,
        verify: bool = True,
        timeout: int = 180) -> dict:
    """파이프라인 1회 실행. 결과를 dict 로 반환.

    반환 구조:
    {
      "diagnosis":   analyzer.analyze_plan 결과(dict),
      "plan_text":   기본 EXPLAIN 텍스트(프롬프트에 들어간 것),
      "proposals":   parser 가 만든 proposals (검증 전, dict 리스트),
      "warnings":    parser 경고 리스트,
      "verified":    proposal_verifier 결과 (verify=True 일 때, dict 리스트),
      "llm":         {"model":..., "raw_len":...} 메타,
    }
    """
    # 1) 진단 (DB 접근)
    root = analyzer.get_plan_from_db(dsn, sql, analyze=analyze)
    diagnosis = analyzer.analyze_plan(root, dsn=dsn)

    # 2) 프롬프트용 실행계획 텍스트 (사람이 읽는 EXPLAIN 트리; ANALYZE 없이 가볍게)
    try:
        plan_text = analyzer.get_plan_text(dsn, sql, analyze=False)
    except Exception:
        plan_text = ""

    # 3) 프롬프트 조립 (텍스트만 — DB 접근 없음). 핫스팟으로 진짜 범인 식별 유도.
    p = ai_prompt.build(sql, plan_text,
                        diagnosis.get("findings", []),
                        hotspots=diagnosis.get("hotspots", []))

    # 4) LLM 호출 (텍스트 in / 텍스트 out — DB 접근 없음)
    if not llm_client.health(host):
        raise RuntimeError(
            f"Ollama 에 연결할 수 없음 ({host}). "
            f"`ollama serve` 또는 `systemctl status ollama` 확인.")
    raw = llm_client.generate(
        p.user, model=model, host=host, system=p.system,
        force_json=True, timeout=timeout)

    # 5) 응답 파싱 (텍스트 → proposals; 신뢰하지 않고 스키마 검증)
    proposals, warnings = ai_parser.parse(raw)

    result: dict = {
        "diagnosis": diagnosis,
        "plan_text": plan_text,
        "proposals": [asdict(p) for p in proposals],
        "warnings": warnings,
        "verified": [],
        "llm": {"model": model, "raw_len": len(raw or "")},
    }

    # 6) 제안 검증 (DB 접근 — hypopg/EXPLAIN 으로 환각 차단)
    if verify and proposals:
        verified = proposal_verifier.verify_all(dsn, sql, proposals)
        result["verified"] = [asdict(v) for v in verified]

    return result


# ---------------------------------------------------------------------------
# 사람용 리포트 (pgtune ai 출력 / 로그 공용)
# ---------------------------------------------------------------------------
def format_report(result: dict) -> str:
    lines = []

    # (1) 규칙엔진 진단 — analyzer 의 포맷 재사용
    lines.append(analyzer.format_report(result["diagnosis"]))
    lines.append("")

    # (2) LLM 제안 + 검증 결과
    lines.append(f"=== LLM 제안 (model={result['llm']['model']}) ===")

    verified = result.get("verified", [])
    proposals = result.get("proposals", [])

    if not proposals:
        lines.append("  LLM 이 제안을 내지 않았습니다.")
    elif not verified:
        # 검증을 끈 경우: 제안만 (미검증 명시)
        lines.append("  ⚠ 미검증 제안 (--no-verify):")
        for pr in proposals:
            lines.append(f"  - [{pr['type']}] {_detail(pr)}")
            lines.append(f"      근거: {pr.get('rationale','')}")
    else:
        for v in verified:
            lines += _format_verified(v)

    # (3) 파서 경고
    if result.get("warnings"):
        lines.append("")
        lines.append("=== 파서 경고 ===")
        for w in result["warnings"]:
            lines.append(f"  - {w}")

    return "\n".join(lines)


def _detail(pr: dict) -> str:
    if pr["type"] == "index":
        return pr.get("ddl", "")
    if pr["type"] == "rewrite":
        return (pr.get("sql", "") or "")[:200]
    if pr["type"] == "config":
        return ", ".join(f"{k}={v}" for k, v in (pr.get("set") or {}).items())
    return ""


def _format_verified(v: dict) -> list:
    """검증된 제안 1건을 사람용 텍스트로."""
    type_label = {"index": "인덱스", "rewrite": "SQL 재작성", "config": "설정"}
    head = f"[{type_label.get(v['type'], v['type'])}] {v['detail']}"

    if not v["ok"]:
        return [head, f"  ❌ 검증 실패: {v.get('error','알 수 없음')}",
                f"  근거: {v['rationale']}"]

    out = [head]
    # cost 비교
    base, prop = v.get("baseline_cost"), v.get("proposed_cost")
    if base is not None and prop is not None:
        pct = v.get("improvement_pct", 0.0)
        delta = f"{pct:.1f}% 감소" if pct >= 0 else f"{-pct:.1f}% 증가"
        out.append(f"  cost: {base:,.0f} → {prop:,.0f} ({delta})")
    # index 전용: 플래너 채택 여부
    if v["type"] == "index" and v.get("adopted") is not None:
        out.append(f"  플래너 판정: {'✅ 채택됨' if v['adopted'] else '❌ 미채택'}")
    # rewrite: 결과 동등성 사람 확인
    if v.get("human_check"):
        out.append("  ⚠ 결과 동등성 미검증 — 원본과 같은 결과인지 직접 비교 필요.")
    if v.get("note"):
        out.append(f"  비고: {v['note']}")
    out.append(f"  근거: {v['rationale']}")
    return out
