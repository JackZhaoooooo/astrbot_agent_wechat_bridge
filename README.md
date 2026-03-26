# Agent WeChat Bridge for AstrBot

This repository is a fresh AstrBot plugin that connects AstrBot to personal WeChat through [`agent-wechat`](https://github.com/thisnick/agent-wechat).

The implementation follows the same integration pattern used by the upstream [`openclaw-extension`](https://github.com/thisnick/agent-wechat/tree/main/packages/openclaw-extension):

- `GET /api/status/auth` to check login state
- `GET /api/chats` to find chats with unread messages
- `POST /api/chats/{id}/open` to open the chat and clear unread state
- `GET /api/messages/{id}` to fetch new messages
- `GET /api/messages/{id}/media/{localId}` to download attachments
- `POST /api/messages/send` to send replies

## What this plugin does

- Registers a new AstrBot platform adapter named `agent_wechat`
- Polls unread WeChat chats from `agent-wechat`
- Converts inbound WeChat messages into AstrBot events
- Sends AstrBot replies back through `agent-wechat`
- Supports text, image, file, and voice/file-like outbound messages
- Supports DM and group sender policies

## Prerequisites

1. A working `agent-wechat` server.
2. A WeChat account logged in through that server.
3. AstrBot `>= 4.16`.

Example local startup:

```bash
npm install -g @agent-wechat/cli
wx up
```

Example Docker Compose:

```yaml
services:
  agent-wechat:
    image: ghcr.io/thisnick/agent-wechat:latest
    container_name: agent-wechat
    security_opt:
      - seccomp=unconfined
    cap_add:
      - SYS_PTRACE
    ports:
      - "6174:6174"
    volumes:
      - agent-wechat-data:/data
      - agent-wechat-home:/home/wechat
    restart: unless-stopped

volumes:
  agent-wechat-data:
  agent-wechat-home:
```

## Install into AstrBot

Clone or copy this repository into AstrBot's plugin directory, then restart AstrBot.

## Platform configuration

After AstrBot loads the plugin, add the `agent_wechat` platform adapter and configure:

| Key | Default | Description |
| --- | --- | --- |
| `server_url` | `http://localhost:6174` | Base URL of the `agent-wechat` REST API |
| `token` | empty | Bearer token if your server is protected |
| `poll_interval_ms` | `1000` | Poll interval for unread chats |
| `auth_poll_interval_ms` | `30000` | Login state refresh interval |
| `dm_policy` | `open` | `open`, `allowlist`, or `disabled` |
| `allow_from` | `[]` | Allowed DM sender IDs when `dm_policy=allowlist` |
| `group_policy` | `open` | `open`, `allowlist`, or `disabled` |
| `group_allow_from` | `[]` | Allowed group sender IDs when `group_policy=allowlist` |
| `require_mention` | `true` | Require `@bot` in groups before forwarding to AstrBot |

## Notes

- Official/service accounts whose IDs start with `gh_` are ignored.
- On the first poll for a chat, the adapter only forwards the unread tail, not the full history.
- Media handling mirrors upstream behavior: open the chat first, then fetch media by `localId`.
- `agent-wechat` does not expose native mention sending, so outbound `At` components are downgraded to plain text.

## Local validation

```bash
python3 -m compileall src tests
pytest
```

`pytest` requires the test dependency from `requirements.txt`.
