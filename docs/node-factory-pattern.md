# 节点工厂与闭包：图里的依赖注入

`make_shop_node(tools)` 返回 `shop` 函数，为什么不能直接写 `async def shop(...)`？
本文解释这个模式的约束、目的和边界——它同样适用于工具（`make_shop_tools`）和子图。

## TL;DR

| 问题 | 答案 |
| --- | --- |
| 为什么用工厂 + 闭包 | LangGraph 节点签名固定为 `(state, runtime)`，额外依赖没地方传 |
| 闭包绑定什么 | **构造期依赖**：backend、工具列表、组件声明 |
| 运行期依赖放哪 | `runtime.context` / `runtime.store` / `config`（`get_runtime()`） |
| 为什么不用模块级单例 | backend 由部署/测试决定；import 期不应构造客户端 |
| 为什么不用 `partial` / 类 | 闭包样板最少、类型最直观；工厂还能做前置装配 |
| 谁必须共享同一份对象 | `make_shop_node(tools)` 和 `ToolNode(tools)` 的工具列表必须同一份 |

## 1. 根本约束：节点签名是固定的

LangGraph 只按这几种形态调用节点：

```python
async def node(state, runtime) -> dict
async def node(state, config) -> dict
async def node(state, config, runtime) -> dict
```

`tools`、`backend` 这类依赖**不可能作为参数传进来**。所以它们必须在图构建时
（`builder.add_node(...)` 之前）先绑定好。可选手段只有三种：

| 手段 | 写法 | 取舍 |
| --- | --- | --- |
| **闭包** | `make_shop_node(tools)` 返回内层函数 | 项目选择：样板最少、函数名清晰 |
| `functools.partial` | `partial(shop_impl, tools=tools)` | 可行，但签名/类型提示更绕 |
| 可调用对象 | `class ShopNode: def __call__(...)` | 为一个无状态节点引入类，偏重 |

## 2. 依赖从哪来：一条显式的注入链

以 shopping 为例（`shopping_agent/graph.py:10` → `builder.py:24-37`）：

```
get_backend()                          # 部署环境决定具体后端
  └─ build_shopping_agent(backend)
       ├─ make_shop_tools(backend)     # tools.py:40，工具闭包住 backend
       ├─ make_shop_node(tools)        # nodes.py:80，节点闭包住工具列表
       └─ ToolNode(tools)              # 执行工具的节点必须拿到同一份列表
```

节点本身（`nodes.py:80-105`）只做两件事：把工具绑到模型、调模型：

```python
def make_shop_node(tools: list) -> Callable[[State, Runtime[Context]], Coroutine[Any, Any, dict[str, Any]]]:
    async def shop(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
        model = load_chat_model_with_fallbacks(
            runtime.context.model, runtime.context.fallback_models
        ).bind_tools(tools)                       # ← 闭包里的 tools
        system = SHOP_SYSTEM_PROMPT.format(...)
        response = await model.ainvoke([SystemMessage(system), *state.messages])
        ...
    return shop
```

**关键不变量**：`make_shop_node(tools)` 和 `ToolNode(tools)` 必须是同一份工具对象。
模型用 `bind_tools(tools)` 生成的 tool_calls，最终由 `ToolNode` 执行；两份列表不一致
就会出现"模型能调用、执行时找不到工具"。显式传参让这个关系写在构造处，
而不是靠两个模块 import 同一个全局。

工具层是同一个模式（`tools.py:1-2`、`tools.py:40-41`）：

```python
def make_shop_tools(backend: ShopBackend) -> list:
    """Build the tool list with the backend bound via closure."""
    @tool
    async def search_products(query: str, ...) -> Command:
        products = await backend.search_products(query)   # ← 闭包里的 backend
        ...
```

## 3. 哪些用工厂，哪些不用

`builder.py:28-34` 的对比就是信号——**有构造期依赖才用工厂**：

| 工厂 | 闭包住什么 | 用途 |
| --- | --- | --- |
| `make_shop_node(tools)` | 工具列表 | `model.bind_tools(tools)` |
| `make_policy_node(backend)` | `ShopBackend` | `backend.search_policies()` |
| `make_fetch_orders_node(backend)` | `ShopBackend` | `backend.get_orders()` |
| `make_merchant_node(tools)` | 工具列表 | 同 shop |
| `make_shop_tools(backend)` | `ShopBackend` | 每个工具调后端 |
| `make_presentation_tool(spec, backend=...)` | 组件声明 + backend | 校验、富化、写 `presentations` |

无依赖的节点直接注册为普通函数：

