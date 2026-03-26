from src.agent_wechat_client import WeChatClient


def test_build_events_ws_url_with_token() -> None:
    client = WeChatClient("http://localhost:6174", token="secret")
    assert client.build_events_ws_url() == "ws://localhost:6174/api/ws/events?token=secret"


def test_build_events_ws_url_https() -> None:
    client = WeChatClient("https://example.com/base", token=None)
    assert client.build_events_ws_url() == "wss://example.com/base/api/ws/events"


def test_send_message_supports_timeout_override(monkeypatch) -> None:
    client = WeChatClient("http://localhost:6174", token="secret")
    captured: dict[str, object] = {}

    def fake_post(path, body=None, *, timeout=None):
        captured["path"] = path
        captured["body"] = body
        captured["timeout"] = timeout
        return {"success": True}

    monkeypatch.setattr(client, "_post", fake_post)
    payload = {"chatId": "wxid_x", "text": "hello"}
    result = client.send_message(payload, timeout=30.0)

    assert result == {"success": True}
    assert captured["path"] == "/api/messages/send"
    assert captured["body"] == payload
    assert captured["timeout"] == 30.0
