"""最小 DB-API 2.0 假连接，用来在不连 MySQL 的前提下测 DB 层。

只实现 proto2mysql 用到的那点接口：cursor / execute / fetchall / rowcount /
lastrowid / nextset / commit / rollback / close。

它记录每次 execute 的 (sql, args)，测试据此断言"发出去的语句长什么样"；
返回值由 `queue_rows` 预先排队。
"""

from __future__ import annotations

class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self._rows: list[tuple] = []
        self.rowcount = 0
        self.lastrowid = 0
        self.description = None

    def execute(self, sql: str, args=None) -> None:
        self._conn.executed.append((sql, tuple(args or ())))
        self._conn.execute_calls += 1
        if self._conn.raise_on_execute_number is not None:
            number, exc = self._conn.raise_on_execute_number
            if self._conn.execute_calls == number:
                self._conn.raise_on_execute_number = None
                raise exc
        if self._conn.raise_on_sql is not None:
            prefix, exc = self._conn.raise_on_sql
            if sql.startswith(prefix):
                self._conn.raise_on_sql = None
                raise exc
        if self._conn.raise_on_execute is not None:
            exc = self._conn.raise_on_execute
            self._conn.raise_on_execute = None
            raise exc
        self._rows = list(self._conn._next_rows())
        self.rowcount = self._conn._next_rowcount()
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
    #: 这个假连接默认按 **autocommit=True** 表现。
    #:
    #: 不是随手挑的默认：DB 判断"在不在事务里"看的是连接的 autocommit
    #: （见 DB._connection_in_transaction）。读不到 autocommit 的连接会被当成
    #: "一直在隐式事务里"，于是缓存读路径整体降级——那样缓存相关的用例就全都
    #: 测不到缓存本身了。要测隐式事务的行为，把实例上的 autocommit 改成 False。
    autocommit = True

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.pending_rows: list[list[tuple]] = []
        self.pending_rowcounts: list[int] = []
        self.next_rowcount = 1
        self.next_lastrowid = 0
        self.raise_on_execute: Exception | None = None
        self.raise_on_sql: tuple[str, Exception] | None = None
        self.raise_on_execute_number: tuple[int, Exception] | None = None
        self.raise_on_commit: Exception | None = None
        self.execute_calls = 0
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        # MySQL 协议 SERVER_STATUS_AUTOCOMMIT；begin() 时再加 IN_TRANS。
        self.server_status = 0x0002

    # ── 测试侧 API ──────────────────────────────────────────────────────

    def queue_rows(self, *result_sets: list[tuple]) -> "FakeConnection":
        """排队若干个结果集，按 execute 顺序依次返回。"""
        self.pending_rows.extend(result_sets)
        return self

    def queue_rowcounts(self, *rowcounts: int) -> "FakeConnection":
        """按 execute 顺序排队 RowsAffected；未排队时仍用 ``next_rowcount``。"""
        self.pending_rowcounts.extend(rowcounts)
        return self

    def last_sql(self) -> str:
        return self.executed[-1][0]

    def last_args(self) -> tuple:
        return self.executed[-1][1]

    def _next_rows(self) -> list[tuple]:
        if self.pending_rows:
            return self.pending_rows.pop(0)
        return []

    def _next_rowcount(self) -> int:
        if self.pending_rowcounts:
            return self.pending_rowcounts.pop(0)
        return self.next_rowcount

    # ── DB-API ──────────────────────────────────────────────────────────

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def begin(self) -> None:
        self.begins += 1
        self.server_status |= 0x0001

    def commit(self) -> None:
        self.commits += 1
        self.server_status &= ~0x0001
        if self.raise_on_commit is not None:
            exc = self.raise_on_commit
            self.raise_on_commit = None
            raise exc

    def rollback(self) -> None:
        self.rollbacks += 1
        self.server_status &= ~0x0001

    def close(self) -> None:
        self.closed = True


class FakeIntegrityError(Exception):
    """模拟驱动抛出的唯一键冲突（错误码在 args[0]，各 DB-API 驱动一致）。"""

    def __init__(self, code: int = 1062, message: str = "Duplicate entry") -> None:
        super().__init__(code, message)
