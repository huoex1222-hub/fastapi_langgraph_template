# 前端对话的接口链路：从「你好」到流式回答

在 Customer 侧边栏敲一句「你好」，浏览器一共打了哪些接口、回答从哪条连接回来、
以及 `runs/stream` 这条 SSE 两端各自怎么工作。

## TL;DR

```
浏览器 ─ GET  /threads/{id} ───────→ 探 thread 在不在（404 才 POST /threads 建）
       ─ GET  /threads/{id}/state → 捞【已有】消息（不是拿回答）
       ─ POST /threads/{id}/runs/stream → SSE 长连接，回答从这条回来
```

**最终回答走的是 `runs/stream`，不是 `GET /state`。** `getState` 在 stream **之前**执行，
拿的是发消息之前就存在的历史消息。

**前端自己的代码里只有一处网络调用**（`CatalogGrid.tsx:15` 的裸 `fetch`）——
其余全部由 `LangGraphAgent` 在内部发出。

**`runs/stream` 同时是触发器和订阅器**：它先 `executor.submit(job)` 把图扔进后台跑，
**再**返回 SSE 去 broker 订阅 —— 触发发生在返回响应之前，接口本身不是执行者。

**前端不走代理**：`providers.tsx` 里浏览器直连 `http://localhost:2026`，没有 proxy route。
所以 `HTTPS_PROXY` 那套（mitmweb）只作用于**后端进程**，浏览器的请求不去那儿。

## 目录

| # | 章节 | 内容 |
| --- | --- | --- |
| 01 | [协议分层](01-protocol-layers.md) | AG-UI vs Agent Protocol 的分层，AG-UI 到底干什么，v1/v2 两代 |
| 02 | [协议一致性对照](02-protocol-consistency.md) | SDK 会打的路径 vs 后端注册的路由 |
| 03 | [thread_id 和 run_id](03-thread-and-run-ids.md) | 两个 ID 的生命周期与用途 |
| 04 | [前端结构](04-frontend-structure.md) | providers / page / components |
| 05 | [前端调了哪些接口](05-frontend-calls.md) | 两条通道、10 个 SDK 方法、实测调用链 |
| 06 | [服务端：触发和订阅是两件事](06-trigger-and-subscribe.md) | 接口先触发 run，再返回 SSE 订阅 |
| 07 | [服务端：图和连接是解耦的](07-broker.md) | broker 内部、广播 + 历史、两种实现 |
| 08 | [完整链路](08-full-chain.md) | 从请求到事件回流的每个代码位置 |
| 09 | [响应头分层](09-response-headers.md) | 标准 SSE / 协议约定 / 仅为浏览器 SDK 的 CORS |
| 10 | [断线重连](10-reconnect.md) | 序号 ID、replay buffer、Last-Event-ID |
| 11 | [取消](11-cancel.md) | 显式 / 隐式两条路径，协作式取消的边界 |
| 12 | [SSE 是可选的吗](12-sse-optional.md) | 非流式端点可绕开，但 Pregel 的流绕不开 |
| 13 | [客户端：自己解析 SSE，不用 EventSource](13-client-sse-parsing.md) | getReader 逐块读、按空行切事件 |
| 14 | [坑](14-pitfalls.md) | 六个坑，含 web_search 是模拟的 |
| 15 | [broker 并发模型](15-broker-concurrency.md) | `backlog` 快照的原子性、线程/多进程边界 |
