"""平台适配器插件入口。"""

from astrbot.api.star import Context, Star, register

from .agent_wechat_platform_adapter import AgentWeChatPlatformAdapter


@register(
    "astrbot_agent_wechat_bridge",
    "Codex",
    "AstrBot platform adapter for agent-wechat.",
    "0.3.8",
)
class AgentWeChatBridgePlugin(Star):
    """加载平台适配器并完成注册。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
