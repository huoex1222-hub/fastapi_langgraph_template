# 认证上下文：`request.scope["user"]` 从哪来、为什么这么写

`get_current_user` 里有一行 `user = request.scope.get("user")`（`auth/deps.py:144`）。
它不是中间件写的——本服务**没有安装** Starlette 的 `AuthenticationMiddleware`。
本文把这条链路的写入点、读取点和背后的 ASGI / Starlette / FastAPI 机制讲清楚。

## TL;DR

| 问题 | 答案 |
| --- | --- |
| 谁写 `scope["user"]` | **只有** `require_auth()`，`auth/deps.py:97-98` |
| 谁读 | `get_current_user()`（`deps.py:144`）；以及 Starlette 的 `request.user` / `request.auth` property |
| 为什么用 scope | ASGI 约定：scope 是**每连接一份**的可变 dict，`Request` 只是它的包装 |
| 为什么不读 `request.user` | 没有 `AuthenticationMiddleware` 时它**断言失败**（`AssertionError`），`hasattr` 吞不掉 |
| 为什么读不到要 fallback | FastAPI 不保证 router 级依赖先于端点级依赖执行 |
| 为什么不会重复认证 | 自建的请求级缓存 key `agent_server.auth_result`（`deps.py:12/77/105`） |

## 1. 唯一写入点

grep 全仓，`src/` 里给 scope 写 `user` 的只有一处：

```python
# auth/deps.py:97（在 require_auth 内）
request.scope["user"] = user          # 原始 BaseUser（LangGraphUser，含 handler 返回的额外字段）
request.scope["auth"] = credentials   # starlette AuthCredentials（权限列表）
request.scope[_AUTH_RESULT_SCOPE_KEY] = user_model   # deps.py:105，转换后的 pydantic User
```

其余命中都在测试里（`tests/unit/test_auth/test_deps.py:49`、
`tests/integration/test_auth_flow.py:296`），是手工构造 scope 的 mock。

## 2. 谁可能先跑 `require_auth`

| 入口 | 位置 | 说明 |
| --- | --- | --- |
| 路由级依赖 | `threads.py:51`、`runs.py:40`、`hub/api.py:36` 等 | `APIRouter(dependencies=auth_dependency)`，`auth_dependency = [Depends(require_auth)]`（`deps.py:113`） |
| 声明式 enforcement | `auth/enforcement.py:78-81` | 被 prepend 到路由上，显式 `Depends(require_auth)`（注释说明了为什么不用 `get_current_user`） |
| 自定义路由统一挂载 | `app/main.py:249-257` | `_require_auth_on_http` 包一层，WebSocket 放行 |
| `get_current_user` 自身 | `deps.py:145-146` | scope miss 时直接 `await require_auth(request)` |

但**顺序不可依赖**（见第 6 节），所以 `get_current_user` 的设计是：
"scope 命中就直接转换，miss 就当场认证"。两种情况结果一致。

## 3. 知识点①：ASGI scope 是"每连接一份"的可变字典

ASGI 协议里，server（uvicorn）为每个连接创建 `scope` dict 交给 app；路由匹配往里加
`path_params`，中间件往里加 `state`、`app` 等。Starlette 的 `Request` 只是薄包装：

```python
request.scope is <同一个 dict>   # 所有持有该 scope 的 Request/HTTPConnection 共享
```

因此：

- **依赖里写、路由函数里读**天然成立，不需要显式传递；
- 生命周期止于请求结束，**不会跨请求泄漏**，并发请求各有一份；
- 比全局变量安全，比 `contextvars` 更贴合"每个 HTTP 请求"的语义。

## 4. 知识点②：Starlette 的认证契约

Starlette `AuthenticationMiddleware` 的约定是：调用
`AuthenticationBackend.authenticate(conn)` 拿到 `(AuthCredentials, BaseUser)`，然后写入
`scope["user"]` 和 `scope["auth"]`。读取侧由 `HTTPConnection` 提供两个**只读 property**：

