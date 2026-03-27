"""插件根入口。

框架要求插件根目录存在 `main.py`。该文件用于将根入口桥接到
`src/` 下的实际适配器实现。
"""

from pathlib import Path
import sys

from astrbot.api.star import Context, Star, register

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

try:
    from .src.agent_wechat_platform_adapter import AgentWeChatPlatformAdapter
except ImportError:
    from src.agent_wechat_platform_adapter import AgentWeChatPlatformAdapter


@register(
    "astrbot_agent_wechat_bridge",
    "Codex",
    "AstrBot platform adapter for agent-wechat.",
    "0.3.17",
)
class AgentWeChatBridgePlugin(Star):
    """加载平台适配器并完成注册。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
