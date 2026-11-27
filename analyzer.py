#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 naturalTuna — https://github.com/naturalTuna/pg_tune_ai
"""
analyzer.py  —  쿼리 진단 엔진 (EXPLAIN 플랜 규칙 분석)

  EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT JSON) 결과를 파싱
    → 플랜 트리를 순회하며 규칙 적용 → findings(진단)
    → 고유 시간 핫스팟 → 범인 노드
    → findings 에서 인덱스 후보(hypopg 검증 대상) / 힌트 실험(pg_hint_plan 검증 대상) 도출

  외부 의존성 없음 (python3 표준 라이브러리 + psql).
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, asdict, field
from typing import Iterator, Optional

import pgexec
import sqlguard
from sqlguard import quote_ident, unquote_ident

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


@dataclass
class Finding:
    rule: str
    severity: str
    node_type: str
    relation: Optional[str]
    message: str
    evidence: dict
    suggestion: str


@dataclass
class IndexCandidate:
    relation: str
    columns: list
    reason: str
    ddl: str


@dataclass
class HintExperiment:
    hint: str          # pg_hint_plan 힌트 본문 (예: "HashJoin(a b)")
    target: str        # 무엇을 바꾸려는지 (예: "Nested Loop → Hash Join")
    reason: str
    expect: str        # 적용됐는지 판단할 노드 타입 (예: "Hash Join")
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# psql  (실행은 pgexec 공통부 — 비번 argv 비노출, timeout, ON_ERROR_STOP)
# ---------------------------------------------------------------------------
def run_psql(dsn: str, script: str, variables: Optional[dict] = None) -> str:
    return pgexec.run_psql(dsn, script, variables)


def is_dml(sql: str) -> bool:
    return sqlguard.is_dml(sql)


def _json_from(out: str) -> dict:
    s, e = out.find("["), out.rfind("]")
    if s == -1 or e == -1:
        raise RuntimeError(f"플랜 JSON 을 찾지 못함. psql 출력:\n{out[:500]}")
    return json.loads(out[s:e + 1])[0]


def _check_hint(hint: str) -> str:
    if not hint:
        return ""
    if "*/" in hint or "/*" in hint or ";" in hint or "\\" in hint:
        raise sqlguard.SqlGuardError("힌트에 주석 기호 / ';' / 역슬래시는 쓸 수 없음")
    return hint


def explain_script(sql: str, analyze: bool, opts: str, prefix: str = "", hint: str = "") -> str:
    """EXPLAIN 스크립트 생성. 대상 SQL 은 반드시 한 문장(sqlguard), 결과는 항상 ROLLBACK.
      - DML        : BEGIN ... ROLLBACK         (ANALYZE 해도 변경은 남지 않음)
      - 그 외 전부  : BEGIN READ ONLY ... ROLLBACK (WITH ... DELETE 같은 숨은 쓰기도 에러로 차단)
    prefix(LOAD/SET) 는 내부에서 만든 문장만 넣는다."""
    stmt = sqlguard.single_statement(sql)
    h = f"/*+ {_check_hint(hint)} */ " if hint else ""
    body = f"EXPLAIN {opts}{h}{stmt};"
    pre = (prefix.rstrip() + "\n") if prefix else ""
    return pre + pgexec.wrap_tx(body, readonly=not sqlguard.is_dml(stmt))


def explain_json(dsn: str, sql: str, analyze: bool, prefix: str = "", hint: str = "") -> dict:
    """EXPLAIN JSON 1회. prefix: 같은 세션에서 먼저 실행할 SQL (LOAD/SET 등).
    hint: pg_hint_plan 힌트 (/*+ ... */ 로 감쌈)."""
    opts = "(ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT JSON) " if analyze \
        else "(VERBOSE, SETTINGS, FORMAT JSON) "
    return _json_from(run_psql(dsn, explain_script(sql, analyze, opts, prefix, hint)))


def get_plan_from_db(dsn: str, sql: str, analyze: bool = True) -> dict:
    return explain_json(dsn, sql, analyze)


def get_plan_text(dsn: str, sql: str, analyze: bool = False) -> str:
    opts = "(ANALYZE, BUFFERS) " if analyze else ""
    out = run_psql(dsn, explain_script(sql, analyze, opts))
    lines = [ln for ln in out.splitlines() if ln.strip() not in {"BEGIN", "ROLLBACK", "COMMIT"}]
    return "\n".join(lines).rstrip("\n")


def load_plan_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return _json_from(f.read())


# ---------------------------------------------------------------------------
# 트리 유틸
# ---------------------------------------------------------------------------
def walk(node: dict) -> Iterator[dict]:
    yield node
    for c in node.get("Plans", []):
        yield from walk(c)


def walk_parent(node: dict, parent: Optional[dict] = None):
    yield node, parent
    for c in node.get("Plans", []):
        yield from walk_parent(c, node)


def total_time(node: dict) -> float:
    return node.get("Actual Total Time", 0.0) * node.get("Actual Loops", 1)


def exclusive_time(node: dict) -> float:
    kids = sum(total_time(c) for c in node.get("Plans", []))
    return max(total_time(node) - kids, 0.0)


def rel_label(node: dict) -> Optional[str]:
    return node.get("Relation Name") or node.get("Index Name")


def qual_rel(node: dict) -> Optional[str]:
    """schema.relation (VERBOSE 플랜). public 은 생략."""
    r = node.get("Relation Name")
    if not r:
        return None
    return sqlguard.qualify(node.get("Schema"), r)


def node_loops(node: dict) -> int:
    return node.get("Actual Loops", 1) or 1


def rows_total(node: dict, key: str) -> int:
    return round(node.get(key, 0) * node_loops(node))


def aliases_under(node: dict) -> list:
    """서브트리 안의 스캔 노드 별칭 (힌트 대상). 순서 유지, 중복 제거."""
    out = []
    for n in walk(node):
        a = n.get("Alias")
        if a and n.get("Relation Name") and a not in out:
            out.append(a)
    return out


def has_actual(root_plan: dict) -> bool:
    return "Actual Rows" in root_plan


# ---------------------------------------------------------------------------
# 조건식 파싱 (플랜의 Filter / Index Cond / Join Filter 문자열)
#   - 문자열 리터럴 내용 제거, 형변환(::type) 제거, (col) 괄호 벗기기
#   - 따옴표 식별자 / 대소문자 식별자 지원:  ("Region" = 3)
#   - ((status)::text = 'A'::text) 에서 'text' 를 컬럼으로 잡던 문제 해결
# ---------------------------------------------------------------------------
_ID = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_TYPE = (r'(?:timestamp(?:\(\d\))?\s+with(?:out)?\s+time\s+zone|time(?:\(\d\))?\s+with(?:out)?\s+time\s+zone|'
         r'character\s+varying|double\s+precision|bit\s+varying|"[^"]+"|[A-Za-z_][\w.]*)')
_CAST = re.compile(r'::\s*' + _TYPE + r'(?:\(\d+(?:\s*,\s*\d+)?\))?(?:\[\])*', re.I)
_PAREN1 = re.compile(r'(?<![\w"$])\(\s*((?:' + _ID + r'\s*\.\s*)?' + _ID + r')\s*\)')
_OPS = r'(=|<>|!=|<=|>=|<|>|!~~\*?|~~\*?|~\*?|\bIS\b|\bLIKE\b|\bILIKE\b|\bIN\b|\bBETWEEN\b)'
_COLREF = re.compile(r'(?<![\w"$.])(?:(' + _ID + r')\s*\.\s*)?(' + _ID + r')\s*' + _OPS + r'\s*(.?)', re.I)
_SKIP_WORDS = {"and", "or", "not", "is", "null", "true", "false", "any", "all", "some", "array",
               "case", "when", "then", "else", "end", "in", "like", "ilike", "between", "exists"}


def normalize_cond(expr: Optional[str]) -> str:
    if not expr:
        return ""
    try:
        s = sqlguard.code_only(expr)          # '...' 내용 → 공백
    except sqlguard.SqlGuardError:
        s = expr
    s = re.sub(r"'\s*'", "'?'", s)
    prev = None
    while prev != s:
        prev = s
        s = _CAST.sub("", s)
        s = _PAREN1.sub(r"\1", s)
    return s


def cond_refs(expr: Optional[str]) -> list:
    """[(alias|None, column, op, is_equality_filter)] — 컬럼 비교만."""
    out = []
    s = normalize_cond(expr)
    for m in _COLREF.finditer(s):
        alias, col, op, rhs = m.group(1), m.group(2), m.group(3).upper(), m.group(4)
        c = unquote_ident(col)
        if not col.startswith('"') and c.lower() in _SKIP_WORDS:
            continue
        # 오른쪽이 다른 컬럼이면 조인 조건 → equality 필터로 보지 않음
        rhs_is_col = bool(re.match(r'[A-Za-z_"]', rhs or "")) and not re.match(
            r'(?i)(ANY|ALL|NULL|TRUE|FALSE|NOT)\b', s[m.end(4) - 1:m.end(4) + 5])
        eq = op == "=" and not rhs_is_col
        out.append((unquote_ident(alias) if alias else None, c, op, eq))
    return out


def extract_filter_columns(expr: Optional[str]) -> list:
    cols = []
    for _, c, _, _ in cond_refs(expr):
        if c not in cols:
            cols.append(c)
    return cols


def equality_columns(expr: Optional[str]) -> list:
    """= 조건 컬럼만 (복합 인덱스 선두에 둘 컬럼). col = ANY(...) (IN 리스트) 포함."""
    out = []
    for _, c, _, eq in cond_refs(expr):
        if eq and c not in out:
            out.append(c)
    return out


_SORTKEY = re.compile(r'^(?:(' + _ID + r')\s*\.\s*)?(' + _ID + r')(\s+(?:DESC|ASC))?(\s+NULLS\s+(?:FIRST|LAST))?$', re.I)


def sort_key_columns(keys: list, alias: Optional[str] = None) -> list:
    """Sort Key ["o.created_at DESC", "o.id"] → ["created_at DESC", "id"] (단순 컬럼만)."""
    out = []
    for k in keys or []:
        m = _SORTKEY.match(k.strip())
        if not m:
            return []           # 표현식 정렬은 인덱스 후보로 만들지 않음
        if alias and m.group(1) and unquote_ident(m.group(1)) != alias:
            return []
        out.append(unquote_ident(m.group(2)) + (m.group(3) or "").upper())
    return out


def _col_name(c: str) -> str:
    """'created_at DESC' → 'created_at'  ('(date(x))' 같은 표현식은 그대로)"""
    return re.sub(r"\s+(DESC|ASC)$", "", c, flags=re.I)


def _col_sql(c: str) -> str:
    if c.startswith("("):            # 표현식 인덱스 키는 이미 SQL 형태
        return c
    base = _col_name(c)
    return quote_ident(base) + c[len(base):]


# ---------------------------------------------------------------------------
# 규칙 (node, parent, ctx) -> Finding | None
# ---------------------------------------------------------------------------
def rule_bad_estimate(node, parent, ctx):
    if "Actual Rows" not in node:
        return None
    est = max(node.get("Plan Rows", 0), 0)
    act = max(node.get("Actual Rows", 0), 0)
    lo, hi = max(min(est, act), 1), max(est, act, 1)
    ratio = hi / lo
    if ratio < 10 or hi < 100:
        return None
    return Finding(
        "bad_estimate", "high" if ratio >= 100 else "medium", node["Node Type"], rel_label(node),
        f"추정 {est:,}행 vs 실제 {act:,}행 ({ratio:.0f}배 차이)",
        {"plan_rows": est, "actual_rows": act, "ratio": round(ratio, 1),
         "aliases": aliases_under(node)},
        "해당 테이블 ANALYZE. 여러 컬럼 조건이 얽혀 있으면 CREATE STATISTICS (dependencies, ndistinct) 검토. "
        "통계 오차로 조인 방식이 잘못 선택됐는지는 아래 힌트 실험(Rows 보정)으로 확인.")


def rule_seq_scan_inefficient(node, parent, ctx, min_rows=1000):
    if node.get("Node Type") not in ("Seq Scan", "Parallel Seq Scan") or "Filter" not in node:
        return None
    if "Actual Rows" in node:
        removed = rows_total(node, "Rows Removed by Filter")
        kept = rows_total(node, "Actual Rows")
    else:  # 추정 플랜: 테이블 추정 행수 대비 결과 행수
        kept = int(node.get("Plan Rows", 0))
        removed = max(ctx.get("reltuples", {}).get(qual_rel(node) or "", 0) - kept, 0)
    scanned = removed + kept
    if scanned < min_rows or removed <= kept * 2:
        return None
    return Finding(
        "seq_scan_inefficient", "medium", node["Node Type"], qual_rel(node),
        f"{scanned:,}행 스캔 후 {removed:,}행을 필터로 버림",
        {"scanned": scanned, "removed": removed, "kept": kept, "workers": node_loops(node),
         "filter": node.get("Filter"), "alias": node.get("Alias")},
        "Filter 컬럼에 인덱스 후보 → hypopg 로 플래너 채택 여부 검증.")


# 비교 연산자 왼쪽에 있는 func(col) 만 (우변 상수쪽 upper('abc') 같은 건 제외)
_FUNC_ON_COL = re.compile(r'(?<![\w"$.])([A-Za-z_][A-Za-z0-9_]{1,})\s*\(\s*(?:' + _ID + r'\s*\.\s*)?(' + _ID
                          + r')\s*\)\s*(?:=|<>|!=|<=|>=|<|>|~~|\bBETWEEN\b|\bIN\b|\bIS\b)', re.I)
_FUNC_SKIP = {"not", "any", "all", "in", "exists", "and", "or"}


def rule_function_on_column(node, parent, ctx):
    filt = node.get("Filter")
    if not filt:
        return None
    m = _FUNC_ON_COL.search(normalize_cond(filt))
    if not m or m.group(1).lower() in _FUNC_SKIP:
        return None
    func, col = m.group(1), unquote_ident(m.group(2))
    rel = qual_rel(node) or rel_label(node)
    return Finding(
        "function_on_column", "medium", node.get("Node Type", "Scan"), rel,
        f"Filter 에서 컬럼에 함수 적용: {func}({col}) — 일반 인덱스 / 파티션 프루닝 무효화",
        {"function": func, "column": col, "removed": rows_total(node, "Rows Removed by Filter"), "filter": filt},
        f"범위 조건으로 재작성 (예: date(col)=X → col >= X AND col < X + 1일). "
        f"불가피하면 표현식 인덱스: CREATE INDEX ON {rel} (({func}({quote_ident(col)}))).")


# 컬럼에 형변환이 붙어 인덱스를 못 타는 경우: ((col)::text = '...'::text)
_CAST_ON_COL = re.compile(r'\(\(?(?:' + _ID + r'\s*\.\s*)?(' + _ID + r')\)?::([a-z][a-z ]*?)\s*(=|<>|<|>|<=|>=|~~)', re.I)
# 이 타입 → 이 캐스트는 인덱스 사용에 지장 없음 (binary-coercible / 같은 opfamily)
_HARMLESS_CAST = {("character varying", "text"), ("text", "text"), ("name", "text")}


def rule_implicit_cast(node, parent, ctx):
    filt = node.get("Filter")
    if not filt or node.get("Node Type") not in ("Seq Scan", "Parallel Seq Scan"):
        return None
    m = _CAST_ON_COL.search(filt)
    if not m:
        return None
    col, typ = unquote_ident(m.group(1)), m.group(2).strip().lower()
    coltype = ctx.get("columns", {}).get(qual_rel(node) or "", {}).get(col)
    if coltype is not None and (re.sub(r"\(.*\)", "", coltype), typ) in _HARMLESS_CAST:
        return None          # varchar 컬럼의 ::text 는 플랜 표기일 뿐 인덱스 사용 가능
    if coltype is None and typ == "text":
        return None          # 타입을 모르면 가장 흔한 무해한 경우(varchar)로 보고 경고하지 않음
    return Finding(
        "implicit_cast_on_column", "medium", node["Node Type"], qual_rel(node),
        f"컬럼 {col} 이 {typ} 로 형변환되어 비교됨 — 컬럼 인덱스를 사용할 수 없음",
        {"column": col, "cast_to": typ, "filter": filt},
        f"비교값의 타입을 컬럼 타입에 맞추세요 (애플리케이션 바인드 타입 / 리터럴 캐스트). "
        f"예: WHERE {col} = '123' 처럼 문자열로 넘기는지 확인.")


def rule_like_leading_wildcard(node, parent, ctx):
    filt = node.get("Filter") or ""
    m = re.search(r'(?:' + _ID + r'\s*\.\s*)?(' + _ID + r')\)?(?:::[a-z ]+)?\s*~~\*?\s*\'%', filt, re.I)
    if not m:
        return None
    col = unquote_ident(m.group(1))
    rel = qual_rel(node) or rel_label(node)
    return Finding(
        "like_leading_wildcard", "medium", node.get("Node Type", "Scan"), rel,
        f"LIKE '%...' (앞쪽 와일드카드) 검색: {col} — B-tree 인덱스 사용 불가",
        {"column": col, "filter": filt},
        f"pg_trgm GIN 인덱스: CREATE EXTENSION pg_trgm; CREATE INDEX ON {rel} USING gin ({quote_ident(col)} gin_trgm_ops). "
        f"또는 '...%' (접두 검색) 로 바꿀 수 있는지 검토.")


def rule_or_condition(node, parent, ctx):
    if node.get("Node Type") not in ("Seq Scan", "Parallel Seq Scan"):
        return None
    filt = node.get("Filter") or ""
    if " OR " not in filt:
        return None
    removed = rows_total(node, "Rows Removed by Filter")
    if "Actual Rows" in node and removed < 10000:
        return None
    return Finding(
        "or_condition_seq_scan", "low", node["Node Type"], qual_rel(node),
        "OR 조건 때문에 전체 스캔",
        {"filter": filt, "removed": removed},
        "OR 양쪽 컬럼에 각각 인덱스가 있으면 BitmapOr 로 풀림. 안 되면 UNION ALL 로 분리 검토.")


def rule_index_filter_removed(node, parent, ctx, min_rows=1000):
    """인덱스는 탔지만 인덱스 조건 뒤 Filter 로 대부분 버림 → 복합 인덱스 후보."""
    if node.get("Node Type") not in ("Index Scan", "Index Only Scan", "Bitmap Heap Scan"):
        return None
    if "Filter" not in node or "Actual Rows" not in node:
        return None
    removed = rows_total(node, "Rows Removed by Filter")
    kept = rows_total(node, "Actual Rows")
    if removed < min_rows or removed <= kept * 2:
        return None
    cond = node.get("Index Cond") or node.get("Recheck Cond") or ""
    return Finding(
        "index_then_filter", "medium", node["Node Type"], qual_rel(node),
        f"인덱스로 찾은 뒤 Filter 로 {removed:,}행을 버림 (남은 {kept:,}행)",
        {"index": node.get("Index Name"), "index_cond": cond, "filter": node.get("Filter"),
         "removed": removed, "kept": kept, "alias": node.get("Alias")},
        "인덱스 조건 컬럼 + Filter 컬럼으로 복합 인덱스 후보 → hypopg 검증.")


def rule_nested_loop_many(node, parent, ctx):
    if node.get("Node Type") != "Nested Loop" or len(node.get("Plans", [])) < 2:
        return None
    outer, inner = node["Plans"][0], node["Plans"][1]
    if "Actual Loops" in inner:
        loops = inner.get("Actual Loops", 1)
    else:
        loops = int(outer.get("Plan Rows", 0))
    if loops < 1000:
        return None
    inner_scan = inner
    while inner_scan.get("Node Type") in ("Materialize", "Memoize") and inner_scan.get("Plans"):
        inner_scan = inner_scan["Plans"][0]
    seq = inner_scan.get("Node Type") in ("Seq Scan", "Parallel Seq Scan")
    sev = "high" if seq else ("medium" if total_time(inner) > 0.3 * ctx.get("exec_ms", 1e18) else "low")
    if sev == "low":
        return None
    return Finding(
        "nested_loop_many_loops", sev, "Nested Loop", qual_rel(inner_scan),
        f"Nested Loop 안쪽({inner_scan.get('Node Type')} {inner_scan.get('Relation Name') or ''}) 을 {loops:,}번 반복"
        + (" — 안쪽이 전체 스캔" if seq else ""),
        {"loops": loops, "inner_type": inner_scan.get("Node Type"), "join_filter": node.get("Join Filter"),
         "aliases": aliases_under(node), "inner_alias": inner_scan.get("Alias"),
         "inner_filter": inner_scan.get("Filter")},
        "안쪽 테이블 조인 키에 인덱스가 없으면 인덱스 후보, 아니면 Hash Join 이 나은지 힌트 실험으로 확인.")


def rule_sort_for_limit(node, parent, ctx):
    """LIMIT 위해 전체 정렬 → (= 조건 컬럼 + 정렬 컬럼) 인덱스면 정렬 없이 앞에서 N건만 읽음.
    Limit → (Gather Merge) → Sort → (Gather) → Scan 형태를 모두 처리."""
    if node.get("Node Type") != "Limit" or not node.get("Plans"):
        return None
    sort = node["Plans"][0]
    while sort.get("Node Type") in ("Gather", "Gather Merge") and sort.get("Plans"):
        sort = sort["Plans"][0]
    if sort.get("Node Type") not in ("Sort", "Incremental Sort") or not sort.get("Plans"):
        return None
    child = sort["Plans"][0]
    while child.get("Node Type") in ("Gather", "Gather Merge") and child.get("Plans"):
        child = child["Plans"][0]
    if child.get("Node Type") not in ("Seq Scan", "Parallel Seq Scan", "Bitmap Heap Scan"):
        return None
    alias = child.get("Alias")
    keys = sort_key_columns(sort.get("Sort Key"), alias)
    if not keys:
        return None
    if "Actual Rows" in child:
        rows_in = rows_total(child, "Actual Rows")
    else:
        rows_in = int(child.get("Plan Rows", 0)) * max(1 + int(sort.get("Workers Planned", 0) or 0), 1)
    if rows_in < 1000:
        return None
    return Finding(
        "sort_for_limit", "medium", sort["Node Type"], qual_rel(child),
        f"LIMIT 을 위해 {rows_in:,}행을 정렬 (정렬 키: {', '.join(keys)})",
        {"sort_key": keys, "filter": child.get("Filter"), "rows_sorted": rows_in, "alias": alias},
        "= 조건 컬럼 + 정렬 컬럼 순서의 인덱스면 정렬 없이 앞에서 N건만 읽음 → hypopg 검증.")


def rule_sort_spill(node, parent, ctx):
    if node.get("Sort Space Type") != "Disk":
        return None
    used = node.get("Sort Space Used", 0)
    return Finding(
        "sort_spill_to_disk", "medium", node["Node Type"], None,
        f"정렬이 디스크로 스필 ({used:,} KB, {node.get('Sort Method')})",
        {"sort_space_used_kb": used, "sort_method": node.get("Sort Method")},
        "work_mem 상향 (아래 설정 실험에서 효과 확인). 정렬 대상 과다 추정 여부도 점검.")


def rule_hash_spill(node, parent, ctx):
    batches = node.get("Hash Batches", 1)
    if batches <= 1:
        return None
    orig = node.get("Original Hash Batches", batches)
    return Finding(
        "hash_spill_to_disk", "medium", node["Node Type"], None,
        f"해시가 {batches}개 배치로 디스크 스필" + (" (런타임 증가 = 과소추정)" if batches > orig else ""),
        {"hash_batches": batches, "original_hash_batches": orig},
        "work_mem 상향 (아래 설정 실험에서 효과 확인). 런타임 배치 증가면 빌드측 통계 점검.")


def rule_lossy_bitmap(node, parent, ctx):
    lossy = node.get("Lossy Heap Blocks", 0)
    if node.get("Node Type") != "Bitmap Heap Scan" or lossy <= 0:
        return None
    return Finding(
        "lossy_bitmap", "low", "Bitmap Heap Scan", qual_rel(node),
        f"비트맵이 lossy 로 전환 ({lossy:,} 블록 재검사)",
        {"lossy_blocks": lossy, "exact_blocks": node.get("Exact Heap Blocks", 0)},
        "work_mem 이 부족해 블록 단위로 뭉개짐 → work_mem 상향.")


def rule_heap_fetches(node, parent, ctx, threshold=1000):
    if node.get("Node Type") != "Index Only Scan":
        return None
    f = node.get("Heap Fetches", 0)
    if f < threshold:
        return None
    return Finding(
        "high_heap_fetches", "low", "Index Only Scan", qual_rel(node),
        f"Index Only Scan 이지만 heap fetch {f:,}회",
        {"heap_fetches": f}, "대상 테이블 VACUUM 으로 가시성 맵 갱신.")


def rule_subplan_loops(node, parent, ctx):
    if node.get("Parent Relationship") != "SubPlan":
        return None
    loops = node.get("Actual Loops", 0)
    if loops < 1000:
        return None
    return Finding(
        "correlated_subquery", "high" if loops >= 10000 else "medium", node["Node Type"], qual_rel(node),
        f"상관 서브쿼리가 {loops:,}번 실행됨 ({node.get('Subplan Name', 'SubPlan')})",
        {"loops": loops, "subplan": node.get("Subplan Name")},
        "JOIN 또는 EXISTS / LATERAL 로 재작성 검토. 서브쿼리 조건 컬럼 인덱스 확인.")


def rule_low_cache_hit(node, parent, ctx, min_blocks=10000):
    hit = node.get("Shared Hit Blocks", 0)
    read = node.get("Shared Read Blocks", 0)
    if read < min_blocks or read <= hit:
        return None
    ratio = hit / max(hit + read, 1)
    return Finding(
        "low_cache_hit", "low", node["Node Type"], qual_rel(node) or rel_label(node),
        f"캐시 히트율 {ratio*100:.0f}% (read={read:,} blk, hit={hit:,} blk)",
        {"shared_hit_blocks": hit, "shared_read_blocks": read, "hit_ratio": round(ratio, 3)},
        "콜드 캐시인지 shared_buffers 부족인지 구분 (같은 쿼리 재실행 시 read 비교).")


RULES = [
    rule_bad_estimate, rule_seq_scan_inefficient, rule_function_on_column, rule_implicit_cast,
    rule_like_leading_wildcard, rule_or_condition, rule_index_filter_removed, rule_nested_loop_many,
    rule_sort_for_limit, rule_sort_spill, rule_hash_spill, rule_lossy_bitmap, rule_heap_fetches,
    rule_subplan_loops, rule_low_cache_hit,
]


def root_findings(root: dict) -> list:
    out = []
    et, pt = root.get("Execution Time"), root.get("Planning Time")
    if et is not None and pt is not None and pt > 50 and pt > et:
        out.append(Finding(
            "planning_heavy", "low", "Query", None,
            f"계획 시간({pt:.0f} ms) 이 실행 시간({et:.0f} ms) 보다 김",
            {"planning_ms": pt, "execution_ms": et},
            "자주 실행되는 쿼리면 prepared statement 사용. 파티션이 매우 많으면 프루닝 조건 확인."))
    jit = root.get("JIT") or {}
    jt = (jit.get("Timing") or {}).get("Total")
    if isinstance(jt, (int, float)) and et and jt > 0.2 * et and jt > 50:
        out.append(Finding(
            "jit_overhead", "medium", "Query", None,
            f"JIT 컴파일 {jt:.0f} ms (실행의 {jt/et*100:.0f}%)",
            {"jit_ms": jt, "execution_ms": et},
            "짧은 OLTP 쿼리면 jit = off 또는 jit_above_cost 상향 (아래 설정 실험에서 확인)."))
    return out


# ---------------------------------------------------------------------------
# 후처리 (파티션 묶기 / 중복 제거)
# ---------------------------------------------------------------------------
_PARENT_CACHE: dict = {}


def partition_parent(dsn: Optional[str], relname: str) -> Optional[str]:
    if not dsn or not relname:
        return None
    if relname in _PARENT_CACHE:
        return _PARENT_CACHE[relname]
    try:
        out = run_psql(dsn, "SELECT n.nspname || '|' || c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE c.oid = pg_partition_root(to_regclass(:'r'));",
                       {"r": relname}).strip()
        parent = sqlguard.qualify(*out.split("|", 1)) if "|" in out else None
        parent = parent if parent and parent != relname else None
    except Exception:
        parent = None
    _PARENT_CACHE[relname] = parent
    return parent


def _common_prefix_parent(names: list) -> str:
    import os
    pref = os.path.commonprefix(names).rstrip("_0123456789")
    return pref.rstrip("_") or names[0]


def collapse_partitions(findings: list, dsn: Optional[str]) -> list:
    seq = [f for f in findings if f.rule == "seq_scan_inefficient" and f.relation]
    others = [f for f in findings if not (f.rule == "seq_scan_inefficient" and f.relation)]
    groups: dict = {}
    for f in seq:
        groups.setdefault(tuple(extract_filter_columns(f.evidence.get("filter"))), []).append(f)
    merged = []
    for cols, grp in groups.items():
        if len(grp) == 1:
            merged.append(grp[0])
            continue
        rels = [g.relation for g in grp]
        parent = partition_parent(dsn, rels[0]) or _common_prefix_parent(rels)
        scanned = sum(g.evidence.get("scanned", 0) for g in grp)
        removed = sum(g.evidence.get("removed", 0) for g in grp)
        merged.append(Finding(
            "seq_scan_inefficient", "medium", "Seq Scan", parent,
            f"{len(grp)}개 파티션에서 총 {scanned:,}행 스캔 후 {removed:,}행 버림 (파티션 프루닝 실패)",
            {"scanned": scanned, "removed": removed, "partitions": len(grp), "parent": parent,
             "filter": grp[0].evidence.get("filter"), "alias": grp[0].evidence.get("alias")},
            f"부모 테이블에 인덱스 1개(CREATE INDEX ON {parent} (...)) 면 자식에 자동 전파. "
            f"또는 WHERE 에 파티션 키 조건 추가."))
    return others + merged


def dedupe_findings(findings: list) -> list:
    seen, out = set(), []
    for f in findings:
        key = (f.rule, f.relation, json.dumps(f.evidence, sort_keys=True, default=str))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# 후보 도출
# ---------------------------------------------------------------------------
def _join_key_for_alias(join_filter: Optional[str], alias: Optional[str]) -> list:
    """Join Filter "(o.customer_id = c.id)" 에서 alias 쪽 컬럼."""
    if not join_filter or not alias:
        return []
    cols = []
    for m in re.finditer(r'(?<![\w"$.])(' + _ID + r')\s*\.\s*(' + _ID + r')', normalize_cond(join_filter)):
        a, c = unquote_ident(m.group(1)), unquote_ident(m.group(2))
        if a == alias and c not in cols:
            cols.append(c)
    return cols


def derive_index_candidates(findings: list) -> list:
    cands, seen = [], set()
    # 함수 / 형변환 / 앞쪽 와일드카드로 감싸진 컬럼은 B-tree 인덱스를 못 탐 → 후보에서 제외
    unusable = {(f.relation, f.evidence.get("column")) for f in findings
                if f.rule in ("function_on_column", "implicit_cast_on_column", "like_leading_wildcard")}

    def add(rel, cols, reason):
        cols = [c for c in cols if (rel, _col_name(c)) not in unusable]
        if not rel or not cols:
            return
        key = (rel, tuple(cols))
        if key in seen:
            return
        seen.add(key)
        cands.append(IndexCandidate(rel, list(cols), reason,
                                    f"CREATE INDEX ON {rel} ({', '.join(_col_sql(c) for c in cols)})"))

    for f in findings:
        ev = f.evidence
        rel = ev.get("parent") or f.relation
        if f.rule == "seq_scan_inefficient":
            cols = extract_filter_columns(ev.get("filter"))
            eq = [c for c in equality_columns(ev.get("filter")) if c in cols]
            ordered = eq + [c for c in cols if c not in eq]      # = 컬럼을 앞으로
            add(rel, ordered, f"{rel} 전체 스캔에서 {ev.get('removed', 0):,}행을 필터로 버림")
            if len(ordered) > 1:
                add(rel, ordered[:1], f"{rel} 단일 컬럼 대안 ({ordered[0]})")
        elif f.rule == "index_then_filter":
            icols = extract_filter_columns(ev.get("index_cond"))
            fcols = [c for c in extract_filter_columns(ev.get("filter")) if c not in icols]
            add(rel, icols + fcols, f"{ev.get('index')} 로 찾은 뒤 {ev.get('removed', 0):,}행을 Filter 로 버림")
        elif f.rule == "sort_for_limit":
            eq = equality_columns(ev.get("filter"))
            keys = [k for k in ev.get("sort_key", []) if _col_name(k) not in eq]
            add(rel, eq + keys, f"LIMIT 을 위한 {ev.get('rows_sorted', 0):,}행 정렬 제거")
        elif f.rule == "function_on_column" and ev.get("function") and ev.get("column"):
            # 쿼리를 못 고칠 때의 대안: 표현식 인덱스 — hypopg 로 실제 채택되는지 검증 (함수가 IMMUTABLE 이어야 생성 가능)
            add(rel, [f"({ev['function']}({quote_ident(ev['column'])}))"],
                f"컬럼에 {ev['function']}() 적용 — 쿼리 재작성이 안 될 때의 표현식 인덱스 대안")
        elif f.rule == "nested_loop_many_loops" and f.relation:
            jcols = _join_key_for_alias(ev.get("join_filter"), ev.get("inner_alias")) \
                or extract_filter_columns(ev.get("inner_filter"))
            add(f.relation, jcols, f"Nested Loop 안쪽 {ev.get('loops', 0):,}회 반복 — 조인 키 인덱스")
    return cands


def derive_hint_experiments(findings: list, root_plan: dict) -> list:
    """findings → pg_hint_plan 실험 목록. 실제 효과는 hint_verifier 가 측정."""
    exps, seen = [], set()
    unusable = {(f.relation, f.evidence.get("column")) for f in findings
                if f.rule in ("function_on_column", "implicit_cast_on_column", "like_leading_wildcard")}

    def add(hint, target, reason, expect, **extra):
        if hint in seen:
            return
        seen.add(hint)
        exps.append(HintExperiment(hint, target, reason, expect, extra))

    for f in findings:
        ev = f.evidence
        if f.rule == "nested_loop_many_loops" and len(ev.get("aliases", [])) >= 2:
            al = " ".join(ev["aliases"])
            add(f"HashJoin({al})", "Nested Loop → Hash Join", f.message, "Hash Join")
            add(f"MergeJoin({al})", "Nested Loop → Merge Join", f.message, "Merge Join")
        elif f.rule in ("seq_scan_inefficient", "sort_for_limit") and ev.get("alias"):
            a = ev["alias"]
            cols = [c for c in extract_filter_columns(ev.get("filter")) + [k.split()[0] for k in ev.get("sort_key", [])]
                    if (f.relation, c) not in unusable]
            if not cols:
                continue
            # 기존 인덱스가 있는데 안 타는 경우만 의미 있음 → hint_verifier 가 인덱스 유무를 확인 후 실행
            add(f"IndexScan({a})", f"{a}: 전체 스캔 → 기존 인덱스 사용", f.message, "Index Scan",
                alias=a, rel=f.relation, cols=cols, need_index=True)
            add(f"BitmapScan({a})", f"{a}: 전체 스캔 → Bitmap 스캔", f.message, "Bitmap Heap Scan",
                alias=a, rel=f.relation, cols=cols, need_index=True)
        elif f.rule == "bad_estimate" and len(ev.get("aliases", [])) >= 2 and ev.get("actual_rows"):
            al = " ".join(ev["aliases"])
            add(f"Rows({al} #{ev['actual_rows']})", f"조인 추정 행수를 실제값({ev['actual_rows']:,})으로 보정",
                f.message, "", rows_fix=True)
    # 큰 해시 조인이 스필하면 Nested Loop + 인덱스가 나을 수도 → 대칭 실험은 노이즈가 커서 넣지 않음
    return exps


# ---------------------------------------------------------------------------
# 분석
# ---------------------------------------------------------------------------
def _reltuples(dsn: Optional[str], plan: dict) -> dict:
    """추정 플랜(ANALYZE 없음)일 때 테이블 행수 참고용."""
    rels = sorted({qual_rel(n) for n in walk(plan) if qual_rel(n)})
    if not dsn or not rels:
        return {}
    try:
        out = run_psql(dsn, "SELECT r, c.reltuples::bigint FROM jsonb_array_elements_text(:'rels'::jsonb) r "
                            "JOIN pg_class c ON c.oid = to_regclass(r);", {"rels": json.dumps(rels)})
        return {ln.rsplit("|", 1)[0]: int(float(ln.rsplit("|", 1)[1])) for ln in out.splitlines() if "|" in ln}
    except Exception:
        return {}


def catalog_columns(dsn: Optional[str], rels: list) -> dict:
    """{rel: {column: type}} — 후보 컬럼 검증 / 캐스트 판단용. 실패하면 {}."""
    if not dsn or not rels:
        return {}
    try:
        out = run_psql(dsn, """
