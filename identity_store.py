"""Identity / visibility graph for mem0 memory scopes.

The graph is stored in a plugin-private SQLite database under AstrBot's
``plugin_data`` directory.

* ``alt``: undirected edge, two accounts belong to the same person.
* ``member_of``: directed ``member -> group``, the member may read the group's
  memories.

Traversal must never follow ``group -> member``: a group node is only a scope
target, never a springboard that reveals other members' personal memories.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - fallback for standalone tests
    logger = logging.getLogger(__name__)

SCHEMA = """
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

CREATE TABLE IF NOT EXISTS link_requests (
    requester TEXT NOT NULL,
    target TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY (requester, target)
);

CREATE INDEX IF NOT EXISTS idx_link_requests_target
    ON link_requests(target);

CREATE INDEX IF NOT EXISTS idx_link_requests_expires_at
    ON link_requests(expires_at);
"""

# SQLite builds often default to 999 host parameters.  Chunk IN queries well
# below the limit so large alt components cannot break groups_for_members().
_SQL_VAR_CHUNK = 500
LINK_REQUEST_TTL_MINUTES = 15
_REQUEST_TS_FORMAT = "%Y-%m-%dT%H-%M-%S"


@dataclass(frozen=True)
class Edge:
    """One row of the identity graph."""

    src: str
    dst: str
    kind: str
    created_at: str
    created_by: str | None = None


@dataclass(frozen=True)
class LinkRequestResult:
    """Result of submitting a link request."""

    status: str  # "linked" | "pending" | "already_linked" | "invalid"
    requester: str
    target: str
    expires_at: str = ""
    added_edge: bool = False


@dataclass(frozen=True)
class UnlinkResult:
    """Result of unlinking two user nodes."""

    removed_edge: bool
    removed_requests: int


class IdentityStore:
    """SQLite adjacency store for alt-account and group-visibility edges.

    Node format: ``<platform>:<kind>:<id>``.

    Edge kinds:

    * ``alt``: undirected, two accounts are the same person.
    * ``member_of``: directed ``member -> group``, the member can see the
      group's memories.
    """

    def __init__(self, path: str | Path) -> None:
        """Open the SQLite database and ensure the schema exists."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _now() -> str:
        """Return an ISO-8601 UTC timestamp."""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def format_request_time(dt: datetime | None = None) -> str:
        """Format a UTC timestamp as YYYY-MM-DDThh-mm-ss."""
        current = dt or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        return current.strftime(_REQUEST_TS_FORMAT)

    @classmethod
    def request_expires_at(
        cls,
        created_at: datetime | None = None,
        ttl_minutes: int = LINK_REQUEST_TTL_MINUTES,
    ) -> str:
        """Compute the expiration timestamp from message creation time."""
        base = created_at or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        else:
            base = base.astimezone(timezone.utc)
        return cls.format_request_time(base + timedelta(minutes=ttl_minutes))

    # ------------------------------------------------------------------
    # Link request table (with pre-read/write expiration cleanup)
    # ------------------------------------------------------------------

    def _purge_expired_link_requests_locked(self, now_ts: str) -> int:
        """Delete expired link requests inside an existing lock."""
        cur = self._conn.execute(
            "DELETE FROM link_requests WHERE expires_at <= ?",
            (now_ts,),
        )
        purged = int(cur.rowcount or 0)
        if purged > 0:
            logger.info(
                "identity_db link_requests purge: removed=%d cutoff=%s",
                purged,
                now_ts,
            )
        return purged

    def _has_alt_edge_locked(self, a: str, b: str) -> bool:
        """Check whether a direct alt edge exists between a and b."""
        src, dst = sorted((a, b))
        row = self._conn.execute(
            """
            SELECT 1 FROM identity_edges
            WHERE src=? AND dst=? AND kind='alt'
            LIMIT 1
            """,
            (src, dst),
        ).fetchone()
        return row is not None

    def request_link(
        self,
        requester: str,
        target: str,
        expires_at: str,
        created_by: str | None = None,
        now_ts: str | None = None,
    ) -> LinkRequestResult:
        """Record a link request and establish an alt edge if reverse exists."""
        req = (requester or "").strip()
        tgt = (target or "").strip()
        actor = (created_by or req).strip()
        exp = (expires_at or "").strip()
        if not req or not tgt or req == tgt or not exp:
            return LinkRequestResult(
                status="invalid",
                requester=req,
                target=tgt,
                expires_at=exp,
            )

        current_ts = now_ts or self.format_request_time()
        with self._lock:
            try:
                self._purge_expired_link_requests_locked(current_ts)

                if self._has_alt_edge_locked(req, tgt):
                    self._conn.commit()
                    logger.info(
                        "identity_db link_requests skip_existing_alt: "
                        "requester=%s target=%s actor=%s",
                        req,
                        tgt,
                        actor,
                    )
                    return LinkRequestResult(
                        status="already_linked",
                        requester=req,
                        target=tgt,
                        expires_at=exp,
                    )

                self._conn.execute(
                    """
                    INSERT INTO link_requests
                        (requester, target, expires_at, created_by)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(requester, target) DO UPDATE SET
                        expires_at = excluded.expires_at,
                        created_by = excluded.created_by
                    """,
                    (req, tgt, exp, actor),
                )
                logger.info(
                    "identity_db link_requests upsert: "
                    "requester=%s target=%s expires_at=%s created_by=%s",
                    req,
                    tgt,
                    exp,
                    actor,
                )

                reverse = self._conn.execute(
                    """
                    SELECT expires_at, created_by FROM link_requests
                    WHERE requester=? AND target=?
                    LIMIT 1
                    """,
                    (tgt, req),
                ).fetchone()

                if reverse is None:
                    self._conn.commit()
                    return LinkRequestResult(
                        status="pending",
                        requester=req,
                        target=tgt,
                        expires_at=exp,
                    )

                src, dst = sorted((req, tgt))
                edge_cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO identity_edges
                        (src, dst, kind, created_at, created_by)
                    VALUES (?, ?, 'alt', ?, ?)
                    """,
                    (src, dst, self._now(), actor),
                )
                added_edge = (edge_cur.rowcount or 0) > 0
                del_cur = self._conn.execute(
                    """
                    DELETE FROM link_requests
                    WHERE (requester=? AND target=?)
                       OR (requester=? AND target=?)
                    """,
                    (req, tgt, tgt, req),
                )
                self._conn.commit()
                logger.info(
                    "identity_db link_requests matched_alt: "
                    "src=%s dst=%s added_edge=%s cleared_requests=%d created_by=%s",
                    src,
                    dst,
                    added_edge,
                    int(del_cur.rowcount or 0),
                    actor,
                )
                return LinkRequestResult(
                    status="linked",
                    requester=req,
                    target=tgt,
                    expires_at=exp,
                    added_edge=added_edge,
                )
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def unlink_alt(
        self,
        a: str,
        b: str,
        *,
        clear_reverse: bool = False,
        actor: str | None = None,
        now_ts: str | None = None,
    ) -> UnlinkResult:
        """Remove an alt edge and clear pending link requests."""
        node_a = (a or "").strip()
        node_b = (b or "").strip()
        if not node_a or not node_b or node_a == node_b:
            return UnlinkResult(removed_edge=False, removed_requests=0)

        current_ts = now_ts or self.format_request_time()
        src, dst = sorted((node_a, node_b))
        by_actor = (actor or node_a).strip()

        with self._lock:
            try:
                self._purge_expired_link_requests_locked(current_ts)

                if clear_reverse:
                    req_cur = self._conn.execute(
                        """
                        DELETE FROM link_requests
                        WHERE (requester=? AND target=?)
                           OR (requester=? AND target=?)
                        """,
                        (node_a, node_b, node_b, node_a),
                    )
                else:
                    req_cur = self._conn.execute(
                        """
                        DELETE FROM link_requests
                        WHERE requester=? AND target=?
                        """,
                        (node_a, node_b),
                    )
                removed_requests = int(req_cur.rowcount or 0)

                edge_cur = self._conn.execute(
                    """
                    DELETE FROM identity_edges
                    WHERE src=? AND dst=? AND kind='alt'
                    """,
                    (src, dst),
                )
                removed_edge = (edge_cur.rowcount or 0) > 0
                self._conn.commit()
                logger.info(
                    "identity_db unlink_alt: src=%s dst=%s removed_edge=%s "
                    "removed_requests=%d clear_reverse=%s actor=%s",
                    src,
                    dst,
                    removed_edge,
                    removed_requests,
                    clear_reverse,
                    by_actor,
                )
                return UnlinkResult(
                    removed_edge=removed_edge,
                    removed_requests=removed_requests,
                )
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def list_link_requests(
        self,
        node: str | None = None,
        now_ts: str | None = None,
    ) -> list[dict[str, str]]:
        """Purge expired requests and return active link requests."""
        current_ts = now_ts or self.format_request_time()
        target_node = (node or "").strip()
        with self._lock:
            self._purge_expired_link_requests_locked(current_ts)
            self._conn.commit()
            if target_node:
                rows = self._conn.execute(
                    """
                    SELECT requester, target, expires_at, created_by
                    FROM link_requests
                    WHERE requester=? OR target=?
                    ORDER BY expires_at ASC, requester ASC, target ASC
                    """,
                    (target_node, target_node),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT requester, target, expires_at, created_by
                    FROM link_requests
                    ORDER BY expires_at ASC, requester ASC, target ASC
                    """
                ).fetchall()
        return [
            {
                "requester": row["requester"],
                "target": row["target"],
                "expires_at": row["expires_at"],
                "created_by": row["created_by"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Edge writes
    # ------------------------------------------------------------------

    def add_alt(self, a: str, b: str, created_by: str | None = None) -> bool:
        """Create (or ignore) an undirected alt-account edge."""
        a, b = (a or "").strip(), (b or "").strip()
        if not a or not b or a == b:
            return False
        src, dst = sorted((a, b))
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO identity_edges
                    (src, dst, kind, created_at, created_by)
                VALUES (?, ?, 'alt', ?, ?)
                """,
                (src, dst, self._now(), created_by),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def remove_alt(self, a: str, b: str) -> bool:
        """Remove an alt-account edge."""
        a, b = (a or "").strip(), (b or "").strip()
        if not a or not b or a == b:
            return False
        src, dst = sorted((a, b))
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM identity_edges WHERE src=? AND dst=? AND kind='alt'",
                (src, dst),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def add_member(self, member: str, group: str, created_by: str | None = None) -> bool:
        """Create (or ignore) a directed member -> group edge."""
        member, group = (member or "").strip(), (group or "").strip()
        if not member or not group or member == group:
            return False
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO identity_edges
                    (src, dst, kind, created_at, created_by)
                VALUES (?, ?, 'member_of', ?, ?)
                """,
                (member, group, self._now(), created_by),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def remove_member(self, member: str, group: str) -> bool:
        """Remove a directed member -> group edge."""
        member, group = (member or "").strip(), (group or "").strip()
        if not member or not group:
            return False
        with self._lock:
            cur = self._conn.execute(
                """
                DELETE FROM identity_edges
                WHERE src=? AND dst=? AND kind='member_of'
                """,
                (member, group),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def replace_group_members(
        self,
        group: str,
        members: Iterable[str],
        created_by: str | None = None,
    ) -> int:
        """Replace the whole member list of one group.

        The caller must only pass a *complete* member list.  Passing a partial
        list would silently remove edges for members that were not returned by
        the platform.

        An empty list is a no-op; callers should use :meth:`add_member` as a
        fallback instead.
        """
        group = (group or "").strip()
        if not group:
            return 0
        cleaned = sorted(_dedupe(m for m in members if (m or "").strip() != group))
        if not cleaned:
            return 0

        now = self._now()
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM identity_edges WHERE kind='member_of' AND dst=?",
                    (group,),
                )
                self._conn.executemany(
                    """
                    INSERT OR IGNORE INTO identity_edges
                        (src, dst, kind, created_at, created_by)
                    VALUES (?, ?, 'member_of', ?, ?)
                    """,
                    [(member, group, now, created_by) for member in cleaned],
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
        return len(cleaned)

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    def identity_component(self, node: str) -> list[str]:
        """Return the alt-account component of node, starting with node."""
        node = (node or "").strip()
        if not node:
            return []

        seen = {node}
        order = [node]
        queue = deque([node])
        with self._lock:
            while queue:
                current = queue.popleft()
                rows = self._conn.execute(
                    """
                    SELECT src, dst FROM identity_edges
                    WHERE kind='alt' AND (src=? OR dst=?)
                    """,
                    (current, current),
                ).fetchall()
                for row in rows:
                    other = row["dst"] if row["src"] == current else row["src"]
                    if other not in seen:
                        seen.add(other)
                        order.append(other)
                        queue.append(other)
        return order

    def groups_for_members(self, members: Iterable[str]) -> list[str]:
        """Return groups visible to any member.

        Ordering is newest *sync* first: ``replace_group_members`` refreshes
        ``created_at`` for every member edge of a group, so the maximum value
        per group reflects the last full member sync, not the join time.
        """
        cleaned = _dedupe(members)
        if not cleaned:
            return []

        last_seen: dict[str, str] = {}
        with self._lock:
            for start in range(0, len(cleaned), _SQL_VAR_CHUNK):
                chunk = cleaned[start : start + _SQL_VAR_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"""
                    SELECT dst, MAX(created_at) AS last_seen
                    FROM identity_edges
                    WHERE kind='member_of' AND src IN ({placeholders})
                    GROUP BY dst
                    """,
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    group = row["dst"]
                    seen_at = row["last_seen"] or ""
                    if group not in last_seen or seen_at > last_seen[group]:
                        last_seen[group] = seen_at

        return [
            group
            for group, _ in sorted(
                last_seen.items(),
                key=lambda item: (item[1], item[0]),
                reverse=True,
            )
        ]

    def members_of_group(self, group: str) -> list[str]:
        """Return the members recorded for one group."""
        group = (group or "").strip()
        if not group:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT src FROM identity_edges
                WHERE kind='member_of' AND dst=?
                ORDER BY src ASC
                """,
                (group,),
            ).fetchall()
        return [row["src"] for row in rows]

    def private_scope(self, node: str) -> list[str]:
        """Return the private-chat query scope.

        ``identity(node) + groups(identity(node))``.  Full expansion is safe in
        private chat because only the person themself can see the result.
        """
        identity = self.identity_component(node)
        if not identity:
            return []
        groups = self.groups_for_members(identity)
        return _dedupe(identity + groups)

    def group_scope(self, group: str, node: str) -> list[str]:
        """Return the group-chat query scope.

        ``[group] + identity(node)``.  Deliberately *not* expanded through the
        speaker's other groups: the audience of this group must not see
        memories from groups they do not belong to.
        """
        group = (group or "").strip()
        identity = self.identity_component(node)
        if not group:
            return identity
        return _dedupe([group] + identity)

    def edges_for(self, node: str) -> list[Edge]:
        """Return edges touching one node."""
        node = (node or "").strip()
        if not node:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT src, dst, kind, created_at, created_by
                FROM identity_edges
                WHERE src=? OR dst=?
                ORDER BY kind, src, dst
                """,
                (node, node),
            ).fetchall()
        return [
            Edge(
                src=row["src"],
                dst=row["dst"],
                kind=row["kind"],
                created_at=row["created_at"],
                created_by=row["created_by"],
            )
            for row in rows
        ]

    def stats(self) -> dict[str, int]:
        """Return edge counts by kind."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM identity_edges GROUP BY kind"
            ).fetchall()
        result = {"alt": 0, "member_of": 0}
        for row in rows:
            result[row["kind"]] = int(row["n"])
        return result


def _dedupe(items: Iterable[str]) -> list[str]:
    """Deduplicate while preserving first-seen order."""
    return [item for item in dict.fromkeys(items) if item]
