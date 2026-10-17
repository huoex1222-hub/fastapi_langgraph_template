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

## 协议分层

`/threads/{thread_id}/runs/stream` 是 **Agent Protocol** 定义的，**不是 AG-UI**。
两者是不同层的东西：

```
AG-UI                      事件协议：定义 RUN_STARTED / TEXT_MESSAGE_CONTENT /
   ↑                       STATE_SNAPSHOT / TOOL_CALL_* 这些【事件类型】，
   │ @ag-ui/langgraph      不定义任何 REST 路径
   │ （适配器）
LangGraph SDK              客户端：调 REST + 解析 SSE
   ↑
Agent Protocol             REST 协议：/assistants、/threads、/runs、/store、/crons
```

**AG-UI 是「调用方那层」，Agent Protocol 是「被调用的接口」。** `@ag-ui/langgraph`
只做翻译：

```
Agent Protocol 的 SSE 帧            AG-UI 事件
  values / updates / messages  →  STATE_SNAPSHOT / TEXT_MESSAGE_CONTENT / ...
```

项目自己也是这么定位的：

- `README.md:5` —— *"It speaks the Agent Protocol (LangGraph Platform compatible), so the
  LangGraph SDK, Agent Chat UI and LangGraph Studio work against it out of the box"*
- `docs/openapi.json` 的 `info.description` —— *"Production-ready Agent Protocol server"*

服务端只要符合 Agent Protocol，LangGraph SDK / Studio / Agent Chat UI 都能直连 ——
AG-UI 只是众多前端消费方式之一。

### AG-UI 到底干什么

**后端完全不知道 AG-UI 存在。** `grep -rni "ag-ui" src/` 只命中一条注释（`domain/runs.py:152`
提到「AG-UI 客户端会这么发」）—— 服务端只认 Agent Protocol，所以 LangGraph Studio /
Agent Chat UI / curl 都能连。

它存在的理由：**两者的「事件语义」不同**。

| Agent Protocol 的 SSE 帧 | 它是给状态机看的 |
| --- | --- |
| `values` | 完整 state 快照 |
| `updates` | 某节点的增量 |
| `messages` | LangChain 消息元组 |
| `checkpoint` | 断点信息 |

UI 要的不是这些，而是「往气泡追加一个字」「画一个工具卡片」「更新应用状态」。
`@ag-ui/langgraph` 就干这个翻译（`EventType.*` 共 20 种）：

| 收到的帧 | 翻成 AG-UI 事件 | UI 反应 |
| --- | --- | --- |
| `metadata`（含 run_id） | `RUN_STARTED` | 显示「思考中」 |
| token 流 | `TEXT_MESSAGE_START` / `CONTENT` / `END` | 打字机效果 |
| 工具调用 | `TOOL_CALL_START` / `ARGS` / `END` / `RESULT` | 工具卡片 |
| `values` / `updates` | `STATE_SNAPSHOT` | 更新 `agent.state` |
| 推理块 | `REASONING_START` / `MESSAGE_CONTENT` / `END` | 折叠的思考过程 |
| `end` / 错误 | `RUN_FINISHED` / `RUN_ERROR` | 结束状态 |

### 它在项目里干三件事

1. **HTTP 客户端** —— 那 10 个 `this.client.*` 调用，走 Agent Protocol（见下节）。
2. **事件翻译器** —— SSE 帧 → 上面的 AG-UI 事件。
3. **生成式 UI 的桥**（最值钱的一件）：

```
图的 presentations/cart 状态
   → SSE values 帧
   → AG-UI STATE_SNAPSHOT
   → useAgent({updates:[OnStateChanged]})
   → page.tsx 的组件注册表
   → <ProductCarousel> / <PlanChecklist> …
```

没有 AG-UI，这条链要自己写：解析 SSE、diff state、触发 React 更新。

### 换掉它，后端不用改

| 换成 | 要做什么 |
| --- | --- |
| LangGraph Studio | 什么都不用改（它也讲 Agent Protocol） |
| 自己写的 `fetch` + SSE 解析 | 自己解析 `values` / `updates` 帧 |
| curl | 直接看原始帧 |

**AG-UI 是「消费 Agent Protocol 的一种方式」，不是协议本身的一部分。**

> 一句话：**Agent Protocol 决定「后端说什么」，AG-UI 决定「前端怎么听懂并画出来」。**

### Agent Protocol 内部还分两代

| | **v1**（AG-UI 当前在用） | **v2** |
| --- | --- | --- |
| 端点 | `POST /threads/{id}/runs/stream` | `POST /threads/{id}/stream/events`<br>`POST /threads/{id}/commands` |
| 事件形状 | `values` / `updates` / `messages` | 协议事件信封（按 channel 过滤） |
| 项目状态 | ✅ | ✅ 也实现了（`event_streaming.py`），由 flag 门控 |
| AG-UI | 默认 `streamProtocol="legacy"` 走这条 | ❌ 不走 |

