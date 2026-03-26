"""访问控制与文本归一化辅助函数。"""

from __future__ import annotations

import re

DM_POLICIES = {"open", "allowlist", "disabled"}
GROUP_POLICIES = {"open", "allowlist", "disabled"}
INVISIBLE_TEXT_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f]")


def normalize_wechat_id(raw: str | None) -> str:
    if not raw:
        return ""
    return raw.strip().removeprefix("wechat:").strip()


def normalize_allowlist(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        normalized = normalize_wechat_id(str(value))
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def strip_leading_mentions(text: str) -> str:
    """移除消息开头的微信 @提及，仅保留真正正文。"""

    cleaned = INVISIBLE_TEXT_RE.sub("", text or "").strip()
    if not cleaned:
        return ""

    parts = [part.strip() for part in re.split(r"\u2005+", cleaned) if part.strip()]
    while parts and parts[0].startswith(("@", "＠")):
        parts.pop(0)
    if parts:
        return " ".join(parts).strip()
    return cleaned


def is_official_account(chat_id: str) -> bool:
    return normalize_wechat_id(chat_id).startswith("gh_")


def is_group_chat(chat_id: str) -> bool:
    return normalize_wechat_id(chat_id).endswith("@chatroom")


def is_sender_allowed(sender_id: str | None, allowlist: list[str]) -> bool:
    if "*" in allowlist:
        return True
    normalized_sender = normalize_wechat_id(sender_id)
    if not normalized_sender:
        return False
    return normalized_sender in allowlist


def should_forward_message(
    *,
    is_group: bool,
    sender_id: str | None,
    was_mentioned: bool,
    require_mention: bool,
    dm_policy: str,
    dm_allowlist: list[str],
    group_policy: str,
    group_allowlist: list[str],
) -> tuple[bool, str]:
    """返回消息是否应转发到机器人框架。"""

    dm_policy = dm_policy if dm_policy in DM_POLICIES else "disabled"
    group_policy = group_policy if group_policy in GROUP_POLICIES else "disabled"

    if is_group:
        if group_policy == "disabled":
            return False, "group_policy_disabled"
        if group_policy == "allowlist" and not is_sender_allowed(sender_id, group_allowlist):
            return False, "group_sender_not_allowlisted"
        if require_mention and not was_mentioned:
            return False, "mention_required"
        return True, f"group_policy_{group_policy}"

    if dm_policy == "disabled":
        return False, "dm_policy_disabled"
    if dm_policy == "allowlist" and not is_sender_allowed(sender_id, dm_allowlist):
        return False, "dm_sender_not_allowlisted"
    return True, f"dm_policy_{dm_policy}"
