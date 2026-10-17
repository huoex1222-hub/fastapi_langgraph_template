# 限流：按身份分桶、两档、默认关闭

基于 **`limits`** 库（flask-limiter / slowapi 的底层引擎）的每身份限流，
用 FastAPI 依赖实现，没有装饰器魔法。

## TL;DR

| 设计 | 做法 |
| --- | --- |
| **分桶键** | 登录用户 → `user:{identity}`；匿名 → `ip:{client.host}` |
| **算法** | `MovingWindowRateLimiter`（滑动窗口） |
| **存储** | `REDIS_BROKER_ENABLED=true` → Redis（多副本共享）；否则内存 |
| **两档** | `RATE_LIMIT_DEFAULT=200/minute`、`RATE_LIMIT_RUNS=30/minute` |
| **超限** | 429，body 走 Agent Protocol 形状（`error: rate_limited`），带 `Retry-After` header |
| **默认** | **关闭** —— `RATE_LIMIT_ENABLED=false` |

## 代码位置

```
src/agent_server/auth/rate_limit.py        实现（84 行）
src/agent_server/app/main.py:401-411       接线
src/agent_server/config/settings.py:111-115 配置
tests/unit/test_auth/test_rate_limit.py     5 个测试
```

核心就两个依赖函数：

```python
# auth/rate_limit.py:76
async def rate_limit_default(request, user=Depends(get_current_user)) -> None:
    """Generous tier for ordinary endpoints (`RATE_LIMIT_DEFAULT`)."""
    await _enforce(request, user, "default", settings.app.RATE_LIMIT_DEFAULT)


# auth/rate_limit.py:81
async def rate_limit_runs(request, user=Depends(get_current_user)) -> None:
    """Tighter tier for run/streaming endpoints (`RATE_LIMIT_RUNS`)."""
    await _enforce(request, user, "runs", settings.app.RATE_LIMIT_RUNS)
```

`_enforce` 做的事很少：检查开关 → 算 key → `limiter.hit()` → 超了就抛 429。

```python
# auth/rate_limit.py:58
async def _enforce(request, user, tier, limit_expr) -> None:
    if not settings.app.RATE_LIMIT_ENABLED:
        return
    key = (
        f"user:{user.identity}"
        if user.identity != "anonymous"
        else f"ip:{request.client.host if request.client else 'unknown'}"
    )
    limit = parse(limit_expr)
    if not _get_limiter().hit(limit, tier, key):
        logger.warning("rate_limit_exceeded", tier=tier, key=key, path=request.url.path)
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Retry later.",
            headers={"Retry-After": str(int(limit.get_expiry()))},
        )
```

429 会经过全局 `HTTPException` handler，转成 Agent Protocol 的错误形状
（`app/main.py:227` 的 `exception_handlers`）。

## 接线：谁用哪一档

```python
# app/main.py:404
app.include_router(health_router)  # 不限流
app.include_router(assistants_router, dependencies=[Depends(rate_limit_default)])
app.include_router(threads_router, dependencies=[Depends(rate_limit_default)])
app.include_router(runs_router, dependencies=[Depends(rate_limit_runs)])  # 紧
app.include_router(stateless_runs_router, dependencies=[Depends(rate_limit_runs)])  # 紧
app.include_router(crons_router, dependencies=[Depends(rate_limit_default)])
app.include_router(store_router, dependencies=[Depends(rate_limit_default)])
app.include_router(event_streaming_router, dependencies=[Depends(rate_limit_runs)])  # 紧
```

**为什么 runs 档更紧**（`main.py:399` 的注释）：

> runs/streaming endpoints hold LLM connections open and get the tighter tier;
> everything else gets the generous default. Health and probes are exempt.

## 配置与当前状态

```python
# config/settings.py:113
RATE_LIMIT_ENABLED: bool = False
RATE_LIMIT_DEFAULT: str = "200/minute"
RATE_LIMIT_RUNS: str = "30/minute"
```

`.env` / `.env.example` 里三行**都是注释掉的**：

```ini
# RATE_LIMIT_ENABLED=true
# RATE_LIMIT_DEFAULT=200/minute
# RATE_LIMIT_RUNS=30/minute
```