所以更准确的说法是：**这个端点是 Agent Protocol v1 的「run-scoped SSE」**，
AG-UI 的默认配置恰好选了它，而不是 v2 的 thread-scoped event stream。

## 协议一致性对照

后端实现的是 Agent Protocol，`@ag-ui/langgraph` 调的是它下面的 LangGraph SDK。
把 SDK 会打的路径和后端注册的路由（`docs/openapi.json`）逐项对一遍。

### 前端实际用到的 10 个 —— 全部一致

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

### 全量协议面

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

## thread_id 和 run_id

**`run_id` = 一次图执行的唯一标识。** 用户在同一个 thread 里每问一句，就产生一个新的
`run_id`。

| | `thread_id` | `run_id` |
| --- | --- | --- |
| 是什么 | **会话 / 状态容器** | **一次执行** |
| 生命周期 | 跨多轮，持久 | 一次 run，跑完就结束 |
| 谁在用 | checkpointer 的 key、消息历史 | 事件、任务、取消、日志 |
| 类比 | 一个聊天会话 | 会话里的一条「提问-回答」 |

```
thread_id = 0890e1cc-…（你问「你好」的那个会话）
   ├─ run_id = aaaa…   ← 第一句
   ├─ run_id = bbbb…   ← 第二句
   └─ run_id = cccc…   ← 第三句
```

### run_id 被谁用

| 层面 | 用途 | 位置 |
| --- | --- | --- |
| 数据库 | `RunORM` 主键，一条执行记录 | `run_preparation.py` |
| API 路径 | `/threads/{tid}/runs/{run_id}` 的查 / 等 / 流 / 取消 | `runs.py` |
| 任务注册表 | `active_runs[run_id] = task` — 取消时靠它找到 asyncio task | `local_executor.py:36` |
| broker | 每个 run 一个 broker；replay buffer 和订阅者都挂在它下面 | `broker_manager` |
| 事件 ID | `{run_id}_event_{seq}` — 每个事件自带 run 归属 | `sse_utils.py:1` |
| 取消 | `request_cancel(run_id)` | `broker.py:162` |
| 观测 | 绑进 structlog contextvars，作为 `x-run-id` 请求头发给 LLM，也写进 Langfuse metadata | `make_run_trace_context`、`_correlation_headers` |
| Redis | 缓存 key / 计数 key / 取消频道都按 run_id 隔离 | `redis_broker.py` |

### 为什么事件 ID 要带 run_id

```python
# infra/sse_utils.py:1
def generate_event_id(run_id: str, sequence: int) -> str:
    """Generate SSE event ID in the format: {run_id}_event_{sequence}"""
```

`Last-Event-ID` 会跨连接传回来，带上 `run_id` 之后：

- 服务端能确认「这个断点属于哪个 run」
- 同一个 thread 下有多个 run 时，事件不会串
- `extract_event_sequence()` 切出序号做去重

### 它也是排障的锚点

| 键 | 粒度 |
| --- | --- |
| `thread_id` | 整个会话 |
| **`run_id`** | **一次执行**（mitmweb 里 `~hq"x-run-id: <uuid>"` 筛这个） |
| `x-ds-trace-id` | 单次 LLM 调用 |

## 前端结构

| 文件 | 作用 |
| --- | --- |
| `apps/web/app/providers.tsx` | 建 `LangGraphAgent`，直连 `AGENT_SERVER` |
| `apps/web/app/page.tsx` | 页面；`CopilotSidebar` 就是对话窗口 |
| `apps/web/components/*.tsx` | 状态卡片（`ProductCarousel`、`PlanChecklist`…） |

```ts
// providers.tsx:11,39
const AGENT_SERVER = process.env.NEXT_PUBLIC_AGENT_SERVER_URL ?? "http://localhost:2026";
new LangGraphAgent({ deploymentUrl: AGENT_SERVER, graphId: id })
```

`AGENTS` 里三个图对应页面上的三个 tab：`shopping_agent` = **Customer**、
`merchant_agent` = Merchant、`research_agent` = Research。侧边栏默认开的是 Customer。

## 前端调了哪些接口

### 两条通道

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

### 通道 ①：AG-UI 封装的 Agent Protocol 调用

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

### 通道 ②：为什么没有 SDK

后端是两个面**合并**进同一个 FastAPI 的（`langgraph.json` 的 `http.app` →
`src/http_app.py:app`，再由 `app/main.py:create_app` 合并核心路由）：

| 面 | 有 SDK 客户端吗 | 前端怎么调 |
| --- | --- | --- |
| Agent Protocol（`/threads`、`/runs`） | ✅ AG-UI + langgraph-sdk | 走封装 |
| 领域 REST（`/shop/products`） | ❌ 没有 | 组件里裸 `fetch` |

