#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parser.py — LLM 응답 → proposals 구조화 + 스키마 수준 검증

LLM 출력은 신뢰하지 않는다. JSON 파싱 + 타입별 필수필드 확인 후 유효한 항목만
Proposal 로 만든다. 깨진 항목은 버리고 warning 으로 남긴다.
(의미/효과 검증은 proposal_verifier 가 실제 플래너로 수행)
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass

VALID_TYPES = {"index", "rewrite", "config"}


@dataclass
class Proposal:
    type: str
    rationale: str
    ddl: str | None = None          # index
    sql: str | None = None          # rewrite
    set: dict | None = None         # config

    def detail(self) -> str:
        if self.type == "index":
            return self.ddl or ""
        if self.type == "rewrite":
            return self.sql or ""
        if self.type == "config":
            return ", ".join(f"{k}={v}" for k, v in (self.set or {}).items())
        return ""


def _extract_json(text: str) -> str:
    """코드펜스/잡음 제거 후 최외곽 JSON 오브젝트만 추출."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e == -1 or e < s:
        raise ValueError("JSON 오브젝트를 찾지 못함")
    return t[s:e + 1]


def parse(text: str) -> tuple[list[Proposal], list[str]]:
    """(proposals, warnings) 반환. 파싱 자체가 실패하면 ([], [에러])."""
    warnings: list[str] = []
    try:
        obj = json.loads(_extract_json(text))
    except (ValueError, json.JSONDecodeError) as e:
        return [], [f"LLM 응답 JSON 파싱 실패: {e}"]

    raw = obj.get("proposals")
    if not isinstance(raw, list):
        return [], ["응답에 'proposals' 배열이 없음"]

    out: list[Proposal] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"proposals[{i}]: 객체가 아님 — 건너뜀")
            continue
        t = item.get("type")
        rationale = str(item.get("rationale", "")).strip()
        if t not in VALID_TYPES:
            warnings.append(f"proposals[{i}]: 알 수 없는 type={t!r} — 건너뜀")
            continue

        if t == "index":
            ddl = item.get("ddl")
            if not isinstance(ddl, str) or "create index" not in ddl.lower():
                warnings.append(f"proposals[{i}](index): 유효한 ddl 없음 — 건너뜀")
                continue
            out.append(Proposal("index", rationale, ddl=ddl.strip()))
        elif t == "rewrite":
            s = item.get("sql")
            if not isinstance(s, str) or not s.strip():
                warnings.append(f"proposals[{i}](rewrite): 유효한 sql 없음 — 건너뜀")
                continue
            out.append(Proposal("rewrite", rationale, sql=s.strip()))
        elif t == "config":
            st = item.get("set")
            if not isinstance(st, dict) or not st:
                warnings.append(f"proposals[{i}](config): 유효한 set 없음 — 건너뜀")
                continue
            out.append(Proposal("config", rationale,
                                set={str(k): str(v) for k, v in st.items()}))
    return out, warnings
