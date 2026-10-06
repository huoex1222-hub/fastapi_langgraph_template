# 调试 LLM 调用：Langfuse ↔ mitmweb 精确关联

一次 run 里有十几次 LLM 调用时，靠时间戳和 token 数对表很痛苦。现在**每次调用都有
provider 自己发的唯一编号**，两边都记得住，一一对应、零猜测。

## TL;DR 工作流

```
Langfuse 点开可疑的 generation → Output → response_metadata.headers["x-ds-trace-id"] → 复制
   → mitmweb 过滤框输入  ~hs"<那个id>"   → 唯一命中这次请求的字面字节
```

反向：mitmweb 点开 flow → 响应头 `x-ds-trace-id` / 请求头 `x-run-id` → 回 Langfuse 搜。

## 三个关联键（粒度从粗到细）

| 键 | 位置 | 用途 |
| --- | --- | --- |
| `run_id` / `thread_id` | 请求头（LLM 调用）+ 日志字段 + trace metadata | 缩到"这一次 run"的全部调用 |
| `x-ds-trace-id` | **响应头**（DeepSeek 每次调用一个）+ generation 的 output | **精确到单次调用**（主键） |
| `request_id`（错误响应里） | 报错文本 + 响应体 | 失败调用的锚点 |

## 从 span 跳到代码行

Langfuse 的 span 名是**节点名**（`classify`、`shop`、`extract_memory`），不是代码位置。
现在每个**构造模型的节点 span** 都带 OTel 官方的代码属性：

```
code.filepath: graphs/shopping_agent/nodes.py
code.lineno:   52
code.function: shop
```

在 Langfuse 里点开 `shop` span → Metadata 面板 → 直接定位到那一行。

实现（踩了两个坑之后才定下来）：

1. 模型构造时，`shared/models.py::_caller_code_location()` 走栈拿到**调用者的帧**
   （跳过 site-packages），写进 `set_span_code_location()` 的 **ContextVar**；
2. span 开始时，`infra/observability/span_enrichment.py::CodeLocationProcessor` 读 ContextVar
   并打属性。

为什么不能直接走栈、也不能用 `get_current_span()`：

- **节点体里没有"当前 span"**——openinference 不把 span 挂在 OTEL 上下文里，
  `trace.get_current_span()` 在节点里返回 `span_id=0x0`；
- **span 是在线程池的工作线程上创建的**——langchain 对 async 链路执行同步 callback 时走
  `run_in_executor`，那个线程的栈里**没有业务代码的帧**（`threading.py` 就到底了）。
  所以位置必须在"有帧的地方"（模型构造）捕获，靠 ContextVar 跨线程传递（langchain 提交
  callback 时会复制上下文）。

已知边界：

- 图启动阶段的 span（`LangGraph`、节点自己的 span）创建于位置被写入之前，所以**没有**这个
  属性——有它的是**每次 LLM 调用的 span**（也就是排查时真正要看的那层）；
- 纯路由节点（`route_after_*`）不构造模型，自然没有该属性——但它们名字本身就能 grep 到。

## 坑（都踩过）

1. **别全局搜 trace id**：历史消息会把之前调用的 `x-ds-trace-id` 带进下一条
   generation 的 **input**，所以全局搜会命中多条。**只读那一条 generation 的 Output**。
2. **mitmweb 的过滤是客户端行为**（`/flows` API 不支持 filter 参数），只能用 UI 顶部的过滤框：
   `~hs"<id>"` 响应头 / `~hq"x-run-id: <uuid>"` 请求头 / `~bq"页面文本"` 请求体 / `~bs"文本"` 响应体。
3. **mitmweb 的 web UI 有鉴权**：本地固定密码 `agent`，地址 <http://127.0.0.1:8081/?token=agent>；
   不设 `web_password` 时每次启动生成随机 token（打印在任务终端里）。
4. **起服务顺序**：先跑 `proxy: mitmweb` 任务，再用带代理的 launch 配置启动后端；
   否则 LLM 调用会以 `APIConnectionError` 失败（连不上 8082）。
5. **结构化输出（`response_format=json_schema`）拿不到响应头**——langchain 会跳过
   headers 捕获。项目里 classify 用的是 `function_calling`，不受影响。

## 本地环境速查

| | |
| --- | --- |
| Langfuse（自建，已跑了很久） | <http://localhost:3000>，账号在容器 `LANGFUSE_INIT_*` 里 |
| mitmweb | 代理 `127.0.0.1:8082`，UI `127.0.0.1:8081`（密码 `agent`） |
| 前端 / 后端 | 3100 / 2026（`.env` 里 Postgres 5433、Redis 6380，避开 Dify 占用的 5432/6379） |
| VS Code | `proxy: mitmweb` 任务 + `Server (debug breakpoints)` 配置（已内置代理环境变量） |

## 代码在哪（改动集中在三处）

- `src/graphs/shared/models.py`
  - `_correlation_headers()` —— 把当前 run 的 `run_id`/`thread_id` 从 structlog contextvars
    取出来，作为 `default_headers` 发给 provider；
  - `include_response_headers=True` —— 让 langchain 捕获响应头（否则拿不到 `x-ds-trace-id`）；
  - `_TrimResponseHeaders`（callback）—— 把捕获到的十几个响应头**裁到只剩 trace id**，
    避免每条 AI 消息拖 ~700B 进 state / checkpoint。
  - 新增 provider 时，往 `_PROVIDER_TRACE_HEADERS` 里加一行即可；
  - `_stamp_code_location()` —— 给当前 span 打 `code.filepath/lineno/function`，
    让调用链上的节点 span 能跳回代码行。
- `src/agent_server/infra/observability/span_enrichment.py` / `usecase/execution/worker_executor.py`
  —— run 的 ids 就是在这里绑进 contextvars 的（两条执行路径都有）。
- `.env` —— `OTEL_TARGETS=LANGFUSE` + 本机 Langfuse 的 key/base_url。

## 为什么要有这些（一句话）

Langfuse 告诉你**哪一步错了、为什么**；mitmweb 告诉你**SDK 到底发了什么字节**。
两者用同一个 id 串起来，就不用再在两个 UI 之间靠时间猜了。
