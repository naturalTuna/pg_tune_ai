#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 naturalTuna — https://github.com/naturalTuna/pg_tune_ai
"""
pgexec.py  —  psql 실행 공통부

  - DSN 의 password 는 argv 대신 PGPASSWORD 환경변수로 넘긴다 (ps 로 비번 노출 방지)
  - ON_ERROR_STOP=1, statement_timeout / lock_timeout 기본 적용 (PGOPTIONS)
  - 값은 psql 변수(-v name=value)로 넘기고 스크립트에서 :'name' 으로 받는다
    → 문자열을 SQL 에 직접 이어붙이지 않음 (SQL 삽입 방지)

  설정: PGTUNE_STATEMENT_TIMEOUT (기본 5min), PGTUNE_LOCK_TIMEOUT (기본 5s)
        set_timeouts() 로 CLI 에서 변경
"""

from __future__ import annotations
import os
import subprocess

import sqlguard

STATEMENT_TIMEOUT = os.environ.get("PGTUNE_STATEMENT_TIMEOUT", "5min")
LOCK_TIMEOUT = os.environ.get("PGTUNE_LOCK_TIMEOUT", "5s")


def set_timeouts(statement: str | None = None, lock: str | None = None) -> None:
    global STATEMENT_TIMEOUT, LOCK_TIMEOUT
    if statement:
        STATEMENT_TIMEOUT = statement
    if lock:
        LOCK_TIMEOUT = lock


def _env(dsn: str) -> tuple:
    clean, pw = sqlguard.split_password(dsn)
    env = dict(os.environ)
    if pw is not None:
        env["PGPASSWORD"] = pw
    opts = env.get("PGOPTIONS", "")
    for k, v in (("statement_timeout", STATEMENT_TIMEOUT), ("lock_timeout", LOCK_TIMEOUT)):
        if v and f"{k}=" not in opts:
            opts += f" -c {k}={v.replace(' ', '')}"
    env["PGOPTIONS"] = opts.strip()
    return clean, env


def run_psql(dsn: str, script: str, variables: dict | None = None) -> str:
    """psql 로 스크립트 실행. 실패하면 RuntimeError(psql stderr)."""
    clean, env = _env(dsn)
    args = ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"]
    for k, v in (variables or {}).items():
        args += ["-v", f"{k}={v}"]
    if clean:
        args += ["-d", clean]
    proc = subprocess.run(args, input=script, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"psql 실패:\n{proc.stderr.strip()}")
    return proc.stdout


def wrap_tx(body: str, readonly: bool) -> str:
    """BEGIN [READ ONLY] ... ROLLBACK 로 감싼다. 결과는 항상 롤백."""
    head = "BEGIN TRANSACTION READ ONLY;" if readonly else "BEGIN;"
    return f"{head}\n{body.rstrip()}\nROLLBACK;\n"
