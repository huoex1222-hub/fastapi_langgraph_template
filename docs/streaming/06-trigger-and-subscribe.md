# 服务端：触发和订阅是两件事

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

## 触发和订阅之间的空档为什么安全

①和②差几十毫秒，图可能已经吐了几个事件。**不会丢** —— 订阅的第一步就是补发
（见下节 `_replay_stored_events`）。

---

[← 返回大纲](README.md)
