# -*- coding: utf-8 -*-
"""pg_tune_ai — pg_tune 의 LLM 보조 계층 (Ollama 로컬 모델).

설계 원칙
  - LLM 은 DB 에 접근하지 않는다 (텍스트 in / 텍스트 out).
  - LLM 제안은 반드시 검증 계층(hypopg / EXPLAIN)을 거쳐 사용자에게 닿는다.
  - 규칙엔진 진단의 evidence(실제 수치)를 프롬프트에 주입해 환각을 억제한다.

흐름: analyzer(진단) → findings → prompt → llm_client → parser → proposal_verifier
"""
