"""平台适配器使用的消息事件对象。"""

from __future__ import annotations

import asyncio
import base64
import inspect
import mimetypes
import os
import re
import unicodedata
from collections.abc import AsyncGenerator
from io import BytesIO
from typing import Any

import requests
from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    At,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.api.platform import Group

from .agent_wechat_client import WeChatClient

SEND_REQUEST_TIMEOUT_SECONDS = 30.0
SEND_LOG_PREFIX = "[agent_wechat][send]"
SEND_RECOVERY_RETRY_ATTEMPTS = 3
SEND_RECOVERY_RETRY_INTERVAL_SECONDS = 1.0
SEND_RECOVERY_ERRORS = {"No action selected"}
IGNORED_SERIALIZED_SEG_TYPES = {"reply"}
MAX_FILENAME_LENGTH = 96


def _component_type_name(component: Any) -> str:
    return type(component).__name__


def _guess_mime_type(path: str, fallback: str = "application/octet-stream") -> str:
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type or fallback


def _basename_from_url(url: str, default: str = "file") -> str:
    name = url.rstrip("/").split("/")[-1].split("?")[0]
    return name or default


def _extract_segment_source(seg_data: dict[str, Any]) -> str | None:
    for key in ("file", "url", "path", "src", "local_path", "temp_file"):
        value = seg_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("file", "url", "path", "src"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return None


def _extract_segment_filename(seg_data: dict[str, Any], fallback: str = "file") -> str:
    for key in ("name", "filename", "file_name", "title"):
        value = seg_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _sanitize_filename(name: str, fallback: str = "file.bin") -> str:
    original = (name or "").strip()
    if not original:
        original = fallback
    original = os.path.basename(original).replace("\x00", "")
    stem, ext = os.path.splitext(original)
    ext = (ext or ".bin")[:16]
    normalized = unicodedata.normalize("NFKD", stem)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("._-")
    if not safe:
        safe = "file"
    max_stem_len = max(8, MAX_FILENAME_LENGTH - len(ext))
    safe = safe[:max_stem_len]
    return f"{safe}{ext}"


def _normalize_image_for_wechat(data: bytes, mime_type: str) -> tuple[bytes, str]:
    lower_mime = (mime_type or "").lower()
    if lower_mime in {"image/png", "image/jpeg", "image/jpg", "image/gif"}:
        return data, ("image/jpeg" if lower_mime == "image/jpg" else lower_mime)
    try:
        with PILImage.open(BytesIO(data)) as img:
            if img.mode not in {"RGB", "RGBA"}:
                img = img.convert("RGBA")
            output = BytesIO()
            img.save(output, format="PNG")
            return output.getvalue(), "image/png"
    except Exception:
        logger.warning(
            f"{SEND_LOG_PREFIX} failed to normalize image mime={mime_type}, keep original bytes"
        )
        return data, mime_type or "image/png"


async def _segment_to_dict(seg: Any) -> dict[str, Any] | None:
    if isinstance(seg, dict):
        return seg
    if not hasattr(seg, "to_dict"):
        return None
    serialized = seg.to_dict()
    if inspect.isawaitable(serialized):
        serialized = await serialized
    if isinstance(serialized, dict):
        return serialized
    return None


def _load_binary_from_path(
    path: str,
    timeout: int = 30,
    fallback_mime: str = "application/octet-stream",
) -> tuple[bytes, str, str]:
    if path.startswith("base64://"):
        encoded = path.split("://", 1)[1].lstrip("/")
        data = base64.b64decode(encoded)
        return data, fallback_mime, "inline.bin"

    if path.startswith(("http://", "https://")):
        response = requests.get(path, timeout=timeout)
        response.raise_for_status()
        mime_type = response.headers.get("Content-Type") or _guess_mime_type(
            path, fallback_mime
        )
        return response.content, mime_type, _basename_from_url(path)

    normalized = path[8:] if path.startswith("file:///") else path
    with open(normalized, "rb") as handle:
        data = handle.read()
    return (
        data,
        _guess_mime_type(normalized, fallback_mime),
        os.path.basename(normalized),
    )


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
        payloads: list[dict[str, Any]] = []
        text_buffer: list[str] = []

        def flush_text_only() -> None:
            if not text_buffer:
                return
            text = "".join(text_buffer)
            if text != "":
                payloads.append({"chatId": chat_id, "text": text})
            text_buffer.clear()

        async def expand_serialized_nodes(serialized: dict[str, Any]) -> int:
            messages = serialized.get("messages")
            if not isinstance(messages, list):
                return 0

            flush_text_only()
            expanded = 0
            for node_item in messages:
                if not isinstance(node_item, dict):
                    continue
                node_data = node_item.get("data")
                if not isinstance(node_data, dict):
                    continue
                content = node_data.get("content")
                if not isinstance(content, list):
                    continue

                node_text_parts: list[str] = []

                def flush_node_text() -> None:
                    nonlocal expanded
                    if not node_text_parts:
                        return
                    node_text = "".join(node_text_parts)
                    if node_text != "":
                        payloads.append({"chatId": chat_id, "text": node_text})
                        expanded += 1
                    node_text_parts.clear()

                for seg_raw in content:
                    seg = await _segment_to_dict(seg_raw)
                    if not isinstance(seg, dict):
                        continue
                    seg_type = str(seg.get("type") or "").lower()
                    seg_data = seg.get("data")
                    if not isinstance(seg_data, dict):
                        continue

                    if seg_type in {"text", "plain"}:
                        seg_text = str(seg_data.get("text") or "")
                        if seg_text != "":
                            node_text_parts.append(seg_text)
                        continue

                    if seg_type == "image":
                        source = _extract_segment_source(seg_data)
                        if not source:
                            logger.warning(
                                f"{SEND_LOG_PREFIX} skip serialized node image without source "
                                f"chat={chat_id}"
                            )
                            continue
                        flush_node_text()
                        data, mime_type, _ = await asyncio.to_thread(
                            _load_binary_from_path,
                            str(source),
                            30,
                            "image/png",
                        )
                        image_data, normalized_mime = await asyncio.to_thread(
                            _normalize_image_for_wechat,
                            data,
                            mime_type or "image/png",
                        )
                        payloads.append(
                            {
                                "chatId": chat_id,
                                "image": {
                                    "data": base64.b64encode(image_data).decode(
                                        "utf-8"
                                    ),
                                    "mimeType": normalized_mime or "image/png",
                                },
                            }
                        )
                        expanded += 1
                        continue

                    if seg_type in {"video", "file", "record", "audio"}:
                        source = _extract_segment_source(seg_data)
                        if not source:
                            logger.warning(
                                f"{SEND_LOG_PREFIX} skip serialized node file without source "
                                f"chat={chat_id} seg_type={seg_type}"
                            )
                            continue
                        flush_node_text()
                        data, _, filename = await asyncio.to_thread(
                            _load_binary_from_path, str(source)
                        )
                        safe_filename = _sanitize_filename(
                            _extract_segment_filename(
                                seg_data, fallback=str(filename or "file.bin")
                            )
                        )
                        payloads.append(
                            {
                                "chatId": chat_id,
                                "file": {
                                    "data": base64.b64encode(data).decode("utf-8"),
                                    "filename": safe_filename,
                                },
                            }
                        )
                        expanded += 1
                        continue

                flush_node_text()

            return expanded

        async def expand_node_component(component: Node | Nodes) -> int:
            flush_text_only()

            node_items = (
                list(getattr(component, "nodes", []) or [])
                if isinstance(component, Nodes)
                else [component]
            )
            expanded = 0
            for node_item in node_items:
                node_content = list(getattr(node_item, "content", []) or [])
                if not node_content:
                    continue
                nested_payloads = await cls._build_send_payloads(
                    chat_id, MessageChain(node_content)
                )
                if not nested_payloads:
                    continue
                payloads.extend(nested_payloads)
                expanded += len(nested_payloads)
            return expanded

        for component in message_chain.chain:
            if isinstance(component, (Nodes, Node)):
                expanded = await expand_node_component(component)
                if expanded > 0:
                    continue

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
                        f"{SEND_LOG_PREFIX} skip image component without source chat={chat_id}"
                    )
                    continue
                data, mime_type, _ = await asyncio.to_thread(
                    _load_binary_from_path, source, 30, "image/png"
                )
                image_data, normalized_mime = await asyncio.to_thread(
                    _normalize_image_for_wechat, data, mime_type or "image/png"
                )
                text = "".join(text_buffer)
                if text != "":
                    payloads.append({"chatId": chat_id, "text": text})
                text_buffer.clear()
                payloads.append(
                    {
                        "chatId": chat_id,
                        "image": {
                            "data": base64.b64encode(image_data).decode("utf-8"),
                            "mimeType": normalized_mime or "image/png",
                        },
                    }
                )
                continue

            if isinstance(component, (File, Record, Video)):
                source = getattr(component, "file", None) or getattr(
                    component, "url", None
                )
                if not source:
                    logger.warning(
                        f"{SEND_LOG_PREFIX} skip file/record/video component without source "
                        f"chat={chat_id}"
                    )
                    continue
                data, _, filename = await asyncio.to_thread(
                    _load_binary_from_path, source
                )
                safe_filename = _sanitize_filename(
                    str(getattr(component, "name", None) or filename or "file.bin")
                )
                text = "".join(text_buffer)
                if text != "":
                    payloads.append({"chatId": chat_id, "text": text})
                text_buffer.clear()
                payloads.append(
                    {
                        "chatId": chat_id,
                        "file": {
                            "data": base64.b64encode(data).decode("utf-8"),
                            "filename": safe_filename,
                        },
                    }
                )
                continue

            serialized = component.to_dict() if hasattr(component, "to_dict") else None
            if inspect.isawaitable(serialized):
                serialized = await serialized

            if isinstance(serialized, dict):
                seg_type = str(serialized.get("type") or "").lower()
                if seg_type in IGNORED_SERIALIZED_SEG_TYPES:
                    continue
                seg_data = serialized.get("data")
                if isinstance(seg_data, dict) and seg_type in {
                    "video",
                    "file",
                    "record",
                    "audio",
                }:
                    source = _extract_segment_source(seg_data)
                    if source:
                        text = "".join(text_buffer)
                        if text != "":
                            payloads.append({"chatId": chat_id, "text": text})
                        text_buffer.clear()
                        data, _, filename = await asyncio.to_thread(
                            _load_binary_from_path, str(source)
                        )
                        safe_filename = _sanitize_filename(
                            _extract_segment_filename(
                                seg_data, fallback=str(filename or "file.bin")
                            )
                        )
                        payloads.append(
                            {
                                "chatId": chat_id,
                                "file": {
                                    "data": base64.b64encode(data).decode("utf-8"),
                                    "filename": safe_filename,
                                },
                            }
                        )
                        continue

                expanded = await expand_serialized_nodes(serialized)
                if expanded > 0:
                    continue
                cls._push_text(text_buffer, str(serialized))
                continue

            logger.warning(
                f"{SEND_LOG_PREFIX} unsupported component ignored "
                f"chat={chat_id} component={_component_type_name(component)}"
            )

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
        if not payloads:
            return

        for idx, payload in enumerate(payloads):
            recovered = False
            for attempt in range(1, SEND_RECOVERY_RETRY_ATTEMPTS + 1):
                try:
                    result = await asyncio.to_thread(
                        client.send_message,
                        payload,
                        timeout=SEND_REQUEST_TIMEOUT_SECONDS,
                    )
                except requests.exceptions.ReadTimeout as exc:
                    logger.exception(
                        f"{SEND_LOG_PREFIX} send payload timeout "
                        f"chat={chat_id} idx={idx + 1}/{len(payloads)}"
                    )
                    raise RuntimeError(
                        "agent-wechat 发送超时（30秒未响应），"
                        "请检查微信客户端是否卡在聊天切换/自动化操作中。"
                    ) from exc
                except requests.exceptions.RequestException as exc:
                    logger.exception(
                        f"{SEND_LOG_PREFIX} send payload request error "
                        f"chat={chat_id} idx={idx + 1}/{len(payloads)}"
                    )
                    raise RuntimeError(f"agent-wechat 发送请求失败: {exc}") from exc

                if result.get("success", True):
                    recovered = True
                    break

                error = str(result.get("error") or "")
                recoverable = error in SEND_RECOVERY_ERRORS
                if recoverable and attempt < SEND_RECOVERY_RETRY_ATTEMPTS:
                    logger.warning(
                        f"{SEND_LOG_PREFIX} recoverable send error "
                        f"chat={chat_id} idx={idx + 1}/{len(payloads)} "
                        f"attempt={attempt}/{SEND_RECOVERY_RETRY_ATTEMPTS} "
                        f"error={error}; trying open_chat and retry"
                    )
                    try:
                        await asyncio.to_thread(client.open_chat, chat_id, False)
                    except Exception as exc:
                        logger.warning(
                            f"{SEND_LOG_PREFIX} open_chat before retry failed "
                            f"chat={chat_id} idx={idx + 1}/{len(payloads)} error={exc}"
                        )
                    await asyncio.sleep(SEND_RECOVERY_RETRY_INTERVAL_SECONDS)
                    continue

                logger.error(
                    f"{SEND_LOG_PREFIX} send payload failed "
                    f"chat={chat_id} idx={idx + 1}/{len(payloads)} "
                    f"error={error}"
                )
                raise RuntimeError(error or "agent-wechat 发送失败")

            if not recovered:
                raise RuntimeError("agent-wechat 发送失败：重试后仍失败")

    async def send(self, message: MessageChain) -> None:
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
