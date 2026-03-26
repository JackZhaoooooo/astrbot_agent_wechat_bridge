from src.agent_wechat_client import WeChatClient


def test_build_events_ws_url_with_token() -> None:
    client = WeChatClient("http://localhost:6174", token="secret")
    assert client.build_events_ws_url() == "ws://localhost:6174/api/ws/events?token=secret"


def test_build_events_ws_url_https() -> None:
    client = WeChatClient("https://example.com/base", token=None)
    assert client.build_events_ws_url() == "wss://example.com/base/api/ws/events"
