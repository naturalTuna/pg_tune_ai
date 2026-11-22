# pg_tune_ai — PostgreSQL 쿼리 튜닝 도구 (규칙엔진 + 실제 검증 + 선택적 로컬 LLM)

EXPLAIN 실행계획을 **규칙엔진**으로 진단하고, 인덱스 후보는 **hypopg 가상 인덱스**,
실행계획 대안은 **pg_hint_plan**, 설정은 **같은 세션 SET** 으로 실제 검증해서
"검증을 통과한 추천" 만 보여주는 CLI 도구입니다.

- Python 3.7+ 표준 라이브러리 + `psql` 만 사용 (pip 의존성 없음 → 폐쇄망 OK)
- 로컬 LLM(Ollama) 으로 추가 제안을 받는 `ai` 는 **선택 기능** — Ollama 가 없어도 나머지는 그대로 동작
- `pg_project/pg_tune` 과 같은 엔진 (이 레포가 기준)

```bash
python3 pgtune.py tune --dsn "host=10.0.0.1 dbname=app user=postgres" --sql "SELECT ..."
python3 pgtune.py tune ... --analyze      # 실제 실행해서 시간까지 비교 (항상 ROLLBACK)
```

---

## 설치

```bash
python3 pgtune.py --help          # 설치 없이 실행
pip3 install .                    # 또는 설치 → pgtune 명령
```

대상 DB 준비 (없으면 해당 단계만 "검증 불가" 로 표시되고 나머지는 동작)

| extension | 용도 | 준비 |
|---|---|---|
| hypopg | 인덱스 후보 검증 | `CREATE EXTENSION hypopg;` (어느 스키마든 자동 인식) |
| pg_hint_plan | 실행계획 대안 실험 | 설치만. 세션에서 `LOAD` 하므로 superuser 계정 필요 |
| pgstattuple | `index-check --exact-bloat` | `CREATE EXTENSION pgstattuple;` |

---

## 서브커맨드

| 명령 | 내용 |
|---|---|
| `tune` | ★ 전체: 진단 → 테이블 통계 → 인덱스 후보(hypopg) → 실행계획 대안(pg_hint_plan) → 설정 실험 → 우선순위 추천 |
| `analyze` | 진단만 (규칙 + 핫스팟 + 후보 목록). `--plan-file plan.json` 으로 오프라인 진단 가능 |
| `verify` | 인덱스 1개를 hypopg 로 검증 |
| `hint` | 힌트 1개를 pg_hint_plan 으로 실험 (`--hint "HashJoin(o c)"`) |
| `stats` | 쿼리에 쓰인 테이블 통계 / vacuum 상태 |
| `index-check` | DB 전체 인덱스 점검 (미사용 / 중복 / INVALID / 큰 인덱스, `--exact-bloat` 실측) |
| `ai` | 진단 + 로컬 LLM 제안 + 검증 (선택 기능) |

공통 옵션: `--format json`, `--no-log`, `--analyze` / `--no-analyze`(기본),
전역 `--statement-timeout 30s`, `--lock-timeout 5s` (서브커맨드 앞에 지정)

### 진단 규칙 (15종)

| 규칙 | 감지 | 조치 방향 |
|---|---|---|
| seq_scan_inefficient | 대부분 버리는 전체 스캔 | 인덱스 후보 (= 컬럼 우선) |
| index_then_filter | 인덱스로 찾고 Filter 로 대부분 버림 | 복합 인덱스 |
| sort_for_limit | LIMIT 위해 전체 정렬 | (= 컬럼 + 정렬 컬럼) 인덱스 |
| nested_loop_many_loops | 안쪽을 수천 번 반복 | 조인 키 인덱스 / Hash Join 실험 |
| correlated_subquery | 상관 서브쿼리 반복 실행 | JOIN / EXISTS 재작성 |
| function_on_column | `date(col) = ...` | 범위 조건 재작성 / 표현식 인덱스 |
| implicit_cast_on_column | `col::numeric = ...` (varchar→text 같은 무해한 캐스트는 제외) | 바인드 타입 맞추기 |
| like_leading_wildcard | `LIKE '%...'` | pg_trgm GIN |
| or_condition_seq_scan | OR 때문에 전체 스캔 | 컬럼별 인덱스 / UNION ALL |
| bad_estimate | 추정 vs 실제 10배 이상 | ANALYZE / CREATE STATISTICS / Rows 힌트 실험 |
| sort / hash spill, lossy bitmap | 디스크 스필 | work_mem 실험 |
| high_heap_fetches | Index Only Scan 인데 heap 접근 | VACUUM |
| low_cache_hit | 디스크 read 위주 | shared_buffers / 콜드 캐시 확인 |
| planning_heavy / jit_overhead | 계획 시간 / JIT 과다 | prepared statement / jit off |

