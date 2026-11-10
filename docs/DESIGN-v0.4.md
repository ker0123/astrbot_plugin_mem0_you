# v0.4 设计：agent_id + 身份图

> 状态：已实现。`main.py` / `identity_store.py` 已填入具体实现，细节决策见 [`DECISIONS-v0.4.md`](DECISIONS-v0.4.md)。

## 1. 目标

1. `agent_id` 与 `user_id` 分开存储：
   - `agent_id`：人格（persona），使用稳定的 `persona_id`。
   - `user_id`：agent 记忆中的对象，可以是**个人**，也可以是**群**。
2. 私聊可以回忆「这个人」以及「这个人所在的所有群」。
3. 群聊只回忆「当前群」+「当前发言人（及其小号）」，不扩散到发言人的其他群。
4. 确认的小号共享个人记忆；群聊中仍不扩散到小号的其他群。
5. 从读取侧杜绝「A 打听 B」：A 的检索集合里永远没有 B 的个人记忆。
6. 写入侧分桶：群记忆只存群级事实，个人事实只存个人节点。
7. 图操作管理员确认、可审计、可撤销。

## 2. 节点与边

```text
<platform>:user:<sender_id>
<platform>:group:<group_id>
```

- `platform` 优先 `event.get_platform_id()`，回退 `get_platform_name()`。
- 片段经过 `_safe_part()`，把 `:`、空白、`/` 替换为 `_`。
- 节点字符串同时作为 mem0 的 `user_id`。

| kind | 方向 | 含义 |
|---|---|---|
| `alt` | 双向 | 两个账号是同一个人 |
| `member_of` | `member -> group` | 成员能看到该群的记忆 |

**只沿箭头方向遍历，绝不从 `group` 反向遍历到 `member`。**

## 3. 作用域

```text
identity(A) = A 在 alt 边上的连通分量
groups(X)   = X 通过 member_of 指向的群

私聊 A:
  scope = identity(A) ∪ groups(identity(A))

群 G 中 A 发言:
  scope = {G} ∪ identity(A)
```

群聊不取 `groups(identity(A)) \ {G}`：群 G 的受众是 G 的全部成员，不只是 A；把 A 的其他群带进来会向 G 的其他人泄露他们看不到的内容。

例子：A 在 G1、G2；A 的小号 A2 在 G3。

| 场景 | 查询集合 |
|---|---|
| 私聊 A | `{A, A2, G1, G2, G3}` |
| 群 G1 中 A 发言 | `{G1, A, A2}` |
| 群 G1 中 B 发言 | `{G1, B, alts(B)}` |

## 4. mem0 查询

### 不能使用 `user_id: {"in": [...]}`

mem0 2.2.1 会先对 `user_id` 做字符串校验，dict 被转成带空格的字符串，实测返回 400：

```text
Invalid user_id: cannot contain whitespace.
```

### 使用 `agent_id` + `OR`

```json
{
  "query": "...",
  "filters": {
    "agent_id": "astrbot:parry",
    "OR": [
      {"user_id": "qq:user:1485465108"},
      {"user_id": "qq:user:8239390588"},
      {"user_id": "qq:group:987654321"}
    ]
  },
  "top_k": 10,
  "threshold": 0.1
}
```

- 一次 embedding、一次 pgvector 检索。
- `agent_id` 是外层 AND，`OR` 内是 user/group 节点。
- 单节点退回 `{"agent_id": ..., "user_id": node}`。
- 节点数上限固定为 50（内部常量，不暴露配置），超出时优先保留 identity 和最近同步的群。
- `top_k` 内部按 `max(10, 当前场景注入上限之和 * 2)` 计算。

## 5. 写入策略

### 私聊 A

```text
写 user:A
messages = [user, assistant]
prompt   = USER_MEMORY_PROMPT
metadata = {scope: "private", ...}
```

### 群 G 中 A 发言

```text
写 group:G
messages = [user, assistant]
prompt   = GROUP_MEMORY_PROMPT
metadata = {scope: "group", group_id: G, speaker_id: A, ...}

写 user:A
messages = [user]            # 不传 assistant，避免把 B 的信息带进 A 的个人记忆
prompt   = USER_MEMORY_PROMPT
metadata = {scope: "group_speaker", group_id: G, ...}
```

### Prompt 约束

- 个人：只提取当前发言者本人的稳定事实；不提其他人；不把 assistant 的话当成发言者事实。
- 群：只提取群级事实（活动、约定、规则、共同计划、群话题）；不提任何成员的个人事实。
- mem0 的 `prompt` 参数是 `custom_instructions`，会拼进系统抽取提示，不需要自己提供 JSON schema。
- 仅 OSS 接口传 `prompt`；V3 兼容路径不传。

## 6. 数据表