`CatalogGrid.tsx` 的注释点明了这点：*"Catalog browsing over the REST surface —
same backend the agent uses."*

### 怎么看真实调用

F12 → Network → 过滤框输 `2026`，再发消息。
`runs/stream` 会显示 `pending` 挂住几秒（流式期间一直不关），结束后变 `200`。

### 实测调用链

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

### 两个待查的点

1. ~~谁发的 `/assistants/{id}/schemas` 和 `/graph`~~ → **已查明：就是 `@ag-ui/langgraph`
   自己发的。** `getSchemaKeys()` 调 `assistants.getSchemas`，`prepareStream()` 调
   `assistants.getGraph`（`dist/index.js` 里的 `this.client.assistants.*`）。
2. **`GET /threads/{id}/state` 为什么发 3 次**（开流前 1 次、开流后 2 次）—— 仍未查清。

> **更正**：`.workbuddy/memory/2026-10-08.md` 说序列第一步是 `assistants/search` ——
> **这条是对的**。`getAssistant()` 里调 `assistants.search({graphId, limit: 1})` 解析
> assistant_id。它没出现在这次按消息录制的 Network 窗口里，是因为它在更早的初始化阶段
> 就发了（或被缓存），不代表没有。

## 服务端：触发和订阅是两件事

**是 `runs/stream` 触发的图执行。** 触发点在 `run_preparation.py:369`：

```python
# run_preparation.py:249  async def _prepare_run(...)
    ...
    session.add(run_orm)
    await session.commit()
    run = Run.model_validate(run_orm)

    # Submit to executor
    await executor.submit(job)          # ← ★ 图从这里开始跑
    logger.info("Submitted run to executor", run_id=run_id)

    return run_id, run, job
```

而它发生在**返回 SSE 响应之前**：

```
runs.py:146   create_and_stream_run()
    :165   run_id, run, _job = await _prepare_run(...)      ← 内部 :369 已把图提交出去了
    :196   return make_sse_response(stream_run_execution(run), ...)   ← 之后才返回 SSE
```

| 顺序 | 做什么 | 对应代码 |
| --- | --- | --- |
| ① **触发** | 建 Run 记录 → `executor.submit(job)` → **图在后台 task 里开始跑** | `run_preparation.py:369` |
| ② **订阅** | 返回 SSE，把自己挂成 broker 的订阅者 | `runs.py:196` |

**图不是被 SSE 连接「驱动」的，是在 SSE 响应返回之前就已经跑起来了。** 连接只是去接它的输出。
接口本身不是执行者，是**启动器 + 订阅器**。

`submit` 之后由 `LocalExecutor` 起独立 task：

```python
# usecase/execution/local_executor.py:28-36
trace_ctx = make_run_trace_context(run_id, thread_id, graph_id, user_id, ...)
task = asyncio.create_task(execute_run(job), context=trace_ctx)
active_runs[run_id] = task
```

`context=trace_ctx` 是 ContextVar 的载体 —— **`run_id` / `thread_id` 就在这一行绑定**，
这个 task 里往下的一切都能看到。（这也是为什么在节点内构造模型能拿到 `x-run-id`，
而在图加载时构造拿不到：后者发生在这一行**之前**。）

### 触发和订阅之间的空档为什么安全

①和②差几十毫秒，图可能已经吐了几个事件。**不会丢** —— 订阅的第一步就是补发
（见下节 `_replay_stored_events`）。

## 服务端：图和连接是解耦的

```python
# controller/http/routers/runs.py:146
@router.post("/threads/{thread_id}/runs/stream", ...)
async def create_and_stream_run(...) -> EventSourceResponse:
    ...
    run_id, run, _job = await _prepare_run(session, thread_id, request, user, ...)
    ...
    return make_sse_response(
        sse_to_bytes(streaming_service.stream_run_execution(run, None)),
        close_handler=close_handler,
        headers={..., "Location": f"/threads/{thread_id}/runs/{run_id}/stream"},
    )
```

注意 **`_prepare_run()` 把活派给 executor 之后就返回了，不等图跑完**。
SSE 端点只是 broker 的一个**订阅者**：

```python
# usecase/streaming/streaming_service.py:84
async def stream_run_execution(self, run, last_event_id=None):
    # ① 先补发已存事件（断线重连用）
    async for event_id, sse_event in self._replay_stored_events(run_id, last_event_id):
        yield sse_event
    # ② 再实时订阅
    async for sse_event in self._stream_live_events(run, last_sent_sequence):
        yield sse_event
```

```python
# ① 里的 broker 调用
broker = broker_manager.get_or_create_broker(run_id)
stored_events = await broker.replay(last_event_id)

# ② 里的
broker = broker_manager.get_broker(run_id)
```

数据流：

