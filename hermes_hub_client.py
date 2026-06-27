"""Hermes Hub 异步 HTTP 客户端 + JWT 刷新 + SSE 监听。

参考 HAPI Connector 的 hapi_client.py 设计。
"""
import time
import asyncio
import json

import aiohttp
from astrbot.api import logger


class AsyncTokenManager:
    """异步 JWT 令牌管理：获取、缓存、主动刷新。"""

    def __init__(self, endpoint: str, access_token: str, jwt_lifetime: int = 900, refresh_before: int = 180, verify_ssl: bool = False):
        self._endpoint = endpoint.rstrip("/")
        self._access_token = access_token
        self._jwt_lifetime = jwt_lifetime
        self._refresh_before = refresh_before
        self._verify_ssl = verify_ssl
        self._jwt: str | None = None
        self._obtained_at: float = 0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if self._should_refresh():
                await self._do_auth()
            return self._jwt

    async def force_refresh(self) -> str:
        async with self._lock:
            await self._do_auth()
            return self._jwt

    def _should_refresh(self) -> bool:
        if self._jwt is None:
            return True
        elapsed = time.time() - self._obtained_at
        return elapsed >= (self._jwt_lifetime - self._refresh_before)

    async def _do_auth(self):
        url = f"{self._endpoint}/api/auth"
        payload = {"accessToken": self._access_token}
        connector = aiohttp.TCPConnector(ssl=self._verify_ssl)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                self._jwt = data["token"]
                self._obtained_at = time.time()
                logger.info("Hermes Hub JWT 获取成功")


class AsyncHermesHubClient:
    """异步 Hermes Hub 客户端，封装鉴权、重试。"""

    def __init__(self, endpoint: str, access_token: str, timeout: int = 120, verify_ssl: bool = False):
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        self._token_mgr = AsyncTokenManager(self._endpoint, access_token, verify_ssl=verify_ssl)
        self._session: aiohttp.ClientSession | None = None
        self._connector: aiohttp.TCPConnector | None = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            if self._connector is None or self._connector.closed:
                self._connector = aiohttp.TCPConnector(ssl=self._verify_ssl)
            self._session = aiohttp.ClientSession(connector=self._connector)

    async def _auth_headers(self) -> dict:
        token = await self._token_mgr.get_token()
        return {"Authorization": f"Bearer {token}"}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        if self._connector and not self._connector.closed:
            await self._connector.close()
            self._connector = None

    async def request(self, method: str, path: str, *, retry_on_401: bool = True, **kwargs) -> aiohttp.ClientResponse:
        await self._ensure_session()
        url = f"{self._endpoint}{path}"
        headers = kwargs.pop("headers", {})
        headers.update(await self._auth_headers())
        timeout = kwargs.pop("timeout", aiohttp.ClientTimeout(total=self._timeout))

        resp = await self._session.request(method, url, headers=headers, timeout=timeout, **kwargs)

        if resp.status == 401 and retry_on_401:
            await resp.release()
            logger.warning("Hermes Hub 返回 401，刷新 JWT 后重试")
            await self._token_mgr.force_refresh()
            headers.update(await self._auth_headers())
            resp = await self._session.request(method, url, headers=headers, timeout=timeout, **kwargs)

        return resp

    async def get(self, path: str, **kwargs) -> aiohttp.ClientResponse:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> aiohttp.ClientResponse:
        return await self.request("POST", path, **kwargs)

    async def delete(self, path: str, **kwargs) -> aiohttp.ClientResponse:
        return await self.request("DELETE", path, **kwargs)

    async def get_json(self, path: str, **kwargs) -> dict:
        resp = await self.get(path, **kwargs)
        if resp.status >= 400:
            body = await resp.text()
            resp.release()
            raise Exception(f"Hermes Hub HTTP {resp.status}: {body[:200]}")
        data = await resp.json()
        resp.release()
        return data

    async def post_json(self, path: str, **kwargs) -> dict:
        resp = await self.post(path, **kwargs)
        if resp.status >= 400:
            body = await resp.text()
            resp.release()
            raise Exception(f"Hermes Hub HTTP {resp.status}: {body[:200]}")
        data = await resp.json()
        resp.release()
        return data

    async def health(self) -> dict:
        await self._ensure_session()
        async with self._session.get(
            f"{self._endpoint}/health",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            return await resp.json()

    # ---- Hermes 业务接口 ----

    async def list_sessions(self) -> list[dict]:
        data = await self.get_json("/api/sessions")
        return data.get("sessions", [])

    async def create_session(self, message: str, workdir: str | None = None, model: str | None = None,
                             timeout: int = 120, yolo: bool = False) -> dict:
        return await self.post_json("/api/sessions", json={
            "message": message,
            "workdir": workdir,
            "model": model,
            "timeout": timeout,
            "yolo": yolo,
        })

    async def get_session(self, session_id: str) -> dict:
        data = await self.get_json(f"/api/sessions/{session_id}")
        return data.get("session", data)

    async def get_messages(self, session_id: str, limit: int = 50) -> list[dict]:
        data = await self.get_json(f"/api/sessions/{session_id}/messages", params={"limit": limit})
        return data.get("messages", [])

    async def send_message(self, session_id: str, text: str, workdir: str | None = None,
                           model: str | None = None, timeout: int = 120, yolo: bool = False) -> dict:
        return await self.post_json(f"/api/sessions/{session_id}/messages", json={
            "text": text,
            "workdir": workdir,
            "model": model,
            "timeout": timeout,
            "yolo": yolo,
        })

    async def stop_session(self, session_id: str) -> dict:
        return await self.post_json(f"/api/sessions/{session_id}/stop")

    async def rename_session(self, session_id: str, title: str) -> dict:
        return await self.post_json(f"/api/sessions/{session_id}/rename", json={"title": title})

    async def delete_session(self, session_id: str) -> dict:
        resp = await self.delete(f"/api/sessions/{session_id}")
        data = await resp.json()
        resp.release()
        return data

    async def prune_sessions(self, older_than: int = 90, source: str | None = None) -> dict:
        return await self.post_json("/api/sessions/prune", json={
            "older_than": older_than,
            "source": source,
        })

    async def subscribe_events(self, sse_timeout: int = 90):
        """订阅 SSE 事件流，返回 (event, data) 异步生成器。"""
        await self._ensure_session()
        token = await self._token_mgr.get_token()
        url = f"{self._endpoint}/api/events"
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        # 给 SSE 长连接设置一个 read timeout，触发后可由上层重连
        aio_timeout = aiohttp.ClientTimeout(total=None, sock_read=sse_timeout)
        async with self._session.get(url, headers=headers, timeout=aio_timeout) as resp:
            resp.raise_for_status()
            event_name = None
            async for line in resp.content:
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    payload = line[5:].strip()
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        data = payload
                    if event_name:
                        yield event_name, data
