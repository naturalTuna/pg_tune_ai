#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prompt.py — findings + 핫스팟 → LLM 프롬프트 조립 (경량 + 균형 버전)

설계 원칙
  - findings 의 evidence(실제 수치)를 근거로 주입해 환각을 억제한다.
  - "시간 핫스팟"을 함께 제공해 LLM 이 '진짜 범인 노드'를 공략하게 한다.
  - 원본 SQL 의 구조적 안티패턴(상관 서브쿼리/함수조건/OR체인)을 보고
    필요할 때만 rewrite 를 제안하게 한다.
  - CPU 추론을 고려해 프롬프트는 최소화하되, 판단에 필요한 정보는 빠뜨리지 않는다.
"""

from __future__ import annotations
import json
from dataclasses import dataclass

SYSTEM = (
    "You are a senior PostgreSQL performance tuning expert. "
    "You receive an SQL query, a rule-engine diagnosis with evidence numbers, "
    "and the top time-consuming plan nodes (hotspots). "
    "Ground every proposal in the given evidence; never invent table names, "
    "columns, or statistics not in the input. "
    "NEVER reference partition child tables (e.g. orders_2025_01) in SQL — "
    "always use the parent table.\n"
    "Priorities:\n"
    "1) Target the node in [시간 핫스팟] that consumes the MOST time. Do NOT "
    "propose indexes on tables that consume little time.\n"
    "2) Inspect [원본 SQL] for structural anti-patterns: a correlated subquery "
    "(subquery referencing the outer table, runs once per row), a function "
    "applied to a filtered column, OR-chains, or NOT IN with nullable columns. "
    "If present, propose a 'rewrite' that removes the anti-pattern while "
    "preserving identical results (e.g. correlated subquery → JOIN + GROUP BY).\n"
    "3) If the query has no anti-pattern and only lacks an index, propose only "
    "'index'.\n"
    "Respond ONLY with a JSON object matching the schema. No prose."
)

SCHEMA_HINT = """\
출력 JSON 스키마 (이 형식만):
{"proposals":[
  {"type":"index","ddl":"CREATE INDEX ...","rationale":"근거(한국어)"},
  {"type":"rewrite","sql":"개선된 전체 SQL","rationale":"근거(한국어)"},
  {"type":"config","set":{"work_mem":"256MB"},"rationale":"근거(한국어)"}
]}
규칙:
- 근거가 약하면 제안하지 마라. proposals 가 비어도 된다.
- rationale 은 진단 evidence 의 수치를 인용할 것.
- 시간을 많이 쓰는 노드를 우선 공략하라.
- 상관 서브쿼리/함수조건/OR체인 등 구조 문제가 보이면 rewrite 를 제안하라."""

_KEEP = ("rule", "relation", "message", "evidence")


@dataclass
class Prompt:
    system: str
    user: str


def _compact_findings(findings: list[dict], max_items: int = 6) -> list[dict]:
    """(rule, relation) 기준 중복만 제거하고 핵심 필드만 남긴다.
    같은 rule 이라도 테이블(relation)이 다르면 모두 유지한다
    (예: seq_scan_inefficient 가 customers 와 orders 양쪽에 있는 경우)."""
    seen: set = set()
    out: list[dict] = []
    for f in findings:
        key = (f.get("rule"), f.get("relation"))
        if key in seen:
            continue
        seen.add(key)
        out.append({k: f[k] for k in _KEEP if k in f})
        if len(out) >= max_items:
            break
    return out


def _compact_hotspots(hotspots: list[dict], top: int = 3) -> list[dict]:
    """상위 N개 핫스팟을 (노드/테이블/고유시간)만 추려 제공."""
    out = []
    for h in hotspots[:top]:
        out.append({
            "node": h.get("node_type"),
            "relation": h.get("relation"),
            "exclusive_ms": h.get("exclusive_ms"),
        })
    return out


@dataclass
class _Unused:
    pass


def build(sql: str, plan_text: str, findings: list[dict],
          hotspots: list[dict] | None = None,
          extra_note: str | None = None, include_plan: bool = False) -> Prompt:
    """프롬프트(system/user) 조립.

    hotspots: analyzer 결과의 hotspots 리스트(상위 노드 고유시간). 진짜 범인 식별용.
    include_plan=False(기본): EXPLAIN 텍스트는 넣지 않는다 (토큰 절감).
    """
    compact_f = _compact_findings(findings)
    findings_json = json.dumps(compact_f, ensure_ascii=False)

    parts = ["[원본 SQL]", (sql or "").strip(), ""]

    if hotspots:
        hs_json = json.dumps(_compact_hotspots(hotspots), ensure_ascii=False)
        parts += ["[시간 핫스팟 — 고유시간 큰 노드 = 진짜 범인]", hs_json, ""]

    parts += ["[규칙엔진 진단 — evidence 수치]", findings_json, ""]

    if include_plan and plan_text:
        parts += ["[실행계획]", plan_text.strip(), ""]
    if extra_note:
        parts += ["[참고]", extra_note.strip(), ""]

    parts += [
        "[요청] 위 핫스팟과 evidence 를 근거로 개선안을 제안하라. "
        "시간을 가장 많이 쓰는 노드를 우선 공략하고, SQL 구조 문제가 있으면 "
        "rewrite 도 제안하라.", "",
        SCHEMA_HINT,
    ]
    return Prompt(system=SYSTEM, user="\n".join(parts))
