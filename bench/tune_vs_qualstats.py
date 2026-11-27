#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 naturalTuna — https://github.com/naturalTuna/pg_tune_ai
# 실행: 대상 DB 에 pg_qualstats(shared_preload_libraries) + hypopg 설치, bench/setup.sql 적재 후
#   BENCH_DSN="host=... dbname=cmp" python3 bench/tune_vs_qualstats.py
"""pg_tune_ai vs pg_qualstats_index_advisor — 같은 쿼리 6개, 추천 인덱스를 실제로 만들어 실행시간 비교."""
import json, os, subprocess, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DSN = os.environ.get("BENCH_DSN", "host=/tmp port=5432 user=postgres dbname=cmp")
Q = {
 "Q1 varchar 등호":      "SELECT * FROM orders WHERE status = 'A'",
 "Q2 등호+정렬+LIMIT":   "SELECT * FROM orders WHERE cust_id = 123 ORDER BY created_at DESC LIMIT 10",
 "Q3 대소문자컬럼+범위": "SELECT * FROM orders WHERE \"Region\" = 3 AND amt > 990",
 "Q4 조인":              "SELECT o.id FROM orders o JOIN customers c ON c.id = o.cust_id WHERE c.grade = 'V'",
 "Q5 컬럼에 함수":       "SELECT * FROM orders WHERE date(created_at) = '2024-03-01'",
 "Q6 LIKE 앞 %":         "SELECT * FROM orders WHERE note LIKE '%abc1%'",
}
def psql(sql):
    return subprocess.run(["psql", "-XAtq", "-d", DSN, "-c", sql], capture_output=True, text=True).stdout.strip()
def exec_ms(sql, n=3):
    best = 1e9
    for _ in range(n):
        out = psql(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}")
        best = min(best, json.loads(out)[0]["Execution Time"])
    return best

# 1) pg_qualstats 에 워크로드 기록
psql("SELECT pg_qualstats_reset()")
for _ in range(5):
    for s in Q.values():
        psql(s)
adv = json.loads(psql("SELECT pg_qualstats_index_advisor(min_filter => 50, min_selectivity => 50)::text"))
qs_idx = [i["ddl"] for i in adv.get("indexes", [])]
qs_unopt = [u["qual"] for u in adv.get("unoptimised", [])]

# 2) pg_tune_ai : 쿼리별 tune (hypopg 검증 통과한 것만 추천)
pt = {}
for name, s in Q.items():
    t0 = time.time()
    out = subprocess.run([sys.executable, os.path.join(ROOT, "pgtune.py"), "tune", "--dsn", DSN, "--sql", s,
                          "--analyze", "--no-log", "--no-hint", "--format", "json"], capture_output=True, text=True)
    d = json.loads(out.stdout)
    adopted = d.get("recommended_indexes", [])
    rules = sorted({f["rule"] for f in d["diagnosis"]["findings"]})
    pt[name] = {"adopted": adopted, "rules": rules, "sec": round(time.time() - t0, 1)}

# 3) 실제 인덱스로 실행시간 측정
base = {n: exec_ms(s) for n, s in Q.items()}
def measure(ddls):
    made = []
    for d in ddls:
        nm = f"cmp_ix_{len(made)}"
        sql = d.replace("CREATE INDEX ON", f"CREATE INDEX {nm} ON", 1) if " ON " in d and d.upper().startswith("CREATE INDEX ON") else d
        r = subprocess.run(["psql", "-XAtq", "-d", DSN, "-c", sql], capture_output=True, text=True)
        if r.returncode == 0:
            made.append(psql("SELECT indexrelid::regclass FROM pg_index ORDER BY indexrelid DESC LIMIT 1"))
    psql("ANALYZE")
    res = {n: exec_ms(s) for n, s in Q.items()}
    for m in made:
        psql(f"DROP INDEX {m}")
    return res
pt_all = sorted({d for v in pt.values() for d in v["adopted"]})
r_pt = measure(pt_all)
r_qs = measure(qs_idx)

print("== 추천 인덱스 ==")
print("pg_tune_ai       :", pt_all)
print("pg_qualstats     :", qs_idx)
print("pg_qualstats 최적화 불가 조건:", qs_unopt)
print()
print(f"{'쿼리':<22}{'기준(ms)':>10}{'pg_tune_ai':>12}{'qualstats':>11}   pg_tune_ai 진단 규칙")
for n in Q:
    print(f"{n:<22}{base[n]:>10.2f}{r_pt[n]:>12.2f}{r_qs[n]:>11.2f}   {', '.join(pt[n]['rules']) or '-'}")
print()
print("pg_tune_ai 쿼리당 소요(초):", {n: v["sec"] for n, v in pt.items()})
json.dump({"pt": pt, "qs": qs_idx, "qs_unopt": qs_unopt, "base": base, "r_pt": r_pt, "r_qs": r_qs},
          open(os.path.join(ROOT, "bench", "tune_vs_qualstats.json"), "w"), ensure_ascii=False, indent=1)
