# 上下文管理：四层上下文、生命周期与边界

这个项目里"上下文"不是一个东西，而是四套独立机制，分别解决不同问题：
**单线程内的对话历史**、**模型窗口预算**、**跨会话长期记忆**、**单次 run 的配置**，
外加一层进程内的 **contextvars** 用于认证/日志/追踪传递。

本文按层讲清楚：存在哪、活多久、谁负责清理、哪里还有缺口。

## TL;DR

| 层 | 存什么 | 生命周期 | 关键代码 |
| --- | --- | --- | --- |
| **会话上下文** | `state["messages"]` + 全部图状态 | 单个 thread，跨 run 持久化 | `graphs/*/state.py`、`repo/database.py:78-81` |
| **窗口管理** | 摘要 + 被淘汰历史的落盘 | 每次模型调用前 | deepagents `SummarizationMiddleware`（仅 paradigm B） |
| **长期记忆** | 用户偏好事实 | 跨 thread，按 retention（默认 90 天） | `graphs/shared/memory.py` |
| **运行上下文** | model、限额、开关 | 单次 run，run 内不可变 | `graphs/*/state.py` 的 `Context`、`usecase/execution/` |
| **请求上下文** | user / run_id / trace attrs | 单次请求或单次 run | `auth/ctx.py`、`infra/observability/span_enrichment.py` |

一句话记忆：**State 会变、Context 不变、Store 跨会话、contextvars 不进 checkpoint。**

## 0. 先分清 State 和 Context

`shopping_agent/state.py:3-5` 写得很直白：

```python
State  = data that evolves during a run (messages, routing intent, …).
Context = per-run configuration that never changes mid-run (model, limits).
```

- **State** 进 checkpoint，会随对话增长，是"对话上下文"。
- **Context** 是本次 run 的参数，不持久化、不进入历史，是"运行配置"。
- 混淆这两者是最常见的错误：把 `model` 放进 State 会被写进每个 checkpoint；
  把 `messages` 放进 Context 则无法跨 run 延续。

## 1. 会话上下文：State + Checkpointer

所有 graph 的对话历史就是一个 state 字段：

```python
# graphs/shopping_agent/state.py:23（merchant_agent/state.py:15 同形）
messages: Annotated[Sequence[AnyMessage], add_messages] = field(default_factory=list)
```

`add_messages` reducer 负责按 message id 追加/替换。持久化链路：

1. `repo/database.py:78-81` 创建 `AsyncPostgresSaver`（与 Store 共用 psycopg 连接池）。
2. `langgraph_service.get_graph()`（`repo/graphs/langgraph_service.py:294-407`）在**每个请求**里
   给 graph 副本注入 checkpointer/store——静态图走 `base_graph.copy(update=...)`，
   工厂图在工厂产出后注入。
3. 恢复的键是 `configurable.thread_id`，由服务端强制写入，客户端覆盖无效
   （`langgraph_service.py:699-701`；`create_run_config` 的 docstring 说明了原因：
   checkpointer 只认 thread_id，否则可以重定向到别人的线程）。
4. HITL 的 interrupt/resume 也完全依赖这份 checkpoint：恢复时状态原样回放。

**Paradigm A 没有窗口裁剪。** `shopping_agent`、`merchant_agent` 的每个模型节点都是把
完整历史直接喂进去：

```python
# graphs/shopping_agent/nodes.py:89（merchant_agent/nodes.py:23、order_agent/nodes.py:44 同形）
response = await model.ainvoke([SystemMessage(system), *state.messages])
```

`nodes.py:42-61` 的 `_classify_history` 看起来像裁剪，其实只是给分类器**去掉
tool_calls 和 ToolMessage**，避免模型模仿 tool call 导致 structured output 解析失败，
与 token 预算无关。

所以：长线程的消息列表会无界增长，服务端唯一的兜底是 Thread TTL（第 6 节）。

## 2. 窗口管理：只有 paradigm B 有

`research_agent` 走 `create_deep_agent`（`research_agent/agent.py:96-107`），
deepagents 0.7.13 的默认中间件栈里**自动包含** `SummarizationMiddleware`
（`create_summarization_middleware`），无需显式配置。