---

## 안전장치 (운영 DB 에서 쓰기 전에 꼭 읽기)

이 도구는 사용자 SQL 과 LLM 이 만든 SQL 을 `psql` 로 EXPLAIN 합니다. 그래서 아래를 강제합니다.

| 위험 | 막는 방법 |
|---|---|
| `SELECT ...; COMMIT; DROP TABLE x` 처럼 뒤에 붙은 문장 실행 | `sqlguard` 문장 분리기(주석/문자열/`$tag$` 인식)로 **한 문장만** 허용, psql 메타명령 `\` 차단 |
| `--analyze` 로 쿼리가 실제 실행되며 데이터 변경 | 항상 트랜잭션 안에서 실행 후 **ROLLBACK**. DML 이 아닌 문장은 **READ ONLY 트랜잭션** → `WITH d AS (DELETE ...) SELECT` 같은 숨은 쓰기는 에러로 차단 |
| LLM 이 만든 재작성 SQL / 설정 / 인덱스 DDL | 재작성은 조회 전용만(쓰기 키워드, `nextval`·`dblink` 등 부작용 함수, `FOR UPDATE` 금지), 설정은 플래너/메모리 GUC 화이트리스트, 인덱스는 `CREATE INDEX` 형식만. 값은 전부 psql 변수(`:'v'`)로 전달 |
| 오래 도는 쿼리 / 락 대기 | `statement_timeout` 5분, `lock_timeout` 5초 기본 (`--statement-timeout` 으로 변경) |
| 비밀번호 노출 | DSN 의 `password=` 는 argv 대신 `PGPASSWORD` 로 전달(ps 에 안 보임), 로그에는 `****` 로 마스킹, 로그 파일 0600 |

남는 한계
- ROLLBACK 해도 **시퀀스 nextval, dblink, 외부 파일 쓰기** 같은 트랜잭션 밖 부작용은 되돌릴 수 없습니다.
  DML 을 `--analyze` 할 땐 이 점을 알고 쓰세요.
- 더 안전하게 쓰려면 **검증 전용 계정**을 권장합니다 (PG14+):
  ```sql
  CREATE ROLE pgtune LOGIN PASSWORD '...';
  GRANT pg_read_all_data, pg_read_all_stats TO pgtune;
  -- hypopg 함수는 기본적으로 PUBLIC 실행 가능. pg_hint_plan 실험만 superuser 필요
  ```
- 인덱스 추천은 cost(추정) 기준이며 **쓰기 부하(INSERT/UPDATE 비용)는 반영하지 않습니다.**
- `index-check` 의 `idx_scan` 은 그 노드 통계입니다. 스탠바이가 있으면 리포트에 경고가 붙으며,
  스탠바이에서 조회용으로 쓰는 인덱스일 수 있으니 모든 노드에서 확인 후 판단하세요.

---

## ai 서브커맨드 (선택)

```bash
python3 pgtune.py ai --dsn "$DSN" --sql "SELECT ..." \
          --model qwen2.5-coder:7b --host http://localhost:11434 --timeout 180
