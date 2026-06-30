#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyzer.py  —  쿼리 진단 엔진 (EXPLAIN 플랜 분석, 1계층)

하는 일
-------
  EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) 결과를 파싱
    → 플랜 트리를 순회하며 규칙 적용 → findings(진단)
    → 시간 핫스팟(고유 시간) 식별 → 범인 노드 지목
    → findings에서 인덱스 후보 DDL 도출
    → 사람용 리포트 / JSON 출력

사용법
-----
  # DB에 직접 접속 (psql 필요)
  python3 analyzer.py --dsn "dbname=postgres" --sql "SELECT * FROM orders WHERE customer_id = 12345"

  # 미리 떠둔 플랜 JSON 파일로 (DB 없이 테스트)
  python3 analyzer.py --plan-file plan.json

  # JSON으로 출력 (나중에 verifier/LLM 계층 입력용)
  python3 analyzer.py --plan-file plan.json --format json
"""

from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from typing import Iterator, Optional


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


# ---------------------------------------------------------------------------
# 진단 결과 모델 (공용 계약의 일부)
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    rule: str
    severity: str
    node_type: str
    relation: Optional[str]
    message: str
    evidence: dict       # 판단 근거 수치 (나중에 LLM 환각 차단의 핵심)
    suggestion: str


@dataclass
class IndexCandidate:
    relation: str
    columns: list           # 후보 컬럼
    reason: str
    ddl: str                # CREATE INDEX ... (verifier가 hypopg로 검증할 대상)


# ---------------------------------------------------------------------------
# 플랜 수집
# ---------------------------------------------------------------------------
def get_plan_from_db(dsn: str, sql: str, analyze: bool = True) -> dict:
    opts = "ANALYZE, BUFFERS, VERBOSE, SETTINGS, FORMAT JSON" if analyze \
        else "VERBOSE, SETTINGS, FORMAT JSON"
    explain = f"EXPLAIN ({opts}) {sql}"

    first = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    is_dml = first in {"INSERT", "UPDATE", "DELETE", "MERGE"}
    # ANALYZE는 실제 실행 → DML이면 롤백 트랜잭션으로 감싼다 (데이터 보호)
    script = f"BEGIN;\n{explain};\nROLLBACK;\n" if (analyze and is_dml) else f"{explain};\n"

    proc = subprocess.run(
        ["psql", dsn, "-At", "-X", "-q"],
        input=script, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql 실패:\n{proc.stderr.strip()}")

    out = proc.stdout
    s, e = out.find("["), out.rfind("]")
    if s == -1 or e == -1:
        raise RuntimeError(f"플랜 JSON을 찾지 못함. psql 출력:\n{out}")
    return json.loads(out[s:e + 1])[0]


def get_plan_text(dsn: str, sql: str, analyze: bool = False) -> str:
    """텍스트(트리) 형식 EXPLAIN 출력을 psql 그대로 반환 (로그/표시용).

    JSON 플랜에서는 트리 표현을 안전하게 복원할 수 없어, 표시용 텍스트는
    psql 로 다시 받아온다.
      analyze=False : 추정 플랜만(실행 안 함) — '일반 쿼리에 EXPLAIN 달았을 때' 모습
      analyze=True  : ANALYZE 실행(실제 시간 포함). DML 은 ROLLBACK 으로 감쌈
    """
    opts = "(ANALYZE, BUFFERS) " if analyze else ""
    explain = f"EXPLAIN {opts}{sql}"

    first = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    is_dml = first in {"INSERT", "UPDATE", "DELETE", "MERGE"}
    script = f"BEGIN;\n{explain};\nROLLBACK;\n" if (analyze and is_dml) else f"{explain};\n"

    proc = subprocess.run(
        ["psql", dsn, "-At", "-X", "-q"],
        input=script, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql 실패:\n{proc.stderr.strip()}")
    # BEGIN/ROLLBACK 명령 태그가 섞이면 제거 (DML+analyze 경로)
    lines = [ln for ln in proc.stdout.splitlines()
             if ln.strip() not in {"BEGIN", "ROLLBACK", "COMMIT"}]
    return "\n".join(lines).rstrip("\n")


def load_plan_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    # 파일 앞에 SET, NOTICE 등 잡음이 섞여 있어도 JSON 배열만 뽑는다
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1:
        raise RuntimeError(f"플랜 JSON을 찾지 못함. 파일 내용:\n{text[:200]}")
    data = json.loads(text[s:e + 1])
    return data[0] if isinstance(data, list) else data


# ---------------------------------------------------------------------------
# 트리 순회 + 시간/행수 계산
# ---------------------------------------------------------------------------
def walk(node: dict) -> Iterator[dict]:
    yield node
    for child in node.get("Plans", []):
        yield from walk(child)


def total_time(node: dict) -> float:
    """이 노드의 누적 시간(모든 루프 합)."""
    return node.get("Actual Total Time", 0.0) * node.get("Actual Loops", 1)


def exclusive_time(node: dict) -> float:
    """고유 시간 = 내 누적 − 자식 누적 합. (진짜 범인 노드 식별)"""
    kids = sum(total_time(c) for c in node.get("Plans", []))
    return max(total_time(node) - kids, 0.0)


def rel_label(node: dict) -> Optional[str]:
    return node.get("Relation Name") or node.get("Index Name")


def node_loops(node: dict) -> int:
    """Actual Loops = 반복/워커 수. 병렬 노드면 워커+리더 수가 됨."""
    return node.get("Actual Loops", 1) or 1


def rows_total(node: dict, key: str) -> int:
    """루프당 평균값(Actual Rows, Rows Removed by Filter 등)을 전체값으로 환산."""
    return round(node.get(key, 0) * node_loops(node))


# 필터 문자열에서 컬럼명 추출:  "(customer_id = 12345)" -> ["customer_id"]
_COL = re.compile(r"\b([a-z_][a-z0-9_]*)\b\s*(?:=|<|>|<=|>=|<>|!=|IS|LIKE|IN|BETWEEN)")


def extract_filter_columns(expr: Optional[str]) -> list:
    if not expr:
        return []
    cols, seen = [], set()
    for m in _COL.finditer(expr):
        c = m.group(1)
        if c.lower() in {"and", "or", "not", "is", "null", "true", "false"}:
            continue
        if c not in seen:
            seen.add(c)
            cols.append(c)
    return cols


# ---------------------------------------------------------------------------
# 규칙들 (각 규칙: node -> Finding | None). 새 규칙은 함수 추가 후 RULES에 등록.
# ---------------------------------------------------------------------------
def rule_bad_estimate(node: dict) -> Optional[Finding]:
    if "Actual Rows" not in node:
        return None
    est = max(node.get("Plan Rows", 0), 0)
    act = max(node.get("Actual Rows", 0), 0)
    lo, hi = max(min(est, act), 1), max(est, act, 1)
    ratio = hi / lo
    if ratio < 10:
        return None
    return Finding(
        rule="bad_estimate",
        severity="high" if ratio >= 100 else "medium",
        node_type=node["Node Type"], relation=rel_label(node),
        message=f"추정 {est}행 vs 실제 {act}행 ({ratio:.0f}배 차이)",
        evidence={"plan_rows": est, "actual_rows": act, "ratio": round(ratio, 1)},
        suggestion="해당 테이블 ANALYZE 실행. 여러 컬럼 조건이 얽혀 있으면 "
                   "CREATE STATISTICS로 상관관계 통계 검토.",
    )


def rule_seq_scan_inefficient(node: dict, min_rows: int = 1000) -> Optional[Finding]:
    if node.get("Node Type") != "Seq Scan" or "Filter" not in node:
        return None
    # 병렬 노드 대응: 루프(워커) 수를 곱해 전체 행수로 환산
    removed = rows_total(node, "Rows Removed by Filter")
    kept = rows_total(node, "Actual Rows")
    scanned = removed + kept
    if scanned < min_rows or removed <= kept * 2:  # 작은 테이블/저선택도는 정상
        return None
    return Finding(
        rule="seq_scan_inefficient", severity="medium",
        node_type="Seq Scan", relation=rel_label(node),
        message=f"{scanned:,}행 스캔 후 {removed:,}행을 필터로 버림",
        evidence={"scanned": scanned, "removed": removed, "kept": kept,
                  "workers": node_loops(node), "filter": node.get("Filter")},
        suggestion="Filter 컬럼에 인덱스 후보. hypopg로 플래너 채택 여부를 먼저 검증.",
    )


# Filter 에서 "함수(컬럼)" 형태를 잡는다: date(created_at), lower(t.name) 등
# 컬럼 앞에 테이블 별칭이 붙는 경우(VERBOSE)도 허용: date(orders_1.created_at)
_FUNC_ON_COL = re.compile(
    r"\b([a-z_]{2,})\s*\(\s*(?:[a-z_][a-z0-9_]*\.)?([a-z_][a-z0-9_]*)\s*\)")
# 함수가 아니거나(논리/구문) 안티패턴이 아닌 키워드는 제외
_FUNC_SKIP = {"not", "any", "all", "in", "exists", "and", "or"}


def rule_function_on_column(node: dict) -> Optional[Finding]:
    """WHERE 절 컬럼에 함수가 씌워져(예: date(created_at)=...) 인덱스/파티션
    프루닝을 무효화하는 안티패턴을 감지한다."""
    filt = node.get("Filter")
    if not filt:
        return None
    m = _FUNC_ON_COL.search(filt)
    if not m:
        return None
    func, col = m.group(1), m.group(2)
    if func in _FUNC_SKIP:
        return None
    removed = rows_total(node, "Rows Removed by Filter")
    rel = rel_label(node)
    return Finding(
        rule="function_on_column", severity="medium",
        node_type=node.get("Node Type", "Scan"), relation=rel,
        message=f"Filter에서 컬럼에 함수 적용: {func}({col}) — 일반 인덱스/파티션 프루닝 무효화",
        evidence={"function": func, "column": col, "removed": removed,
                  "filter": filt},
        suggestion=(f"{func}({col}) 처럼 컬럼에 함수를 씌우면 인덱스를 타지 못하고 "
                    f"파티션 프루닝도 막힙니다. 범위 조건으로 재작성하세요 "
                    f"(예: date(col)=X → col >= X AND col < X + 1일). "
                    f"불가피하면 표현식 인덱스: CREATE INDEX ON {rel} ({func}({col}))."),
    )


def rule_sort_spill(node: dict) -> Optional[Finding]:
    if node.get("Sort Space Type") != "Disk":
        return None
    used = node.get("Sort Space Used", 0)
    return Finding(
        rule="sort_spill_to_disk", severity="medium",
        node_type=node["Node Type"], relation=None,
        message=f"정렬이 디스크로 스필 ({used:,} KB, {node.get('Sort Method')})",
        evidence={"sort_space_used_kb": used, "sort_method": node.get("Sort Method")},
        suggestion="work_mem 상향(세션/쿼리 단위). 정렬 대상이 과다 추정된 건 아닌지도 점검.",
    )


def rule_hash_spill(node: dict) -> Optional[Finding]:
    batches = node.get("Hash Batches", 1)
    if batches <= 1:
        return None
    orig = node.get("Original Hash Batches", batches)
    grew = batches > orig
    return Finding(
        rule="hash_spill_to_disk", severity="medium",
        node_type=node["Node Type"], relation=None,
        message=f"해시가 {batches}개 배치로 디스크 스필" + (" (런타임 증가=과소추정)" if grew else ""),
        evidence={"hash_batches": batches, "original_hash_batches": orig},
        suggestion="work_mem 상향. 런타임 배치 증가가 있으면 빌드측 통계도 점검.",
    )


def rule_heap_fetches(node: dict, threshold: int = 1000) -> Optional[Finding]:
    if node.get("Node Type") != "Index Only Scan":
        return None
    f = node.get("Heap Fetches", 0)
    if f < threshold:
        return None
    return Finding(
        rule="high_heap_fetches", severity="low",
        node_type="Index Only Scan", relation=rel_label(node),
        message=f"Index Only Scan이지만 heap fetch {f:,}회",
        evidence={"heap_fetches": f},
        suggestion="대상 테이블 VACUUM으로 가시성 맵 갱신.",
    )


def rule_low_cache_hit(node: dict, min_blocks: int = 10000) -> Optional[Finding]:
    hit = node.get("Shared Hit Blocks", 0)
    read = node.get("Shared Read Blocks", 0)
    if read < min_blocks or read <= hit:
        return None
    ratio = hit / max(hit + read, 1)
    return Finding(
        rule="low_cache_hit", severity="low",
        node_type=node["Node Type"], relation=rel_label(node),
        message=f"캐시 히트율 {ratio*100:.0f}% (read={read:,} blk, hit={hit:,} blk)",
        evidence={"shared_hit_blocks": hit, "shared_read_blocks": read,
                  "hit_ratio": round(ratio, 3)},
        suggestion="콜드 캐시 vs shared_buffers 부족 구분 필요(같은 쿼리 재실행 시 read 비교).",
    )


RULES = [
    rule_bad_estimate, rule_seq_scan_inefficient, rule_function_on_column,
    rule_sort_spill, rule_hash_spill, rule_heap_fetches, rule_low_cache_hit,
]


# ---------------------------------------------------------------------------
# 파티션 인식: 같은 필터로 여러 파티션을 스캔하면 하나로 묶고 부모에 인덱스 추천
# ---------------------------------------------------------------------------
_PARENT_CACHE: dict = {}


def partition_parent(dsn: str | None, relname: str) -> str | None:
    """파티션 자식의 최상위 부모 테이블명을 조회 (pg_partition_root).
    DSN 없으면 None."""
    if not dsn or not relname:
        return None
    if relname in _PARENT_CACHE:
        return _PARENT_CACHE[relname]
    try:
        out = subprocess.run(
            ["psql", dsn, "-At", "-X", "-q", "-c",
             f"SELECT pg_partition_root('{relname}'::regclass)::text"],
            capture_output=True, text=True,
        ).stdout.strip()
        parent = out if out and out != relname else None
    except Exception:
        parent = None
    _PARENT_CACHE[relname] = parent
    return parent


def _common_prefix_parent(names: list[str]) -> str:
    """DSN 없을 때 휴리스틱: 공통 접두에서 파티션 접미(_날짜/_숫자)를 떼어 부모 추정."""
    import os
    pref = os.path.commonprefix(names).rstrip("_0123456789")
    return pref.rstrip("_") or names[0]


def collapse_partitions(findings: list, dsn: str | None) -> list:
    """seq_scan_inefficient 들을 필터 기준으로 묶어, 여러 파티션이면 부모 1건으로 합침."""
    seq = [f for f in findings if f.rule == "seq_scan_inefficient" and f.relation]
    others = [f for f in findings if not (f.rule == "seq_scan_inefficient" and f.relation)]

    groups: dict = {}
    for f in seq:
        cols = tuple(extract_filter_columns(f.evidence.get("filter")))
        groups.setdefault(cols, []).append(f)

    merged = []
    for cols, grp in groups.items():
        if len(grp) == 1:
            merged.append(grp[0])
            continue
        # 여러 파티션이 같은 필터로 스캔됨 = 파티션 프루닝 실패
        rels = [g.relation for g in grp]
        parent = partition_parent(dsn, rels[0]) or _common_prefix_parent(rels)
        scanned = sum(g.evidence.get("scanned", 0) for g in grp)
        removed = sum(g.evidence.get("removed", 0) for g in grp)
        merged.append(Finding(
            rule="seq_scan_inefficient", severity="medium",
            node_type="Seq Scan", relation=parent,
            message=f"{len(grp)}개 파티션에서 총 {scanned:,}행 스캔 후 {removed:,}행 버림 "
                    f"(파티션 프루닝 실패)",
            evidence={"scanned": scanned, "removed": removed,
                      "partitions": len(grp), "parent": parent,
                      "filter": grp[0].evidence.get("filter")},
            suggestion=f"WHERE에 파티션 키가 없어 모든 파티션을 스캔함. 부모 테이블에 "
                       f"인덱스 1개(CREATE INDEX ON {parent} (...))를 만들면 자식 파티션에 "
                       f"자동 전파됨. 또는 쿼리에 파티션 키 조건을 추가해 프루닝 유도.",
        ))
    return others + merged


def dedupe_findings(findings: list) -> list:
    """동일한 (rule, relation, evidence) 중복 제거. (병렬 플랜에서 부모/자식이
    같은 버퍼 수치를 중복 보고하는 경우 등)"""
    seen, out = set(), []
    for f in findings:
        key = (f.rule, f.relation, json.dumps(f.evidence, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def collapse_function_on_column(findings: list, dsn: str | None) -> list:
    """function_on_column 이 여러 파티션에서 중복 발동하면 (function, column)
    기준으로 묶어 부모 1건으로 합친다."""
    fns = [f for f in findings if f.rule == "function_on_column"]
    if len(fns) <= 1:
        return findings
    others = [f for f in findings if f.rule != "function_on_column"]

    groups: dict = {}
    for f in fns:
        key = (f.evidence.get("function"), f.evidence.get("column"))
        groups.setdefault(key, []).append(f)

    merged = []
    for (func, col), grp in groups.items():
        if len(grp) == 1:
            merged.append(grp[0])
            continue
        rels = [g.relation for g in grp]
        parent = partition_parent(dsn, rels[0]) or _common_prefix_parent(rels)
        removed = sum(g.evidence.get("removed", 0) for g in grp)
        merged.append(Finding(
            rule="function_on_column", severity="medium",
            node_type="Seq Scan", relation=parent,
            message=f"{len(grp)}개 파티션 Filter에서 컬럼에 함수 적용: {func}({col}) "
                    f"— 인덱스/파티션 프루닝 무효화",
            evidence={"function": func, "column": col, "removed": removed,
                      "partitions": len(grp), "parent": parent},
            suggestion=(f"{func}({col}) 함수 조건은 인덱스를 못 타고 파티션 프루닝도 "
                        f"막습니다. 범위 조건으로 재작성하세요 "
                        f"(예: date(col)=X → col >= X AND col < X + 1일)."),
        ))
    return others + merged


# ---------------------------------------------------------------------------
# findings -> 인덱스 후보 도출 (verifier(③)가 hypopg로 검증할 대상)
# ---------------------------------------------------------------------------
def derive_index_candidates(findings: list[Finding]) -> list[IndexCandidate]:
    cands, seen = [], set()
    for f in findings:
        if f.rule != "seq_scan_inefficient" or not f.relation:
            continue
        cols = extract_filter_columns(f.evidence.get("filter"))
        if not cols:
            continue
        # 파티션이 묶인 경우 부모 테이블에 후보 생성
        rel = f.evidence.get("parent") or f.relation
        key = (rel, tuple(cols))
        if key in seen:
            continue
        seen.add(key)
        cands.append(IndexCandidate(
            relation=rel, columns=cols,
            reason=f"{rel} Seq Scan에서 {f.evidence.get('removed', 0):,}행을 필터로 버림",
            ddl=f"CREATE INDEX ON {rel} ({', '.join(cols)})",
        ))
    return cands


# ---------------------------------------------------------------------------
# 분석 실행
# ---------------------------------------------------------------------------
def analyze_plan(root: dict, dsn: str | None = None) -> dict:
    plan = root["Plan"]
    findings, hotspots = [], []
    for node in walk(plan):
        for rule in RULES:
            r = rule(node)
            if r:
                findings.append(r)
        if "Actual Total Time" in node:
            hotspots.append({
                "node_type": node["Node Type"], "relation": rel_label(node),
                "exclusive_ms": round(exclusive_time(node), 2),
                "total_ms": round(total_time(node), 2),
                "rows": int(node.get("Actual Rows", 0) * node.get("Actual Loops", 1)),
            })

    # 후처리: 파티션 스캔 묶기 + 중복 제거
    findings = dedupe_findings(findings)
    findings = collapse_partitions(findings, dsn)
    findings = collapse_function_on_column(findings, dsn)

    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    hotspots.sort(key=lambda h: h["exclusive_ms"], reverse=True)
    candidates = derive_index_candidates(findings)

    return {
        "planning_time_ms": root.get("Planning Time"),
        "execution_time_ms": root.get("Execution Time"),
        "findings": [asdict(f) for f in findings],
        "hotspots": hotspots[:5],
        "index_candidates": [asdict(c) for c in candidates],
    }


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
SEV = {"high": "[!!]", "medium": "[! ]", "low": "[  ]", "info": "[i ]"}


def format_report(res: dict) -> str:
    out = []
    et, pt = res.get("execution_time_ms"), res.get("planning_time_ms")
    if et is not None:
        out += [f"실행 {et:.1f} ms / 계획 {pt:.1f} ms", ""]

    out.append("=== 진단 ===")
    if not res["findings"]:
        out.append("  규칙 기준 특이사항 없음.")
    for f in res["findings"]:
        rel = f" @{f['relation']}" if f["relation"] else ""
        out += [f"{SEV.get(f['severity'], '[  ]')} {f['rule']}{rel}",
                f"      {f['message']}",
                f"      → {f['suggestion']}"]

    if res["hotspots"]:
        out += ["", "=== 시간 핫스팟 (고유 시간) ==="]
        for h in res["hotspots"]:
            rel = f" {h['relation']}" if h["relation"] else ""
            out.append(f"  {h['exclusive_ms']:>9.1f} ms  {h['node_type']}{rel} "
                       f"(rows={h['rows']:,})")

    if res["index_candidates"]:
        out += ["", "=== 인덱스 후보 (verifier로 검증 대상) ==="]
        for c in res["index_candidates"]:
            out += [f"  {c['ddl']};", f"      근거: {c['reason']}"]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="규칙엔진 기반 PostgreSQL 쿼리 튜닝 (1계층)")
    ap.add_argument("--dsn", help='psql DSN, 예: "dbname=postgres"')
    ap.add_argument("--sql", help="분석할 SQL (--dsn과 함께)")
    ap.add_argument("--plan-file", help="EXPLAIN JSON 파일")
    ap.add_argument("--no-analyze", action="store_true", help="ANALYZE 없이 추정만(안전/빠름)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    try:
        if args.plan_file:
            root = load_plan_file(args.plan_file)
        elif args.dsn and args.sql:
            root = get_plan_from_db(args.dsn, args.sql, analyze=not args.no_analyze)
        else:
            ap.error("--plan-file 또는 (--dsn 과 --sql)이 필요합니다.")
            return 2
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    res = analyze_plan(root, dsn=getattr(args, "dsn", None))
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.format == "json"
          else format_report(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