```
 executor 独立跑图                    SSE 连接
        │                                 │
        │ 产出事件                        │
        ↓                                 │
    ┌─────────┐                          │
    │  broker │  ←─ 订阅 ────────────────┘
    │(内存/   │
    │ Redis)  │  ←─ replay 补发（重连时）
    └─────────┘
```

### broker 内部：广播 + 历史

不是「一个队列被多方争抢」，是**广播**。内存实现（`broker.py:38`）：

```python
self._replay_buffer: list[tuple[str, Any]] = []                  # 历史（重连用）
self._subscribers: set[asyncio.Queue[tuple[str, Any]]] = set()   # 每个消费者一个队列
```

`put()` 同时做两件事（`broker.py:43`）：

```python
if resumable:
    self._replay_buffer.append((event_id, payload))   # ① 存历史
for queue in list(self._subscribers):
    queue.put_nowait((event_id, payload))             # ② 广播给每个订阅者
```

类 docstring：*"per-subscriber fan-out + replay buffer … each `aiter()` caller gets its own
queue … so multiple concurrent SSE consumers on one run each receive every event."*

**所以多个 SSE 连接可以同时看同一个 run，每人都拿到全量事件。**

**生产者不等消费者**：`put()` 写完历史就返回，`_subscribers` 为空也无所谓。这正是
「客户端断开 run 继续跑」和「事后重连能补」的底层原因。

`aiter()` 里还有个防丢设计（`broker.py:60-69`）—— **先注册、再读历史**：

```python
self._subscribers.add(queue)          # 先注册
backlog = list(self._replay_buffer)   # 再取快照
for ... backlog: yield ...
# 然后才进实时循环
```

这样「补发」和「实时」之间的窗口里产生的事件会落进队列，不丢；重叠部分靠 `event_id` 去重。

Redis 模式下结构变了、语义没变：

| | 内存（`RunBroker`） | Redis（`RedisRunBroker`） |
| --- | --- | --- |
| 广播 | `set[asyncio.Queue]` + `put_nowait` | Redis **pub/sub** |
| 历史 | Python list | Redis **List**（`RPUSH` + `LTRIM`） |

> 严格说 Redis 模式下「队列」这个词就不准了 —— 那时是发布/订阅 + 一个 List 做补发。

**完整调用链**（浏览器不碰队列，它消费的是 HTTP 流）：

```
LangGraph（executor task）
    │ put(event_id, payload)
    ▼
broker ──┬─ replay buffer（list，历史）
         └─ 订阅者队列（fan-out）
                 │
      ┌──────────┴──────────┐
      ▼                     ▼
 SSE 连接 A             SSE 连接 B        ← 端点作为【订阅者】
      │                     │
      ▼                     ▼
  浏览器 A               浏览器 B        ← 消费 HTTP/SSE 字节流
```

### 这个解耦带来三个能力

| 能力 | 靠什么 |
| --- | --- |
| `on_disconnect="continue"`：客户端关了，run 继续跑 | 图不依赖连接 |
| 断线重连不丢事件 | `last_event_id` → `broker.replay()`（详见下文「断线重连」） |
| 多实例（任意 pod 都能服务流） | `RedisRunBroker` 的 pub/sub |

### 两种 broker

| 实现 | 文件 | 何时用 |
| --- | --- | --- |
| `RunBroker` | `usecase/streaming/broker.py:26` | 内存，`REDIS_BROKER_ENABLED=false` |
| `RedisRunBroker` | `usecase/streaming/redis_broker.py:76` | Redis pub/sub，WorkerExecutor 多实例 |

本机 `.env` 是 `REDIS_BROKER_ENABLED=false` → 走内存那套。

## 完整链路

从点击发送到事件回到浏览器，中途经过的**每一个代码位置**：

```
浏览器                        agent server                            executor task
   │                              │                                        │
   ├─ POST /threads ─────────────→│                                        │
   │←──── thread_id ──────────────┤                                        │
   │                              │                                        │
   ├─ GET /threads/{id}/state ───→│  (捞历史消息)                           │
   │←──── messages ───────────────┤                                        │
   │                              │                                        │
   ├─ POST .../runs/stream ──────→│                                        │
   │                              ├─ _prepare_run       run_preparation.py:249
   │                              │   └─ executor.submit(job)      :369 ───→│ 起 task
   │                              │                                        │ local_executor.py:35
   │                              │                                        ├─ execute_run(job)
   │                              │                                        │  run_executor.py:58
   │                              │                                        ├─ _stream_graph(job)
   │                              │                                        │  run_executor.py:172
   │                              │                                        ├─ stream_graph_events(...)
   │                              │                                        │  run_executor.py:209
   │                              │                                        └─ graph.astream(...)   ★ 真正跑图
   │                              │                                           graph_streaming.py:297
   │                              │                               事件 ←────┘
   │                              │←──── 写进 broker ──────────────────────┤
   │←──── event: messages ────────┤  stream_run_execution 订阅 broker
   │←──── event: updates ─────────┤  streaming_service.py:84
   │←──── event: messages ────────┤
   │←──── event: end ─────────────┤  (连接关闭)
```

