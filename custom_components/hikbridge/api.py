"""Async client for the hikbridge add-on HTTP API."""
from __future__ import annotations

import asyncio
import json

import aiohttp


class HikBridgeError(Exception):
    """Generic bridge failure."""


class HikBridgeAuthError(HikBridgeError):
    """Bad or missing token."""


class HikBridgeClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int,
                 token: str | None = None) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._token = token or None

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self._token} if self._token else {}

    async def _request(self, method: str, path: str, *, timeout: float = 30,
                       data: bytes | None = None, json_body: dict | None = None) -> bytes:
        kwargs: dict = {
            "headers": self._headers,
            "timeout": aiohttp.ClientTimeout(total=timeout),
        }
        if data is not None:
            kwargs["data"] = data
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            async with self._session.request(
                method, f"{self._base}{path}", **kwargs
            ) as resp:
                body = await resp.read()
                if resp.status == 401:
                    raise HikBridgeAuthError("bridge rejected the auth token")
                if resp.status >= 400:
                    detail = body.decode("utf-8", "replace")[:200]
                    raise HikBridgeError(f"HTTP {resp.status}: {detail}")
                return body
        except aiohttp.ClientError as exc:
            raise HikBridgeError(f"cannot reach bridge: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise HikBridgeError("bridge timed out") from exc

    async def health(self) -> dict:
        raw = await self._request("GET", "/health", timeout=10)
        return json.loads(raw or b"{}")

    async def ptz(self, direction: str, duration: float) -> None:
        await self._request(
            "POST", "/ptz", timeout=duration + 15,
            json_body={"direction": direction, "duration": duration},
        )

    async def stop(self) -> None:
        await self._request("POST", "/ptz/stop", timeout=15)

    async def snapshot(self) -> bytes:
        return await self._request("GET", "/snapshot.jpg", timeout=30)

    async def speak_wav(self, wav: bytes) -> None:
        # generous timeout: long clips are streamed at real time
        await self._request("POST", "/speak", timeout=180, data=wav)
