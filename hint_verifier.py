#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hint_verifier.py  —  실행계획 대안 / 설정 실험 (pg_hint_plan, SET)

  hypopg 가 "인덱스가 있다면?" 을 검증한다면, 여기서는
    1) pg_hint_plan 으로 "다른 조인 / 스캔 방식이었다면?" 을 실제로 돌려보고
    2) SET work_mem / jit 등 "설정을 바꾸면?" 을 같은 세션에서 돌려본다.

  - 추정 모드(ANALYZE 없음): 추정 cost 만 비교. 실제 실행 안 함.
  - 실측 모드(ANALYZE)     : 실제 실행 시간 비교. DML 은 ROLLBACK.
  - pg_hint_plan 은 LOAD 'pg_hint_plan' 으로 세션에만 적용 (shared_preload 불필요).
    LOAD 는 superuser 권한이 필요하다 (튜닝 실행 계정을 postgres 로).
"""

from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Optional

import analyzer

HINT_PREFIX = "LOAD 'pg_hint_plan';\nSET pg_hint_plan.enable_hint = on;"


@dataclass
class ExperimentResult:
    kind: str                 # hint / setting
    label: str                # 힌트 본문 또는 SET 문
    target: str
    applied: bool             # 힌트가 실제 플랜에 반영됐나 (setting 은 항상 True)
    plan_changed: bool
    cost_before: float
    cost_after: float
    ms_before: Optional[float]
    ms_after: Optional[float]
    verdict: str
    detail: dict = field(default_factory=dict)


def hint_available(dsn: str) -> tuple:
    try:
        analyzer.run_psql(dsn, HINT_PREFIX + "\nSELECT 1;")
        return True, ""
    except RuntimeError as e:
        msg = str(e).splitlines()[-1]
        if "permission denied" in msg or "superuser" in msg or "is not allowed" in msg:
            return False, "LOAD 'pg_hint_plan' 권한 없음 — 튜닝 실행 계정을 superuser(postgres) 로"
        if "could not access file" in msg or "No such file" in msg:
            return False, "pg_hint_plan 미설치 — 패키지 extension 빌드 확인 (installer extensions)"
        return False, msg


def signature(plan: dict) -> list:
    return [(n.get("Node Type"), n.get("Relation Name"), n.get("Index Name"))
            for n in analyzer.walk(plan)]


def count_type(plan: dict, t: str) -> int:
    return sum(1 for n in analyzer.walk(plan) if n.get("Node Type") == t
               or (t == "Index Scan" and n.get("Node Type") == "Index Only Scan"))


def spills(plan: dict) -> int:
    n_sp = 0
    for n in analyzer.walk(plan):
        if n.get("Sort Space Type") == "Disk" or n.get("Hash Batches", 1) > 1 \
                or n.get("Lossy Heap Blocks", 0) > 0:
            n_sp += 1
    return n_sp


def _ms(root: dict) -> Optional[float]:
    return root.get("Execution Time")


def _cost(root: dict) -> float:
    return float(root["Plan"].get("Total Cost", 0.0))


def _verdict(applied, changed, cb, ca, mb, ma, analyze) -> str:
    if not applied:
        return "힌트 미적용 (플래너가 이 방식을 쓸 수 없는 조건 / 별칭 불일치)"
    if not changed:
        return "플랜 변화 없음"
    if analyze and mb and ma is not None:
        pct = (1 - ma / mb) * 100
        if pct >= 20:
            return f"✅ 효과 있음 — 실제 {mb:,.1f} → {ma:,.1f} ms ({pct:.0f}% 감소)"
        if pct <= -20:
            return f"❌ 더 느림 — 실제 {mb:,.1f} → {ma:,.1f} ms (플래너 선택이 맞음)"
        return f"차이 미미 — 실제 {mb:,.1f} → {ma:,.1f} ms"
    if ca < cb * 0.8:
        return "추정 비용 감소 — 플래너가 비용을 잘못 계산 중일 수 있음 (통계 확인, --analyze 로 실측 권장)"
    return "추정상 더 비쌈 — 실측(--analyze) 으로만 판단 가능"


def run_hint(dsn: str, sql: str, exp: dict, base: dict, analyze: bool) -> ExperimentResult:
    root = analyzer.explain_json(dsn, sql, analyze, prefix=HINT_PREFIX, hint=exp["hint"])
    bp, ap = base["Plan"], root["Plan"]
    changed = signature(bp) != signature(ap)
    if exp.get("expect"):
        applied = count_type(ap, exp["expect"]) > count_type(bp, exp["expect"])
    else:  # Rows 보정 같은 경우: 플랜이 바뀌었는지로 판단
        applied = True
    cb, ca = _cost(base), _cost(root)
    mb, ma = _ms(base), _ms(root)
    return ExperimentResult(
        "hint", exp["hint"], exp["target"], applied, changed, cb, ca, mb, ma,
        _verdict(applied, changed, cb, ca, mb, ma, analyze),
        {"reason": exp.get("reason", ""),
         "plan_before": [s[0] for s in signature(bp)][:8],
         "plan_after": [s[0] for s in signature(ap)][:8]})


def setting_experiments(res: dict, cur: dict) -> list:
    """진단 결과 → 시험해 볼 SET 목록."""
    rules = {f["rule"] for f in res.get("findings", [])}
    out = []
    if rules & {"sort_spill_to_disk", "hash_spill_to_disk", "lossy_bitmap"}:
        wm = cur.get("work_mem_kb", 4096)
        target = min(max(wm * 4, 65536), 1048576)
        out.append((f"SET work_mem = '{target // 1024}MB'",
                    f"work_mem {wm // 1024}MB → {target // 1024}MB (스필 제거 확인)"))
    if "jit_overhead" in rules:
        out.append(("SET jit = off", "JIT 끄기"))
    return out


def current_settings(dsn: str) -> dict:
    try:
        out = analyzer.run_psql(dsn, "SELECT setting FROM pg_settings WHERE name='work_mem';").strip()
        return {"work_mem_kb": int(out)}
    except Exception:
        return {"work_mem_kb": 4096}


def run_setting(dsn: str, sql: str, set_sql: str, target: str, base: dict) -> ExperimentResult:
    root = analyzer.explain_json(dsn, sql, True, prefix=set_sql + ";")
    mb, ma = _ms(base), _ms(root)
    sb, sa = spills(base["Plan"]), spills(root["Plan"])
    v = _verdict(True, True, _cost(base), _cost(root), mb, ma, True)
    if sb and not sa:
        v += " / 디스크 스필 사라짐"
    return ExperimentResult("setting", set_sql, target, True, signature(base["Plan"]) != signature(root["Plan"]),
                            _cost(base), _cost(root), mb, ma, v, {"spills_before": sb, "spills_after": sa})


def indexed_leading_cols(dsn: str, rel: str) -> set:
    """rel 에 이미 있는 인덱스들의 선두 컬럼."""
    try:
        out = analyzer.run_psql(dsn, f"""