两条分支：

| 情况 | 走哪条 |
| --- | --- |
| `stream_mode` 含 `events` | `graph_streaming.py:209` 的 `graph.astream_events(...)` |
| 其余（默认） | `graph_streaming.py:297` 的 `graph.astream(...)` |
| Agent Protocol **v2** 协议 | `usecase/streaming/v2/native_stream.py:77` |

## 响应头分层

`POST /threads/{id}/runs/stream` 的响应头，按「为什么存在」可以分三层 —— 只有最外一层
是为浏览器 SDK 服务的：

| 头 | 性质 | 跟 SDK 有关吗 |
| --- | --- | --- |
| `Content-Type: text/event-stream` | W3C SSE 标准 | ❌ |
| `Cache-Control: no-cache` | 标准 | ❌ |
| `Connection: keep-alive` | 标准 | ❌ |
| `X-Accel-Buffering: no` | nginx 部署相关（关掉代理缓冲） | ❌ |
| `Location` / `Content-Location` | 标准 HTTP 头，用法是 Agent Protocol 约定 | ⚠️ |
| `Access-Control-Allow-Origin: *` | CORS | ⚠️ 浏览器才需要 |
| `Access-Control-Allow-Headers: Last-Event-ID` | CORS | ✅ 为让 JS 能带重连头 |
| `expose_headers = [Content-Location, Location]` | CORS 暴露 | ✅ 为让 JS 能读这两个头 |

**SSE 主体是纯标准** —— 任何 SSE 客户端（`EventSource`、curl、Python）都能消费，不是为
某个 SDK 定制的。

**真正「为前端 SDK 而设」的只有 CORS 那一层**：

```python
# main.py
# Default CORS headers required for LangGraph SDK stream reconnection
DEFAULT_EXPOSE_HEADERS = ["Content-Location", "Location"]
```

```python
# infra/sse.py:72
"Access-Control-Allow-Headers": "Last-Event-ID",
```

浏览器跨域时，默认**读不到**非 `expose_headers` 的响应头、**发不出**非 `allow_headers`
的请求头。换成 curl 或服务端客户端，这两条根本不需要。

### 服务端是「按协议给」，不是「按这个前端给」

`Content-Location` 这条通道，`@ag-ui/langgraph` **压根没用**（它等流事件，见「取消」一节）。
这些头服务于 Agent Protocol 的其它客户端 —— LangGraph Studio、Agent Chat UI、其它 SDK 用法。

### 缺口：这些头没写进 OpenAPI

```json
"responses": { "200": { ... } }      // docs/openapi.json 里没有 "headers" 字段
```

依赖 `Content-Location` 拿 run_id 的客户端，依赖的是**未文档化的行为**。对这个仓库有点
矛盾 —— 它有一套很严的 `make openapi-check` 漂移门禁，但这两个头在契约里是隐形的。

> 一句话：**协议优先（Agent Protocol 兼容），SDK 便利性其次（CORS 暴露）。**

## 断线重连

**事件带序号 ID → broker 存 replay buffer → 重连带 `Last-Event-ID` → 先补历史、再实时、按序号去重。**

### ① 事件 ID 带序号

```python
# infra/sse_utils.py:1
def generate_event_id(run_id: str, sequence: int) -> str:
    """Generate SSE event ID in the format: {run_id}_event_{sequence}"""
```

序号由 broker **原子分配**（`base_broker.py:107` 的 `allocate_event_id`），Redis 模式下用
INCR，跨实例唯一。

### ② broker 存一份 replay buffer

每个事件写进 broker 时同时入缓存：

| | 内存（`broker.py`） | Redis（`redis_broker.py`） |
| --- | --- | --- |
| 结构 | `_replay_buffer: list` | Redis List（`RPUSH`） |
| 上限 | 无条数上限 | `LTRIM` 保留最近 **10,000** 条 |
| 保留期 | `REPLAY_RETENTION_SECONDS = 600` 后清扫 | TTL **600 秒**，每次写入刷新 |

### ③ 重连走专门的 GET 接口

```python
# runs.py:418
@router.get("/threads/{thread_id}/runs/{run_id}/stream", ...)
async def stream_run(
    thread_id: str,
    run_id: str,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),   # ← 关键
    ...
) -> EventSourceResponse:
```

### ④ 补发历史

```python
# streaming_service.py:127
async def _replay_stored_events(self, run_id, last_event_id):
    broker = broker_manager.get_or_create_broker(run_id)
    stored_events = await broker.replay(last_event_id)
    ...
```

