"""最小 DB-API 2.0 假连接，用来在不连 MySQL 的前提下测 DB 层。

只实现 proto2mysql 用到的那点接口：cursor / execute / fetchall / rowcount /
lastrowid / nextset / commit / rollback / close。

它记录每次 execute 的 (sql, args)，测试据此断言"发出去的语句长什么样"；
返回值由 `queue_rows` 预先排队。
"""

from __future__ import annotations

from typing import Any


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self._rows: list[tuple] = []
        self.rowcount = 0
        self.lastrowid = 0
        self.description = None

    def execute(self, sql: str, args=None) -> None:
        self._conn.executed.append((sql, tuple(args or ())))
        if self._conn.raise_on_execute is not None:
            exc = self._conn.raise_on_execute
            self._conn.raise_on_execute = None
            raise exc
        self._rows = list(self._conn._next_rows())
        self.rowcount = self._conn.next_rowcount
        self.lastrowid = self._conn.next_lastrowid

    def fetchall(self) -> list[tuple]:
        return self._rows

    def nextset(self) -> bool:
        if not self._conn.pending_rows:
            return False
        self._rows = list(self._conn._next_rows())
        return True

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.pending_rows: list[list[tuple]] = []
        self.next_rowcount = 1
        self.next_lastrowid = 0
        self.raise_on_execute: Exception | None = None
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    # ── 测试侧 API ──────────────────────────────────────────────────────

    def queue_rows(self, *result_sets: list[tuple]) -> "FakeConnection":
        """排队若干个结果集，按 execute 顺序依次返回。"""
        self.pending_rows.extend(result_sets)
        return self

    def last_sql(self) -> str:
        return self.executed[-1][0]

    def last_args(self) -> tuple:
        return self.executed[-1][1]

    def _next_rows(self) -> list[tuple]:
        if self.pending_rows:
            return self.pending_rows.pop(0)
        return []

    # ── DB-API ──────────────────────────────────────────────────────────

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeIntegrityError(Exception):
    """模拟驱动抛出的唯一键冲突（错误码在 args[0]，各 DB-API 驱动一致）。"""

    def __init__(self, code: int = 1062, message: str = "Duplicate entry") -> None:
        super().__init__(code, message)
