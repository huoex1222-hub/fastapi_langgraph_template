# 数据库连接池与 Session 生命周期

大量路由写着 `session: AsyncSession = Depends(get_session)`。本文回答三个问题：
连接是不是共用池？每次请求到底新建了什么？连接什么时候还回去？

## TL;DR

**池共用、Session 每请求一份、连接用完归还（checkin），不是关掉。**

```
AsyncEngine（每进程 1 个，lifespan 里创建）
  └─ QueuePool（pool_size=10 + max_overflow=20）
       └─ AsyncSession（每请求 1 个，Depends 创建）
            └─ Connection（第一次执行 SQL 才 checkout，事务结束/close 时 checkin）
```

## 两个池，两个驱动

`db_manager.initialize()`（`repo/database.py:32-97`）在 lifespan 里各建一次，`close()`
（`database.py:99-113`）在关停时销毁：

| 池 | 驱动 | 用途 | 配置 |
| --- | --- | --- | --- |
| `db_manager.engine` | asyncpg（SQLAlchemy） | 框架元数据表（assistant / thread / runs / crons …） | `pool_size=10`、`max_overflow=20`、`pool_pre_ping=True` |
| `db_manager.lg_pool` | psycopg（`AsyncConnectionPool`） | LangGraph checkpoint / store | `min_size=5`、`max_size=20`，`autocommit=True` |

两个池**不能混用**：`settings.db.database_url` 是 SQLAlchemy 专用的（asyncpg），
`database_url_sync` 是 psycopg 专用的。`prepared_statement_cache_size=0`（asyncpg）和
`prepare_threshold=None`（psycopg）都是为了 PgBouncer 兼容。

池大小在 `config/settings.py:312-319`：

```python
SQLALCHEMY_POOL_SIZE = 10
SQLALCHEMY_MAX_OVERFLOW = 20
LANGGRAPH_MIN_POOL_SIZE = 5
LANGGRAPH_MAX_POOL_SIZE = 20
```

**池是每进程的。** 单个进程的 SQLAlchemy 连接上限 = 10 + 20 = 30。多个 uvicorn
worker 或多个容器时，总上限 ≈ 进程数 × 30，再加每个进程的 LangGraph 池，要对照
Postgres 的 `max_connections` 核算。

## `get_session` 做了什么

`repo/orm.py:285-307`：

```python
def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global async_session_maker
    if async_session_maker is None:
        engine = db_manager.get_engine()
        async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
    return async_session_maker


async def get_session() -> AsyncIterator[AsyncSession]:
    maker = get_session_maker()
    async with maker() as session:
        yield session
```

- `async_sessionmaker` 是**工厂**，本身不持有连接；惰性单例，只绑 engine。
- 每次 `Depends(get_session)` 真正执行时 `maker()` 创建一个新的 `AsyncSession`。
- `async with` 在依赖退出时调用 `session.close()`：回滚未提交事务，把连接还回池。
- 同一个请求内多次 `Depends(get_session)` 拿到的是**同一个 session**——FastAPI 对依赖
  做请求级缓存；跨请求才是新的。

## 连接的借还时机

| 时刻 | 发生什么 |
| --- | --- |
| session 创建 | 不占连接，只创建对象 |
| 第一条 SQL | 从池 **checkout** 一条连接（autobegin 事务） |
| `commit()` / `rollback()` | 事务结束，连接 **checkin** 回池；下一条 SQL 再借 |
| `session.close()` | 回滚未提交事务 + 归还连接 |
| 进程关停 | `engine.dispose()` 关闭池内所有连接 |

细节：

- **只读端点**若不 commit/rollback，连接会一直被该 session 占到依赖 teardown 的
  `close()` 为止。
- **溢出连接**（超过 `pool_size` 的那部分，最多 `max_overflow` 条）在归还时会被直接
  关闭，不回池。
- `pool_pre_ping=True`：借出前做一次轻量探活，被数据库/代理断掉的连接会被替换。
- 没有配 `pool_recycle`，长期空闲连接靠 pre_ping 兜底。
- `pool_timeout` 用默认 30s：池 + overflow 全满时，新的 checkout 等 30s 后抛
  `TimeoutError`。
- `expire_on_commit=False`：commit 后 ORM 对象不过期，响应序列化不会触发额外查询。

## 流式端点为什么不用 `Depends(get_session)`

FastAPI 当前版本（0.141）中，带 `yield` 的依赖默认 `scope="request"`：teardown 在
**响应发送之后**执行。对 `EventSourceResponse` 来说，这意味着整个 SSE 生命周期
（可能几分钟）都结束不了 `async with`。

所以所有 SSE 端点都刻意手动开短会话，例如 `runs.py:166-177`：

```python
maker = _get_session_maker()
async with maker() as session:
    ...  # 读 thread、准备 run
# 出了 async with 才返回 EventSourceResponse
```

`stateless_runs.py`、`event_streaming.py` 同样如此。**流式端点挂 `Depends(get_session)`
会让一条池连接被整条流占住**，高并发 SSE 下很快耗尽 30 条上限。

后台任务同理：不能把请求的 session 传进去（请求结束时它已经被关了），而要自己开
短会话——`thread_naming.py:69`、`thread_ttl.py:364`、`hub/queries.py:23` 都是
`async with get_session_maker()() as session: ...`。

## 常见误区

| 说法 | 实际 |
| --- | --- |
| 「每次 Depends 会新建一个数据库连接」 | 新建的是 Session；连接按需从池借，用完归还 |
| 「连接池是全局共享的」 | 进程内共享；多 worker / 多容器各有一份 |
| 「session.close() 会断开连接」 | 归还到池，TCP 连接保留复用；只有 overflow 连接会被关闭 |
| 「流式接口也可以用 Depends(get_session)」 | 会把连接占用整个流的时长，应手动短会话 |
| 「LangGraph 和元数据表共用一个池」 | 两个池、两个驱动，互不相干 |

## 相关代码位置

| 内容 | 位置 |
| --- | --- |
| 池创建 / 销毁 | `repo/database.py:32-113` |
| Session 工厂 / 依赖 | `repo/orm.py:285-307` |
| 池大小配置 | `config/settings.py:312-319` |
| lifespan 中的初始化 | `app/main.py:132`（初始化）、`:199`（关闭） |
| 流式端点的手动短会话 | `controller/http/routers/runs.py:166-177` |
| 后台任务的手动短会话 | `usecase/thread_naming.py:69`、`usecase/thread_ttl.py:364` |
