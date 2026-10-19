# 异常处理

从 HTTP 边界到后台 run，再到 SSE 流——同一个异常在不同位置有四种不同的处理方式。
重点是最后一节：**LangGraph 内部抛异常时到底发生了什么**。

## TL;DR

```
请求
 ├─ 中间件：记日志 → re-raise
 ├─ 路由：raise HTTPException(404/409/…)
 ├─ usecase：也 raise HTTPException（偏离分层，37 处）
 └─ 全局 handler
      ├─ HTTPException → AgentProtocolError（保留 headers）
      └─ Exception     → 500，details=None，不泄露

后台 run（不在请求里）
 ├─ CancelledError → 按来源分 4 种处理
 └─ Exception      → finalize(error) + 安全的错误事件

SSE 流（连接已建立）
 └─ Exception      → yield error 事件，不抛 500
```

## 一、出口：统一的错误体

`domain/errors.py` 定义了一切的基础：

```python
class AgentProtocolError(BaseModel):
    error: str
    message: str
    details: dict[str, Any] | None


def get_error_type(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "rate_limited",
        500: "internal_error",
        501: "not_implemented",
        503: "service_unavailable",
    }.get(status_code, "unknown_error")
```

外加给路由装饰器用的 OpenAPI 常量：`NOT_FOUND` / `CONFLICT` / `BAD_REQUEST` /
`UNAVAILABLE` / `SSE_RESPONSE`。

## 二、全局兜底：两个 handler

`app/main.py:203-231`：

| 捕获 | 行为 |
| --- | --- |
| `HTTPException` | 重建成 `{error, message, details}`，**转发 `exc.headers`** |
| `Exception` | 500，且**不泄露内部信息**：`details=None`，message 固定 `"An unexpected error occurred"` |

```python
# 行内注释：FIX: do not leak internal exception details
details = (None,)
```

## 三、中间件：只记日志，**重新抛出**

```python
# controller/http/middleware/logger_middleware.py:46
except Exception as e:
    # Log the exception here, but re-raise so application-level
    # exception handlers (e.g. Agent Protocol formatter) can run.
    app_logger.exception("...", exc_info=e, stack_info=True)
    raise
```

**不吞、不改写** —— 把格式化留给 app 层。

## 四、分层现状

| 层 | `raise HTTPException` | 评价 |
| --- | --- | --- |
| `controller/http/` | 46 | ✅ 约定内的位置 |
| **`usecase/`** | **37** | ⚠️ **偏离约定** |
| `repo/` | **0** | ✅ 干净 |
| `auth/` | 19 | 边界层，可接受 |

偏离分布：

```
usecase/assistant_service.py               19
usecase/cron/service.py                    12
usecase/execution/run_preparation.py        6
```

AGENTS.md 写的是「HTTP 状态 → `controller/http/`」，但这三个 usecase 文件直接抛了
`HTTPException`。不是 bug，是一处架构不一致。

## 五、LangGraph 内部异常

### 传播路径

`graph_streaming.py` 里的 `graph.astream(...)` **没有自己的 try/except** ——节点里抛出的
异常会一路冒泡：

```
节点函数抛异常
   ↓
LangGraph Pregel 中止当前超步，astream() 抛出
   ↓
_stream_legacy / _stream_native_v2        不捕获，直接冒
   ↓
_stream_graph                             不捕获
   ↓
execute_run 的 except Exception:          ← 唯一真正处理的地方
```

### 关键代码

```python
# usecase/execution/run_executor.py:117
except Exception as exc:
    logger.exception("Run failed", run_id=run_id)
    safe_message = f"{type(exc).__name__}: execution failed"
    finalized = await finalize_run(
        run_id, thread_id, user_id=user_id,
        status="error",
        thread_status="error",
        output={},
        error=str(exc),                    # ① DB 存【完整】异常
    )
    if finalized:
        await _best_effort_signal(
            streaming_service.signal_run_error,
            run_id,
            safe_message,                  # ② 客户端只拿【安全】版本
            type(exc).__name__,
        )
```

**① 和 ② 的不对称是刻意的**：

| 去处 | 内容 |
| --- | --- |
| 数据库 `error_message` | `str(exc)` —— 完整堆栈信息相关文本，给运维排查 |
| SSE 事件 `message` | `"ValueError: execution failed"` —— 只有类型名 |
| SSE 事件 `error` | 异常类名 |

`signal_run_error` 做的是（`streaming_service.py:66`）：

```python
await broker.put(error_event_id, ("error", {"error": error_type, "message": error_message}))
await broker.put(end_event_id, ("end", {"status": "error"}))
broker_manager.cleanup_broker(run_id)
```

