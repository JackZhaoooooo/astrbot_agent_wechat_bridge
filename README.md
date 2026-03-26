# AstrBot Agent WeChat Bridge

这是一个用于将 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 接入个人微信的插件，底层依赖 [`agent-wechat`](https://github.com/thisnick/agent-wechat) 提供的 WebSocket 和 REST API。

本项目现在采用“`/api/ws/events` 事件流优先 + REST 补偿同步”的接入方式，参考了上游 `agent-wechat` 仓库中的 WebSocket 与消息同步实现思路：

- `WS /api/ws/events`：建立事件 WebSocket 连接
- `GET /api/status/auth`：检查微信登录状态
- `GET /api/chats`：做补偿同步，获取未读会话
- `POST /api/chats/{id}/open`：打开会话并清除未读
- `GET /api/messages/{id}`：拉取新消息
- `GET /api/messages/{id}/media/{localId}`：下载媒体附件
- `POST /api/messages/send`：发送回复

## 架构图

```mermaid
flowchart LR
    subgraph AW["agent-wechat 容器"]
        WS["/api/ws/events\nWebSocket 事件流"]
        REST["REST API\nstatus / chats / messages / media / send"]
        WX["WeChat Desktop"]
    end

    WS -->|实时事件 / 唤醒信号| BRIDGE["AstrBot Agent WeChat Bridge\nWS 客户端"]
    BRIDGE -->|补偿同步 / 媒体下载 / 发送消息| REST
    BRIDGE -->|AstrBotMessage 事件| CORE["AstrBot Core"]
    CORE -->|MessageChain 回复| BRIDGE
    REST --> WX
```

说明：

- WebSocket 连接负责“优先触发”同步
- REST 负责鉴权检查、消息补偿、媒体下载和消息发送
- 这样即使上游事件流暂时没有完整广播所有消息，也不会影响插件可用性

## 功能说明

- 注册 AstrBot 平台适配器 `agent_wechat`
- 通过 WebSocket 客户端连接 `agent-wechat`
- 在 WS 事件或空闲超时后执行 REST 补偿同步
- 将微信私聊和群聊消息转换为 AstrBot 事件
- 将 AstrBot 的回复通过 `agent-wechat` 发回微信
- 支持文本、图片、文件、语音类消息发送
- 支持私聊白名单、群聊白名单、群聊 `@` 触发

## 运行前提

1. 已部署并可访问的 `agent-wechat` 服务
2. 对应微信账号已通过 `agent-wechat` 登录
3. AstrBot 版本 `>= 4.16`

## 快速使用


### 先安装agent-wechat

```bash
npm install -g @agent-wechat/cli
wx up
```
- wx up运行后得到类似
```bash
[root@VM-0-10-opencloudos ~]# wx up
Container agent-wechat is already running.
API: http://localhost:6174
noVNC: http://localhost:6174/vnc/?token=cda8bb0945b636df2c5802edad83e2417f3cc9500e65cb4f3xxxxxxxxx&autoconnect=true
```
- cda8bb0945b636df2c5802edad83e2417f3cc9500e65cb4f3xxxxxxxxx,即为平台配置的token，后续配置要用到

### 在astrbot安装本项目的插件，安装后启用并重启

### 在侧边菜单中点击“机器人”，点右侧的“创建机器人”，在选择平台最下面会有个“agant_wechat”平台

### 填入上面获取到的token，启用即可

## 平台配置

AstrBot 加载插件后，在平台管理中添加 `agent_wechat`，配置项如下：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `server_url` | `http://localhost:6174` | `agent-wechat` REST API 地址 |
| `token` | 空 | 如果服务开启鉴权，填写 Bearer Token |
| `poll_interval_ms` | `200` | 快速探测循环间隔（毫秒） |
| `full_sync_interval_ms` | `900` | 全量会话同步间隔（毫秒） |
| `auth_poll_interval_ms` | `30000` | 登录状态检查间隔 |
| `hot_path_timeout_ms` | `1200` | 低延迟热路径（`list_messages/open_chat`）超时阈值（毫秒） |
| `dm_policy` | `open` | 私聊策略：`open` / `allowlist` / `disabled` |
| `allow_from` | `[]` | 私聊白名单，`dm_policy=allowlist` 时生效 |
| `group_policy` | `open` | 群聊策略：`open` / `allowlist` / `disabled` |
| `group_allow_from` | `[]` | 群聊发送者白名单，`group_policy=allowlist` 时生效 |
| `require_mention` | `true` | 群聊中是否必须 `@机器人` 才转发给 AstrBot |
| `active_probe_limit` | `5` | 每轮主动探测的会话数量上限 |
| `fast_probe_limit` | `2` | 每轮快速探测的活跃会话数量 |
| `fast_probe_fetch_limit` | `2` | 快速探测单会话拉取消息条数 |
| `fast_probe_open_chat` | `true` | 快速探测未命中时是否执行 `open_chat(clearUnreads=false)` 轻量刷新 |
| `active_probe_fetch_limit` | `3` | 主动探测单会话拉取消息条数 |
| `active_probe_open_chat` | `false` | 主动探测前是否执行 `open_chat(clearUnreads=false)` 轻量刷新 |
| `media_retry_attempts` | `4` | 媒体消息拉取重试次数 |
| `media_retry_interval_ms` | `250` | 媒体消息重试间隔（毫秒） |

也可以直接在 `cmd_config.json` 的 `platform` 数组里确保存在如下项：

```json
{
  "id": "agent_wechat",
  "type": "agent_wechat",
  "enable": true,
  "server_url": "http://localhost:6174",
  "token": "你的_agent_wechat_token",
  "poll_interval_ms": 200,
  "full_sync_interval_ms": 900,
  "auth_poll_interval_ms": 30000,
  "hot_path_timeout_ms": 1200,
  "dm_policy": "open",
  "allow_from": [],
  "group_policy": "open",
  "group_allow_from": [],
  "require_mention": true,
  "active_probe_limit": 5,
  "fast_probe_limit": 2,
  "fast_probe_fetch_limit": 2,
  "fast_probe_open_chat": true,
  "active_probe_fetch_limit": 3,
  "active_probe_open_chat": false,
  "media_retry_attempts": 4,
  "media_retry_interval_ms": 250
}
```

## 常见排查

- 插件已安装但“完全没日志、没反应”：
  - 检查 AstrBot 的 `cmd_config.json` 是否真的有 `type=agent_wechat` 且 `enable=true`
  - 检查 `token` 是否正确。若 `curl http://localhost:6174/api/status/auth` 返回 `Unauthorized`，说明必须携带 token
- 日志里显示已收到消息但没有进入对话：
  - 检查 AstrBot 全局白名单。若开启 `enable_id_white_list=true`，需把会话 ID（如 `agent_wechat:FriendMessage:wxid_xxx`）加入 `id_whitelist`
- 群里发消息没触发：
  - 默认 `require_mention=true`，需要 `@机器人` 才会转发到 AstrBot
- 消息进入 AstrBot 有明显延迟（如十几秒）：
  - 先确认 `fast_probe_limit>=1`、`fast_probe_fetch_limit=1~3`、`fast_probe_open_chat=true`
  - 再确认 `poll_interval_ms=150~300`、`full_sync_interval_ms=700~1200`
  - `hot_path_timeout_ms` 建议 `800~1500`，避免慢请求阻塞低延迟探测
  - 若消息仍慢，可逐步提高 `fast_probe_limit`（建议最大不超过 `4`，避免接口负载过高）
  - 若消息里媒体较多，可适当降低 `media_retry_attempts` 或 `media_retry_interval_ms`，减少单轮阻塞时间
- 发送后想看是否收到了：
  - 查看 AstrBot 日志中的 `[agent_wechat] inbound accepted ...`

## 实现细节

- 忽略以 `gh_` 开头的公众号/服务号会话
- 首次同步某个会话时，只处理未读尾部消息，不回灌整段历史
- 媒体下载采用与上游一致的方式：先 `open chat`，再按 `localId` 取媒体
- 针对媒体准备延迟，下载逻辑带有重试机制
- 当消息数据库写入晚于未读状态变化时，会用 `lastMsgLocalId` 做补偿轮询
- 当前上游的 `/api/ws/events` 路由已经存在，但实时消息广播仍未完全接通，所以插件保留 REST 补偿同步以保证稳定性
- 由于 `agent-wechat` 没有原生的微信 `@` 发送接口，AstrBot 的 `At` 组件会降级为普通文本

## 更新日志

从当前版本开始，每次版本更新都会同步记录到 [CHANGELOG.md](./CHANGELOG.md)。

## 本地校验

```bash
python3 -m compileall src tests
pytest
```

如果当前环境没有安装 `pytest`，请先执行：

```bash
pip install -r requirements.txt
```
