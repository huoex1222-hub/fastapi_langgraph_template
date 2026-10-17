# 坑

## 1. 别把 `GET /state` 当成「拿回答」

SDK 里的顺序是**先 getState、再 stream**：

```js
let m = await this.getOrCreateThread(u, ...)              // 1. 拿/建 thread
let h = await this.client.threads.getState(m.thread_id)   // 2. ← GET /state
let g = h.values.messages ?? []                           //    捞【已有】消息
let y = this.langGraphDefaultMergeState({...i, messages: g}, _, t)
...
this.client.runs.stream(u, this.assistant.assistant_id, W) // 3. ← 才发 run
```

`getState` 的另两个调用点也跟「回答」无关：一个查 `interrupts`（HITL 恢复），
一个发 `STATE_SNAPSHOT`（刷新页面恢复现场）。

## 2. 长连接的 keepalive

`KEEPALIVE_INTERVAL_SECS` 会周期性发 SSE 注释行，防止空闲代理把
长时间没输出的节点（比如握着上游 WebSocket 的 agent）的连接掐掉。

## 3. 流中途出错不会返回 500

```python
# streaming_service.py:117-125
except Exception as e:
    logger.exception("stream execution failed", run_id=run_id)
    yield create_error_event({"error": type(e).__name__, "message": "execution failed"})
```

连接已经建立了，再抛 500 客户端没法重连。所以错误**以事件形式**发出去，
且 `str(e)` 被刻意丢弃（异常消息可能泄露文件路径和内部 ID）。

## 4. 发消息后页面闪一下

结构上的可疑点（`page.tsx`）：

- **`Home` 顶层挂了 `useAgent`**（`page.tsx:138`）—— 任何 state 变化都重渲染整个页面
  （header / hero / 正文 / 侧边栏全在内），流式期间每秒变好几次；
- **`Presentations` 用数组下标当 key**（`page.tsx:65` 的 `key={i}`）——
  列表一变 React 卸载重建而不是更新，视觉上就是闪；
- **`getState` 回来后 `mergeState` 替换了消息数组** —— 消息列表整个重挂一次。
  如果是这条，闪的时机应该**固定在回答出现之前**。

**怎么确认**：React DevTools 勾上 "Highlight updates when components render"，
再发一句看是哪块在闪；或看 Network 里 `GET /state` 返回的瞬间是否正好是闪烁点。

## 5. `web_search` 是模拟的，不走网络

`src/graphs/shared/tools/web_search.py` 目前是占位实现，返回
`"Simulated result N for '<query>'"`，**不发任何 HTTP 请求**。所以：

- mitmweb 里看不到它（没流量）
- research_agent 的「研究」是在占位文本上做的推理

换真实实现时（Tavily/Exa/SerpAPI），**要确认所选 SDK 的 HTTP 层认 `HTTPS_PROXY`**，
否则流量绕开 mitmweb 而且**不报错**：`httpx`/`requests`/`urllib` 默认认，
`aiohttp` 需要显式 `trust_env=True`。

## 6. mitmweb 里只有 LLM 调用

| 流量 | 走向 | mitmweb |
| --- | --- | --- |
| LLM 调用（`api.deepseek.com`） | httpx + `HTTPS_PROXY` | ✅ |
| 浏览器 → agent server | 浏览器直连 `:2026` | ❌ 不走代理 |
| `web_search` | 本地函数 | ❌ |
| MCP 工具（`acme-kb_*`） | stdio 子进程 | ❌ 不是 HTTP |
| Langfuse OTLP 上报 | `NO_PROXY=localhost,127.0.0.1` 排除 | ❌ |

---

[← 返回大纲](README.md)
