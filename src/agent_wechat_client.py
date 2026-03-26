"""个人微信桥接服务客户端辅助实现。"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode, urlparse, urlunparse

try:
    import websockets
except ImportError:
    websockets = None

import requests


class AgentWeChatAPIError(RuntimeError):
    """桥接服务接口返回非成功状态时抛出。"""


class WeChatClient:
    """对桥接服务接口的轻量封装。"""

    def __init__(self, base_url: str, token: str | None = None, timeout: int = 15) -> None:
        self.base_url = self._normalize_url(base_url)
        self.token = token
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._thread_local = threading.local()

    @staticmethod
    def _normalize_url(base_url: str) -> str:
        url = base_url.strip()
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return url.rstrip("/")

    def build_ws_url(self, path: str) -> str:
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        joined_path = f"{parsed.path.rstrip('/')}{path}"
        query = urlencode({"token": self.token}) if self.token else ""
        return urlunparse((scheme, parsed.netloc, joined_path, "", query, ""))

    def build_events_ws_url(self) -> str:
        return self.build_ws_url("/api/ws/events")

    def build_login_ws_url(self) -> str:
        return self.build_ws_url("/api/ws/login")

    @staticmethod
    def _qs(params: dict[str, Any]) -> str:
        items = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            items.append((key, value))
        if not items:
            return ""
        return "?" + "&".join(
            f"{quote(str(key))}={quote(str(value))}"
            for key, value in items
        )

    def _get(self, path: str) -> Any:
        response = self._session().get(
            f"{self.base_url}{path}",
            timeout=self.timeout,
        )
        if not response.ok:
            raise AgentWeChatAPIError(f"{response.status_code}: {response.text}")
        return response.json()

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        response = self._session().post(
            f"{self.base_url}{path}",
            json=body,
            timeout=self.timeout,
        )
        if not response.ok:
            raise AgentWeChatAPIError(f"{response.status_code}: {response.text}")
        return response.json()

    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self.headers)
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=8,
                pool_maxsize=8,
                max_retries=0,
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._thread_local.session = session
        return session

    def status(self) -> dict[str, Any]:
        return self._get("/api/status")

    def auth_status(self) -> dict[str, Any]:
        return self._get("/api/status/auth")

    def login(self) -> dict[str, Any]:
        return self._post("/api/status/login")

    def logout(self) -> dict[str, Any]:
        return self._post("/api/status/logout")

    def list_chats(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        result = self._get(f"/api/chats{self._qs({'limit': limit, 'offset': offset})}")
        return result if isinstance(result, list) else []

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        result = self._get(f"/api/chats/{quote(chat_id)}")
        return result if isinstance(result, dict) else None

    def open_chat(self, chat_id: str, clear_unreads: bool = True) -> dict[str, Any]:
        return self._post(
            f"/api/chats/{quote(chat_id)}/open{self._qs({'clearUnreads': clear_unreads})}"
        )

    def list_messages(
        self,
        chat_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        result = self._get(
            f"/api/messages/{quote(chat_id)}{self._qs({'limit': limit, 'offset': offset})}"
        )
        return result if isinstance(result, list) else []

    def get_media(self, chat_id: str, local_id: int) -> dict[str, Any]:
        return self._get(f"/api/messages/{quote(chat_id)}/media/{local_id}")

    def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/messages/send", payload)


Callback = Callable[..., Any | Awaitable[Any]]


class WeChatEventWebSocketClient:
    """用于事件流的可重连客户端。"""

    def __init__(
        self,
        ws_url: str,
        *,
        on_open: Callback | None = None,
        on_message: Callback | None = None,
        on_close: Callback | None = None,
        on_error: Callback | None = None,
    ) -> None:
        self.ws_url = ws_url
        self.on_open = on_open
        self.on_message = on_message
        self.on_close = on_close
        self.on_error = on_error

    @staticmethod
    async def _maybe_call(callback: Callback | None, *args: Any) -> None:
        if callback is None:
            return
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        if websockets is None:
            raise RuntimeError(
                "需要安装 `websockets` 包才能使用事件流。"
            )

        reconnect_delay = 1.0
        while not stop_event.is_set():
            opened = False
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=20,
                    close_timeout=10,
                    max_size=4 * 1024 * 1024,
                ) as websocket:
                    opened = True
                    # 连接成功后重置重连退避时间。
                    reconnect_delay = 1.0
                    await self._maybe_call(self.on_open)

                    while not stop_event.is_set():
                        recv_task = asyncio.create_task(websocket.recv())
                        stop_task = asyncio.create_task(stop_event.wait())
                        done, pending = await asyncio.wait(
                            {recv_task, stop_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()

                        if stop_task in done:
                            recv_task.cancel()
                            await websocket.close()
                            return

                        raw_message = recv_task.result()
                        if isinstance(raw_message, bytes):
                            raw_message = raw_message.decode("utf-8", errors="ignore")
                        await self._maybe_call(self.on_message, raw_message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._maybe_call(self.on_error, exc)
                if stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=reconnect_delay)
                except asyncio.TimeoutError:
                    pass
                # 指数退避重连，避免在服务不可用时高频重试。
                reconnect_delay = min(reconnect_delay * 2, 15.0)
            finally:
                if opened:
                    await self._maybe_call(self.on_close)
