# SSE 是可选的吗

**SSE 传输能绕开，但内部那套事件机制绕不开。**

## 能绕开的：非流式端点

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

## 绕不开的：图的执行本身就是「流」

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

---

[← 返回大纲](README.md)
