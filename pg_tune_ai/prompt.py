#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prompt.py — findings + 실행계획 → LLM 프롬프트 조립

설계 원칙
  - findings 의 evidence(실제 수치)를 그대로 프롬프트에 넣어, LLM 이 수치에서
    벗어나지 못하게 한다 (환각 억제).
  - 출력은 반드시 정해진 JSON 스키마. (Ollama format=json 과 함께 사용)
"""

from __future__ import annotations
import json
from dataclasses import dataclass

SYSTEM = (
    "You are a senior PostgreSQL performance tuning expert. "
    "You receive an SQL query, its execution plan, and a rule-engine "
    "diagnosis with concrete evidence numbers. Propose concrete improvements. "
    "Ground every proposal in the given evidence numbers; do NOT invent table "
    "names, columns, or statistics that are not present in the input. "
    "Respond ONLY with a JSON object matching the requested schema. No prose."
)

SCHEMA_HINT = """\
출력 JSON 스키마 (이 형식만, 다른 텍스트 금지):
{
  "proposals": [
    {"type": "index",   "ddl": "CREATE INDEX ...",   "rationale": "왜(한국어)"},
    {"type": "rewrite", "sql": "개선된 전체 SQL",      "rationale": "왜(한국어)"},
    {"type": "config",  "set": {"work_mem": "256MB"}, "rationale": "왜(한국어)"}
  ]
}
규칙:
- proposals 는 0개 이상. 근거가 약하면 넣지 마라.
- index   : ddl 은 실행 가능한 CREATE INDEX 문 전체.
- rewrite : sql 은 원본과 의미가 동일해야 하며, 실행 가능한 전체 SQL.
- config  : set 은 {GUC이름: 값} 객체. 값은 문자열.
- rationale 은 반드시 위 진단 evidence 의 수치를 근거로 들 것.
"""


@dataclass
class Prompt:
    system: str
    user: str


def build(sql: str, plan_text: str, findings: list[dict],
          extra_note: str | None = None) -> Prompt:
    """프롬프트(system/user) 조립."""
    findings_json = json.dumps(findings, ensure_ascii=False, indent=2)
    parts = [
        "[원본 SQL]", (sql or "").strip(), "",
        "[실행계획 (EXPLAIN)]", (plan_text or "").strip() or "(없음)", "",
        "[규칙엔진 진단 — evidence 수치 포함]", findings_json, "",
    ]
    if extra_note:
        parts += ["[추가 참고]", extra_note.strip(), ""]
    parts += [
        "[요청]",
        "위 근거를 바탕으로 개선안을 제안하라. 반드시 진단 evidence 의 수치를 "
        "rationale 의 근거로 사용하라.",
        "",
        SCHEMA_HINT,
    ]
    return Prompt(system=SYSTEM, user="\n".join(parts))
