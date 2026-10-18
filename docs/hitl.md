# Human-in-the-Loop（HITL）

项目**原生支持** HITL，但没有独立的 `hitl/` 模块 —— 它由 Agent Protocol 的
`interrupt` / `command` 语义、v2 事件流的 `input.requested` / `input.respond`，
以及示例图里的 LangGraph `interrupt()` 共同组成。检索时不要只搜 "hitl"，
框架代码里拼作 `interrupt`、`interrupted`、`input.requested`、`input.respond`。

## 先厘清 v1 与 v2

下文的 **v1** / **v2** 是 **Agent Protocol 的两代流式接口**，同一个服务端都实现了，
不是两个独立协议：

| | v1（run-scoped） | v2（thread-scoped） |
| --- | --- | --- |
| 端点 | `POST /threads/{id}/runs`、`/runs/stream`、`/runs/wait` 等 | `POST /threads/{id}/stream/events`（SSE）+ `POST /threads/{id}/commands` |
| 流的范围 | 一次 run 一条流 | 整个 thread 一条流，跨多次 run |
| 事件形状 | 传统 SSE 帧：`values` / `updates` / `messages` / `events` | 协议信封：`{type, seq, event_id, method, params}`，按 channel 过滤 |
| 谁在用 | LangGraph SDK、Agent Chat UI、AG-UI（默认 `streamProtocol="legacy"`） | 支持 v2 的新版 SDK；由 `FF_V2_EVENT_STREAMING` 开关 + 运行时能力门控 |
| 代码位置 | `controller/http/routers/runs.py`、`usecase/streaming/graph_streaming.py` | `controller/http/routers/event_streaming.py`、`usecase/streaming/v2/` |

对应到 HITL，两条路的「恢复方式」不同：

- **v1**：`POST /threads/{id}/runs`，body 里放 `command: {"resume": ...}`
  （或 `update` / `goto`），服务端转成 LangGraph `Command`。
- **v2**：发 `input.respond` 命令（`{interrupt_id, response}`），或者对已中断
  线程发 `run.start` 带 `input`，服务端自动当成 resume。中断本身在 v2 里是
  `input.requested` 事件。

> 注意：`docs/error-handling.md` 里的 "v1/v2" 指 LangGraph 的事件生产方式
> （`astream` vs `astream_events(version="v3")`），和这里的协议代际无关。

## 支持的入口

| 入口 | 位置 | 说明 |
| --- | --- | --- |
| v1 REST：`POST /threads/{thread_id}/runs` + `command` | `domain/runs.py:81-93` | 用 `Command` 恢复被中断的 run |
| v1 REST：`interrupt_before` / `interrupt_after` | `domain/runs.py:86-93` | 在指定节点执行前/后设置断点，`"*"` 表示全部 |
| v2 事件流：`input.requested` → `input.respond` | `usecase/streaming/v2/commands.py:224-269` | SDK 的 HITL 交互通道 |
| v2 事件流：`run.start` 带 `input` | `usecase/streaming/v2/commands.py:181-189` | 线程已中断时自动当作 `{"resume": input}` |
| Cron | `domain/crons.py:57-58`、`usecase/cron/scheduler.py:60-61` | 定时任务同样能声明节点断点 |

## 一次完整的生命周期

```
图内调用 interrupt(payload)
        │
        ▼
LangGraph 暂停，检查点落库
        │
        ▼
run_executor 检测到 final_output.has_interrupt
  → run.status = "interrupted"
  → thread.status = "interrupted"
        │
        ▼
事件推送到客户端
  v1: interrupt 出现在 values/updates 的 __interrupt__ 中
  v2: 归一化为 input.requested（每 interrupt_id 只发一次）
        │
        ▼
客户端提交人工决议
  v1: POST /runs，body.command = {"resume": ...}
  v2: input.respond {interrupt_id, response}
        │
        ▼
服务端校验 thread 确实处于 interrupted
  → 组装 RunCreate(command=...)
  → map_command_to_langgraph() 转成 LangGraph Command
        │
        ▼
图从检查点恢复，interrupt() 的返回值即人工决议
```

## 服务端实现落点

### 请求模型

`RunCreate` 的三个 HITL 字段（`src/agent_server/domain/runs.py:81-93`）：

- `command: dict | None` —— 恢复命令（`resume` / `update` / `goto`）
- `interrupt_before: str | list[str] | None`
- `interrupt_after: str | list[str] | None`

`input` 与 `command` 同时出现时 **command 优先、input 被丢弃**
（`domain/runs.py:149-165`）。这是为 AG-UI 前端保留的兼容行为：它恢复时总是
同时发送两者，input 携带会话状态，command 携带恢复值。两者都没有且没有
checkpoint 时才报错。