SELECT a.attname FROM pg_index i
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
WHERE i.indrelid = '{rel}'::regclass;""")
        return {ln.strip() for ln in out.splitlines() if ln.strip()}
    except Exception:
        return set()


def run_all(dsn: str, sql: str, res: dict, base: dict, analyze: bool, use_hint: bool = True) -> dict:
    out = {"hint_available": None, "hint_message": "", "results": []}
    if use_hint and res.get("hint_experiments"):
        ok, msg = hint_available(dsn)
        out["hint_available"], out["hint_message"] = ok, msg
        if ok:
            for exp in res["hint_experiments"]:
                ex = exp.get("extra") or {}
                if ex.get("need_index"):
                    # 쓸 수 있는 기존 인덱스가 없으면 스캔 힌트는 의미 없음 (인덱스 후보 쪽에서 다룸)
                    if not (indexed_leading_cols(dsn, ex.get("rel", "")) & set(ex.get("cols", []))):
                        continue
                try:
                    out["results"].append(asdict(run_hint(dsn, sql, exp, base, analyze)))
                except RuntimeError as e:
                    out["results"].append({"kind": "hint", "label": exp["hint"], "target": exp["target"],
                                           "applied": False, "verdict": "실험 실패: " + str(e).splitlines()[-1]})
    if analyze:
        for set_sql, target in setting_experiments(res, current_settings(dsn)):
            try:
                out["results"].append(asdict(run_setting(dsn, sql, set_sql, target, base)))
            except RuntimeError as e:
                out["results"].append({"kind": "setting", "label": set_sql, "target": target,
                                       "applied": False, "verdict": "실험 실패: " + str(e).splitlines()[-1]})
    return out


def format_results(out: dict, analyze: bool) -> str:
    lines = ["=== 실행계획 대안 / 설정 실험 (pg_hint_plan · SET) ==="]
    if out.get("hint_available") is False:
        lines.append(f"  ⚠ 힌트 실험 불가: {out.get('hint_message')}")
    if not out.get("results"):
        if out.get("hint_available") is not False:
            lines.append("  실험할 대안이 없습니다.")
        return "\n".join(lines)
    mode = "실측(ANALYZE)" if analyze else "추정 cost"
    lines.append(f"  비교 기준: {mode}")
    for r in out["results"]:
        lab = f"/*+ {r['label']} */" if r["kind"] == "hint" else r["label"]
        lines.append(f"  {lab}")
        lines.append(f"      {r.get('target', '')}")
        if "cost_before" in r and r.get("applied"):
            lines.append(f"      cost {r['cost_before']:,.0f} → {r['cost_after']:,.0f}"
                         + (f" / plan {' > '.join(r['detail'].get('plan_after', [])[:4])}"
                            if r["kind"] == "hint" and r.get("plan_changed") else ""))
        lines.append(f"      → {r['verdict']}")
    return "\n".join(lines)
