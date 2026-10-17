# thread_id 和 run_id

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

## run_id 被谁用

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

## 为什么事件 ID 要带 run_id

```python
# infra/sse_utils.py:1
def generate_event_id(run_id: str, sequence: int) -> str:
    """Generate SSE event ID in the format: {run_id}_event_{sequence}"""
```

`Last-Event-ID` 会跨连接传回来，带上 `run_id` 之后：

- 服务端能确认「这个断点属于哪个 run」
- 同一个 thread 下有多个 run 时，事件不会串
- `extract_event_sequence()` 切出序号做去重

## 它也是排障的锚点

| 键 | 粒度 |
| --- | --- |
| `thread_id` | 整个会话 |
| **`run_id`** | **一次执行**（mitmweb 里 `~hq"x-run-id: <uuid>"` 筛这个） |
| `x-ds-trace-id` | 单次 LLM 调用 |

---

[← 返回大纲](README.md)
