#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 naturalTuna — https://github.com/naturalTuna/pg_tune_ai
"""
proposal_verifier.py — LLM proposals 를 실제 플래너로 검증 (환각 차단)

- index   : 기존 verifier.verify_candidate (hypopg 가상 인덱스, 채택 여부까지)
- config  : 세션 SET LOCAL 후 EXPLAIN cost 비교 (BEGIN/ROLLBACK, 영구변경 X)
- rewrite : 재작성 SQL 의 EXPLAIN cost 비교 + '실행계획이 잡히는지' 자체가 1차 검증
            ⚠ 결과 동등성은 자동 검증 불가 → 항상 human_check=True

모두 cost(추정) 기준이다. 특히 config(work_mem 등)는 런타임 개선이 cost 에
잘 안 드러날 수 있으므로 note 로 명시한다.
"""

from __future__ import annotations
import json
from dataclasses import dataclass

import pgexec
import sqlguard
import verifier  # 상위 디렉터리 verifier.py (hypopg 인덱스 검증 재사용)


@dataclass
class VerifiedProposal:
    type: str
    detail: str
    rationale: str
    ok: bool                          # EXPLAIN 성공 & 유효
    baseline_cost: float | None = None
    proposed_cost: float | None = None
    improvement_pct: float | None = None
    adopted: bool | None = None       # index 전용: 플래너가 가상 인덱스 채택?
    human_check: bool = False         # rewrite: 결과 동등성 사람 확인 필요
    note: str = ""
    error: str | None = None
    explain_before: str = ""          # 로그용: 적용 전 EXPLAIN(텍스트)
    explain_after: str = ""           # 로그용: 적용 후 EXPLAIN(텍스트)


def _set_script(settings: dict | None) -> tuple:
    """화이트리스트 검사를 통과한 설정만 set_config(:'k', :'v', true) 로 (값은 psql 변수)."""
    lines, vars_ = [], {}
    for i, (k, v) in enumerate((settings or {}).items()):
        k, v = sqlguard.guc_setting(k, v)
        vars_[f"k{i}"], vars_[f"v{i}"] = k, v
        lines.append(f"SELECT set_config(:'k{i}', :'v{i}', true);")
    return "\n".join(lines), vars_


def _run_explain(dsn: str, sql: str, settings: dict | None, opts: str) -> str:
    """LLM / 사용자 SQL 을 READ ONLY 트랜잭션 안에서 EXPLAIN (실행 안 함) 후 ROLLBACK.
    sql 은 한 문장만 (sqlguard), 설정은 화이트리스트만."""
    stmt = sqlguard.single_statement(sql)
    pre, vars_ = _set_script(settings)
    body = (pre + "\n" if pre else "") + f"EXPLAIN {opts}{stmt};"
    return verifier.run_sql(dsn, pgexec.wrap_tx(body, readonly=True), vars_)


def _explain_text(dsn: str, sql: str, settings: dict | None = None) -> str:
    out = _run_explain(dsn, sql, settings, "")
    keep = [ln for ln in out.splitlines()
            if ln and ln.strip() not in ("BEGIN", "ROLLBACK", "SET") and not ln.startswith(tuple(
                str(v) for v in (settings or {}).values()))]
    return "\n".join(keep).rstrip()


def _explain_cost(dsn: str, sql: str, settings: dict | None = None) -> float:
    out = _run_explain(dsn, sql, settings, "(FORMAT JSON) ")
    s, e = out.find("["), out.rfind("]")
    if s == -1 or e == -1:
        raise RuntimeError(f"EXPLAIN 결과를 파싱하지 못함:\n{out}")
    plan = json.loads(out[s:e + 1])[0]["Plan"]
    return float(plan.get("Total Cost", 0.0))


def _pct(base: float | None, new: float | None) -> float:
    return round((1 - new / base) * 100, 1) if base else 0.0


def verify_index(dsn: str, sql: str, p) -> VerifiedProposal:
    cap: dict = {}
    try:
        r = verifier.verify_candidate(dsn, sql, p.ddl, capture=cap)
    except Exception as e:
        return VerifiedProposal("index", p.ddl, p.rationale, ok=False, error=str(e))
    note = "" if r.adopted_by_planner else "플래너가 채택하지 않음 → 효과 낮음"
    return VerifiedProposal(
        "index", p.ddl, p.rationale, ok=True,
        baseline_cost=r.baseline_cost, proposed_cost=r.indexed_cost,
        improvement_pct=r.improvement_pct, adopted=r.adopted_by_planner, note=note,
        explain_before=cap.get("before_text", ""),
        explain_after=cap.get("after_text", ""))


def verify_config(dsn: str, sql: str, p, baseline: float | None = None) -> VerifiedProposal:
    detail = ", ".join(f"{k}={v}" for k, v in p.set.items())
    try:
        for k, v in p.set.items():
            sqlguard.guc_setting(k, v)            # 허용 안 된 설정이면 여기서 실패 처리
        base = baseline if baseline is not None else _explain_cost(dsn, sql)
        new = _explain_cost(dsn, sql, p.set)
        eb = _explain_text(dsn, sql)
        ea = _explain_text(dsn, sql, p.set)
    except Exception as e:
        return VerifiedProposal("config", detail, p.rationale, ok=False, error=str(e))
    return VerifiedProposal(
        "config", detail, p.rationale, ok=True,
        baseline_cost=base, proposed_cost=new, improvement_pct=_pct(base, new),
        note="cost(추정) 기준. work_mem 등은 런타임 개선이 cost 에 안 드러날 수 있음.",
        explain_before=eb, explain_after=ea)


def verify_rewrite(dsn: str, sql: str, p, baseline: float | None = None) -> VerifiedProposal:
    try:
        rewritten = sqlguard.readonly_query(p.sql)   # 조회 전용 한 문장만 (쓰기/다중문 차단)
        base = baseline if baseline is not None else _explain_cost(dsn, sql)
        new = _explain_cost(dsn, rewritten)   # 재작성 SQL 이 일단 계획이 잡히는지도 검증
        eb = _explain_text(dsn, sql)
        ea = _explain_text(dsn, rewritten)
    except Exception as e:
        return VerifiedProposal("rewrite", p.sql, p.rationale, ok=False,
                                human_check=True, error=str(e))
    return VerifiedProposal(
        "rewrite", p.sql, p.rationale, ok=True,
        baseline_cost=base, proposed_cost=new, improvement_pct=_pct(base, new),
        human_check=True,
        note="⚠ 결과 동등성 미검증 — 원본과 같은 결과를 내는지 직접 비교 필요.",
        explain_before=eb, explain_after=ea)


def verify_all(dsn: str, sql: str, proposals: list) -> list:
    """proposals 를 타입별로 검증. baseline(원본 cost)은 한 번만 계산해 재사용."""
    baseline = None
    if any(p.type in ("config", "rewrite") for p in proposals):
        try:
            baseline = _explain_cost(dsn, sql)
        except Exception:
            baseline = None

    results: list[VerifiedProposal] = []
    for p in proposals:
        if p.type == "index":
            results.append(verify_index(dsn, sql, p))
        elif p.type == "config":
            results.append(verify_config(dsn, sql, p, baseline=baseline))
        elif p.type == "rewrite":
            results.append(verify_rewrite(dsn, sql, p, baseline=baseline))
        else:
            results.append(VerifiedProposal(p.type, p.detail(), p.rationale,
                                            ok=False, error="검증 미지원 타입"))
    return results
