"""AstrBot plugin root entrypoint.

AstrBot expects `main.py` at the plugin root. This file bridges the root
plugin entrypoint to the existing adapter implementation under `src/`.
"""

from pathlib import Path
import sys

from astrbot.api.star import Context, Star, register

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

try:
    from .src.agent_wechat_platform_adapter import AgentWeChatPlatformAdapter  # noqa: F401
except ImportError:
    from src.agent_wechat_platform_adapter import AgentWeChatPlatformAdapter  # noqa: F401


@register(
    "astrbot_agent_wechat_bridge",
    "Codex",
    "AstrBot platform adapter for agent-wechat.",
    "0.3.4",
)
class AgentWeChatBridgePlugin(Star):
    """Loads the platform adapter so AstrBot can register it."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
