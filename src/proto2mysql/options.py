"""从 proto 描述符读取建表元数据（file / message / field option），以及代码级 TableOption。

对应 Go 版的 options.go。选项定义见 ``proto2mysql/proto/proto2mysql_option.proto``，
运行时**按字段号**反射读取，不 import 生成的 option stub——这一点是刻意的：

  * 用户的 ``.proto`` 一旦 ``import "proto2mysql_option.proto"``，protoc 生成的
    ``_pb2`` 模块就会 import option stub，扩展自动进 descriptor pool，
    ``ListFields()`` 直接按字段号读得到。本库自己再 import 一份反而会往默认 pool 里
    重复注册同名文件（用户工程里通常已经有一份自己生成的 stub），触发
    "duplicate file name" 冲突。
  * 按字段号匹配还能兼容动态描述符（从 FileDescriptorSet 加载的、扩展未注册的情况），
    与 Go 版 rangeExtensions 的注释是同一个理由。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Iterable

from google.protobuf.descriptor import Descriptor, FieldDescriptor, FileDescriptor

from . import _wire

log = logging.getLogger("proto2mysql")

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，运行期不 import（避免循环依赖）
    from .table import MessageTable

# ── message option 字段号 ────────────────────────────────────────────────
OPT_NUM_TABLE_NAME = 500001  # 表名
OPT_NUM_PRIMARY_KEY = 500002  # 主键（逗号分隔=联合主键）
OPT_NUM_AUTO_INCREMENT_KEY = 500006  # 自增字段
OPT_NUM_INDEX = 500011  # 普通索引（分号分隔多个索引，索引内逗号分隔=联合索引）
OPT_NUM_UNIQUE_KEY = 500012  # 唯一键（逗号分隔=联合唯一键）

# ── field option 字段号 ──────────────────────────────────────────────────
OPT_NUM_FIELD_NULLABLE = 600100  # 该字段允许为 NULL

# ── file option 字段号 ───────────────────────────────────────────────────
OPT_NUM_FILE_DB = 500000  # 标记该 .proto 文件用于 proto2mysql 建表

# descriptor.proto 给所有 *Options 消息声明的扩展区间都是 `extensions 1000 to max`，
# 所以裸字节里字段号 >= 1000 的一定是扩展，不会和标准 option 字段撞号。
_EXTENSION_RANGE_START = 1000

TableOption = Callable[["MessageTable"], None]


def range_extensions(options) -> dict[int, object]:
    """遍历 options 消息上**已设置的扩展**，返回 {字段号: 值}。

    对应 Go 的 rangeExtensions。两条路径叠加：

    1. ``ListFields()``——扩展已注册时走这条，拿到的是解好类型的值；
    2. 裸字节扫描——扩展没注册时值落在 unknown fields 里，而 upb 实现的
       ``UnknownFields()`` 抛 NotImplementedError，只能自己扫 wire 格式。

    两条路径的结果合并，路径 1 优先（类型已解好）。
    """
    if options is None:
        return {}

    out: dict[int, object] = {}
    for field, value in options.ListFields():
        if field.is_extension:
            out[field.number] = value

    # 补扫裸字节，捡回未注册的扩展。只取字段号 >= 1000 的，避开标准 option 字段。
    try:
        raw = options.SerializeToString()
    except Exception:  # pragma: no cover - 理论上不会失败
        return out
    if not raw:
        return out
    try:
        scanned = _wire.scan_fields(raw)
    except ValueError as exc:
        # 运行期刻意**不抛**：一份坏 option 不该打断 register_all_tables 的整轮注册。
        # 但绝不能无声——退化的后果是"这个 message 看起来一个扩展都没有"，
        # 表现为表名退回 proto full name、主键凭空消失，症状离根因十万八千里。
        log.warning("option 裸字节扫描失败，已退化为只读已注册的扩展：%s", exc)
        return out
    for num, value in scanned.items():
        if num >= _EXTENSION_RANGE_START and num not in out:
            out[num] = value
    return out


def _as_str(value: object) -> str:
    """把扩展值取成 str。裸字节扫描路径拿到的是 bytes，需要 decode。"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return "" if value is None else str(value)


def _as_bool(value: object) -> bool:
    """把扩展值取成 bool。裸字节扫描路径拿到的 varint 是 int。"""
    if isinstance(value, bytes):
        return value != b""
    return bool(value)


