# 响应头分层

`POST /threads/{id}/runs/stream` 的响应头，按「为什么存在」可以分三层 —— 只有最外一层
是为浏览器 SDK 服务的：

| 头 | 性质 | 跟 SDK 有关吗 |
| --- | --- | --- |
| `Content-Type: text/event-stream` | W3C SSE 标准 | ❌ |
| `Cache-Control: no-cache` | 标准 | ❌ |
| `Connection: keep-alive` | 标准 | ❌ |
| `X-Accel-Buffering: no` | nginx 部署相关（关掉代理缓冲） | ❌ |
| `Location` / `Content-Location` | 标准 HTTP 头，用法是 Agent Protocol 约定 | ⚠️ |
| `Access-Control-Allow-Origin: *` | CORS | ⚠️ 浏览器才需要 |
| `Access-Control-Allow-Headers: Last-Event-ID` | CORS | ✅ 为让 JS 能带重连头 |
| `expose_headers = [Content-Location, Location]` | CORS 暴露 | ✅ 为让 JS 能读这两个头 |

**SSE 主体是纯标准** —— 任何 SSE 客户端（`EventSource`、curl、Python）都能消费，不是为
某个 SDK 定制的。

**真正「为前端 SDK 而设」的只有 CORS 那一层**：

```python
# main.py
# Default CORS headers required for LangGraph SDK stream reconnection
DEFAULT_EXPOSE_HEADERS = ["Content-Location", "Location"]
```

```python
# infra/sse.py:72
"Access-Control-Allow-Headers": "Last-Event-ID",
```

浏览器跨域时，默认**读不到**非 `expose_headers` 的响应头、**发不出**非 `allow_headers`
的请求头。换成 curl 或服务端客户端，这两条根本不需要。

## 服务端是「按协议给」，不是「按这个前端给」

`Content-Location` 这条通道，`@ag-ui/langgraph` **压根没用**（它等流事件，见「取消」一节）。
这些头服务于 Agent Protocol 的其它客户端 —— LangGraph Studio、Agent Chat UI、其它 SDK 用法。

## 缺口：这些头没写进 OpenAPI

```json
"responses": { "200": { ... } }      // docs/openapi.json 里没有 "headers" 字段
```

依赖 `Content-Location` 拿 run_id 的客户端，依赖的是**未文档化的行为**。对这个仓库有点
矛盾 —— 它有一套很严的 `make openapi-check` 漂移门禁，但这两个头在契约里是隐形的。

> 一句话：**协议优先（Agent Protocol 兼容），SDK 便利性其次（CORS 暴露）。**

---

[← 返回大纲](README.md)
