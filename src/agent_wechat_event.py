"""平台适配器使用的消息事件对象。"""

from __future__ import annotations

import asyncio
import base64
import inspect
import mimetypes
import os
from collections.abc import AsyncGenerator
from typing import Any

import requests

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, File, Image, Plain, Record
from astrbot.api.platform import Group

from .agent_wechat_client import WeChatClient

SEND_REQUEST_TIMEOUT_SECONDS = 30.0
SEND_LOG_PREFIX = "[agent_wechat][send]"


def _truncate(value: str, limit: int = 80) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...(len={len(value)})"


def _component_type_name(component: Any) -> str:
    return type(component).__name__


def _describe_component(component: Any) -> str:
    comp_type = _component_type_name(component)
    if isinstance(component, Plain):
        text = str(getattr(component, "text", "") or "")
        return f"{comp_type}(text_len={len(text)}, text={_truncate(text)})"
    if isinstance(component, At):
        target = getattr(component, "name", None) or getattr(component, "qq", None)
        return f"{comp_type}(target={target})"
    if isinstance(component, Image):
        source = getattr(component, "file", None) or getattr(component, "url", None)
        return f"{comp_type}(source={source})"
    if isinstance(component, (File, Record)):
        source = getattr(component, "file", None) or getattr(component, "url", None)
        name = getattr(component, "name", None)
        return f"{comp_type}(source={source}, name={name})"
    return comp_type


