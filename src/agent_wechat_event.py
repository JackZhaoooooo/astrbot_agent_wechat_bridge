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
MERGE_NODES_TO_SINGLE_TEXT = True
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060-\u206f\ufeff]")
FORWARD_HEADER_RE = re.compile(r"^from\s+@[^:]{1,100}:?$", re.IGNORECASE)
MAX_FILENAME_LENGTH = 96


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
    if isinstance(component, (File, Record, Video)):
        source = getattr(component, "file", None) or getattr(component, "url", None)
        name = getattr(component, "name", None)
        return f"{comp_type}(source={source}, name={name})"
    if isinstance(component, Nodes):
        nodes = list(getattr(component, "nodes", []) or [])
        return f"{comp_type}(nodes={len(nodes)})"
    if isinstance(component, Node):
        content = list(getattr(component, "content", []) or [])
        return f"{comp_type}(content={len(content)})"
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


def _component_to_merge_text(component: Any) -> str:
    if isinstance(component, Plain):
        return str(getattr(component, "text", "") or "")
    if isinstance(component, At):
        name = (
            getattr(component, "name", None) or getattr(component, "qq", None) or "user"
        )
        return f"@{name}"
    if isinstance(component, Image):
        return "[image]"
    if isinstance(component, Video):
        return "[video]"
    if isinstance(component, Record):
        return "[audio]"
    if isinstance(component, File):
        return "[file]"
    return f"[{_component_type_name(component)}]"


def _segment_dict_to_merge_text(segment: dict[str, Any]) -> str:
    seg_type = str(segment.get("type") or "").lower()
    seg_data = segment.get("data")
    if not isinstance(seg_data, dict):
        return ""
    if seg_type in {"text", "plain"}:
        return str(seg_data.get("text") or "")
    if seg_type == "image":
        return "[image]"
    if seg_type == "video":
        return "[video]"
    if seg_type in {"record", "audio", "voice"}:
        return "[audio]"
    if seg_type == "file":
        return "[file]"
    if seg_type == "at":
        target = (
            seg_data.get("name") or seg_data.get("qq") or seg_data.get("id") or "user"
        )
        return f"@{target}"
    return f"[{seg_type or 'segment'}]"


def _component_to_summary_text(component: Any) -> str:
    if isinstance(component, Plain):
        return str(getattr(component, "text", "") or "")
    if isinstance(component, At):
        name = (
            getattr(component, "name", None) or getattr(component, "qq", None) or "user"
        )
        return f"@{name}"
    return ""


def _segment_dict_to_summary_text(segment: dict[str, Any]) -> str:
    seg_type = str(segment.get("type") or "").lower()
    seg_data = segment.get("data")
    if not isinstance(seg_data, dict):
        return ""
    if seg_type in {"text", "plain"}:
        return str(seg_data.get("text") or "")
    if seg_type == "at":
        target = (
            seg_data.get("name") or seg_data.get("qq") or seg_data.get("id") or "user"
        )
        return f"@{target}"
    return ""


def _component_media_marker(component: Any) -> bool:
    return isinstance(component, (Image, Video, Record, File))


def _segment_media_marker(seg_type: str) -> bool:
    return seg_type in {"image", "video", "record", "audio", "voice", "file"}


