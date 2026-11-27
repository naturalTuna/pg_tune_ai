#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 naturalTuna — https://github.com/naturalTuna/pg_tune_ai
"""
sqlguard.py  —  psql 스크립트에 들어가는 SQL 문자열 검사 (표준 라이브러리만)

pg_tune 은 사용자 SQL / LLM 제안 SQL 을 psql 스크립트에 끼워 EXPLAIN 한다.
psql 은 ';' 뒤의 문장도 전부 실행하므로, 끼워 넣기 전에 반드시 여기서 검사한다.

  split_statements(sql)   주석 / 문자열 / 따옴표 식별자 / $tag$ 를 고려해 문장 분리
  single_statement(sql)   한 문장만 허용 (끝의 ';' 는 제거해서 반환)
  first_keyword(sql)      주석 / 여는 괄호를 건너뛴 첫 키워드 (대문자)
  is_dml(sql)             INSERT / UPDATE / DELETE / MERGE 인지
  readonly_query(sql)     SELECT / WITH / VALUES / TABLE 이고 데이터 변경 키워드가 없는지
  guc_setting(name, val)  LLM 이 제안한 설정이 화이트리스트에 있는지 + 값 형식 확인
  index_ddl(ddl)          CREATE INDEX 한 문장인지 형식 확인
  quote_ident / qualify   식별자 쿼팅
  mask_dsn(dsn)           로그용 비밀번호 마스킹
"""

from __future__ import annotations
import re


class SqlGuardError(ValueError):
    """검사 실패 — 실행하면 안 되는 SQL."""


# ---------------------------------------------------------------------------
# 토크나이저: 주석/문자열 밖의 텍스트만 골라낸다
# ---------------------------------------------------------------------------
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _scan(sql: str):
    """(code_text, statements) — code_text 는 주석/문자열을 공백으로 바꾼 사본.
    statements 는 원문 기준 문장 리스트 (주석 제외 공백만 있는 문장은 버림)."""
    i, n = 0, len(sql)
    code = []
    stmts, start = [], 0
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":                      # -- 주석
            j = sql.find("\n", i)
            j = n if j == -1 else j
            code.append(" " * (j - i)); i = j
        elif c == "/" and nxt == "*":                    # /* */ (중첩 허용)
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1; j += 2
                elif sql.startswith("*/", j):
                    depth -= 1; j += 2
                else:
                    j += 1
            if depth:
                raise SqlGuardError("닫히지 않은 /* 주석")
            code.append(" " * (j - i)); i = j
        elif c == "'":                                   # '...' / E'...'
            esc = i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            j = i + 1
            while j < n:
                if esc and sql[j] == "\\":
                    j += 2; continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2; continue
                    break
                j += 1
            if j >= n:
                raise SqlGuardError("닫히지 않은 문자열 리터럴")
            code.append("'" + " " * (j - i - 1) + "'"); i = j + 1
        elif c == '"':                                   # "식별자"
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2; continue
                    break
                j += 1
            if j >= n:
                raise SqlGuardError('닫히지 않은 따옴표 식별자')
            code.append(sql[i:j + 1]); i = j + 1
        elif c == "$" and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            m = _DOLLAR.match(sql, i)
            if m:                                        # $tag$ ... $tag$
                tag = m.group(0)
                j = sql.find(tag, m.end())
                if j == -1:
                    raise SqlGuardError("닫히지 않은 달러 인용")
                j += len(tag)
                code.append(" " * (j - i)); i = j
            else:
                code.append(c); i += 1
        elif c == ";":
            stmts.append(sql[start:i]); start = i + 1
            code.append(c); i += 1
        else:
            code.append(c); i += 1
    stmts.append(sql[start:])
    return "".join(code), stmts


def _blank(s: str) -> bool:
    try:
        return not _scan(s)[0].strip()
    except SqlGuardError:
        return False


def split_statements(sql: str) -> list:
    _, stmts = _scan(sql or "")
    return [s.strip() for s in stmts if not _blank(s)]


def code_only(sql: str) -> str:
    """주석/문자열 내용을 지운 사본 (키워드 검사용)."""
    return _scan(sql or "")[0]


