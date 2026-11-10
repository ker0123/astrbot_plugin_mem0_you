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

import sqlite3
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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
"""

# SQLite builds often default to 999 host parameters.  Chunk IN queries well
# below the limit so large alt components cannot break groups_for_members().
_SQL_VAR_CHUNK = 500


@dataclass(frozen=True)
class Edge:
    """One row of the identity graph."""

    src: str
    dst: str
    kind: str
    created_at: str
    created_by: str | None = None


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