```sql
CREATE TABLE IF NOT EXISTS identity_edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('alt', 'member_of')),
    created_at TEXT NOT NULL,
    created_by TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_edges_unique
    ON identity_edges(src, dst, kind);

CREATE INDEX IF NOT EXISTS idx_identity_edges_src_kind
    ON identity_edges(src, kind);

CREATE INDEX IF NOT EXISTS idx_identity_edges_dst_kind
    ON identity_edges(dst, kind);
```

- `alt` 写入时对 `(a, b)` 排序，无向边只存一份。
- `member_of` 由 `(member, group)` 唯一约束去重。
- `created_at` 是边最近一次写入时间；群成员整体替换时统一刷新，因此群排序语义是“最近同步优先”。
- 退群：群成员同步成功后 `DELETE ... WHERE kind='member_of' AND dst=group` 再批量插入；不加 `joined_at/left_at` 字段。
- 群成员同步在群聊请求路径只检查 TTL 并调度后台任务，不阻塞 LLM 请求；`/mem0mng sync` 才同步等待。
- 成功 TTL 300 秒，失败 TTL 60 秒；退群成员最多 5 分钟内仍可能可见，属于最终一致性。
- 单实例放 `data/plugin_data/astrbot_plugin_mem0_you/identity.db`；多实例再换 Postgres。

## 7. 函数定义

### `identity_store.py`

```python
class IdentityStore:
    def __init__(self, path: str | Path) -> None
    def close(self) -> None

    def add_alt(self, a: str, b: str, created_by: str | None = None) -> bool
    def remove_alt(self, a: str, b: str) -> bool
    def add_member(self, member: str, group: str, created_by: str | None = None) -> bool
    def remove_member(self, member: str, group: str) -> bool
    def replace_group_members(
        self, group: str, members: Iterable[str], created_by: str | None = None
    ) -> int

    def identity_component(self, node: str) -> list[str]
    def groups_for_members(self, members: Iterable[str]) -> list[str]
    def members_of_group(self, group: str) -> list[str]
    def private_scope(self, node: str) -> list[str]
    def group_scope(self, group: str, node: str) -> list[str]
    def edges_for(self, node: str) -> list[Edge]
    def stats(self) -> dict[str, int]
```

### `main.py`

生命周期：

```python
async def initialize(self) -> None
async def terminate(self) -> None
```

身份与作用域：

```python
async def _persona_identity(self, event, req=None) -> tuple[str, str]
def _agent_id(self, persona_id: str) -> str
def _platform_key(self, event) -> str
def _speaker_id(self, event) -> str
def _group_node(self, event) -> str | None
def _scope_nodes(self, event) -> list[str]
```

群成员同步：

```python
async def _sync_group_members(self, event, group: str) -> int
def _schedule_group_sync(self, event, group: str) -> None
async def _background_group_sync(self, event, group: str) -> None
async def _fetch_group_members(self, event, group: str) -> list[str]
async def _fetch_group_profile(self, event, group: str) -> tuple[str | None, list[tuple[str, str]]]
```

检索：

```python
def _inject_limits(self, is_group: bool) -> tuple[int, int]
async def _search_memories(self, query, agent_id, nodes, is_group: bool) -> list[dict]
def _normalize_results(self, response) -> list[dict]
def _select_memories(self, results, group_node) -> tuple[list[dict], list[dict]]
def _format_memory_context(self, group_memories, personal_memories, is_group) -> str
```

写入：

```python
def _add_memory(self, *, agent_id, subject_id, messages, prompt, metadata) -> None
def _enqueue(self, path: str, payload: dict, label: str) -> None
def _ensure_add_worker(self) -> asyncio.Queue
async def _add_worker_loop(self) -> None
```

钩子与命令：

```python
@filter.on_llm_request(priority=20)
async def inject_mem0_memory(self, event, req) -> None

@filter.on_llm_response(priority=20)
async def record_mem0_memory(self, event, response) -> None

@filter.command("mem0")
async def mem0_command(self, event) -> AsyncIterator

@filter.command("mem0mng")
@filter.permission_type(filter.PermissionType.ADMIN)
async def mem0mng_command(self, event) -> AsyncIterator
```

命令辅助：

```python
def _parse_full_node(self, arg: str) -> str | None
def _node_kind(self, node: str) -> str
def _node_platform(self, node: str) -> str

async def _cmd_me(self, event) -> str
async def _cmd_group(self, event) -> str
async def _cmd_persona(self, event) -> str

async def _cmd_mng_info(self, query: str, event) -> str
async def _cmd_mng_link(self, args: list[str], event) -> str
async def _cmd_mng_unlink(self, args: list[str], event) -> str
async def _cmd_mng_sync(self, event) -> str
```

## 8. 配置

