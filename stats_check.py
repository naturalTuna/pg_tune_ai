#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stats_check.py  —  플랜에 나온 테이블의 통계 / vacuum 상태 점검

  플래너 오판의 상당수는 오래된 통계에서 온다. 쿼리에 쓰인 테이블에 대해
    - ANALYZE 한 번도 안 됨 / 마지막 ANALYZE 이후 변경 비율
    - dead tuple 비율 (VACUUM 필요)
    - pg_class 추정 행수 vs 실제 live 행수 차이
  를 확인해 조치를 제안한다.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, asdict

import analyzer


@dataclass
class TableStat:
    relation: str
    live: int
    dead: int
    mod_since_analyze: int
    reltuples: int
    last_analyze: str
    last_vacuum: str
    size: str
    issues: list
    actions: list


Q = """
SELECT coalesce(json_agg(t), '[]') FROM (
  SELECT c.oid::regclass::text AS relation,
         coalesce(s.n_live_tup,0) AS live, coalesce(s.n_dead_tup,0) AS dead,
         coalesce(s.n_mod_since_analyze,0) AS mod_since_analyze,
         greatest(c.reltuples,0)::bigint AS reltuples,
         coalesce(to_char(greatest(s.last_analyze, s.last_autoanalyze),'YYYY-MM-DD HH24:MI'),'없음') AS last_analyze,
         coalesce(to_char(greatest(s.last_vacuum, s.last_autovacuum),'YYYY-MM-DD HH24:MI'),'없음') AS last_vacuum,
         pg_size_pretty(pg_total_relation_size(c.oid)) AS size
  FROM pg_class c LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
  WHERE c.oid::regclass::text = ANY (ARRAY[%s])
) t;
"""


def check(dsn: str, relations: list) -> list:
    if not relations:
        return []
    arr = ",".join("'" + r.replace("'", "''") + "'" for r in relations)
    rows = json.loads(analyzer.run_psql(dsn, Q % arr).strip() or "[]")
    out = []
    for r in rows:
        issues, actions = [], []
        live, dead, mod = r["live"], r["dead"], r["mod_since_analyze"]
        if r["last_analyze"] == "없음" and live > 0:
            issues.append("ANALYZE 이력 없음")
            actions.append(f"ANALYZE {r['relation']};")
        elif live > 1000 and mod > 0.2 * live:
            issues.append(f"마지막 ANALYZE 이후 {mod:,}행 변경 ({mod / max(live, 1) * 100:.0f}%)")
            actions.append(f"ANALYZE {r['relation']};")
        if live + dead > 10000 and dead > 0.2 * (live + dead):
            issues.append(f"dead tuple {dead:,} ({dead / (live + dead) * 100:.0f}%)")
            actions.append(f"VACUUM (ANALYZE) {r['relation']};")
        if live > 10000 and r["reltuples"] and abs(r["reltuples"] - live) > 0.5 * live:
            issues.append(f"플래너 추정 행수 {r['reltuples']:,} vs 실제 {live:,}")
            if f"ANALYZE {r['relation']};" not in actions:
                actions.append(f"ANALYZE {r['relation']};")
        out.append(TableStat(r["relation"], live, dead, mod, r["reltuples"], r["last_analyze"],
                             r["last_vacuum"], r["size"], issues, actions))
    return [asdict(t) for t in out]


def format_report(stats: list) -> str:
    lines = ["=== 테이블 통계 / VACUUM 상태 ==="]
    if not stats:
        lines.append("  (조회할 테이블 없음)")
    for s in stats:
        mark = "[! ]" if s["issues"] else "[ok]"
        lines.append(f"{mark} {s['relation']}  {s['size']}  live {s['live']:,} / dead {s['dead']:,}  "
                     f"ANALYZE {s['last_analyze']}")
        for i in s["issues"]:
            lines.append(f"      - {i}")
        for a in s["actions"]:
            lines.append(f"      → {a}")
    return "\n".join(lines)
