# 客户端：自己解析 SSE，不用 EventSource

浏览器的 `EventSource` 只能发 GET，而这里是 POST，所以 SDK **手工解析 SSE 线格式**：
`fetch` 拿响应 → `response.body.getReader()` 逐块读字节 → 累积 buffer → 按**空行**切分事件。

SSE 在网络上就是纯文本：

```
id: 5
event: messages
data: {"type":"messages","data":[{"content":"你好"}]}

id: 6
event: updates
data: {"type":"updates","data":{...}}

```

**两条换行 = 一个事件结束。** SDK 把 `data:` 行 `JSON.parse` 出来再派发出去。

常见的 `event:` 类型：`messages`（token 流）、`updates`（节点状态增量）、
`values`（完整 state 快照）、`metadata`、`interrupt`（HITL 中断）、`end`。

---

[← 返回大纲](README.md)
