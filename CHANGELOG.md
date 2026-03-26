# 更新日志

本项目遵循“每次版本更新都记录变更”的约定。

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