```python
# starlette/requests.py（当前环境 1.3.1）
@property
def user(self) -> Any:
    assert "user" in self.scope, "AuthenticationMiddleware must be installed to access request.user"
    return self.scope["user"]

@property
def auth(self) -> Any:
    assert "auth" in self.scope, "AuthenticationMiddleware must be installed to access request.auth"
    return self.scope["auth"]
```

本仓库的做法（`auth/deps.py:62`）是**用 FastAPI 依赖取代中间件**：

- 依赖可以出现在 OpenAPI 里，也能被 router/endpoint 精细控制；
- 但 key 仍沿用 `"user"` / `"auth"`，保持与 Starlette 生态的互操作——只要
  `require_auth` 跑过，`request.user` / `request.auth` 就能正常工作。

认证后端本身在 `auth/middleware.py`：`LangGraphAuthBackend.authenticate`
（`middleware.py:217`）调用 `langgraph.json` 里 `auth.path` 指向的 handler；
没有配置时返回匿名用户（`middleware.py:233-246`，启动时警告过一次）。
`get_auth_backend()` 用 `lru_cache`（`middleware.py:289`），认证模块每进程只加载一次。

## 5. 知识点③：`request.user` 的两个坑

```python
# auth/deps.py:100-101 —— 死代码
if not hasattr(request, "user"):
    request.user = user
```

1. **读会断言**：没有 `AuthenticationMiddleware` 时 `request.user` 抛 `AssertionError`。
   `hasattr` 只吞 `AttributeError`，所以 `deps.py:144` 必须用 `scope.get("user")` 兜底，
   不能写 `getattr(request, "user", None)`。
2. **写会失败且不可达**：`user` 是只读 property（无 setter）。第 97 行刚设完 scope，
   第 100 行的 `hasattr` 必然为 `True`，分支永远不进入；如果真进入了，
   `request.user = user` 会抛 `AttributeError`。这 3 行可以直接删。

## 6. 知识点④：FastAPI 依赖的缓存与顺序

**缓存**：`Depends` 默认 `use_cache=True`，但缓存 key 是**依赖函数对象**。
`Depends(require_auth)` 和 `Depends(get_current_user)` 是两个不同的 callable，
FastAPI 不会互相复用结果。所以代码自己做了请求级缓存：

```python
# auth/deps.py:12
_AUTH_RESULT_SCOPE_KEY = "agent_server.auth_result"

# deps.py:77-79 —— 命中缓存直接返回
cached = request.scope.get(_AUTH_RESULT_SCOPE_KEY)
if cached is not None:
    return cached
```

这解决两个问题：

- 一个请求里 `router` 级、enforcement、rate limiter（`auth/rate_limit.py:76/81`）
  可能多次触发认证；
- **凭据可能是一次性的**，重复校验会失败——backend 必须每请求只跑一次。
  测试 `test_require_auth_calls_backend_once_per_request`
  （`tests/unit/test_auth/test_deps.py:53`）钉住了这一点。

**顺序**：router 级依赖通常先于端点参数依赖执行，但 FastAPI 没有强契约
（0.137 起 `include_context` 的合并方式就变过，`app/main.py:281-289` 的注释也在防这个）。
因此 `get_current_user` 不能假设 `require_auth` 已经跑过，才有了兜底调用。
这是"**不依赖框架顺序、幂等**"的防御式写法。

**直接调用依赖函数**（`deps.py:146`）是合法的——FastAPI 依赖本质就是普通 async 函数；
代价是绕过了 FastAPI 的缓存，所以 `require_auth` 必须自带 scope 缓存（如上）。

## 7. 知识点⑤：scope / `request.state` / `contextvars` 的分工

| 载体 | 本质 | 适合放什么 |
| --- | --- | --- |
| `scope["user"]` / `["auth"]` | ASGI 约定 key | 认证主体，跨 Starlette 生态互通 |
| `request.state.xxx` | `scope["state"]` 子 dict | 业务自定义的请求态数据 |
| `contextvars` | 进程/任务级 | correlation id、trace 等（如 `CorrelationIdMiddleware`） |

