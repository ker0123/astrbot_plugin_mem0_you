"""Command handlers for the AstrBot mem0 plugin."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from .main import Mem0MemoryPlugin


def _split_command(text: str) -> tuple[str, list[str]]:
    """Split '<command> <sub> <args...>' into subcommand and args."""
    parts = (text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return "", []
    rest = parts[1].strip().split(maxsplit=1)
    sub = rest[0].lower()
    args = rest[1].split() if len(rest) > 1 else []
    return sub, args


class Mem0Commands:
    """Handle /mem0 and /mem0mng commands."""

    def __init__(self, plugin: Mem0MemoryPlugin):
        self.plugin = plugin

    # ------------------------------------------------------------------
    # Node helpers
    # ------------------------------------------------------------------

    def _parse_full_node(self, arg: str) -> str | None:
        """Validate one full graph node."""
        text = (arg or "").strip()
        parts = text.split(":")
        if len(parts) != 3:
            return None
        platform, kind, raw_id = parts
        if not platform or not kind or not raw_id:
            return None
        if kind not in ("user", "group"):
            return None
        if any(re.search(r"\s", part) for part in parts):
            return None
        return text

    def _node_kind(self, node: str) -> str:
        """Return the node kind."""
        return node.split(":", 2)[1]

    def _node_platform(self, node: str) -> str:
        """Return the node platform."""
        return node.split(":", 2)[0]

    def _parse_user_node(self, arg: str) -> str | None:
        """Validate one full user graph node."""
        node = self._parse_full_node(arg)
        if not node or self._node_kind(node) != "user":
            return None
        return node

    def _event_created_at(self, event: AstrMessageEvent) -> datetime:
        """Extract UTC message creation time from event, falling back to now."""
        raw = getattr(getattr(event, "message_obj", None), "timestamp", None)
        if raw is None:
            raw = getattr(event, "timestamp", None)
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                return raw.replace(tzinfo=timezone.utc)
            return raw.astimezone(timezone.utc)
        if isinstance(raw, (int, float)) and raw > 0:
            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                pass
        return datetime.now(timezone.utc)

    def _format_alt_summary(self, state: str, a: str, b: str) -> str:
        """Format alt-edge status summary."""
        identity = self.plugin.identity
        if identity is None:
            return f"{state} alt 边: {a} <-> {b}"
        component = identity.identity_component(a)
        groups = identity.groups_for_members(component)
        return (
            f"{state} alt 边: {a} <-> {b}\n"
            f"身份分量: {len(component)}\n"
            f"关联群: {len(groups)}"
        )

    # ------------------------------------------------------------------
    # Member commands
    # ------------------------------------------------------------------

    async def _cmd_me(self, event: AstrMessageEvent) -> str:
        """Build /mem0 me output."""
        speaker = self.plugin.speaker_id(event)
        lines = [f"节点: {speaker}"]

        if (
            self.plugin.group_node(event) is None
            and self.plugin.identity is not None
        ):
            try:
                neighbors: list[str] = []
                for edge in self.plugin.identity.edges_for(speaker):
                    other = edge.dst if edge.src == speaker else edge.src
                    if other and other != speaker:
                        neighbors.append(other)
                unique_neighbors = [
                    node for node in dict.fromkeys(neighbors) if node
                ]
                lines.extend(f"连接: {node}" for node in unique_neighbors)
            except Exception as exc:
                logger.warning("mem0 me lookup failed: %s", exc)

        return "\n".join(lines)

    async def _cmd_group(self, event: AstrMessageEvent) -> str:
        """Build /mem0 group output."""
        group = self.plugin.group_node(event)
        if not group:
            return "当前为私聊，无群聊节点。"
        return f"节点: {group}"

    async def _cmd_persona(self, event: AstrMessageEvent) -> str:
        """Build /mem0 persona output."""
        persona_id, persona_name = await self.plugin.persona_identity(event)
        return (
            f"人格: {persona_name}\n"
            f"persona_id: {persona_id}\n"
            f"agent_id: {self.plugin.agent_id(persona_id)}"
        )

    async def _cmd_link(
        self, args: list[str], event: AstrMessageEvent
    ) -> str:
        """Handle /mem0 link <b>."""
        if len(args) != 1:
            return "用法: /mem0 link <b>"
        b = self._parse_user_node(args[0])
        if not b:
            return "只接受完整用户节点: <platform>:user:<id>"
        if self.plugin.identity is None:
            return "身份图未初始化。"

        a = self.plugin.speaker_id(event)
        if a == b:
            return "不能连接同一个节点。"

        msg_dt = self._event_created_at(event)
        now_ts = self.plugin.identity.format_request_time(msg_dt)
        expires_at = self.plugin.identity.request_expires_at(msg_dt)

        res = self.plugin.identity.request_link(
            a,
            b,
            expires_at,
            created_by=a,
            now_ts=now_ts,
        )
        if res.status == "already_linked":
            return self._format_alt_summary("已存在", a, b)
        if res.status == "linked":
            state = "新增" if res.added_edge else "已存在"
            return self._format_alt_summary(state, a, b)
        if res.status == "pending":
            return (
                f"已发起连接请求: {a} -> {b}\n"
                f"有效期至(UTC): {res.expires_at}\n"
                f"请让对方在有效期内执行: /mem0 link {a}"
            )
        return "连接请求无效。"

    async def _cmd_unlink(
        self, args: list[str], event: AstrMessageEvent
    ) -> str:
        """Handle /mem0 unlink <b>."""
        if len(args) != 1:
            return "用法: /mem0 unlink <b>"
        b = self._parse_user_node(args[0])
        if not b:
            return "只接受完整用户节点: <platform>:user:<id>"
        if self.plugin.identity is None:
            return "身份图未初始化。"

        a = self.plugin.speaker_id(event)
        if a == b:
            return "不能解除同一个节点的关系。"

        msg_dt = self._event_created_at(event)
        now_ts = self.plugin.identity.format_request_time(msg_dt)
        res = self.plugin.identity.unlink_alt(
            a,
            b,
            clear_reverse=False,
            actor=a,
            now_ts=now_ts,
        )
        if res.removed_edge:
            return f"已删除 alt 边: {a} <-> {b}"
        if res.removed_requests > 0:
            return f"已撤回连接请求: {a} -> {b}"
        return f"未找到 alt 边或待确认请求: {a} <-> {b}"

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    async def _cmd_mng_info(self, query: str, event: AstrMessageEvent) -> str:
        """Build /mem0mng info output."""
        query = (query or "").strip()
        if not query:
            return "用法: /mem0mng info <query>"

        group = self.plugin.group_node(event)
        if not group:
            return "仅群聊可用。"

        group_name, members = await self.plugin.fetch_group_profile(
            event, group
        )
        needle = query.casefold()

        candidates: list[tuple[str, str]] = []
        group_name_text = str(group_name) if group_name else ""
        if group_name_text and needle in group_name_text.casefold():
            candidates.append((group_name_text, group))
        for node, name in members:
            if name and needle in str(name).casefold():
                candidates.append((str(name), node))

        seen: set[str] = set()
        lines: list[str] = []
        for name, node in candidates:
            if node in seen:
                continue
            seen.add(node)
            lines.append(f"{name} -> {node}")
            if len(lines) >= 20:
                break

        if not lines:
            return "未找到匹配节点。"
        return "匹配节点:\n" + "\n".join(lines)

    async def _cmd_mng_link(
        self, args: list[str], event: AstrMessageEvent
    ) -> str:
        """Handle /mem0mng link <a> <b> via two-step request matching."""
        if len(args) != 2:
            return "用法: /mem0mng link <a> <b>"
        a = self._parse_user_node(args[0])
        b = self._parse_user_node(args[1])
        if not a or not b:
            return "只接受完整用户节点: <platform>:user:<id>"
        if self.plugin.identity is None:
            return "身份图未初始化。"
        if a == b:
            return "不能连接同一个节点。"

        created_by = self.plugin.speaker_id(event)
        msg_dt = self._event_created_at(event)
        now_ts = self.plugin.identity.format_request_time(msg_dt)
        expires_at = self.plugin.identity.request_expires_at(msg_dt)

        res1 = self.plugin.identity.request_link(
            a,
            b,
            expires_at,
            created_by=created_by,
            now_ts=now_ts,
        )
        if res1.status == "already_linked":
            return self._format_alt_summary("已存在", a, b)
        if res1.status == "linked":
            state = "新增" if res1.added_edge else "已存在"
            return self._format_alt_summary(state, a, b)

        res2 = self.plugin.identity.request_link(
            b,
            a,
            expires_at,
            created_by=created_by,
            now_ts=now_ts,
        )
        state = "新增" if res2.added_edge else "已存在"
        return self._format_alt_summary(state, a, b)

    async def _cmd_mng_unlink(
        self, args: list[str], event: AstrMessageEvent
    ) -> str:
        """Handle /mem0mng unlink <a> <b>."""
        if len(args) != 2:
            return "用法: /mem0mng unlink <a> <b>"
        a = self._parse_user_node(args[0])
        b = self._parse_user_node(args[1])
        if not a or not b:
            return "只接受完整用户节点: <platform>:user:<id>"
        if self.plugin.identity is None:
            return "身份图未初始化。"
        if a == b:
            return "不能解除同一个节点的关系。"

        actor = self.plugin.speaker_id(event)
        msg_dt = self._event_created_at(event)
        now_ts = self.plugin.identity.format_request_time(msg_dt)
        res = self.plugin.identity.unlink_alt(
            a,
            b,
            clear_reverse=True,
            actor=actor,
            now_ts=now_ts,
        )
        if res.removed_edge:
            return f"已删除 alt 边: {a} <-> {b}"
        if res.removed_requests > 0:
            return f"已清理待确认请求: {a} <-> {b}"
        return f"未找到 alt 边: {a} <-> {b}"

    async def _cmd_mng_sync(self, event: AstrMessageEvent) -> str:
        """Handle /mem0mng sync."""
        if self.plugin.identity is None:
            return "身份图未初始化。"
        group = self.plugin.group_node(event)
        if not group:
            return "仅群聊可用。"

        count = await self.plugin.sync_group_members(event, group)
        return f"群成员同步完成: {count} 个节点。"

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    async def handle_mem0(self, event: AstrMessageEvent) -> str:
        """Dispatch member /mem0 subcommands."""
        sub, args = _split_command(event.get_message_str() or "")
        if sub in ("", "me"):
            return await self._cmd_me(event)
        if sub == "group":
            return await self._cmd_group(event)
        if sub == "persona":
            return await self._cmd_persona(event)
        if sub == "link":
            return await self._cmd_link(args, event)
        if sub == "unlink":
            return await self._cmd_unlink(args, event)
        return (
            "用法: /mem0 me | /mem0 group | /mem0 persona | "
            "/mem0 link <b> | /mem0 unlink <b>"
        )

    async def handle_mem0mng(self, event: AstrMessageEvent) -> str:
        """Dispatch admin /mem0mng subcommands."""
        sub, args = _split_command(event.get_message_str() or "")
        if sub == "info":
            return await self._cmd_mng_info(" ".join(args), event)
        if sub == "link":
            return await self._cmd_mng_link(args, event)
        if sub == "unlink":
            return await self._cmd_mng_unlink(args, event)
        if sub == "sync":
            return await self._cmd_mng_sync(event)
        return (
            "用法: /mem0mng info <query> | link <a> <b> | "
            "unlink <a> <b> | sync"
        )