def _normalize_merged_text(value: str) -> str:
    text = ZERO_WIDTH_RE.sub("", value or "")
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    if lines and FORWARD_HEADER_RE.match(lines[0]) and len(lines) > 1:
        lines = lines[1:]
    return " ".join(lines).strip()


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

        def merge_node_component_text(component: Node | Nodes) -> str:
            node_items = (
                list(getattr(component, "nodes", []) or [])
                if isinstance(component, Nodes)
                else [component]
            )
            lines: list[str] = []
            media_only_count = 0
            for node_item in node_items:
                node_content = list(getattr(node_item, "content", []) or [])
                if not node_content:
                    continue
                sender = str(getattr(node_item, "name", "") or "").strip()
                parts = [
                    _normalize_merged_text(_component_to_summary_text(item)).strip()
                    for item in node_content
                ]
                parts = [part for part in parts if part]
                if not parts:
                    if any(_component_media_marker(item) for item in node_content):
                        media_only_count += 1
                    continue
                body = _normalize_merged_text(" ".join(parts))
                if not body:
                    if any(_component_media_marker(item) for item in node_content):
                        media_only_count += 1
                    continue
                if sender:
                    lines.append(f"{sender}: {body}")
                else:
                    lines.append(body)
            if not lines and media_only_count <= 0:
                return ""
            if lines:
                return f"Merged message ({len(lines)} items):\n" + "\n".join(lines)
            return f"Merged message (media only: {media_only_count} items)"

        def merge_serialized_nodes_text(serialized: dict[str, Any]) -> str:
            messages = serialized.get("messages")
            if not isinstance(messages, list):
                return ""
            lines: list[str] = []
            media_only_count = 0
            for node_item in messages:
                if not isinstance(node_item, dict):
                    continue
                node_data = node_item.get("data")
                if not isinstance(node_data, dict):
                    continue
                sender = str(
                    node_data.get("nickname")
                    or node_data.get("name")
                    or node_data.get("user_id")
                    or ""
                ).strip()
                content = node_data.get("content")
                if not isinstance(content, list):
                    continue
                parts = [
                    _normalize_merged_text(_segment_dict_to_summary_text(seg)).strip()
                    for seg in content
                    if isinstance(seg, dict)
                ]
                parts = [part for part in parts if part]
                if not parts:
                    if any(
                        _segment_media_marker(str(seg.get("type") or "").lower())
                        for seg in content
                        if isinstance(seg, dict)
                    ):
                        media_only_count += 1
                    continue
                body = _normalize_merged_text(" ".join(parts))
                if not body:
                    if any(
                        _segment_media_marker(str(seg.get("type") or "").lower())
                        for seg in content
                        if isinstance(seg, dict)
                    ):
                        media_only_count += 1
                    continue
                if sender:
                    lines.append(f"{sender}: {body}")
                else:
                    lines.append(body)
            if not lines and media_only_count <= 0:
                return ""
            if lines:
                return f"Merged message ({len(lines)} items):\n" + "\n".join(lines)
            return f"Merged message (media only: {media_only_count} items)"

        async def expand_serialized_nodes(
            serialized: dict[str, Any],
            *,
            component_index: int,
            include_text: bool = True,
        ) -> int:
            messages = serialized.get("messages")
            if not isinstance(messages, list):
                return 0

            expanded = 0
            for node_idx, node_item in enumerate(messages):
                if not isinstance(node_item, dict):
                    continue
                node_data = node_item.get("data")
                if not isinstance(node_data, dict):
                    continue
                content = node_data.get("content")
                if not isinstance(content, list):
                    continue

                node_text_parts: list[str] = []

                async def flush_node_text() -> None:
                    node_text = "".join(node_text_parts).strip()
                    if not node_text:
                        return
                    if include_text:
                        payloads.append({"chatId": chat_id, "text": node_text})
                        logger.info(
                            f"{SEND_LOG_PREFIX} append node-text payload "
                            f"chat={chat_id} index={component_index} node={node_idx} "
                            f"text_len={len(node_text)}"
                        )
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
                        if seg_text:
                            node_text_parts.append(seg_text)
                        continue

                    if seg_type == "image":
                        source = _extract_segment_source(seg_data)
                        if not source:
                            logger.warning(
                                f"{SEND_LOG_PREFIX} skip serialized node image without source "
                                f"chat={chat_id} index={component_index} node={node_idx} "
                                f"seg_data_keys={sorted(seg_data.keys())}"
                            )
                            continue
                        await flush_node_text()
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
                        logger.info(
                            f"{SEND_LOG_PREFIX} append serialized node image payload "
                            f"chat={chat_id} index={component_index} node={node_idx} "
                            f"source={_truncate(str(source), 120)}"
                        )
                        continue

                    if seg_type in {"video", "file", "record", "audio"}:
                        source = _extract_segment_source(seg_data)
                        if not source:
                            logger.warning(
                                f"{SEND_LOG_PREFIX} skip serialized node file without source "
                                f"chat={chat_id} index={component_index} node={node_idx} "
                                f"seg_type={seg_type} seg_data_keys={sorted(seg_data.keys())}"
                            )
                            continue
                        await flush_node_text()
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
                        logger.info(
                            f"{SEND_LOG_PREFIX} append serialized node file payload "
                            f"chat={chat_id} index={component_index} node={node_idx} "
                            f"seg_type={seg_type} source={_truncate(str(source), 120)}"
                        )
                        continue

                node_text = "".join(node_text_parts).strip()
                if node_text and include_text:
                    payloads.append({"chatId": chat_id, "text": node_text})
                    expanded += 1
                    logger.info(
                        f"{SEND_LOG_PREFIX} append node-text payload "
                        f"chat={chat_id} index={component_index} node={node_idx} "
                        f"text_len={len(node_text)}"
                    )

            if expanded > 0:
                logger.info(
                    f"{SEND_LOG_PREFIX} expanded serialized nodes "
                    f"chat={chat_id} index={component_index} payloads={expanded}"
                )
            return expanded

        async def expand_node_component(
            component: Node | Nodes,
            *,
            component_index: int,
            include_text: bool = True,
        ) -> int:
            flush_text_only()

            node_items: list[Any]
            if isinstance(component, Nodes):
                node_items = list(getattr(component, "nodes", []) or [])
            else:
                node_items = [component]

            expanded = 0
            for node_idx, node_item in enumerate(node_items):
                node_content = list(getattr(node_item, "content", []) or [])
                if not node_content:
                    continue
                nested_payloads = await cls._build_send_payloads(
                    chat_id, MessageChain(node_content)
                )
                if not nested_payloads:
                    continue
                append_payloads = (
                    nested_payloads
                    if include_text
                    else [
                        payload
                        for payload in nested_payloads
                        if isinstance(payload, dict)
                        and ("image" in payload or "file" in payload)
                    ]
                )
                if not append_payloads:
                    continue
                payloads.extend(append_payloads)
                expanded += len(append_payloads)
                logger.info(
                    f"{SEND_LOG_PREFIX} expanded node component "
                    f"chat={chat_id} index={component_index} node={node_idx} "
                    f"payloads={len(append_payloads)} include_text={include_text}"
                )

            if expanded > 0:
                logger.info(
                    f"{SEND_LOG_PREFIX} expanded nodes from component "
                    f"chat={chat_id} index={component_index} payloads={expanded}"
                )
            return expanded

        for index, component in enumerate(message_chain.chain):
            logger.info(
                f"{SEND_LOG_PREFIX} process component "
                f"chat={chat_id} index={index} detail={_describe_component(component)}"
            )

            if isinstance(component, (Nodes, Node)):
                if MERGE_NODES_TO_SINGLE_TEXT:
                    merged_text = merge_node_component_text(component).strip()
                    if merged_text:
                        flush_text_only()
                        payloads.append({"chatId": chat_id, "text": merged_text})
                        logger.info(
                            f"{SEND_LOG_PREFIX} merged node component to single text "
                            f"chat={chat_id} index={index} text_len={len(merged_text)}"
                        )
                    expanded_media = await expand_node_component(
                        component,
                        component_index=index,
                        include_text=False,
                    )
                    if merged_text or expanded_media > 0:
                        if expanded_media > 0:
                            logger.info(
                                f"{SEND_LOG_PREFIX} merged node component appended media payloads "
                                f"chat={chat_id} index={index} payloads={expanded_media}"
                            )
                        continue
                expanded = await expand_node_component(
                    component,
                    component_index=index,
                )
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
                        f"{SEND_LOG_PREFIX} skip image component without source "
                        f"chat={chat_id} index={index}"
                    )
                    continue
                data, mime_type, _ = await asyncio.to_thread(
                    _load_binary_from_path, source, 30, "image/png"
                )
                image_data, normalized_mime = await asyncio.to_thread(
                    _normalize_image_for_wechat, data, mime_type or "image/png"
                )
                payload: dict[str, Any] = {
                    "chatId": chat_id,
                    "image": {
                        "data": base64.b64encode(image_data).decode("utf-8"),
                        "mimeType": normalized_mime or "image/png",
                    },
                }
                text = "".join(text_buffer).strip()
                if text:
                    payloads.append({"chatId": chat_id, "text": text})
                    logger.info(
                        f"{SEND_LOG_PREFIX} split text before image "
                        f"chat={chat_id} index={index} text_len={len(text)}"
                    )
                text_buffer.clear()
                payloads.append(payload)
                logger.info(
                    f"{SEND_LOG_PREFIX} append image payload "
                    f"chat={chat_id} index={index} summary={_summarize_payload(payload)}"
                )
                continue

            if isinstance(component, (File, Record, Video)):
                source = getattr(component, "file", None) or getattr(
                    component, "url", None
                )
                if not source:
                    logger.warning(
                        f"{SEND_LOG_PREFIX} skip file/record/video component without source "
                        f"chat={chat_id} index={index}"
                    )
                    continue
                data, _, filename = await asyncio.to_thread(
                    _load_binary_from_path, source
                )
                safe_filename = _sanitize_filename(
                    str(getattr(component, "name", None) or filename or "file.bin")
                )
                payload = {
                    "chatId": chat_id,
                    "file": {
                        "data": base64.b64encode(data).decode("utf-8"),
                        "filename": safe_filename,
                    },
                }
                text = "".join(text_buffer).strip()
                if text:
                    payloads.append({"chatId": chat_id, "text": text})
                    logger.info(
                        f"{SEND_LOG_PREFIX} split text before file "
                        f"chat={chat_id} index={index} text_len={len(text)}"
                    )
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
                if MERGE_NODES_TO_SINGLE_TEXT:
                    merged_text = merge_serialized_nodes_text(serialized).strip()
                    if merged_text:
                        flush_text_only()
                        payloads.append({"chatId": chat_id, "text": merged_text})
                        logger.info(
                            f"{SEND_LOG_PREFIX} merged serialized nodes to single text "
                            f"chat={chat_id} index={index} text_len={len(merged_text)}"
                        )
                    expanded_media = await expand_serialized_nodes(
                        serialized,
                        component_index=index,
                        include_text=False,
                    )
                    if merged_text or expanded_media > 0:
                        if expanded_media > 0:
                            logger.info(
                                f"{SEND_LOG_PREFIX} merged serialized nodes appended media payloads "
                                f"chat={chat_id} index={index} payloads={expanded_media}"
                            )
                        continue

                seg_type = str(serialized.get("type") or "").lower()
                seg_data = serialized.get("data")
                if isinstance(seg_data, dict) and seg_type in {
                    "video",
                    "file",
                    "record",
                    "audio",
                }:
                    source = _extract_segment_source(seg_data)
                    if source:
                        text = "".join(text_buffer).strip()
                        if text:
                            payloads.append({"chatId": chat_id, "text": text})
                            logger.info(
                                f"{SEND_LOG_PREFIX} split text before serialized file "
                                f"chat={chat_id} index={index} text_len={len(text)}"
                            )
                        text_buffer.clear()
                        data, _, filename = await asyncio.to_thread(
                            _load_binary_from_path, str(source)
                        )
                        safe_filename = _sanitize_filename(
                            _extract_segment_filename(
                                seg_data, fallback=str(filename or "file.bin")
                            )
                        )
                        payload = {
                            "chatId": chat_id,
                            "file": {
                                "data": base64.b64encode(data).decode("utf-8"),
                                "filename": safe_filename,
                            },
                        }
                        payloads.append(payload)
                        logger.info(
                            f"{SEND_LOG_PREFIX} append serialized file payload "
                            f"chat={chat_id} index={index} seg_type={seg_type} "
                            f"summary={_summarize_payload(payload)}"
                        )
                        continue

                expanded = await expand_serialized_nodes(
                    serialized,
                    component_index=index,
                )
                if expanded > 0:
                    continue
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
            recovered = False
            for attempt in range(1, SEND_RECOVERY_RETRY_ATTEMPTS + 1):
                logger.info(
                    f"{SEND_LOG_PREFIX} send payload start "
                    f"chat={chat_id} idx={idx + 1}/{len(payloads)} "
                    f"attempt={attempt}/{SEND_RECOVERY_RETRY_ATTEMPTS} "
                    f"summary={payload_summary}"
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

                if result.get("success", True):
                    logger.info(
                        f"{SEND_LOG_PREFIX} send payload ok "
                        f"chat={chat_id} idx={idx + 1}/{len(payloads)} "
                        f"attempt={attempt}/{SEND_RECOVERY_RETRY_ATTEMPTS} "
                        f"result_keys={sorted(result.keys())}"
                    )
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
                    f"chat={chat_id} idx={idx + 1}/{len(payloads)} summary={payload_summary} "
                    f"error={error}"
                )
                raise RuntimeError(error or "agent-wechat 发送失败")

            if not recovered:
                raise RuntimeError("agent-wechat 发送失败：重试后仍失败")
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