`broker.replay()` 的三种情况（`broker.py:83`）：

| `last_event_id` | 返回 |
| --- | --- |
| `None`（首次连接） | **全部** buffer |
| 在 buffer 里找到 | 它**之后**的事件 |
| **找不到**（已被淘汰） | **全部** buffer ← 降级 |

### ⑤ 再切到实时，按序号去重

```python
# streaming_service.py:84
async for event_id, sse_event in self._replay_stored_events(run_id, last_event_id):
    last_sent_sequence = max(last_sent_sequence, extract_event_sequence(event_id))
    yield sse_event

async for sse_event in self._stream_live_events(run, last_sent_sequence):
    yield sse_event
```

```python
# streaming_service.py:141
async for event_id, raw_event in broker.aiter():
    current_sequence = extract_event_sequence(event_id)
    if current_sequence <= last_sent_sequence:     # ← 已补发过的跳过
        continue
    ...
```

**补发和实时之间没有断档**：补发结束时的最大序号就是实时的起点。

### ⑥ 已结束的 run 也能补

```python
# runs.py:455
# If already terminal and no Last-Event-ID, just emit end.
# If Last-Event-ID is present, fall through to stream_run_execution
if run_status in TERMINAL_STATES and not last_event_id:
    ...
```

带 `Last-Event-ID` 时会掉进正常流程，从 buffer 把错过的事件全补回来。

### 客户端

SDK 不用浏览器的 `EventSource`（只能 GET），自己实现了重连：

| 参数 | 默认值 |
| --- | --- |
| `maxReconnectAttempts` | **5** |
| `idleReconnect` | `"auto"` |
| 退避 | `min(1000 × 2^(attempt-1), 5000) + random(0, 1000)` ms |

重连时把上次收到的 ID 塞进请求头（`dist/client/base.cjs`）：

```js
const requestHeaders = isReconnect && reconnectParams?.lastEventId
  ? { "Last-Event-ID": reconnectParams.lastEventId, ... }
  : ...
```

### 边界 / 坑

| 情况 | 后果 |
| --- | --- |
| 断开超过 **10 分钟** | buffer 已清扫，补不回来；`get_or_create_broker` 建空 broker，补发 `[]`，**静默少一段** |
| `last_event_id` 找不到 | `replay()` 退化成返回**全部** buffer → 客户端可能收到**重复事件** |
| 内存模式 + 多实例 | 重连落到另一进程 → 没有该 run 的 broker → 补发为空。**可靠重连必须开 Redis** |
| 内存 buffer 无条数上限 | 只靠 10 分钟清扫，超长 run 会在内存里堆很多事件 |
| keepalive | `KEEPALIVE_INTERVAL_SECS` 发的注释行**不带 event id**，不参与重连 |

## 取消

前端在对话框里点「停止」，最终落到 **`run_id`** 上 —— 它是 `active_runs` 这个字典的 key。

### 显式路径

`@ag-ui/langgraph` 的 `abortRun()`：

```js
abortRun() {
  this.cancelRequested = true;
  let threadId = this.activeRun?.threadId;
  let runId    = this.activeRun?.id;
  threadId && runId && !this.cancelSent
    && this.client.runs.cancel(threadId, runId).then(() => { this.cancelSent = true }).catch(() => {});
  super.abortRun();          // ← 同时中断本地流
}
```

→ `POST /threads/{thread_id}/runs/{run_id}/cancel`（`runs.py:487`），带两个可选 query：
`wait=1`（最多等 10 秒让 task 落定）、`action=cancel|interrupt`。

### run_id 什么时候到客户端手里

**服务端其实在响应头里就给了**（`runs.py:196`）：

```python
headers={
    **get_sse_headers(),
    "Location": f"/threads/{thread_id}/runs/{run_id}/stream",
    "Content-Location": f"/threads/{thread_id}/runs/{run_id}",
},
```

而且 `main.py` 专门把这两个头 CORS 暴露出去：

```python
# Default CORS headers required for LangGraph SDK stream reconnection
DEFAULT_EXPOSE_HEADERS = ["Content-Location", "Location"]
```

**SDK 也实现了从响应头解析**（`dist/client/base.cjs:212`）：

```js
const REGEX_RUN_METADATA = /(\/threads\/(?<thread_id>.+))?\/runs\/(?<run_id>.+)/;

function getRunMetadataFromResponse(response) {
  const contentLocation = response.headers.get("Content-Location");
  if (!contentLocation) return void 0;
  const match = REGEX_RUN_METADATA.exec(contentLocation);
  ...
  return { run_id: match.groups.run_id, thread_id: match.groups.thread_id || void 0 };
}
```

它在**读第一个事件之前**（`onInitialResponse`）就能拿到。

**但 `@ag-ui/langgraph` 没用这条** —— 它等到流里的 metadata 事件才设 `activeRun.id`：

