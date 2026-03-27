import asyncio
import importlib
import sys
import types


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    components_mod = types.ModuleType("astrbot.api.message_components")
    platform_mod = types.ModuleType("astrbot.api.platform")

    class AstrMessageEvent:
        def __init__(
            self,
            message_str=None,
            message_obj=None,
            platform_meta=None,
            session_id=None,
        ):
            self.message_obj = message_obj

        async def send(self, message):
            return None

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

    class Plain:
        def __init__(self, text: str = "", **_):
            self.text = text

    class At:
        def __init__(self, qq=None, name=None, **_):
            self.qq = qq
            self.name = name

    class Image:
        def __init__(self, file=None, url=None, **_):
            self.file = file
            self.url = url

    class File:
        def __init__(self, name=None, file=None, url=None, **_):
            self.name = name
            self.file = file
            self.url = url

    class Record:
        def __init__(self, file=None, url=None, name=None, **_):
            self.file = file
            self.url = url
            self.name = name

    class Node:
        def __init__(self, content=None, **_):
            self.content = list(content or [])

    class Nodes:
        def __init__(self, nodes=None, **_):
            self.nodes = list(nodes or [])

    class Group:
        def __init__(self, group_id):
            self.group_id = group_id
            self.group_name = None

    api_mod.logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    components_mod.At = At
    components_mod.File = File
    components_mod.Image = Image
    components_mod.Node = Node
    components_mod.Nodes = Nodes
    components_mod.Plain = Plain
    components_mod.Record = Record
    platform_mod.Group = Group

    astrbot.api = api_mod
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = components_mod
    sys.modules["astrbot.api.platform"] = platform_mod


def _load_event_module():
    _install_astrbot_stubs()
    if "src.agent_wechat_event" in sys.modules:
        return importlib.reload(sys.modules["src.agent_wechat_event"])
    return importlib.import_module("src.agent_wechat_event")


def test_build_send_payloads_expands_nodes_to_multiple_texts():
    module = _load_event_module()
    chain = module.MessageChain(
        [
            module.Nodes(
                nodes=[
                    module.Node(content=[module.Plain(text="one")]),
                    module.Node(content=[module.Plain(text="two")]),
                ]
            )
        ]
    )

    payloads = asyncio.run(
        module.AgentWeChatMessageEvent._build_send_payloads("chat_x", chain)
    )

    assert payloads == [
        {"chatId": "chat_x", "text": "one"},
        {"chatId": "chat_x", "text": "two"},
    ]


def test_build_send_payloads_expands_serialized_nodes_messages():
    module = _load_event_module()

    class SerializedNodes:
        async def to_dict(self):
            return {
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "content": [
                                {"type": "text", "data": {"text": "first"}},
                            ]
                        },
                    },
                    {
                        "type": "node",
                        "data": {
                            "content": [
                                {"type": "text", "data": {"text": "second"}},
                            ]
                        },
                    },
                ]
            }

    chain = module.MessageChain([SerializedNodes()])
    payloads = asyncio.run(
        module.AgentWeChatMessageEvent._build_send_payloads("chat_y", chain)
    )

    assert payloads == [
        {"chatId": "chat_y", "text": "first"},
        {"chatId": "chat_y", "text": "second"},
    ]
