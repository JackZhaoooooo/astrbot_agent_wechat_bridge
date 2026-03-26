"""AstrBot plugin entrypoint for the agent-wechat platform adapter."""

from astrbot.api.star import Context, Star, register

from .agent_wechat_platform_adapter import AgentWeChatPlatformAdapter  # noqa: F401


@register(
    "astrbot_agent_wechat_bridge",
    "Codex",
    "AstrBot platform adapter for agent-wechat.",
    "0.2.0",
)
class AgentWeChatBridgePlugin(Star):
    """Loads the platform adapter so AstrBot can register it."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
