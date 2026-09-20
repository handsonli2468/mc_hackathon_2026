from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from .settings import settings
from .telemetry import record_model_call


class LLMHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str, response_body: str = ""):
        super().__init__(message)
        self.status_code = int(status_code)
        self.response_body = response_body


def extract_json_object(text: str) -> dict[str, Any]:
    if not text:
        raise ValueError("empty model response")
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    start = stripped.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    obj, _ = decoder.raw_decode(stripped[start:])
    if not isinstance(obj, dict):
        raise ValueError("model JSON must be an object")
    return obj


def _schema_name(model_cls: type[BaseModel]) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", model_cls.__name__)[:64] or "StructuredOutput"


def _json_schema_response_format(model_cls: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": _schema_name(model_cls),
            "schema": model_cls.model_json_schema(),
        },
    }


def _prompt_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            try:
                total += len(json.dumps(content, ensure_ascii=False))
            except Exception:
                total += len(str(content))
    return total


async def openai_chat(
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.0,
    max_tokens: int = 8192,
    response_format: dict[str, Any] | None = None,
    thinking_token_budget: int | None = None,
    chat_template_kwargs: dict[str, Any] | None = None,
    *,
    telemetry_role: str = "unknown",
    operation: str = "unspecified",
) -> str:
    """Call a vLLM/OpenAI-compatible chat endpoint and record timing metadata.

    v5 keeps Qwen thinking enabled.  Planner calls may additionally provide a bounded
    ``thinking_token_budget``; this limits reasoning length but never switches the model
    to non-thinking/instruct mode.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if response_format is not None:
        payload["response_format"] = response_format
    if thinking_token_budget is not None and int(thinking_token_budget) > 0:
        payload["thinking_token_budget"] = int(thinking_token_budget)
    if chat_template_kwargs:
        payload["chat_template_kwargs"] = dict(chat_template_kwargs)

    prompt_chars = _prompt_chars(messages)
    started = perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            res = await client.post(url, json=payload)
            elapsed = perf_counter() - started
            if res.is_error:
                body = res.text.strip()
                record_model_call(
                    role=telemetry_role,
                    operation=operation,
                    model=model,
                    elapsed_sec=elapsed,
                    prompt_chars=prompt_chars,
                    status="error",
                    http_status=res.status_code,
                    error=body,
                    thinking_token_budget=thinking_token_budget,
                )
                message = (
                    f"LLM request failed: HTTP {res.status_code} {res.reason_phrase}; "
                    f"url={url}; model={model}; response={body[:4000]}"
                )
                raise LLMHTTPError(res.status_code, message, body)
            data = res.json()
    except LLMHTTPError:
        raise
    except Exception as exc:
        elapsed = perf_counter() - started
        record_model_call(
            role=telemetry_role,
            operation=operation,
            model=model,
            elapsed_sec=elapsed,
            prompt_chars=prompt_chars,
            status="error",
            error=str(exc),
            thinking_token_budget=thinking_token_budget,
        )
        raise

    choice = (data.get("choices") or [{}])[0]
    message_obj = choice.get("message") or {}
    content = message_obj.get("content") or ""
    reasoning = message_obj.get("reasoning_content") or message_obj.get("reasoning")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    record_model_call(
        role=telemetry_role,
        operation=operation,
        model=model,
        elapsed_sec=elapsed,
        prompt_chars=prompt_chars,
        response_chars=len(content),
        usage=usage,
        finish_reason=choice.get("finish_reason"),
        reasoning_chars=len(reasoning) if isinstance(reasoning, str) else None,
        http_status=res.status_code,
        thinking_token_budget=thinking_token_budget,
    )
    return content


async def planner_chat(
    messages: list[dict[str, Any]],
    temperature: float = 0.0,
    max_tokens: int = 8192,
    response_format: dict[str, Any] | None = None,
    thinking_token_budget: int | None = None,
    *,
    operation: str = "planner.chat",
) -> str:
    """Call Qwen with thinking explicitly kept on and an optional reasoning budget.

    v5 never switches the planner to instruct/non-thinking mode.  The budget only
    bounds how long the reasoning block may grow before the model must produce its
    final answer.  If the current vLLM build rejects the budget parameter, retry the
    same request without the budget; Qwen's default thinking behavior is preserved.
    """
    template_kwargs = {"enable_thinking": True} if settings.planner_force_thinking else None
    try:
        return await openai_chat(
            settings.planner_base_url,
            settings.planner_model,
            messages,
            temperature,
            max_tokens,
            response_format=response_format,
            thinking_token_budget=thinking_token_budget,
            chat_template_kwargs=template_kwargs,
            telemetry_role="planner",
            operation=operation,
        )
    except LLMHTTPError as exc:
        if (
            thinking_token_budget
            and settings.planner_thinking_budget_fallback
            and exc.status_code in {400, 404, 422}
        ):
            # Compatibility fallback for vLLM builds that do not yet understand
            # thinking_token_budget and/or explicit chat-template kwargs.  Omitting
            # these fields does NOT disable Qwen thinking; Qwen3.x defaults to thinking.
            return await openai_chat(
                settings.planner_base_url,
                settings.planner_model,
                messages,
                temperature,
                max_tokens,
                response_format=response_format,
                thinking_token_budget=None,
                chat_template_kwargs=None,
                telemetry_role="planner",
                operation=f"{operation}.thinking_budget_fallback",
            )
        raise


async def compiler_chat(
    messages: list[dict[str, Any]],
    temperature: float = 0.0,
    max_tokens: int | None = None,
    *,
    operation: str = "compiler.chat",
) -> str:
    budget = settings.compiler_max_output_tokens if max_tokens is None else max_tokens
    return await openai_chat(
        settings.compiler_base_url,
        settings.compiler_model,
        messages,
        temperature,
        budget,
        telemetry_role="compiler",
        operation=operation,
    )


async def planner_json(
    model_cls: type[BaseModel],
    messages: list[dict[str, Any]],
    retries: int = 1,
    temperature: float = 0.0,
    max_tokens: int = 8192,
    thinking_token_budget: int | None = None,
    retry_thinking_token_budget: int | None = None,
    *,
    operation: str = "planner.structured",
) -> BaseModel:
    """Generate Pydantic-compatible JSON using vLLM constrained decoding.

    Qwen thinking remains enabled.  v5.5 may bound reasoning tokens per request so a
    structured final answer is produced without spending minutes on unbounded thinking.
    """
    current = list(messages)
    last_error = ""
    response_format = _json_schema_response_format(model_cls)
    structured_supported = True
    # max_tokens counts the whole generated completion on OpenAI-compatible APIs.
    # Keep at least ~4k tokens available for the final schema-constrained JSON after
    # the bounded reasoning block, otherwise a larger thinking budget can paradoxically
    # cause truncation and an expensive retry.
    for attempt in range(retries + 1):
        attempt_operation = f"{operation}.attempt_{attempt + 1}"
        attempt_thinking_budget = (
            retry_thinking_token_budget
            if attempt > 0 and retry_thinking_token_budget is not None
            else thinking_token_budget
        )
        effective_max_tokens = max(
            int(max_tokens),
            (int(attempt_thinking_budget) + 4096) if attempt_thinking_budget else int(max_tokens),
        )
        try:
            text = await planner_chat(
                current,
                temperature=temperature,
                max_tokens=effective_max_tokens,
                response_format=response_format if structured_supported else None,
                thinking_token_budget=attempt_thinking_budget,
                operation=attempt_operation,
            )
        except LLMHTTPError as exc:
            status = exc.status_code
            if structured_supported and settings.structured_output_fallback and status in {400, 404, 422}:
                structured_supported = False
                last_error = f"vLLM structured output rejected ({status}); falling back to JSON-only generation"
                text = await planner_chat(
                    current,
                    temperature=temperature,
                    max_tokens=effective_max_tokens,
                    thinking_token_budget=attempt_thinking_budget,
                    operation=f"{attempt_operation}.json_fallback",
                )
            else:
                raise

        try:
            try:
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ValueError("model JSON must be an object")
            except Exception:
                payload = extract_json_object(text)
            return model_cls.model_validate(payload)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            if attempt >= retries:
                break
            current = current + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "The previous output did not satisfy the required schema. "
                        f"Validation error: {last_error}. Return only a corrected JSON object."
                    ),
                },
            ]

    raise ValueError(f"structured planner output could not be validated: {last_error}")


async def model_server_models(base_url: str) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(url)
        res.raise_for_status()
        data = res.json()
    return [str(x.get("id")) for x in data.get("data", []) if x.get("id")]
