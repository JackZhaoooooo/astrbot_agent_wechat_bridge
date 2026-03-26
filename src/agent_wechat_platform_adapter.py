"""基于事件流与接口轮询的个人微信平台适配器。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from contextlib import suppress
from datetime import datetime
from typing import Any, cast

from astrbot.api import logger
from astrbot.api.message_components import File, Image, Plain, Record
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from .agent_wechat_access import (
    is_group_chat,
    is_official_account,
    normalize_allowlist,
    strip_leading_mentions,
    should_forward_message,
)
from .agent_wechat_client import AgentWeChatAPIError, WeChatClient
from .agent_wechat_client import WeChatEventWebSocketClient
from .agent_wechat_event import AgentWeChatMessageEvent

MSG_TYPE_TEXT = 1
MSG_TYPE_IMAGE = 3
MSG_TYPE_VOICE = 34
MSG_TYPE_VIDEO = 43
MSG_TYPE_APP = 49
MEDIA_TYPES = {MSG_TYPE_IMAGE, MSG_TYPE_VOICE, MSG_TYPE_VIDEO, MSG_TYPE_APP}

CONFIG_METADATA = {
    "en-US": {
        "server_url": {
            "label": "Server URL",
            "help_text": "Base URL of the agent-wechat REST service.",
            "field_type": "str",
        },
        "token": {
            "label": "Token",
            "help_text": "Optional bearer token used by agent-wechat.",
            "field_type": "str",
            "secret": True,
        },
        "poll_interval_ms": {
            "label": "Poll Interval",
            "help_text": "Fallback REST sync interval in milliseconds when WS events are idle.",
            "field_type": "int",
        },
        "auth_poll_interval_ms": {
            "label": "Auth Poll Interval",
            "help_text": "Login status refresh interval in milliseconds.",
            "field_type": "int",
        },
        "dm_policy": {
            "label": "DM Policy",
            "help_text": "open, allowlist, or disabled.",
            "field_type": "select",
            "options": [
                {"label": "Open", "value": "open"},
                {"label": "Allowlist", "value": "allowlist"},
                {"label": "Disabled", "value": "disabled"},
            ],
        },
        "allow_from": {
            "label": "DM Allowlist",
            "help_text": "Allowed sender IDs when DM policy is allowlist.",
            "field_type": "list",
        },
        "group_policy": {
            "label": "Group Policy",
            "help_text": "open, allowlist, or disabled.",
            "field_type": "select",
            "options": [
                {"label": "Open", "value": "open"},
                {"label": "Allowlist", "value": "allowlist"},
                {"label": "Disabled", "value": "disabled"},
            ],
        },
        "group_allow_from": {
            "label": "Group Sender Allowlist",
            "help_text": "Allowed group sender IDs when group policy is allowlist.",
            "field_type": "list",
        },
        "require_mention": {
            "label": "Require Mention",
            "help_text": "Only forward group messages that mention the bot.",
            "field_type": "bool",
        },
        "active_probe_limit": {
            "label": "Active Probe Limit",
            "help_text": "Probe latest chats directly via list_messages each sync to reduce receive latency caused by stale unread metadata.",
            "field_type": "int",
        },
        "active_probe_fetch_limit": {
            "label": "Active Probe Fetch Limit",
            "help_text": "Message fetch limit for each actively probed chat.",
            "field_type": "int",
        },
        "active_probe_open_chat": {
            "label": "Active Probe Open Chat",
            "help_text": "Whether active probing should call open_chat to refresh chat state before pulling messages.",
            "field_type": "bool",
        },
        "media_retry_attempts": {
            "label": "Media Retry Attempts",
            "help_text": "Retry attempts when media data is not ready yet.",
            "field_type": "int",
        },
        "media_retry_interval_ms": {
            "label": "Media Retry Interval",
            "help_text": "Retry interval in milliseconds for media fetch.",
            "field_type": "int",
        },
    }
}

DEFAULT_CONFIG = {
    "server_url": "http://localhost:6174",
    "token": "",
    "poll_interval_ms": 1000,
    "auth_poll_interval_ms": 30000,
    "dm_policy": "open",
    "allow_from": [],
    "group_policy": "open",
    "group_allow_from": [],
    "require_mention": True,
    "active_probe_limit": 5,
    "active_probe_fetch_limit": 5,
    "active_probe_open_chat": True,
    "media_retry_attempts": 4,
    "media_retry_interval_ms": 250,
}


def _parse_timestamp(value: str | None) -> int:
    if not value:
        return int(time.time())
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(time.time())


def _safe_temp_dir() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        return get_astrbot_temp_path()
    except Exception:
        return tempfile.gettempdir()


def _mime_to_component(path: str, mime_type: str, filename: str):
    if mime_type.startswith("image/"):
        return Image(file=path)
    if mime_type.startswith("audio/"):
        return Record(file=path)
    return File(name=filename or os.path.basename(path), file=path)


@register_platform_adapter(
    "agent_wechat",
    "WeChat adapter powered by agent-wechat.",
    default_config_tmpl=DEFAULT_CONFIG,
    support_streaming_message=False,
    config_metadata=CONFIG_METADATA,
    adapter_display_name="Agent WeChat",
)
class AgentWeChatPlatformAdapter(Platform):
    """以事件流为主触发，接口补偿兜底。"""

    def __init__(
        self,
        platform_config: dict[str, Any],
        platform_settings: dict[str, Any],
        event_queue: asyncio.Queue,
    ) -> None:
        try:
            super().__init__(platform_config, event_queue)
        except TypeError:
            super().__init__(event_queue)
            self.config = platform_config

        self.settings = platform_settings
        self.config = {**DEFAULT_CONFIG, **(platform_config or {})}
        self.metadata = PlatformMetadata(
            name="agent_wechat",
            description="WeChat adapter powered by agent-wechat.",
            id=cast(str, self.config.get("id", "agent_wechat")),
            support_streaming_message=False,
        )
        self.client = WeChatClient(
            base_url=str(self.config["server_url"]),
            token=str(self.config.get("token") or "") or None,
        )
        self.shutdown_event = asyncio.Event()
        self.sync_event = asyncio.Event()
        self.last_seen_id: dict[str, int] = {}
        self.last_auth_check = 0.0
        self.last_auth_status: str | None = None
        self.self_id = "agent_wechat"
        self.ws_task: asyncio.Task[None] | None = None
        self.ws_connected = False

    def meta(self) -> PlatformMetadata:
        return self.metadata

    def get_client(self) -> WeChatClient:
        return self.client

    async def terminate(self) -> None:
        self.shutdown_event.set()
        if self.ws_task is not None:
            self.ws_task.cancel()

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain,
    ) -> None:
        await AgentWeChatMessageEvent.send_message_chain(
            self.client,
            session.session_id,
            message_chain,
        )
        await super().send_by_session(session, message_chain)

    async def run(self) -> None:
        self.ws_task = asyncio.create_task(self._run_events_ws())
        self.sync_event.set()
        try:
            while not self.shutdown_event.is_set():
                try:
                    # 有事件时立即同步；无事件时按轮询超时兜底。
                    await asyncio.wait_for(
                        self.sync_event.wait(),
                        timeout=max(0.1, int(self.config["poll_interval_ms"]) / 1000),
                    )
                except asyncio.TimeoutError:
                    pass

                self.sync_event.clear()
                if self.shutdown_event.is_set():
                    break

                try:
                    await self._sync_once()
                except Exception as exc:
                    logger.exception(f"[agent_wechat] sync failed: {exc}")
        finally:
            self.shutdown_event.set()
            if self.ws_task is not None:
                self.ws_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.ws_task

    async def _run_events_ws(self) -> None:
        ws_client = WeChatEventWebSocketClient(
            self.client.build_events_ws_url(),
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_close=self._on_ws_close,
            on_error=self._on_ws_error,
        )
        await ws_client.run_forever(self.shutdown_event)

    async def _on_ws_open(self) -> None:
        self.ws_connected = True
        self.sync_event.set()

    async def _on_ws_close(self) -> None:
        self.ws_connected = False
        if not self.shutdown_event.is_set():
            logger.warning("[agent_wechat] events websocket disconnected")

    async def _on_ws_error(self, exc: Exception) -> None:
        if not self.shutdown_event.is_set():
            err_text = str(exc)
            if "HTTP 401" in err_text or "Unauthorized" in err_text:
                logger.warning(
                    "[agent_wechat] events websocket error: "
                    "未授权，终端运行wx up获取token，并填入平台配置"
                )
                return
            logger.warning(f"[agent_wechat] events websocket error: {exc}")

    async def _on_ws_message(self, raw_message: str) -> None:
        if not raw_message:
            self.sync_event.set()
            return

        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            self.sync_event.set()
            return

        await self._dispatch_ws_payload(payload)

    async def _dispatch_ws_payload(self, payload: Any) -> None:
        if isinstance(payload, list):
            for item in payload:
                await self._dispatch_ws_payload(item)
            return

        if not isinstance(payload, dict):
            self.sync_event.set()
            return

        event_type = str(
            payload.get("type")
            or payload.get("event")
            or payload.get("kind")
            or ""
        )

        if event_type in {"ping", "pong"}:
            return

        if event_type in {"auth", "login", "login_state", "login_success", "status"}:
            self.last_auth_check = 0
            self.sync_event.set()
            return

        if event_type in {"message", "message_received", "message_created", "wechat_message"}:
            chat = payload.get("chat")
            message = payload.get("message")
            if isinstance(chat, dict) and isinstance(message, dict):
                chat_id = str(chat.get("username") or chat.get("id") or "")
                local_id = int(message.get("localId", 0) or 0)
                if chat_id and local_id and local_id <= self.last_seen_id.get(chat_id, 0):
                    return
                converted = await self._convert_message(chat, message)
                if converted is not None:
                    await self.handle_msg(converted)
                    self._log_inbound_message(
                        source="ws",
                        chat=chat,
                        message=message,
                        session_id=converted.session_id,
                    )
                    if chat_id and local_id:
                        self.last_seen_id[chat_id] = max(self.last_seen_id.get(chat_id, 0), local_id)
                return

            chat_id = payload.get("chatId") or payload.get("chat_id")
            if chat_id:
                await self._sync_chat_by_id(str(chat_id))
                return

        self.sync_event.set()

    async def _sync_chat_by_id(self, chat_id: str) -> None:
        try:
            chat = await asyncio.to_thread(self.client.get_chat, chat_id)
        except Exception as exc:
            logger.warning(f"[agent_wechat] failed to load chat {chat_id} from ws event: {exc}")
            self.sync_event.set()
            return

        if not chat:
            self.sync_event.set()
            return

        await self._process_chat(chat, skip_open=False)

    async def _sync_once(self) -> None:
        if not await self._refresh_auth_if_needed():
            return

        chats = await asyncio.to_thread(self.client.list_chats, 50, 0)
        processed_chat_ids: set[str] = set()
        for chat in chats:
            chat_id = str(chat.get("username") or chat.get("id") or "")
            if not chat_id or chat_id in self.last_seen_id:
                continue
            if is_official_account(chat_id):
                continue

            unread = int(chat.get("unreadCount", 0) or 0)
            if unread > 0:
                continue
            last_msg_local_id = int(chat.get("lastMsgLocalId", 0) or 0)
            if last_msg_local_id > 0:
                self.last_seen_id[chat_id] = last_msg_local_id

        unread_chats = [
            chat
            for chat in chats
            if int(chat.get("unreadCount", 0) or 0) > 0
            and not is_official_account(chat.get("username") or chat.get("id") or "")
        ]
        for chat in unread_chats:
            if self.shutdown_event.is_set():
                break
            await self._process_chat(chat, skip_open=False)
            chat_id = str(chat.get("username") or chat.get("id") or "")
            if chat_id:
                processed_chat_ids.add(chat_id)

        unread_ids = {
            str(chat.get("username") or chat.get("id") or "")
            for chat in unread_chats
        }
        catchup_chats: list[dict[str, Any]] = []
        for chat in chats:
            if self.shutdown_event.is_set():
                break
            chat_id = str(chat.get("username") or chat.get("id") or "")
            if not chat_id or chat_id in unread_ids or chat_id not in self.last_seen_id:
                continue
            last_msg_local_id = int(chat.get("lastMsgLocalId", 0) or 0)
            if last_msg_local_id > self.last_seen_id[chat_id]:
                catchup_chats.append(chat)

        for chat in catchup_chats:
            if self.shutdown_event.is_set():
                break
            await self._process_chat(chat, skip_open=True)
            chat_id = str(chat.get("username") or chat.get("id") or "")
            if chat_id:
                processed_chat_ids.add(chat_id)

        await self._probe_active_chats(chats, processed_chat_ids)

    async def _probe_active_chats(
        self,
        chats: list[dict[str, Any]],
        processed_chat_ids: set[str],
    ) -> None:
        """主动探测最近会话，绕过未读元数据更新滞后。"""
        try:
            limit = max(0, int(self.config.get("active_probe_limit", 5) or 0))
        except (TypeError, ValueError):
            limit = 5
        if limit <= 0:
            return
        try:
            fetch_limit = max(1, int(self.config.get("active_probe_fetch_limit", 5) or 5))
        except (TypeError, ValueError):
            fetch_limit = 5
        open_chat_before_probe = bool(self.config.get("active_probe_open_chat", True))

        probed = 0
        for chat in chats:
            if self.shutdown_event.is_set() or probed >= limit:
                break
            chat_id = str(chat.get("username") or chat.get("id") or "")
            if not chat_id or chat_id in processed_chat_ids:
                continue
            if is_official_account(chat_id):
                continue

            await self._process_chat(
                chat,
                skip_open=not open_chat_before_probe,
                clear_unreads=False,
                fetch_limit_override=fetch_limit,
            )
            probed += 1

    async def _refresh_auth_if_needed(self) -> bool:
        now_ms = time.time() * 1000
        if now_ms - self.last_auth_check < int(self.config["auth_poll_interval_ms"]):
            return self.last_auth_status == "logged_in"

        self.last_auth_check = now_ms
        try:
            auth = await asyncio.to_thread(self.client.auth_status)
        except Exception as exc:
            logger.warning(f"[agent_wechat] auth check failed: {exc}")
            self.last_auth_status = None
            return False

        self.last_auth_status = str(auth.get("status") or "unknown")
        if auth.get("loggedInUser"):
            self.self_id = str(auth["loggedInUser"])

        if self.last_auth_status != "logged_in":
            return False
        return True

    async def _process_chat(
        self,
        chat: dict[str, Any],
        skip_open: bool = False,
        *,
        clear_unreads: bool = True,
        fetch_limit_override: int | None = None,
    ) -> None:
        chat_id = str(chat.get("username") or chat.get("id") or "")
        if not chat_id:
            return

        if not skip_open:
            try:
                await asyncio.to_thread(self.client.open_chat, chat_id, clear_unreads)
            except Exception as exc:
                logger.warning(f"[agent_wechat] failed to open chat {chat_id}: {exc}")

        if fetch_limit_override is None:
            fetch_limit = max(int(chat.get("unreadCount", 0) or 0), 20)
        else:
            fetch_limit = max(1, int(fetch_limit_override))
        messages = await asyncio.to_thread(self.client.list_messages, chat_id, fetch_limit, 0)
        if not messages:
            return

        new_messages = self._select_new_messages(chat_id, chat, messages)
        if not new_messages:
            return

        for message in new_messages:
            if bool(message.get("isSelf")):
                continue
            converted = await self._convert_message(chat, message)
            if converted is None:
                continue
            await self.handle_msg(converted)
            self._log_inbound_message(
                source="rest",
                chat=chat,
                message=message,
                session_id=converted.session_id,
            )

        self.last_seen_id[chat_id] = max(int(item.get("localId", 0) or 0) for item in new_messages)

    def _select_new_messages(
        self,
        chat_id: str,
        chat: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ordered = sorted(messages, key=lambda item: int(item.get("localId", 0) or 0))
        if chat_id not in self.last_seen_id:
            # 首次看到该会话：仅消费未读尾部，避免把历史消息整段回灌到机器人框架。
            unread = int(chat.get("unreadCount", 0) or 0)
            if unread <= 0:
                self.last_seen_id[chat_id] = int(ordered[-1].get("localId", 0) or 0)
                return []
            if unread < len(ordered):
                seen_max = int(ordered[-unread - 1].get("localId", 0) or 0)
                self.last_seen_id[chat_id] = seen_max
                return ordered[-unread:]
            return ordered

        prev_last_seen = self.last_seen_id[chat_id]
        return [item for item in ordered if int(item.get("localId", 0) or 0) > prev_last_seen]

    async def _convert_message(
        self,
        chat: dict[str, Any],
        message: dict[str, Any],
    ) -> AstrBotMessage | None:
        chat_id = str(chat.get("username") or chat.get("id") or "")
        sender_id = str(message.get("sender") or chat_id)
        sender_name = str(message.get("senderName") or sender_id or chat.get("name") or "WeChat")
        is_group = is_group_chat(chat_id) or bool(chat.get("isGroup"))
        raw_text = str(message.get("content") or "")
        normalized_text = strip_leading_mentions(raw_text) if is_group else raw_text.strip()
        was_mentioned = bool(message.get("isMentioned"))

        allowed, reason = should_forward_message(
            is_group=is_group,
            sender_id=sender_id,
            was_mentioned=was_mentioned,
            require_mention=bool(self.config.get("require_mention", True)),
            dm_policy=str(self.config.get("dm_policy", "open")),
            dm_allowlist=normalize_allowlist(self.config.get("allow_from")),
            group_policy=str(self.config.get("group_policy", "open")),
            group_allowlist=normalize_allowlist(self.config.get("group_allow_from")),
        )
        if not allowed:
            return None

        components: list[Any] = []
        message_str_parts: list[str] = []

        if normalized_text:
            components.append(Plain(text=normalized_text))
            message_str_parts.append(normalized_text)

        base_type = int(message.get("type", 0) or 0) & 0x7FFFFFFF
        if base_type in MEDIA_TYPES:
            media = await self._download_media(chat_id, int(message.get("localId", 0) or 0))
            if media is not None:
                path, mime_type, filename = media
                components.append(_mime_to_component(path, mime_type, filename))
                if not normalized_text:
                    if mime_type.startswith("image/"):
                        message_str_parts.append("<media:image>")
                    elif mime_type.startswith("audio/"):
                        message_str_parts.append("<media:audio>")
                    else:
                        message_str_parts.append(f"[file:{filename}]")

        reply = message.get("reply")
        if isinstance(reply, dict) and reply.get("content"):
            quoted = str(reply.get("content"))[:80]
            reply_block = f"[reply to {reply.get('sender') or 'unknown'}] {quoted}"
            components.append(Plain(text=reply_block))
            message_str_parts.append(reply_block)

        if not components:
            return None

        abm = AstrBotMessage()
        abm.self_id = self.self_id
        abm.sender = MessageMember(user_id=sender_id, nickname=sender_name)
        abm.message = components
        abm.message_str = "\n".join(part for part in message_str_parts if part).strip()
        abm.raw_message = {
            "chat": chat,
            "message": message,
        }
        abm.timestamp = _parse_timestamp(cast(str | None, message.get("timestamp")))
        abm.message_id = f"wechat:{chat_id}:{message.get('localId')}"

        if is_group:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = chat_id
            abm.group = Group(chat_id)
            abm.group.group_name = str(chat.get("name") or chat_id)
            abm.session_id = chat_id
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = sender_id

        return abm

    def _log_inbound_message(
        self,
        *,
        source: str,
        chat: dict[str, Any],
        message: dict[str, Any],
        session_id: str,
    ) -> None:
        chat_id = str(chat.get("username") or chat.get("id") or "")
        sender_id = str(message.get("sender") or chat_id or "")
        local_id = int(message.get("localId", 0) or 0)
        raw_type = int(message.get("type", 0) or 0)
        base_type = raw_type & 0x7FFFFFFF
        logger.info(
            "[agent_wechat] inbound accepted "
            f"source={source} "
            f"chat={chat_id} "
            f"sender={sender_id} "
            f"localId={local_id} "
            f"type={base_type} "
            f"session={session_id}"
        )

    async def _download_media(
        self,
        chat_id: str,
        local_id: int,
    ) -> tuple[str, str, str] | None:
        try:
            max_attempts = max(1, int(self.config.get("media_retry_attempts", 4) or 4))
        except (TypeError, ValueError):
            max_attempts = 4
        try:
            retry_interval = max(
                0.0,
                float(self.config.get("media_retry_interval_ms", 250) or 250) / 1000.0,
            )
        except (TypeError, ValueError):
            retry_interval = 0.25

        result: dict[str, Any] | None = None
        for attempt in range(max_attempts):
            try:
                candidate = await asyncio.to_thread(self.client.get_media, chat_id, local_id)
            except AgentWeChatAPIError as exc:
                logger.warning(f"[agent_wechat] media fetch failed for {chat_id}:{local_id}: {exc}")
                return None
            except Exception as exc:
                logger.warning(f"[agent_wechat] media fetch error for {chat_id}:{local_id}: {exc}")
                return None

            if candidate.get("type") == "unsupported":
                return None
            if candidate.get("data"):
                result = candidate
                break
            if attempt < max_attempts - 1 and retry_interval > 0:
                # 上游媒体落库可能有延迟，短暂等待后重试。
                await asyncio.sleep(retry_interval)

        if result is None:
            return None
        encoded = result.get("data")
        if not encoded:
            return None

        media_type = str(result.get("type") or "file")
        media_format = str(result.get("format") or "bin").lower()
        filename = str(result.get("filename") or f"{local_id}.{media_format}")

        mime_map = {
            "image": {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "webp": "image/webp",
            },
            "voice": {
                "mp3": "audio/mpeg",
                "wav": "audio/wav",
                "ogg": "audio/ogg",
            },
            "video": {
                "mp4": "video/mp4",
            },
        }
        mime_type = mime_map.get(media_type, {}).get(media_format, "application/octet-stream")

        directory = os.path.join(_safe_temp_dir(), "agent_wechat_bridge")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{chat_id.replace('/', '_')}_{local_id}_{filename}")
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(encoded))
        return path, mime_type, filename

    async def handle_msg(self, message: AstrBotMessage) -> None:
        is_group = getattr(message, "type", None) == MessageType.GROUP_MESSAGE
        chat_id = getattr(message, "group_id", None) if is_group else message.session_id
        event = AgentWeChatMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            client=self.client,
            chat_id=str(chat_id),
            is_group=bool(is_group),
        )
        self.commit_event(event)
