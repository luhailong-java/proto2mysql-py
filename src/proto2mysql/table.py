"""MessageTable：protobuf 消息 <-> MySQL 表的映射、建表 DDL、迁移子句与 DML 片段。

对应 Go 版 proto2mysql.go 里挂在 *MessageTable 上的那一半（另一半 *DB 在 db.py）。

生成的 SQL 与 Go 版**逐字节一致**——这是刻意的硬约束：Go 版测试里断言的 SQL 字符串
被原样搬成了本仓库的黄金用例（tests/test_golden_sql.py），两边跑同一份 .proto
必须产出同一份 DDL/DML，否则"并存迁移"就没有可验证的基准。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field as _dc_field
from typing import Any, Iterable, Sequence

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.message import Message

from . import pbconv
from .errors import (
    BatchSizeExceededError,
    InvalidFieldKindError,
    ExpandOnlyViolationError,
    FieldNotFoundError,
    FieldNumberReusedError,
    PrimaryKeyNotFoundError,
    Proto2MySQLError,
)
from .options import (
    TableOption,
    apply_options,
    table_name_from_descriptor,
    table_options_from_descriptor,
)

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
# 没列出的类型（sint32/sint64/fixed32/fixed64/sfixed32/sfixed64）**不支持**，
# get_mysql_field_type 会在建表时就 fail-fast 抛 InvalidFieldKindError。
#
# 早先它们静默回落成 TEXT：建表一路成功，跑到第一次写入才抛异常，而那时列已经
# 建出来了、可能还上了线。这类 zigzag / 定长编码没有直接对应的 MySQL 列类型，
# 改用 int32 / int64 / uint32 / uint64 即可（取值范围一样，只是线上编码不同）。
# 数值列判定。注意这里**包含** sint/fixed/sfixed：Go 版 isNumericKind 就是这么写的。
# 它们建表会落到 TEXT、写入会被 pbconv 拒——但那是另一条链路上的既有限制，
# 这里保持与 Go 版同样的判定，别顺手"修"成不一致。
#
# 放在 table.py 而不是 sqlbuilder.py：算术赋值有两个入口（SQLBuilder 的
# add_col/sub_col/…，以及 DB.incr_by_pk/decr_by_pk_if_enough 直接拼的裸 SQL），
# 判定只能有一份，否则总有一条路漏校验。
NUMERIC_FIELD_TYPES = frozenset(
    {
        _T.TYPE_INT32,
        _T.TYPE_SINT32,
        _T.TYPE_SFIXED32,
        _T.TYPE_INT64,
        _T.TYPE_SINT64,
        _T.TYPE_SFIXED64,
        _T.TYPE_UINT32,
        _T.TYPE_FIXED32,
        _T.TYPE_UINT64,
        _T.TYPE_FIXED64,
        _T.TYPE_FLOAT,
        _T.TYPE_DOUBLE,
    }
)


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

# MySQL 对 index / constraint 等标识符的上限是 64 字符。表名本身可以合法地占满
# 64 字符，直接再拼 ``idx_`` / ``uk_`` 就会让整条 CREATE/ALTER 以 1059 失败。
MYSQL_IDENTIFIER_MAX_LENGTH = 64


def _bounded_mysql_identifier(name: str) -> str:
    """保留短名称；长名称用稳定摘要收尾，限制在 MySQL 的 64 字符内。"""
    if len(name) <= MYSQL_IDENTIFIER_MAX_LENGTH:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    prefix_len = MYSQL_IDENTIFIER_MAX_LENGTH - len(digest) - 1
    return f"{name[:prefix_len]}_{digest}"


def index_name_for(table_name: str, ordinal: int) -> str:
    """返回 CREATE 与 schema reconciliation 共用的普通索引名。"""
    return _bounded_mysql_identifier(f"idx_{table_name}_{ordinal}")


def unique_key_name_for(table_name: str) -> str:
    """返回 CREATE 与 schema reconciliation 共用的唯一索引名。"""
    return _bounded_mysql_identifier(f"uk_{table_name}")


def escape_mysql_name(name: str) -> str:
    """整体转义 MySQL 标识符，兼容包含点号的 protobuf full name 表名。"""
    return "`" + name.replace("`", "``") + "`"


#: DDL 的 ``COMMENT='...'`` 里，单引号怎么转义。
#:
#: ``"standard"``（默认）：单引号翻倍 ``''``。这是 SQL 标准写法，
#:   在**任何** sql_mode 下都是合法且闭合的字面量。
#: ``"go"``：与 Go 版 escapeMySQLComment 逐字节一致（单引号前加反斜杠）。
#:   它有一个真实缺陷：表名含单引号时，在 ``NO_BACKSLASH_ESCAPES`` 下
#:   那个反斜杠不再是转义符，字面量提前闭合 —— 直接语法错误。
#:
#: 两种模式下反斜杠都翻倍、CR/LF 都变空格（这两条与 Go 一致），
#: 所以差别**只在**表名含单引号时才出现，而语料里的表名一个都不含。
COMMENT_ESCAPE_MODE = "standard"


def escape_mysql_comment(comment: str) -> str:
    """转义 MySQL 注释字面量里的特殊字符。

    为什么单引号不跟 Go 用反斜杠：反斜杠转义**只在** sql_mode 不含
    ``NO_BACKSLASH_ESCAPES`` 时成立。单引号翻倍是 SQL 标准，两种模式下都成立。

    反斜杠仍然翻倍（与 Go 一致）。残留的一点不对称必须知道，否则下一个人会当 bug 提：
    MySQL 的字符串字面量**写不出**一个"两种 sql_mode 下语义完全相同"且含反斜杠的形式。
    这里的取舍是——默认模式下注释文本精确还原，``NO_BACKSLASH_ESCAPES`` 下
    注释里会多显示一个反斜杠，纯观感差异，语法在两边都合法。
    """
    text = comment.replace("\n", " ").replace("\r", " ")
    text = text.replace("\\", "\\\\")
    if COMMENT_ESCAPE_MODE == "go":
        return text.replace("'", "\\'")
    return text.replace("'", "''")


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
    "varchar": "varchar",
    "char": "char",
}


# 同族容量阶梯。判定口径统一为「线上装得下目标就不动它」：
#   线上容量 >= 目标容量  → 兼容，不生成 ALTER
#   线上容量 <  目标容量  → 必须 ALTER 拓宽
#
# 这条口径原先只有 datetime 分支做对了，另外两族是坏的：
#
#   * varchar/char 与 float/double 的方向**是反的**（写的是 target >= current）。
#     后果双向都错：线上 varchar(50)、proto 要 varchar(100) 时判「兼容」不拓宽，
#     写 100 字符报 1406；线上 varchar(100)、proto 要 varchar(50) 时反而去 ALTER 收窄。
#   * 整数族**根本没有方向判断**。int 与 bigint 是不同的 base_type，在下面
#     `current_base != target_base` 处就直接判不兼容了，于是**两个方向都 ALTER**。
#
# 为什么这在滚动发布下是 P0：本库没有 schema 版本概念，每个进程都把自己的 proto
# 当成唯一正确的目标结构。v2 把 uint32 拓宽成 uint64 之后，任何一个还在跑 v1 的
# 副本**一重启**就把列 MODIFY 回 int unsigned —— 不需要写任何数据，schema 就在
# 新旧副本之间来回翻面。文本族同理：2026-08-19 实测撞到过线上 mediumtext 被
# varchar(255) 的一侧重建，同一条写入在宽列副本成功、窄列副本报 1406，且不可复现。
#
# 收窄是有损操作，与本库「永不 DROP COLUMN」的既有立场一致：确实要收窄请手写 ALTER。
_INT_RANK = {"tinyint": 1, "smallint": 2, "mediumint": 3, "int": 4, "bigint": 5}
_TEXT_RANK = {"char": 1, "varchar": 2, "tinytext": 3, "text": 4, "mediumtext": 5, "longtext": 6}
_BLOB_RANK = {"binary": 1, "varbinary": 2, "tinyblob": 3, "blob": 4, "mediumblob": 5, "longblob": 6}
_FLOAT_RANK = {"float": 1, "double": 2}
_FAMILIES = (_INT_RANK, _TEXT_RANK, _BLOB_RANK, _FLOAT_RANK)


def _base_of(info: "MySQLTypeInfo") -> str:
    return _TYPE_ALIASES.get(info.base_type) or info.base_type


def is_rename_convertible(current_type: str, target_type: str) -> bool:
    """线上旧列的类型，能不能安全承接改名后的新类型。

    改名保留数据靠的是 ``CHANGE COLUMN``，而 MySQL 会对它做**隐式类型转换**。
    同基础类型、或同族（int↔bigint、varchar↔mediumtext）都算能接；
    跨族（mediumtext↔bigint）一律不能——那多半根本不是改名，而是
    proto 里把一个已删字段的编号让给了类型完全不同的新字段。
    """
    current = parse_mysql_type(current_type)
    target = parse_mysql_type(target_type)
    current_base = _base_of(current)
    target_base = _base_of(target)
    # signed 与 unsigned 的值域互不包含：signed 的负数装不进 unsigned，unsigned 的
    # 高半区也装不进 signed。即使字段号相同，也不能把这种变化当成安全改名。
    if current_base in _INT_RANK and target_base in _INT_RANK:
        if current.unsigned != target.unsigned:
            return False
    if current_base == target_base:
        return True
    return any(current_base in family and target_base in family for family in _FAMILIES)


def _reject_unsafe_temporal_type_change(
    table_name: str,
    current_col: str,
    target_col: str,
    current_type: str,
    target_type: str,
) -> None:
    """TIMESTAMP 与 DATETIME 的转换依赖会话时区，schema sync 不自动猜。"""
    current_base = parse_mysql_type(current_type).base_type
    target_base = parse_mysql_type(target_type).base_type
    if {current_base, target_base} != {"timestamp", "datetime"}:
        return
    raise Proto2MySQLError(
        f"表 {table_name} 的列 {current_col}（{current_type}）与目标列 {target_col}"
        f"（{target_type}）在 TIMESTAMP / DATETIME 语义上不一致，拒绝自动迁移。\n"
        "  TIMESTAMP 会按 @@session.time_zone 转换、受 2038 上限约束；DATETIME 不会。\n"
        "  请先核对历史数据的时区语义和 @@session.time_zone，再人工回填/ALTER，"
        "确认线上列已是目标类型后重试。"
    )


def attribute_drift(
    meta: "ColumnMeta", target_type: str, *, is_primary_key: bool = False
) -> tuple[bool, str]:
    """线上列的 NULL / AUTO_INCREMENT 属性与目标是否漂了，且这次漂移能不能**自动改**。

    ``COLUMN_TYPE``（也就是 ColumnMeta.col_type）里**不含**这几样：
    ``int unsigned`` 这个字符串既看不出可空不可空，也看不出有没有 AUTO_INCREMENT。
    早先整个对齐只比 col_type，于是下面两种漂移完全看不见。

    只自动改**放宽或补齐**的那两种；收紧的那种只报不改（见 report_unsafe_attribute_drift），
    与「永不 DROP COLUMN」是同一个立场。
    """
    if meta.nullable is None:
        return False, ""  # 没有扩展元信息，什么都不比

    upper = target_type.upper()
    want_nullable = "NOT NULL" not in upper
    want_auto = "AUTO_INCREMENT" in upper

    if want_auto and meta.auto_increment is False:
        # 自增列丢了 AUTO_INCREMENT：不带主键值的 insert 会全部写 0，第二条就撞 1062。
        return True, "缺 AUTO_INCREMENT"
    if want_nullable and is_primary_key:
        # **主键列在 MySQL 里恒为 NOT NULL**（建表时被强制加上，改不掉）。
        # 拿"proto 要可空"去要求它，会产出一条永远不生效的 MODIFY：
        # 发下去、MySQL 照旧保持 NOT NULL、下次启动再发一遍……无休止。
        # 开了 expand_only 更糟——每次启动都抛 ExpandOnlyViolationError，服务永远起不来。
        # 实测：主键含 string 列（映射成不带 NOT NULL 的 MEDIUMTEXT）就会撞上。
        return False, ""
    if want_nullable and meta.nullable is False:
        # 放宽值域，无损，可以自动改。Timestamp 列必然落在这里
        # （get_mysql_field_type 恒返回不带 NOT NULL 的 DATETIME(6)）：
        # 线上一旦被改成 NOT NULL，凡是没赋值该字段的行就全部插不进去。
        return True, "线上 NOT NULL 而 proto 要可空"
    return False, ""


def report_unsafe_attribute_drift(
    table_name: str, col: str, meta: "ColumnMeta", target_type: str,
    *, is_primary_key: bool = False,
) -> None:
    """报告**不自动改**的那几类属性漂移。只报不改，但绝不静默。"""
    if meta.nullable is None:
        return

    upper = target_type.upper()
    if "NOT NULL" in upper and meta.nullable is True and not is_primary_key:
        log.warning(
            "table %s: 列 %s 线上可空、proto 要 NOT NULL——**本次不动它**。"
            "线上很可能已经有 NULL 行：改成 NOT NULL 在严格模式下整条 ALTER 失败，"
            "非严格模式下会把这些 NULL 静默改成 0。确需收紧请人工回填后再写 ALTER。",
            table_name, col,
        )

    want_default = _default_literal(target_type)
    if meta.default != want_default:
        log.warning(
            "table %s: 列 %s 的默认值漂了（线上 %r，proto 要 %r）——**本次不动它**。"
            "默认值只影响不点名该列的 INSERT，改它会重写表定义，收益远小于风险。",
            table_name, col, meta.default, want_default,
        )

def _default_literal(target_type: str) -> str | None:
    """从目标列定义里取出 ``DEFAULT`` 字面量；没有 DEFAULT 子句返回 None。"""
    match = re.search(r"\bDEFAULT\s+(\S+)", target_type, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).strip("'\"")


def aligned_column_type(current_type: str, target_type: str) -> str:
    """本次 ALTER 里该写哪个列类型：默认就是 proto 的目标类型，
    但目标比线上**窄**时，保留线上的**类型本体**（"永不收窄"），只带上目标的属性。

    属性必须来自目标：NOT NULL / DEFAULT / AUTO_INCREMENT 这些是 proto 侧的决定，
    而 information_schema 的 COLUMN_TYPE 里根本没有它们。所以做的是
    "换类型本体、留属性"，而不是整段替换成线上那串。

    没有这一层的话，凡是**因为别的原因**要发 MODIFY / CHANGE 的场合都会顺手收窄：

    * 回填 ``pb:N`` 注释（类型本来是兼容的，只是注释缺了）；
    * 属性漂移（线上丢了 AUTO_INCREMENT）；
    * 按字段号改名（必须发 CHANGE COLUMN，MySQL 会真的搬数据）。

    三条路都会把 ``bigint`` 的列写成 ``int``：严格模式整条 ALTER 失败，
    非严格模式把 5000000000 静默截成 4294967295。
    """
    if not narrowing_suppressed(current_type, target_type):
        return target_type
    return _replace_column_base_type(target_type, current_type)


def _replace_column_base_type(target_type: str, base_spec: str) -> str:
    """把列定义 target_type 的**类型本体**换成 base_spec，其余属性原样保留。

        _replace_column_base_type("int NOT NULL DEFAULT 0", "bigint")
            -> "bigint NOT NULL DEFAULT 0"
        _replace_column_base_type("int unsigned NOT NULL", "bigint unsigned")
            -> "bigint unsigned NOT NULL"

    base_spec 取自 information_schema 的 COLUMN_TYPE，它自己就带着 unsigned / 长度，
    所以目标里紧跟类型本体的那个 unsigned 要一起丢掉，
    否则会拼出 ``bigint unsigned unsigned``。
    """
    parts = target_type.split()
    if not parts:
        return base_spec
    rest = parts[1:]
    if rest and rest[0].lower() == "unsigned":
        rest = rest[1:]
    if not rest:
        return base_spec
    return base_spec + " " + " ".join(rest)


def narrowing_suppressed(current_type: str, target_type: str) -> bool:
    """本次"判为兼容"是不是**因为挡下了一次收窄**（而不是两边本来就一样）。

    专门用来打日志。收窄抑制是本库唯一一个「什么都不做、也什么都不说」的分支：
    改名有 WARNING、expand_only 违规有带语句清单的报错，唯独这里一声不吭——
    于是有人在 proto 里把 bigint 改回 int、期待列跟着变窄，结果什么也没发生，
    也没有任何线索告诉他为什么。

    判据：类型确实不同，且线上那一侧更宽。
    """
    current = parse_mysql_type(current_type)
    target = parse_mysql_type(target_type)
    current_base = _base_of(current)
    target_base = _base_of(target)

    if current_base != target_base:
        for family in _FAMILIES:
            c, t = family.get(current_base), family.get(target_base)
            if c is None or t is None:
                continue
            if family is _INT_RANK and current.unsigned != target.unsigned:
                return False  # 值域方向不同，不是宽窄问题
            return c > t
        return False

    if current_base in ("varchar", "char") or current_base == "datetime":
        return current.length > target.length
    if current_base in _FLOAT_RANK:
        return current.decimal > target.decimal
    return False


def is_type_match(current_type: str, target_type: str) -> bool:
    """判断线上列类型与目标类型是否兼容（不兼容才需要 MODIFY COLUMN）。

    口径是**线上装得下目标就算兼容**：线上更宽时一律不动它。理由见 _INT_RANK
    上方的注释——收窄不但丢数据，在滚动发布期间还会被新旧副本来回改。
    """
    current = parse_mysql_type(current_type)
    target = parse_mysql_type(target_type)

    current_base = _base_of(current)
    target_base = _base_of(target)

    if current_base != target_base:
        # 同族跨类型（int↔bigint、varchar↔mediumtext、float↔double）按容量阶梯判方向。
        # 跨族（比如 int↔mediumtext）没有可比性，一律判不兼容。
        for family in _FAMILIES:
            current_rank = family.get(current_base)
            target_rank = family.get(target_base)
            if current_rank is None or target_rank is None:
                continue
            if family is _INT_RANK and current.unsigned != target.unsigned:
                # 有无符号决定的是值域方向而不是宽窄，两边都可能装不下对方，必须 ALTER。
                return False
            return current_rank >= target_rank
        return False

    if current_base in ("varchar", "char"):
        return current.length >= target.length
    if current_base in _INT_RANK:
        # 位宽相同，只剩有无符号要比。
        return current.unsigned == target.unsigned
    if current_base in _FLOAT_RANK:
        return current.decimal >= target.decimal
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
    #: 下面三项都是**三态**：``None`` = 调用方没提供这项元信息，一律**不参与比较**。
    #:
    #: 之所以必须是三态而不是"猜一个默认值"：跨语言对拍语料与大量既有调用方
    #: 都是两参构造 ``ColumnMeta("int unsigned", 1)``。默认成 ``nullable=True`` 之类，
    #: 会让每一列都被判成属性漂移，凭空长出一堆 MODIFY 子句。
    #:
    #: 只有真的从 information_schema 读到了（见 DB.get_table_column_meta），
    #: 才敢拿它去决定要不要发 ALTER。
    nullable: bool | None = None
    auto_increment: bool | None = None
    default: str | None = None


def _qmark_to_format(sql: str) -> str:
    """``?`` → ``%s``，但**只在「代码区」替换**；``%`` → ``%%`` 保持全文替换。

    为什么要分区：WHERE 子句是调用方给的自由文本，里面完全可能出现
    ``nick = 'who?'``、JSON path ``'$.a?b'``、或者 ``-- 这里有个问号?``。
    朴素逐字符替换会把它们一并变成 ``%s``，于是占位符个数比参数多，
    驱动做 ``query % args`` 时抛 TypeError——一条**合法**查询被拒，
    报错还指向驱动内部，谁也看不出是本库替换出来的。

    ``%`` 则必须继续全文转义（含引号内）：PyMySQL 的反转义也是全文范围的，
    只跳 ``?`` 不跳 ``%`` 才对得上。

    ``/*! ... */`` 刻意**不**当注释：那是 MySQL 的版本条件执行块，里面的内容
    真的会被执行，跳过它就可能漏掉一个真占位符。普通 ``/* */`` 才跳。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]

        if ch == "%":
            out.append("%%")
            i += 1
            continue
        if ch == "?":
            out.append("%s")
            i += 1
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            mark, opened_at = len(out), i
            closed = False
            out.append(quote)
            i += 1
            while i < n:
                c = sql[i]
                if c == "%":
                    out.append("%%")
                    i += 1
                    continue
                # 反斜杠转义只在字符串字面量里成立，反引号标识符里没有这一说
                if c == "\\" and quote != "`" and i + 1 < n:
                    nxt = sql[i + 1]
                    out.append(c)
                    out.append("%%" if nxt == "%" else nxt)
                    i += 2
                    continue
                if c == quote:
                    if i + 1 < n and sql[i + 1] == quote:  # '' "" `` 是转义写法
                        out.append(quote * 2)
                        i += 2
                        continue
                    out.append(quote)
                    i += 1
                    closed = True
                    break
                out.append(c)
                i += 1
            if not closed:
                # 引号没闭合 = SQL 本来就是坏的。此时把整个尾巴当字面量的话，
                # 后面**真正的**占位符也会被吞掉，占位符个数比参数少，
                # 报错变成驱动内部的 TypeError，比 MySQL 的语法错误难查得多。
                # 所以退回逐字符处理：把这个引号当普通字符，让主循环继续。
                del out[mark:]
                out.append(quote)
                i = opened_at + 1
            continue

        # -- 行注释（MySQL 要求 -- 后面跟空白）、# 行注释、/* */ 块注释
        if sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\r\n"):
            end = sql.find("\n", i)
            end = n if end < 0 else end
        elif ch == "#":
            end = sql.find("\n", i)
            end = n if end < 0 else end
        elif sql.startswith("/*", i) and not sql.startswith("/*!", i):
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
            continue

        out.append(sql[i:end].replace("%", "%%"))
        i = end

    return "".join(out)