| 行为 | 细节 |
| --- | --- |
| **触发阈值** | 模型 profile 有 `max_input_tokens`：用到 **85%** 触发，保留最近 **10%**；没有 profile：**170k tokens** 触发，保留 **6 条消息** |
| **淘汰的历史不丢** | 先追加到 backend 的 `/conversation_history/{session_id}.md`，摘要里带上路径，agent 之后可 `read_file` 读回 |
| **落盘位置** | 默认 `StateBackend`——文件存在 graph state 的 `files` 通道里，**随 checkpoint 持久化、同一 thread 内可读，不跨 thread**；配置 `SANDBOX_PROVIDER` 时写到 pod 外的 sandbox |
| **状态不篡改** | 用私有 `_summarization_event` 记录，`state["messages"]` 保留原始日志，可回放、可评测 |
| **溢出兜底** | provider 报 `ContextOverflowError` 时先摘要再重试 |
| **参数截断** | `truncate_args_settings` 会先裁剪旧消息里大体积的 `write_file` / `edit_file` 参数，常常不用真正摘要就够用 |

其他与上下文预算相关的设计：

- **SubAgent**（`task` 工具）：子任务在独立上下文里跑，只把结果带回主线程。
- **SkillRouterMiddleware**（`shared/middleware/skill_router.py`）：progressive disclosure
  + per-request top-k，先只给技能描述、按需展开正文——本质是控制技能目录对上下文的占用。
- `MemoryMiddleware` 只有传 `memory=` 才启用，本仓库未启用。

注意：DeepSeek 走 OpenAI 兼容端点，模型 profile 不一定暴露 `max_input_tokens`，
实际很可能落在 170k tokens / 保留 6 条消息这一档。

## 3. 长期记忆：BaseStore

跨会话的"上下文"走 LangGraph Store，实现在 `graphs/shared/memory.py:69-113`：

- 命名空间 `("preferences",)`，key 是 `user_id`，值是一组
  `{key: {value, saved_at}}`。
- **写入过滤**：key/value 长度上限 64/200 字符；正则拒绝卡号、IBAN、邮箱等
  PII 形状的数据（`memory.py:26-30`）——记忆只存偏好和长期规则。
- **保留期**：默认 90 天，读取时按 `saved_at` 过滤过期项。
- 生命周期：`save / load / delete / purge`。

在 graph 里的用法（`shopping_agent/nodes.py`）：

| 节点 | 做什么 |
| --- | --- |
| `initialize`（`:28-35`） | run 开始时按 `configurable.user_id` 读偏好，放进 `State.preferences`，再拼进 system prompt |
| `extract_memory`（`:147-178`） | 一轮结束后只读最近一次 user/assistant 文本（跳过 tool 结果），让模型抽取候选事实，经写过滤后保存；由 `Context.enable_memory_extraction` 控制 |

Store 的隔离与检索：

- `langgraph.json` 的 `store.scopes` + `controller/http/routers/store.py:292-313`
  的 `apply_namespace_scoping`：默认命名空间是 `users/{identity}`；配置 `orgs` 等前缀后，
  可以按用户属性共享给同一组织。
- 语义检索可选：`store.index` 配置（`repo/database.py:83-95`）。

## 4. 运行上下文：`Runtime[Context]`

`Context` 是每次 run 的不可变配置，典型定义：

```python
# graphs/shopping_agent/state.py:46-59
@dataclass(kw_only=True)
class Context:
    model: str = "openai/deepseek-flash"
    fallback_models: list[str] = field(default_factory=list)
    max_quantity_per_line: int = 5
    max_cart_lines: int = 20
    enable_memory: bool = True
    enable_memory_extraction: bool = True
```

流转链路：

```
API run 的 context 字段
  → RunExecution.context                  # usecase/execution/run_job.py:41
  → _filter_context_by_schema()           # usecase/streaming/graph_streaming.py:186-192
  → graph.astream(..., context=context)   # graph_streaming.py:208-218
  → 节点里 runtime.context.model          # shopping_agent/nodes.py:69
```

要点：

- **schema 过滤**：`run_utils.py:61-83` 只保留 `get_context_jsonschema()` 里声明过的 key，
  其余**静默丢弃**，只打 debug 日志（`run_utils.py:77-81`）。用户传错字段的表现为
  "配置没生效"，排查时应先看这条日志。
