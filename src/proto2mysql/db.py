"""连库执行层：注册表、建表/迁移、CRUD。

对应 Go 版的 proto2mysql.go（*DB 那一半）。

与 Go 版的三处结构性差异，都是语言/生态决定的，不是取舍失误：

1. **连接不是池。**
   Go 的 ``*sql.DB`` 本身是并发安全的连接池，一个实例全进程共用。Python 的 DB-API
   连接是单连接且**不是线程安全**的。所以 :class:`DB` 绑定一个连接，
   多线程/多请求请各自 :meth:`bind` 一个连接（表注册表是共享的，不重复解析描述符）。

2. **占位符。**
   生成的 SQL 一律用 ``?``（与 Go 一致，黄金用例可跨语言逐字节比对），
   执行前由 :meth:`Statement.for_paramstyle` 转成驱动要的 ``%s``。
   构造 ``DB(..., paramstyle="qmark")`` 可用于 SQLite 之类。

3. **不区分 GormDB。**
   Go 版为 gorm 复制了一整套 CRUD（proto2gorm.go，949 行）。Python 侧不需要：
   本层只要求"能给出 DB-API cursor"的对象，SQLAlchemy 用
   ``engine.raw_connection()`` 直接传进来即可，不必再写一份。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field as _dc_field
from types import ModuleType
from typing import Any, Iterable, Iterator, Sequence

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.message import Message

from . import pbconv
from .cache import Cache, decode_entry, encode_entry
from .errors import (
    CacheMissError,
    DuplicateKeyError,
    MultipleRepeatedFieldError,
    MultipleRowsFoundError,
    NoRepeatedFieldError,
    NoRowsFoundError,
    PrimaryKeyNotFoundError,
    Proto2MySQLError,
    TableNotFoundError,
)
from .options import TableOption, file_has_db_option, table_name_from_descriptor
from .registry import iter_file_descriptors, iter_messages
from .sqlbuilder import QueryOptions, SQLBuilder, normalize_where_clause
from .table import (
    BATCH_INSERT_MAX_SIZE,
    TEXT_INDEX_PREFIX_LENGTH,
    ColumnMeta,
    MessageTable,
    Statement,
    build_placeholders,
    escape_mysql_name,
    index_name_for,
    parse_field_num_from_comment,
    unique_key_name_for,
)

log = logging.getLogger("proto2mysql")

_MYSQL_DUPLICATE_ENTRY = 1062

#: 当前已经开着事务的连接：``id(connection) -> connection``。
#:
#: 「在不在事务里」是**连接**的状态，不是 DB 实例的状态。只在实例上记
#: ``_in_transaction`` 拦不住最常见的那种写法——外层事务还开着，
#: 又拿**原始的 db**（而不是 tx）再开一个：那个 db 上的标志一直是 False，
#: 于是内层照常 begin()，而 MySQL 的 BEGIN 会**隐式提交**外层还没提交的改动，
#: 内层退出时又 commit() 一次。外层随后 rollback() 什么也回滚不了，全程零报错。
#: bind() 出来的另一个实例共用同一个连接时同理。
#:
#: 值是 ``(connection, pending_cache_dels, cache_backend)``。连接持强引用是为了杜绝
#: id 被回收后复用
#: （那会让一个新连接被误判成"已在事务中"）；pending 列表是**这条连接上这个事务**
#: 共用的那一份——在 with 块里拿父对象写时，失效也必须挂进它。否则父对象把 key 挂在
#: 自己的 _pending_cache_dels 上，而提交后冲刷的是 tx 那一份，于是那些 key 永远没人删。
#: cache_backend 也固定在事务开始时：同连接另一个 wrapper 若换成别的 backend，提交者
#: 无法替它冲刷，因此执行入口会 fail-closed。
#: 退出时在 finally 里摘除，不会长期滞留。不挂到连接对象的属性上、也不用 weakref：
#: 两者都不是所有驱动的连接对象都支持（sqlite3.Connection 两样都不行）。
_ACTIVE_TX_CONNECTIONS: dict[int, tuple[Any, list[str], Cache | None]] = {}

#: 缓存 key 里要不要带**库名**命名空间。
#:
#: 默认必须带库名。不带时，两个库共用 Redis 会让同表同主键落在同一个 key 上，
#: 直接把 A 库的整条 protobuf 返回给 B 库——这是数据隔离问题，不能为了兼容旧 key
#: 把不安全行为继续留作默认。滚动升级时新版本**只读新 key**，写后会同时删除旧 key；
#: 因而不会读到旧格式的跨库条目，又能逐步清掉旧进程留下的脏值。
#:
#: ``False`` 只保留给已经证明每个数据库独占缓存、且必须与未升级 Go 进程共享 key 的
#: 迁移窗口。它是显式的不安全兼容开关，不再是默认值。
CACHE_KEY_NAMESPACED = True


def _escape_cache_key_part(value: str) -> str:
    """转义缓存 key 分量里的分隔符：``%`` -> ``%25``、``:`` -> ``%3A``。

    不转义的话，两个不同的复合主键会得到同一个 key：

        ("x:y", "z")  ->  pb:t:x:y:z
        ("x", "y:z")  ->  pb:t:x:y:z     <- 同一个 key

    命中时返回的是另一行的**整条 protobuf**（含主键），而且两边互相投毒——
    这是静默的跨行脏读，库里数据自始至终是对的，零日志零异常。
    字符串主键（订单号、外部账号 ID、复合业务键）里带冒号一点都不罕见。

    只动 ``%`` 和 ``:`` 两个字符，其余分量原样；默认的数据库 namespace 仍会让
    所有新 key 与旧版分开（这是避免跨库读到旧脏值所必需的冷切换）。只有显式启用
    不安全兼容模式时，不含这两个字符的主键 key 才会与旧版逐字节相同。
    与 Go 的 escapeCacheKeyPart 逐字对应。
    """
    if "%" not in value and ":" not in value:
        return value
    return value.replace("%", "%25").replace(":", "%3A")


def _parse_index_rows(rows: Sequence[tuple]) -> dict[str, "IndexMeta"]:
    """把 information_schema.STATISTICS 的行归并成 {小写索引名: IndexMeta}。

    刻意**不**放进调用方那个 try/except 里：行形状不对是代码或驱动的问题，
    把它吞成"线上一个索引都没有"会变成一串莫名其妙的 ADD INDEX。
    """
    out: dict[str, IndexMeta] = {}
    for name, non_unique, _seq, col, sub_part in rows:
        if not name or name.upper() == "PRIMARY":
            continue
        # 索引名与列名在 MySQL 里都不区分大小写，统一小写再比
        meta = out.setdefault(name.lower(), IndexMeta())
        meta.unique = not int(non_unique or 0)
        meta.columns.append(((col or "").lower(), int(sub_part) if sub_part else None))
    return out


@dataclass
class IndexMeta:
    """线上单个索引的结构（供 DB._existing_indexes 使用）。

    columns 是 ``[(小写列名, 前缀长度或 None), ...]``，按 SEQ_IN_INDEX 排序——
    **列序是索引语义的一部分**，(a,b) 与 (b,a) 是两个不同的索引。
    """

    unique: bool = False
    columns: list[tuple[str, int | None]] = _dc_field(default_factory=list)


def _wrap_exec_error(exc: Exception) -> Exception:
    """把 MySQL 1062（唯一键冲突）包装成可捕获的 DuplicateKeyError。

    对应 Go 的 wrapExecErr。DB-API 的异常里错误码在 ``args[0]``，各驱动一致。
    """
    args = getattr(exc, "args", ())
    if args and args[0] == _MYSQL_DUPLICATE_ENTRY:
        return DuplicateKeyError(str(exc))
    return exc


class DB:
    """一个连接 + 一份表注册表。"""

    def __init__(
        self,
        connection: Any = None,
        dbname: str = "",
        *,
        paramstyle: str = "format",
        tables: dict[str, MessageTable] | None = None,
        cache: Cache | None = None,
        cache_ttl: float | None = None,
        expand_only: bool = False,
        _in_transaction: bool = False,
    ) -> None:
        self.connection = connection
        self.dbname = dbname
        self.paramstyle = paramstyle
        #: proto full name -> MessageTable。注册键固定为 full name，
        #: table.table_name 只决定生成 SQL 里的表名。
        self.tables: dict[str, MessageTable] = tables if tables is not None else {}
        self._cache = cache
        self._cache_ttl = cache_ttl
        #: 只允许「纯新增」的结构变更。默认关（保持既有行为，改名照常保留数据）。
        #: **滚动 / 金丝雀发布必须打开**，理由见 MessageTable.build_alter_clauses。
        self.expand_only = expand_only
        self._in_transaction = _in_transaction
        self._pending_cache_dels: list[str] = []
        self._table_exists_cache: dict[str, bool] = {}
        # 列元数据也属于具体数据库，不能放在共享的 MessageTable 上：bind() 出来的
        # 两个 wrapper 可能连不同库，共享会把 A 库的列结构直接返回给 B 库。
        self._table_columns_cache: dict[str, dict[str, str]] = {}

    # ── 连接 ────────────────────────────────────────────────────────────

    def open_db(self, connection: Any, dbname: str) -> "DB":
        """绑定连接并切库（对应 Go 的 OpenDB）。"""
        if (
            self.connection is not None
            and id(self.connection) in _ACTIVE_TX_CONNECTIONS
        ) or id(connection) in _ACTIVE_TX_CONNECTIONS:
            raise Proto2MySQLError(
                "managed transaction 内不能重新绑定连接；请先退出 transaction()"
            )
        if self._cache is not None:
            _require_cache_namespace(dbname)
            _require_cache_autocommit(connection)
        # 先在目标连接上成功 USE，再发布新状态。反过来先改 self.connection/dbname，
        # USE 一旦失败，wrapper 的 cache namespace 已是目标库、连接却仍停在原默认库，
        # 调用方若捕获异常继续使用就会把 SQL 发到错误数据库。
        probe = DB(connection, dbname, paramstyle=self.paramstyle)
        probe.execute(f"USE {escape_mysql_name(dbname)}")
        self.connection = connection
        self.dbname = dbname
        self._table_exists_cache.clear()
        self._table_columns_cache.clear()
        return self

    def bind(self, connection: Any) -> "DB":
        """返回绑定新连接的实例，**共享表注册表与缓存配置**。

        多线程/多请求的正确用法：进程启动时注册一次表，之后每个连接 bind 一下。
        """
        if self._cache is not None:
            # enable_cache() 时还没有连接是合法的；真正 bind 时再做硬门禁。
            _require_cache_namespace(self.dbname)
            _require_cache_autocommit(connection)
        return DB(
            connection,
            self.dbname,
            paramstyle=self.paramstyle,
            tables=self.tables,
            cache=self._cache,
            cache_ttl=self._cache_ttl,
            expand_only=self.expand_only,
        )

    def commit(self) -> None:
        """提交连接；若当前对象持有保守失效项，则无论 ACK 是否成功都冲刷。"""
        if self.connection is not None and id(self.connection) in _ACTIVE_TX_CONNECTIONS:
            raise Proto2MySQLError(
                "managed transaction 内不能手动 commit；请退出 with db.transaction() 统一提交"
            )
        try:
            self.connection.commit()
        finally:
            # 与 transaction() 尾部同一条理由：COMMIT 抛异常不等于事务没提交。
            keys, self._pending_cache_dels = self._pending_cache_dels, []
            self._cache_del_keys(*keys)

    def rollback(self) -> None:
        """回滚连接上的事务，并**丢弃**挂起的缓存失效（回滚了就不该删还有效的缓存）。"""
        if self.connection is not None and id(self.connection) in _ACTIVE_TX_CONNECTIONS:
            raise Proto2MySQLError(
                "managed transaction 内不能手动 rollback；请抛出异常让 transaction() 统一回滚"
            )
        try:
            self.connection.rollback()
        finally:
            self._pending_cache_dels.clear()

    def close(self) -> None:
        if self.connection is not None and id(self.connection) in _ACTIVE_TX_CONNECTIONS:
            raise Proto2MySQLError(
                "managed transaction 内不能关闭连接；请先退出 transaction()"
            )
        if self.connection is not None:
            self.connection.close()

    # ── 语句执行 ────────────────────────────────────────────────────────

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        if self.connection is None:
            raise Proto2MySQLError("no connection bound; call open_db()/bind() first")
        cur = self.connection.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def execute(self, sql: str, args: Sequence[Any] | None = None) -> int:
        """执行一条语句，返回受影响行数。"""
        stmt = Statement(sql, list(args or []))
        query, params = stmt.for_paramstyle(self.paramstyle)
        with self._cursor() as cur:
            try:
                cur.execute(query, params)
            except Exception as exc:  # noqa: BLE001 - 统一包装后原样抛出
                raise _wrap_exec_error(exc) from exc
            return cur.rowcount

    def _exec_stmt(self, stmt: Statement) -> int:
        return self.execute(stmt.sql, stmt.args)

    def _exec_returning_lastrowid(self, stmt: Statement) -> int:
        query, params = stmt.for_paramstyle(self.paramstyle)
        with self._cursor() as cur:
            try:
                cur.execute(query, params)
            except Exception as exc:  # noqa: BLE001
                raise _wrap_exec_error(exc) from exc
            return cur.lastrowid

    def query(self, sql: str, args: Sequence[Any] | None = None) -> list[tuple]:
        """执行查询，返回全部行。"""
        stmt = Statement(sql, list(args or []))
        query, params = stmt.for_paramstyle(self.paramstyle)
        with self._cursor() as cur:
            cur.execute(query, params)
            return list(cur.fetchall())

    def query_one_value(self, sql: str, args: Sequence[Any] | None = None) -> Any:
        """执行查询并取第一行第一列（无行时返回 None）。"""
        rows = self.query(sql, args)
        return rows[0][0] if rows else None

    # ── 事务 ────────────────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator["DB"]:
        """在事务中执行：退出时提交，抛异常则回滚。

            with db.transaction() as tx:
                tx.decr_by_pk_if_enough(row, "gold", 100)
                tx.insert(log_row)

        启用缓存时，事务内的缓存失效会延迟处理：回滚不删；一旦尝试提交就保守删除。
        后者覆盖“服务端已提交、ACK 丢失、客户端只看到异常”的不确定提交结果。
        """
        if self.connection is None:
            raise Proto2MySQLError("no connection bound; call open_db()/bind() first")
        if self._in_transaction or id(self.connection) in _ACTIVE_TX_CONNECTIONS:
            raise Proto2MySQLError("nested transaction is not supported")
        if _connection_autocommit(self.connection) and _connection_server_in_transaction(
            self.connection
        ):
            raise Proto2MySQLError(
                "连接已处于包装器之外开启的外部事务；再次 transaction() 会隐式提交它。"
                "请先 rollback/commit，再只用 db.transaction() 管理原子区间"
            )

        connection = self.connection
        tx = DB(
            connection,
            self.dbname,
            paramstyle=self.paramstyle,
            tables=self.tables,
            cache=self._cache,
            cache_ttl=self._cache_ttl,
            expand_only=self.expand_only,
            _in_transaction=True,
        )
        # 只在 autocommit=True 时才显式 begin()。
        #
        # 这条不能照直觉写成"总是 begin()"：PyMySQL 的 begin() 就是往服务端发 BEGIN，
        # 而 MySQL 的 BEGIN 会**隐式提交**当前连接上还没提交的改动。autocommit=False
        # 时连接在第一条语句后就已经在事务里了，此时再 begin()，前面那些还没提交的写
        # 会被静默提交掉——回滚也救不回来，且全程无任何报错。
        #
        # 反过来 autocommit=True 时不 begin() 更糟：每条语句各自提交，
        # 出错时 rollback() 什么也回滚不了，事务形同虚设。所以必须按实际模式分流。
        conn_key = id(connection)
        # pending 列表就是 tx 自己那一份：登记进去之后，任何持有同一条连接的 DB 对象
        # （包括父对象）写完都会把失效挂到这里，提交尝试结束后统一冲刷。
        _ACTIVE_TX_CONNECTIONS[conn_key] = (
            connection,
            tx._pending_cache_dels,
            self._cache,
        )
        try:
            if _connection_autocommit(connection):
                begin = getattr(connection, "begin", None)
                if callable(begin):
                    begin()
                else:
                    # DB-API 2.0 不要求连接暴露 begin()。静默跳过会让 autocommit
                    # 连接里的每条语句分别提交，异常后的 rollback 完全无效。
                    # 目标后端是 MySQL/TiDB，标准 SQL 入口可可靠开启事务。
                    self.execute("START TRANSACTION")
            try:
                yield tx
            except BaseException:
                # 必须是 BaseException 而不是 Exception：KeyboardInterrupt /
                # GeneratorExit 走不到 Exception 分支，连接会被留在一个**开着的事务**里，
                # 而下面的 finally 已经把它从登记表里摘牌——于是下一个 transaction()
                # 被放行，autocommit=True 时它第一句就是 begin()，
                # 而 MySQL 的 BEGIN 会**隐式提交**前一笔没回滚的写。
                try:
                    connection.rollback()
                except Exception as exc:  # noqa: BLE001
                    # rollback 自己抛异常时**不能**顶掉原始异常：调用方要看到的是
                    # 业务失败的那一条，而不是"回滚也失败了"。
                    # 连接断开时服务端会自动回滚，所以这里降级成一条告警是安全的。
                    log.warning("回滚失败（连接断开时服务端会自动回滚）：%s", exc)
                raise
            try:
                connection.commit()
            finally:
                # 失效放在 finally 里：**COMMIT 抛异常不等于事务没提交**。
                # 服务端写完并落盘之后、ACK 回到客户端之前断线，库里已经是新值，
                # 客户端只看到一个异常。此时若跳过失效，就是"数据库新值 + 缓存旧值"，
                # 一直脏到 TTL 到期，而且没有任何日志指向它。
                # 删缓存是幂等的，最坏只是多一次回源——这条路上必须删。
                self._cache_del_keys(*tx._pending_cache_dels)
        finally:
            # pop 而不是 del：登记与摘除之间如果有人（比如另一个线程用同一条连接）
            # 抢先摘掉了，del 会从 finally 里抛一个裸 KeyError，
            # 把事务体里真正的异常顶掉。
            _ACTIVE_TX_CONNECTIONS.pop(conn_key, None)

    # ── 缓存 ────────────────────────────────────────────────────────────

    def enable_cache(self, cache: Cache, ttl: float | None = None) -> None:
        """启用 cache-aside 缓存（按主键的单行读写生效）。

        语义：

        * 读（:meth:`find_one_by_pk` / :meth:`find_or_create` 命中路径）：
          先查缓存，未命中读 DB 后回填；
        * 写（按主键的 save/update/delete/incr 等）：先写 DB，成功后删缓存；
        * 事务：回滚不删；一旦尝试提交就保守删除（包括 COMMIT ACK 丢失）；
        * 降级：缓存出错**仅记日志**，不影响 DB 结果（弱依赖）。

        注意：按 WHERE 条件的更新/删除无法定位受影响主键，**不做缓存失效**；
        缓存表请优先用按主键的接口，或调用 :meth:`invalidate_cache` 手动失效。
        """
        if self.connection is not None and id(self.connection) in _ACTIVE_TX_CONNECTIONS:
            raise Proto2MySQLError(
                "managed transaction 内不能启用或切换 cache backend；请先退出 transaction()"
            )
        if self.connection is not None:
            _require_cache_namespace(self.dbname)
            _require_cache_autocommit(self.connection)
        self._cache = cache
        self._cache_ttl = ttl

    def _cache_enabled(self) -> bool:
        if self._cache is None:
            return False
        # 不能只在 enable/bind 时检查：驱动允许调用方之后动态切换 autocommit。
        # 一旦切回隐式事务，直接 conn.commit() 的提交时刻不经过 DB，库无法可靠地做
        # 提交后第二次失效；继续用缓存会留下永久旧值，所以运行期也 fail-closed。
        _require_cache_autocommit(self.connection)
        return True

    def _connection_in_transaction(self) -> bool:
        """这条连接现在是不是在一个事务里——**不能只看 self._in_transaction**。

        ``_in_transaction`` 只说明"这个 DB 对象是 transaction() 产出的那个"，
        而事务是**连接**的属性。三条判据缺一不可：

        1. 这个对象自己就是 tx；
        2. 这条连接上有别人开着事务——``with db.transaction()`` 块里拿**父对象** db
           读写走的是同一个连接同一个事务，而父对象的标志一直是 False；
        3. 连接不是 autocommit（PyMySQL 的默认），那它从第一条语句起就一直在
           一个隐式事务里，哪怕调用方从没用过 transaction()。

        漏掉任何一条，都会把**未提交**的值当成已提交的值缓存下来。
        """
        return (
            self._in_transaction
            or id(self.connection) in _ACTIVE_TX_CONNECTIONS
            or _connection_server_in_transaction(self.connection)
            or not _connection_autocommit(self.connection)
        )

    def cache_key(self, message: Message) -> str:
        """message 对应的缓存 key：``pb:<库名>:<表名>:<主键值...>``

        ⚠️ **schema 版本刻意不进 key，而是进 value 的头部**（见 cache.encode_entry）。

        看起来把指纹拼进 key 更省事——新旧版本天然不共享条目、投毒路径直接消失。
        但失效路径与读路径共用本函数：v1 写库后去删的是**自己那个指纹**的 key，
        v2 缓存的那条永远没人删，于是「读到残缺数据」被升级成
        「跨版本永久脏读」，一直脏到 TTL 到期。比原来的问题更糟。

        放进 value 头部则：key 不变 → 失效跨版本照常生效；读时做超集判定 →
        只有「认识更多字段的一方读到认识更少的一方写的条目」才 miss（单向），
        雪崩面小，而且是原地覆写，不会把旧 key 空间搁浅占内存。
        """
        return self._cache_key_for(self._table_for_message(message), message)

    def _cache_key_for(self, table: MessageTable, message: Message) -> str:
        return self._cache_key_for_values(table, table.primary_key_values(message))

    def _cache_key_for_values(self, table: MessageTable, values: Sequence[Any]) -> str:
        """由主键值拼出缓存 key：``pb:[<库名>:]<表名>:<主键值...>``。

        分量都过一遍 :func:`_escape_cache_key_part`（那里写了为什么必须转义分隔符）。
        库名默认进入 key；迁移期开关及双删策略见 :data:`CACHE_KEY_NAMESPACED`。
        """
        parts = "".join(f":{_escape_cache_key_part(str(v))}" for v in values)
        table_name = _escape_cache_key_part(table.table_name)
        prefix = (
            f"pb:{_escape_cache_key_part(self.dbname)}"
            if CACHE_KEY_NAMESPACED
            else "pb"
        )
        return f"{prefix}:{table_name}{parts}"

    @staticmethod
    def _compat_cache_key_for_values(table: MessageTable, values: Sequence[Any]) -> str:
        """迁移期开关产出的无库名、但已转义 key；只用于兼容删除。"""
        parts = "".join(f":{_escape_cache_key_part(str(v))}" for v in values)
        return f"pb:{_escape_cache_key_part(table.table_name)}{parts}"

    @staticmethod
    def _legacy_cache_key_for_values(table: MessageTable, values: Sequence[Any]) -> str:
        """升级前的无库名 key；只用于写后兼容删除，绝不用于读取。"""
        # 必须逐字复刻旧算法：旧版连 ':' / '%' 都没转义。若在这里套新转义，
        # 恰好最危险的那些历史碰撞 key 会永远删不到。
        parts = "".join(f":{v}" for v in values)
        return f"pb:{table.table_name}{parts}"

    def _invalidation_keys_for(self, table: MessageTable, message: Message) -> list[str]:
        return self._invalidation_keys_for_values(table, table.primary_key_values(message))

    def _invalidation_keys_for_values(
        self, table: MessageTable, values: Sequence[Any]
    ) -> list[str]:
        current = self._cache_key_for_values(table, values)
        compat = self._compat_cache_key_for_values(table, values)
        legacy = self._legacy_cache_key_for_values(table, values)
        # 保序去重：默认安全 key、迁移期开关 key、最老的裸 key 三种都删。
        # 含 ':' / '%' 的复合键会同时存在后两种格式，少删一种都会让旧进程继续读脏。
        return list(dict.fromkeys((current, compat, legacy)))

    def invalidate_cache(self, *messages: Message) -> None:
        """手动失效一批消息的缓存（按 WHERE 批量写后调用）。

        managed transaction 内与自动写后失效使用同一份 pending 队列：回滚不删，
        一旦尝试提交再删，避免 COMMIT 前的并发读者把旧值回填后永久留存。
        """
        if self._cache is None or not messages:
            return
        self._require_managed_cache_backend()
        keys: list[str] = []
        for m in messages:
            table = self._table_by_key(m.DESCRIPTOR.full_name)
            keys.extend(self._invalidation_keys_for(table, m))
        self._queue_invalidation(keys)

    def _cache_get_proto(self, table: MessageTable, message: Message) -> bool:
        """读缓存并反序列化到 message；返回是否命中。任何错误都视为未命中（降级）。"""
        try:
            key = self._cache_key_for(table, message)
        except Proto2MySQLError:
            return False
        try:
            data = self._cache.get(key)
        except CacheMissError:
            return False
        except Exception as exc:  # noqa: BLE001 - 缓存是弱依赖，出错只降级
            log.warning("cache get %s failed (fallback to db): %s", key, exc)
            return False

        entry = decode_entry(data)
        if entry is None:
            # 没有信封：要么是老版本写的裸 pb 字节，要么根本不是本库写的。
            # 一律当未命中回源——下一次写会把它原地覆盖成带信封的条目。
            log.warning("cache entry %s has no envelope (fallback to db)", key)
            return False
        writer_fields, payload = entry
        if not writer_fields >= table.field_numbers:
            # 写入方认识的字段比我少，这条记录对我来说是**残缺**的。
            # 直接用会让我新增的那些字段静默拿到零值，而库里其实是有值的。
            log.warning(
                "cache entry %s was written by an older schema "
                "(missing pb:%s); falling back to db",
                key, sorted(table.field_numbers - writer_fields),
            )
            return False

        # 解析到临时对象再拷回去：原先是先 Clear() 再 ParseFromString()，
        # 解析一旦失败，调用方 message 的**主键已经被清成 0**，
        # 上层拿着 0 回去查库，于是查错行/查不到，且看不出根因。
        scratch = table.new_message()
        try:
            scratch.ParseFromString(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache unmarshal %s failed (fallback to db): %s", key, exc)
            return False
        message.Clear()
        message.MergeFrom(scratch)
        return True

    def _cache_set_proto(self, table: MessageTable, message: Message) -> None:
        try:
            key = self._cache_key_for(table, message)
        except Proto2MySQLError:
            return
        try:
            payload = encode_entry(table.field_numbers, message.SerializeToString())
            self._cache.set(key, payload, self._cache_ttl)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache set %s failed: %s", key, exc)

    def _cache_del_keys(self, *keys: str) -> None:
        # 写后失效不能再依赖 DB 连接状态：statement/commit ACK 丢失时连接可能已断，
        # 但缓存仍是可用的独立边界，而且这恰好是最必须保守删除的路径。
        if self._cache is None or not keys:
            return
        try:
            self._cache.delete(*keys)
        except Exception as exc:  # noqa: BLE001 - 删不掉只会脏读到 TTL 为止
            log.warning("cache del %s failed (stale until ttl): %s", keys, exc)

    def _invalidate_messages(self, table: MessageTable, *messages: Message) -> None:
        """写 DB 成功后失效缓存：事务内先暂存，尝试提交后统一删除。

        **本函数自己不抛**。它被放在若干 ``finally`` 里，一旦抛出就会顶掉原始异常，
        让调用方看到一个缓存报错、而真正失败的是那条写语句。缓存是弱依赖，
        这条约束该由函数自己守住，而不是靠每个调用点凑巧都安全。
        """
        if self._cache is None:
            return
        keys = []
        for msg in messages:
            try:
                keys.extend(self._invalidation_keys_for(table, msg))
            except Exception:  # noqa: BLE001 - 见 docstring：绝不能顶掉原始异常
                continue  # 无主键的表不参与缓存
        self._queue_invalidation(keys)

    def _queue_invalidation(self, keys: list[str]) -> None:
        """按当前事务状态决定这些 key 是现在删、还是等提交后删。"""
        if not keys:
            return
        active = _ACTIVE_TX_CONNECTIONS.get(id(self.connection))
        if active is not None:
            # 这条连接上有事务开着（不管是 self 还是父对象发起的）：延到提交尝试之后
            # 再删，回滚就不删（否则会误删还有效的缓存）。**必须挂进那个事务共用的
            # 那一份 pending**——挂到 self 自己身上的话，提交后冲刷不到，
            # 而删除又已经跳过了，结果是库新值、缓存旧值，一直脏到 TTL。
            active[1].extend(keys)
            return
        self._cache_del_keys(*keys)

    def _require_managed_cache_backend(self) -> None:
        """同连接事务内的所有 wrapper 必须使用事务开始时固定的 cache backend。"""
        active = _ACTIVE_TX_CONNECTIONS.get(id(self.connection))
        if active is not None and self._cache is not active[2]:
            raise Proto2MySQLError(
                "managed transaction 内不能混用不同的 cache backend；"
                "请从 transaction() 返回的 tx 或相同缓存配置的 wrapper 执行"
            )

    # ── 注册 ────────────────────────────────────────────────────────────

    def register_table(
        self, m: Message | type[Message], *opts: TableOption
    ) -> MessageTable:
        """注册消息与表的映射。

        表配置优先从 proto 的 message/field option 读取，通常无需传任何 TableOption；
        显式传入的 opts 可覆盖 proto 里的声明。

        注册键固定为 **proto full name**；``table.table_name`` 只决定生成 SQL 中的表名。
        """
        table = MessageTable.from_message(m, opts)
        self.tables[table.descriptor.full_name] = table
        return table

    def register_all_tables(
        self, modules: Iterable[ModuleType] | None = None
    ) -> list[str]:
        """扫描进程内已加载的 proto 描述符，自动注册所有"用于建表"的消息。

        一个消息只有**同时**满足两个条件才会被注册：

        1. 所在 .proto 文件声明了 ``option (proto2mysql.db) = true;``
        2. 该 message 自身声明了 ``option (proto2mysql.table_name) = "...";``

        即：db 文件选项圈定"哪些文件参与建表"，table_name 决定"文件里哪些 message 建表"。

        前提是这些 .proto 生成的 ``_pb2`` 模块**已经被 import**（与 Go 侧
        "包必须链接进二进制"是同一个前提）。可用 ``modules`` 显式点名，
        避免依赖 import 副作用。返回被注册的表名（proto full name）列表。
        """
        registered: list[str] = []
        for fd in iter_file_descriptors(modules):
            if not file_has_db_option(fd):
                continue
            for md in iter_messages(fd):
                _, ok = table_name_from_descriptor(md)
                if not ok:
                    continue
                self._register_table_from_descriptor(md)
                registered.append(md.full_name)
        return registered

    def _register_table_from_descriptor(self, md: Descriptor) -> MessageTable:
        table = MessageTable.from_descriptor(md)
        self.tables[md.full_name] = table
        return table

    def sql_builder(self, m: Message | type[Message]) -> SQLBuilder:
        """复用已注册表的配置生成 SQL（不执行任何语句）。"""
        return SQLBuilder(self._table_for_message(m))

    # ── 建表 / 迁移 ─────────────────────────────────────────────────────

    def is_table_exists(self, table_name: str) -> bool:
        """表是否存在（带进程内缓存）。"""
        cached = self._table_exists_cache.get(table_name)
        if cached is not None:
            return cached
        count = self.query_one_value(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [self.dbname, table_name],
        )
        exists = bool(count)
        self._table_exists_cache[table_name] = exists
        return exists

    def table_has_primary_key(self, table_name: str) -> bool:
        """线上表当前是否已存在主键约束。"""
        return bool(self._table_primary_key_signature(table_name))

    def _table_primary_key_signature(
        self, table_name: str
    ) -> list[tuple[str, int | None]]:
        """PRIMARY 的列、顺序与前缀长度；同步时用它做 fail-closed 校验。"""
        rows = self.query(
            "SELECT COLUMN_NAME, SUB_PART FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND INDEX_NAME = 'PRIMARY' "
            "ORDER BY SEQ_IN_INDEX",
            [self.dbname, table_name],
        )
        return [
            ((name or "").lower(), int(sub_part) if sub_part else None)
            for name, sub_part in rows
        ]

    def get_table_columns(self, registry_key: str) -> dict[str, str]:
        """线上表的列名 -> 列类型（带表级缓存）。"""
        table = self._table_by_key(registry_key)
        cached = self._table_columns_cache.get(registry_key)
        if cached is not None:
            return cached
        rows = self.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [self.dbname, table.table_name],
        )
        columns = {name: col_type for name, col_type in rows}
        self._table_columns_cache[registry_key] = columns
        return columns

    def get_table_column_meta(self, registry_key: str) -> dict[str, ColumnMeta]:
        """线上表每列的类型 + 从注释解析出的 proto 字段号。

        迁移时靠字段号识别列，从而支持改名保留数据。**不走列类型缓存**：
        迁移不频繁，且这里要的是注释信息，混用会读到只有类型的旧缓存。
        """
        table = self._table_by_key(registry_key)
        rows = self.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, IS_NULLABLE, EXTRA, COLUMN_DEFAULT "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [self.dbname, table.table_name],
        )
        metas: dict[str, ColumnMeta] = {}
        for name, col_type, comment, is_nullable, extra, default in rows:
            num, ok = parse_field_num_from_comment(comment or "")
            metas[name] = ColumnMeta(
                col_type=col_type,
                field_num=num if ok else 0,
                # COLUMN_TYPE 里**不含**这三样："int unsigned" 既看不出可空不可空，
                # 也看不出有没有 AUTO_INCREMENT。只比 col_type 会把这几类漂移全漏掉。
                nullable=(is_nullable or "").upper() == "YES",
                auto_increment="auto_increment" in (extra or "").lower(),
                default=None if default is None else str(default),
            )
        return metas

    def clear_column_cache(self, registry_key: str) -> None:
        self._table_columns_cache.pop(registry_key, None)

    def create_or_update_table(self, m: Message | type[Message]) -> None:
        """建表（不存在时）或对齐已有表的字段结构。"""
        table = self._table_for_message(m)
        self._sync_table_schema(table.descriptor.full_name, table)

    def update_table_field(self, m: Message | type[Message]) -> None:
        """同步表字段（表不存在则创建，存在则对齐字段类型）。"""
        self.create_or_update_table(m)

    #: 抢 DDL 咨询锁的等待秒数。
    SYNC_LOCK_TIMEOUT = 30
    #: 咨询锁的名字（同一个库内全局）。
    SYNC_LOCK_NAME = "proto2mysql:sync"

    def sync_all_tables(self) -> None:
        """对全部已注册表执行建表 / 字段对齐。常与 register_all_tables 搭配。

        全程持有一把 MySQL 咨询锁（``GET_LOCK``），同一时刻只有一个进程在改结构。

        没有这把锁时：N 个副本同时冷启动 → 一个 ALTER 成功、其余全部撞
        ``Error 1060 Duplicate column name`` → 抛异常 → 启动失败。下一次重启会成功
        （列已经存在，对齐结果为空），所以它是**"自愈式蒙对"**——日志里留下一串启动失败、
        服务最终起来了，很容易被当成偶发 flake 忽略，直到某次重启风暴把它放大。

        锁拿不到时**不阻断**：MySQL 的 GET_LOCK 在超时时返回 0、连接异常时返回 NULL，
        而 TiDB 等兼容实现不一定支持这个函数。所以拿不到锁只降级为"无锁执行 + 一条告警"，
        再靠下面的 1060/1061/1050 容错兜底——把可用性问题看得比"锁一定要拿到"更重。
        """
        acquired = self._acquire_sync_lock()
        try:
            for key, table in self.tables.items():
                self._sync_table_schema(key, table)
        finally:
            if acquired:
                self._release_sync_lock()

    def _query_index_rows(self, table_name: str) -> list[tuple]:
        """读线上这张表的索引结构行（不含 PRIMARY 由 _parse_index_rows 过滤）。

        早先只读索引名。只比名字，「线上有一个同名的**非唯一**索引」会被判成
        "唯一键已经有了、无需补"——而唯一约束事实上根本不存在，业务却正按
        "有唯一键"在写。同名但列不同 / 列序不同的索引同样会被判成正确。
        """
        return self.query(
            "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART "
            "FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            [self.dbname, table_name],
        )

    @staticmethod
    def _wanted_index_columns(table: MessageTable, spec: str) -> list[tuple[str, int | None]]:
        """proto 里声明的索引列，归一成与 _existing_indexes 可比的形状。"""
        cols: list[tuple[str, int | None]] = []
        for raw in spec.split(","):
            col = raw.strip()
            prefix = TEXT_INDEX_PREFIX_LENGTH if table._needs_index_prefix(col) else None
            cols.append((col.lower(), prefix))
        return cols

    def _missing_index_clauses(
        self, table: MessageTable, *, column_renames: dict[str, str] | None = None
    ) -> list[str]:
        """proto 里声明了、线上却没有的索引，生成 ADD INDEX / ADD UNIQUE KEY。

        **只加不删**：线上多出来的索引一律不动（可能是 DBA 按查询模式手工加的，
        库没有立场去删）。与「永不 DROP COLUMN」是同一个立场。

        索引名与 CREATE TABLE 分支保持一致（``idx_<表名>_<序号>`` / ``uk_<表名>``），
        否则同一份 proto 在"新建表"和"老表补索引"两条路径上会产出不同的索引名。
        """
        if not table.indexes and not table.unique_keys:
            return []  # proto 里一个索引都没声明，不必去查 information_schema

        try:
            rows = self._query_index_rows(table.table_name)
        except Exception as exc:  # noqa: BLE001 - 无法验证时必须 fail-closed
            raise Proto2MySQLError(
                f"读取表 {table.table_name} 的既有索引失败，无法验证 schema：{exc}"
            ) from exc
        # 解析**不**包在上面那个 except 里：行形状不对是代码/驱动的问题，
        # 装作"线上一个索引都没有"会让它变成一串莫名其妙的 ADD INDEX。
        existing = _parse_index_rows(rows)

        clauses: list[str] = []
        for idx, index_cols in enumerate(table.indexes):
            name = index_name_for(table.table_name, idx)
            if self._index_is_in_place(
                table,
                name,
                index_cols,
                unique=False,
                existing=existing,
                column_renames=column_renames,
            ):
                continue
            cols = ",".join(table._index_column(c.strip()) for c in index_cols.split(","))
            clauses.append(f"ADD INDEX {escape_mysql_name(name)} ({cols})")
            log.info("table %s 补索引 %s (%s)", table.table_name, name, cols)

        if table.unique_keys:
            name = unique_key_name_for(table.table_name)
            if not self._index_is_in_place(
                table,
                name,
                table.unique_keys,
                unique=True,
                existing=existing,
                column_renames=column_renames,
            ):
                cols = ",".join(
                    table._index_column(c.strip()) for c in table.unique_keys.split(",")
                )
                clauses.append(f"ADD UNIQUE KEY {escape_mysql_name(name)} ({cols})")
                log.warning(
                    "table %s 补唯一键 %s (%s)：线上若已有重复行，这条 ALTER 会失败，"
                    "需先人工去重再重试（fail-closed，不会静默跳过）",
                    table.table_name, name, cols,
                )
        return clauses

    def _index_is_in_place(
        self,
        table: MessageTable,
        name: str,
        spec: str,
        *,
        unique: bool,
        existing: dict[str, "IndexMeta"],
        column_renames: dict[str, str] | None = None,
    ) -> bool:
        """线上那个同名索引是不是**真的**就是 proto 要的这一个。

        名字不在 → False（去补）。名字在但唯一性 / 列 / 列序 / 前缀长度对不上 →
        fail-closed：本库「只加不删」，改一个已存在的索引需要 DROP + ADD，
        而线上那个可能是 DBA 按查询模式手工建的，库没有立场擅自删除。

        只打一条 warning 后继续运行也不够：漂移最要命的形态是「同名的非唯一索引」——
        唯一约束事实上不存在，而业务正按"有唯一键"在写，直到某天发现重复数据。
        """
        meta = existing.get(name.lower())
        if meta is None:
            return False
        wanted = self._wanted_index_columns(table, spec)
        renames = column_renames or {}
        effective_columns = [
            (renames.get(col, col), prefix) for col, prefix in meta.columns
        ]
        if meta.unique == unique and effective_columns == wanted:
            return True
        raise Proto2MySQLError(
            f"table {table.table_name} 的索引 {name} 与 proto 声明不一致，"
            f"线上 unique={meta.unique} cols={meta.columns}"
            f"（应用同条 ALTER 的列改名后为 {effective_columns}），"
            f"proto 要 unique={unique} cols={wanted}。本库只加不删，"
            "不会擅自 DROP 可能由 DBA 管理的索引；请人工确认并 DROP INDEX 后重跑。"
        )

    def _acquire_sync_lock(self) -> bool:
        """抢 DDL 咨询锁。拿到返回 True；超时 / 不支持 / 出错都返回 False 并降级。"""
        try:
            got = self.query_one_value(
                "SELECT GET_LOCK(?, ?)", [self.SYNC_LOCK_NAME, self.SYNC_LOCK_TIMEOUT]
            )
        except Exception as exc:  # noqa: BLE001 - 不支持 GET_LOCK 的实现会直接报错
            log.warning(
                "拿不到 DDL 咨询锁（%s），本次结构同步无锁执行：%s。"
                "多副本同时启动时可能撞 Error 1060，重启即可自愈",
                self.SYNC_LOCK_NAME, exc,
            )
            return False
        if got is None or int(got) != 1:
            # 返回 0 = 等超时了（别人正在改）；NULL = 连接出错。两种都只告警不阻断。
            log.warning(
                "DDL 咨询锁 %s 未取得（返回 %r），本次结构同步无锁执行", self.SYNC_LOCK_NAME, got
            )
            return False
        return True

    def _release_sync_lock(self) -> None:
        try:
            self.query_one_value("SELECT RELEASE_LOCK(?)", [self.SYNC_LOCK_NAME])
        except Exception as exc:  # noqa: BLE001
            # 连接断开时锁会被服务端自动释放，这里失败不影响正确性。
            log.warning("释放 DDL 咨询锁 %s 失败（连接断开时会自动释放）：%s", self.SYNC_LOCK_NAME, exc)

    def _plan_schema_changes(
        self, registry_key: str, table: MessageTable
    ) -> tuple[list[str], list[str]]:
        """算出 ALTER 子句：``(列阶段, 依赖列阶段的约束/索引)`` 两组。

        在线同步（:meth:`_sync_table_schema`）与离线迁移（:meth:`generate_migration_sql`）
        **共用这一份**。早先两条路径各算各的，离线那条只调了 build_alter_clauses——
        漏掉索引和主键：库里缺唯一键，生成的迁移文件却是空的，审核者据此判定
        「无需迁移」。同一件事算两遍，迟早再分叉一次。

        分成两组不只为补主键：TiDB 也不能在同一条 ALTER 中让索引引用刚由
        ADD/CHANGE 产生的列名，所以任何这种依赖都要等列阶段完成后再执行。
        """
        alter_sqls = table.build_alter_clauses(
            self.get_table_column_meta(registry_key), expand_only=self.expand_only
        )
        newly_visible_columns = {
            _clause_target_column(clause)
            for clause in alter_sqls
            if clause.startswith(("ADD COLUMN ", "CHANGE COLUMN "))
        }
        newly_visible_columns.discard("")
        post_sqls: list[str] = []

        # 补齐 proto 里声明了、但线上还没有的索引。
        #
        # 早先索引只出现在 CREATE TABLE 分支：表一旦建成，之后在 .proto 里新加
        # index / unique_key **完全不生效**，而且零提示——查询照常能跑，只是走全表扫描，
        # 数据量上来才表现为"莫名其妙变慢"，谁也想不到是建表选项没落地。
        index_sqls = self._missing_index_clauses(
            table, column_renames=_column_rename_map(alter_sqls)
        )
        for clause in index_sqls:
            target = set(_index_clause_columns(clause))
            if target & newly_visible_columns:
                post_sqls.append(clause)
            else:
                alter_sqls.append(clause)

        # 补齐缺失的主键——**必须与列对齐分成两条 ALTER**。
        #
        # 主键列常同时带 AUTO_INCREMENT，而 MySQL 要求「自增列必须是键」：
        # 单独 MODIFY 成 AUTO_INCREMENT 会报 Error 1075，所以那条 MODIFY 必须与
        # ADD PRIMARY KEY 同句。早先的做法是把它俩连同全部 ADD COLUMN 塞进**一条**
        # ALTER，理由是"列对齐与补主键原子成功或失败"。
        #
        # 但 2026-08-26 在真 TiDB v8.5.1 上实测发现：**TiDB 根本不支持给已存在的列
        # 加 AUTO_INCREMENT**（Error 8200 Unsupported modify column: can't set
        # auto_increment），合并一条、拆成两条都一样。于是那条 ALTER 整体失败，
        # 连带把所有 ADD COLUMN 一起废掉——服务启动成功，第一条 SELECT 就
        # Error 1054 Unknown column，而根因埋在一条看起来只是"补主键"的语句里。
        #
        # 所以拆开：**普通列与无依赖索引先落地，引用新列名的索引、补主键再执行**。
        # 补主键失败时非主键列不会被连坐，但同步调用仍 fail-closed 抛错，运维必须
        # 处理主键或由上层明确决定是否继续启动。
        #
        # 这里只补"从无到有"，绝不自动 DROP/改写已有主键；但已存在的主键若
        # 列/顺序/前缀不对，必须 fail-closed，不能把“有一个 PRIMARY”当成已对齐。
        live_pk: list[tuple[str, int | None]] = []
        if table.primary_key:
            live_pk = self._table_primary_key_signature(table.table_name)
            rename_map = _column_rename_map(alter_sqls)
            effective_pk = [
                (rename_map.get(col, col), prefix) for col, prefix in live_pk
            ]
            wanted_pk = self._wanted_index_columns(
                table, ",".join(table.primary_key)
            )
            if live_pk and effective_pk != wanted_pk:
                raise Proto2MySQLError(
                    f"table {table.table_name} 的 PRIMARY KEY 与 proto 声明不一致，"
                    f"线上 cols={live_pk}（应用同条 ALTER 的列改名后为 {effective_pk}），"
                    f"proto 要 cols={wanted_pk}。本库只加不删，不会擅自 DROP/改写主键；"
                    "请人工迁移后重跑。"
                )
        if table.primary_key and not live_pk:
            # 与 CREATE TABLE 分支同样走 _index_column：TEXT/BLOB 主键列必须带前缀长度，
            # 否则 MySQL 直接 Error 1170（string 映射成 MEDIUMTEXT，很容易撞上）。
            cols = ",".join(table._index_column(pk) for pk in table.primary_key)
            # 主键列若要获得 AUTO_INCREMENT，必须与 ADD PRIMARY KEY 同句，否则
            # MySQL 报 Error 1075。普通 ADD / MODIFY / CHANGE 则留在第一阶段先落地：
            # TiDB 不能在同一条 ALTER 里让 ADD PRIMARY KEY / ADD INDEX 引用刚刚
            # CHANGE 出来的新列名（Error 1072），必须先完成列变更再加约束。
            #
            # 早先只挪 MODIFY，前提是"主键列已经存在"。主键列**本身也还不存在**时
            # （老表按旧 proto 建的，新 proto 才加上这个自增主键），生成的是
            # `ADD COLUMN `id` bigint NOT NULL AUTO_INCREMENT`，它留在第一条里
            # 照样 1075，而且是整条 ALTER 失败——所有 ADD COLUMN 一起废掉，
            # 报错还指向一条看起来只是"加列"的语句。既有测试只覆盖了前一种情形。
            pk_set = set(table.primary_key)
            pk_column_clauses = [
                c for c in alter_sqls if _clause_target_column(c) in pk_set
            ]
            moved = [c for c in pk_column_clauses if "AUTO_INCREMENT" in c.upper()]
            # 按 id() 剔除而不是按值：语义是"把这几条搬走"，不是"删掉所有长这样的"。
            moved_ids = {id(c) for c in moved}
            alter_sqls = [c for c in alter_sqls if id(c) not in moved_ids]
            post_sqls = moved + [f"ADD PRIMARY KEY ({cols})"] + post_sqls

            log.warning(
                "table %s is missing its primary key; adding %s", table.table_name, cols
            )
        return alter_sqls, post_sqls

    def _sync_table_schema(
        self, registry_key: str, table: MessageTable, *, _ddl_retries: int = 1
    ) -> None:
        if not table.has_explicit_table_name:
            # 表名退化成了 proto full name（含 package）。这在 package 一改就出事：
            # v2 把 package 从 game.v1 改成 game.v2，表名跟着变 → 建出一张**全新的空表**，
            # v1 的数据留在旧表里，而**两边都不报错**——服务照常起来，玩家数据"凭空消失"。
            #
            # 只在真正要拿这个表名去动数据库时才告警：构造期就喊会把 golang_test_list
            # 这类"列表包装消息"（压根不是表）也一起喊上，变成噪音。
            log.warning(
                "表 %s 没有声明 table_name 选项，表名退化为 proto full name。"
                "proto 的 package 一改表名就跟着变，会建出一张空表而旧数据留在旧表里，"
                "且两边都不报错。建议在 .proto 里显式写 option (proto2mysql.table_name)",
                table.table_name,
            )
        if not self.is_table_exists(table.table_name):
            try:
                self.execute(table.get_create_table_sql())
            except Exception as exc:
                # IF NOT EXISTS 通常只给 warning；少数兼容实现仍抛 1050。
                # 这只表示另一副本抢先建好了，下面必须继续回读完整结构。
                if _mysql_error_code(exc) != 1050:
                    raise
                log.warning("并发建表已由另一副本完成，回读并继续对齐 %s", table.table_name)
            self._table_exists_cache[table.table_name] = True
            # 刻意**不 return**，继续往下走列对齐。
            #
            # 建表语句是 CREATE TABLE IF NOT EXISTS，在并发下可能整条是 no-op：
            # 两个版本的进程同时冷启动到空库时，先到的那个按自己的 proto 建表，
            # 后到的这条 CREATE 只会拿到一条 Warning 1050——不报错、不改结构。
            # 早先这里直接 return，于是后到进程独有的新列**从未被添加**，而它自己
            # 启动成功、零异常，一直到第一条 SELECT 才报 Error 1054 Unknown column；
            # 且 _table_exists_cache 已置 True，重启也不会重新对齐，**不自愈**。
            #
            # 落到对齐路径上就没有这个问题：get_table_column_meta 直读
            # INFORMATION_SCHEMA（不走缓存），拿到的是真实建成的结构，
            # 缺什么补什么。自己建成功的那条路径上 build_alter_clauses 返回空，
            # 只多一次元信息查询，代价可以忽略。

        alter_sqls, post_sqls = self._plan_schema_changes(registry_key, table)

        if alter_sqls:
            alter_sql = (
                f"ALTER TABLE {escape_mysql_name(table.table_name)} {', '.join(alter_sqls)}"
            )
            try:
                self.execute(alter_sql)
            except Exception as exc:
                if _ddl_retries and _is_concurrent_ddl_conflict(exc):
                    log.warning(
                        "表 %s 的 ALTER 与另一副本并发（Error %s），回读真实结构后重算一次",
                        table.table_name,
                        _mysql_error_code(exc),
                    )
                    self.clear_column_cache(registry_key)
                    self._sync_table_schema(
                        registry_key, table, _ddl_retries=_ddl_retries - 1
                    )
                    return
                raise Proto2MySQLError(
                    f"更新表 {table.table_name} 结构失败: {exc}, SQL: {alter_sql}"
                ) from exc
            self.clear_column_cache(registry_key)
            self._await_schema_visible(registry_key, table, alter_sqls)

        if post_sqls:
            post_sql = (
                f"ALTER TABLE {escape_mysql_name(table.table_name)} {', '.join(post_sqls)}"
            )
            adds_primary_key = any(
                clause.startswith("ADD PRIMARY KEY ") for clause in post_sqls
            )
            try:
                self.execute(post_sql)
            except Exception as exc:
                if _ddl_retries and _is_concurrent_ddl_conflict(exc):
                    log.warning(
                        "表 %s 的第二阶段 ALTER 与另一副本并发（Error %s），"
                        "回读真实结构后重算一次",
                        table.table_name,
                        _mysql_error_code(exc),
                    )
                    self.clear_column_cache(registry_key)
                    self._sync_table_schema(
                        registry_key, table, _ddl_retries=_ddl_retries - 1
                    )
                    return
                if adds_primary_key:
                    raise Proto2MySQLError(
                        f"表 {table.table_name} 的其余列已对齐，但补齐主键失败: {exc}\n"
                        f"  SQL: {post_sql}\n"
                        f"  ⚠️ 含 AUTO_INCREMENT 的主键列子句也在这条语句里（它必须与\n"
                        f"     ADD PRIMARY KEY 同句，否则 Error 1075），所以这条一失败，\n"
                        f"     **自增主键列可能压根没建出来**——按 proto 读写它会 Unknown column。\n"
                        f"  两个常见原因：\n"
                        f"    1) 线上已有重复行——先人工去重再重试（fail-closed，不会静默跳过）\n"
                        f"    2) 后端是 TiDB——它**不支持给已存在的列加 AUTO_INCREMENT**\n"
                        f"       （Error 8200），只能重建表或去掉 auto_increment_key 选项"
                    ) from exc
                raise Proto2MySQLError(
                    f"表 {table.table_name} 的列已对齐，但补齐依赖索引失败: {exc}, "
                    f"SQL: {post_sql}"
                ) from exc
            self.clear_column_cache(registry_key)
            self._await_schema_visible(registry_key, table, post_sqls)

    #: 等 DDL 在所有节点生效的最长秒数与轮询间隔。
    SCHEMA_SETTLE_TIMEOUT = 60.0
    SCHEMA_SETTLE_INTERVAL = 0.2

    def _reject_for_update_outside_transaction(self, opts: QueryOptions) -> None:
        """事务外的 ``QueryOptions(for_update=True)`` 一律拒绝。

        自动提交下 ``SELECT ... FOR UPDATE`` 的行锁在语句返回那一刻就释放了：
        调用方拿到的是"读的时候锁过一下"的行，随后的读-改-写照样丢更新——
        而代码里明明白白写着 ``for_update=True``。**看起来加了锁其实没加**
        比压根不加锁更危险，因为它会让人跳过真正需要的并发控制。

        判据是 :meth:`_connection_in_transaction`（连接在不在事务里）而不是
        ``self._in_transaction``：``autocommit=False`` 的连接本来就一直在事务里，
        在那种连接上 FOR UPDATE 是**合法**的，按对象级标志判会把它误拒。

        :meth:`find_one_by_pk_for_update` 一直是拒绝的，走 QueryOptions 的
        两条路（find_one_with_options / find_all_with_options）漏了。
        拦的是 DB 上的执行入口；``sql_builder()`` 出来的 SQLBuilder 是纯文本生成器，
        手里没有事务上下文，那条逃生口不拦也拦不了。
        """
        if opts.for_update and not self._connection_in_transaction():
            raise Proto2MySQLError(
                "QueryOptions(for_update=True) 只能在 transaction() 内使用："
                "事务外单句自动提交，FOR UPDATE 的锁在语句返回时就释放了，等于没加"
            )

    @staticmethod
    def _newly_visible_column_names(alter_sqls: Sequence[str]) -> set[str]:
        """从 ALTER 子句里挑出本次新增或改名后的目标列名。

        CHANGE 的旧名本来可见，但依赖索引使用的是**新名**；TiDB schema 尚未传播时，
        新名仍不可见。MODIFY 不改变列名，不需要等待。
        """
        names: set[str] = set()
        for clause in alter_sqls:
            if not clause.startswith(("ADD COLUMN ", "CHANGE COLUMN ")):
                continue
            name = _clause_target_column(clause)
            if name:
                names.add(name)
        return names

    def _await_schema_visible(
        self, registry_key: str, table: MessageTable, alter_sqls: Sequence[str]
    ) -> None:
        """等到 ALTER 的结果**真的能被看见**，再放行后续 SQL。

        MySQL 单机上这一步立刻就过（DDL 返回即生效），几乎零开销。

        但 **TiDB 的 DDL 是异步 online 的**：ALTER 语句返回时，schema 变更只是进了
        DDL 队列，各个 TiDB 节点要按 lease（默认 45s）分批加载新的 schema 版本。
        库执行完 ALTER 立刻按新 proto 发 INSERT/SELECT，这段窗口里连到**还没加载
        新 schema 的节点**就会报 Unknown column——启动日志漂亮，第一批请求全挂。
        原先这里没有任何等待或重试。

        实现上刻意**不去检测"后端是不是 TiDB"**：版本号/变量嗅探很容易被兼容层骗过，
        而"回读 information_schema 直到结构真的对上"是纯行为判定，对 MySQL、TiDB、
        以及任何自称兼容的实现都成立。
        """
        want = self._newly_visible_column_names(alter_sqls)
        if not want:
            return  # 本次没有新增/改名列（只有 MODIFY / 索引），无需等待

        deadline = time.monotonic() + self.SCHEMA_SETTLE_TIMEOUT
        while True:
            try:
                live = set(self.get_table_column_meta(registry_key))
            except Exception as exc:  # noqa: BLE001 - 探测失败不该反过来阻断启动
                log.warning("回读表 %s 结构失败，跳过就绪探测：%s", table.table_name, exc)
                return
            if not live:
                # 一列都读不到 = 根本看不见这张表（权限/库名不对/驱动不给结果）。
                # 真表不可能零列，所以这不是"还没生效"，继续轮询只会白等到超时。
                log.debug("表 %s 回读不到任何列，跳过就绪探测", table.table_name)
                return
            missing = want - live
            if not missing:
                return
            if time.monotonic() >= deadline:
                # 只告警不抛：结构可能确实还在后台排队。抛异常会把"慢"升级成"起不来"。
                log.warning(
                    "表 %s 的结构变更在 %.0fs 内没有全部可见（仍缺 %s）。"
                    "TiDB 的 DDL 是异步的，可能仍在后台排队；"
                    "若后续 SQL 报 Unknown column，等一个 schema lease 再重试",
                    table.table_name, self.SCHEMA_SETTLE_TIMEOUT, sorted(missing),
                )
                return
            time.sleep(self.SCHEMA_SETTLE_INTERVAL)

    def get_create_table_sql(self, m: Message | type[Message]) -> str:
        return self._table_for_message(m).get_create_table_sql()

    def generate_migration_sql(self, m: Message | type[Message]) -> str:
        """生成把线上表结构对齐到 proto 定义所需的 SQL。

        * 表不存在   → CREATE TABLE
        * 表已存在   → ALTER TABLE（新增字段 / 按字段号改名 / 类型对齐 / 补索引 / 补主键）
        * 无任何差异 → 空串

        与 :meth:`update_table_field` 的区别：**只产出 SQL 不执行**，
        便于生成迁移文件交人工/CI 审核。

        引用本次 ADD/CHANGE 目标列的新索引会放到列变更后的第二条语句；补主键也
        使用这一阶段（自增列必须与 ADD PRIMARY KEY 同句，且 TiDB 上这条可能失败
        而其余列必须先对齐好）。因此返回值可能是**两条**以 ``\n`` 分隔的语句——
        与在线路径发出去的完全一致，两边共用 :meth:`_plan_schema_changes`。
        """
        table = self._table_for_message(m)
        if not self.is_table_exists(table.table_name):
            return table.get_create_table_sql()

        alter_sqls, post_sqls = self._plan_schema_changes(
            table.descriptor.full_name, table
        )
        escaped = escape_mysql_name(table.table_name)
        stmts = [
            f"ALTER TABLE {escaped} {', '.join(clauses)};"
            for clauses in (alter_sqls, post_sqls)
            if clauses
        ]
        return "\n".join(stmts)

    def write_migration_sql(self, w, *messages: Message | type[Message]) -> None:
        """依次为每个消息生成迁移 SQL 并写入 w（无差异的表自动跳过）。"""
        for m in messages:
            stmt = self.generate_migration_sql(m)
            if not stmt:
                continue
            w.write(stmt)
            w.write("\n\n")

    def dump_migration_sql_file(self, path: str, *messages: Message | type[Message]) -> None:
        """把多个消息的迁移 SQL 写到文件（覆盖写），需连库。"""
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            self.write_migration_sql(f, *messages)

    def dump_create_table_sql_file(self, path: str) -> None:
        """把全部已注册表的建表语句写到文件（覆盖写），不需要连库。"""
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            self.write_create_table_sql(f)

    def write_create_table_sql(self, w) -> None:
        """把全部已注册表的 CREATE TABLE 写入 w（按表名排序，输出稳定）。"""
        from .sqlgen import write_create_table_sql as _write

        _write(w, self.tables.values())

    # ── INSERT ──────────────────────────────────────────────────────────

    def insert(self, message: Message) -> None:
        """全字段插入。"""
        table = self._table_for_message(message)
        self._exec_stmt(table.get_insert_sql(message))

    def batch_insert(self, messages: Sequence[Message]) -> None:
        """批量插入，超过 BATCH_INSERT_MAX_SIZE 自动分批。

        ⚠️ **分批之间不是一个事务**：第 3 批失败时前两批已经落库。需要整批原子，
        请自己包一层 ``with db.transaction()``。

        插入路径**刻意不做缓存失效**（与 Go 版一致）。要让插入时缓存里有陈旧条目，
        必须是「读者已回源 → delete 失效 → insert → 读者的回填才落地」这个序列，
        而在 insert 后面补一次删除只是把窗口挪窄，读者的回填照样可以落在它之后。
        真正的解药是 cache-aside 读-填竞态本身（见 docs/concurrency.md 第四节），
        补一次删只会让人以为这条已经安全了。
        """
        if not messages:
            raise Proto2MySQLError("no messages to insert")
        table = self._table_for_message(messages[0])
        for batch in _chunks(messages, BATCH_INSERT_MAX_SIZE):
            self._exec_stmt(table.get_batch_insert_sql(batch))

    def insert_ignore(self, message: Message) -> bool:
        """幂等插入：只忽略唯一键冲突，其他数据错误照常抛出。

        MySQL 的 ``INSERT IGNORE`` 还会把截断、非法日期、NOT NULL 等数据错误
        降级成 warning；这里改用普通 INSERT，并且只捕获明确包装过的 1062。
        """
        table = self._table_for_message(message)
        try:
            return self._exec_stmt(table.get_insert_sql(message)) > 0
        except DuplicateKeyError:
            return False

    def insert_returning_id(self, message: Message) -> int:
        """插入并返回自增主键（LAST_INSERT_ID）。自增主键表建议用这个。"""
        table = self._table_for_message(message)
        return self._exec_returning_lastrowid(table.get_insert_sql(message))

    def insert_on_dup_update(self, message: Message) -> None:
        """按主键有则更新、无则插入，只覆盖消息中已赋值的字段。

        不直接使用 ODKU：任意二级 UNIQUE 都能触发 ODKU，候选主键不存在时会
        错改另一主键所拥有的行。这里始终以完整主键作为行身份。
        """
        table = self._table_for_message(message)
        try:
            self._save_by_primary_key(table, message, only_set_fields=True)
        finally:
            # 语句抛错也可能是服务端已提交、ACK 丢失；保守失效只会多一次回源。
            self._invalidate_messages(table, message)

    def _save_by_primary_key(
        self,
        table: MessageTable,
        message: Message,
        *,
        only_set_fields: bool,
    ) -> None:
        """以完整主键为唯一行身份执行 UPDATE→INSERT 保存状态机。

        首次 UPDATE 为 0 既可能表示不存在，也可能只是值未变化，所以尝试 INSERT。
        INSERT 的 1062 可能来自同主键并发插入，也可能来自另一行的二级唯一键；
        再 UPDATE 一次并按主键查存在性，才能安全区分这两种情况。
        """
        update_stmt = table.get_save_update_sql(
            message, only_set_fields=only_set_fields
        )
        if self._exec_stmt(update_stmt) > 0:
            return

        try:
            self._exec_stmt(table.get_insert_sql(message))
            return
        except DuplicateKeyError as insert_error:
            if self._exec_stmt(update_stmt) > 0:
                return
            if self.exists_by_pk(message):
                # MySQL 默认只统计真正发生变化的行；同值 UPDATE 返回 0 仍是成功。
                return
            raise DuplicateKeyError(
                f"upsert on table {table.table_name} conflicts with a different "
                f"unique-key owner: {insert_error}"
            ) from insert_error

    def save(self, message: Message) -> None:
        """整行落库：有则更新、无则插入。

        走按完整主键的 ``UPDATE → INSERT`` 状态机，**不是** ``REPLACE INTO``，
        也不让二级 UNIQUE 冲突决定要更新哪一行。因此既不会清掉本进程不认识的列，
        也不会在候选主键不存在时误改另一主键的行。

        需要「整行推倒重来」的旧语义时，显式用 ``SQLBuilder.replace``。
        """
        table = self._table_for_message(message)
        try:
            self._save_by_primary_key(table, message, only_set_fields=False)
        finally:
            self._invalidate_messages(table, message)

    def batch_save(self, messages: Sequence[Message]) -> None:
        """逐行整行落库，语义同 :meth:`save`。

        ⚠️ **各行之间不是一个事务**：后一行失败时，前面的行可能已经落库。
        需要整批原子时请显式包一层 ``with db.transaction()``。
        """
        if not messages:
            return
        table = self._table_for_message(messages[0])
        for msg in messages:
            table.validate_message(msg)
        for message in messages:
            try:
                self._save_by_primary_key(table, message, only_set_fields=False)
            finally:
                # 前一行可能已提交，失败行也可能只是 ACK 丢失；逐行保守失效。
                self._invalidate_messages(table, message)

    # ── UPDATE ──────────────────────────────────────────────────────────

    def update(self, message: Message) -> None:
        """按主键更新消息中**已赋值**的字段。"""
        table = self._table_for_message(message)
        try:
            self._exec_stmt(table.get_update_sql(message))
        finally:
            self._invalidate_messages(table, message)

    def update_by_where(
        self, message: Message, where_clause: str, where_args: Sequence[Any] | None = None
    ) -> None:
        """按自定义 WHERE 更新已赋值字段（**不做缓存失效**，定位不到主键）。"""
        table = self._table_for_message(message)
        self._exec_stmt(table.get_update_sql_by_where(message, where_clause, where_args))

    def update_fields_by_pk(self, message: Message, *fields: str) -> None:
        """按主键只更新指定字段（部分更新）。

        避免 :meth:`update` 全字段覆盖冲掉别处的并发写入（如改名操作把刚加的金币盖回去），
        也是写零值的正确姿势（proto3 零值在 :meth:`update` 里会被当未赋值跳过）。
        """
        if not fields:
            raise Proto2MySQLError("no fields to update")
        table = self._table_for_message(message)

        clauses: list[str] = []
        args: list[Any] = []
        for name in fields:
            fd = table.field(name)
            clauses.append(f"{escape_mysql_name(name)} = ?")
            args.append(pbconv.serialize_field_value(message, fd))

        where_clause, where_args = table.primary_key_where(message)
        try:
            self.execute(
                f"UPDATE {escape_mysql_name(table.table_name)} SET {', '.join(clauses)} "
                f"WHERE {where_clause}",
                args + where_args,
            )
        finally:
            self._invalidate_messages(table, message)

    def update_kv_by_pk(self, message: Message, field: str, value: Any) -> None:
        """按主键设置单个字段的值（改状态、封号一类）。"""
        table = self._table_for_message(message)
        table.field(field)  # 未知列直接抛错
        where_clause, where_args = table.primary_key_where(message)
        try:
            self.execute(
                f"UPDATE {escape_mysql_name(table.table_name)} "
                f"SET {escape_mysql_name(field)} = ? WHERE {where_clause}",
                [value] + where_args,
            )
        finally:
            self._invalidate_messages(table, message)

    def update_if_version(self, message: Message, version_field: str) -> bool:
        """乐观锁 CAS 更新：仅当库里的 version_field 等于消息当前值时生效，成功后自动 +1。

        返回 False 表示版本冲突（被其他写入抢先），调用方应重读后重试。
        不想用行锁时的轻量并发控制。
        """
        table = self._table_for_message(message)
        version_desc = table.field(version_field)
        cur_version = pbconv.serialize_field_value(message, version_desc)

        pk_set = set(table.primary_key)
        clauses: list[str] = []
        args: list[Any] = []
        for fd in table.fields:
            if fd.name == version_field or fd.name in pk_set:
                continue
            if not pbconv.has_field(message, fd):
                continue
            clauses.append(f"{escape_mysql_name(fd.name)} = ?")
            args.append(pbconv.serialize_field_value(message, fd))
        if not clauses:
            raise Proto2MySQLError("no fields to update")

        return self._exec_version_cas(table, message, version_field, cur_version, clauses, args)

    def update_fields_if_version(
        self, message: Message, version_field: str, *fields: str
    ) -> bool:
        """乐观锁 CAS + 显式字段列表。

        与 :meth:`update_if_version` 的区别：不靠"已赋值"自动挑字段，而是显式列出要写的列，
        规避 proto3 隐式 presence 下零值字段（空 bytes / 0 / ""）被跳过的坑。
        """
        if not fields:
            raise Proto2MySQLError("no fields to update")
        table = self._table_for_message(message)
        version_desc = table.field(version_field)
        cur_version = pbconv.serialize_field_value(message, version_desc)

        clauses: list[str] = []
        args: list[Any] = []
        for name in fields:
            if name == version_field:
                continue  # version 由下面统一 +1
            fd = table.field(name)
            clauses.append(f"{escape_mysql_name(name)} = ?")
            args.append(pbconv.serialize_field_value(message, fd))

        return self._exec_version_cas(table, message, version_field, cur_version, clauses, args)

    def _exec_version_cas(
        self,
        table: MessageTable,
        message: Message,
        version_field: str,
        cur_version: Any,
        clauses: list[str],
        args: list[Any],
    ) -> bool:
        escaped_version = escape_mysql_name(version_field)
        clauses = clauses + [f"{escaped_version} = {escaped_version} + 1"]
        where_clause, where_args = table.primary_key_where(message)
        sql = (
            f"UPDATE {escape_mysql_name(table.table_name)} SET {', '.join(clauses)} "
            f"WHERE {where_clause} AND {escaped_version} = ?"
        )
        try:
            affected = self.execute(sql, args + where_args + [cur_version])
        except BaseException:
            self._invalidate_messages(table, message)
            raise
        if affected > 0:
            self._invalidate_messages(table, message)
        return affected > 0

    def incr_by_pk(self, message: Message, field: str, delta: int) -> None:
        """按主键对数值字段原子加减，避免"读-改-写"竞态。"""
        table = self._table_for_message(message)
        # 必须是数值列：MySQL 会把 'abc' + 1 隐式算成 1，非严格 sql_mode 下
        # 一次"加 1 金币"就把整列原值抹掉，且不报错（见 numeric_field）。
        table.numeric_field(field)
        where_clause, where_args = table.primary_key_where(message)
        escaped = escape_mysql_name(field)
        try:
            self.execute(
                f"UPDATE {escape_mysql_name(table.table_name)} SET {escaped} = {escaped} + ? "
                f"WHERE {where_clause}",
                [delta] + where_args,
            )
        finally:
            self._invalidate_messages(table, message)

    def decr_by_pk_if_enough(self, message: Message, field: str, delta: int) -> bool:
        """按主键原子扣减，余额不足时不扣并返回 False（防止负数余额）。"""
        if delta < 0:
            raise Proto2MySQLError(f"delta must be non-negative, got {delta}")
        table = self._table_for_message(message)
        table.numeric_field(field)
        where_clause, where_args = table.primary_key_where(message)
        escaped = escape_mysql_name(field)
        try:
            affected = self.execute(
                f"UPDATE {escape_mysql_name(table.table_name)} SET {escaped} = {escaped} - ? "
                f"WHERE {where_clause} AND {escaped} >= ?",
                [delta] + where_args + [delta],
            )
        except BaseException:
            self._invalidate_messages(table, message)
            raise
        if affected > 0:
            self._invalidate_messages(table, message)
        return affected > 0

    # ── DELETE ──────────────────────────────────────────────────────────

    def delete(self, message: Message) -> None:
        """按主键删除。"""
        table = self._table_for_message(message)
        try:
            self._exec_stmt(table.get_delete_sql(message))
        finally:
            self._invalidate_messages(table, message)

    def delete_by_where(
        self, message: Message, where_clause: str, where_args: Sequence[Any] | None = None
    ) -> None:
        """按自定义 WHERE 删除（**不做缓存失效**）。"""
        table = self._table_for_message(message)
        self._exec_stmt(table.get_delete_sql_by_where(where_clause, where_args))

    def delete_by_kv(self, message: Message, key: str, value: Any) -> None:
        """按单个字段等值条件删除。"""
        self.delete_by_where(message, f"{escape_mysql_name(key)} = ?", [value])

    def batch_delete(self, messages: Sequence[Message]) -> None:
        """按主键批量删除（``WHERE (pk...) IN ((..),(..))``，自动分批）。

        ⚠️ **分批之间不是一个事务**（见 :meth:`batch_insert`）。
        """
        if not messages:
            return
        table = self._table_for_message(messages[0])
        if not table.primary_key:
            raise PrimaryKeyNotFoundError(f"primary key not found: table {table.table_name}")
        for msg in messages:
            table.validate_message(msg)

        pk_names = ", ".join(escape_mysql_name(pk) for pk in table.primary_key)
        for batch in _chunks(messages, BATCH_INSERT_MAX_SIZE):
            args: list[Any] = []
            tuples: list[str] = []
            for msg in batch:
                args.extend(table.primary_key_values(msg))
                tuples.append(f"({build_placeholders(len(table.primary_key))})")
            where = f"({pk_names}) IN ({', '.join(tuples)})"
            # 与 batch_save 同理：分批不是一个事务，必须逐批失效（见那边的注释）。
            try:
                self.delete_by_where(messages[0], where, args)
            finally:
                self._invalidate_messages(table, *batch)

    # ── SELECT ──────────────────────────────────────────────────────────

    def find_one_by_pk(self, message: Message) -> None:
        """按消息中的主键值查一行，查到后写回 message。

        启用缓存时这是 cache-aside 读路径：先查缓存，未命中读 DB 后回填。
        **事务内不走缓存**（需要读到事务内未提交的最新值）。
        """
        table = self._table_for_message(message)
        # 判据是**连接**在不在事务里，不是这个 DB 对象是不是 tx：显式事务中
        # 父 wrapper 与 tx 都必须绕过缓存。autocommit=False + cache 已在
        # _table_for_message 阶段 fail-closed，不会走到这里。
        use_cache = self._cache_enabled() and not self._connection_in_transaction()
        if use_cache and self._cache_get_proto(table, message):
            return

        where_clause, where_args = table.primary_key_where(message)
        self.find_one_by_where(message, where_clause, where_args)

        if use_cache:
            self._cache_set_proto(table, message)

    def find_one_by_pk_for_update(self, message: Message) -> None:
        """按主键查一行并加行锁（SELECT ... FOR UPDATE）。

        只在事务内有意义：事务外单句自动提交，锁立即释放，形同没加。
        """
        if not self._connection_in_transaction():
            raise Proto2MySQLError(
                "find_one_by_pk_for_update must be called inside transaction()"
            )
        table = self._table_for_message(message)
        where_clause, where_args = table.primary_key_where(message)
        rows = self.query(
            f"{table.select_fields_sql} WHERE {where_clause} FOR UPDATE", where_args
        )
        _scan_one_row(rows, message)

    def find_one_by_kv(self, message: Message, where_key: str, where_val: Any) -> None:
        """按单个字段等值条件查一行。"""
        self.find_one_by_where(
            message, f"{escape_mysql_name(where_key)} = ?", [where_val]
        )

    def find_one_by_where(
        self, message: Message, where_clause: str, where_args: Sequence[Any] | None = None
    ) -> None:
        """按条件查一行并写回 message。多行匹配抛 MultipleRowsFoundError，零行抛 NoRowsFoundError。"""
        table = self._table_for_message(message)
        rows = self.query(
            f"{table.select_fields_sql} WHERE {normalize_where_clause(where_clause)}",
            where_args,
        )
        _scan_one_row(rows, message)

    def find_one_with_options(
        self,
        message: Message,
        where_clause: str = "",
        where_args: Sequence[Any] | None = None,
        opts: QueryOptions | None = None,
    ) -> None:
        """按条件 + 排序取一条（排行第一名、最新一条记录一类）。自动追加 LIMIT 1。"""
        table = self._table_for_message(message)
        opts = opts or QueryOptions()
        self._reject_for_update_outside_transaction(opts)
        opts = QueryOptions(
            order_by=opts.order_by, limit=1, offset=0, for_update=opts.for_update
        )
        rows = self.query(
            f"{table.select_fields_sql} WHERE {normalize_where_clause(where_clause)}"
            f"{opts.sql_suffix()}",
            where_args,
        )
        _scan_one_row(rows, message)

    def find_or_create(self, message: Message) -> bool:
        """按主键查，不存在则用 message 当前值插入。返回是否新建了记录。"""
        try:
            self.find_one_by_pk(message)
        except NoRowsFoundError:
            self.insert(message)
            return True
        return False

    def find_all(self, list_message: Message) -> None:
        """查全表到列表消息（含单个 repeated 字段的消息）。"""
        self.find_all_by_where(list_message, "1=1", None)

    def find_all_by_where(
        self,
        list_message: Message,
        where_clause: str,
        where_args: Sequence[Any] | None = None,
    ) -> None:
        """按条件查多行到列表消息。"""
        table, list_field = self._resolve_list_table(list_message)
        rows = self.query(
            f"{table.select_fields_sql} WHERE {normalize_where_clause(where_clause)}",
            where_args,
        )
        _scan_rows_to_list(rows, list_message, list_field)

    def find_all_with_options(
        self,
        list_message: Message,
        where_clause: str = "",
        where_args: Sequence[Any] | None = None,
        opts: QueryOptions | None = None,
    ) -> None:
        """按条件查多行，支持 ORDER BY / LIMIT / OFFSET。"""
        table, list_field = self._resolve_list_table(list_message)
        opts = opts or QueryOptions()
        self._reject_for_update_outside_transaction(opts)
        rows = self.query(
            f"{table.select_fields_sql} WHERE {normalize_where_clause(where_clause)}"
            f"{opts.sql_suffix()}",
            where_args,
        )
        _scan_rows_to_list(rows, list_message, list_field)

    def find_page(
        self,
        list_message: Message,
        where_clause: str,
        where_args: Sequence[Any] | None,
        page_index: int,
        page_size: int,
    ) -> None:
        """按页取数据（page_index 从 1 开始）。深分页请改用 :meth:`find_page_by_cursor`。"""
        if page_index < 1 or page_size < 1:
            raise Proto2MySQLError(
                f"invalid page params: page_index={page_index}, page_size={page_size}"
            )
        self.find_all_with_options(
            list_message,
            where_clause,
            where_args,
            QueryOptions(limit=page_size, offset=(page_index - 1) * page_size),
        )

    def find_page_by_cursor(
        self,
        list_message: Message,
        where_clause: str,
        where_args: Sequence[Any] | None,
        cursor_field: str,
        cursor_val: Any,
        page_size: int,
    ) -> None:
        """游标分页（keyset pagination）：按 cursor_field 升序返回 cursor_val 之后的若干条。

        深分页时性能远好于 OFFSET，适合流水/邮件列表。首页传 cursor_val=None，
        下一页传上一页最后一条的 cursor_field 值。cursor_field 应有索引且唯一（如自增 id）。
        """
        if page_size < 1:
            raise Proto2MySQLError(f"invalid page_size: {page_size}")
        table, _ = self._resolve_list_table(list_message)
        table.field(cursor_field)

        where = normalize_where_clause(where_clause)
        args = list(where_args or [])
        if cursor_val is not None:
            where = f"({where}) AND {escape_mysql_name(cursor_field)} > ?"
            args.append(cursor_val)

        self.find_all_with_options(
            list_message,
            where,
            args,
            QueryOptions(order_by=f"{escape_mysql_name(cursor_field)} ASC", limit=page_size),
        )

    def find_all_by_kv_in(
        self, list_message: Message, key: str, values: Sequence[Any]
    ) -> None:
        """按某列的取值集合批量查。"""
        table, list_field = self._resolve_list_table(list_message)
        if not values:
            del getattr(list_message, list_field.name)[:]
            return
        table.field(key)
        where = f"{escape_mysql_name(key)} IN ({build_placeholders(len(values))})"
        self.find_all_by_where(list_message, where, values)

    def find_all_by_pk_in(self, list_message: Message, pk_values: Sequence[Any]) -> None:
        """按主键批量查（类似 Redis MGET：给一批主键，返回命中的行，不存在的自动跳过）。

        **复合主键**时，``pk_values`` 的每一项必须是与主键列数等长的元组/列表，
        且**按 ``primary_key`` 选项里的声明顺序**排列，生成
        ``WHERE (`pk1`, `pk2`) IN ((?,?), (?,?))``——与 :meth:`batch_delete` 同构。
        顺序错了不会报错（两列同为整型时更是毫无症状），只会查错行，
        与本方法当初那个缺陷是同一类，所以这条顺序是硬约定。

        Go 版 ``FindAllByPKIn`` 对复合主键直接 fail-closed（它的
        ``pkValues []interface{}`` 签名表达不了元组）。Python 这边签名表达得了，
        所以做成超集：标量在复合主键下照样拒，元组则正确执行。

        早先无论主键几列都只取 ``primary_key[0]``（Go 版 FindAllByPKIn 同构，
        那边也只认 primaryKeyField 一列）。复合主键下这条 SQL 会把**只是第一列相同**
        的行一并捞回来：给 ``(1, "wx")`` 查，回来的是所有 ``user_id = 1`` 的行，
        而多出来的那些会被原样塞进结果列表，调用方看不出条件少了一半。
        单列主键的 SQL 一字未改，跨语言对拍不受影响。
        """
        table, list_field = self._resolve_list_table(list_message)
        if not pk_values:
            del getattr(list_message, list_field.name)[:]
            return
        if not table.primary_key:
            raise PrimaryKeyNotFoundError(f"primary key not found: table {table.table_name}")

        width = len(table.primary_key)
        if width == 1:
            pk_name = escape_mysql_name(table.primary_key[0])
            where = f"{pk_name} IN ({build_placeholders(len(pk_values))})"
            self.find_all_by_where(list_message, where, pk_values)
            return

        if len(pk_values) > BATCH_INSERT_MAX_SIZE:
            # 与 batch_delete 同一条理由：一条 IN 里塞几万个元组会撞
            # max_allowed_packet。这里不自动分批（结果要合并进同一个列表消息，
            # 分批就得自己拼），直接拒掉并告诉调用方怎么办。
            raise Proto2MySQLError(
                f"pk_values 有 {len(pk_values)} 项，超过 {BATCH_INSERT_MAX_SIZE}："
                f"一条 (pk...) IN (...) 塞太多会撞 max_allowed_packet。请自行分批调用。"
            )
        pk_names = ", ".join(escape_mysql_name(pk) for pk in table.primary_key)
        args: list[Any] = []
        tuples: list[str] = []
        for value in pk_values:
            # str/bytes 本身也是序列，但它们是"一个值"而不是"一组值"，必须挡掉，
            # 否则 "wx" 会被摊成 'w','x' 两个参数，占位符数量还刚好对得上。
            if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
                raise Proto2MySQLError(
                    f"表 {table.table_name} 的主键有 {width} 列"
                    f"（{', '.join(table.primary_key)}），"
                    f"pk_values 的每一项必须是等长的元组，收到的是 {value!r}"
                )
            if len(value) != width:
                raise Proto2MySQLError(
                    f"表 {table.table_name} 的主键有 {width} 列"
                    f"（{', '.join(table.primary_key)}），"
                    f"但 pk_values 里有一项是 {len(value)} 个值：{value!r}"
                )
            args.extend(value)
            tuples.append(f"({build_placeholders(width)})")
        where = f"({pk_names}) IN ({', '.join(tuples)})"
        self.find_all_by_where(list_message, where, args)

    def count(
        self,
        message: Message,
        where_clause: str = "",
        where_args: Sequence[Any] | None = None,
    ) -> int:
        """统计行数（message 可以是行消息或列表消息）。"""
        table = self._resolve_any_table(message)
        value = self.query_one_value(
            f"SELECT COUNT(*) FROM {escape_mysql_name(table.table_name)} "
            f"WHERE {normalize_where_clause(where_clause)}",
            where_args,
        )
        return int(value or 0)

    def exists(
        self,
        message: Message,
        where_clause: str = "",
        where_args: Sequence[Any] | None = None,
    ) -> bool:
        """是否存在满足条件的行（``SELECT 1 ... LIMIT 1``，比 COUNT(*) 便宜）。"""
        table = self._resolve_any_table(message)
        rows = self.query(
            f"SELECT 1 FROM {escape_mysql_name(table.table_name)} "
            f"WHERE {normalize_where_clause(where_clause)} LIMIT 1",
            where_args,
        )
        return bool(rows)

    def exists_by_pk(self, message: Message) -> bool:
        """按消息中的主键值判断行是否存在。"""
        table = self._table_for_message(message)
        where_clause, where_args = table.primary_key_where(message)
        return self.exists(message, where_clause, where_args)

    def find_multi_by_where_clauses(self, queries: Sequence["MultiQuery"]) -> None:
        """一次往返查多张无关表，每张表返回一条结果。

        依赖服务端多语句支持：PyMySQL 要在 connect 时传
        ``client_flag=pymysql.constants.CLIENT.MULTI_STATEMENTS``，否则第二条语句会报语法错误。
        """
        if not queries:
            raise Proto2MySQLError("no queries provided")

        parts: list[str] = []
        all_args: list[Any] = []
        for q in queries:
            table = self._table_for_message(q.message)
            parts.append(f"{table.select_fields_sql} WHERE {q.where_clause}")
            all_args.extend(q.where_args or [])

        sql = "; ".join(parts)
        query, params = Statement(sql, all_args).for_paramstyle(self.paramstyle)
        with self._cursor() as cur:
            cur.execute(query, params)
            for idx, q in enumerate(queries):
                _scan_one_row(list(cur.fetchall()), q.message)
                if idx < len(queries) - 1 and not cur.nextset():
                    raise Proto2MySQLError(
                        f"missing result set for table {queries[idx + 1].message.DESCRIPTOR.full_name}"
                    )

    # ── 内部解析 ────────────────────────────────────────────────────────

    def _table_by_key(self, registry_key: str) -> MessageTable:
        table = self.tables.get(registry_key)
        if table is None:
            raise TableNotFoundError(f"table not found: {registry_key}")
        return table

    def _table_for_message(self, m: Message | type[Message]) -> MessageTable:
        """解析行消息对应的已注册表。"""
        self._require_managed_cache_backend()
        if self._cache is not None:
            # 运行期有人把连接从 autocommit 切回 False 时，要在任何 typed CRUD
            # 发 SQL **之前**拒绝；等写完才在失效阶段报错已经太晚。
            _require_cache_namespace(self.dbname)
            _require_cache_autocommit(self.connection)
        descriptor = m.DESCRIPTOR
        return self._table_by_key(descriptor.full_name)

    def _resolve_list_table(
        self, list_message: Message
    ) -> tuple[MessageTable, FieldDescriptor]:
        """从"含单个 repeated 字段的列表消息"解析出已注册的表和该字段。"""
        list_field = _single_repeated_field(list_message)
        return self._table_by_key(list_field.message_type.full_name), list_field

    def _resolve_any_table(self, message: Message) -> MessageTable:
        """行消息或列表消息都能解析。"""
        table = self.tables.get(message.DESCRIPTOR.full_name)
        if table is not None:
            return table
        try:
            table, _ = self._resolve_list_table(message)
        except Proto2MySQLError:
            raise TableNotFoundError(
                f"table not found: {message.DESCRIPTOR.full_name}"
            ) from None
        return table


class MultiQuery:
    """:meth:`DB.find_multi_by_where_clauses` 的单表查询参数。"""

    __slots__ = ("message", "where_clause", "where_args")

    def __init__(
        self, message: Message, where_clause: str, where_args: Sequence[Any] | None = None
    ) -> None:
        self.message = message
        self.where_clause = where_clause
        self.where_args = list(where_args or [])


# ── 模块级工具 ──────────────────────────────────────────────────────────


def _require_cache_autocommit(connection: Any) -> None:
    """缓存连接必须使用 autocommit；隐式事务无法提供正确的 cache-aside 语义。

    仅告警不够：写后第一次删除到外部 ``conn.commit()`` 之间，其他读者仍能把旧值
    回填；而包装器观察不到那次 commit，无法再删一次。多个 DB wrapper 共用同一连接
    时，pending 还可能挂在错误对象上。唯一可靠的公共契约是缓存连接用 autocommit，
    原子区间统一走 :meth:`DB.transaction`（库能观察 begin/commit/rollback）。
    """
    if connection is None:
        return
    if not _connection_autocommit(connection):
        raise Proto2MySQLError(
            "启用缓存的连接必须设置 autocommit=True；隐式事务的 conn.commit() "
            "无法触发提交后的缓存失效。需要原子性时请使用 db.transaction()"
        )
    if (
        _connection_server_in_transaction(connection)
        and id(connection) not in _ACTIVE_TX_CONNECTIONS
    ):
        raise Proto2MySQLError(
            "启用缓存时检测到连接处于包装器之外开启的外部事务；"
            "请 rollback/commit 后改用 db.transaction()，否则未提交数据可能污染共享缓存"
        )


def _require_cache_namespace(dbname: str) -> None:
    """安全 key 模式必须有真实库名，空 namespace 无法隔离不同数据库。"""
    if CACHE_KEY_NAMESPACED and not dbname:
        raise Proto2MySQLError(
            "启用缓存时必须提供非空 dbname，供缓存 key 做数据库 namespace；"
            "请使用 DB(connection, dbname) 或 open_db(connection, dbname)"
        )


def _connection_autocommit(connection: Any) -> bool:
    """连接当前是不是 autocommit 模式。

    PyMySQL / mysqlclient 提供 ``get_autocommit()``，部分驱动是 ``autocommit`` 属性。
    都读不到时按 DB-API 的默认（autocommit 关）处理——那种情况下连接本来就在事务里，
    不 begin 才是对的。
    """
    getter = getattr(connection, "get_autocommit", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception:  # noqa: BLE001 - 读不到就按默认走
            return False
    value = getattr(connection, "autocommit", None)
    if isinstance(value, bool):
        return value
    return False


def _connection_server_in_transaction(connection: Any) -> bool:
    """尽力读取 MySQL 协议的 SERVER_STATUS_IN_TRANS 标志。

    PyMySQL 在 ``autocommit=True`` 连接上执行 ``conn.begin()`` 后，
    ``get_autocommit()`` 仍是 True；仅靠 autocommit 门禁会把真实未提交数据写进缓存。
    PyMySQL/mysqlclient 都暴露服务端 status；其他驱动读不到时返回 False，随后仍由
    autocommit 契约和 :data:`_ACTIVE_TX_CONNECTIONS` 覆盖库管理的事务。
    """
    try:
        status = getattr(connection, "server_status", None)
    except Exception:  # noqa: BLE001 - 驱动属性读取失败按“不支持探测”降级
        return False
    return isinstance(status, int) and bool(status & 0x0001)


def _mysql_error_code(exc: BaseException) -> int | None:
    """从 DB-API 异常（或包装链）取 MySQL 数字错误码。"""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        args = getattr(cur, "args", ())
        if args and isinstance(args[0], int):
            return args[0]
        next_exc = cur.__cause__ or cur.__context__
        cur = next_exc if isinstance(next_exc, BaseException) else None
    return None


def _is_concurrent_ddl_conflict(exc: BaseException) -> bool:
    # 1050 table exists、1060 duplicate column、1061 duplicate key name、
    # 1068 multiple primary key：都可能是无咨询锁时另一副本刚完成同一变更。
    # 只重算一次；若签名仍不一致，第二次会按正常错误 fail-closed。
    return _mysql_error_code(exc) in {1050, 1060, 1061, 1068}


def _quoted_names(text: str) -> list[str]:
    """按出现顺序取出一段 SQL 里所有反引号标识符。"""
    out: list[str] = []
    i = 0
    while True:
        start = text.find("`", i)
        if start < 0:
            return out
        end = text.find("`", start + 1)
        if end < 0:
            return out
        out.append(text[start + 1 : end])
        i = end + 1


def _clause_target_column(clause: str) -> str:
    """这条 ALTER 子句最终**定义**的是哪一列（不是列定义则返回空串）。

    ``CHANGE COLUMN `old` `new` ...`` 取的是新名——改完之后叫这个名字的那一列，
    才是主键要指向的列。

    索引子句（``ADD INDEX`` / ``ADD UNIQUE KEY``）一律返回空串：它们里面同样有
    反引号列名，但那不是列定义，误判会把索引子句挪进补主键那条 ALTER 里去。
    """
    for prefix, which in (("ADD COLUMN ", 0), ("MODIFY COLUMN ", 0), ("CHANGE COLUMN ", 1)):
        if clause.startswith(prefix):
            names = _quoted_names(clause[len(prefix) :])
            return names[which] if len(names) > which else ""
    return ""


def _column_rename_map(clauses: Sequence[str]) -> dict[str, str]:
    """取同一 ALTER 里的 ``CHANGE COLUMN old new``，供索引签名投影。"""
    out: dict[str, str] = {}
    for clause in clauses:
        if not clause.startswith("CHANGE COLUMN "):
            continue
        names = _quoted_names(clause)
        if len(names) >= 2:
            out[names[0].lower()] = names[1].lower()
    return out


def _index_clause_columns(clause: str) -> list[str]:
    """``ADD INDEX`` / ``ADD UNIQUE KEY`` 子句引用到的列名（不是索引子句就返回空）。

    子句形如 ``ADD INDEX `idx_t_0` (`a`,`b`(191))``：第一个反引号标识符是索引名，
    其余才是列名。
    """
    for prefix in ("ADD INDEX ", "ADD UNIQUE KEY "):
        if clause.startswith(prefix):
            return _quoted_names(clause[len(prefix) :])[1:]
    return []


def _chunks(seq: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _scan_one_row(rows: list[tuple], message: Message) -> None:
    """把恰好一行写回 message。"""
    if not rows:
        raise NoRowsFoundError("no rows found")
    if len(rows) > 1:
        raise MultipleRowsFoundError("multiple rows found")
    pbconv.parse_from_row(message, rows[0])


def _scan_rows_to_list(
    rows: list[tuple], list_message: Message, list_field: FieldDescriptor
) -> None:
    """把结果集逐行反序列化并写进 repeated 字段（先清空旧数据）。"""
    container = getattr(list_message, list_field.name)
    del container[:]
    for row in rows:
        element = container.add()
        pbconv.parse_from_row(element, row)


def _single_repeated_field(list_message: Message) -> FieldDescriptor:
    """取列表消息里唯一的 repeated message 字段。"""
    if list_message is None:
        raise Proto2MySQLError("list message cannot be nil")

    found: FieldDescriptor | None = None
    for fd in list_message.DESCRIPTOR.fields:
        if not pbconv.is_list_field(fd):
            continue
        if found is not None:
            raise MultipleRepeatedFieldError("message has multiple repeated fields")
        found = fd

    if found is None:
        raise NoRepeatedFieldError("message has no repeated field")
    if found.message_type is None:
        raise Proto2MySQLError(f"repeated field {found.name} is not a message type")
    return found


def get_element_table_name(list_message: Message) -> str:
    """列表消息里 repeated 元素类型对应的表名（proto full name）。"""
    return _single_repeated_field(list_message).message_type.full_name