def single_statement(sql: str) -> str:
    stmts = split_statements(sql)
    if not stmts:
        raise SqlGuardError("빈 SQL")
    if len(stmts) > 1:
        raise SqlGuardError(f"한 문장만 허용 ({len(stmts)}개 문장 발견 — ';' 로 이어진 추가 문장은 실행하지 않음)")
    s = stmts[0]
    # psql 메타명령(\! 등) 차단: 문자열/주석 밖의 역슬래시
    if "\\" in code_only(s):
        raise SqlGuardError("psql 메타명령(\\...) 은 허용하지 않음")
    return s


def first_keyword(sql: str) -> str:
    m = re.match(r"[\s(]*([A-Za-z]+)", code_only(sql))
    return m.group(1).upper() if m else ""


_DML = {"INSERT", "UPDATE", "DELETE", "MERGE"}
_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|CALL|DO|"
    r"VACUUM|REINDEX|CLUSTER|LOCK|COMMIT|ROLLBACK|SET|RESET|LOAD|NOTIFY|LISTEN|PREPARE|EXECUTE|"
    r"REFRESH|SECURITY|IMPORT|DISCARD)\b", re.I)
# SELECT 안에서도 쓰기/위험 동작을 하는 대표 함수
_WRITE_FUNCS = re.compile(
    r"\b(nextval|setval|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|"
    r"lo_import|lo_export|lo_unlink|pg_read_file|pg_read_binary_file|pg_write_file|pg_ls_dir|"
    r"dblink\w*|set_config|pg_advisory\w*|txid_current|pg_switch_wal|pg_create_\w+|pg_drop_\w+)\s*\(",
    re.I)


def is_dml(sql: str) -> bool:
    return first_keyword(sql) in _DML


def readonly_query(sql: str) -> str:
    """LLM 재작성 SQL 처럼 '조회 전용' 이어야 하는 문장 검사. 통과하면 정리된 문장 반환."""
    s = single_statement(sql)
    if first_keyword(s) not in {"SELECT", "WITH", "VALUES", "TABLE"}:
        raise SqlGuardError(f"조회문만 허용 (첫 키워드 {first_keyword(s) or '없음'})")
    code = re.sub(r'"(?:[^"]|"")*"', ' _q_ ', code_only(s))   # 따옴표 식별자는 키워드 검사 제외
    m = _WRITE_WORDS.search(code)
    if m:
        raise SqlGuardError(f"조회문에 허용되지 않는 키워드: {m.group(1).upper()}")
    m = _WRITE_FUNCS.search(code)
    if m:
        raise SqlGuardError(f"조회문에 허용되지 않는 함수: {m.group(1)}")
    if re.search(r"\bFOR\s+(UPDATE|SHARE|NO\s+KEY|KEY)\b", code, re.I):
        raise SqlGuardError("FOR UPDATE / SHARE 잠금 절은 허용하지 않음")
    return s


# ---------------------------------------------------------------------------
# 설정(GUC) — LLM 이 제안할 수 있는 것만
# ---------------------------------------------------------------------------
_MEM = re.compile(r"^\d{1,7}\s*(kB|MB|GB)?$", re.I)
_NUM = re.compile(r"^\d+(\.\d+)?$")
_BOOL = re.compile(r"^(on|off|true|false|0|1)$", re.I)
GUC_WHITELIST = {
    "work_mem": _MEM, "hash_mem_multiplier": _NUM, "maintenance_work_mem": _MEM,
    "effective_cache_size": _MEM, "random_page_cost": _NUM, "seq_page_cost": _NUM,
    "cpu_tuple_cost": _NUM, "cpu_index_tuple_cost": _NUM, "cpu_operator_cost": _NUM,
    "effective_io_concurrency": re.compile(r"^\d{1,4}$"),
    "default_statistics_target": re.compile(r"^\d{1,5}$"),
    "from_collapse_limit": re.compile(r"^\d{1,3}$"), "join_collapse_limit": re.compile(r"^\d{1,3}$"),
    "max_parallel_workers_per_gather": re.compile(r"^\d{1,3}$"),
    "parallel_setup_cost": _NUM, "parallel_tuple_cost": _NUM,
    "jit": _BOOL, "jit_above_cost": _NUM, "jit_inline_above_cost": _NUM, "jit_optimize_above_cost": _NUM,
    "plan_cache_mode": re.compile(r"^(auto|force_custom_plan|force_generic_plan)$"),
}
GUC_WHITELIST.update({k: _BOOL for k in (
    "enable_seqscan", "enable_indexscan", "enable_indexonlyscan", "enable_bitmapscan",
    "enable_hashjoin", "enable_mergejoin", "enable_nestloop", "enable_hashagg", "enable_sort",
    "enable_incremental_sort", "enable_material", "enable_memoize", "enable_parallel_hash",
    "enable_parallel_append", "enable_partitionwise_join", "enable_partitionwise_aggregate",
    "enable_partition_pruning", "enable_gathermerge", "enable_tidscan")})