- **工厂图**：`graph(runtime: ServerRuntime[T])` 形式走 `coerce_context`
  （`repo/graphs/graph_factory.py:284-322`），把 dict 校验成 dataclass 或 pydantic 模型，
  失败时降级为原始 dict；`ServerRuntime` 里带 user/store/context（`:339-384`）。
- **用户身份另走一条线**：`configurable.user_id` / `user_display_name` /
  `langgraph_auth_user` 由 `inject_user_context` 注入（`langgraph_service.py:640-665`），
  不放在 `Context` 里。

## 5. 请求 / 执行上下文：contextvars

HTTP 层的认证走 ASGI scope（见 [auth-context.md](auth-context.md)），
graph 执行层则用 `contextvars`。两套并存，服务对象不同：

| 用途 | 机制 | 位置 |
| --- | --- | --- |
| 认证用户 | `ContextVar`，整个 run 生命周期有效 | `auth/ctx.py:23-67`；`run_executor.py:189` 用 `with_auth_ctx(job.user, ...)` 包住执行；工厂通过 `get_auth_ctx()` 读取（`graph_factory.py:365`） |
| 日志关联 | structlog contextvars：`request_id` / `run_id` / `thread_id` / `graph_id` / `user_id` | `logger_middleware.py:32-33`；worker 的 `_restore_trace_context`（`worker_executor.py:594-632`） |
| OTEL span 属性 | `SpanEnrichmentProcessor` 读 ContextVar，给**每个 span** 打 langfuse user/session/trace 属性 | `span_enrichment.py:42-66` |
| 后台任务传递 | `contextvars.copy_context()` → `asyncio.create_task(..., context=ctx)` | `worker_executor.py:358-362`；`span_enrichment.make_run_trace_context`（`:213-263`） |
| 授权分发去重 | `_dispatched` ContextVar | `auth/enforcement.py:38` |

两个容易踩的点：

1. **worker 进程复用，必须每个 job 先 clear 再 bind**。`_restore_trace_context`
   （`worker_executor.py:594-632`）第一行就是 `clear_contextvars()`，否则上一个 run 的
   `run_id` 会污染下一个。
2. **`contextvars` 不会进 checkpoint**。它只影响当前进程/任务的日志与追踪；
   跨进程（Redis worker）需要把 `run_id`、`thread_id`、trace 信息随 job 序列化后重建。

## 6. 清理与压缩：Thread TTL

`usecase/thread_ttl.py` 是服务端唯一会"缩减已有上下文"的机制，两种策略：

| 策略 | 行为 |
| --- | --- |
| `delete` | 删除 checkpoints，再删 thread 行（级联 runs/crons） |
| `keep_latest` | 每个 `checkpoint_ns` 只保留最新 checkpoint，删除历史 checkpoint 和孤儿 writes（`thread_ttl.py:65-76`），然后重新计时 |

实现要点：

- `FOR UPDATE SKIP LOCKED` 分片领取，跳过有 pending/running run 的线程（不取消运行）。
- 配置优先级：`THREAD_TTL` 环境变量 > `langgraph.json` 的 `checkpointer.ttl`
  （`thread_ttl.py:121-147`）。当前 `langgraph.json` 没有 `checkpointer` 段，**默认关闭**——
  `.env.example:131`：`Unset = threads never expire.`
- 注意：`keep_latest` 压缩的是 **checkpoint 历史**，最新状态里的
  `state["messages"]` 仍然完整。它不能替代窗口管理。

## 7. 边界与已知缺口

1. **两套范式不对等**：`research_agent` 有自动摘要 + 历史落盘；
   `shopping_agent` / `merchant_agent` 没有任何窗口管理。长线程要么加 trimming /
   `SummarizationMiddleware`，要么依赖 TTL。
2. **`Context` 过滤是静默的**：未声明的 key 直接丢弃，只有 debug 日志。
   新增 context 字段时必须同步 graph 的 `context_schema`。
3. **Store ≠ 运行上下文**：`runtime.store` 是持久记忆，`runtime.context` 是本次 run 的配置。
   把临时配置写进 Store 会跨会话泄漏。