### 恢复校验与执行

- `usecase/execution/run_preparation.py:39-75` —— 带 `resume` 的 command 只能
  打到 `interrupted` 状态的线程。由于 interrupt 事件可能先于
  `finalize_run` 的提交到达客户端，这里会用**新 session 轮询**最多 10 次、
  每次 100ms，避免合法恢复被误判成 400。
- `usecase/execution/run_executor.py:58-77` —— `has_interrupt` 时把 run 和
  thread 都置为 `interrupted`，而不是 `success`/`error`。
- `usecase/execution/run_executor.py:253-260` —— 从 `values.params.interrupts`
  或 payload 里的 `__interrupt__` 键识别中断。
- `usecase/execution/run_executor.py:263-284` —— 把
  `interrupt_before`/`interrupt_after` 写进 LangGraph config，并调用
  `map_command_to_langgraph`。
- `usecase/execution/run_utils.py:13-29` —— API 的 command dict 转成
  `Command(update=..., goto=..., resume=...)`。

### v2 事件流

- `usecase/streaming/v2/session.py:250-270` —— 把 updates/values 里的
  `__interrupt__` 转成 `input.requested`，按 `interrupt_id` 去重。
- `usecase/streaming/v2/session.py:339-354` —— `input.requested` 映射到 `input`
  订阅通道。
- `usecase/streaming/v2/commands.py:106-119` —— `INPUT_RESPOND_KEYS` 显式声明
  接受的参数；未知键会打 warning（API 兼容策略）。
- `usecase/streaming/v2/commands.py:272-316` —— 支持单条 `response`、批量
  `responses`，以及以 `interrupt_id` 为键的 resume map（多中断同时待处理时
  唯一可用的定向恢复形式）。
- `usecase/streaming/v2/normalizers.py:52-53` —— run 状态 `interrupted` 的
  线上映射。

## 图作者视角

图里只需要一个 `interrupt()` 调用，返回值就是客户端提交的决议：

```python
# src/graphs/shopping_agent/tools.py:92-109
approved = interrupt({"type": "checkout_approval", "cart": format_cart(cart)})
if not approved:
    return tool_ok("Checkout cancelled; the cart is unchanged.", tool_call_id)
```

```python
# src/graphs/merchant_agent/tools.py:112-138
# 写操作先 stage，apply 时永远暂停等人工批准，并重新校验 guardrail
approved = interrupt({"type": "apply_change_approval", "change_id": _format_change(change)})
if not approved:
    return _msg(f"Change {change_id} was not approved; it stays pending.", tool_call_id)
```

两个示例分别演示了「客户确认下单」和「员工审批写操作」。OAuth 连接也复用了
同一个 HITL 通道把 `connect_url` 交给前端（`src/hub/oauth/flow.py:10`）。

## 前端

- `apps/web/components/CheckoutApproval.tsx` —— 聊天内渲染的审批卡片
  （Approve / Cancel）。
- `apps/web/app/page.tsx:99` —— 收到 interrupt 后渲染该卡片并回写决议。

## 测试覆盖

- `tests/e2e/test_human_in_loop/test_human_in_loop.py` —— 完整周期、拒绝、
  忽略、编辑参数、标记解决、流式场景。
- 载体图：`tests/e2e/graphs/react_agent_hitl/`（含 `human_approval` 节点）、
  `tests/e2e/graphs/subgraph_hitl_agent/`（子图内中断）。
- `tests/integration/test_usecase/test_streaming_hitl.py` —— 流式服务的 HITL 行为。
- `tests/unit/test_usecase/test_event_streaming/test_session.py`、
  `test_commands.py` —— v2 事件归一化与恢复命令组装。
- `tests/e2e/test_event_streaming/test_sdk_v2_e2e.py` —— `input.requested` /
  `input.respond` 的 SDK 端到端路径。

## 注意事项

- **状态是契约的一部分**：`interrupted` 同时是 run 和 thread 的合法状态，
  恢复请求必须命中 `interrupted` 线程，否则 400。
- **不要吞掉未声明的 SDK 字段**：v2 的参数集合在
  `commands.py` 的 `RUN_START_KEYS` / `INPUT_RESPOND_KEYS` 中显式声明，
  新字段必须要么生效、要么返回 422（见 `AGENTS.md` 的 API 兼容章节）。
- **恢复值可以是 `None`**：校验看的是 `resume` 键是否存在，而不是值是否非空
  （`run_preparation.py:51-55`）。
