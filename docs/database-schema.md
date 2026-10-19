# 数据库表与设计意图

项目里的表分三类，**归属和迁移链严格分开**：

| 类别 | 定义位置 | 迁移方式 | 版本表 |
| --- | --- | --- | --- |
| 框架表（Agent Protocol 元数据） | `src/agent_server/repo/orm.py` | `src/agent_server/migrations` | `alembic_version` |
| Hub 业务表（skills / MCP / OAuth） | `src/hub/db.py` | `src/hub/migrations` | `alembic_version_hub` |
| LangGraph 持久化表（checkpoint / store） | LangGraph 包内 | `.setup()` 启动时自动建 | `checkpoint_migrations`、`store_migrations` |

`src/shop`、`src/ml` 没有数据库表：它们是 demo 用的内存实现
（`src/shop/backends.py:99` 的 `FakeShopBackend`），不落库。

## 总览

| 表 | 归属 | 一句话设计意图 |
| --- | --- | --- |
| `assistant` | 框架 | 用户可调用的 graph 配置实例；唯一索引 `(user_id, graph_id, md5(config))` 保证默认 assistant 幂等创建 |
| `assistant_versions` | 框架 | 配置快照历史；当前配置只读主表，历史走版本表 |
| `thread` | 框架 | 会话元数据与状态（idle/busy/interrupted/error）；对话内容不在这里 |
| `runs` | 框架 | 一次图执行；`execution_params` 让 Redis worker 能从 DB 重建 RunJob，`claimed_by/lease_expires_at` 支持崩溃恢复 |
| `crons` | 框架 | 定时任务；`(enabled, next_run_date)` 支撑到期扫描，`claimed_until` 防多实例重复触发 |
| `thread_ttl` | 框架 | 逐线程 TTL；sweeper 用 `FOR UPDATE SKIP LOCKED` 领取并跳过活跃 run |
| `skill` / `skill_file` | Hub | 用户级 skill；内置 skill 不进表，文件内容用 `bytea` 预留二进制 |
| `mcp_connection` | Hub | 用户级 MCP 连接；仅 `streamable_http`，`headers` 用 Fernet 加密落库 |
| `mcp_oauth_client` | Hub | DCR 客户端注册持久化；token 反而是短期的，放 TokenStore |
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | LangGraph | HITL 的基础：暂停时状态落库，恢复时按 `thread_id` 找到 pending task 继续 |
| `store`（+ 可选 `store_vectors`） | LangGraph | 跨线程长期记忆；默认不建向量表，取决于 `store.index` |
| `alembic_version` / `alembic_version_hub` | 迁移 | 框架链与 Hub 链各自的版本游标，永不混用 |

## 框架表

### `assistant`

一条记录 = 一个用户可调用的 graph 配置实例。

| 列 | 说明 |
| --- | --- |
| `assistant_id` | `text` 主键，默认 `gen_random_uuid()::text` |
| `graph_id` | 指向 `langgraph.json` 里注册的图 |
| `config` / `context` | JsonbSafe，运行默认配置与运行时上下文 |
| `user_id` | 租户隔离键；`system` 表示内置默认 assistant |
| `version` | 当前版本号 |
| `metadata` | JsonbSafe，供搜索/过滤 |

索引与意图：

- `idx_assistant_user_assistant (user_id, assistant_id)` 唯一 —— 所有查询都带租户前缀。
- `idx_assistant_user_graph_config (user_id, graph_id, md5(config::text))` 唯一 ——
  **保证同一用户不会重复创建相同 graph + 相同 config 的 assistant**。
  配合 `uuid5(namespace, graph_id)` 的确定性 ID，图的默认 assistant 可以幂等创建。
- `idx_assistant_metadata_gin`（`jsonb_path_ops`）—— metadata 查询走 GIN。

### `assistant_versions`

assistant 的配置快照历史，每改一次配置追加一行。

- 主键 `(assistant_id, version)`，外键 `ON DELETE CASCADE`。
- 存 `graph_id` / `config` / `context` / `metadata` / `name` / `description`。
- 意图：`GET /assistants/{id}/versions` 能回看历史配置，同时 `assistant` 表只保留
  “当前版本”，读取路径不必扫历史。

### `thread`

一条记录 = 一段可持久化的会话；也是 LangGraph checkpoint 的路由键。

| 列 | 说明 |
| --- | --- |
| `thread_id` | `text` 主键，客户端也可以自己生成 |
| `status` | `idle` / `busy` / `interrupted` / `error` |
| `metadata_json` | JsonbSafe，GIN 索引支持按 metadata 搜索 |
| `user_id` | 租户隔离；所有线程查询都必须带 |

设计意图：

- `thread` 本身只存元数据和状态，**不存对话内容**；消息和状态在 LangGraph 的
  checkpoint 表里，按 `thread_id` 关联。这样框架表和 LangGraph 表解耦。
