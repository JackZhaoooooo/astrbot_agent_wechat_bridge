"""插件根入口。

框架要求插件根目录存在 `main.py`。该文件用于将根入口桥接到
`src/` 下的实际适配器实现。
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import sys
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

try:
    from .src.agent_wechat_platform_adapter import (
        AgentWeChatPlatformAdapter as _AgentWeChatPlatformAdapter,
    )
except ImportError:
    from src.agent_wechat_platform_adapter import (
        AgentWeChatPlatformAdapter as _AgentWeChatPlatformAdapter,
    )

AgentWeChatPlatformAdapter = _AgentWeChatPlatformAdapter


@register(
    "astrbot_agent_wechat_bridge",
    "Codex",
    "AstrBot platform adapter for agent-wechat.",
    "0.3.19",
)
class AgentWeChatBridgePlugin(Star):
    """加载平台适配器并完成注册。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or AstrBotConfig()
        AgentWeChatPlatformAdapter.set_logout_notifier(self._notify_logout)
        AgentWeChatPlatformAdapter.set_logout_notify_policy(
            interval_seconds=self._get_logout_notify_interval_seconds(),
            max_count=self._get_logout_notify_max_count(),
        )

    def _get_logout_notify_umos(self) -> list[str]:
        raw = self.config.get("logout_notify_umos", [])
        if not isinstance(raw, list):
            return []
        umos: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if value:
                umos.append(value)
        return umos

    def _get_logout_notify_max_count(self) -> int:
        raw = self.config.get("logout_notify_max_count", 0)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0
        return max(0, value)

    def _get_logout_notify_interval_seconds(self) -> float:
        raw = self.config.get("logout_notify_interval_seconds", 60)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 60.0
        return max(1.0, value)

    async def _broadcast_chain(self, chain: MessageChain) -> int:
        umos = self._get_logout_notify_umos()
        if not umos:
            return 0

        sent = 0
        for umo in umos:
            try:
                ok = await self.context.send_message(umo, chain)
                if ok:
                    sent += 1
                else:
                    logger.warning(f"[agent_wechat] UMO 发送失败: {umo}")
            except Exception as exc:
                logger.warning(f"[agent_wechat] UMO 发送异常: umo={umo} err={exc}")
        return sent

    async def _notify_logout(self, warn_text: str) -> None:
        _ = warn_text
        AgentWeChatPlatformAdapter.set_logout_notify_policy(
            interval_seconds=self._get_logout_notify_interval_seconds(),
            max_count=self._get_logout_notify_max_count(),
        )
        chain = MessageChain().message(
            "[agent-wechat]微信好像退出登录了呢，输入/wxauth登录吧"
        )
        await self._broadcast_chain(chain)

    async def terminate(self) -> None:
        AgentWeChatPlatformAdapter.set_logout_notifier(None)

    @filter.command("wxauth")
    async def wxauth(self, event: AstrMessageEvent):
        """执行 wx auth login 并将终端二维码截图推送到配置的 UMO 列表。"""
        yield event.plain_result("开始执行 `wx auth login`，正在抓取终端二维码截图...")

        output = await self._run_wx_auth_login_capture()
        image_url = await self._render_terminal_capture_image(output)

        chain = MessageChain().message("[agent-wechat] `wx auth login` 终端二维码截图")
        if image_url:
            chain.url_image(image_url)
        else:
            chain.message("\n（截图生成失败，请查看日志）")

        sent_count = await self._broadcast_chain(chain)
        if sent_count <= 0:
            yield event.plain_result(
                "未发送：请先在插件配置的“退出登录提醒”中填写 UMO 列表（可用 /sid 获取）。"
            )
            return

        yield event.plain_result(f"已推送 wx 登录二维码截图到 {sent_count} 个 UMO。")

    async def _run_wx_auth_login_capture(self) -> str:
        cmd = "wx auth login"
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            return f"启动命令失败: {exc}"

        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
            output = raw.decode("utf-8", errors="replace") if raw else ""
        except asyncio.TimeoutError:
            output = ""
            if proc.stdout is not None:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(16384), timeout=1.5)
                    output = chunk.decode("utf-8", errors="replace") if chunk else ""
                except Exception:
                    pass
            with contextlib.suppress(Exception):
                proc.kill()
                await proc.wait()

        output = output.strip()
        if not output:
            output = "命令已执行，但未捕获到终端输出。"
        return output

    async def _render_terminal_capture_image(self, output: str) -> str | None:
        escaped = html.escape(output)
        template = """
        <div style=\"display:inline-block;padding:12px 14px;background:#0b1220;color:#e5e7eb;font-family:Consolas,Monaco,monospace;border-radius:8px;\">
          <div style=\"font-size:16px;font-weight:600;margin-bottom:8px;color:#93c5fd;\">wx auth login terminal capture</div>
          <pre style=\"margin:0;white-space:pre-wrap;word-break:break-word;line-height:1.35;font-size:15px;\">{{ text }}</pre>
        </div>
        """
        options = {
            "full_page": False,
            "omit_background": False,
        }
        try:
            return await self.html_render(
                template,
                {"text": escaped},
                return_url=True,
                options=options,
            )
        except Exception as exc:
            logger.warning(f"[agent_wechat] 终端截图渲染失败: {exc}")
            return None
