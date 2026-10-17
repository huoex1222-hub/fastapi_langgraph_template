# 协议分层

`/threads/{thread_id}/runs/stream` 是 **Agent Protocol** 定义的，**不是 AG-UI**。
两者是不同层的东西：

```
AG-UI                      事件协议：定义 RUN_STARTED / TEXT_MESSAGE_CONTENT /
   ↑                       STATE_SNAPSHOT / TOOL_CALL_* 这些【事件类型】，
   │ @ag-ui/langgraph      不定义任何 REST 路径
   │ （适配器）
LangGraph SDK              客户端：调 REST + 解析 SSE
   ↑
Agent Protocol             REST 协议：/assistants、/threads、/runs、/store、/crons
```

**AG-UI 是「调用方那层」，Agent Protocol 是「被调用的接口」。** `@ag-ui/langgraph`
只做翻译：

```
Agent Protocol 的 SSE 帧            AG-UI 事件
  values / updates / messages  →  STATE_SNAPSHOT / TEXT_MESSAGE_CONTENT / ...
```

项目自己也是这么定位的：

- `README.md:5` —— *"It speaks the Agent Protocol (LangGraph Platform compatible), so the
  LangGraph SDK, Agent Chat UI and LangGraph Studio work against it out of the box"*
- `docs/openapi.json` 的 `info.description` —— *"Production-ready Agent Protocol server"*

服务端只要符合 Agent Protocol，LangGraph SDK / Studio / Agent Chat UI 都能直连 ——
AG-UI 只是众多前端消费方式之一。

## AG-UI 到底干什么

**后端完全不知道 AG-UI 存在。** `grep -rni "ag-ui" src/` 只命中一条注释（`domain/runs.py:152`
提到「AG-UI 客户端会这么发」）—— 服务端只认 Agent Protocol，所以 LangGraph Studio /
Agent Chat UI / curl 都能连。

它存在的理由：**两者的「事件语义」不同**。

| Agent Protocol 的 SSE 帧 | 它是给状态机看的 |
| --- | --- |
| `values` | 完整 state 快照 |
| `updates` | 某节点的增量 |
| `messages` | LangChain 消息元组 |
| `checkpoint` | 断点信息 |

UI 要的不是这些，而是「往气泡追加一个字」「画一个工具卡片」「更新应用状态」。
`@ag-ui/langgraph` 就干这个翻译（`EventType.*` 共 20 种）：

| 收到的帧 | 翻成 AG-UI 事件 | UI 反应 |
| --- | --- | --- |
| `metadata`（含 run_id） | `RUN_STARTED` | 显示「思考中」 |
| token 流 | `TEXT_MESSAGE_START` / `CONTENT` / `END` | 打字机效果 |
| 工具调用 | `TOOL_CALL_START` / `ARGS` / `END` / `RESULT` | 工具卡片 |
| `values` / `updates` | `STATE_SNAPSHOT` | 更新 `agent.state` |
| 推理块 | `REASONING_START` / `MESSAGE_CONTENT` / `END` | 折叠的思考过程 |
| `end` / 错误 | `RUN_FINISHED` / `RUN_ERROR` | 结束状态 |

## AG-UI ↔ Agent Protocol 映射

分三层：**概念**（谁对应谁）、**调用**（动作对应哪个 HTTP）、**事件**（帧对应哪个事件）。

### 概念映射

| AG-UI 概念 | Agent Protocol 概念 | 说明 |
| --- | --- | --- |
| **Agent**（`LangGraphAgent({graphId})`） | **Assistant** | ⚠️ 名字不同，是同一个东西。`assistants.search({graphId})` 把它解析成 `assistant_id` |
| Thread | **Thread** | 1:1 |
| Run | **Run** | 1:1 |
| `agent.state` | **Thread state**（`values`） | AG-UI 的 state 不是独立资源，就是线程状态 |
| AG-UI 的消息列表 | **thread state 的 `messages` 通道** | |
| Interrupt（HITL） | **Run 的 `interrupts`** | 来自 `getState()` 的 `tasks[].interrupts` |
| `tools` | `RunCreate.tools` | |

**AG-UI 里没有「Assistant」这个词，但每次都要靠它工作。**

### 调用映射

