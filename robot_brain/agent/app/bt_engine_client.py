from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .settings import settings


class BtEngineUnavailable(RuntimeError):
    """Raised when the BT engine cannot be reached over HTTP."""


@dataclass(frozen=True)
class BtEngineHttpResponse:
    status_code: int
    body: Any
    raw_text: str
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


def _base_url() -> str:
    return settings.bt_engine_url.rstrip("/")


def _decode_body(raw: bytes, content_type: str) -> tuple[Any, str]:
    text = raw.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    if "json" in (content_type or "").lower() or stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(text), text
        except json.JSONDecodeError:
            pass
    return text, text


def _request_sync(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    timeout: float | None = None,
) -> BtEngineHttpResponse:
    if not settings.bt_engine_enabled:
        raise BtEngineUnavailable("BT-engine integration is disabled (BT_ENGINE_ENABLED=0).")

    url = f"{_base_url()}{path}"
    headers = {"Accept": "application/json, application/xml, text/xml, text/plain"}
    token = str(getattr(settings, "bt_engine_token", "") or "").strip()
    if token:
        headers["X-BT-Token"] = token
    data = raw_body
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        headers["Content-Type"] = "application/xml"

    req = urlrequest.Request(url, data=data, headers=headers, method=method.upper())
    request_timeout = float(timeout if timeout is not None else settings.bt_engine_request_timeout)
    try:
        with urlrequest.urlopen(req, timeout=request_timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            body, text = _decode_body(raw, ctype)
            return BtEngineHttpResponse(int(resp.status), body, text, ctype)
    except urlerror.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
        body, text = _decode_body(raw, ctype)
        return BtEngineHttpResponse(int(exc.code), body, text, ctype)
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise BtEngineUnavailable(f"BT engine unavailable at {_base_url()}: {reason}") from exc


async def request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    timeout: float | None = None,
) -> BtEngineHttpResponse:
    return await asyncio.to_thread(
        _request_sync,
        method,
        path,
        json_body=json_body,
        raw_body=raw_body,
        timeout=timeout,
    )


async def health() -> BtEngineHttpResponse:
    return await request("GET", "/health", timeout=settings.bt_engine_health_timeout)


async def nodes(*, include_builtin: bool = True) -> BtEngineHttpResponse:
    suffix = "" if include_builtin else "?builtin=0"
    return await request("GET", f"/nodes{suffix}")


async def validate(xml: str) -> BtEngineHttpResponse:
    return await request("POST", "/validate", json_body={"xml": xml})


async def execute(xml: str) -> BtEngineHttpResponse:
    return await request("POST", "/execute", json_body={"xml": xml})


async def status(run_id: str | None = None, *, full_trace: bool = False) -> BtEngineHttpResponse:
    path = f"/status/{run_id}" if run_id else "/status"
    if full_trace:
        path += "?trace=full"
    return await request("GET", path)


async def cancel() -> BtEngineHttpResponse:
    return await request("POST", "/cancel", json_body={})


async def runs() -> BtEngineHttpResponse:
    return await request("GET", "/runs")
