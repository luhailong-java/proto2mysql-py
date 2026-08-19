"""MessageTable：protobuf 消息 <-> MySQL 表的映射、建表 DDL、迁移子句与 DML 片段。

对应 Go 版 proto2mysql.go 里挂在 *MessageTable 上的那一半（另一半 *DB 在 db.py）。

生成的 SQL 与 Go 版**逐字节一致**——这是刻意的硬约束：Go 版测试里断言的 SQL 字符串
被原样搬成了本仓库的黄金用例（tests/test_golden_sql.py），两边跑同一份 .proto
必须产出同一份 DDL/DML，否则"并存迁移"就没有可验证的基准。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as _dc_field
from typing import Any, Iterable, Sequence

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.message import Message

from . import pbconv
from .errors import (
    BatchSizeExceededError,
    FieldNotFoundError,
    PrimaryKeyNotFoundError,
    Proto2MySQLError,
)
from .options import TableOption, apply_options, table_options_from_descriptor

BATCH_INSERT_MAX_SIZE = 1000  # 批量插入最大条数

# 与 MySQL 关键字冲突的标识符（建表/迁移时只告警，不改写——改写会让列名对不上）
_MYSQL_KEYWORD_PATTERN = (
    r"^(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|AND|OR|JOIN|ON|IN|NOT|NULL|PRIMARY|KEY|"
    r"INDEX|UNIQUE|AUTO_INCREMENT|INT|VARCHAR|TEXT|BLOB|DATETIME|TIMESTAMP|FLOAT|DOUBLE|"
    r"BOOL|TINYINT|BIGINT)$"
)
_keyword_regex = re.compile(_MYSQL_KEYWORD_PATTERN)

# 与 Go 版一致：schema 上的可疑之处只记日志，不改写、不中断。
# 刻意不用 warnings.warn —— 那是给 API 误用/废弃用的，而且调用方一开 -W error
# 就会因为"表名撞了个关键字"整个挂掉。
log = logging.getLogger("proto2mysql")

_T = FieldDescriptor

# MySQL 字段类型映射表。键是 protobuf 的 FieldDescriptorProto.Type，
# 与 Go 的 protoreflect.Kind 是同一套编号，所以两边这张表可以逐行对照。
#
# 没列出的类型（sint32/sfixed64/fixed32 等）会落到 _DEFAULT_FIELD_TYPE。
# 这是 Go 版的既有行为，本库保持一致：建表给 TEXT，写入时 pbconv 抛
# InvalidFieldKindError。也就是说这些类型在 Go 版本来就用不了，别当成 Python 版的缺陷。
MYSQL_FIELD_TYPES: dict[int, str] = {
    _T.TYPE_INT32: "int NOT NULL DEFAULT 0",
    _T.TYPE_UINT32: "int unsigned NOT NULL DEFAULT 0",
    _T.TYPE_FLOAT: "float NOT NULL DEFAULT 0",
    _T.TYPE_STRING: "MEDIUMTEXT",
    _T.TYPE_INT64: "bigint NOT NULL DEFAULT 0",
    _T.TYPE_UINT64: "bigint unsigned NOT NULL DEFAULT 0",
    _T.TYPE_DOUBLE: "double NOT NULL DEFAULT 0",
    _T.TYPE_BOOL: "tinyint(1) NOT NULL DEFAULT 0",
    _T.TYPE_ENUM: "int NOT NULL DEFAULT 0",
    _T.TYPE_BYTES: "MEDIUMBLOB",
    _T.TYPE_MESSAGE: "MEDIUMBLOB",
}
_DEFAULT_FIELD_TYPE = "TEXT"

# 列注释中记录 proto 字段号的前缀，形如 COMMENT 'pb:3'。
# 迁移时据此按字段号识别列，从而支持字段改名（CHANGE COLUMN）并保留原有数据。
COLUMN_COMMENT_PREFIX = "pb:"

# protobuf 合法字段号区间（与 Go 的 protowire.MinValidNumber/MaxValidNumber 一致）
_MIN_VALID_FIELD_NUMBER = 1
_MAX_VALID_FIELD_NUMBER = (1 << 29) - 1

# 在 TEXT/BLOB 列上建索引时使用的前缀长度。
#
# MySQL 不允许对 TEXT/BLOB 列建不带前缀长度的索引（Error 1170）。而 string 按 Go 版的
# 映射规则落在 MEDIUMTEXT 上，所以只要 proto 里对 string 列声明了 index / unique_key，
# 不补前缀长度产出的就是一条建不了表的 DDL。
#
# 191 是 utf8mb4 下的经典安全值（旧的 767 字节索引上限 ÷ 4）。
# 设为 0 表示不补前缀 —— 输出与 Go 版逐字节一致，但那条 DDL MySQL 会拒绝，
# 只在做跨语言字符串比对时才有意义。
TEXT_INDEX_PREFIX_LENGTH = 191


def escape_mysql_name(name: str) -> str:
    """整体转义 MySQL 标识符，兼容包含点号的 protobuf full name 表名。"""
    return "`" + name.replace("`", "``") + "`"


def escape_mysql_comment(comment: str) -> str:
    """转义 MySQL 注释里的特殊字符（仅保留基础转义，与 Go 版一致）。"""
    return comment.replace("'", "\\'").replace("\n", " ")


def column_comment(num: int) -> str:
    """生成带 proto 字段号的列注释片段（含前导空格），如 " COMMENT 'pb:3'"。"""
    return f" COMMENT '{COLUMN_COMMENT_PREFIX}{num}'"


def parse_field_num_from_comment(comment: str) -> tuple[int, bool]:
    """从列注释解析 proto 字段号；无 pb:N 前缀或非法时返回 (0, False)。"""
    if not comment.startswith(COLUMN_COMMENT_PREFIX):
        return 0, False
    try:
        num = int(comment[len(COLUMN_COMMENT_PREFIX) :])
    except ValueError:
        return 0, False
    if num < _MIN_VALID_FIELD_NUMBER or num > _MAX_VALID_FIELD_NUMBER:
        return 0, False
    return num, True


def build_placeholders(count: int) -> str:
    """生成 "?, ?, ?" 形式的占位符串。"""
    if count <= 0:
        return ""
    return ", ".join("?" * count)


@dataclass
class MySQLTypeInfo:
    """从列类型字符串解析出的信息，如 "bigint(20) unsigned"。"""

    base_type: str = ""
    length: int = 0
    decimal: int = 0
    unsigned: bool = False


def parse_mysql_type(col_type: str) -> MySQLTypeInfo:
    """解析 MySQL 列类型字符串。"""
    info = MySQLTypeInfo()
    parts = col_type.lower().split()
    if not parts:
        return info

    base_part = parts[0]
    idx = base_part.find("(")
    if idx != -1:
        info.base_type = base_part[:idx]
        params = base_part[idx:].strip("()")
        if "," in params:
            pieces = params.split(",")
            info.length = _atoi(pieces[0])
            if len(pieces) >= 2:
                info.decimal = _atoi(pieces[1])
        else:
            info.length = _atoi(params)
    else:
        info.base_type = base_part

    info.unsigned = "unsigned" in parts[1:]
    return info


def _atoi(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        return 0


# 基础类型归一表：把等价写法折叠到同一个名字再比较
_TYPE_ALIASES = {
    "bool": "tinyint",
    "integer": "int",
    "mediumtext": "mediumtext",
    "text": "text",
    "blob": "blob",
    "mediumblob": "mediumblob",
    "datetime": "datetime",
    "timestamp": "datetime",
    "varchar": "varchar",
    "char": "char",
}


def is_type_match(current_type: str, target_type: str) -> bool:
    """判断线上列类型与目标类型是否兼容（不兼容才需要 MODIFY COLUMN）。"""
    current = parse_mysql_type(current_type)
    target = parse_mysql_type(target_type)

    current_base = _TYPE_ALIASES.get(current.base_type) or current.base_type
    target_base = _TYPE_ALIASES.get(target.base_type) or target.base_type

    if current_base != target_base:
        return False

    if current_base in ("varchar", "char"):
        # 目标长度 >= 当前长度视为兼容
        return target.length >= current.length
    if current_base in ("int", "bigint", "tinyint", "smallint"):
        # 无符号属性必须一致
        return current.unsigned == target.unsigned
    if current_base in ("float", "double"):
        return target.decimal >= current.decimal
    if current_base == "datetime":
        # 括号里的是小数秒精度(fsp)。线上精度低于目标时必须 ALTER，
        # 否则老表停留在 DATETIME(0)，写入的毫秒会被静默丢掉——精度修了等于没修。
        # 线上精度更高时不动它（降精度会丢数据）。
        return current.length >= target.length

    return True


@dataclass
class ColumnMeta:
    """线上单列的元信息。

    field_num 为 0 表示该列没有 pb:N 注释，通常是旧版本创建的表。
    """

    col_type: str
    field_num: int = 0


@dataclass
class Statement:
    """带占位符的 SQL 与其参数，对应 Go 的 SqlWithArgs。

    ``sql`` 用 ``?`` 占位（与 Go 的 database/sql 一致，也让黄金用例可以跨语言比对）。
    Python 的 DB-API 驱动大多是 ``pyformat``/``format`` 风格（``%s``），
    执行前用 :meth:`for_paramstyle` 转换，别自己手写替换——``%`` 字面量必须同时转义，
    漏了会让 ``LIKE '%x%'`` 这种 where 子句在 PyMySQL 里抛
    ``ValueError: unsupported format character``。
    """

    sql: str
    args: list = _dc_field(default_factory=list)

    def for_paramstyle(self, paramstyle: str = "format") -> tuple[str, tuple]:
        """转换成目标 paramstyle，返回 (sql, args) 可直接交给 cursor.execute。

        - ``qmark``  : 原样返回（SQLite / 直接和 Go 版比对时用）
        - ``format`` : ``?`` → ``%s``，并把已有的 ``%`` 转义成 ``%%``（PyMySQL / aiomysql）
        """
        if paramstyle == "qmark":
            return self.sql, tuple(self.args)
        if paramstyle != "format":
            raise ValueError(f"unsupported paramstyle: {paramstyle}")

        out = []
        for ch in self.sql:
            if ch == "%":
                out.append("%%")
            elif ch == "?":
                out.append("%s")
            else:
                out.append(ch)
        return "".join(out), tuple(self.args)


class MessageTable:
    """一个 protobuf 消息对应的表映射。

    构造时把建表配置合并好（proto option 在前、代码传入的 TableOption 在后覆盖），
    并预生成常用 SQL 片段——与 Go 版一样，构造完就只读。
    """

    __slots__ = (
        "table_name",
        "descriptor",
        "message_class",
        "primary_key",
        "indexes",
        "unique_keys",
        "auto_increase_key",
        "nullable_fields",
        "_fields",
        "_field_by_name",
        "_fields_list_sql",
        "_select_fields_sql",
        "_insert_sql_template",
        "_replace_sql_prefix",
        "_primary_key_field",
        "_encoders",
        "_cached_columns",
    )

    def __init__(
        self,
        descriptor: Descriptor,
        opts: Iterable[TableOption] = (),
        message_class: type[Message] | None = None,
    ) -> None:
        self.table_name: str = descriptor.full_name
        self.descriptor: Descriptor = descriptor
        self.message_class = message_class
        self.primary_key: list[str] = []
        self.indexes: list[str] = []
        self.unique_keys: str = ""
        self.auto_increase_key: str = ""
        self.nullable_fields: list[str] = []
        self._cached_columns: dict[str, str] | None = None

        apply_options(self, table_options_from_descriptor(descriptor))
        apply_options(self, opts)
        self._init_sql_fragments()

    # ── 构造 ────────────────────────────────────────────────────────────

    @classmethod
    def from_message(cls, message: Message | type[Message], opts: Iterable[TableOption] = ()) -> "MessageTable":
        """从消息实例或消息类构造，对应 Go 的 newMessageTable。"""
        msg_cls = message if isinstance(message, type) else type(message)
        return cls(msg_cls.DESCRIPTOR, opts, message_class=msg_cls)

    @classmethod
    def from_descriptor(cls, descriptor: Descriptor, opts: Iterable[TableOption] = ()) -> "MessageTable":
        """只有描述符时构造（自动注册路径）。消息类按需从 descriptor pool 解析。"""
        return cls(descriptor, opts)

    def _init_sql_fragments(self) -> None:
        """预生成 SQL 片段（注册时调用一次）。对应 Go 的 Init()。"""
        self._fields: list[FieldDescriptor] = list(self.descriptor.fields)
        self._field_by_name: dict[str, FieldDescriptor] = {f.name: f for f in self._fields}

        names = [escape_mysql_name(f.name) for f in self._fields]
        self._fields_list_sql = ", ".join(names)
        # 注册期把每列的编码闭包定死，热路径直接迭代（见 all_field_args）
        self._encoders = [pbconv.value_encoder(fd) for fd in self._fields]

        escaped_table = escape_mysql_name(self.table_name)
        self._select_fields_sql = f"SELECT {self._fields_list_sql} FROM {escaped_table}"
        self._insert_sql_template = (
            f"INSERT INTO {escaped_table} ({self._fields_list_sql}) "
            f"VALUES ({build_placeholders(len(self._fields))})"
        )
        self._replace_sql_prefix = f"REPLACE INTO {escaped_table} ({self._fields_list_sql}) VALUES ("

        self._primary_key_field = (
            self._field_by_name.get(self.primary_key[0]) if self.primary_key else None
        )

    def new_message(self) -> Message:
        """新建一个该表对应的空消息。"""
        if self.message_class is None:
            from google.protobuf import message_factory

            self.message_class = message_factory.GetMessageClass(self.descriptor)
        return self.message_class()

    # ── 元信息 ──────────────────────────────────────────────────────────

    @property
    def fields(self) -> list[FieldDescriptor]:
        return self._fields

    @property
    def select_fields_sql(self) -> str:
        """``SELECT <所有列> FROM <表>``，不带 WHERE、不带分号。"""
        return self._select_fields_sql

    @property
    def fields_list_sql(self) -> str:
        """转义后的列名列表，如 "`id`, `ip`"。"""
        return self._fields_list_sql

    def field(self, name: str) -> FieldDescriptor:
        """按列名取字段描述符，不存在时抛 FieldNotFoundError。"""
        fd = self._field_by_name.get(name)
        if fd is None:
            raise FieldNotFoundError(f"field not found in message: {name} in table {self.table_name}")
        return fd

    def has_field(self, name: str) -> bool:
        return name in self._field_by_name

    def is_nullable_field(self, name: str) -> bool:
        return name in self.nullable_fields

    def is_auto_increment_field(self, name: str) -> bool:
        return self.auto_increase_key == name

    def get_mysql_field_type(self, fd: FieldDescriptor) -> str:
        """字段对应的 MySQL 目标列类型。

        Timestamp 有两条硬约束：

        - **DATETIME(6)**：不带小数秒精度会把毫秒/纳秒**静默**截断到整秒（不报错、无警告）。
          MySQL 时间类型最高到微秒，纳秒位仍会丢，这是 MySQL 的硬上限。
        - **恒定可空**：proto 的 message 字段天然是"有/无"两态，未设置时唯一正确的表示
          是 NULL（空串在 STRICT 模式下被拒，'0000-00-00' 在 NO_ZERO_DATE 下非法）。
          声明成 NOT NULL 的话，凡是没赋值该字段的行都插不进去，等于把这列变成必填，
          所以这里**不受 nullable 选项影响**。
        """
        if fd.message_type is not None and fd.message_type.full_name == pbconv.TIMESTAMP_FULL_NAME:
            return "DATETIME(6)"

        if fd.is_repeated:  # map / list 统一用 MEDIUMBLOB
            return "MEDIUMBLOB"

        base_type = MYSQL_FIELD_TYPES.get(fd.type, _DEFAULT_FIELD_TYPE)

        if self.is_nullable_field(fd.name):
            base_type = base_type.replace(" NOT NULL", "")

        if self.is_auto_increment_field(fd.name):
            # 移除 DEFAULT 0：自增列带默认值会被 MySQL 拒（Error 1067）
            base_type = base_type.replace(" DEFAULT 0", "")
            base_type += " AUTO_INCREMENT"

        return base_type

    # ── DDL ─────────────────────────────────────────────────────────────

    def get_create_table_sql(self) -> str:
        """生成 CREATE TABLE 语句（与 Go 版逐字节一致）。"""
        lines: list[str] = []
        index_lines: list[str] = []

        for fd in self._fields:
            lines.append(
                f"  {escape_mysql_name(fd.name)} {self.get_mysql_field_type(fd)}"
                f"{column_comment(fd.number)}"
            )

        if self.primary_key:
            cols = ",".join(self._index_column(pk) for pk in self.primary_key)
            lines.append(f"  PRIMARY KEY ({cols})")

        for idx, index_cols in enumerate(self.indexes):
            cols = ",".join(self._index_column(c.strip()) for c in index_cols.split(","))
            index_name = f"idx_{self.table_name}_{idx}"
            index_lines.append(f"  INDEX {escape_mysql_name(index_name)} ({cols})")

        if self.unique_keys:
            unique_cols = [c.strip() for c in self.unique_keys.split(",")]
            for col in unique_cols:
                if self._needs_index_prefix(col):
                    log.warning(
                        "unique key on TEXT/BLOB column %s in table %s only enforces "
                        "uniqueness over the first %d characters",
                        col, self.table_name, TEXT_INDEX_PREFIX_LENGTH,
                    )
            cols = ",".join(self._index_column(c) for c in unique_cols)
            index_lines.append(f"  UNIQUE KEY {escape_mysql_name('uk_' + self.table_name)} ({cols})")

        stmt = f"CREATE TABLE IF NOT EXISTS {escape_mysql_name(self.table_name)} (\n"
        stmt += ",\n".join(lines)
        if index_lines:
            stmt += ",\n" + ",\n".join(index_lines)
        stmt += (
            "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci "
            f"COMMENT='{escape_mysql_comment(self.table_name)}';"
        )
        return stmt

    def _needs_index_prefix(self, col: str) -> bool:
        """该列建索引时是否必须带前缀长度（TEXT/BLOB 系列都要）。"""
        if TEXT_INDEX_PREFIX_LENGTH <= 0:
            return False
        fd = self._field_by_name.get(col)
        if fd is None:
            return False
        col_type = self.get_mysql_field_type(fd).upper()
        return "TEXT" in col_type or "BLOB" in col_type

    def _index_column(self, col: str) -> str:
        """索引里的一列，必要时补前缀长度。

        MySQL **不允许**对 TEXT/BLOB 列建不带前缀长度的索引，直接报
        ``Error 1170 BLOB/TEXT column used in key specification without a key length``。
        而本库（跟着 Go 版）把 string 映射成 MEDIUMTEXT，所以只要在 string 列上声明了
        index / unique_key，生成的 DDL 就是一条 MySQL 会拒绝的语句。

        Go 版有同样的问题且至今没暴露：它的测试只比对 SQL 字符串，从不真的执行——
        用 Go 仓库自带的 tools/proto2sql/testdata/account.proto 生成的建表语句
        打到 MySQL 8.4 上就是 Error 1170。这里补上前缀长度，让产出的 DDL 真的能建表。

        把 ``TEXT_INDEX_PREFIX_LENGTH`` 设成 0 可以退回与 Go 逐字节一致的输出
        （那条 DDL 建不了表，只在做跨语言字符串比对时才有意义）。
        """
        escaped = escape_mysql_name(col)
        if self._needs_index_prefix(col):
            return f"{escaped}({TEXT_INDEX_PREFIX_LENGTH})"
        return escaped

    def build_alter_clauses(self, current_cols: dict[str, ColumnMeta]) -> list[str]:
        """按 proto 定义与线上列结构比对，生成 ALTER TABLE 的子句列表。

        匹配优先级：

        1. **列名精确匹配**：类型不兼容、或该列还没有字段号注释时 ``MODIFY COLUMN``
           （顺带回填注释）；
        2. **字段号匹配**（列名不同但注释里的 proto 字段号一致，即 proto 里改了名）：
           ``CHANGE COLUMN 旧名 新名 新类型 COMMENT 'pb:N'``，**原有数据保留**；
        3. 都无匹配：``ADD COLUMN`` 新增。

        所有生成的列都带 ``COMMENT 'pb:N'``，以便后续迁移继续按字段号识别列。
        不修改传入的 current_cols（内部拷贝一份）。
        """
        remaining = dict(current_cols)
        by_field_num = {m.field_num: name for name, m in current_cols.items() if m.field_num != 0}

        alter_sqls: list[str] = []
        for fd in self._fields:
            field_name = fd.name

            if _keyword_regex.match(field_name.upper()):
                log.warning(
                    "field %s in table %s conflicts with MySQL keyword",
                    field_name, self.table_name,
                )

            target_type = self.get_mysql_field_type(fd)
            comment = column_comment(fd.number)

            # 1) 列名精确匹配
            meta = remaining.get(field_name)
            if meta is not None:
                if not is_type_match(meta.col_type, target_type) or meta.field_num != fd.number:
                    alter_sqls.append(
                        f"MODIFY COLUMN {escape_mysql_name(field_name)} {target_type}{comment}"
                    )
                del remaining[field_name]
                continue

            # 2) 字段号匹配（改名场景）
            old_name = by_field_num.get(fd.number)
            if old_name is not None and old_name in remaining:
                alter_sqls.append(
                    f"CHANGE COLUMN {escape_mysql_name(old_name)} {escape_mysql_name(field_name)} "
                    f"{target_type}{comment}"
                )
                del remaining[old_name]
                continue

            # 3) 全新字段
            alter_sqls.append(f"ADD COLUMN {escape_mysql_name(field_name)} {target_type}{comment}")

        return alter_sqls

    # ── DML 片段 ────────────────────────────────────────────────────────

    def validate_message(self, message: Message) -> None:
        """校验消息类型与本表一致。"""
        if message is None:
            raise Proto2MySQLError("message cannot be nil")
        if message.DESCRIPTOR is not self.descriptor:
            raise Proto2MySQLError(
                f"message descriptor {message.DESCRIPTOR.full_name} does not match table {self.table_name}"
            )

    def all_field_args(self, message: Message) -> list[Any]:
        """按字段**声明顺序**序列化全部字段，作为 INSERT/REPLACE 的参数。

        顺序必须与 ``_fields_list_sql`` 同源：那是 CREATE TABLE 的列顺序，也是
        SELECT 回读时 ``parse_from_row`` 按下标对位的依据。用字段号序（``ListFields()``
        的顺序）会在声明序≠编号序的消息上整行错列写进去，而且**不报错**。

        走注册期编好的闭包表，热路径上不再逐字段查类型（见 pbconv 的编码闭包一节）。
        """
        self.validate_message(message)
        return [encode(message) for encode in self._encoders]

    def get_insert_sql(self, message: Message) -> Statement:
        return Statement(self._insert_sql_template, self.all_field_args(message))

    def get_batch_insert_sql(self, messages: Sequence[Message]) -> Statement:
        if not messages:
            raise Proto2MySQLError("no messages to insert")
        if len(messages) > BATCH_INSERT_MAX_SIZE:
            raise BatchSizeExceededError(f"batch size exceeds maximum {BATCH_INSERT_MAX_SIZE}")

        all_args: list[Any] = []
        groups: list[str] = []
        placeholders = build_placeholders(len(self._fields))
        for msg in messages:
            all_args.extend(self.all_field_args(msg))
            groups.append(placeholders)

        sql = (
            f"INSERT INTO {escape_mysql_name(self.table_name)} ({self._fields_list_sql}) "
            f"VALUES ({'), ('.join(groups)})"
        )
        return Statement(sql, all_args)

    def get_batch_replace_sql(self, messages: Sequence[Message]) -> Statement:
        stmt = self.get_batch_insert_sql(messages)
        return Statement("REPLACE" + stmt.sql[len("INSERT") :], stmt.args)

    def get_replace_sql(self, message: Message) -> Statement:
        args = self.all_field_args(message)
        return Statement(f"{self._replace_sql_prefix}{build_placeholders(len(args))})", args)

    def get_insert_on_dup_update_sql(self, message: Message) -> Statement:
        """INSERT ... ON DUPLICATE KEY UPDATE，冲突时用**已赋值**的字段覆盖。"""
        stmt = self.get_insert_sql(message)

        clauses: list[str] = []
        update_args: list[Any] = []
        for fd in self._fields:
            if not pbconv.has_field(message, fd):
                continue
            clauses.append(f"{escape_mysql_name(fd.name)} = ?")
            update_args.append(pbconv.serialize_field_value(message, fd))

        if not clauses:
            return stmt
        return Statement(
            f"{stmt.sql} ON DUPLICATE KEY UPDATE {', '.join(clauses)}",
            stmt.args + update_args,
        )

    def get_insert_on_dup_key_for_primary_key_sql(self, message: Message) -> Statement:
        """INSERT ... ON DUPLICATE KEY UPDATE pk = pk：只拿行锁，不改数据。"""
        if self._primary_key_field is None:
            raise PrimaryKeyNotFoundError(f"primary key not found: table {self.table_name}")

        stmt = self.get_insert_sql(message)
        pk_name = self._primary_key_field.name
        pk_value = pbconv.serialize_field_value(message, self._primary_key_field)
        return Statement(
            f"{stmt.sql} ON DUPLICATE KEY UPDATE {escape_mysql_name(pk_name)} = ?",
            stmt.args + [pk_value],
        )

    def get_select_sql(self, include_semicolon: bool = True) -> str:
        return self._select_fields_sql + (";" if include_semicolon else " ")

    def get_select_sql_by_kv(self, where_key: str, where_val: Any) -> Statement:
        self.field(where_key)  # 未知列直接抛错，不拼进 SQL
        return Statement(
            f"{self._select_fields_sql} WHERE {escape_mysql_name(where_key)} = ?;", [where_val]
        )

    def get_select_sql_by_where(self, where_clause: str, where_args: Sequence[Any] | None = None) -> Statement:
        return Statement(
            f"{self._select_fields_sql} WHERE {where_clause};", list(where_args or [])
        )

    def get_delete_sql(self, message: Message) -> Statement:
        where_clause, where_args = self.primary_key_where(message)
        return Statement(
            f"DELETE FROM {escape_mysql_name(self.table_name)} WHERE {where_clause}", where_args
        )

    def get_delete_sql_by_where(self, where_clause: str, where_args: Sequence[Any] | None = None) -> Statement:
        return Statement(
            f"DELETE FROM {escape_mysql_name(self.table_name)} WHERE {where_clause}",
            list(where_args or []),
        )

    def get_update_set(self, message: Message) -> tuple[str, list[Any]]:
        """生成 SET 子句和参数，**只包含已赋值的字段**。

        proto3 的零值就是"未赋值"：标量为 0/""/false 的字段会被跳过。
        要显式写零值，把字段声明为 optional，或改用 update_fields_by_pk。
        """
        self.validate_message(message)
        clauses: list[str] = []
        args: list[Any] = []
        for fd in self._fields:
            if not pbconv.has_field(message, fd):
                continue
            clauses.append(f"{escape_mysql_name(fd.name)} = ?")
            args.append(pbconv.serialize_field_value(message, fd))
        return ", ".join(clauses), args

    def get_update_sql(self, message: Message) -> Statement:
        set_clause, set_args = self.get_update_set(message)
        if not set_clause:
            raise Proto2MySQLError("no fields to update")
        where_clause, where_args = self.primary_key_where(message)
        return Statement(
            f"UPDATE {escape_mysql_name(self.table_name)} SET {set_clause} WHERE {where_clause}",
            set_args + where_args,
        )

    def get_update_sql_by_where(
        self, message: Message, where_clause: str, where_args: Sequence[Any] | None = None
    ) -> Statement:
        set_clause, set_args = self.get_update_set(message)
        if not set_clause:
            raise Proto2MySQLError("no fields to update")
        return Statement(
            f"UPDATE {escape_mysql_name(self.table_name)} SET {set_clause} WHERE {where_clause}",
            set_args + list(where_args or []),
        )

    # ── 主键 ────────────────────────────────────────────────────────────

    def primary_key_values(self, message: Message) -> list[Any]:
        """按主键声明顺序取主键值（已序列化成 SQL 参数形态）。"""
        self.validate_message(message)
        if not self.primary_key:
            raise PrimaryKeyNotFoundError(f"primary key not found: table {self.table_name}")

        values: list[Any] = []
        for pk in self.primary_key:
            fd = self._field_by_name.get(pk)
            if fd is None:
                raise FieldNotFoundError(
                    f"field not found in message: primary key {pk} in table {self.table_name}"
                )
            values.append(pbconv.serialize_field_value(message, fd))
        return values

    def primary_key_where(self, message: Message) -> tuple[str, list[Any]]:
        """生成 "`a` = ? AND `b` = ?" 形式的主键 WHERE 片段及其参数。"""
        args = self.primary_key_values(message)
        clause = " AND ".join(f"{escape_mysql_name(pk)} = ?" for pk in self.primary_key)
        return clause, args

    # ── 列缓存（供 DB 层复用）────────────────────────────────────────────

    @property
    def cached_columns(self) -> dict[str, str] | None:
        return self._cached_columns

    @cached_columns.setter
    def cached_columns(self, cols: dict[str, str] | None) -> None:
        self._cached_columns = cols

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<MessageTable {self.descriptor.full_name} -> {self.table_name}>"
