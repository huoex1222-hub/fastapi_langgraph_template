# 断线重连

**事件带序号 ID → broker 存 replay buffer → 重连带 `Last-Event-ID` → 先补历史、再实时、按序号去重。**

## ① 事件 ID 带序号

```python
# infra/sse_utils.py:1
def generate_event_id(run_id: str, sequence: int) -> str:
    """Generate SSE event ID in the format: {run_id}_event_{sequence}"""
```

序号由 broker **原子分配**（`base_broker.py:107` 的 `allocate_event_id`），Redis 模式下用
INCR，跨实例唯一。

## ② broker 存一份 replay buffer

每个事件写进 broker 时同时入缓存：

| | 内存（`broker.py`） | Redis（`redis_broker.py`） |
| --- | --- | --- |
| 结构 | `_replay_buffer: list` | Redis List（`RPUSH`） |
| 上限 | 无条数上限 | `LTRIM` 保留最近 **10,000** 条 |
| 保留期 | `REPLAY_RETENTION_SECONDS = 600` 后清扫 | TTL **600 秒**，每次写入刷新 |

## ③ 重连走专门的 GET 接口

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

## ④ 补发历史

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

## ⑤ 再切到实时，按序号去重

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

## ⑥ 已结束的 run 也能补

```python
# runs.py:455
# If already terminal and no Last-Event-ID, just emit end.
# If Last-Event-ID is present, fall through to stream_run_execution
if run_status in TERMINAL_STATES and not last_event_id:
    ...
```

带 `Last-Event-ID` 时会掉进正常流程，从 buffer 把错过的事件全补回来。

## 客户端

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

## 边界 / 坑

| 情况 | 后果 |
| --- | --- |
| 断开超过 **10 分钟** | buffer 已清扫，补不回来；`get_or_create_broker` 建空 broker，补发 `[]`，**静默少一段** |
| `last_event_id` 找不到 | `replay()` 退化成返回**全部** buffer → 客户端可能收到**重复事件** |
| 内存模式 + 多实例 | 重连落到另一进程 → 没有该 run 的 broker → 补发为空。**可靠重连必须开 Redis** |
| 内存 buffer 无条数上限 | 只靠 10 分钟清扫，超长 run 会在内存里堆很多事件 |
| keepalive | `KEEPALIVE_INTERVAL_SECS` 发的注释行**不带 event id**，不参与重连 |

---

[← 返回大纲](README.md)
