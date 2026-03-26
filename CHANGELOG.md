# 更新日志

本项目遵循“每次版本更新都记录变更”的约定。

## 0.3.11 - 2026-03-26

- 修复“群消息已进 AstrBot 但回复发不出去”的核心问题：发送链路新增保护窗口，发送期间抑制 `open_chat`，避免与轮询探测争抢会话焦点
- 新增配置 `outbound_guard_ms`（默认 `1800`），用于控制发送前后保护时长
- 入站探测与发送协同优化：发送进行中暂停快速探测/主动探测分支，降低 UI 自动化冲突概率
- 统一发送入口：事件回复与 `send_by_session` 都走同一发送保护逻辑，避免路径不一致导致的间歇性失败

## 0.3.10 - 2026-03-26

- 进一步优化低延迟链路：`chatId` 事件改为直接快速拉取消息，移除事件路径中的额外 `get_chat` 请求
- 新增 `hot_path_timeout_ms` 配置（默认 `1200ms`），为热路径 `list_messages/open_chat` 设置超时，防止慢请求阻塞消息接收
- 优化消息处理顺序：改为“先拉消息再按需刷新会话”，减少常见路径下的请求往返次数
- 增加按会话加锁与快速探测并发执行，降低同一会话重复处理概率，同时减少多会话场景的串行等待
- 默认参数继续向低时延倾斜：`poll_interval_ms=200`、`full_sync_interval_ms=900`、`fast_probe_fetch_limit=2`、`active_probe_fetch_limit=3`、`active_probe_open_chat=false`
- 收到消息日志提早输出：在消息成功转换后先记录 `inbound accepted`，再提交 AstrBot 事件

## 0.3.9 - 2026-03-26

- 新增“快速探测 + 全量同步”双通道收消息策略：默认每 `300ms` 进行活跃会话快速探测，同时每 `1200ms` 或事件触发时执行全量同步
- 新增 `fast_probe_limit`、`fast_probe_fetch_limit`、`fast_probe_open_chat`、`full_sync_interval_ms` 配置项，用于低延迟场景精细调优
- 优化活跃会话优先级：自动维护最近活跃会话列表，优先探测最近有收发消息的会话
- 新增“探测 miss 时轻量刷新”逻辑：快速探测未命中时执行 `open_chat(clearUnreads=false)` 再次拉取，提升消息可见性时效
- 默认 `poll_interval_ms` 从 `1000` 调整为 `300`，降低插件侧最小探测粒度

## 0.3.8 - 2026-03-26

- 优化低延迟探测链路：主动探测会话时支持 `open_chat(clearUnreads=false)` 轻量刷新，降低消息可见性滞后
- 新增配置 `active_probe_fetch_limit`（默认 `5`）与 `active_probe_open_chat`（默认 `true`），减少探测分支每轮拉取负担并可按需开关
- 优化媒体获取阻塞：新增 `media_retry_attempts`（默认 `4`）和 `media_retry_interval_ms`（默认 `250`），缩短媒体未就绪时的阻塞窗口
- 优化 HTTP 调用性能：`WeChatClient` 改为线程局部 `requests.Session` 复用连接，减少频繁建连开销

## 0.3.7 - 2026-03-26

- 优化 WebSocket 鉴权失败提示：当 `/api/ws/events` 返回 `HTTP 401/Unauthorized` 时，日志改为更友好的中文指引
- 新提示文案：`未授权，终端运行wx up获取token，并填入平台配置`

## 0.3.6 - 2026-03-26

- 修复“消息晚几秒才进 AstrBot”的主要原因：对最近会话增加直接 `list_messages` 探测，降低对 `unreadCount/lastMsgLocalId` 更新滞后的依赖
- 新增配置 `active_probe_limit`（默认 `5`），可控制每轮低延迟探测的会话数量
- 保持“仅入站消息 info 日志 + 报错日志”的输出策略不变

## 0.3.5 - 2026-03-26

- 按需精简日志：仅保留“收到消息”的 `inbound accepted` 信息日志
- 移除适配器内其他普通 `info/debug` 日志（启动、同步轮次、鉴权状态、过滤原因等）
- 保留全部报错相关日志（`warning` / `exception`）

## 0.3.4 - 2026-03-26

- 修复 `open_chat` 参数序列化问题：将 `clearUnreads` 布尔值改为小写 `true/false`，兼容 `agent-wechat` 接口校验
- 减少未读会话重复拉取告警，避免因 `clearUnreads` 失败导致的持续轮询噪音
- 补充接入排查闭环：确认消息已到达适配器后，提示检查 AstrBot 会话白名单

## 0.3.3 - 2026-03-26

- 修复“插件已安装但没有任何收消息反应”的主因：补充平台启用与鉴权场景下的可观测性
- 增强 `agent_wechat` 适配器日志，新增启动参数、鉴权状态变化、同步轮次、消息接收与过滤原因日志
- 改进补偿同步：为 `unreadCount=0` 的会话建立 `lastMsgLocalId` 基线，避免新会话无未读时长期漏收

## 0.3.2 - 2026-03-26

- 修复 `register_platform_adapter()` 参数不兼容导致插件导入失败的问题
- 移除不被 AstrBot v4.22.1 支持的 `support_proactive_message` 参数

## 0.3.1 - 2026-03-26

- 修复插件无法在 AstrBot 插件列表中显示的问题
- 在插件根目录新增 `main.py` 入口，符合 AstrBot 的插件识别要求
- 增强根入口导入兼容性，兼容不同的模块加载方式

## 0.3.0 - 2026-03-26

- 将适配器调整为 WebSocket 客户端优先架构，连接 `agent-wechat` 的 `/api/ws/events`
- 保留 REST 补偿同步逻辑，兼容上游事件流尚未完整广播消息的现状
- 新增 WebSocket URL 构建与事件流客户端封装
- README 增加中文架构图，并更新为 WS + REST 的接入说明

## 0.2.0 - 2026-03-26

- 重建整个仓库，替换为全新的 AstrBot `agent-wechat` 接入插件
- 参考 `agent-wechat` 上游 `openclaw-extension` 的轮询和媒体处理流程实现适配器
- 新增 AstrBot 平台适配器 `agent_wechat`
- 支持微信私聊、群聊、媒体下载、消息回发
- 支持私聊白名单、群聊白名单和群聊 `@` 触发策略
- 新增基础测试和中文文档说明
