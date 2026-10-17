# 前端调了哪些接口

## 两条通道

前端**自己的代码里只有一处直接网络调用**，其余全部由 `providers.tsx` 注入的
`LangGraphAgent` 在内部发出：

| # | 方式 | 谁在用 | 调什么 |
| --- | --- | --- | --- |
| ① | **AG-UI `LangGraphAgent`**（内部包 `@langchain/langgraph-sdk` 的 Client） | 对话相关的一切 | Agent Protocol `/threads/*`、`/runs/*` |
| ② | **裸 `fetch`** | 只有 `CatalogGrid.tsx:15` | 领域 REST `GET /shop/products` |
| ③ | `<img src>` | `CatalogGrid.tsx:64` | `picsum.photos`（外链，不是后端） |

```ts
// CatalogGrid.tsx:15 —— 前端源码里唯一一处自己的网络调用
fetch(`${AGENT_SERVER}/shop/products`)
```

## 通道 ①：AG-UI 封装的 Agent Protocol 调用

前端代码只写一句：

```ts
// providers.tsx:39
new LangGraphAgent({ deploymentUrl: AGENT_SERVER, graphId: id })
```

其余由 SDK 内部发。从 `@ag-ui/langgraph/dist/index.js` 能直接读到它调了哪些方法：

| SDK 方法 | → HTTP | 什么时候发 |
| --- | --- | --- |
| `threads.get` | `GET /threads/{id}` | 发消息前先探 thread 在不在（404 才 create） |
| `threads.create` | `POST /threads` | 上面 404 时 |
| `threads.getState` | `GET /threads/{id}/state` | 开流**之前**，捞历史消息 |
| **`runs.stream`** | **`POST /threads/{id}/runs/stream`** | **主事件流（SSE 长连接）** |
| `threads.updateState` | `POST /threads/{id}/state` | HITL 决议回写 |
| `threads.getHistory` | `GET /threads/{id}/history` | 恢复现场 |
| `runs.cancel` | `POST /threads/{id}/runs/{run_id}/cancel` | 停止生成 / 客户端断开 |

这张表是**代码可读到的全部方法**，一次「你好」只会用到其中一部分 —— 实际发了哪几条见
下方「实测调用链」。点商品卡片（`page.tsx:141` 的 `ask()`）复用同一 thread，只发 `runs/stream`。

## 通道 ②：为什么没有 SDK

后端是两个面**合并**进同一个 FastAPI 的（`langgraph.json` 的 `http.app` →
`src/http_app.py:app`，再由 `app/main.py:create_app` 合并核心路由）：

| 面 | 有 SDK 客户端吗 | 前端怎么调 |
| --- | --- | --- |
| Agent Protocol（`/threads`、`/runs`） | ✅ AG-UI + langgraph-sdk | 走封装 |
| 领域 REST（`/shop/products`） | ❌ 没有 | 组件里裸 `fetch` |

`CatalogGrid.tsx` 的注释点明了这点：*"Catalog browsing over the REST surface —
same backend the agent uses."*

## 怎么看真实调用

F12 → Network → 过滤框输 `2026`，再发消息。
`runs/stream` 会显示 `pending` 挂住几秒（流式期间一直不关），结束后变 `200`。

## 实测调用链

在 Customer 侧边栏发一句「你好」，Chrome Network 里实际是这 7 条：

```
GET  /threads/0890e1cc-…              ← threads.get（thread 已存在，没走到 create）
GET  /threads/0890e1cc-…/state        ← threads.getState，开流前捞历史
GET  /assistants/243920ff-…/schemas   ← 来源未确定
GET  /assistants/243920ff-…/graph     ← 来源未确定
POST /threads/0890e1cc-…/runs/stream  ← 主事件流
GET  /threads/0890e1cc-…/state        ← 之后又发了两次
GET  /threads/0890e1cc-…/state
```

**没有 `POST /threads`** —— `threads.get` 那次成功了，所以没走到 create。

**assistant_id 是算出来的，不是查出来的**（`repo/graphs/langgraph_service.py:196`）：

```python
NS = ASSISTANT_NAMESPACE_UUID        # 6ba7b821-9dad-11d1-80b4-00c04fd430c8
assistant_id = str(uuid5(NS, graph_id))
```

```
uuid5(NS, 'shopping_agent') = 243920ff-4b8f-5c79-a285-ee73d56fd22a   ← 与实测一致
uuid5(NS, 'merchant_agent') = 06a74579-e0a4-5bd2-83bc-d0170f90855a
uuid5(NS, 'research_agent') = 9dc0ca3b-1aa6-547d-93f0-e21597d2011c
```

同一个 graph 永远对应同一个 assistant UUID —— 客户端不需要先 `assistants/search` 去查。

## 两个待查的点

1. ~~谁发的 `/assistants/{id}/schemas` 和 `/graph`~~ → **已查明：就是 `@ag-ui/langgraph`
   自己发的。** `getSchemaKeys()` 调 `assistants.getSchemas`，`prepareStream()` 调
   `assistants.getGraph`（`dist/index.js` 里的 `this.client.assistants.*`）。
2. **`GET /threads/{id}/state` 为什么发 3 次**（开流前 1 次、开流后 2 次）—— 仍未查清。

> **更正**：`.workbuddy/memory/2026-10-08.md` 说序列第一步是 `assistants/search` ——
> **这条是对的**。`getAssistant()` 里调 `assistants.search({graphId, limit: 1})` 解析
> assistant_id。它没出现在这次按消息录制的 Network 窗口里，是因为它在更早的初始化阶段
> 就发了（或被缓存），不代表没有。

---

[← 返回大纲](README.md)