def guc_setting(name: str, value) -> tuple:
    k = str(name).strip().lower()
    v = str(value).strip().strip("'\"").strip()
    if k not in GUC_WHITELIST:
        raise SqlGuardError(f"허용되지 않은 설정: {k!r} (검증 가능한 플래너/메모리 설정만 허용)")
    if not GUC_WHITELIST[k].match(v):
        raise SqlGuardError(f"설정값 형식 오류: {k}={v!r}")
    return k, v


# ---------------------------------------------------------------------------
# CREATE INDEX 형식
# ---------------------------------------------------------------------------
_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_INDEX_RE = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?:{_IDENT}\s+)?ON\s+(?:ONLY\s+)?{_IDENT}(?:\s*\.\s*{_IDENT})?"
    r"\s*(?:USING\s+[A-Za-z_]+\s*)?\(.+\)\s*"
    r"(?:INCLUDE\s*\(.+\)\s*)?(?:WITH\s*\(.+\)\s*)?(?:WHERE\s+.+)?$",
    re.I | re.S)


def index_ddl(ddl: str) -> str:
    s = single_statement(ddl)
    if not _INDEX_RE.match(code_only(s).strip()):
        raise SqlGuardError("CREATE INDEX ... ON 테이블 (...) 형식이 아님")
    code = code_only(s)
    for bad in ("SELECT", "DROP", "INSERT", "UPDATE", "DELETE", "COMMIT", "COPY"):
        if re.search(rf"\b{bad}\b", code, re.I):
            raise SqlGuardError(f"인덱스 정의에 허용되지 않는 키워드: {bad}")
    return s


# ---------------------------------------------------------------------------
# 식별자 / DSN
# ---------------------------------------------------------------------------
_PLAIN = re.compile(r"^[a-z_][a-z0-9_$]*$")
_RESERVED = {"all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric", "both",
             "case", "cast", "check", "collate", "column", "constraint", "create", "current_date",
             "current_role", "current_time", "current_timestamp", "current_user", "default",
             "deferrable", "desc", "distinct", "do", "else", "end", "except", "false", "fetch", "for",
             "foreign", "from", "grant", "group", "having", "in", "initially", "intersect", "into",
             "lateral", "leading", "limit", "localtime", "localtimestamp", "not", "null", "offset", "on",
             "only", "or", "order", "placing", "primary", "references", "returning", "select",
             "session_user", "some", "symmetric", "table", "then", "to", "trailing", "true", "union",
             "unique", "user", "using", "variadic", "when", "where", "window", "with"}


def quote_ident(name: str) -> str:
    if _PLAIN.match(name) and name not in _RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def unquote_ident(tok: str) -> str:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == tok[-1] == '"':
        return tok[1:-1].replace('""', '"')
    return tok


def qualify(schema, rel: str) -> str:
    """schema.rel (public 생략) — 쿼팅 포함."""
    if schema and schema != "public":
        return f"{quote_ident(schema)}.{quote_ident(rel)}"
    return quote_ident(rel)


_PW_KV = re.compile(r"(password\s*=\s*)('(?:[^'\\]|\\.)*'|\S+)", re.I)
_PW_URI = re.compile(r"(postgres(?:ql)?://[^:/@\s]+:)([^@\s]+)(@)", re.I)


def mask_dsn(dsn) -> str:
    if not dsn:
        return ""
    s = _PW_KV.sub(r"\1****", str(dsn))
    return _PW_URI.sub(r"\1****\3", s)


def split_password(dsn: str) -> tuple:
    """DSN 에서 password 를 떼어 (dsn_without_pw, password|None). argv 에 비번이 안 보이게."""
    if not dsn:
        return dsn, None
    m = _PW_KV.search(dsn)
    if m:
        pw = m.group(2)
        if pw.startswith("'") and pw.endswith("'"):
            pw = re.sub(r"\\(.)", r"\1", pw[1:-1])
        return (dsn[:m.start()] + dsn[m.end():]).strip(), pw
    m = _PW_URI.search(dsn)
    if m:
        from urllib.parse import unquote
        return dsn[:m.start(2) - 1] + dsn[m.end(2):], unquote(m.group(2))
    return dsn, None