- 删除 thread 会级联删除 `runs`、`crons`、`thread_ttl`（外键 `ON DELETE CASCADE`）。

### `runs`

一条记录 = 一次图执行（一次 `POST .../runs`）。

| 列 | 说明 |
| --- | --- |
| `run_id` | `text` 主键，默认 `gen_random_uuid()::text` |
| `thread_id` | 外键，级联删除 |
| `assistant_id` | 可空外键，级联删除 |
| `status` | `pending` / `running` / `success` / `error` / `timeout` / `interrupted` |
| `input` / `config` / `context` / `output` | JsonbSafe |
| `error_message` | 失败原因 |
| `execution_params` | **完整 `RunJob` 的 JSONB 快照** |
| `claimed_by` / `lease_expires_at` | worker 租约 |

索引与意图：

- `idx_runs_thread_id` / `idx_runs_user` / `idx_runs_assistant_id` —— 列表与按租户查询。
- `idx_runs_status` —— 状态过滤与统计。
- `idx_runs_created_at` —— 按时间排序/分页。
- `idx_runs_lease_reaper (status, lease_expires_at)` —— **租约回收器**扫描
  “运行中但租约过期”的 run，重新入队，实现 worker 崩溃恢复。

关键设计：

- `execution_params` 让 **API 进程与 worker 解耦**：Redis 队列里只传 `run_id`，
  worker 从数据库重建 `RunJob`（`RunJob.from_run_orm`），不需要原始 HTTP 请求。
- `claimed_by` + `lease_expires_at` 支持多实例：一个 run 同一时刻只应被一个
  worker 执行；worker 定时续租，崩溃后租约过期由 reaper 接管。
- `interrupted` 是正常终态，不是错误；HITL 恢复会创建一个**新的 run 行**。

### `crons`

定时任务定义。

| 列 | 说明 |
| --- | --- |
| `cron_id` | 主键 |
| `assistant_id` | 必填外键 |
| `thread_id` | 可空：共享线程或每次新建 |
| `schedule` | cron 表达式 |
| `payload` / `metadata` | 触发时传给 run 的输入与元数据 |
| `on_run_completed` | 完成后行为（如 delete/keep） |
| `enabled` / `end_time` | 启停与截止时间 |
| `next_run_date` | 下次触发时间 |
| `claimed_until` | 多实例调度器的抢锁时间窗 |

索引 `idx_cron_next_run (enabled, next_run_date)` 专为
“扫描到期任务”设计；`claimed_until` 防止多个 scheduler 实例重复触发同一个 cron。

### `thread_ttl`

线程 TTL 的逐线程配置。

| 列 | 说明 |
| --- | --- |
| `thread_id` | 主键 + 外键，级联删除 |
| `strategy` | `delete`（删线程）或 `keep_latest`（裁剪 checkpoint，保留最新状态） |
| `ttl_minutes` | TTL 时长 |
| `expires_at` | 过期时间，建索引供 sweeper 扫描 |

设计意图（`src/agent_server/usecase/thread_ttl.py:1-12`）：

- TTL 是 **opt-in**：创建线程时按服务器默认或单线程覆盖写入一行。
- 后台 sweeper 用 `FOR UPDATE SKIP LOCKED` 批量领取，**同时锁住 `thread` 行**，
  避免“检查时无活跃 run、随后新 run 插进来”的竞态。
- 有 `pending`/`running` run 的线程跳过而不是取消，下一轮再处理。
- `keep_latest` 会直接操作 checkpoint 表（LangGraph 自有表），因此有 schema
  漂移金丝雀测试兜底。

## Hub 业务表

Hub 是与协议框架同级的应用层领域包，表迁移走自己的链，不混入框架链。

### `skill`

用户安装的 skill 元数据。

- 主键 `skill_id`；`user_id` 租户隔离。
- 唯一索引 `(user_id, name)` —— 同一用户下名字唯一。
- **内置 skill 存在图包里，不进这张表**；这里只存用户上传的。

### `skill_file`

skill 的文件内容。

- 主键 `skill_file_id`；`skill_id` 外键级联删除。
- 唯一索引 `(skill_id, path)` —— 同一 skill 内路径唯一。
- `content` 用 `bytea`：当前 API 只接受 UTF-8 文本，但类型预留了二进制资产，
  以后加图片/压缩包不需要改 schema。

### `mcp_connection`

用户级 MCP 连接。

- 主键 `mcp_connection_id`；唯一索引 `(user_id, name)`。
- `transport` 默认/仅允许 `streamable_http`：**stdio 是部署特权**，用户 API 不能
  传 `command`（见 `AGENTS.md` 的 hub 规则）。