只保留以下 9 项：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `mem0_api_url_base` | `"https://api.mem0.ai"` | mem0 服务地址 |
| `mem0_api_key` | `""` | mem0 API Key |
| `agent_id_prefix` | `"astrbot"` | `agent_id` 前缀 |
| `search_threshold` | `0.1` | 检索相关性阈值 |
| `search_timeout` | `12` | 查询超时（秒）；写入超时固定 300 秒 |
| `inject_p_in_p_chat` | `5` | 私聊个人记忆注入上限 |
| `inject_p_in_g_chat` | `2` | 群聊个人记忆注入上限 |
| `inject_g_in_p_chat` | `2` | 私聊群记忆注入上限，所有可见群共享 |
| `inject_g_in_g_chat` | `4` | 群聊当前群记忆注入上限 |

记忆桶按 `user_id` 区分：`*:user:*` 为个人，`*:group:*` 为群。

| 场景 | 个人上限 | 群上限 |
|---|---|---|
| 私聊 | `inject_p_in_p_chat` | `inject_g_in_p_chat` |
| 群聊 | `inject_p_in_g_chat` | `inject_g_in_g_chat` |

- 值为 `0` 表示该桶不注入。
- 以下行为不再提供配置，固定为：始终启用、始终写群记忆、始终召回当前群、始终注入 `system_prompt`、始终忽略命令消息。
- 管理员命令 `mem0mng` 使用 AstrBot 的 `PermissionType.ADMIN`，插件内不再手写 `event.is_admin()`。
- 内部常量：`MAX_SCOPE_NODES=50`、`MAX_MEMORY_CHARS=240`、`GROUP_SYNC_TTL=300`、写入超时 300 秒。
- 身份图固定放在插件数据目录下的 `identity.db`。
- 不迁移旧配置值，也不迁移旧版记忆索引。

## 9. 命令

| 命令 | 权限 | 作用 |
|---|---|---|
| `/mem0 me` | 所有人 | 显示当前用户完整节点；私聊额外显示直接相连的节点 |
| `/mem0 group` | 所有人 | 显示当前群完整节点；私聊不可用 |
| `/mem0 persona` | 所有人 | 显示当前人格名、`persona_id`、`agent_id` |
| `/mem0mng info <query>` | 管理员 | 当前群内模糊匹配群名/昵称/群名片，输出候选节点 |
| `/mem0mng link <a> <b>` | 管理员 | 只接受完整节点，自动建立 `alt` 或 `member_of` |
| `/mem0mng unlink <a> <b>` | 管理员 | 只接受完整节点，自动删除 `alt` 或 `member_of` |
| `/mem0mng sync` | 管理员 | 强制同步当前群成员 |

管理员判断使用 AstrBot 的 `@filter.permission_type(filter.PermissionType.ADMIN)`。

### link / unlink 类型规则

| a | b | 行为 |
|---|---|---|
| `user` | `user` | 建立/删除 `alt` |
| `user` | `group` | 建立/删除 `member_of` |
| `group` | `user` | 归一化为 `user -> group` 后处理 |
| `group` | `group` | 拒绝 |
| 跨平台 `user` + `group` | 拒绝 | 群成员关系必须同平台 |
| 跨平台 `user` + `user` | 允许 | 小号可以跨平台 |

昵称只用于 `/mem0mng info` 的匹配展示；`link` / `unlink` 不接受昵称或模糊匹配。

## 10. 测试计划

1. 小号分量：A-B、B-C 的传递闭包。
2. `private_scope(A)` 包含 A、小号、所有群。
3. `group_scope(G1, A)` 包含 G1、A、小号，不包含 G2。
4. `replace_group_members` 让退群成员从边表消失。
5. 私聊召回群记忆；群聊不召回其他群。
6. A 在群/私聊都查不到 B 的个人记忆。
7. `/mem0mng link` 后小号个人记忆合并，但群聊不合并小号的其他群。
8. `/mem0mng link` 只接受完整节点；重名昵称不会产生歧义。
9. `/mem0 me` 私聊显示直接相连节点，群聊只显示当前用户节点。

## 11. 风险

1. **群记忆写入分类是最大风险**：图只保证读取隔离，不能阻止把 B 的个人事实写进群记忆；必须靠 prompt 约束和抽查。
2. **小号传递性**：A-B、B-C 会合并 A/C；如果 B 是共用号，可能误合并。每条边需确认，`link` 后显示分量大小。
3. **群成员同步依赖平台 API**：aiocqhttp 可用；其他平台失败时只保证当前发言人可见，需要 `/mem0mng link` 手工补。
4. **OR 查询规模**：节点很多时 SQL 变长；内部固定上限 50 截断，优先保留 identity 和最近同步的群。
5. **群聊召回个人记忆**：这是刻意设计，但必须配合 prompt 护栏，避免主动披露私聊内容。
6. **退群最终一致性**：只靠 300 秒 TTL，退群成员最多可能在 5 分钟内仍被视为可见。