所以浏览器最后收到的是：

```
event: error
data: {"error": "ValueError", "message": "ValueError: execution failed"}

event: end
data: {"status": "error"}
```

### 两个版本都一样

v1（`astream`）和 v2（`astream_events(version="v3")`）**都没有在流层捕获异常** ——
两者都靠 `execute_run` 那一个 `except Exception` 兜底。

`graph_streaming.py` 里唯一一个 `except Exception` 是给 context schema 过滤用的，
只 `adebug` 一行，跟主流程无关：

```python
# graph_streaming.py:191
except Exception as e:
    await logger.adebug(f"Failed to get context schema for filtering: {e}", exc_info=e)
```

### 没有自动重试

全仓库搜不到 `retry_policy` / `RetryPolicy` —— **节点抛异常不会自动重试**。

唯一的重试在 **LLM 客户端层**：

```python
# shared/models.py::_load_resilient
init_chat_model(..., max_retries=max_retries, timeout=request_timeout)
```

也就是：模型 HTTP 调用会退避重试；但**节点逻辑里抛的异常一次就终止整个 run**。

> 想加节点级重试，得在 `graph.add_node(..., retry_policy=...)` 或编译时配置。

### 部分状态去哪了

- `finalize_run(..., output={})` —— run 的 `output` 字段**清空**
- 但 **checkpointer 不受影响**：按 Pregel 的超步语义，失败的超步不落检查点，
  thread 会停在上一个成功的检查点（*这条是按模型推断，未实测*）

所以 thread 可以重新发一条消息继续，历史不会丢。

### 中断不是异常

图正常结束但带 `__interrupt__` 时走的是另一条路：

```python
# _stream_legacy
if isinstance(event_data, dict) and "__interrupt__" in event_data:
    result.has_interrupt = True
```

然后 `finalize_run(status="interrupted", thread_status="interrupted")`，
而不是 `error`。**HITL 暂停和真正的失败在状态上是分开的。**

## 六、其他特殊路径

### 清理：只吞「预期内的基础设施故障」

```python
# usecase/execution/run_cleanup.py:30
# Transient infra failures we tolerate during cleanup. Programmer errors
# (TypeError, AttributeError, ...) propagate.
_CLEANUP_ERRORS = (RedisError, SQLAlchemyError, OSError, PsycopgError)
```

全项目最讲究的一处 —— 列出明确类型，程序错误照常冒出来。

### 取消：按「来源」区分语义

```python
except asyncio.CancelledError:
    if run_id in _lease_loss_cancellations:   → 不 finalize（别处接管）
    elif run_id in _shutdown_cancellations:   → 不 finalize（回队列重跑）
    elif run_id in _timeout_cancellations:    → finalize(error, 超时)
    else:                                     → finalize(interrupted, 用户取消)
```

同一个 `CancelledError`，四种语义。

### 信令失败不影响已提交的状态

```python
async def _best_effort_signal(fn, *args):
    try:
        await fn(*args)
    except Exception:
        logger.warning("Signal failed (best-effort, DB status already committed)", ...)
```

DB 状态已经写完了，发事件失败只记日志 —— 不能因为通知失败把已定的事实推翻。

### 流式：错误变成事件，不抛 500

```python
# streaming_service.py:117
except Exception as e:
    logger.exception("stream execution failed", run_id=run_id)
    yield create_error_event({"error": type(e).__name__, "message": "execution failed"})
```

连接已建立，抛 500 客户端没法重连；`str(e)` 被刻意丢弃。

## 七、自定义异常：12 个

```
auth/opa_policy.py            OpaUnavailable
infra/circuit_breaker.py      CircuitOpenError
infra/serializers/base.py     SerializationError
infra/skill_fetcher.py        SkillFetchError
infra/url_guard.py            UrlGuardError(ValueError)
graphs/shared/memory.py       MemoryWriteRejected(ValueError)
graphs/shared/presentation.py PresentationRefused(ValueError)
hub/models.py                 SkillValidationError(ValueError)
hub/models.py                 McpConnectionValidationError(ValueError)
ml/errors.py                  ModelLoadError / ModelNotLoadedError / PredictionError
```

一半继承 `ValueError` —— 调用方可以按「输入问题」统一处理。

## 八、静默吞：只有 3 处

全仓库 **95 处** `except Exception`，但 except 后只 `pass`/`continue` 的只有 3 处：

```
infra/crypto.py:80              InvalidToken → 返回 None（解不开就是没有）
repo/graphs/mcp_loader.py:272   JSONDecodeError/ValueError → 回退
usecase/execution/worker_executor.py:131  RedisError → 继续
```

都有明确理由，不是掩盖问题。
