# 图状态（State）设计：通道、reducer 与边界

项目里的 "state" 分两套：**graph 状态**（LangGraph 通道，进 checkpoint）和
**服务端状态**（DB 里的 run/thread status）。本文讲前者——状态 schema 怎么切分、
每个字段用什么 reducer、工具和子图如何写状态、HTTP 层如何读写它。

## TL;DR

| 设计 | 做法 |
| --- | --- |
| **输入/内部隔离** | `InputState` 只含 `messages`，`State` 继承它再加内部字段；`input_schema=InputState` |
| **运行配置隔离** | `Context` 是 per-run 配置，不进 checkpoint |
| **默认覆盖** | 不加 reducer 的字段每次写入直接替换 |
| **消息追加** | `Annotated[..., add_messages]`，按 message id 去重/替换 |
| **列表拼接** | `Annotated[..., operator.add]`（provenance、UI blocks） |
| **托管值** | `IsLastStep` 由 LangGraph 按 recursion_limit 维护 |
| **大通道增量** | `DeltaChannel` 把 checkpoint 增长从 O(N²) 降到 O(N)（deepagents） |
| **工具写状态** | 返回 `Command(update={...})`，消息和状态原子写入 |
| **子图共享** | 同名 + 同 reducer 的通道自动桥接；私有通道不出子图 |
| **HTTP 读写** | `aget_state` / `aupdate_state` / `aget_state_history` → `ThreadState` |

## 1. 四份 state schema

| 包 | 文件 | 形态 | 特点 |
| --- | --- | --- | --- |
| `shopping_agent` | `state.py:19-59` | `InputState` → `State` + `Context` | 最完整：provenance、UI、记忆、购物车 |
| `merchant_agent` | `state.py:13-48` | 同形 | staged writes 的 provenance + 分析预算 |
| `order_agent`（子图） | `subgraphs/order_agent/state.py:17-22` | 独立 `OrderState` | 只与父图共享 `messages` 和 `presentations` |
| `research_agent` | `agent.py:46-49` | `ResearchAgentState(DeepAgentState)` | 复用 deepagents 状态，只加 `presentations` |

`graphs/README.md:113` 的规则：**`state.py` 永远不变成包——一个图，一份数据契约。**

## 2. 三层切分：InputState / State / Context

以 shopping 为例：

```python
# state.py:19-23 —— 外部接口：调用方只能传 messages
@dataclass
class InputState:
    messages: Annotated[Sequence[AnyMessage], add_messages] = field(default_factory=list)

# state.py:26-43 —— 内部完整状态
@dataclass
class State(InputState):
    intent: Intent = "chat"
    seen_product_ids: Annotated[list[str], operator.add] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)
    presentations: Annotated[list[dict[str, Any]], operator.add] = field(default_factory=list)
    cart: dict[str, Any] = field(default_factory=dict)
    is_last_step: IsLastStep = field(default=False)

# state.py:46-59 —— 单次 run 的配置，不进 checkpoint
@dataclass(kw_only=True)
class Context:
    model: str = "openai/deepseek-flash"
    max_quantity_per_line: int = 5
    ...
```

`builder.py:27` 把它接到图定义上：

```python
builder = StateGraph(State, input_schema=InputState, context_schema=Context)
```

设计要点：

- **`input_schema=InputState` 是安全边界**：API 调用方无法注入 `intent`、`seen_product_ids`、
  `cart`、`presentations`。`seen_product_ids` 是购物车 provenance gate 的依据
  （`tools.py:79`），`presentations` 是前端 UI 的真相来源，都不能由客户端伪造。
- **`State` 继承 `InputState`**：输入字段是内部状态的子集。节点只声明 `State`，
  调用方只可能提供 `InputState` 的字段。
- **`Context` 与 State 完全隔离**：model、fallback、限额、开关不写进 checkpoint。
  详见 [context-management.md](context-management.md) 第 4 节。
- **`merchant_agent` 同形**（`state.py:13-48`）：`InputState` → `State`
  （`seen_listing_ids`、`staged_change_ids`、`analytics_calls`）+ 独立的 `Context`。

## 3. 通道语义与 reducer

LangGraph 的每个字段是一个 channel，写入语义由 `Annotated[..., reducer]` 决定：

| 语义 | 写法 | 例子 |
| --- | --- | --- |
| 覆盖（默认） | 不加 reducer | `intent`、`cart`、`preferences`、`analytics_calls` |
| 消息追加 | `Annotated[..., add_messages]` | `messages`（按 id 去重/替换） |
| 列表拼接 | `Annotated[..., operator.add]` | `seen_product_ids`、`presentations` |
| 托管值 | `IsLastStep` | `is_last_step`，由 LangGraph 按 recursion_limit 维护 |
| 增量通道 | `DeltaChannel(reducer, snapshot_frequency=50)` | deepagents 的 `messages`、`files` |

### `add_messages` 与 `operator.add` 的区别

- `add_messages` 是**按 id 合并**：同 id 的消息替换，新 id 追加；还支持
  `RemoveMessage` 删除。它不只是 `list.extend`。