- `auth_type`：`none` / `headers` / `oauth`。
- `headers` 用 `EncryptedJson`（Fernet）落库：凭证**写后不可读**，
  API 响应只返回 header 名，不返回值。
- `enabled` 支持软停用，不删历史配置。

### `mcp_oauth_client`

OAuth 的 DCR（动态客户端注册）持久化。

- 复合主键 `(user_id, connection_name)`。
- `registration` 用 `EncryptedJson`（里面可能有 `client_secret`）。
- 设计意图：**client registration 是长期资产，token 是短期资产**。
  注册信息落 Postgres；access/refresh token 交给 `TokenStore`
  （开发内存、生产 Redis），不写这张表。

## LangGraph 自有表

这些表由 `AsyncPostgresSaver.setup()` / `AsyncPostgresStore.setup()`
在应用启动时创建（`src/agent_server/repo/database.py:78-91`），
**不归 alembic 管**，也刻意从 Hub autogenerate 中剔除（否则会被误判为待删除）。

### Checkpoint 链（会话状态与 HITL）

| 表 | 作用 |
| --- | --- |
| `checkpoint_migrations` | LangGraph 自己的 schema 版本 |
| `checkpoints` | 每个 checkpoint 的元数据：`thread_id` / `checkpoint_ns` / `checkpoint_id` / 父 checkpoint / JSONB 状态 |
| `checkpoint_blobs` | channel 值的大块内容，按 `(thread_id, ns, channel, version)` 存 |
| `checkpoint_writes` | 待落盘的 write（task 级），用于故障恢复 |

设计意图：

- **checkpoint 是 HITL 的基础**：`interrupt()` 暂停时状态已经落库；恢复时
  LangGraph 用 `configurable.thread_id` 找到最后一个 checkpoint，定位 pending
  task，把 `Command(resume=...)` 的值交给 `interrupt()` 返回。
- `checkpoint_blobs` 与 `checkpoints` 分离：状态按 channel/version 增量存储，
  避免每个 checkpoint 重复复制整份大状态。
- `checkpoint_writes` 保证节点执行到一半崩溃时，已产生的写入不会丢。
- 三张表都有 `thread_id` 索引；主线查询都按线程走。

### Store 链（跨线程长期记忆）

| 表 | 作用 |
| --- | --- |
| `store_migrations` | store schema 版本 |
| `store` | `(prefix, key) -> value`，即 namespace + key 的 KV，带 `expires_at` / `ttl_minutes` |
| `vector_migrations` + `store_vectors` | **可选**：配置了 `store.index` 时才建，依赖 pgvector，存嵌入向量 |

设计意图：

- `prefix` 就是 namespace 路径，`store_prefix_idx`（`text_pattern_ops`）支持前缀扫描。
- `expires_at` + 部分索引支持 TTL 清理，与线程生命周期解耦。
- 默认不启用向量索引；`langgraph.json` 的 `store.index` 决定是否创建
  `store_vectors`，避免无 pgvector 的环境启动失败。

## 迁移链与启动

- **框架链**：`src/agent_server/migrations`，版本表 `alembic_version`。
  `run_migrations()` 带 advisory lock；`run_migrations_if_needed()` 先做轻量
  precheck，已是 head 就跳过，避免多 pod 启动时抢锁。
- **Hub 链**：`src/hub/migrations`，版本表 `alembic_version_hub`。启动时由
  `run_hub_migrations_if_needed()` 自动检查并升级；两条链互不感知。
- **LangGraph 链**：应用启动时 `setup()` 建表，和 alembic 完全分离。
- 新增业务表一律走所属领域的链：框架实体进框架链，Hub 实体进 Hub 链，
  永远不要用应用代码跑裸 DDL。

## 横向设计约定

- **租户隔离**：所有用户可见表都带 `user_id`，查询必须在 WHERE 里带上；
  主键本身不保证归属。
- **JSONB 安全**：`JsonbSafe` 在写入前递归剥离 `\u0000`，因为 Postgres JSONB
  不接受 NULL 字节；`EncryptedJson` 在类型边界做 Fernet 加解密，保证凭证不落明文。
- **软状态机而非删除**：run 的 `status`、thread 的 `status`、cron 的 `enabled`
  都是显式状态；`interrupted` / `timeout` 等终态用于协议语义，不当异常处理。
- **多实例安全**：`runs.claimed_by/lease_expires_at`、`crons.claimed_until`、
  `thread_ttl` 的 `FOR UPDATE SKIP LOCKED` 都是为多副本部署设计的。
- **级联边界**：thread 删除会级联 runs/crons/thread_ttl；assistant 删除会级联
  runs/versions/crons。Hub 的 `skill_file` 随 `skill` 级联，
  `mcp_oauth_client` 不设外键（连接名是逻辑键，允许连接先删后清）。
