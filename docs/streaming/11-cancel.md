# 取消

前端在对话框里点「停止」，最终落到 **`run_id`** 上 —— 它是 `active_runs` 这个字典的 key。

## 显式路径

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

## run_id 什么时候到客户端手里

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

## 隐式路径：断开连接就取消

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

## 服务端最终怎么停

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

---

[← 返回大纲](README.md)
