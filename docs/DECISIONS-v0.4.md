# v0.4 身份图：已确认决策

> 本文记录已确认的图存储、作用域、同步策略和命令设计。

## 1. 存储方案

使用插件私有 SQLite：

```text
data/plugin_data/astrbot_plugin_mem0_you/identity.db
```

- 路径由 `StarTools.get_data_dir("astrbot_plugin_mem0_you")` 决定，不新增配置项。
- 使用 AstrBot 的 `plugin_data` 备份与卸载清理机制。
- 连接使用 `check_same_thread=False`、`threading.RLock`、WAL。
- `terminate` 时关闭连接。
- 不使用 AstrBot 主库，也不使用插件 KV 存整张图。

## 2. 节点与边

```text
<platform>:user:<sender_id>
<platform>:group:<group_id>
```

| kind | 方向 | 含义 |
|---|---|---|
| `alt` | 双向 | 两个账号是同一个人 |
| `member_of` | `member -> group` | 成员能看到该群记忆 |

- `alt` 写入时对 `(a, b)` 排序，同一对账号只存一份。
- `member_of` 只允许 `member -> group`，绝不反向遍历。
- 群成员同步成功后，用完整成员列表整体替换该群的 `member_of` 边。
- 空成员列表不调用替换，回退为只写入当前发言人。

## 3. 作用域

```text
identity(A) = A 在 alt 边上的连通分量
groups(X)   = X 通过 member_of 指向的群

私聊 A:
  scope = identity(A) ∪ groups(identity(A))

群 G 中 A 发言:
  scope = {G} ∪ identity(A)
```

群聊刻意不取 `groups(identity(A)) \ {G}`，避免把 A 的其他群内容带进 G。

## 4. 群成员同步

- 群聊收到消息时，检查该群 TTL 和 in-flight 状态。
- TTL 未到期或已有任务在跑：直接返回，不阻塞 LLM 请求。
- 需要同步时创建后台任务，立即返回。
- 后台任务拉取完整群成员列表，调用 `replace_group_members`，成功后更新 TTL。
- 失败时记录 warning，并设置短失败 TTL（内部固定 60 秒）。
- `/mem0mng sync` 保留同步等待语义，管理员需要立即看到结果。
- `terminate` 时取消未完成的后台同步任务。

## 5. TTL 与一致性

- 群成员同步 TTL 固定 300 秒，不暴露配置。
- 不做群成员增减事件监听。
- 退群成员最多可能在 5 分钟内仍被视为群成员；这是最终一致性，不承诺实时。

## 6. 群排序语义

`groups_for_members` 按 `MAX(created_at)` 排序。

由于 `replace_group_members` 每次整体刷新该群所有成员边的时间，实际含义是：

> 最近同步过的群优先

不是“最近加入的群优先”。文档和后续实现统一使用“最近同步”表述。

## 7. 节点兜底

- `get_sender_id()` 为空时回退 `"unknown"`。
- 不使用 `get_session_id()` 兜底，避免群聊中把群号当用户号。
- 群成员列表排除机器人自己的 `self_id`。

## 8. 非目标

- 不引入实时退群监听。
- 不迁移旧版复合 `user_id` 数据。
- 不把身份图存进 AstrBot 主库或单个 KV JSON。
- 不为图存储新增配置项。

## 9. 命令设计

### 权限

- `/mem0`：所有人。
- `/mem0mng`：管理员，使用 AstrBot 的 `@filter.permission_type(filter.PermissionType.ADMIN)`。
- 插件内不再手写 `event.is_admin()`。

### 成员命令

| 命令 | 输出 |
|---|---|
| `/mem0 me` | 当前用户的完整节点。群聊只输出节点；私聊额外输出与该节点**直接相连**的节点（alt + member_of）。 |
| `/mem0 group` | 当前群的完整节点。私聊提示“当前为私聊，无群聊节点”。 |
| `/mem0 persona` | 当前人格名、`persona_id`、`agent_id`。 |

输出只使用完整节点，例如：

```text
节点: qq:user:123456
连接: qq:user:654321
连接: qq:group:987654321
```

### 管理员命令

| 命令 | 说明 |
|---|---|
| `/mem0mng info <query>` | 仅在群聊可用；用 `query` 模糊匹配当前群群名、成员昵称、群名片；输出“名称 + 完整节点”候选列表；不修改图。 |
| `/mem0mng link <a> <b>` | 只接受完整节点；按节点类型自动建立 `alt` 或 `member_of`。 |
| `/mem0mng unlink <a> <b>` | 只接受完整节点；按节点类型自动删除 `alt` 或 `member_of`。 |
| `/mem0mng sync` | 仅在群聊可用；强制忽略 TTL，同步等待当前群成员更新，返回成员数量。 |

### link / unlink 类型规则

| a | b | 行为 |
|---|---|---|
| `user` | `user` | 建立/删除 `alt` |
| `user` | `group` | 建立/删除 `member_of` |
| `group` | `user` | 归一化为 `user -> group` 后处理 |
| `group` | `group` | 拒绝 |
| 跨平台 `user` + `group` | 拒绝 | 群成员关系必须同平台 |
| 跨平台 `user` + `user` | 允许 | 小号可以跨平台 |

- 不支持昵称、模糊匹配或自动补全；昵称只用于 `info` 的匹配展示。
- `link` 成功返回：边类型；`user+user` 时返回身份分量大小和会合并的群数量；
  `user+group` 时返回是否新增。
- `unlink` 成功返回：边类型和是否真的删除。

### 已删除命令

```text
/mem0 status
/mem0 search
/mem0 link
/mem0 unlink
/mem0 join
/mem0 leave
/mem0 graph
/mem0 syncgroup
```