- `operator.add` 是纯拼接，没有去重——`presentations` 重复写入就会重复渲染。

### Managed value：`IsLastStep`

`is_last_step`（`shopping_agent/state.py:43`）不是普通字段，由 LangGraph 根据
`recursion_limit` 在执行期维护。节点用它做优雅退出：

```python
# nodes.py:93-102
if state.is_last_step and response.tool_calls:
    return {"messages": [AIMessage(content="...")]}   # 不再请求工具
```

### `DeltaChannel`：research_agent 的 checkpoint 优化

deepagents 的 `DeepAgentState`（`.venv/.../deepagents/graph.py:73-76`）把 `messages`
换成了：

```python
messages: Required[Annotated[list[AnyMessage],
    DeltaChannel(_messages_delta_reducer, snapshot_frequency=50)]]
```

普通 `add_messages` 每个 superstep 都把完整消息列表写进 checkpoint，长对话是
**O(N²)** 增长；`DeltaChannel` 只存增量写、每 50 次更新（或 5000 个 superstep）
落一次全量快照，增长变成 **O(N)**，恢复时按 reducer 回放祖先写。

`AgentState` 另外两个字段也有明确语义：

- `jump_to`：`EphemeralValue + PrivateStateAttr`——中间件跳转用，不进 checkpoint。
- `structured_response`：`OmitFromInput`——只由模型结构化输出产生，不接受外部输入。

`FilesystemMiddleware` 还会动态贡献 `files` 通道（同样 `DeltaChannel`，
`.venv/.../deepagents/middleware/filesystem.py:1088-1091`），但只在 backend 使用
`StateBackend` 时注册；配了 `SANDBOX_PROVIDER` 时文件在 pod 外，不进 state。

`ResearchAgentState` 自己只加了一个通道（`agent.py:46-49`）：

```python
class ResearchAgentState(DeepAgentState):
    presentations: NotRequired[Annotated[list[dict], operator.add]]
```

`NotRequired` 让这个通道对输入可选，同时保留默认 reducer。

## 4. 工具怎么写 state

工具有两种返回方式，项目统一用 `Command`：

```python
# shared/tooling.py:23-28
def tool_ok(result, tool_call_id, *, state_update=None) -> Command:
    update = {"messages": [ToolMessage(...)]}
    if state_update:
        update.update(state_update)
    return Command(update=update)
```

实际写入点：

| 字段 | 写入者 | 位置 |
| --- | --- | --- |
| `seen_product_ids` | `search_products` | `shopping_agent/tools.py:51` |
| `cart` | `get_cart` / `add_to_cart` | `tools.py:64,89` |
| `presentations` | `present_*` | `shared/presentation.py:157` |
| `analytics_calls` | 分析工具 | `merchant_agent/tools.py:187` |

要点：

- **消息和状态在同一个原子写里**：不会出现"ToolMessage 说成功了，但 state 没更新"。
- **gate 拒绝不写 state**：`tool_blocked`（`shared/tooling.py:31-34`）只返回错误消息，
  语义上就是"这次操作没发生"。provenance、数量、购物车容量检查都走这条路。
- **工具不直接 import 后端构造客户端**：依赖由 builder 注入（`graphs/README.md:122-124`），
  state 写入只是返回值的一部分。

## 5. 子图边界

`order_agent` 被当作普通节点编译进父图（`shopping_agent/builder.py:37`）：

```python
builder.add_node("order_agent", build_order_agent(backend).compile())
```

`OrderState`（`subgraphs/order_agent/state.py:17-22`）：

```python
@dataclass
class OrderState:
    messages: Annotated[Sequence[AnyMessage], add_messages] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    presentations: Annotated[list[dict[str, Any]], operator.add] = field(default_factory=list)
```

- **共享通道**：`messages`、`presentations` 同名 + 同 reducer，父子图自动桥接——
  对话流进去，回答和 UI blocks 流回来。
- **私有通道**：`orders` 只存在于子图，返回父图时丢弃。
- **复用父图 Context**：`order_agent/builder.py:14` 直接
  `StateGraph(OrderState, context_schema=Context)`，不定义独立配置。

这也是 `graphs/README.md` 的结构规则：子图没有自己的 state 时是一个模块，不是包；
有独立 state 时递归同样的目录结构。

## 6. 服务端视角：checkpoint → ThreadState

state 本体在 Postgres checkpointer 里（`repo/database.py:78-81`），HTTP 层把它映射成
`ThreadState`（`domain/threads.py:185-198`）：

```python
values, next, tasks, interrupts, metadata,
created_at, checkpoint, parent_checkpoint
```

三个入口，都通过 `langgraph_service.get_graph(..., access_context=...)` 拿到带
checkpointer 的图副本（`langgraph_service.py:294-407`）：

