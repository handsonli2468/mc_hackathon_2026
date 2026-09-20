"""Thin proxy to the BT engine HTTP API (see mc_main_nav docs/llm_interface.md)."""
from typing import Any

import httpx

from .config import Settings


class BtEngineError(Exception):
    """bt_engine could not be reached at all."""


class BtClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        headers = {"X-BT-Token": settings.bt_engine_token} if settings.bt_engine_token else {}
        self._client = httpx.AsyncClient(
            base_url=settings.bt_engine_url.rstrip("/"),
            headers=headers,
            timeout=settings.bt_timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        """Return (status_code, json body). The engine replies JSON even on errors."""
        try:
            r = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise BtEngineError(f"bt_engine unreachable: {e.__class__.__name__}: {e}") from e
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"ok": False, "error": r.text[:500]}

    async def status(self, run_id: str | None = None, full_trace: bool = False):
        path = f"/status/{run_id}" if run_id else "/status"
        return await self.request("GET", path, params={"trace": "full"} if full_trace else None)

    async def runs(self):
        return await self.request("GET", "/runs")

    async def cancel(self):
        return await self.request("POST", "/cancel")

    async def health(self):
        return await self.request("GET", "/health")

    async def execute(self, xml: str):
        return await self.request("POST", "/execute", json={"xml": xml})
