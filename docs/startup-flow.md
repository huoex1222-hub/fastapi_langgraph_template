# 服务启动过程：从 `make run` 到 `/health` 返回 healthy

一条链路从进程入口走到能接请求，中间有三个**性质完全不同**的阶段。分清楚它们，才能
在"服务起不来"或"断点不生效"时知道该看哪儿。

## TL;DR

```
阶段 1  模块导入期   app = create_app()   ← main.py:493，一行在模块底部，导入即执行
                     └─ 载入 http_app.py，把核心路由/中间件/异常处理器挂上去

阶段 2  core lifespan  ← main.py:97，merge_lifespans 保证 core 先跑、user 后跑
                     └─ 迁移 → 连接池 → 观测 → 图 → broker → executor → cron → TTL
        user lifespan  ← http_app.py:28
                     └─ hub 业务迁移 → ML 模型载入
                     ↓ yield（此时才打印 Application startup complete）

阶段 3  关停          严格逆序：ttl → cron → reaper → executor(排空) → broker → Redis → DB
```

**阶段 1 失败 = 进程根本起不来**（导入异常，栈是 Python 的）。
**阶段 2 失败 = 卡在 `Waiting for application startup.` 之后**（这才是排查启动问题的战场）。

## 三种起法

| 命令 | 做什么 | Redis / executor |
| --- | --- | --- |
| `make deps` | 只起 Postgres + Redis 容器，服务在本地跑 | — |
| `make run` | 本地 uvicorn 热重载（`Makefile:112`） | 按 `.env`，本机是关 |
| `make dev` | 全容器 + `docker-compose.dev.yml` 覆盖 | `REDIS_BROKER_ENABLED=false` → LocalExecutor |
| `make up` | 全容器，完整栈 | `REDIS_BROKER_ENABLED=true` → WorkerExecutor |

`make run` 的真实命令：

```
uv run uvicorn --env-file .env agent_server.app.main:app --reload --host 0.0.0.0 --port 2026
```

## 阶段 1：模块导入期

`main.py:493` 一行 `app = create_app()`，**导入这个模块就会执行**。比它更早的只有
`main.py:74` 的 `setup_logging()`（模块顶层）。

`create_app()`（`main.py:421`）的顺序：

1. `load_http_config()` 读 `langgraph.json` 的 `http` 段
2. 看到 `http.app = "./src/http_app.py:app"` → `load_custom_app()` 动态导入
   - 先把 `dependencies`（`./src/graphs`、`./src`）塞进 `sys.path`，否则用户包 import 不到
   - 拿到的对象**必须是 FastAPI 实例**，否则直接 `TypeError`
3. **以这个自定义 app 为主体**（不是新建 FastAPI），核心往外挂：
   - `_apply_auth_to_custom_routes()` —— 给 shop/ml/hub 的路由前置认证依赖
   - `_include_core_routers()` —— health / assistants / threads / runs / stateless_runs /
     crons / store / event_streaming（各自带限流依赖；`ENV_MODE=LOCAL` 时额外挂 `/mcp` 调试路由）
   - `merge_lifespans()`（`route_merger.py:12`）—— 核心 lifespan **包住** http_app 的 lifespan
   - `merge_exception_handlers()`、`_add_common_middleware()`
4. `setup_prometheus_metrics()`

### 中间件实际执行顺序

注册顺序和**执行顺序相反**（Starlette：`add_middleware` 往 `user_middleware[0]` 插，
最后注册的在最外层）：

```
RequestSizeLimit   ← 最外层，先拒超大 body
ContentTypeFix
SecurityHeaders
CORS
CorrelationId
StructLog          ← 最内层，此时才有 correlation id 可打
```

> `main.py:366` 的函数 docstring 写的是 "ContentTypeFixMiddleware (outermost)"，**与代码实际行为
> 不符**（`RequestSizeLimitMiddleware` 是最后注册的，也就是最外层）。以 `main.py:380` 那行行内
> 注释为准。

## 阶段 2：lifespan

`merge_lifespans` 的关键作用：**core 先跑、user 后跑，退出时天然逆序**。这也是
**两条 alembic 链顺序的保证** —— 框架链在 core、hub 链在 user。

core lifespan（`main.py:98`）顺序：

```
get_default_durability()        ← 值非法直接启动失败，不拖到每个 run 才炸
配置 MCP 工具授权拦截器 + policy engine（OPA_URL 非空则换 OPA）
run_migrations_async()          ← main.py:123  框架 alembic 链
db_manager.initialize()         ← 双连接池：SQLAlchemy/asyncpg + LangGraph/psycopg
setup_observability()
langgraph_service.initialize()  ← main.py:142  加载 langgraph.json 里的图
redis_manager.initialize()      ← 仅 REDIS_BROKER_ENABLED=true
broker_manager.start()
executor.start()                ← 按配置选 LocalExecutor / WorkerExecutor
lease_reaper.start()            ← 仅 Redis 模式
cron_scheduler.start()          ← CRON_ENABLED=true
thread_ttl_sweeper.start()
        ↓ yield
```

`db_manager.initialize()` 建的两个池、Session 与连接的借还时机，见
[数据库连接池与 Session 生命周期](database-session-pooling.md)。

executor 的选择是**导入期**就定死的（`usecase/execution/executor.py:17-29`，模块级
`executor: BaseExecutor = _create_executor()`）。

user lifespan（`http_app.py:28`）紧随其后：

```
run_hub_migrations_if_needed()  ← hub 自己的 alembic 链；asyncio.to_thread 跑，
                                  因为 alembic env.py 自带事件循环
ml_service.load_model()
```

## 阶段 3：关停