所以默认部署下**完全不生效**。

## 测试覆盖

```
test_allows_within_limit
test_rejects_beyond_limit_with_retry_after
test_identities_are_isolated          ← 一个用户刷爆不连累别人
test_runs_tier_is_tighter
test_disabled_is_passthrough
```

## 坑 / 注意

### 1. 内存模式下限额是「每进程」的

`_get_limiter()` 在没开 Redis broker 时用 `MemoryStorage`。多 worker 或多副本时，
每个进程各记各的计数，**实际限额被放大 N 倍**。

内存模式对应的是单实例开发，生产该开 `REDIS_BROKER_ENABLED=true` 才会共享计数。

### 2. Redis 运行时挂掉可能直接 500，而不是降级

`_get_limiter()` 的 `try/except` 包的是**构造**：

```python
# auth/rate_limit.py:37-48
if settings.redis.REDIS_BROKER_ENABLED:
    try:
        storage = storage_from_string(settings.redis.REDIS_URL, key_prefix="RATE_LIMIT")
        _limiter = MovingWindowRateLimiter(storage)
        return _limiter
    except Exception as e:
        logger.warning("rate_limit_redis_unavailable_falling_back_to_memory", error=str(e))
_limiter = MovingWindowRateLimiter(_memory_storage())
```

而 `limits` 的存储通常是**懒连接** —— 构造时不报错，真正 `hit()` 时才连 Redis。而
`_enforce` 里：

```python
if not _get_limiter().hit(limit, tier, key):     # ← 没有 try/except
```

所以 Redis 运行时不可用会抛异常 → 500，走不到那条 `falling_back_to_memory`。

> **这条是读代码推断的，没实测。**

另外一旦降级到内存，`_limiter` 这个模块级单例就**永远**是内存了 —— 后续 Redis 恢复也不会切回去。

### 3. 自定义 app 的路由不参与限流

`main.py:453` 对 custom routes（`http_app.py` 挂的 shop/ml/hub）只做了**认证**：

```python
if http_config and http_config.get("enable_custom_route_auth", False):
    _apply_auth_to_custom_routes(application, CUSTOM_ROUTE_AUTH)  # 只有 auth
```

限流是随 `_include_core_routers` 挂上去的，所以 `/shop/products`、`/ml/*`、`/hub/*`
**没有限流**。前端每次进页面都会拉 `/shop/products`，算一个现成的缺口。

### 4. SSE 的限流只在连接建立时检查一次

`dependencies=` 在请求开始时执行一次。`runs/stream` 是长连接，流式期间不会重复计数
—— 这是合理的设计（否则一个长流会被反复计数），但意味着一个已建立的流可以一直占着。

### 5. `Retry-After` 是窗口长度，不是精确剩余时间

```python
headers = {"Retry-After": str(int(limit.get_expiry()))}
```

对 `200/minute` 就是固定 60 秒，即使用户其实再过 2 秒就滑出窗口了。

### 6. 自定义错误 handler 会重建响应 —— header 必须显式转发

`agent_protocol_exception_handler`（`main.py:203`）不是「改一下 body」，而是**重新构造
一个 `JSONResponse`**。所以 raise 处放在 `HTTPException(headers=...)` 上的东西
（比如限流的 `Retry-After`）必须手动转发，否则**静默丢失**：

```python
return JSONResponse(
    status_code=exc.status_code,
    content=...,
    headers=exc.headers,  # ← 少了这行，Retry-After 就到了不了浏览器
)
```

同一条链上还有第二个坑：`get_error_type()` 的映射表原先没有 429，限流错误返回的是
`"error": "unknown_error"`。已补 `429: "rate_limited"`。

**为什么单测没抓到**：原来的 `test_rejects_beyond_limit_with_retry_after` 直接调
`rate_limit_default()`、断言**抛出的异常对象**上有 header —— 从没经过 FastAPI 的 HTTP 层。
现在补了一个走 `TestClient` 的用例，断言的是**真实响应**的 header 和 body。

> 教训：断言异常对象 ≠ 断言 HTTP 响应。凡是"handler 会重建响应"的地方，
> 单测都得穿过框架边界。
