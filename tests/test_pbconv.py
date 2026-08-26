"""值转换层的测试：类型映射、浮点格式、Timestamp、二进制往返。

这些是"改了不报错、只是数据悄悄变形"的地方，所以逐条钉死。
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from proto2mysql import pbconv
from proto2mysql.errors import NonFiniteFloatError


def field(msg, name):
    return msg.DESCRIPTOR.fields_by_name[name]


# ── 浮点格式（对齐 Go 的 strconv.FormatFloat(f, 'f', -1, bitSize)）──────


@pytest.mark.parametrize(
    ("value", "bits", "want"),
    [
        (0.1, 32, "0.1"),  # float32 必须按 32 位求最短表示，否则会写成 0.10000000149011612
        (0.1, 64, "0.1"),
        (1.0, 64, "1"),
        (-0.0, 64, "-0"),
        (1e20, 64, "100000000000000000000"),  # 'f' 格式不用指数
        (1e-7, 64, "0.0000001"),
        (1.5, 32, "1.5"),
        (3.4028234663852886e38, 32, "340282350000000000000000000000000000000"),
    ],
)
def test_format_float(value, bits, want):
    assert pbconv.format_float(value, bits) == want


def test_float32_shortest_roundtrip(kitchenpb):
    """float32 字段读回来是 double 化的 0.10000000149011612，但必须写成 0.1。"""
    m = kitchenpb.kitchen_sink(f32=0.1)
    assert m.f32 != 0.1  # 先确认前提：Python 读回的确实不是 0.1
    assert pbconv.serialize_field_value(m, field(m, "f32")) == "0.1"


def test_non_finite_float_rejected(kitchenpb):
    """NaN/Inf 必须在写库前拦下，不能丢给 MySQL 静默存成 0。"""
    for bad in (float("nan"), float("inf"), float("-inf")):
        m = kitchenpb.kitchen_sink(f64=bad)
        with pytest.raises(NonFiniteFloatError):
            pbconv.serialize_field_value(m, field(m, "f64"))


# ── Timestamp ───────────────────────────────────────────────────────────


def test_timestamp_unset_is_sql_null(kitchenpb):
    """未设置的 Timestamp 必须下发 SQL NULL，不能是空串（空串会被 STRICT 模式拒掉整行）。"""
    m = kitchenpb.kitchen_sink()
    assert pbconv.serialize_field_value(m, field(m, "created_at")) is None


def test_timestamp_microsecond_precision(kitchenpb):
    m = kitchenpb.kitchen_sink()
    m.created_at.seconds = 1700000000
    m.created_at.nanos = 123456789  # 纳秒位会丢，这是 MySQL 的硬上限
    assert (
        pbconv.serialize_field_value(m, field(m, "created_at"))
        == "2023-11-14 22:13:20.123456"
    )


def test_timestamp_nanos_truncated_not_rounded(kitchenpb):
    """截断而不是四舍五入——与 Go 的 Format 一致。999999999ns 是 .999999 不是下一秒。"""
    m = kitchenpb.kitchen_sink()
    m.created_at.seconds = 0
    m.created_at.nanos = 999999999
    assert pbconv.serialize_field_value(m, field(m, "created_at")) == "1970-01-01 00:00:00.999999"


def test_timestamp_parse_back(kitchenpb):
    m = kitchenpb.kitchen_sink()
    fd = field(m, "created_at")
    pbconv.set_field_from_raw(m, fd, "2023-11-14 22:13:20.123456")
    assert m.created_at.seconds == 1700000000
    assert m.created_at.nanos == 123456000

    # datetime 对象（PyMySQL 对 DATETIME 列直接给 datetime）
    m2 = kitchenpb.kitchen_sink()
    pbconv.set_field_from_raw(m2, fd, dt.datetime(2023, 11, 14, 22, 13, 20, 123456))
    assert m2.created_at.seconds == 1700000000
    assert m2.created_at.nanos == 123456000


def test_timestamp_null_clears_field(kitchenpb):
    """读到 NULL 必须显式清除字段，否则复用 message 时会残留上一次的值。"""
    m = kitchenpb.kitchen_sink()
    m.created_at.seconds = 1700000000
    pbconv.set_field_from_raw(m, field(m, "created_at"), None)
    assert not m.HasField("created_at")


# ── 二进制：裸字节、不做 base64 ─────────────────────────────────────────


def test_bytes_raw_roundtrip(kitchenpb):
    raw = bytes(range(256))
    m = kitchenpb.kitchen_sink(payload=raw)
    fd = field(m, "payload")
    assert pbconv.serialize_field_value(m, fd) == raw  # 原样，没有 base64

    back = kitchenpb.kitchen_sink()
    pbconv.set_field_from_raw(back, fd, raw)
    assert back.payload == raw


def test_nested_message_is_wire_bytes(kitchenpb):
    """嵌套消息落库的字节必须与手写 SerializeToString() 逐字节相同。"""
    m = kitchenpb.kitchen_sink()
    m.sub.a = 7
    m.sub.b = "x"
    fd = field(m, "sub")
    assert pbconv.serialize_field_value(m, fd) == m.sub.SerializeToString()

    back = kitchenpb.kitchen_sink()
    pbconv.set_field_from_raw(back, fd, m.sub.SerializeToString())
    assert back.sub.a == 7 and back.sub.b == "x"


def test_container_roundtrip(kitchenpb):
    m = kitchenpb.kitchen_sink()
    m.tags.extend([1, 2, 3])
    m.attrs["a"] = 1
    m.attrs["b"] = 2

    tags_fd, attrs_fd = field(m, "tags"), field(m, "attrs")
    tags_blob = pbconv.serialize_field_value(m, tags_fd)
    attrs_blob = pbconv.serialize_field_value(m, attrs_fd)
    assert isinstance(tags_blob, bytes) and tags_blob

    back = kitchenpb.kitchen_sink()
    pbconv.set_field_from_raw(back, tags_fd, tags_blob)
    pbconv.set_field_from_raw(back, attrs_fd, attrs_blob)
    assert list(back.tags) == [1, 2, 3]
    assert dict(back.attrs) == {"a": 1, "b": 2}


def test_list_parse_overwrites_not_appends(kitchenpb):
    """读回 repeated 必须覆盖旧值。追加的话复用 message 查两次就会翻倍。"""
    m = kitchenpb.kitchen_sink()
    m.tags.extend([1, 2, 3])
    blob = pbconv.serialize_field_value(m, field(m, "tags"))

    pbconv.set_field_from_raw(m, field(m, "tags"), blob)
    assert list(m.tags) == [1, 2, 3]


def test_empty_container_is_empty_bytes(kitchenpb):
    m = kitchenpb.kitchen_sink()
    assert pbconv.serialize_field_value(m, field(m, "tags")) == b""
    assert pbconv.serialize_field_value(m, field(m, "attrs")) == b""


# ── 标量 ────────────────────────────────────────────────────────────────


def test_bool_is_one_zero_not_true_false(kitchenpb):
    """必须是 "1"/"0"：tinyint(1) 列在 STRICT 模式下拒绝 'true'（Error 1366）。"""
    m = kitchenpb.kitchen_sink(flag=True)
    assert pbconv.serialize_field_value(m, field(m, "flag")) == "1"
    m.flag = False
    assert pbconv.serialize_field_value(m, field(m, "flag")) == "0"


def test_bool_parse_accepts_legacy_literals(kitchenpb):
    """存量行可能是旧版本写的 "true"/"false"，读侧必须兼容。"""
    m = kitchenpb.kitchen_sink()
    fd = field(m, "flag")
    for text, want in [("1", True), ("0", False), ("true", True), ("False", False)]:
        pbconv.set_field_from_raw(m, fd, text)
        assert m.flag is want


def test_uint64_max_roundtrip(kitchenpb):
    m = kitchenpb.kitchen_sink(u64=2**64 - 1)
    fd = field(m, "u64")
    assert pbconv.serialize_field_value(m, fd) == "18446744073709551615"
    back = kitchenpb.kitchen_sink()
    pbconv.set_field_from_raw(back, fd, "18446744073709551615")
    assert back.u64 == 2**64 - 1


def test_int_out_of_range_rejected(kitchenpb):
    m = kitchenpb.kitchen_sink()
    with pytest.raises(ValueError):
        pbconv.set_field_from_raw(m, field(m, "zone_id"), "99999999999")


def test_enum_is_numeric(kitchenpb):
    m = kitchenpb.kitchen_sink(tier=kitchenpb.GRADE_GOLD)
    assert pbconv.serialize_field_value(m, field(m, "tier")) == "1"


def test_native_driver_types_accepted(kitchenpb):
    """PyMySQL 对数值列返回 int/float 而不是字符串，读侧要吃得下。"""
    m = kitchenpb.kitchen_sink()
    pbconv.set_field_from_raw(m, field(m, "id"), 42)
    pbconv.set_field_from_raw(m, field(m, "f64"), 1.5)
    pbconv.set_field_from_raw(m, field(m, "name"), "n")
    assert (m.id, m.f64, m.name) == (42, 1.5, "n")


# ── presence 语义（proto3 零值 = 未赋值）────────────────────────────────


def test_has_field_semantics(kitchenpb):
    m = kitchenpb.kitchen_sink()
    assert not pbconv.has_field(m, field(m, "id"))
    m.id = 0
    assert not pbconv.has_field(m, field(m, "id")), "proto3 标量零值必须算未赋值"
    m.id = 1
    assert pbconv.has_field(m, field(m, "id"))

    # optional 字段有 presence：写 0 也算已赋值
    m.opt_score = 0
    assert pbconv.has_field(m, field(m, "opt_score"))

    # 容器非空才算已赋值
    assert not pbconv.has_field(m, field(m, "tags"))
    m.tags.append(1)
    assert pbconv.has_field(m, field(m, "tags"))


def test_empty_value_restores_default_keeping_presence(kitchenpb):
    """空值回填默认值时，optional 字段必须保持"已设置"——与 Go 的 Set(fd.Default()) 一致。"""
    m = kitchenpb.kitchen_sink(opt_score=5)
    pbconv.set_field_from_raw(m, field(m, "opt_score"), "")
    assert m.HasField("opt_score")
    assert m.opt_score == 0


def test_full_row_roundtrip(kitchenpb):
    """整行序列化 → 反序列化，逐字段等价。"""
    m = kitchenpb.kitchen_sink(
        id=1, name="名字", zone_id=3, u32=4, u64=5, f32=0.5, f64=0.25,
        flag=True, payload=b"\x00\xff", tier=kitchenpb.GRADE_GOLD, opt_score=9,
    )
    m.tags.extend([7, 8])
    m.attrs["k"] = 1
    m.sub.a = 2
    m.created_at.seconds = 1700000000
    m.created_at.nanos = 500000

    row = [pbconv.serialize_field_value(m, fd) for fd in m.DESCRIPTOR.fields]
    back = kitchenpb.kitchen_sink()
    pbconv.parse_from_row(back, row)
    assert back == m


def test_math_isclose_not_needed_for_double(kitchenpb):
    """double 走最短往返表示，读回来必须是**同一个** double，不是近似值。"""
    m = kitchenpb.kitchen_sink(f64=math.pi)
    fd = field(m, "f64")
    text = pbconv.serialize_field_value(m, fd)
    back = kitchenpb.kitchen_sink()
    pbconv.set_field_from_raw(back, fd, text)
    assert back.f64 == m.f64


def test_format_float_always_roundtrips():
    """性质测试：随机取值，格式化出来的字符串必须能精确还原成同一个浮点数。

    这条比逐个金字面量更重要——最短表示的实现（repr vs 逐位搜索）换了也不能破。
    """
    import random
    import struct

    rng = random.Random(20260818)
    for _ in range(2000):
        bits = rng.getrandbits(64)
        value = struct.unpack("<d", struct.pack("<Q", bits))[0]
        if math.isnan(value) or math.isinf(value):
            continue
        text = pbconv.format_float(value, 64)
        assert float(text) == value, f"float64 往返失败: {value!r} -> {text}"
        assert "e" not in text and "E" not in text, f"不该出现指数: {text}"

    for _ in range(2000):
        bits = rng.getrandbits(32)
        f32 = struct.unpack("<f", struct.pack("<I", bits))[0]
        if math.isnan(f32) or math.isinf(f32):
            continue
        text = pbconv.format_float(f32, 32)
        back = struct.unpack("<f", struct.pack("<f", float(text)))[0]
        assert back == f32, f"float32 往返失败: {f32!r} -> {text}"


def test_format_float_shortest_not_just_correct():
    """不只要能还原，还要是**最短**的那个——否则 SQL 字面量和 Go 版对不上。"""
    assert pbconv.format_float(0.1, 32) == "0.1"  # 不是 0.10000000149011612
    assert pbconv.format_float(1 / 3, 64) == "0.3333333333333333"
    assert len(pbconv.format_float(0.1, 64)) == len("0.1")


# ── NULL 列跨行串位（P0，两版同构） ─────────────────────────────────────


def test_null_columns_do_not_leak_across_rows(kitchenpb):
    """列值为 NULL 时必须把字段重置成默认值，否则复用 message 会串行。

    原先 _set_scalar_default 只处理 8 种标量，**刻意跳过 bytes / enum / message**
    （注释写着"与 Go 版一致"）。于是：

        out = kitchen_sink(); find_one_by_pk(out)   # alice: payload=b"alice", tier=GOLD
        out.id = 2;           find_one_by_pk(out)   # bob 这三列都是 NULL
        # → out.payload / out.tier / out.sub 全是 alice 的值，**bob 拿到了 alice 的数据**

    而 find_one_by_pk(out) 的 out 既是入参（主键）又是出参，复用同一个 message
    正是这个 API 的天然用法，所以这不是"误用"。Go 侧 scanOneProtoRow 同构。
    """
    from proto2mysql import pbconv

    sub = kitchenpb.nested(a=7, b="alice-note")
    out = kitchenpb.kitchen_sink()

    # 第一行：三类都有值
    pbconv.parse_from_row(out, (
        1, "alice", 7, 0, 0, 0.0, 0.0, False,
        b"alice-payload", 1, None, None, None, 0, sub.SerializeToString(),
    ))
    assert out.payload == b"alice-payload"
    assert out.tier == 1
    assert out.sub.b == "alice-note"

    # 第二行：payload / tier / sub 三列都是 NULL
    pbconv.parse_from_row(out, (
        2, "bob", 0, 0, 0, 0.0, 0.0, False,
        None, None, None, None, None, 0, None,
    ))
    assert out.id == 2 and out.name == "bob"
    assert out.payload == b"", "bytes 列为 NULL 必须清空，否则串到下一行"
    assert out.tier == 0, "enum 列为 NULL 必须归零"
    assert out.sub.b == "", "message 列为 NULL 必须 ClearField"