def _summarize_payload(payload: dict[str, Any]) -> str:
    text = str(payload.get("text") or "")
    has_image = isinstance(payload.get("image"), dict)
    has_file = isinstance(payload.get("file"), dict)
    file_name = ""
    if has_file:
        file_name = str(payload["file"].get("filename") or "")
    image_mime = ""
    if has_image:
        image_mime = str(payload["image"].get("mimeType") or "")
    return (
        f"keys={sorted(payload.keys())}, "
        f"text_len={len(text)}, "
        f"has_image={has_image}, image_mime={image_mime}, "
        f"has_file={has_file}, file_name={file_name}"
    )


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

    @staticmethod
    def _push_text(buffer: list[str], value: str | None) -> None:
        if value:
            buffer.append(value)

    @staticmethod
    def _at_to_text(component: At) -> str:
        name = (
            getattr(component, "name", None) or getattr(component, "qq", None) or "user"
        )
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
        component_count = len(message_chain.chain)
        component_types = [_component_type_name(comp) for comp in message_chain.chain]
        logger.info(
            f"{SEND_LOG_PREFIX} build payloads start "
            f"chat={chat_id} components={component_count} types={component_types}"
        )

        def flush_text_only() -> None:
            text = "".join(text_buffer).strip()
            if text:
                payloads.append({"chatId": chat_id, "text": text})
                logger.info(
                    f"{SEND_LOG_PREFIX} append text payload "
                    f"chat={chat_id} text_len={len(text)}"
                )
            text_buffer.clear()

        for index, component in enumerate(message_chain.chain):
            logger.info(
                f"{SEND_LOG_PREFIX} process component "
                f"chat={chat_id} index={index} detail={_describe_component(component)}"
            )

            if isinstance(component, Plain):
                cls._push_text(text_buffer, component.text)
                continue

            if isinstance(component, At):
                cls._push_text(text_buffer, cls._at_to_text(component))
                cls._push_text(text_buffer, " ")
                continue

            if isinstance(component, Image):
                source = getattr(component, "file", None) or getattr(
                    component, "url", None
                )
                if not source:
                    logger.warning(
                        f"{SEND_LOG_PREFIX} skip image component without source "
                        f"chat={chat_id} index={index}"
                    )
                    continue
                data, mime_type, _ = await asyncio.to_thread(
                    _load_binary_from_path, source
                )
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
                logger.info(
                    f"{SEND_LOG_PREFIX} append image payload "
                    f"chat={chat_id} index={index} summary={_summarize_payload(payload)}"
                )
                continue

            if isinstance(component, (File, Record)):
                source = getattr(component, "file", None) or getattr(
                    component, "url", None
                )
                if not source:
                    logger.warning(
                        f"{SEND_LOG_PREFIX} skip file/record component without source "
                        f"chat={chat_id} index={index}"
                    )
                    continue
                data, _, filename = await asyncio.to_thread(
                    _load_binary_from_path, source
                )
                payload = {
                    "chatId": chat_id,
                    "file": {
                        "data": base64.b64encode(data).decode("utf-8"),
                        "filename": getattr(component, "name", None)
                        or filename
                        or "file",
                    },
                }
                text = "".join(text_buffer).strip()
                if text:
                    payload["text"] = text
                text_buffer.clear()
                payloads.append(payload)
                logger.info(
                    f"{SEND_LOG_PREFIX} append file payload "
                    f"chat={chat_id} index={index} summary={_summarize_payload(payload)}"
                )
                continue

            serialized = component.to_dict() if hasattr(component, "to_dict") else None
            if inspect.isawaitable(serialized):
                logger.warning(
                    f"{SEND_LOG_PREFIX} awaitable to_dict detected "
                    f"chat={chat_id} index={index} component={_component_type_name(component)}"
                )
                serialized = await serialized
            if isinstance(serialized, dict):
                cls._push_text(text_buffer, str(serialized))
                logger.info(
                    f"{SEND_LOG_PREFIX} append serialized component as text "
                    f"chat={chat_id} index={index} serialized_keys={sorted(serialized.keys())}"
                )
            else:
                logger.warning(
                    f"{SEND_LOG_PREFIX} unsupported component ignored "
                    f"chat={chat_id} index={index} component={_component_type_name(component)}"
                )

        flush_text_only()
        payload_summaries = [_summarize_payload(item) for item in payloads]
        logger.info(
            f"{SEND_LOG_PREFIX} build payloads done "
            f"chat={chat_id} payload_count={len(payloads)} summaries={payload_summaries}"
        )
        return payloads

    @classmethod
    async def send_message_chain(
        cls,
        client: WeChatClient,
        chat_id: str,
        message_chain: MessageChain,
    ) -> None:
        logger.info(
            f"{SEND_LOG_PREFIX} send chain start "
            f"chat={chat_id} components={len(message_chain.chain)}"
        )
        payloads = await cls._build_send_payloads(chat_id, message_chain)
        if not payloads:
            logger.warning(f"{SEND_LOG_PREFIX} no payload built chat={chat_id}")
            return

        for idx, payload in enumerate(payloads):
            payload_summary = _summarize_payload(payload)
            logger.info(
                f"{SEND_LOG_PREFIX} send payload start "
                f"chat={chat_id} idx={idx + 1}/{len(payloads)} summary={payload_summary}"
            )
            try:
                result = await asyncio.to_thread(
                    client.send_message,
                    payload,
                    timeout=SEND_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.exceptions.ReadTimeout as exc:
                logger.exception(
                    f"{SEND_LOG_PREFIX} send payload timeout "
                    f"chat={chat_id} idx={idx + 1}/{len(payloads)} summary={payload_summary}"
                )
                raise RuntimeError(
                    "agent-wechat 发送超时（30秒未响应），"
                    "请检查微信客户端是否卡在聊天切换/自动化操作中。"
                ) from exc
            except requests.exceptions.RequestException as exc:
                logger.exception(
                    f"{SEND_LOG_PREFIX} send payload request error "
                    f"chat={chat_id} idx={idx + 1}/{len(payloads)} summary={payload_summary}"
                )
                raise RuntimeError(f"agent-wechat 发送请求失败: {exc}") from exc
            if not result.get("success", True):
                logger.error(
                    f"{SEND_LOG_PREFIX} send payload failed "
                    f"chat={chat_id} idx={idx + 1}/{len(payloads)} summary={payload_summary} "
                    f"error={result.get('error')}"
                )
                raise RuntimeError(result.get("error") or "agent-wechat 发送失败")
            logger.info(
                f"{SEND_LOG_PREFIX} send payload ok "
                f"chat={chat_id} idx={idx + 1}/{len(payloads)} result_keys={sorted(result.keys())}"
            )
        logger.info(
            f"{SEND_LOG_PREFIX} send chain done chat={chat_id} payload_count={len(payloads)}"
        )

    async def send(self, message: MessageChain) -> None:
        logger.info(
            f"{SEND_LOG_PREFIX} event.send "
            f"chat={self.chat_id} is_group={self.is_group} components={len(message.chain)}"
        )
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

    async def get_group(
        self, group_id: str | None = None, **kwargs: Any
    ) -> Group | None:
        if not self.is_group:
            return None
        raw_group_id = (
            group_id or getattr(self.message_obj, "group_id", None) or self.chat_id
        )
        group = Group(str(raw_group_id))
        group.group_name = getattr(
            getattr(self.message_obj, "group", None), "group_name", None
        )
        return group
