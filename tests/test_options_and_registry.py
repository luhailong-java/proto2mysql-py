"""option 读取（含扩展未注册时的裸字节兜底）与自动注册。

这两块是整个移植里 Python 与 Go 差别最大的地方，所以测得细一点。
"""

from __future__ import annotations

import pytest
from google.protobuf import descriptor_pb2

from proto2mysql import (
    DB,
    MessageTable,
    file_has_db_option,
    table_name_from_descriptor,
    with_primary_key,
    with_table_name,
)
from proto2mysql._wire import scan_fields
from proto2mysql.options import (
    OPT_NUM_AUTO_INCREMENT_KEY,
    OPT_NUM_FILE_DB,
    OPT_NUM_INDEX,
    OPT_NUM_PRIMARY_KEY,
    OPT_NUM_TABLE_NAME,
    OPT_NUM_UNIQUE_KEY,
    range_extensions,
)


# ── 按字段号读 option ───────────────────────────────────────────────────


def test_message_options_read_by_number(kitchenpb):
    ext = range_extensions(kitchenpb.kitchen_sink.DESCRIPTOR.GetOptions())
    assert ext[OPT_NUM_TABLE_NAME] == "kitchen_sink"
    assert ext[OPT_NUM_PRIMARY_KEY] == "id"
    assert ext[OPT_NUM_AUTO_INCREMENT_KEY] == "id"
    assert ext[OPT_NUM_INDEX] == "name;zone_id,created_at"
    assert ext[OPT_NUM_UNIQUE_KEY] == "name,zone_id"


def test_file_and_field_options(kitchenpb):
    assert file_has_db_option(kitchenpb.DESCRIPTOR) is True
    table = MessageTable.from_message(kitchenpb.kitchen_sink)
    assert table.nullable_fields == ["zone_id"]


def test_table_name_from_descriptor(kitchenpb):
    assert table_name_from_descriptor(kitchenpb.kitchen_sink.DESCRIPTOR) == ("kitchen_sink", True)
    # 没声明 table_name 的消息不算表
    assert table_name_from_descriptor(kitchenpb.nested.DESCRIPTOR) == ("", False)


def test_options_parsed_into_table(kitchenpb):
    t = MessageTable.from_message(kitchenpb.kitchen_sink)
    assert t.table_name == "kitchen_sink"
    assert t.primary_key == ["id"]
    assert t.auto_increase_key == "id"
    assert t.indexes == ["name", "zone_id,created_at"]  # 分号分组，组内逗号=联合索引
    assert t.unique_keys == "name,zone_id"


def test_code_options_override_proto(kitchenpb):
    """代码传入的 TableOption 后应用，优先级高于 proto 里的声明。"""
    t = MessageTable.from_message(
        kitchenpb.kitchen_sink, [with_table_name("other"), with_primary_key("name")]
    )
    assert t.table_name == "other"
    assert t.primary_key == ["name"]


# ── 扩展未注册时的裸字节兜底 ────────────────────────────────────────────


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def test_wire_fallback_reads_unregistered_extensions():
    """扩展**没注册**时也要读得出来——这条是兜底路径的唯一证明。

    构造一个当前进程里根本没有对应扩展定义的字段号（999999），
    它在 protobuf 眼里就是 unknown field：ListFields() 看不见它，
    而 upb 实现（protobuf 5.x/6.x/7.x 默认）的 UnknownFields() 直接抛
    NotImplementedError。只有扫裸字节这一条路。

    离线工具从 FileDescriptorSet 加载描述符、而集合里没带上 option 文件时，
    proto2mysql 的 option 就是这个状态。
    """
    unknown_num = 999999
    payload = b"unregistered"
    raw = _varint((unknown_num << 3) | 2) + _varint(len(payload)) + payload

    opts = descriptor_pb2.MessageOptions()
    opts.ParseFromString(raw)

    # 前提确认：标准路径确实看不见它
    assert [f.number for f, _ in opts.ListFields()] == []
    with pytest.raises(NotImplementedError):
        list(opts.UnknownFields())

    # 兜底路径读得到
    assert range_extensions(opts)[unknown_num] == payload


def test_registered_extensions_read_via_listfields(kitchenpb):
    """扩展已注册时走标准路径，拿到的是解好类型的 str 而不是 bytes。"""
    ext = range_extensions(kitchenpb.kitchen_sink.DESCRIPTOR.GetOptions())
    assert ext[OPT_NUM_TABLE_NAME] == "kitchen_sink"
    assert isinstance(ext[OPT_NUM_TABLE_NAME], str)


def test_wire_scanner_basics():
    """扫描器本身：varint / 长度分隔 / 跳过未知类型都要正确，否则后续字段号全错位。"""
    opts = descriptor_pb2.MessageOptions()
    opts.deprecated = True  # 标准字段 3（varint）
    opts.map_entry = True  # 标准字段 7（varint）
    fields = scan_fields(opts.SerializeToString())
    assert fields[3] == 1
    assert fields[7] == 1


def test_wire_scanner_rejects_garbage():
    with pytest.raises(ValueError):
        scan_fields(b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff")


def test_range_extensions_ignores_standard_fields():
    """裸字节兜底只捡字段号 >= 1000 的，不能把标准 option 当成扩展。"""
    opts = descriptor_pb2.MessageOptions()
    opts.deprecated = True
    assert range_extensions(opts) == {}


# ── 自动注册 ────────────────────────────────────────────────────────────


def test_register_all_tables(testpb, kitchenpb, accountpb):
    db = DB(None, "testdb")
    registered = db.register_all_tables(modules=[testpb, kitchenpb, accountpb])

    assert "golang_test" in registered
    assert "kitchen.kitchen_sink" in registered
    assert "account" in registered

    # 只声明了 db 但没有 table_name 的消息不注册
    assert "golang_test_list" not in registered
    assert "player" not in registered
    assert "kitchen.nested" not in registered
    assert "plain" not in registered


def test_register_all_tables_scans_sys_modules(testpb):
    """不传 modules 时扫 sys.modules，效果与显式点名一致（前提是模块已 import）。"""
    db = DB(None, "testdb")
    registered = db.register_all_tables()
    assert "golang_test" in registered


def test_registry_key_is_full_name_not_table_name(kitchenpb):
    """注册键固定是 proto full name；table_name 只影响生成的 SQL。"""
    db = DB(None, "testdb")
    db.register_table(kitchenpb.kitchen_sink)
    assert "kitchen.kitchen_sink" in db.tables
    assert db.tables["kitchen.kitchen_sink"].table_name == "kitchen_sink"


def test_table_without_options_defaults_to_full_name(kitchenpb):
    """没声明 table_name 的消息，表名默认取 proto full name（含点号，会被整体转义）。"""
    t = MessageTable.from_message(kitchenpb.nested)
    assert t.table_name == "kitchen.nested"
    assert "`kitchen.nested`" in t.get_create_table_sql()
