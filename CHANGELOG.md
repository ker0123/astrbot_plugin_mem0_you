# Changelog

## [0.4.0-dev] - 2026-11-06

### Added

- 新增 `identity_store.py`：用 SQLite 邻接表保存 `alt`（小号）和 `member_of`（成员 -> 群）两类边。
- 新增 `docs/DESIGN-v0.4.md`：记录 `agent_id` + 身份图的完整设计、函数定义、查询语法、写入策略和测试计划。
- 新增管理命令：`/mem0 link`、`/mem0 unlink`、`/mem0 join`、`/mem0 leave`、`/mem0 graph`、`/mem0 syncgroup`。

### Changed

- `main.py` 重写为 v0.4 骨架：函数定义、输入输出注释和函数体内的实现流程注释，具体实现暂未填入。
- 记忆索引从 `user_id = prefix:persona:scope` 改为 `agent_id` + `user_id` 分离：
  - `agent_id = <agent_id_prefix>:<persona_id>`。
  - `user_id = <platform>:user:<sender_id>` 或 `<platform>:group:<group_id>`。
- 私聊作用域改为「身份分量 + 所有可见群」；群聊作用域改为「当前群 + 身份分量」，不扩散到发言人的其他群。
- 配置项收敛为 9 项：`mem0_api_url_base`、`mem0_api_key`、`agent_id_prefix`、`search_threshold`、`search_timeout`、`inject_p_in_p_chat`、`inject_p_in_g_chat`、`inject_g_in_p_chat`、`inject_g_in_g_chat`；其余旧配置移除，不做旧值迁移。
- 检索改为 `agent_id` + `OR[{"user_id": ...}]`，因为 mem0 的 `user_id: {"in": [...]}` 会返回 400。

### Breaking

- 旧版复合 `user_id` 与新查询不兼容；旧 `memories` 表保留归档，不做自动迁移。
- 当前 `dev` 分支是骨架，插件在填入实现前不能正常工作。

## [0.3.0] - 2026-11-02

### Changed

- 展示名改为 `mem0-you`, 仓库、插件目录和 `metadata.yaml` 的 `name` 改为 `astrbot_plugin_mem0_you`.
- 版本号更新为 `0.3.0`, 同步 `metadata.yaml` 与 `main.py` 的 `@register`.
- 默认记忆索引从 `session` 改为 `sender`, 同一发送者在不同群聊和私聊中共享记忆.
- README 按自托管 mem0 重写, 补充来源、原作者和 API 兼容说明.
- 配置项与 `_conf_schema.json` 对齐, 补充 `inject_as_system_prompt` 和 `ignore_commands`.
- 使用异步任务来写记忆, 单次请求超时时间固定为 300s. 查询记忆超时仍可修改, 默认为 12 秒.
- 移除未生效的 `mem0_api_type` 配置项, API 类型仍由 `/openapi.json` 自动探测.
- 同步更新 `AGENTS.md` 中的仓库路径、插件名和配置文件名.

### Added

- 新增本 CHANGELOG.

### Todo

- 写记忆方式对服务器负担太大, 同时还要考虑时效性. 最终决定保持任务队列做法, 但上个请求收到回复或超时前, 后续请求不会简单堆积, 而是根据 user_id 做合并, 后续一次性上传.

## [0.2.1] - 2026-10-31

### Added

- 完成自托管 mem0 OSS API 适配, 使用 `POST /memories` 和 `POST /search`.
- 启动时通过 `/openapi.json` 自动识别自托管服务, 失败时回退到官方 v3 API.
- 记忆 `user_id` 改为 `prefix:persona:session/sender` 组合, 支持按 persona 和发送者索引.
- 增加 persona 解析的多级回退逻辑, 提高获取当前人格的可靠性.

### Changed

- 写入记忆改为异步队列, mem0 请求不再阻塞 LLM 回复.
- 默认请求超时调整为 24 秒.
- 超时错误现在会带上路径和超时时间.
- 单行最大字符数放宽到 100, 并清理部分不安全写法.

## [0.2.0] - 2026-10-31

### Added

- 支持在配置中填写自托管 mem0 服务地址 `mem0_api_url_base`.

### Changed

- 更新插件版本信息和仓库链接.

## [0.1.0] - 2026-05-19

### Added

- Yuk1you 发布初始版本: 面向 mem0 官方托管 API 的 AstrBot 长期记忆插件.