scope 是**无命名空间**的公共字典，第三方中间件也可能往里写 key。所以：
惯用 key（`user`/`auth`）为了兼容照写，**自己的扩展 key 必须带前缀**
（`agent_server.auth_result` 就是范例）。

## 8. 数据形状：`LangGraphUser` → `User`

- `scope["user"]` 存的是 backend 产出的 `LangGraphUser`（`auth/middleware.py:34`），
  它实现 `BaseUser`，并用 `__getattr__` 透传 handler 返回的任意字段
  （`subscription_tier`、`team_id` 之类）。
- `_to_user_model()`（`deps.py:39-59`）把它转成 `domain/user.py` 的 pydantic `User`，
  额外字段靠 `ConfigDict(extra="allow")` 保留；`_extract_user_data` 兼容
  dict / `to_dict()` / `dict()` / 属性兜底四种形状。
- `get_current_user` 在 scope 命中时是**重新转换一次**，而不是返回第 105 行缓存的模型。
  功能等价（结果相同），但读 `agent_server.auth_result` 可以少一次转换——
  只是那样会跳过第 148 行的 `is_authenticated` 检查，改之前要想清楚。

## 9. 边界与注意事项

- **WebSocket 不会自动带上认证**：`_require_auth_on_http` 对 WebSocket 直接放行
  （`app/main.py:249-254`），因为 included router 的依赖会作用到 WS，而 `Request`
  类型的依赖在 WS 下会 `TypeError`。新增 WS 端点时不要假设 `scope["user"]` 存在。
- **匿名模式不是隔离**：没有 `auth.path` 时所有请求共享 `identity="anonymous"`
  （`middleware.py:233-246`），启动日志已警告。
- **不要往 scope 写无前缀的自定义 key**，避免和第三方中间件/Starlette 撞车。
- **不要在依赖外缓存 User**：scope 是请求级的，服务级缓存会跨用户泄漏。

## 10. 排查清单

| 现象 | 先看 |
| --- | --- |
| `AuthenticationMiddleware must be installed...` | 有人读了 `request.user`/`request.auth`，而 `require_auth` 没跑过 → 改用 `scope.get("user")` 或先 `await require_auth` |
| 认证 handler 被调用两次 | 检查是否绕过了 `_AUTH_RESULT_SCOPE_KEY`（例如新建 Request 对象、或跨请求复用 scope） |
| scope 里有 user 但字段缺失 | handler 返回的字段是否被 `_to_user_model` 认为合法；`User.extra="allow"` 是否生效 |
| WS 请求拿不到用户 | 预期行为，见第 9 节 |

## 代码索引

```
src/agent_server/auth/deps.py:12          _AUTH_RESULT_SCOPE_KEY（请求级认证缓存 key）
src/agent_server/auth/deps.py:39-59       _to_user_model / _extract_user_data
src/agent_server/auth/deps.py:62-106      require_auth（唯一写入点：:97-98、缓存：:105）
src/agent_server/auth/deps.py:116-152     get_current_user（读取：:144，fallback：:145-146）
src/agent_server/auth/middleware.py:34-63 LangGraphUser
src/agent_server/auth/middleware.py:217   LangGraphAuthBackend.authenticate
src/agent_server/auth/middleware.py:289   get_auth_backend（lru_cache）
src/agent_server/auth/enforcement.py:72-89 @auth.on 分发依赖（Depends(require_auth)）
src/agent_server/app/main.py:249-257      自定义路由的 auth 挂载（WS 放行）
src/agent_server/app/main.py:368-387      中间件栈（确认没有 AuthenticationMiddleware）
tests/unit/test_auth/test_deps.py:53      每请求只认证一次
tests/unit/test_auth/test_deps.py:149-179 get_current_user 的 scope 命中 / fallback / 401
tests/integration/test_auth_flow.py:296   集成测试手工构造 scope["user"]
```