```js
_.run_id && (this.activeRun.id = _.run_id, this.activeRun.serverRunIdKnown = true, ...)
```

所以对**这个前端**来说 run_id 确实是随流到的；服务端提供的更早通道没被用上。

这也是取消有**竞态**的原因 —— 用户在 run_id 到达之前点取消，代码会先记下
`cancelRequested`，等 id 一到立刻发：

```js
for await (let event of stream) {
  if (this.cancelRequested && !this.cancelSent
      && this.activeRun?.threadId && this.activeRun?.id) {
    await this.client.runs.cancel(this.activeRun.threadId, this.activeRun.id);
    ...
    break;
  }
  ...
}
```

### 隐式路径：断开连接就取消

`super.abortRun()` 会断开 SSE 连接，而服务端默认：

```python
# runs.py
cancel_on_disconnect = (request.on_disconnect or "cancel").lower() == "cancel"
close_handler = _cancel_on_client_close if cancel_on_disconnect else None
```

`_cancel_on_client_close` → `broker_manager.request_cancel(run_id, "cancel")`。

| 路径 | 靠什么 | 何时 |
| --- | --- | --- |
| **显式** | `POST …/runs/{run_id}/cancel` | 前端主动调（知道 run_id 之后） |
| **隐式** | 断开 SSE 连接 | 默认 `on_disconnect="cancel"` 兜底 |

### 服务端最终怎么停

两条路都汇到同一个 `request_cancel(run_id)`（`broker.py:162`）：

```python
task = active_runs.get(run_id)
if task is None or task.done():
    return
task.cancel()                                   # ← 真正打断 asyncio task
await broker.put(event_id, ("end", {"status": "interrupted"}))
```

`asyncio` 是**协作式取消**，只在 `await` 点生效 —— 所以：

- 异步的 LLM 调用能中断（httpx 请求被 abort）
- **同步工具跑在线程池里，task 取消后线程还在跑**
- 纯 CPU 的同步循环不会停

想「硬停」只能靠进程退出。

> 另：`LocalExecutor`（本地开发模式）**没有 run 级超时看门狗** —— `_timeout_cancellations`
> 只在 `worker_executor.py` 里。所以本地模式下卡死的 run 只能靠手动取消 / 断开连接 / 进程退出。
> `runs/wait` 的 300 秒超时也不行 —— 它用的是 `asyncio.shield(task)`，只是「不再等」。

## SSE 是可选的吗

**SSE 传输能绕开，但内部那套事件机制绕不开。**

### 能绕开的：非流式端点

| 端点 | 传输 | SSE? |
| --- | --- | --- |
| `POST /threads/{id}/runs/stream` | `text/event-stream` | ✅ |
| `GET  /threads/{id}/runs/{run_id}/stream` | `text/event-stream` | ✅（重连续传） |
| `POST /threads/{id}/runs/wait` | chunked JSON + `\n` 心跳 | ❌ |
| `POST /threads/{id}/runs` | 立即返回 `Run` JSON（后台跑） | ❌ |
| `GET  /threads/{id}/runs/{run_id}/join` | 等完成 | ❌ |
| `POST /runs/wait`（stateless） | 同 `runs/wait` | ❌ |

`runs/wait` 就是绕开的写法：

```python
# runs.py:402
return StreamingResponse(
    heartbeat_wait_body(run_id, thread_id, user.identity, timeout=...),
    media_type="application/json",        # ← 不是 text/event-stream
)
```

同一次 `_prepare_run()` 触发，只是换了个方式把结果送回去 —— 客户端不需要 SSE 解析器。

### 绕不开的：图的执行本身就是「流」

```python
# run_executor.py:41
async def execute_run(job: RunJob) -> None:
    """Execute a graph run, stream events to the broker, and update DB."""
    ...
    final_output = await _stream_graph(job)      # ← 无条件走这条
```

**不管哪个端点触发的，`execute_run` 都走 `_stream_graph`**，而里面是
`graph.astream(...)`（`graph_streaming.py:297`）。

**`astream` 不是这个项目加的封装 —— 它就是 langgraph / Pregel 的原生执行方式。**
Pregel 逐超步推进，`astream` 是迭代这些超步的接口；`ainvoke` 不过是把它收集成
「等最终结果」而已。

```
Pregel 逐超步产出事件              ← 图的本质，跟 HTTP 无关
        ↓
execute_run 必须消费它             ← 判中断 / 写 broker / 更新状态，绕不开
        ↓
怎么送到浏览器，三选一：
   ├─ text/event-stream  (SSE)     ← 可选
   ├─ chunked JSON       (wait)    ← 可选
   └─ 只落库，不转发      (后台 run) ← 可选
```