4. **TTL ≠ 窗口管理**：见第 6 节，TTL 只动 checkpoint 历史，不动最新消息列表。
5. **摘要阈值依赖模型 profile**：没有 `max_input_tokens` 时用固定 170k 触发，
   对窗口更小的模型可能偏大；接入新 provider 时应确认 profile。
6. **认证有两套机制**：HTTP 端点读 `request.scope["user"]`，graph 内读
   `get_auth_ctx()`。在 graph 代码里不要试图读 `request`；在路由代码里也不要依赖
   `get_auth_ctx()`（run 尚未开始，ctx 为空）。

## 8. 排查清单

| 现象 | 先看 |
| --- | --- |
| 多轮后模型报 context 超限 | 该 graph 是否 paradigm B？A 类没有裁剪。检查 `state["messages"]` 长度与 TTL 配置 |
| `research_agent` 突然"忘事" | 是否触发了摘要：检查 backend 的 `/conversation_history/{session_id}.md` 是否生成 |
| run 的 `context` 参数没生效 | `_filter_context_by_schema` 是否把它丢了（debug 日志 `Filtering out context parameter`） |
| 偏好记忆没加载 | `configurable.user_id` 是否存在、`store.scopes` 是否正确、retention 是否过期 |
| 日志里 `run_id` 串了 | worker 是否在 job 开始时 `clear_contextvars()` |
| thread 历史没被清理 | `THREAD_TTL` / `checkpointer.ttl` 是否配置；thread 是否一直有 active run |
| HITL 恢复后状态不对 | checkpoint 是否被 TTL `keep_latest` 压缩过；中断期间是否有并发写 |

## 代码索引

```
src/graphs/shopping_agent/state.py:3-5         State vs Context 的定义
src/graphs/shopping_agent/state.py:23          messages + add_messages
src/graphs/shopping_agent/state.py:46-59       Context（per-run 配置）
src/graphs/shopping_agent/nodes.py:28-35       initialize：加载长期记忆
src/graphs/shopping_agent/nodes.py:42-61       _classify_history（非裁剪）
src/graphs/shopping_agent/nodes.py:89          完整历史喂给模型（paradigm A）
src/graphs/shopping_agent/nodes.py:147-178     extract_memory：写回长期记忆
src/graphs/merchant_agent/state.py:15/31-48    State / Context
src/graphs/research_agent/agent.py:96-107      create_deep_agent（含摘要中间件）
src/graphs/shared/memory.py:69-113             MemoryStore（过滤 + retention）
src/graphs/shared/middleware/skill_router.py   top-k 技能选择
src/agent_server/repo/database.py:78-95        Checkpointer / Store 初始化
src/agent_server/repo/graphs/langgraph_service.py:294-407  每请求注入 checkpointer/store
src/agent_server/repo/graphs/langgraph_service.py:640-665  inject_user_context
src/agent_server/repo/graphs/langgraph_service.py:680-730  create_run_config（thread_id 钉死）
src/agent_server/repo/graphs/graph_factory.py:284-322      coerce_context
src/agent_server/repo/graphs/graph_factory.py:339-384      ServerRuntime
src/agent_server/usecase/execution/run_job.py:34-48        RunExecution.context
src/agent_server/usecase/execution/run_utils.py:61-83      context schema 过滤
src/agent_server/usecase/execution/run_executor.py:181-190 with_auth_ctx
src/agent_server/usecase/execution/worker_executor.py:594-632  worker trace 上下文重建
src/agent_server/usecase/streaming/graph_streaming.py:186-218  过滤 + astream(context=)
src/agent_server/usecase/thread_ttl.py:1-12/65-76/121-180      TTL 策略与压缩
src/agent_server/controller/http/routers/store.py:292-313      Store 命名空间隔离
src/agent_server/auth/ctx.py:23-67             with_auth_ctx / get_auth_ctx
src/agent_server/infra/observability/span_enrichment.py:42-66/213-263  span/trace 上下文
src/agent_server/controller/http/middleware/logger_middleware.py:32-33  request_id 绑定
src/agent_server/auth/enforcement.py:38        _dispatched ContextVar
```
