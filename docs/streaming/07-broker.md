# 服务端：图和连接是解耦的

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

## broker 内部：广播 + 历史

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

## 这个解耦带来三个能力

| 能力 | 靠什么 |
| --- | --- |
| `on_disconnect="continue"`：客户端关了，run 继续跑 | 图不依赖连接 |
| 断线重连不丢事件 | `last_event_id` → `broker.replay()`（详见下文「断线重连」） |
| 多实例（任意 pod 都能服务流） | `RedisRunBroker` 的 pub/sub |

## 两种 broker

| 实现 | 文件 | 何时用 |
| --- | --- | --- |
| `RunBroker` | `usecase/streaming/broker.py:26` | 内存，`REDIS_BROKER_ENABLED=false` |
| `RedisRunBroker` | `usecase/streaming/redis_broker.py:76` | Redis pub/sub，WorkerExecutor 多实例 |

本机 `.env` 是 `REDIS_BROKER_ENABLED=false` → 走内存那套。

---

[← 返回大纲](README.md)
