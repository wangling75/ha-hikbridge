"""Async client for the hikbridge add-on HTTP API.

The add-on can serve several cameras at once; every call is scoped to one of
them through /api/cameras/<id>/... . When no camera id is configured the
client falls back to the legacy single-camera paths, which keeps working
against older add-on versions.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import aiohttp


class HikBridgeError(Exception):
    """Generic bridge failure."""


class HikBridgeAuthError(HikBridgeError):
    """Bad or missing token."""


class HikBridgeClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int,
                 token: str | None = None, camera_id: str | None = None) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._token = token or None
        self._camera_id = camera_id or None

    @property
    def camera_id(self) -> str | None:
        return self._camera_id

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self._token} if self._token else {}

    def _path(self, suffix: str, camera_id: str | None = None) -> str:
        cid = camera_id or self._camera_id
        if cid:
            return f"/api/cameras/{quote(str(cid), safe='')}{suffix}"
        return suffix

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

    async def cameras(self) -> list[dict]:
        """Camera list reported by the add-on (empty on older versions)."""
        try:
            raw = await self._request("GET", "/api/cameras", timeout=10)
        except HikBridgeError:
            return []
        payload = json.loads(raw or b"{}")
        cameras = payload.get("cameras")
        return cameras if isinstance(cameras, list) else []

    async def ptz(self, direction: str, duration: float,
                  camera_id: str | None = None) -> None:
        await self._request(
            "POST", self._path("/ptz", camera_id), timeout=duration + 15,
            json_body={"direction": direction, "duration": duration},
        )

    async def stop(self, camera_id: str | None = None) -> None:
        await self._request("POST", self._path("/ptz/stop", camera_id), timeout=15)

    async def snapshot(self, camera_id: str | None = None) -> bytes:
        return await self._request("GET", self._path("/snapshot.jpg", camera_id),
                                   timeout=30)

    async def speak_wav(self, wav: bytes, camera_id: str | None = None) -> None:
        # generous timeout: long clips are streamed at real time
        await self._request("POST", self._path("/speak", camera_id), timeout=180,
                            data=wav)
