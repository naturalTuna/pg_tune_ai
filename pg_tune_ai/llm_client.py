#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_client.py — Ollama 로컬 LLM 호출 (폐쇄망)

표준 라이브러리(urllib)만 사용 — requests 미설치 환경에서도 동작.
Ollama /api/generate 사용, stream=False 로 전체 응답을 한 번에 수신.

설계 원칙: LLM 은 DB 에 접근하지 않는다. 여기서는 텍스트 in / 텍스트 out 만.
"""

from __future__ import annotations
import json
import urllib.request
import urllib.error

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:1.5b"   # 개발/배관용. 품질은 7b(Q4_K_M ~5GB) 권장.


def generate(prompt: str, *, model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST,
             system: str | None = None, force_json: bool = True,
             temperature: float = 0.1, num_ctx: int = 8192,
             timeout: int = 180) -> str:
    """Ollama 에 프롬프트를 보내고 응답 텍스트를 반환.

    force_json=True 면 Ollama 의 format="json" 으로 유효 JSON 출력을 강제한다.
    (모델이 스키마를 지키도록 프롬프트에도 스키마를 명시해야 함)
    """
    url = f"{host.rstrip('/')}/api/generate"
    body: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if system:
        body["system"] = system
    if force_json:
        body["format"] = "json"

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Ollama 호출 실패 ({url}): {e}\n"
            f"  - 서비스 확인: `systemctl status ollama` 또는 `ollama serve`\n"
            f"  - 모델 확인 : `ollama list` (없으면 `ollama pull {model}`)"
        ) from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Ollama 응답(JSON) 파싱 실패: {e}") from e
    return payload.get("response", "")


def health(host: str = DEFAULT_HOST, timeout: int = 5) -> bool:
    """Ollama 가 응답하는지 가볍게 확인 (/api/tags)."""
    url = f"{host.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False