@dataclass
class Statement:
    """带占位符的 SQL 与其参数，对应 Go 的 SqlWithArgs。

    ``sql`` 用 ``?`` 占位（与 Go 的 database/sql 一致，也让黄金用例可以跨语言比对）。
    Python 的 DB-API 驱动大多是 ``pyformat``/``format`` 风格（``%s``），
    执行前用 :meth:`for_paramstyle` 转换，别自己手写替换——``%`` 字面量必须同时转义，
    漏了会让 ``LIKE '%x%'`` 这种 where 子句在 PyMySQL 里抛
    ``ValueError: unsupported format character``；而字面量与注释里的 ``?``
    又**不能**被替换，否则占位符个数会比参数多（见 :func:`_qmark_to_format`）。
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
        return _qmark_to_format(self.sql), tuple(self.args)


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
        "field_numbers",
        "has_explicit_table_name",
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
        #: 是否声明了 table_name 选项。没声明时表名退化成 proto full name（含 package），
        #: 真正拿它去动数据库时会告警——见 DB._sync_table_schema。
        self.has_explicit_table_name = table_name_from_descriptor(descriptor)[1] or (
            self.table_name != descriptor.full_name
        )
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
        self._reject_oneof()

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
        self.field_numbers = frozenset(fd.number for fd in self._fields)

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

    def _reject_oneof(self) -> None:
        """含真 oneof 的消息一律拒绝建表——本库表达不了它。

        每个成员各占一列，**行里没有判别位**。回读时逐列 set，最后一个非默认值的列
        胜出，于是写进去的激活成员会被静默换成别的成员（真库实测：写 ``b='hello'``
        读回 ``WhichOneof()=='c'``）。更糟的是激活成员的值恰好等于默认值时，
        整行所有成员列都是默认值——任何算法都还原不出"哪个成员是激活的"。

        写侧同样修不好：``update`` 只写"已赋值"的字段，切换激活成员时旧成员那一列
        仍留着旧值，回读照样取到旧的那个。

        所以这里 fail-closed，与拒绝 sint32 / fixed32 是同一个立场：
        **不存在"既有正确用法"**——每一次回读都在损坏数据，宁可在注册时就拦下。
        proto3 的 ``optional`` 走的是合成 oneof，不受影响（见 pbconv.real_oneof）。
        """
        for fd in self._fields:
            oneof = pbconv.real_oneof(fd)
            if oneof is None:
                continue
            raise InvalidFieldKindError(
                f"表 {self.table_name} 的字段 {fd.name} 属于 oneof {oneof.name}——本库不支持 oneof。\n"
                f"  每个成员各占一列，而行里没有判别位：回读时逐列 set、最后一个非默认列胜出，\n"
                f"  写进去的激活成员会被静默换成别的成员（真库实测：写 b='hello' 读回 c）。\n"
                f"  改法：把 oneof 拆成普通字段 + 一个显式的 kind 枚举列，由业务自己判别。\n"
                f"  注意 proto3 的 optional 不受影响——它是合成 oneof，只有一个成员。"
            )

    def numeric_field(self, name: str) -> FieldDescriptor:
        """按列名取**数值**字段描述符；非数值列直接拒。

        算术赋值（``col = col ± ?``）和拿 0 当哨兵的比较（``IF(col = 0, ...)``）
        一旦落到文本列上，MySQL 会做隐式转换：非严格 sql_mode 下
        ``'abc' + 1`` 算成 1、``'abc' = 0`` 为真，于是一次"加 1 金币"
        把整列原值抹成 1，**不报错、不告警**。严格模式下才会拒。

        所以这条必须在**构造期**挡掉——列类型由 proto 字段类型决定，
        构造期信息就已经齐了，没有理由拖到 MySQL 那边碰运气。
        """
        fd = self.field(name)
        # repeated / map 必须单独挡：它们的 fd.type 仍是**元素**类型
        # （repeated int32 就是 TYPE_INT32），只看 type 会放行，
        # 而这两类落的是 MEDIUMBLOB。
        if pbconv.is_repeated_field(fd) or fd.type not in NUMERIC_FIELD_TYPES:
            raise Proto2MySQLError(
                f"column {name} in table {self.table_name} is not numeric"
            )
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
        # is_repeated 必须判在 Timestamp **之前**。
        #
        # 反过来的话，`repeated google.protobuf.Timestamp` 会被建成 DATETIME(6)，
        # 而写入侧 pbconv.is_timestamp_field 里同样排除了 repeated，
        # 同一个字段在那边算容器、写下去的是 proto wire 裸字节——
        # STRICT 模式下每一次写入都被 Error 1292 拒，非 STRICT 下存进去的是垃圾。
        # 走 MEDIUMBLOB 之后，_serialize_container / _parse_container 本来就对称可逆。
        #
        # 注意 `map<string, Timestamp>` 一直是对的：MapEntry 的 full_name 不是
        # google.protobuf.Timestamp，压根进不了下面那个分支。
        if pbconv.is_repeated_field(fd):  # map / list 统一用 MEDIUMBLOB
            return "MEDIUMBLOB"

        if fd.message_type is not None and fd.message_type.full_name == pbconv.TIMESTAMP_FULL_NAME:
            return "DATETIME(6)"

        base_type = MYSQL_FIELD_TYPES.get(fd.type)
        if base_type is None:
            # 早先这里静默回落到 TEXT：建表一路成功，跑到**第一次写入**才抛
            # InvalidFieldKindError，而那时列已经建出来了、可能还上了线。
            # 现在在建表/生成 DDL 时就 fail-fast，把问题挡在写库之前。
            raise InvalidFieldKindError(
                f"表 {self.table_name} 的字段 {fd.name}（proto type {fd.type}）没有 MySQL 类型映射。\n"
                f"  sint32 / sint64 / fixed32 / fixed64 / sfixed32 / sfixed64 都不支持——"
                f"它们的 zigzag / 定长编码没有直接对应的 MySQL 列类型。\n"
                f"  改用 int32 / int64 / uint32 / uint64 即可（取值范围完全一样，只是线上编码不同）。"
            )

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
            index_name = index_name_for(self.table_name, idx)
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
            index_lines.append(
                f"  UNIQUE KEY {escape_mysql_name(unique_key_name_for(self.table_name))} ({cols})"
            )

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

    def build_alter_clauses(
        self, current_cols: dict[str, ColumnMeta], *, expand_only: bool = False
    ) -> list[str]:
        """按 proto 定义与线上列结构比对，生成 ALTER TABLE 的子句列表。

        匹配优先级：

        1. **列名精确匹配**：类型不兼容、或该列还没有字段号注释时 ``MODIFY COLUMN``
           （顺带回填注释）；
        2. **字段号匹配**（列名不同但注释里的 proto 字段号一致，即 proto 里改了名）：
           ``CHANGE COLUMN 旧名 新名 新类型 COMMENT 'pb:N'``，**原有数据保留**；
        3. 都无匹配：``ADD COLUMN`` 新增。

        所有生成的列都带 ``COMMENT 'pb:N'``，以便后续迁移继续按字段号识别列。
        不修改传入的 current_cols（内部拷贝一份）。

        :param expand_only: 只允许「纯新增」。产生任何 MODIFY / CHANGE 就抛
            :class:`ExpandOnlyViolationError`，把语句打印出来交人工审核。
            **滚动 / 金丝雀发布必须打开**：本库没有 schema 版本概念，每个进程都把
            自己的 proto 当成唯一正确的目标结构，新旧两版同时在跑时 MODIFY / CHANGE
            会被两边来回改——不写任何数据，一次重启就翻一次面。ADD COLUMN 没有这个
            问题（旧版本的 SQL 里根本不会出现新列名），所以放行。

        无论 expand_only 开没开，**字段号被复用**（旧列与新字段类型跨族不可转换）
        一律抛 :class:`FieldNumberReusedError`——那种情况没有任何正当用途。
        """
        remaining = dict(current_cols)
        by_field_num = {m.field_num: name for name, m in current_cols.items() if m.field_num != 0}
        pk_columns = set(self.primary_key)

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
                _reject_unsafe_temporal_type_change(
                    self.table_name,
                    field_name,
                    field_name,
                    meta.col_type,
                    target_type,
                )
                is_pk = field_name in pk_columns
                drifted, why = attribute_drift(meta, target_type, is_primary_key=is_pk)
                if drifted:
                    log.info(
                        "table %s: 列 %s 属性漂移（%s），生成 MODIFY 对齐",
                        self.table_name, field_name, why,
                    )
                report_unsafe_attribute_drift(
                    self.table_name, field_name, meta, target_type, is_primary_key=is_pk
                )
                if (
                    not is_type_match(meta.col_type, target_type)
                    or meta.field_num != fd.number
                    or drifted
                ):
                    # 类型走 aligned_column_type：这条 MODIFY 可能只是为了回填注释
                    # 或补属性，不能顺手把一个更宽的线上列收窄掉。
                    aligned = aligned_column_type(meta.col_type, target_type)
                    alter_sqls.append(
                        f"MODIFY COLUMN {escape_mysql_name(field_name)} {aligned}{comment}"
                    )
                elif narrowing_suppressed(meta.col_type, target_type):
                    # 什么都不做，但必须留痕：否则有人把 bigint 改回 int、期待列变窄，
                    # 结果什么也没发生，也没有任何线索告诉他为什么。
                    log.info(
                        "table %s: 列 %s 线上是 %s、proto 要 %s——**保持线上的不动**。"
                        "收窄会丢数据，且在滚动发布期间会被新旧副本来回改。"
                        "确实要收窄请人工写 ALTER。",
                        self.table_name, field_name, meta.col_type, target_type,
                    )
                del remaining[field_name]
                continue

            # 2) 字段号匹配（改名场景）
            old_name = by_field_num.get(fd.number)
            if old_name is not None and old_name in remaining:
                old_type = remaining[old_name].col_type
                _reject_unsafe_temporal_type_change(
                    self.table_name,
                    old_name,
                    field_name,
                    old_type,
                    target_type,
                )
                if not is_rename_convertible(old_type, target_type):
                    # 类型跨族对不上，这不是改名，是字段号被复用了。
                    # 照常生成 CHANGE 的话，MySQL 的隐式转换会把旧列内容整列吃掉，
                    # 而本库「永不 DROP COLUMN」的保护在这里完全帮不上忙。
                    raise FieldNumberReusedError(
                        f"表 {self.table_name} 的列 {old_name}（{old_type}，pb:{fd.number}）"
                        f"与新字段 {field_name}（{target_type}，pb:{fd.number}）类型不兼容"
                        f"（跨类型族或 signed/unsigned 符号变化），"
                        f"无法当作改名处理。\n"
                        f"  字段号是 protobuf 的身份，**永不复用**：删字段请用 reserved，"
                        f"新字段另取一个没用过的编号。\n"
                        f"  若确实要把这一列的数据转成新类型，请人工写 ALTER 并自行确认转换语义。"
                    )
                # 改名能保留数据，但库无法判断谁新谁旧：滚动发布时新旧两版会把这一列
                # 来回改名（v2 改成新名 → 任何一个 v1 副本重启又改回旧名 → v2 立刻
                # Error 1054）。所以这里必须留痕。
                log.warning(
                    "table %s: column %s -> %s by field number pb:%d. "
                    "滚动发布期间新旧副本会来回改名，正确做法是 expand→migrate→contract"
                    "（先加新列、双写回填、下个版本再删旧列）；或对同步调用打开 expand_only。",
                    self.table_name, old_name, field_name, fd.number,
                )
                # 同样走 aligned_column_type：改名必须发 CHANGE COLUMN，
                # 而 MySQL 会借着这条语句把整列数据真的搬到新类型上。
                # 只判"同族"就放行的话，顺手改个名就等于绕过了"永不收窄"。
                aligned = aligned_column_type(old_type, target_type)
                alter_sqls.append(
                    f"CHANGE COLUMN {escape_mysql_name(old_name)} {escape_mysql_name(field_name)} "
                    f"{aligned}{comment}"
                )
                del remaining[old_name]
                continue

            # 3) 全新字段
            alter_sqls.append(f"ADD COLUMN {escape_mysql_name(field_name)} {target_type}{comment}")

        if expand_only:
            offenders = [c for c in alter_sqls if not c.startswith("ADD COLUMN")]
            if offenders:
                raise ExpandOnlyViolationError(
                    f"表 {self.table_name} 的本次对齐含非「纯新增」变更，expand_only 下拒绝执行：\n  "
                    + "\n  ".join(offenders)
                    + "\n  这些语句在滚动发布下会被新旧副本来回执行。请改成"
                    "expand→migrate→contract 三步，或人工审核后单独执行。"
                )

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
        """⚠️ REPLACE = DELETE+INSERT，**会把本进程不认识的列清回默认值**。见 get_replace_sql。"""
        stmt = self.get_batch_insert_sql(messages)
        return Statement("REPLACE" + stmt.sql[len("INSERT") :], stmt.args)

    def get_replace_sql(self, message: Message) -> Statement:
        """⚠️ ``REPLACE INTO``：**会把本进程不认识的列清回默认值**。

        列清单来自本进程的 descriptor（``self._fields``），而 MySQL 的 REPLACE 语义是
        「先 DELETE 再 INSERT」——语句里没提到的列不是"保持原值"，是**回到列默认值**。
        滚动发布时旧版本进程执行一次，新版本刚写进去的列就没了；本库
        「永不 DROP COLUMN」的保护在这里完全帮不上忙，因为丢的是数据不是列。
        还会触发外键级联删除。

        :meth:`DB.save` 已经改走按完整主键的 UPDATE→INSERT 状态机，
        只覆盖本进程认识的列。本方法保留为**显式逃生口**：确实需要
        「整行推倒重来、未提及列一律归位」时才用。
        """
        args = self.all_field_args(message)
        return Statement(f"{self._replace_sql_prefix}{build_placeholders(len(args))})", args)

    def _validated_primary_key_fields(self) -> list[FieldDescriptor]:
        """返回完整、可用的主键字段；未声明或声明错误都 fail-closed。"""
        if not self.primary_key:
            raise PrimaryKeyNotFoundError(
                f"primary key not found: table {self.table_name}"
            )

        fields: list[FieldDescriptor] = []
        seen: set[str] = set()
        for name in self.primary_key:
            if name in seen:
                raise PrimaryKeyNotFoundError(
                    f"invalid primary key declaration: duplicate column {name} "
                    f"in table {self.table_name}"
                )
            seen.add(name)
            fd = self._field_by_name.get(name)
            if fd is None:
                raise PrimaryKeyNotFoundError(
                    f"primary key column {name} not found in table {self.table_name}"
                )
            fields.append(fd)
        return fields

    def _primary_key_identity_guard(self) -> str:
        """拟插入行与冲突行是同一主键身份时才为真。"""
        return " AND ".join(
            f"{escape_mysql_name(fd.name)} <=> VALUES({escape_mysql_name(fd.name)})"
            for fd in self._validated_primary_key_fields()
        )

    def _primary_key_noop_assignment(self) -> str:
        fd = self._validated_primary_key_fields()[0]
        escaped = escape_mysql_name(fd.name)
        return f"{escaped} = {escaped}"

    def _guarded_values_assignment(self, fd: FieldDescriptor, guard: str) -> str:
        escaped = escape_mysql_name(fd.name)
        return f"{escaped} = IF({guard}, VALUES({escaped}), {escaped})"

    def _values_update_clause(self) -> str:
        """带完整主键身份守卫的 ``VALUES(col)`` 赋值列表。

        与 get_insert_on_dup_update_sql 的区别：那个只覆盖「已赋值」的字段
        （proto3 零值视为未赋值），所以清零写不进去；save 的语义是「整行落库」，
        必须把零值也写进去，因此用全字段。

        不管哪一个，ON DUPLICATE KEY UPDATE 都**只动子句里点名的列**，
        且每项都由完整主键身份守卫；二级唯一键撞到不同主键时保持 owner 原值。
        本进程不认识的列也原样保留——这正是它比 REPLACE 安全的地方。

        主键列必须无条件排除：唯一键冲突时，任何 ``pk = VALUES(pk)`` 都会把
        既存行的主键改写成入参主键，外键和所有持有旧 id 的调用方一起错位。
        """
        guard = self._primary_key_identity_guard()
        pk = set(self.primary_key)
        fields = [fd for fd in self._fields if fd.name not in pk]
        if not fields:
            # 全部列都是主键（纯关联表）：退回无副作用赋值，
            # 避免生成空的 ON DUPLICATE KEY UPDATE。
            return self._primary_key_noop_assignment()
        return ", ".join(
            self._guarded_values_assignment(fd, guard) for fd in fields
        )

    def get_save_sql(self, message: Message) -> Statement:
        """低层整行 upsert SQL：每个更新项都带完整主键身份守卫。

        它不会清掉本进程不认识的列，也不会因二级 UNIQUE 冲突修改另一主键行。
        高层 :meth:`DB.save` 还必须区分「同主键竞态」与「不同 owner 冲突」，因此使用
        :meth:`get_save_update_sql` 驱动 UPDATE→INSERT 状态机，而不直接执行本语句。
        """
        stmt = self.get_insert_sql(message)
        return Statement(f"{stmt.sql} ON DUPLICATE KEY UPDATE {self._values_update_clause()}", stmt.args)

    def get_batch_save_sql(self, messages: Sequence[Message]) -> Statement:
        """批量整行落库，语义同 :meth:`get_save_sql`。"""
        stmt = self.get_batch_insert_sql(messages)
        return Statement(f"{stmt.sql} ON DUPLICATE KEY UPDATE {self._values_update_clause()}", stmt.args)

    def get_save_update_sql(
        self, message: Message, *, only_set_fields: bool = False
    ) -> Statement:
        """按完整主键更新非主键列，供安全的 UPDATE→INSERT 保存流程使用。

        默认是整行语义，包含 proto3 零值；``only_set_fields=True``
        时只更新 :func:`pbconv.has_field` 判定为已赋值的非主键列。
        纯主键表（或 partial 模式没有已赋值的非主键列）用
        ``pk = pk`` 作无副作用更新，保证 SET 子句语法有效且不改值。
        """
        self.validate_message(message)
        pk_fields = self._validated_primary_key_fields()
        pk_names = {fd.name for fd in pk_fields}

        clauses: list[str] = []
        set_args: list[Any] = []
        for fd in self._fields:
            if fd.name in pk_names:
                continue
            if only_set_fields and not pbconv.has_field(message, fd):
                continue
            clauses.append(f"{escape_mysql_name(fd.name)} = ?")
            set_args.append(pbconv.serialize_field_value(message, fd))

        if not clauses:
            clauses.append(self._primary_key_noop_assignment())

        where_clause = " AND ".join(
            f"{escape_mysql_name(fd.name)} = ?" for fd in pk_fields
        )
        where_args = [pbconv.serialize_field_value(message, fd) for fd in pk_fields]
        return Statement(
            f"UPDATE {escape_mysql_name(self.table_name)} SET {', '.join(clauses)} "
            f"WHERE {where_clause}",
            set_args + where_args,
        )

    def get_insert_on_dup_update_sql(self, message: Message) -> Statement:
        """低层 ODKU：仅同一完整主键身份时覆盖**已赋值**字段。

        与 :meth:`get_save_sql` 一样排除主键列，并用完整主键 NULL-safe 条件守卫
        每一个赋值；二级唯一键命中不同主键 owner 时不修改任何列。
        """
        guard = self._primary_key_identity_guard()
        stmt = self.get_insert_sql(message)
        skip = set(self.primary_key)

        clauses: list[str] = []
        for fd in self._fields:
            if fd.name in skip:
                continue
            if not pbconv.has_field(message, fd):
                continue
            clauses.append(self._guarded_values_assignment(fd, guard))

        if not clauses:
            # 只赋了主键列（或一个字段都没赋）时，早先直接返回裸 INSERT——
            # 语义从"有则更新"悄悄变成"有则报 1062"，调用方拿到的是一个
            # 它压根没打算处理的 DuplicateKeyError。退回"只拿行锁不改数据"，
            # 与 _values_update_clause 的全主键回退同一个形状。
            return Statement(
                f"{stmt.sql} ON DUPLICATE KEY UPDATE {self._primary_key_noop_assignment()}",
                stmt.args,
            )
        return Statement(
            f"{stmt.sql} ON DUPLICATE KEY UPDATE {', '.join(clauses)}",
            stmt.args,
        )

    def get_insert_on_dup_key_for_primary_key_sql(self, message: Message) -> Statement:
        """INSERT ... ON DUPLICATE KEY UPDATE pk = pk：只拿行锁，不改数据。"""
        if self._primary_key_field is None:
            raise PrimaryKeyNotFoundError(f"primary key not found: table {self.table_name}")

        stmt = self.get_insert_sql(message)
        pk_name = self._primary_key_field.name
        escaped = escape_mysql_name(pk_name)
        return Statement(
            f"{stmt.sql} ON DUPLICATE KEY UPDATE {escaped} = {escaped}",
            stmt.args,
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
