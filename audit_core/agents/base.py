"""공통 LLM 호출 (SPEC §1-5, §1-10).

- Ollama /api/chat, stream=False, format=출력 스키마(JSON Schema) 강제
- think=false 고정 — qwen3 thinking 분리형 대응. think 미지원 모델이면 자동으로
  빼고 재호출
- num_predict 하한 보장 — thinking 분리형에서 본문이 비는 현상 방지
- temperature=0 + seed 고정 — 동일 입력 → 동일 판정(재현성)
- 스키마 검증 실패 시 오류를 명시해 1회 재시도, 재실패 시 SchemaValidationError
  → 호출측(axis_reviewer 등)이 UNABLE(판정불가) 코드로 처리한다
"""

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable

from pydantic import BaseModel, ValidationError

from audit_core.config import get_settings

NUM_PREDICT_FLOOR = 512

# 호출 계측(2026-07-15 성능 규율) — 단계명·모델·입출력 크기·소요·상태·request_id만.
# 문서 원문·프롬프트 내용은 절대 기록하지 않는다. 핸들러는 운영 진입점(Function)이 단다.
_perf = logging.getLogger("audit_perf")


def _log_call(stage: str, model: str, req_id: str, in_chars: int,
              out_chars: int, t0: float, status: str):
    _perf.info(
        f"stage={stage or '-'} model={model} req={req_id} in_chars={in_chars} "
        f"out_chars={out_chars} dur={time.time() - t0:.1f}s status={status}"
    )


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """Ollama 접속 실패·HTTP 오류."""


class SchemaValidationError(LLMError):
    """재시도 후에도 출력이 스키마와 불일치 — 판정불가(UNABLE) 처리 대상."""


def _default_post(url: str, payload: dict, timeout_s: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise LLMUnavailable(f"Ollama HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMUnavailable(f"Ollama 접속 실패: {e}") from e


class OllamaClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: int | None = None,
        post_fn: Callable[[str, dict, int], dict] | None = None,  # 테스트 주입용
    ):
        s = get_settings()
        self.base_url = (base_url or s.OLLAMA_BASE_URL).rstrip("/")
        self.timeout_s = timeout_s or s.AUDIT_LLM_TIMEOUT_S
        self.num_ctx = s.AUDIT_LLM_NUM_CTX
        self._post = post_fn or _default_post

    def _chat(self, payload: dict, timeout_s: int | None = None) -> dict:
        url = f"{self.base_url}/api/chat"
        t = timeout_s or self.timeout_s
        try:
            return self._post(url, payload, t)
        except LLMUnavailable as e:
            # think 미지원 모델 대응: think 파라미터 제거 후 1회 재호출
            if "think" in payload and "think" in str(e).lower():
                payload = {k: v for k, v in payload.items() if k != "think"}
                return self._post(url, payload, t)
            raise

    def chat_text(
        self,
        *,
        model: str,
        prompt: str,
        system: str = "",
        num_predict: int = 1024,
        temperature: float = 0.0,
        seed: int = 42,
        stage: str = "",
        timeout_s: int | None = None,
    ) -> str:
        """자유 텍스트 응답(Q&A 안내 등 경량 용도). 스키마 없음 — 판정에 쓰지 않는다."""
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        req_id, t0, status, out = uuid.uuid4().hex[:8], time.time(), "error", ""
        try:
            resp = self._chat({
                "model": model,
                "messages": messages,
                "stream": False,
                "think": False,
                "options": {
                    "temperature": temperature,
                    "seed": seed,
                    "num_ctx": self.num_ctx,
                    "num_predict": max(num_predict, NUM_PREDICT_FLOOR),
                },
            }, timeout_s)
            out = (resp.get("message") or {}).get("content", "")
            status = "ok"
            return out
        except LLMUnavailable as e:
            status = "timeout" if "timed out" in str(e).lower() else "unavailable"
            raise
        finally:
            _log_call(stage, model, req_id, len(system) + len(prompt), len(out), t0, status)

    def chat_json(
        self,
        *,
        model: str,
        prompt: str,
        schema: type[BaseModel],
        system: str = "",
        num_predict: int = 2048,
        temperature: float = 0.0,
        seed: int = 42,
        stage: str = "",
        timeout_s: int | None = None,
    ) -> BaseModel:
        """스키마 강제 JSON 응답. 검증 실패 시 1회 재시도 후 SchemaValidationError.

        재시도 규율(2026-07-15): 재시도는 스키마 불일치에 한해 최대 1회.
        타임아웃(LLMUnavailable)은 재시도하지 않는다 — 같은 대형 입력을 다시
        보내느니 호출측이 부분 결과('확인 필요')로 처리하는 편이 빠르다.
        """
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "format": schema.model_json_schema(),
            "options": {
                "temperature": temperature,
                "seed": seed,
                "num_ctx": self.num_ctx,
                "num_predict": max(num_predict, NUM_PREDICT_FLOOR),
            },
        }

        req_id, t0, status, out_chars = uuid.uuid4().hex[:8], time.time(), "error", 0
        # 월클럭 데드라인(2026-07-15 실장애: 소켓 타임아웃 90초인데 think 재시도·
        # 스키마 재시도가 겹쳐 한 호출이 2분+ 지속) — 재시도를 포함한 총 시간이
        # timeout_s를 넘지 않는다. 남은 예산이 10초 미만이면 재시도하지 않는다.
        deadline = t0 + (timeout_s or self.timeout_s)
        last_err: Exception | None = None
        try:
            for _attempt in range(2):
                remain = deadline - time.time()
                if _attempt > 0 and remain < 10:
                    break  # 재시도 예산 부족 — 재전송하지 않고 부분 결과 우선
                resp = self._chat(payload, max(1, int(remain)))
                content = (resp.get("message") or {}).get("content", "")
                out_chars = len(content)
                try:
                    result = schema.model_validate_json(content)
                    status = "ok"
                    return result
                except ValidationError as e:
                    last_err = e
                    payload["messages"] = messages + [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": f"출력이 요구 스키마와 불일치했다: {e}\n"
                            "설명 없이 스키마에 맞는 JSON만 다시 출력하라.",
                        },
                    ]
            status = "schema_error"
            raise SchemaValidationError(f"스키마 검증 2회 실패 (model={model}): {last_err}")
        except LLMUnavailable as e:
            status = "timeout" if "timed out" in str(e).lower() else "unavailable"
            raise
        finally:
            _log_call(stage, model, req_id, len(system) + len(prompt), out_chars, t0, status)