| AG-UI 接口 | Agent Protocol 调用 |
| --- | --- |
| `runAgent()` | `POST /threads/{id}/runs/stream` |
| `abortRun()` | `POST …/runs/{run_id}/cancel` + 断开 SSE |
| `addMessage()` | **本地操作**，随下次 run 作为 `input.messages` 发出去 |
| 读状态 | `GET /threads/{id}/state` |
| 准备 thread | `GET /threads/{id}` → 404 → `POST /threads` |
| 解析 Agent | `POST /assistants/search`（`{graphId, limit: 1}`） |

### 订阅了哪几种 stream mode

```js
streamMode ?? [`events`, `values`, `updates`, `messages-tuple`]
```

四种全要，因为不同事件靠不同 mode 喂：

| mode | 拿来做什么 |
| --- | --- |
| `events` | 原始 LangGraph 事件（`RAW`、节点切换） |
| `values` | 完整 state → `STATE_SNAPSHOT` |
| `updates` | 节点增量 → `STEP_STARTED` / `STEP_FINISHED` |
| `messages-tuple` | token 流 → `TEXT_MESSAGE_*`、`TOOL_CALL_*` |

> 具体到「某一帧唯一对应某个事件」没有逐行验证，这张表是帧语义 + 事件名的合理对应。

### 字段映射（`RunAgentInput` → `RunCreate`）

从 `prepareStream` 的实际组装看：

```js
{
  input: this.langGraphDefaultMergeState(values, [message], e),
  checkpointId: checkpoint.checkpoint_id,
  streamMode: modes,
  config: config,
  command: resumeCommand,          // 恢复中断时
}
```

| AG-UI 输入 | Agent Protocol 字段 |
| --- | --- |
| `threadId` | 路径参数 `{thread_id}` |
| `messages` + `state` | `input`（合并后） |
| `forwardedProps.streamMode` | `stream_mode` |
| `forwardedProps.nodeName` 等 | `config` |
| 检查点 | `checkpoint_id` |
| 恢复中断 | `command`（如 `{resume: ...}`） |

> 一句话：**AG-UI 是同一套东西的「UI 视角命名」** —— Agent = Assistant，
> state = thread state，你发出的动作 = `RunCreate`，你收到的东西 = 四种 stream mode
> 的帧翻译成的事件。

## 它在项目里干三件事

1. **HTTP 客户端** —— 那 10 个 `this.client.*` 调用，走 Agent Protocol（见下节）。
2. **事件翻译器** —— SSE 帧 → 上面的 AG-UI 事件。
3. **生成式 UI 的桥**（最值钱的一件）：

```
图的 presentations/cart 状态
   → SSE values 帧
   → AG-UI STATE_SNAPSHOT
   → useAgent({updates:[OnStateChanged]})
   → page.tsx 的组件注册表
   → <ProductCarousel> / <PlanChecklist> …
```

没有 AG-UI，这条链要自己写：解析 SSE、diff state、触发 React 更新。

## 换掉它，后端不用改

| 换成 | 要做什么 |
| --- | --- |
| LangGraph Studio | 什么都不用改（它也讲 Agent Protocol） |
| 自己写的 `fetch` + SSE 解析 | 自己解析 `values` / `updates` 帧 |
| curl | 直接看原始帧 |

**AG-UI 是「消费 Agent Protocol 的一种方式」，不是协议本身的一部分。**

> 一句话：**Agent Protocol 决定「后端说什么」，AG-UI 决定「前端怎么听懂并画出来」。**

## Agent Protocol 内部还分两代

| | **v1**（AG-UI 当前在用） | **v2** |
| --- | --- | --- |
| 端点 | `POST /threads/{id}/runs/stream` | `POST /threads/{id}/stream/events`<br>`POST /threads/{id}/commands` |
| 事件形状 | `values` / `updates` / `messages` | 协议事件信封（按 channel 过滤） |
| 项目状态 | ✅ | ✅ 也实现了（`event_streaming.py`），由 flag 门控 |
| AG-UI | 默认 `streamProtocol="legacy"` 走这条 | ❌ 不走 |

所以更准确的说法是：**这个端点是 Agent Protocol v1 的「run-scoped SSE」**，
AG-UI 的默认配置恰好选了它，而不是 v2 的 thread-scoped event stream。

---

[← 返回大纲](README.md)
