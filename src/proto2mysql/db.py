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
from types import ModuleType
from typing import Any, Iterable, Iterator, Sequence

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.message import Message

from . import pbconv
from .cache import Cache, decode_entry, encode_entry
from .errors import (
    CacheMissError,
    DuplicateKeyError,
    FieldNotFoundError,
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
    ColumnMeta,
    MessageTable,
    Statement,
    build_placeholders,
    escape_mysql_name,
    parse_field_num_from_comment,
)

log = logging.getLogger("proto2mysql")

_MYSQL_DUPLICATE_ENTRY = 1062


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

    # ── 连接 ────────────────────────────────────────────────────────────

    def open_db(self, connection: Any, dbname: str) -> "DB":
        """绑定连接并切库（对应 Go 的 OpenDB）。"""
        self.connection = connection
        self.dbname = dbname
        self.execute(f"USE {escape_mysql_name(dbname)}")
        return self

    def bind(self, connection: Any) -> "DB":
        """返回绑定新连接的实例，**共享表注册表与缓存配置**。

        多线程/多请求的正确用法：进程启动时注册一次表，之后每个连接 bind 一下。
        """
        return DB(
            connection,
            self.dbname,
            paramstyle=self.paramstyle,
            tables=self.tables,
            cache=self._cache,
            cache_ttl=self._cache_ttl,
            expand_only=self.expand_only,
        )

    def close(self) -> None:
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

        启用缓存时，事务内的缓存失效**延迟到提交成功之后**执行——
        回滚后不删缓存，否则会把还有效的缓存误删（Go 版同款语义）。
        """
        if self._in_transaction:
            raise Proto2MySQLError("nested transaction is not supported")

        tx = DB(
            self.connection,
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
        if _connection_autocommit(self.connection):
            begin = getattr(self.connection, "begin", None)
            if callable(begin):
                begin()
        try:
            yield tx
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        self._cache_del_keys(*tx._pending_cache_dels)

    # ── 缓存 ────────────────────────────────────────────────────────────

    def enable_cache(self, cache: Cache, ttl: float | None = None) -> None:
        """启用 cache-aside 缓存（按主键的单行读写生效）。

        语义：

        * 读（:meth:`find_one_by_pk` / :meth:`find_or_create` 命中路径）：
          先查缓存，未命中读 DB 后回填；
        * 写（按主键的 save/update/delete/incr 等）：先写 DB，成功后删缓存；
        * 事务：删缓存延迟到提交成功之后，避免回滚后缓存脏删；
        * 降级：缓存出错**仅记日志**，不影响 DB 结果（弱依赖）。

        注意：按 WHERE 条件的更新/删除无法定位受影响主键，**不做缓存失效**；
        缓存表请优先用按主键的接口，或调用 :meth:`invalidate_cache` 手动失效。
        """
        self._cache = cache
        self._cache_ttl = ttl

    def _cache_enabled(self) -> bool:
        return self._cache is not None

    def cache_key(self, message: Message) -> str:
        """message 对应的缓存 key：``pb:<表名>:<主键值...>``

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

    @staticmethod
    def _cache_key_for(table: MessageTable, message: Message) -> str:
        values = table.primary_key_values(message)
        parts = "".join(f":{v}" for v in values)
        return f"pb:{table.table_name}{parts}"

    def invalidate_cache(self, *messages: Message) -> None:
        """手动失效一批消息的缓存（按 WHERE 批量写后调用）。"""
        if not self._cache_enabled() or not messages:
            return
        keys = [self.cache_key(m) for m in messages]
        self._cache.delete(*keys)

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
        if not self._cache_enabled() or not keys:
            return
        try:
            self._cache.delete(*keys)
        except Exception as exc:  # noqa: BLE001 - 删不掉只会脏读到 TTL 为止
            log.warning("cache del %s failed (stale until ttl): %s", keys, exc)

    def _invalidate_messages(self, table: MessageTable, *messages: Message) -> None:
        """写 DB 成功后失效缓存：事务内先暂存，提交成功后统一删除。"""
        if not self._cache_enabled():
            return
        keys = []
        for msg in messages:
            try:
                keys.append(self._cache_key_for(table, msg))
            except Proto2MySQLError:
                continue  # 无主键的表不参与缓存
        if not keys:
            return
        if self._in_transaction:
            self._pending_cache_dels.extend(keys)
            return
        self._cache_del_keys(*keys)

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
        """线上表当前是否已存在主键约束。

        用 TABLE_CONSTRAINTS 判定，不看具体列——本库只补"从无到有"的主键，
        不做主键列的比对/改写（那需要 DROP+ADD，属破坏性操作）。
        """
        count = self.query_one_value(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND CONSTRAINT_TYPE = 'PRIMARY KEY'",
            [self.dbname, table_name],
        )
        return bool(count)

    def get_table_columns(self, registry_key: str) -> dict[str, str]:
        """线上表的列名 -> 列类型（带表级缓存）。"""
        table = self._table_by_key(registry_key)
        if table.cached_columns is not None:
            return table.cached_columns
        rows = self.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [self.dbname, table.table_name],
        )
        columns = {name: col_type for name, col_type in rows}
        table.cached_columns = columns
        return columns

    def get_table_column_meta(self, registry_key: str) -> dict[str, ColumnMeta]:
        """线上表每列的类型 + 从注释解析出的 proto 字段号。

        迁移时靠字段号识别列，从而支持改名保留数据。**不走列类型缓存**：
        迁移不频繁，且这里要的是注释信息，混用会读到只有类型的旧缓存。
        """
        table = self._table_by_key(registry_key)
        rows = self.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [self.dbname, table.table_name],
        )
        metas: dict[str, ColumnMeta] = {}
        for name, col_type, comment in rows:
            num, ok = parse_field_num_from_comment(comment or "")
            metas[name] = ColumnMeta(col_type=col_type, field_num=num if ok else 0)
        return metas

    def clear_column_cache(self, registry_key: str) -> None:
        table = self.tables.get(registry_key)
        if table is not None:
            table.cached_columns = None

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

    def _existing_index_names(self, table_name: str) -> set[str]:
        """线上这张表已有的索引名（不含 PRIMARY）。"""
        rows = self.query(
            "SELECT DISTINCT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [self.dbname, table_name],
        )
        return {r[0] for r in rows if r and r[0] and r[0] != "PRIMARY"}

    def _missing_index_clauses(self, table: MessageTable) -> list[str]:
        """proto 里声明了、线上却没有的索引，生成 ADD INDEX / ADD UNIQUE KEY。

        **只加不删**：线上多出来的索引一律不动（可能是 DBA 按查询模式手工加的，
        库没有立场去删）。与「永不 DROP COLUMN」是同一个立场。

        索引名与 CREATE TABLE 分支保持一致（``idx_<表名>_<序号>`` / ``uk_<表名>``），
        否则同一份 proto 在"新建表"和"老表补索引"两条路径上会产出不同的索引名。
        """
        if not table.indexes and not table.unique_keys:
            return []  # proto 里一个索引都没声明，不必去查 information_schema

        try:
            existing = self._existing_index_names(table.table_name)
        except Exception as exc:  # noqa: BLE001 - 查不到就跳过，不阻断结构同步
            log.warning("读取表 %s 的既有索引失败，本次跳过索引补齐：%s", table.table_name, exc)
            return []

        clauses: list[str] = []
        for idx, index_cols in enumerate(table.indexes):
            name = f"idx_{table.table_name}_{idx}"
            if name in existing:
                continue
            cols = ",".join(table._index_column(c.strip()) for c in index_cols.split(","))
            clauses.append(f"ADD INDEX {escape_mysql_name(name)} ({cols})")
            log.info("table %s 补索引 %s (%s)", table.table_name, name, cols)

        if table.unique_keys:
            name = f"uk_{table.table_name}"
            if name not in existing:
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

    def _sync_table_schema(self, registry_key: str, table: MessageTable) -> None:
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
            self.execute(table.get_create_table_sql())
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

        alter_sqls = table.build_alter_clauses(
            self.get_table_column_meta(registry_key), expand_only=self.expand_only
        )

        # 补齐缺失的主键。
        #
        # build_alter_clauses 只对齐列（ADD/MODIFY/CHANGE COLUMN），从不看主键。
        # 主键必须与列变更放进**同一条** ALTER：主键列常同时带 AUTO_INCREMENT，
        # 若先单独 MODIFY 成 AUTO_INCREMENT、再 ADD PRIMARY KEY，MySQL 会在第一条
        # 就报 Error 1075（auto column must be defined as a key），永远到不了补主键那步。
        #
        # 这里只补"从无到有"，绝不自动 DROP/改写已有主键。ADD PRIMARY KEY 在已有重复行时
        # 会失败；这是预期的 fail-closed 行为，调用方必须先人工去重再重试。
        missing_pk = False
        if table.primary_key and not self.table_has_primary_key(table.table_name):
            missing_pk = True
            cols = ",".join(escape_mysql_name(pk) for pk in table.primary_key)
            alter_sqls.append(f"ADD PRIMARY KEY ({cols})")
            log.warning(
                "table %s is missing its primary key; adding %s", table.table_name, cols
            )

        # 补齐 proto 里声明了、但线上还没有的索引。
        #
        # 早先索引只出现在 CREATE TABLE 分支：表一旦建成，之后在 .proto 里新加
        # index / unique_key **完全不生效**，而且零提示——查询照常能跑，只是走全表扫描，
        # 数据量上来才表现为"莫名其妙变慢"，谁也想不到是建表选项没落地。
        alter_sqls.extend(self._missing_index_clauses(table))

        if not alter_sqls:
            return

        alter_sql = (
            f"ALTER TABLE {escape_mysql_name(table.table_name)} {', '.join(alter_sqls)}"
        )

        try:
            self.execute(alter_sql)
        except Exception as exc:
            if missing_pk:
                raise Proto2MySQLError(
                    f"更新表 {table.table_name} 结构并补齐主键失败"
                    f"（可能存在重复行，需先去重再重试）: {exc}, SQL: {alter_sql}"
                ) from exc
            raise Proto2MySQLError(
                f"更新表 {table.table_name} 结构失败: {exc}, SQL: {alter_sql}"
            ) from exc
        self.clear_column_cache(registry_key)
        self._await_schema_visible(registry_key, table, alter_sqls)

    #: 等 DDL 在所有节点生效的最长秒数与轮询间隔。
    SCHEMA_SETTLE_TIMEOUT = 60.0
    SCHEMA_SETTLE_INTERVAL = 0.2

    @staticmethod
    def _added_column_names(alter_sqls: Sequence[str]) -> set[str]:
        """从 ALTER 子句里挑出本次**新增**的列名。

        只等这些列：MODIFY / CHANGE 改的是已有列，回读时本来就看得见，
        等它们既没意义又会把探测拖长。
        """
        names: set[str] = set()
        for clause in alter_sqls:
            if not clause.startswith("ADD COLUMN `"):
                continue
            rest = clause[len("ADD COLUMN `") :]
            end = rest.find("`")
            if end > 0:
                names.add(rest[:end].replace("``", "`"))
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
        want = self._added_column_names(alter_sqls)
        if not want:
            return  # 本次没有新增列（只有 MODIFY / CHANGE / 索引），无需等待

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
        * 表已存在   → ALTER TABLE（新增字段 / 按字段号改名 / 类型对齐）
        * 无任何差异 → 空串

        与 :meth:`update_table_field` 的区别：**只产出 SQL 不执行**，
        便于生成迁移文件交人工/CI 审核。
        """
        table = self._table_for_message(m)
        if not self.is_table_exists(table.table_name):
            return table.get_create_table_sql()

        alter_sqls = table.build_alter_clauses(
            self.get_table_column_meta(table.descriptor.full_name), expand_only=self.expand_only
        )
        if not alter_sqls:
            return ""
        return f"ALTER TABLE {escape_mysql_name(table.table_name)} {', '.join(alter_sqls)};"

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
        """批量插入，超过 BATCH_INSERT_MAX_SIZE 自动分批。"""
        if not messages:
            raise Proto2MySQLError("no messages to insert")
        table = self._table_for_message(messages[0])
        for batch in _chunks(messages, BATCH_INSERT_MAX_SIZE):
            self._exec_stmt(table.get_batch_insert_sql(batch))

    def insert_ignore(self, message: Message) -> bool:
        """幂等插入（INSERT IGNORE）：冲突时跳过不报错。返回是否真的插入了新行。"""
        table = self._table_for_message(message)
        stmt = table.get_insert_sql(message)
        affected = self.execute("INSERT IGNORE" + stmt.sql[len("INSERT") :], stmt.args)
        return affected > 0

    def insert_returning_id(self, message: Message) -> int:
        """插入并返回自增主键（LAST_INSERT_ID）。自增主键表建议用这个。"""
        table = self._table_for_message(message)
        return self._exec_returning_lastrowid(table.get_insert_sql(message))

    def insert_on_dup_update(self, message: Message) -> None:
        """INSERT ... ON DUPLICATE KEY UPDATE（冲突时用已赋值字段覆盖）。"""
        table = self._table_for_message(message)
        self._exec_stmt(table.get_insert_on_dup_update_sql(message))
        self._invalidate_messages(table, message)

    def save(self, message: Message) -> None:
        """整行落库：有则更新、无则插入。

        走 ``INSERT ... ON DUPLICATE KEY UPDATE``，**不是** ``REPLACE INTO``。
        REPLACE 的语义是「先 DELETE 再 INSERT」，语句里没提到的列会**回到默认值**——
        而列清单来自本进程的 descriptor，所以滚动发布时旧版本进程 save 一次，
        新版本刚写进去的列就没了，且零报错。ODKU 只动子句里点名的列，
        本进程不认识的列原样保留。

        需要「整行推倒重来」的旧语义时，显式用 ``SQLBuilder.replace``。
        """
        table = self._table_for_message(message)
        self._exec_stmt(table.get_save_sql(message))
        self._invalidate_messages(table, message)

    def batch_save(self, messages: Sequence[Message]) -> None:
        """批量整行落库（自动分批），语义同 :meth:`save`。"""
        if not messages:
            return
        table = self._table_for_message(messages[0])
        for msg in messages:
            table.validate_message(msg)
        for batch in _chunks(messages, BATCH_INSERT_MAX_SIZE):
            self._exec_stmt(table.get_batch_save_sql(batch))
        self._invalidate_messages(table, *messages)

    # ── UPDATE ──────────────────────────────────────────────────────────

    def update(self, message: Message) -> None:
        """按主键更新消息中**已赋值**的字段。"""
        table = self._table_for_message(message)
        self._exec_stmt(table.get_update_sql(message))
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
        self.execute(
            f"UPDATE {escape_mysql_name(table.table_name)} SET {', '.join(clauses)} "
            f"WHERE {where_clause}",
            args + where_args,
        )
        self._invalidate_messages(table, message)

    def update_kv_by_pk(self, message: Message, field: str, value: Any) -> None:
        """按主键设置单个字段的值（改状态、封号一类）。"""
        table = self._table_for_message(message)
        table.field(field)  # 未知列直接抛错
        where_clause, where_args = table.primary_key_where(message)
        self.execute(
            f"UPDATE {escape_mysql_name(table.table_name)} "
            f"SET {escape_mysql_name(field)} = ? WHERE {where_clause}",
            [value] + where_args,
        )
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
        affected = self.execute(sql, args + where_args + [cur_version])
        if affected > 0:
            self._invalidate_messages(table, message)
        return affected > 0

    def incr_by_pk(self, message: Message, field: str, delta: int) -> None:
        """按主键对数值字段原子加减，避免"读-改-写"竞态。"""
        table = self._table_for_message(message)
        table.field(field)
        where_clause, where_args = table.primary_key_where(message)
        escaped = escape_mysql_name(field)
        self.execute(
            f"UPDATE {escape_mysql_name(table.table_name)} SET {escaped} = {escaped} + ? "
            f"WHERE {where_clause}",
            [delta] + where_args,
        )
        self._invalidate_messages(table, message)

    def decr_by_pk_if_enough(self, message: Message, field: str, delta: int) -> bool:
        """按主键原子扣减，余额不足时不扣并返回 False（防止负数余额）。"""
        if delta < 0:
            raise Proto2MySQLError(f"delta must be non-negative, got {delta}")
        table = self._table_for_message(message)
        table.field(field)
        where_clause, where_args = table.primary_key_where(message)
        escaped = escape_mysql_name(field)
        affected = self.execute(
            f"UPDATE {escape_mysql_name(table.table_name)} SET {escaped} = {escaped} - ? "
            f"WHERE {where_clause} AND {escaped} >= ?",
            [delta] + where_args + [delta],
        )
        if affected > 0:
            self._invalidate_messages(table, message)
        return affected > 0

    # ── DELETE ──────────────────────────────────────────────────────────

    def delete(self, message: Message) -> None:
        """按主键删除。"""
        table = self._table_for_message(message)
        self._exec_stmt(table.get_delete_sql(message))
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
        """按主键批量删除（``WHERE (pk...) IN ((..),(..))``，自动分批）。"""
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
            self.delete_by_where(messages[0], where, args)
        self._invalidate_messages(table, *messages)

    # ── SELECT ──────────────────────────────────────────────────────────

    def find_one_by_pk(self, message: Message) -> None:
        """按消息中的主键值查一行，查到后写回 message。

        启用缓存时这是 cache-aside 读路径：先查缓存，未命中读 DB 后回填。
        **事务内不走缓存**（需要读到事务内未提交的最新值）。
        """
        table = self._table_for_message(message)
        use_cache = self._cache_enabled() and not self._in_transaction
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
        if not self._in_transaction:
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
        """按主键批量查（类似 Redis MGET：给一批主键，返回命中的行，不存在的自动跳过）。"""
        table, list_field = self._resolve_list_table(list_message)
        if not pk_values:
            del getattr(list_message, list_field.name)[:]
            return
        if not table.primary_key:
            raise PrimaryKeyNotFoundError(f"primary key not found: table {table.table_name}")
        pk_name = escape_mysql_name(table.primary_key[0])
        where = f"{pk_name} IN ({build_placeholders(len(pk_values))})"
        self.find_all_by_where(list_message, where, pk_values)

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