```python
builder.add_node("initialize", cast("Any", initialize))       # 依赖来自 runtime.store / config
builder.add_node("classify", cast("Any", classify))           # 依赖来自 runtime.context
builder.add_node("chat", cast("Any", chat))
builder.add_node("extract_memory", cast("Any", extract_memory))
```

它们的依赖都能从 `runtime` / `config` / `get_runtime(Context)` 在**调用期**拿到，
所以不需要闭包。

## 4. 构造期依赖 vs 运行期依赖

这是这套模式的分界线：

| 依赖 | 何时确定 | 怎么拿 |
| --- | --- | --- |
| backend、工具列表、组件 spec | **图构建时**（启动或每请求工厂） | 闭包 |
| model / 限额 / 开关 | 每次 run | `runtime.context` |
| store（长期记忆） | 每次 run | `runtime.store` |
| user_id、thread_id | 每次 run | `config["configurable"]` |
| auth 上下文 | 每次 run | `get_auth_ctx()` |

工具里两种都用：`remember_preference` 通过 `get_runtime(Context)` 拿开关和 store
（`tools.py:119-125`），同时仍然闭包着构建期的依赖。

## 5. 为什么不用模块级单例

```python
# 反例：不要这样
from shop.backends import get_backend
_backend = get_backend()
```

1. **backend 是部署/环境决定的**，不该硬编码进节点模块；
2. **import 期产生副作用**（建连接、读配置），不利于测试和冷启动；
3. **测试无法替换**：只能 monkeypatch 模块全局，容易串味；
4. 违反 `graphs/README.md:122-124` 的规则：*"Tools/nodes receive dependencies by
   injection (builder closures), not by importing concrete backends or constructing
   clients."*

## 6. 测试直接受益

工厂让节点测试不需要任何全局替换（`tests/graphs/shopping_agent/test_nodes_shop.py:29-36`）：

```python
node = make_shop_node([])          # 传空工具列表即可测节点逻辑
result = await node(State(messages=[...]), _runtime())
```

只需 monkeypatch 模型加载器；子图节点同理
（`tests/graphs/shopping_agent/test_order_agent.py:16-24` 传 `FakeShopBackend()`）。

## 7. 与"图工厂"是两层 DI

不要混淆两种工厂：

| 层 | 例子 | 绑定粒度 |
| --- | --- | --- |
| **节点/工具工厂** | `make_shop_node(tools)` | 图构建期，绑定 backend / tools |
| **图工厂** | `def graph(runtime: ServerRuntime[T])` | 每请求（可选），绑定 user / config / context |

`shopping_agent` 用静态图（`graph.py:10`），节点工厂只在启动时跑一次；
如果改成 `graph(runtime: ServerRuntime[T])` 形式（`graphs/README.md:64-70`），
节点工厂可以按用户/租户重新装配。两种情况下，节点本身的写法都不变。

## 8. 新增节点/工具时的规则

1. 有构造期依赖 → 写 `make_xxx(...)`，返回满足 LangGraph 签名的内层函数。
2. 依赖从 `runtime` / `config` / `get_runtime()` 能拿到 → 直接写普通函数。
3. 工厂只做装配，不写业务逻辑（业务逻辑在节点/工具内部）。
4. 工具列表只构造一次，同时传给 `make_*_node(tools)` 和 `ToolNode(tools)`。
5. 节点工厂返回类型的边界用 `cast("Any", ...)` 处理 LangGraph 的类型重载缺口
   （见 `builder.py:28-34`），不要为此改节点签名。

## 代码索引

```
src/graphs/shopping_agent/nodes.py:80-105      make_shop_node（闭包 tools）
src/graphs/shopping_agent/nodes.py:108-121     make_policy_node（闭包 backend）
src/graphs/merchant_agent/nodes.py:17-38       make_merchant_node
src/graphs/shopping_agent/subgraphs/order_agent/nodes.py:26-36  make_fetch_orders_node
src/graphs/shopping_agent/tools.py:40-41       make_shop_tools（闭包 backend）
src/graphs/shared/presentation.py:124-160      make_presentation_tool
src/graphs/research_agent/tools.py:30-35       make_plan_tool
src/graphs/shopping_agent/builder.py:24-37     注入链与 add_node
src/graphs/shopping_agent/graph.py:10          静态图入口：build_shopping_agent(get_backend())
src/graphs/merchant_agent/builder.py:18-29     merchant 注入链
src/graphs/shopping_agent/subgraphs/order_agent/builder.py:13-22  子图装配
src/graphs/README.md:64-70                     graph.py 的三种入口形态
src/graphs/README.md:122-124                   依赖注入规则
tests/graphs/shopping_agent/test_nodes_shop.py:29-36   工厂让测试无需全局 mock
tests/graphs/shopping_agent/test_order_agent.py:16-24  注入 FakeShopBackend
```
