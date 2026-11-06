# pg_tune_ai — PostgreSQL 쿼리 튜닝 도구

EXPLAIN 실행계획을 **규칙엔진**으로 진단하고, 인덱스 후보를 **hypopg 가상 인덱스로 검증**해서
실제로 플래너가 채택하는 추천만 보여주는 CLI 도구입니다.

로컬 LLM(Ollama)으로 추가 제안을 받는 `ai` 서브커맨드는 **선택 기능**입니다.
Ollama가 없어도 `analyze` / `verify` / `index-check` / `tune`은 그대로 동작합니다.
(이전 `pg_tune` 레포의 기능을 전부 포함합니다.)

- Python 3.7+ 표준 라이브러리만 사용 (pip 의존성 없음 → 폐쇄망 OK)
- DB 접속은 `psql` 명령을 사용 (PATH에 있어야 함)
- `verify` / `tune` / `ai` 검증은 대상 DB에 `hypopg` extension 필요

---

## 설치

설치 없이 바로 실행:

```bash
python3 pgtune.py --help
```

명령으로 설치 (`pgtune` 명령 생성):

```bash
pip3 install .
pgtune --help
```

---

## 서브커맨드

| 명령 | 내용 | 필요 |
|---|---|---|
| `analyze` | 쿼리 하나를 진단 (느린 노드, 시간 핫스팟, 인덱스 후보) | psql |
| `verify` | 인덱스 후보를 hypopg로 가상 생성해서 플래너 채택 여부 / 비용 변화 확인 | psql, hypopg |
| `index-check` | DB 전체 인덱스 점검 (안 쓰는 / 중복 / 비대 인덱스) | psql |
| `tune` | `analyze` + 후보 자동 `verify` 를 한 번에 — 검증된 추천만 출력 | psql, hypopg |
| `ai` | `analyze` + 로컬 LLM 제안 + 검증 | psql, hypopg, Ollama |

```bash
DSN="host=10.0.0.5 port=5432 user=postgres dbname=appdb"

pgtune tune        --dsn "$DSN" --sql "SELECT * FROM orders WHERE amt = 5"
pgtune analyze     --dsn "$DSN" --sql "SELECT ..."
pgtune analyze     --plan-file plan.json            # EXPLAIN (FORMAT JSON) 결과 파일로 오프라인 진단
pgtune verify      --dsn "$DSN" --sql "SELECT ..." --index "CREATE INDEX ON orders (amt)"
pgtune index-check --dsn "$DSN" --min-scans 50
```

공통 옵션

- `--format json` : JSON 출력
- `--no-analyze` (analyze / tune / ai) : `EXPLAIN ANALYZE` 대신 `EXPLAIN` 만 (쿼리를 실제로 실행하지 않음 — 운영 DB에서 안전)
- `--no-log` (analyze / verify / tune / ai) : 로그 저장 안 함

> 기본은 `EXPLAIN ANALYZE` 라서 **쿼리가 실제로 실행됩니다.** DML이나 무거운 쿼리는 `--no-analyze` 를 쓰세요.

### 진단 규칙 (analyzer)

추정 행수 오차, 비효율 Seq Scan, **컬럼에 함수 적용**(`date(col)=...` → 인덱스 / 파티션 프루닝 무효화),
Sort / Hash 디스크 spill, Heap Fetches 과다, 낮은 캐시 히트율 등.
파티션 테이블에서 같은 경고가 파티션마다 반복되면 부모 테이블 1건으로 합칩니다.

---

## ai 서브커맨드 (선택)

```bash
pgtune ai --dsn "$DSN" --sql "SELECT ..." \
          --model qwen2.5-coder:7b --host http://localhost:11434 --timeout 180
```

흐름: 규칙엔진 진단 → 진단 수치를 넣은 프롬프트 → Ollama → 응답 파싱 → **hypopg / EXPLAIN 검증** → 리포트

- LLM은 DB에 접근하지 않습니다 (텍스트 in / out).
- LLM 제안은 검증을 통과한 것만 추천으로 표시됩니다 (`--no-verify` 로 끌 수 있지만 권장 안 함).
- 기본 모델은 `qwen2.5-coder:1.5b` (가벼운 테스트용). 품질은 7b 이상 권장.
- Ollama에 연결이 안 되면 오류 메시지를 출력하고 종료합니다. 다른 서브커맨드에는 영향 없습니다.

---

## 로그

실행 위치 기준 `./pgtune_log/` 에 `{analyze|verify|tune|ai}_{쿼리id}_{날짜}.log` 로 쌓입니다.
쿼리id는 공백 정규화한 SQL의 md5 앞 8자리라서, 같은 쿼리는 같은 날 한 파일에 이어서 기록됩니다.

---

## 파일 구조

```
pgtune.py              통합 진입점 (CLI)
analyzer.py            EXPLAIN 진단 규칙엔진 + 인덱스 후보
verifier.py            hypopg 가상 인덱스 검증
index_check.py         DB 전체 인덱스 점검
tunelog.py             실행 로그
pg_tune_ai/            ai 서브커맨드 (orchestrator, prompt, llm_client, parser, proposal_verifier)
```

hypopg 소스는 `pg_project` 의 extension 카탈로그(`extensions/manifest.json`)에서 함께 패키징할 수 있습니다.