```
ttl sweeper → cron → reaper → executor(排空在跑的任务) → broker → Redis → DB
之后 user lifespan 退出：ml_service.unload_model()
```

## 本机实际配置（`.env`）

| 键 | 值 | 含义 |
| --- | --- | --- |
| `ENV_MODE` | `LOCAL` | 人类可读日志 + 挂 `/mcp` 调试路由（`PRODUCTION` 输出 JSON 且不挂） |
| `REDIS_BROKER_ENABLED` | `false` | → LocalExecutor（`asyncio.create_task`），**无崩溃恢复、不能水平扩展** |
| `RUN_MIGRATIONS_ON_STARTUP` | 未设 → 默认 `True`（`settings.py:109`） | 每次启动自动迁移；多副本时设 `false` + `make migrate-up` |
| `POSTGRES_PORT` / `REDIS_PUBLISH_PORT` | `5433` / `6380` | 避让 Dify 占用的 5432/6379 |
| `CRON_ENABLED` | `true` | 启动时起 cron 调度器 |
| `OPA_URL` | 空 | 用本地 policy engine，不起 OPA sidecar |

正常情况下启动耗时约 **5 秒**，`/health` 应返回：

```json
{"status":"healthy","database":"connected","langgraph_checkpointer":"connected","langgraph_store":"connected"}
```

## 坑

### 1. Postgres 不可达时，启动**永久挂死**，而且会吞掉断点

这是最阴的一个，因为它看起来像"调试器坏了"。

现象：

- 日志停在第 `Waiting for application startup.` 之后，**再无任何输出**
- F5 用 `Server (debug breakpoints)`（不带 `--reload`），**`main.py:123` 之后的断点一个都不停**

原因链：

```
lifespan → await run_migrations_async()          main.py:123
         → asyncio.to_thread(run_migrations_if_needed)
         → _is_database_up_to_date()
         → psycopg.connect()                     migrations.py:84   ← 永远不返回
```

**关键在于 `psycopg.connect()` 对"端口无监听"不是报错，是挂死。** 本机实测：

| 场景 | 结果 |
| --- | --- |
| 阻塞式 socket → 端口无监听 | `ConnectionRefusedError`，约 2.0 秒 |
| libpq 非阻塞 connect → 端口无监听 | **永不返回**（90 秒以上仍无结果） |
| libpq → 有真实监听器 | **0.5 秒连上** |

libpq 的网络栈本身没问题，只是**收不到那个 RST**；而 DSN 里没有 `connect_timeout`，
libpq 默认无限等待。（已确认与 Docker/WSL 状态无关 —— 两者完全关闭时照样复现。）

两个后果：

- `main.py:124` 那段 `except (ConnectionRefusedError, OSError)` → `_log_connection_help()` 的
  友好提示**永远够不着**，因为这个失败模式根本不抛异常；
- `run_migrations_if_needed` 里那个 `except Exception` 兜底（`migrations.py:115`）同样够不着。

**处理**：修环境而不是修调试配置 —— `make deps` 或重启 Docker Desktop。

**已知缺陷（待改）**：给 DSN 加 `connect_timeout`（新配置项拼进 `database_url` /
`database_url_sync`），或在 lifespan 里给迁移套 `asyncio.wait_for(..., timeout=30)`。

### 2. "启动卡住了"怎么定位到具体那一帧

不用调试器也能拿到挂死位置 —— `faulthandler` 定时 dump 所有线程栈：

```python
import faulthandler

faulthandler.dump_traceback_later(20, exit=True)  # 20 秒后打印全部线程栈并退出

from uvicorn.config import Config
from uvicorn.server import Server

Server(
    Config(
        "agent_server.app.main:app",
        host="127.0.0.1",
        port=2026,
        env_file=".env",
        loop="agent_server.infra.event_loop:selector_loop_factory",
    )
).run()
```

上一条坑的堆栈就是这么抓到的（主线程停在 asyncio `_run_once → select`，工作线程停在
`psycopg/waiting.py:wait_conn`）。

### 3. 管道会吞掉启动日志

把 uvicorn 的输出接到 `| head` 之类的管道里时，Python 是**块缓冲**的；进程被 SIGTERM 杀掉时
缓冲区从不 flush，结果就是"跑了 45 秒一条日志都没有"。要么加 `-u`，要么重定向到文件再读。

### 4. `--reload` 会让断点不绑定

uvicorn 的 reloader 是"父进程监视 + 子进程跑 app"，debugpy attach 在父进程上，
所以子进程里的断点全都不生效。`.vscode/launch.json` 里为此分了两个配置：

- `Server (hot reload)` —— 日常用，热重载，**断点不可靠**
- `Server (debug breakpoints)` —— 不带 `--reload`，**断点可靠**，改完代码手动重启（Ctrl+Shift+F5）

`make run`（`Makefile:112`）、`docker-compose.yml:52`、`tasks.json` 的 Ctrl+Shift+B
**全都带 `--reload`**，所以调试时不要走这些入口。

### 5. Windows：不带 `--reload` 必须显式指定 selector 事件循环

不带 `--reload` 时 uvicorn 在 Windows 上会选 `ProactorEventLoop`，psycopg 连接池直接拒绝
（"Psycopg cannot use the 'ProactorEventLoop'"）。项目自带这个工厂，加上即可：

```
--loop agent_server.infra.event_loop:selector_loop_factory
```

`make run` 因为带 `--reload`（隐含 selector loop）而恰好不需要，所以这个坑只在裸跑时出现。

### 6. 图的改动要重启才生效

`langgraph_service.initialize()` 在 lifespan 里一次性加载 `langgraph.json` 里的图，
热重载不会重新加载图。
