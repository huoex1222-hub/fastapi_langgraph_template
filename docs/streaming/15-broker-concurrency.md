# broker 并发模型：`backlog` 快照安全吗

`usecase/streaming/broker.py:64` 的 `backlog = list(self._replay_buffer)` 常被问「线程安全吗」。

**结论：在 asyncio 单事件循环模型下是安全的，但这套代码不是线程安全的。**
安全性完全依赖一个隐含约束：所有 `put()` / `aiter()` 调用都跑在创建 broker 的同一事件循环线程上。

## 运行模型：一个进程、一个 loop

本仓库所有执行器都是同一个 loop 上的 `asyncio.create_task`：

- `LocalExecutor`（`REDIS_BROKER_ENABLED=false`）——进程内直接起 task；
- `WorkerExecutor`（`REDIS_BROKER_ENABLED=true`）——`WORKER_COUNT` 个 **asyncio task**
  （`worker_executor.py:148-155`），不是线程，也不是 uvicorn worker 进程。

uvicorn 也只起单 worker（`Makefile:112`、`docker-compose.yml:52` 都没有 `--workers`）。
所以写 broker 的生产者（`run_executor.py:220`、`:250`、`:315`，`streaming_service.py:50`）
和读 broker 的 SSE 消费者都在**同一个线程的同一个事件循环**里。

`RunBroker.__init__` 里的 `asyncio.get_running_loop()`（`broker.py:40`）就是这条约束的
直接体现：broker 只能在有运行中 loop 的上下文里创建。

## 为什么单 loop 下没问题

事件循环只在协程显式 `await`（或让出控制权）时切换 task。两段关键代码内部都**没有
`await`**，因此各自构成一个原子区间：

```python
# put()：broker.py:43-56 —— 全程无 await
if resumable:
    self._replay_buffer.append((event_id, payload))     # ① 写历史
for queue in list(self._subscribers):                   # ② 广播快照
    queue.put_nowait((event_id, payload))
```

```python
# aiter()：broker.py:63-64 —— 两行之间无 await
self._subscribers.add(queue)          # 先注册
backlog = list(self._replay_buffer)   # 再取快照
```

`aiter()` 与 `put()` 的所有可能交错：

| 时序 | 事件去向 | 结果 |
| --- | --- | --- |
| put 在 add 之前 | 在 buffer 快照里 | 从 `backlog` 发出 |
| put 在 add 与快照之间 | **不可能发生**（无 await） | — |
| put 在快照之后 | 只进已注册的 queue | 从 queue 发出 |

所以这个「先注册、再快照」的配对既不会丢事件，也不会让同一个事件在本订阅者身上出现两次。

注释（`broker.py:60-62`）说的另一层保证也成立：调用方先 `replay()`、后进 `aiter()`，
两步之间 `put` 的事件仍在 buffer 里，会被 `backlog` 补上。真正重叠的部分（`replay()`
已经发过、`backlog` 又发一次）由调用方按 `event_id` 去重：

- v1 路径按事件序号过滤：`streaming_service.py:157-159`；
- v2 路径用 `seen` 集合：`v2/session.py:166-172`。

## 什么时候会坏：跨线程 / 跨 loop

代码里没有任何锁，`asyncio.Queue`、`asyncio.Event`、`set`、`list` 都不是线程安全的。
一旦生产者或订阅者跑到别的线程（`asyncio.to_thread`、`run_in_executor`、同步回调、
第二个事件循环）：

| 位置 | 跨线程时的问题 |
| --- | --- |
| `broker.py:52` `queue.put_nowait()` | asyncio 原语绑定 loop，跨线程调用会丢失唤醒甚至抛异常 |
| `broker.py:102` `self.finished.set()` | 跨线程设置不会唤醒该 loop 上的等待者 |
| `broker.py:51` `list(self._subscribers)` | 遍历期间另一线程 `add`/`discard` 可能 `RuntimeError: Set changed size during iteration` |
| `broker.py:64` `list(self._replay_buffer)` | 与另一线程 `append` 竞争；CPython + GIL 下实践中通常没事，但无语言保证，free-threaded 构建会坏 |

要支持跨线程生产者，正确做法是把操作投递回 broker 所属的 loop
（`loop.call_soon_threadsafe(...)`），而不是加 `threading.Lock`——锁挡不住 asyncio
原语的唤醒语义问题。

## 多进程不等于多线程

内存版 `BrokerManager` 是**进程内**的 `dict`（`broker.py:125`）。`uvicorn --workers > 1`
或水平扩容成多个容器时，SSE 请求可能落到没有该 broker 的进程，从此收不到事件。

就 `list(self._replay_buffer)` 这一行本身而言，多进程下反而「安全」：每个进程各有一份
broker 和 buffer，不存在共享内存，也就没有数据竞争。**坏的是语义，不是竞态**：进程 B
的 buffer 永远看不到进程 A 产生的事件。SSE 订阅落到 B 时，若 run 仍是 running，
`_stream_live_events` 会为它建一个空 broker 并进入 `aiter()`；`aiter()` 只在
`finished` 置位后退出（`broker.py:70-76`），而 B 上没有任何东西会置位它 → 连接挂死，
且不报错。`_event_counters` 也是每进程一份，跨进程 `allocate_event_id` 会撞号；
`active_runs` 是进程内 dict，`request_cancel` 落到非执行进程时找不到 task，静默失效。

这不是线程安全能解决的，需要 `REDIS_BROKER_ENABLED=true` 走 `RedisBrokerManager`
（pub/sub + Redis List 做补发）。当前 `make run` / `make dev` / `make up` 都是单
uvicorn 进程（`--workers` 不能与 `--reload` 同用），所以内存 broker 成立；
多实例部署必须开 Redis。

另外 `BrokerManager` 的 `_brokers` / `_event_counters` 同样只在单 loop 下安全。
`allocate_event_id()`（`broker.py:183-187`）是「读 + 写」两步，单 loop 下原子，
跨实例会撞号，所以 Redis 版用 `INCR`（`base_broker.py:107-116` 有说明）。

## 建议

1. 在 `RunBroker` 类 docstring 里显式写明 **loop-affinity**：所有 `put()` / `aiter()`
   必须运行在创建 broker 的同一事件循环线程上——目前这个约束是隐式的，后来的人用
   `asyncio.to_thread` 包一层 producer 就会悄悄破坏它。
2. 需要跨线程生产时用 `call_soon_threadsafe` 回投，不要直接调用 broker。
3. 多 uvicorn worker / 多容器部署时开启 Redis broker。

并发语义有测试兜底：`tests/unit/test_usecase/test_broker.py:104` 验证同一 run 上两个
并发 `aiter()` 各自收到全量事件（v2 SDK 主流 + lifecycle watcher 的场景）。

---

[← 返回大纲](README.md)
