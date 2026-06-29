#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tunelog.py  —  pgtune 실행 로그 기록 헬퍼

모든 로그는 (실행 위치 기준) ./pgtune_log/ 디렉토리 아래에
    {analyze|verify|tune}_{쿼리id}_{날짜}.log
형식으로 누적 저장된다.

  - analyze : pgtune analyze (진단)
  - verify  : pgtune verify  (검증)
  - tune    : pgtune tune    (튜닝, analyze + verify 통합)

쿼리id : SQL(또는 plan-file 경로)을 공백 정규화 후 md5 앞 8자리.
         같은 쿼리는 항상 같은 id → 같은 날 여러 번 실행하면 한 파일에 append.

기본적으로 항상 로그를 남기며, pgtune 의 --no-log 옵션으로 끌 수 있다.
"""

from __future__ import annotations
import hashlib
import os
import sys
from datetime import datetime

LOG_DIR = "pgtune_log"

# 서브커맨드 → 파일명 접두(라벨)
KIND_LABEL = {"analyze": "analyze", "verify": "verify", "tune": "tune"}


def query_id(source: str | None) -> str:
    """SQL(또는 plan-file 경로)로부터 짧고 안정적인 쿼리 식별자를 만든다."""
    if not source:
        return "noquery"
    normalized = " ".join(source.split()).lower()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:8]


def log_path(kind: str, source: str | None, when: datetime | None = None) -> str:
    when = when or datetime.now()
    label = KIND_LABEL.get(kind, kind)
    fname = f"{label}_{query_id(source)}_{when:%Y%m%d}.log"
    return os.path.join(LOG_DIR, fname)


def write(kind: str, source: str | None, content: str,
          dsn: str | None = None, sql: str | None = None,
          echo: bool = True) -> str:
    """로그 1건을 기록하고 파일 경로를 반환한다.

    kind    : analyze / verify / tune
    source  : 쿼리id 산출용 문자열 (보통 SQL, 없으면 plan-file 경로)
    content : 파일에 남길 본문 (사람용 리포트 + raw EXPLAIN 등)
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.now()
    path = log_path(kind, source, now)

    head = ["=" * 64, f"[{now:%Y-%m-%d %H:%M:%S}] pgtune {kind}"]
    if dsn:
        head.append(f"DSN : {dsn}")
    if sql:
        head.append(f"SQL : {sql}")
    head.append("=" * 64)

    block = "\n".join(head) + "\n" + content.rstrip() + "\n\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)

    if echo:
        # 본문 출력(stdout)과 섞이지 않도록 로그 경로는 stderr 로 안내
        print(f"[로그 기록] {path}", file=sys.stderr)
    return path