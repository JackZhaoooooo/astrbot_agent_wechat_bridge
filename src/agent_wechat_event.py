"""平台适配器使用的消息事件对象。"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import requests

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, File, Image, Plain, Record
from astrbot.api.platform import Group

from .agent_wechat_client import WeChatClient


def _guess_mime_type(path: str, fallback: str = "application/octet-stream") -> str:
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type or fallback


def _basename_from_url(url: str, default: str = "file") -> str:
    name = url.rstrip("/").split("/")[-1].split("?")[0]
    return name or default


def _load_binary_from_path(path: str, timeout: int = 30) -> tuple[bytes, str, str]:
    if path.startswith(("http://", "https://")):
        response = requests.get(path, timeout=timeout)
        response.raise_for_status()
        mime_type = response.headers.get("Content-Type") or _guess_mime_type(path)
        return response.content, mime_type, _basename_from_url(path)

    normalized = path[8:] if path.startswith("file:///") else path
    with open(normalized, "rb") as handle:
        data = handle.read()
    return data, _guess_mime_type(normalized), os.path.basename(normalized)


class AgentWeChatMessageEvent(AstrMessageEvent):
    """单条微信入站消息对应的事件封装。"""

    def __init__(
        self,
        message_str: str,
        message_obj: Any,
        platform_meta: Any,
        session_id: str,
        client: WeChatClient,
        chat_id: str,
        is_group: bool,
        send_message_callable: Callable[[str, MessageChain], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(
            message_str=message_str,
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id,
        )
        self.client = client
        self.chat_id = chat_id
        self.is_group = is_group
        self.send_message_callable = send_message_callable

    @staticmethod
    def _push_text(buffer: list[str], value: str | None) -> None:
        if value:
            buffer.append(value)

    @staticmethod
    def _at_to_text(component: At) -> str:
        name = getattr(component, "name", None) or getattr(component, "qq", None) or "user"
        return f"@{name}"

    @classmethod
    async def _build_send_payloads(
        cls,
        chat_id: str,
        message_chain: MessageChain,
    ) -> list[dict[str, Any]]:
        # 将消息链拆分为桥接服务可发送的一个或多个请求体。
        payloads: list[dict[str, Any]] = []
        text_buffer: list[str] = []

        def flush_text_only() -> None:
            text = "".join(text_buffer).strip()
            if text:
                payloads.append({"chatId": chat_id, "text": text})
            text_buffer.clear()

        for component in message_chain.chain:
            if isinstance(component, Plain):
                cls._push_text(text_buffer, component.text)
                continue

            if isinstance(component, At):
                cls._push_text(text_buffer, cls._at_to_text(component))
                cls._push_text(text_buffer, " ")
                continue

            if isinstance(component, Image):
                source = getattr(component, "file", None) or getattr(component, "url", None)
                if not source:
                    continue
                data, mime_type, _ = await asyncio.to_thread(_load_binary_from_path, source)
                payload: dict[str, Any] = {
                    "chatId": chat_id,
                    "image": {
                        "data": base64.b64encode(data).decode("utf-8"),
                        "mimeType": mime_type or "image/png",
                    },
                }
                text = "".join(text_buffer).strip()
                if text:
                    payload["text"] = text
                text_buffer.clear()
                payloads.append(payload)
                continue

            if isinstance(component, (File, Record)):
                source = getattr(component, "file", None) or getattr(component, "url", None)
                if not source:
                    continue
                data, _, filename = await asyncio.to_thread(_load_binary_from_path, source)
                payload = {
                    "chatId": chat_id,
                    "file": {
                        "data": base64.b64encode(data).decode("utf-8"),
                        "filename": getattr(component, "name", None) or filename or "file",
                    },
                }
                text = "".join(text_buffer).strip()
                if text:
                    payload["text"] = text
                text_buffer.clear()
                payloads.append(payload)
                continue

            serialized = component.to_dict() if hasattr(component, "to_dict") else None
            if isinstance(serialized, dict):
                cls._push_text(text_buffer, str(serialized))

        flush_text_only()
        return payloads

    @classmethod
    async def send_message_chain(
        cls,
        client: WeChatClient,
        chat_id: str,
        message_chain: MessageChain,
    ) -> None:
        payloads = await cls._build_send_payloads(chat_id, message_chain)
        for payload in payloads:
            result = await asyncio.to_thread(client.send_message, payload)
            if not result.get("success", True):
                raise RuntimeError(result.get("error") or "agent-wechat 发送失败")

    async def send(self, message: MessageChain) -> None:
        if self.send_message_callable is not None:
            await self.send_message_callable(self.chat_id, message)
        else:
            await self.send_message_chain(self.client, self.chat_id, message)
        await super().send(message)

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        if use_fallback:
            # 兼容不支持原生流式的场景，按分片逐段发送。
            async for chain in generator:
                await self.send(chain)
                await asyncio.sleep(1.2)
            return

        parts: list[str] = []
        async for chain in generator:
            for component in chain.chain:
                if isinstance(component, Plain):
                    parts.append(component.text)

        if parts:
            # 非回退模式下，将流式纯文本聚合为一次发送。
            await self.send(MessageChain([Plain(text="".join(parts))]))

    async def get_group(self, group_id: str | None = None, **kwargs: Any) -> Group | None:
        if not self.is_group:
            return None
        raw_group_id = group_id or getattr(self.message_obj, "group_id", None) or self.chat_id
        group = Group(str(raw_group_id))
        group.group_name = getattr(getattr(self.message_obj, "group", None), "group_name", None)
        return group