def file_has_db_option(fd: FileDescriptor) -> bool:
    """判断某个 .proto 文件是否声明了 ``option (proto2mysql.db) = true;``。

    用于自动注册时筛选"用于建表"的文件。对应 Go 的 FileHasDBOption。
    """
    return _as_bool(range_extensions(fd.GetOptions()).get(OPT_NUM_FILE_DB))


def table_name_from_descriptor(md: Descriptor) -> tuple[str, bool]:
    """读 message option 里的表名；第二个返回值表示该消息是否声明了表选项。

    对应 Go 的 TableNameFromDescriptor。
    """
    name = _as_str(range_extensions(md.GetOptions()).get(OPT_NUM_TABLE_NAME))
    return name, name != ""


def field_is_nullable(fd: FieldDescriptor) -> bool:
    """读 field option 里的 nullable。"""
    return _as_bool(range_extensions(fd.GetOptions()).get(OPT_NUM_FIELD_NULLABLE))


def table_options_from_descriptor(md: Descriptor) -> list[TableOption]:
    """从消息描述符读建表配置，转成 TableOption 列表。

    支持的 message option：表名 / 主键 / 自增 / 索引 / 唯一键；field option：nullable。
    register_table / generate_create_table_sql 会自动应用；
    代码传入的 TableOption 后应用，优先级更高。对应 Go 的 TableOptionsFromDescriptor。
    """
    opts: list[TableOption] = []
    ext = range_extensions(md.GetOptions())

    name = _as_str(ext.get(OPT_NUM_TABLE_NAME))
    if name:
        opts.append(with_table_name(name))

    cols = split_option_csv(_as_str(ext.get(OPT_NUM_PRIMARY_KEY)))
    if cols:
        opts.append(with_primary_key(*cols))

    auto = _as_str(ext.get(OPT_NUM_AUTO_INCREMENT_KEY)).strip()
    if auto:
        opts.append(with_auto_increment_key(auto))

    indexes = split_option_indexes(_as_str(ext.get(OPT_NUM_INDEX)))
    if indexes:
        opts.append(with_indexes(*indexes))

    unique = _as_str(ext.get(OPT_NUM_UNIQUE_KEY)).strip()
    if unique:
        opts.append(with_unique_key(unique))

    nullable = [f.name for f in md.fields if field_is_nullable(f)]
    if nullable:
        opts.append(with_nullable_fields(*nullable))

    return opts


def split_option_csv(s: str) -> list[str]:
    """拆分逗号分隔的字段列表并去空白，忽略空项。"""
    return [p.strip() for p in s.split(",") if p.strip()]


def split_option_indexes(s: str) -> list[str]:
    """拆分索引选项：分号分隔多个索引，每个索引保留内部逗号（联合索引）。

    例："last_login" → 1 个索引；"player_id;zone_id,created_at" → 2 个（第 2 个是联合索引）。
    """
    return [p.strip() for p in s.split(";") if p.strip()]


# ── 代码级 TableOption（可覆盖 proto 里的声明）──────────────────────────


def with_table_name(name: str) -> TableOption:
    """自定义 SQL 表名（默认 = proto full name）。

    注意：注册与查找仍按 proto full name 进行，本选项只影响生成的 SQL。
    """

    def apply(t: "MessageTable") -> None:
        t.table_name = name

    return apply


def with_primary_key(*keys: str) -> TableOption:
    """设置主键字段（多个 = 联合主键）。"""

    def apply(t: "MessageTable") -> None:
        t.primary_key = list(keys)

    return apply


def with_indexes(*indexes: str) -> TableOption:
    """设置普通索引；每项内部用逗号分隔 = 联合索引。"""

    def apply(t: "MessageTable") -> None:
        t.indexes = list(indexes)

    return apply


def with_unique_key(unique_key: str) -> TableOption:
    """设置唯一键（逗号分隔 = 联合唯一键）。"""

    def apply(t: "MessageTable") -> None:
        t.unique_keys = unique_key

    return apply


def with_auto_increment_key(key: str) -> TableOption:
    """设置自增字段。"""

    def apply(t: "MessageTable") -> None:
        t.auto_increase_key = key

    return apply


def with_nullable_fields(*fields: str) -> TableOption:
    """设置允许为 NULL 的字段。"""

    def apply(t: "MessageTable") -> None:
        t.nullable_fields = list(fields)

    return apply


def apply_options(table: "MessageTable", opts: Iterable[TableOption]) -> None:
    """按顺序应用 TableOption，后面的覆盖前面的。"""
    for opt in opts:
        opt(table)
