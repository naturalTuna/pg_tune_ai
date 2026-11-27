# SPDX-License-Identifier: MIT
# Copyright (c) 2026 naturalTuna — https://github.com/naturalTuna/pg_tune_ai
from setuptools import setup

setup(
    name="pg_tune_ai",
    version="0.3",
    description="규칙엔진 기반 PostgreSQL 쿼리 튜닝 도구 (+ 선택적 로컬 LLM 보조)",
    python_requires=">=3.7",
    author="naturalTuna",
    url="https://github.com/naturalTuna/pg_tune_ai",
    license="MIT",
    classifiers=["License :: OSI Approved :: MIT License"],
    # 단일 파일 모듈 — pgtune.py 가 import 하는 것 전부 포함해야 설치 후 동작
    py_modules=["pgtune", "analyzer", "verifier", "index_check", "tunelog",
                "hint_verifier", "stats_check", "pgexec", "sqlguard"],
    # ai 서브커맨드용 패키지 (표준 라이브러리만 사용, 추가 의존성 없음)
    packages=["pg_tune_ai"],
    entry_points={
        "console_scripts": [
            "pgtune=pgtune:main",
        ],
    },
)
