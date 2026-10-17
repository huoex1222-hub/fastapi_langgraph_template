# 协议一致性对照

后端实现的是 Agent Protocol，`@ag-ui/langgraph` 调的是它下面的 LangGraph SDK。
把 SDK 会打的路径和后端注册的路由（`docs/openapi.json`）逐项对一遍。

## 前端实际用到的 10 个 —— 全部一致

| SDK 方法 | 后端路由 |
| --- | --- |
| `assistants.search` | `POST /assistants/search` |
| `assistants.getGraph` | `GET /assistants/{assistant_id}/graph` |
| `assistants.getSchemas` | `GET /assistants/{assistant_id}/schemas` |
| `threads.create` | `POST /threads` |
| `threads.get` | `GET /threads/{thread_id}` |
| `threads.getState` | `GET /threads/{thread_id}/state` |
| `threads.getHistory` | `GET /threads/{thread_id}/history` |
| `threads.updateState` | `POST /threads/{thread_id}/state` |
| `runs.stream` | `POST /threads/{thread_id}/runs/stream` |
| `runs.cancel` | `POST /threads/{thread_id}/runs/{run_id}/cancel` |

这 10 个是 `@ag-ui/langgraph/dist/index.js` 里 `this.client.*` 的**全部**调用点。

## 全量协议面

后端已实现（与 SDK 对应）：

| 面 | 覆盖 |
| --- | --- |
| Assistants | `GET/POST /assistants`、`/{id}` 的 GET/PATCH/DELETE、`/search`、`/count`、`/{id}/latest`、`/versions`、`/graph`、`/schemas`、`/subgraphs` |
| Threads | `GET/POST /threads`、`/{id}`、`/state`、`/state/{ckpt}`、`/state/checkpoint`、`/history`、`/search`、`/prune` |
| Runs | `/{tid}/runs`、`/stream`、`/wait`、`/{rid}`、`/{rid}/cancel`、`/{rid}/join`、`/{rid}/stream`；stateless 的 `/runs`、`/runs/stream`、`/runs/wait`、`/runs/cancel` |
| Crons | `/runs/crons`、`/{tid}/runs/crons`、`/{cid}`、`/search`、`/count` |
| Store | `/store/items`、`/store/items/search`、`/store/namespaces` |
| v2 事件流 | `/threads/{id}/stream/events`、`/threads/{id}/commands` |

后端缺的 4 个（SDK 有，前端不用）：

| SDK 方法 | SDK 路径 | 说明 |
| --- | --- | --- |
| `threads.count()` | `POST /threads/count` | 线程计数 |
| `threads.copy()` | `POST /threads/{id}/copy` | 复制线程 |
| `threads.joinStream()` | `GET /threads/{id}/stream` | **thread 级长连接**（带 `Last-Event-ID`） |
| `runs.joinStream(id, null)` | `GET /runs/{run_id}/stream` | stateless run 的断点续流 |

这四个不影响当前前端；但若要「订阅整个 thread 的事件」或「线程计数」，会撞上。

---

[← 返回大纲](README.md)
