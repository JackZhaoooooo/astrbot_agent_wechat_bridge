"""Minimal synchronous agent-wechat REST client."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests


class AgentWeChatAPIError(RuntimeError):
    """Raised when the agent-wechat API returns a non-success response."""


class WeChatClient:
    """Small REST wrapper around the agent-wechat HTTP API."""

    def __init__(self, base_url: str, token: str | None = None, timeout: int = 15) -> None:
        self.base_url = self._normalize_url(base_url)
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    @staticmethod
    def _normalize_url(base_url: str) -> str:
        url = base_url.strip()
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return url.rstrip("/")

    @staticmethod
    def _qs(params: dict[str, Any]) -> str:
        items = [(key, value) for key, value in params.items() if value is not None]
        if not items:
            return ""
        return "?" + "&".join(
            f"{quote(str(key))}={quote(str(value))}"
            for key, value in items
        )

    def _get(self, path: str) -> Any:
        response = requests.get(
            f"{self.base_url}{path}",
            headers=self.headers,
            timeout=self.timeout,
        )
        if not response.ok:
            raise AgentWeChatAPIError(f"{response.status_code}: {response.text}")
        return response.json()

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        response = requests.post(
            f"{self.base_url}{path}",
            json=body,
            headers=self.headers,
            timeout=self.timeout,
        )
        if not response.ok:
            raise AgentWeChatAPIError(f"{response.status_code}: {response.text}")
        return response.json()

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