| 接口 | LangGraph 调用 | access_context | 位置 |
| --- | --- | --- | --- |
| `GET /threads/{id}/state` | `aget_state(subgraphs=...)` | `threads.read` | `threads.py:453` |
| `POST /threads/{id}/state` | `aupdate_state(values, as_node=...)` | `threads.update` | `threads.py:583-585` |
| `GET/POST /threads/{id}/history` | `aget_state_history(limit, before)` | `threads.read` | `threads.py:824-833` |

细节：

- `aupdate_state` **新建一个 checkpoint**，不是原地修改；`as_node` 决定这次写入以
  哪个节点的身份发生，从而影响后续 edge 路由。
- `as_node="__copy__"` + `checkpoint_id` 是 LangGraph Studio 的 "Re-run from here"
  语义：只做 checkpoint 分叉，不改值（`threads.py:504-508`）。
- `ThreadState.values` 是**全量通道字典**，客户端能看到 `messages`、`cart`、
  `seen_product_ids` 等全部内部字段。
- 快照转换在 `usecase/thread_state_service.py:20-69`；`tasks` 里的子图状态会递归序列化。
- `access_context` 决定工厂图拿到哪种 `ServerRuntime`（`graph_factory.py:48-58`）：
  `threads.create_run` 有完整 `context`，`threads.read` / `threads.update` 只有只读视图。

## 7. 服务端状态：run / thread status 不是图状态

不要和 graph state 混淆：

| 状态 | 取值 | 位置 |
| --- | --- | --- |
| Run status | `pending / running / error / success / timeout / interrupted` | `domain/status_compat.py:25-32` |
| Thread status | `idle / busy / interrupted / error` | `domain/status_compat.py:52` |

这些是 DB 列，用于列表过滤、取消、TTL 判断，**不进入 checkpoint**，也不由图节点直接写。
图状态回答"这次对话进行到哪、有哪些数据"；服务端状态回答"这个 run/thread 在系统里
处于什么生命周期阶段"。

## 8. 设计要点与缺口

**值得借鉴的**：

1. 输入/内部状态分离（`InputState`），挡住客户端伪造 provenance 和 UI 数据。
2. UI、provenance、预算都放进 state，节点和工具只读它，不依赖隐式上下文。
3. 工具用 `Command` 原子更新消息 + 状态，gate 拒绝不产生状态副作用。
4. 大列表用 `DeltaChannel` 控制 checkpoint 增长。
5. 子图靠通道名共享，不需要显式 plumbing。

**要注意的**：

1. `ThreadState.values` 暴露全部内部字段，包括 `seen_product_ids` 这类业务规则依据；
   目前靠 API 鉴权兜底，没有字段级过滤。
2. `presentations` / `seen_product_ids` 用 `operator.add` 只增不减，长线程会持续增长
   （和 `messages` 一样受上下文管理缺口影响，见 [context-management.md](context-management.md)）。
3. `shopping_agent` / `merchant_agent` 的 `messages` 仍是普通 `add_messages`，
   长对话 checkpoint 是 O(N²) 增长；只有 deepagents 路径用了 `DeltaChannel`。
4. `is_last_step` 是 managed value，只在执行期有效，不要试图在 `aupdate_state` 里写它。
5. 新增字段时要想清楚 reducer：追加型字段用 `operator.add` 后基本无法"重置"，
   需要重置语义时用覆盖字段或显式清空节点。

## 代码索引

```
src/graphs/shopping_agent/state.py:19-23        InputState
src/graphs/shopping_agent/state.py:26-43        State（内部字段 + reducer）
src/graphs/shopping_agent/state.py:46-59        Context（per-run 配置）
src/graphs/shopping_agent/builder.py:27         StateGraph(input_schema=..., context_schema=...)
src/graphs/shopping_agent/builder.py:37         子图作为节点
src/graphs/shopping_agent/tools.py:51/64/89     工具写 seen_product_ids / cart
src/graphs/shopping_agent/subgraphs/order_agent/state.py:17-22  OrderState
src/graphs/shopping_agent/subgraphs/order_agent/builder.py:14   复用父图 Context
src/graphs/merchant_agent/state.py:13-48        merchant 的 InputState/State/Context
src/graphs/merchant_agent/tools.py:187          analytics_calls 写入
src/graphs/research_agent/agent.py:46-49        ResearchAgentState
src/graphs/shared/tooling.py:23-34              tool_ok / tool_blocked / tool_error
src/graphs/shared/presentation.py:157           presentations 写入
src/agent_server/domain/threads.py:185-198      ThreadState
src/agent_server/domain/threads.py:200-211      ThreadStateUpdate
src/agent_server/usecase/thread_state_service.py:20-69  snapshot → ThreadState
src/agent_server/repo/database.py:78-81         Postgres checkpointer
src/agent_server/repo/graphs/langgraph_service.py:294-407  每请求注入 checkpointer
src/agent_server/repo/graphs/graph_factory.py:48-58        access_context 语义
src/agent_server/controller/http/routers/threads.py:453    aget_state
src/agent_server/controller/http/routers/threads.py:583-585 aupdate_state
src/agent_server/controller/http/routers/threads.py:824-833 aget_state_history
src/agent_server/domain/status_compat.py:25-52  run/thread status 校验
```