**一句话**：SSE 可以绕开，Pregel 的流绕不开。就算完全不用 HTTP —— 在脚本里直接
`graph.astream(...)` —— 也一样拿到一串事件。选 SSE 是因为要支持断线重连
（`Last-Event-ID` 续传）和 HITL 中断。

broker 存在的理由同理：executor 和 HTTP 连接是两个独立 task（甚至两个进程）。
不 decouple 的话 broker 确实能省掉 —— 但那样就没有 `on_disconnect="continue"`、
没有重连补发、没有多实例了。

## 客户端：自己解析 SSE，不用 EventSource

浏览器的 `EventSource` 只能发 GET，而这里是 POST，所以 SDK **手工解析 SSE 线格式**：
`fetch` 拿响应 → `response.body.getReader()` 逐块读字节 → 累积 buffer → 按**空行**切分事件。

SSE 在网络上就是纯文本：

```
id: 5
event: messages
data: {"type":"messages","data":[{"content":"你好"}]}

id: 6
event: updates
data: {"type":"updates","data":{...}}

```

**两条换行 = 一个事件结束。** SDK 把 `data:` 行 `JSON.parse` 出来再派发出去。

常见的 `event:` 类型：`messages`（token 流）、`updates`（节点状态增量）、
`values`（完整 state 快照）、`metadata`、`interrupt`（HITL 中断）、`end`。

## 坑

### 1. 别把 `GET /state` 当成「拿回答」

SDK 里的顺序是**先 getState、再 stream**：

```js
let m = await this.getOrCreateThread(u, ...)              // 1. 拿/建 thread
let h = await this.client.threads.getState(m.thread_id)   // 2. ← GET /state
let g = h.values.messages ?? []                           //    捞【已有】消息
let y = this.langGraphDefaultMergeState({...i, messages: g}, _, t)
...
this.client.runs.stream(u, this.assistant.assistant_id, W) // 3. ← 才发 run
```

`getState` 的另两个调用点也跟「回答」无关：一个查 `interrupts`（HITL 恢复），
一个发 `STATE_SNAPSHOT`（刷新页面恢复现场）。

### 2. 长连接的 keepalive

`KEEPALIVE_INTERVAL_SECS` 会周期性发 SSE 注释行，防止空闲代理把
长时间没输出的节点（比如握着上游 WebSocket 的 agent）的连接掐掉。

### 3. 流中途出错不会返回 500

```python
# streaming_service.py:117-125
except Exception as e:
    logger.exception("stream execution failed", run_id=run_id)
    yield create_error_event({"error": type(e).__name__, "message": "execution failed"})
```

连接已经建立了，再抛 500 客户端没法重连。所以错误**以事件形式**发出去，
且 `str(e)` 被刻意丢弃（异常消息可能泄露文件路径和内部 ID）。

### 4. 发消息后页面闪一下

结构上的可疑点（`page.tsx`）：

- **`Home` 顶层挂了 `useAgent`**（`page.tsx:138`）—— 任何 state 变化都重渲染整个页面
  （header / hero / 正文 / 侧边栏全在内），流式期间每秒变好几次；
- **`Presentations` 用数组下标当 key**（`page.tsx:65` 的 `key={i}`）——
  列表一变 React 卸载重建而不是更新，视觉上就是闪；
- **`getState` 回来后 `mergeState` 替换了消息数组** —— 消息列表整个重挂一次。
  如果是这条，闪的时机应该**固定在回答出现之前**。

**怎么确认**：React DevTools 勾上 "Highlight updates when components render"，
再发一句看是哪块在闪；或看 Network 里 `GET /state` 返回的瞬间是否正好是闪烁点。

### 5. `web_search` 是模拟的，不走网络

`src/graphs/shared/tools/web_search.py` 目前是占位实现，返回
`"Simulated result N for '<query>'"`，**不发任何 HTTP 请求**。所以：

- mitmweb 里看不到它（没流量）
- research_agent 的「研究」是在占位文本上做的推理

换真实实现时（Tavily/Exa/SerpAPI），**要确认所选 SDK 的 HTTP 层认 `HTTPS_PROXY`**，
否则流量绕开 mitmweb 而且**不报错**：`httpx`/`requests`/`urllib` 默认认，
`aiohttp` 需要显式 `trust_env=True`。

### 6. mitmweb 里只有 LLM 调用

| 流量 | 走向 | mitmweb |
| --- | --- | --- |
| LLM 调用（`api.deepseek.com`） | httpx + `HTTPS_PROXY` | ✅ |
| 浏览器 → agent server | 浏览器直连 `:2026` | ❌ 不走代理 |
| `web_search` | 本地函数 | ❌ |
| MCP 工具（`acme-kb_*`） | stdio 子进程 | ❌ 不是 HTTP |
| Langfuse OTLP 上报 | `NO_PROXY=localhost,127.0.0.1` 排除 | ❌ |