```

흐름: 규칙엔진 진단 → 진단 수치를 넣은 프롬프트 → Ollama → 응답 파싱 + **보안 검사** → **hypopg / EXPLAIN 검증** → 리포트

- LLM 은 DB 에 접근하지 않습니다 (텍스트 in / out).
- 보안 검사에 걸린 제안은 실행하지 않고 "파서 경고" 에 사유를 남깁니다.
- 재작성(rewrite) 제안은 결과 동등성을 자동 검증하지 못합니다 — 원본과 같은 결과인지 직접 비교 필요.

---

## 로그

기본으로 `./pgtune_log/{tune|analyze|verify|hint|ai}_{쿼리id}_{날짜}.log` 에 누적 기록 (`--no-log` 로 끔).
DSN 비밀번호는 마스킹, 디렉토리 0700 / 파일 0600.

---

## 테스트

```bash
python3 -m unittest discover -s tests -v                       # 단위 테스트 (DB 불필요)
PGTUNE_TEST_DSN="host=/tmp port=5432 user=postgres dbname=postgres" \
  python3 -m unittest tests.test_integration -v                # 실제 PG (pgtune_it 스키마 생성 후 삭제)
```

통합 테스트는 이전 버전에서 재현됐던 사고를 그대로 시나리오로 갖고 있습니다:
CTE DELETE 커밋, DML ANALYZE 롤백, 다중문 실행, LLM 재작성/설정/인덱스 DDL 인젝션,
varchar 컬럼 후보 오인('text'), 대소문자 컬럼 누락, 표현식/partial 인덱스 중복 오탐, 완전 중복 미탐, 비밀번호 노출.
GitHub Actions 에서 PostgreSQL 16 + hypopg 로 자동 실행됩니다.

---

## 다른 도구와의 차이

### 실측: pg_qualstats 인덱스 추천과 비교 (`bench/`)
같은 DB(orders 30만 행, customers 2만 행, PostgreSQL 16)에 쿼리 6개. 각 도구가 추천한 인덱스를 **실제로 만들어** 실행시간(ms, 3회 중 최소) 측정.

| 쿼리 | 기준 | pg_tune_ai | pg_qualstats_index_advisor |
|---|---|---|---|
| Q1 `status = 'A'` (varchar) | 14.11 | 3.76 | 3.68 |
| Q2 `cust_id = ? ORDER BY created_at DESC LIMIT 10` | 13.18 | 0.19 | 0.19 |
| Q3 `"Region" = 3 AND amt > 990` | 14.54 | 0.09 | 0.10 |
| Q4 `JOIN customers ... grade = 'V'` | 25.84 | 3.32 | 3.34 |
| Q5 `date(created_at) = ...` | 15.15 | **0.23** (표현식 인덱스 검증) | 16.69 (추천 없음) |
| Q6 `note LIKE '%abc1%'` | 24.56 | 26.97 (진단: pg_trgm GIN 권고) | 24.46 (최적화 불가로 표시) |

- 일반 B-tree 후보 4개는 두 도구 추천이 같음
- pg_tune_ai 만: 표현식 인덱스(hypopg 검증), 규칙 진단(함수/LIKE/캐스트 등)과 쿼리 수정 권고, pg_hint_plan / SET 실험
- pg_qualstats 만: **워크로드 전체**(실제 실행된 조건절 통계)를 보고 한 번에 추천, 쿼리를 하나씩 줄 필요 없음
- 둘 다 못 함: trigram GIN 검증 (hypopg 가 GIN 미지원)

### 기능 비교

| | pg_tune_ai | Dexter | pg_qualstats (+ PoWA) | explain.dalibo (pev2) |
|---|---|---|---|---|
| 입력 | 쿼리 1건 (+ 플랜 파일) | 로그 / pg_stat_statements 워크로드 | 실행된 조건절 통계 | 플랜 1건 |
| 인덱스 검증 | hypopg (표현식 포함) | hypopg | hypopg (PoWA) | - |
| 실행계획 대안 / 설정 실험 | pg_hint_plan, SET | - | - | - |
| 진단 규칙 / 쿼리 수정 권고 | 15종 | - | - | 시각화 |
| SQL 파싱 | 플랜 문자열 + 카탈로그 검증 | pg_query (실제 파서) | 서버 통계 | - |
| 폐쇄망 / 의존성 | 표준 라이브러리 + psql | Ruby gem | 서버 extension (preload 필요) + 웹 | 웹 |

(Dexter 는 이 환경에서 rubygems 접근이 막혀 실측하지 못함)
워크로드 단위(Top N 쿼리 일괄) 분석은 아직 없음 — pg_qualstats 대비 다음 개선 후보.