SELECT coalesce(jsonb_object_agg(r, cols), '{}') FROM (
  SELECT r, jsonb_object_agg(a.attname, format_type(a.atttypid, a.atttypmod)) AS cols
  FROM jsonb_array_elements_text(:'rels'::jsonb) r
  JOIN pg_attribute a ON a.attrelid = to_regclass(r) AND a.attnum > 0 AND NOT a.attisdropped
  GROUP BY r) x;""", {"rels": json.dumps(sorted(rels))})
        return json.loads(out.strip() or "{}")
    except Exception:
        return {}


def validate_candidates(cands: list, columns: dict) -> list:
    """카탈로그에 없는 컬럼(파싱 오인)을 후보에서 제거. 카탈로그 정보가 없으면 그대로."""
    out = []
    for c in cands:
        cols = columns.get(c.relation)
        if cols is None:
            out.append(c)
            continue
        keep = [x for x in c.columns if x.startswith("(") or _col_name(x) in cols]
        if not keep:
            continue
        if keep != c.columns:
            c = IndexCandidate(c.relation, keep, c.reason,
                               f"CREATE INDEX ON {c.relation} ({', '.join(_col_sql(x) for x in keep)})")
        if all(c.ddl != o.ddl for o in out):
            out.append(c)
    return out


def analyze_plan(root: dict, dsn: Optional[str] = None) -> dict:
    plan = root["Plan"]
    rels = sorted({qual_rel(n) for n in walk(plan) if qual_rel(n)})
    columns = catalog_columns(dsn, rels)
    ctx = {"exec_ms": root.get("Execution Time") or 0, "columns": columns,
           "reltuples": {} if has_actual(plan) else _reltuples(dsn, plan)}
    findings, hotspots = root_findings(root), []
    for node, parent in walk_parent(plan):
        for rule in RULES:
            r = rule(node, parent, ctx)
            if r:
                findings.append(r)
        if "Actual Total Time" in node:
            hotspots.append({
                "node_type": node["Node Type"], "relation": qual_rel(node) or rel_label(node),
                "exclusive_ms": round(exclusive_time(node), 2), "total_ms": round(total_time(node), 2),
                "rows": int(node.get("Actual Rows", 0) * node.get("Actual Loops", 1))})
    findings = dedupe_findings(findings)
    findings = collapse_partitions(findings, dsn)
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    hotspots.sort(key=lambda h: h["exclusive_ms"], reverse=True)
    return {
        "planning_time_ms": root.get("Planning Time"),
        "execution_time_ms": root.get("Execution Time"),
        "analyzed": has_actual(plan),
        "settings": root.get("Settings") or {},
        "relations": sorted({qual_rel(n) for n in walk(plan) if qual_rel(n)}),
        "findings": [asdict(f) for f in findings],
        "hotspots": hotspots[:5],
        "index_candidates": [asdict(c) for c in validate_candidates(derive_index_candidates(findings), columns)],
        "hint_experiments": [asdict(h) for h in derive_hint_experiments(findings, plan)],
    }


SEV = {"high": "[!!]", "medium": "[! ]", "low": "[  ]", "info": "[i ]"}


def format_report(res: dict) -> str:
    out = []
    et, pt = res.get("execution_time_ms"), res.get("planning_time_ms")
    if et is not None:
        out += [f"실행 {et:.1f} ms / 계획 {pt:.1f} ms", ""]
    elif pt is not None:
        out += [f"(추정 플랜 — 실제 실행 안 함. 계획 {pt:.1f} ms)", ""]
    if res.get("settings"):
        out += ["기본값과 다른 설정: " + ", ".join(f"{k}={v}" for k, v in res["settings"].items()), ""]
    out.append("=== 진단 ===")
    if not res["findings"]:
        out.append("  규칙 기준 특이사항 없음.")
    for f in res["findings"]:
        rel = f" @{f['relation']}" if f["relation"] else ""
        out += [f"{SEV.get(f['severity'], '[  ]')} {f['rule']}{rel}",
                f"      {f['message']}", f"      → {f['suggestion']}"]
    if res["hotspots"]:
        out += ["", "=== 시간 핫스팟 (고유 시간) ==="]
        for h in res["hotspots"]:
            rel = f" {h['relation']}" if h["relation"] else ""
            out.append(f"  {h['exclusive_ms']:>9.1f} ms  {h['node_type']}{rel} (rows={h['rows']:,})")
    if res["index_candidates"]:
        out += ["", "=== 인덱스 후보 (hypopg 검증 대상) ==="]
        for c in res["index_candidates"]:
            out += [f"  {c['ddl']};", f"      근거: {c['reason']}"]
    if res.get("hint_experiments"):
        out += ["", "=== 실행계획 대안 (pg_hint_plan 실험 대상) ==="]
        for h in res["hint_experiments"]:
            out.append(f"  /*+ {h['hint']} */   {h['target']}")
    return "\n".join(out)
