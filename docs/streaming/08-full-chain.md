# 完整链路

从点击发送到事件回到浏览器，中途经过的**每一个代码位置**：

```
浏览器                        agent server                            executor task
   │                              │                                        │
   ├─ POST /threads ─────────────→│                                        │
   │←──── thread_id ──────────────┤                                        │
   │                              │                                        │
   ├─ GET /threads/{id}/state ───→│  (捞历史消息)                           │
   │←──── messages ───────────────┤                                        │
   │                              │                                        │
   ├─ POST .../runs/stream ──────→│                                        │
   │                              ├─ _prepare_run       run_preparation.py:249
   │                              │   └─ executor.submit(job)      :369 ───→│ 起 task
   │                              │                                        │ local_executor.py:35
   │                              │                                        ├─ execute_run(job)
   │                              │                                        │  run_executor.py:58
   │                              │                                        ├─ _stream_graph(job)
   │                              │                                        │  run_executor.py:172
   │                              │                                        ├─ stream_graph_events(...)
   │                              │                                        │  run_executor.py:209
   │                              │                                        └─ graph.astream(...)   ★ 真正跑图
   │                              │                                           graph_streaming.py:297
   │                              │                               事件 ←────┘
   │                              │←──── 写进 broker ──────────────────────┤
   │←──── event: messages ────────┤  stream_run_execution 订阅 broker
   │←──── event: updates ─────────┤  streaming_service.py:84
   │←──── event: messages ────────┤
   │←──── event: end ─────────────┤  (连接关闭)
```

两条分支：

| 情况 | 走哪条 |
| --- | --- |
| `stream_mode` 含 `events` | `graph_streaming.py:209` 的 `graph.astream_events(...)` |
| 其余（默认） | `graph_streaming.py:297` 的 `graph.astream(...)` |
| Agent Protocol **v2** 协议 | `usecase/streaming/v2/native_stream.py:77` |

---

[← 返回大纲](README.md)
