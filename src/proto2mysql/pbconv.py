"""proto 字段值 <-> MySQL 参数值的双向转换。

对应 Go 版的 pbconv/convert.go。转换规则逐条与 Go 版对齐，包括那些
"改了就会静默出错"的取舍——每条都在注释里写明原因，别按直觉简化。

Go 侧所有值都序列化成 string 下发，二进制装在 Go 的 string 里（Go 的 string 可以
承载任意字节）。Python 的 str 是 Unicode，装不了裸字节，所以二进制字段返回 ``bytes``。
这是两边唯一的表示差异，落到 MySQL 的字节是一样的。
"""

from __future__ import annotations

import datetime as _dt
import math
import struct
from decimal import Decimal
from typing import Any

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

from .errors import InvalidFieldKindError, NonFiniteFloatError

TIMESTAMP_FULL_NAME = "google.protobuf.Timestamp"

# 写入 MySQL DATETIME 列的时间格式（见 _serialize_timestamp）。
#
# 必须带 6 位小数秒：不带的话毫秒/纳秒会被**静默**截断到整秒（不报错、无警告），
# 对应列类型为 DATETIME(6)。MySQL 时间类型最高只支持微秒(fsp=6)，
# 所以 proto Timestamp 的纳秒位仍会丢——这是 MySQL 的硬上限。
#
# ⚠️ 写进未迁移的老 DATETIME(0) 列时，MySQL 会按小数秒**四舍五入**（.9 进位到下一秒）。
# 存量表请先 ALTER ... MODIFY col DATETIME(6)。

# 从 MySQL 读取时间时支持的格式（按优先级尝试）。
# 带 RFC3339 两条是因为：Go 侧连接串开 parseTime=true 时驱动会把 DATETIME 解成 time.Time，
# 再渲染成 RFC3339Nano。Python 侧 PyMySQL 直接给 datetime 对象，一般走不到字符串解析，
# 但跨语言读同一张表 / 读别处写的文本时仍可能遇到，保留兼容。
_TIMESTAMP_PARSE_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",  # MySQL 原生文本（带小数秒）
    "%Y-%m-%d %H:%M:%S",  # MySQL 原生文本（不带小数秒）
    "%Y-%m-%dT%H:%M:%S.%f%z",  # parseTime=true 时驱动转换后的形态
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",  # 仅日期（DATE 列）
)

# time.Time 的零值（公元 1 年 1 月 1 日 UTC）对应的 Unix 秒。
# Go 版用 ts.AsTime().IsZero() 判"零时间"，等价于 seconds 恰好是这个值且 nanos 为 0。
# 注意它**不是** Timestamp{} 的零值（那是 1970-01-01），两者别混。
_GO_ZERO_TIME_SECONDS = -62135596800

_T = FieldDescriptor

# Go 的 protoreflect.Kind 与 protobuf 的 FieldDescriptorProto.Type 是同一套编号，
# 所以 Python 的 fd.type 可以直接和 Go 的 Kind 一一对应。
_INT_TYPES = (_T.TYPE_INT32, _T.TYPE_INT64)
_UINT_TYPES = (_T.TYPE_UINT32, _T.TYPE_UINT64)

_INT_BITS = {
    _T.TYPE_INT32: 32,
    _T.TYPE_INT64: 64,
    _T.TYPE_UINT32: 32,
    _T.TYPE_UINT64: 64,
}


def is_timestamp_field(fd: FieldDescriptor) -> bool:
    """判断字段是否为单值的 google.protobuf.Timestamp。"""
    return (
        not fd.is_repeated
        and fd.type == _T.TYPE_MESSAGE
        and fd.message_type is not None
        and fd.message_type.full_name == TIMESTAMP_FULL_NAME
    )


def is_map_field(fd: FieldDescriptor) -> bool:
    """判断字段是否为 map。

    Python 里 map 字段的 label 也是 REPEATED，所以 is_repeated 分不出 map 和 list，
    必须看 map_entry 选项——这与 Go 的 IsMap()/IsList() 是两个独立判定一致。
    """
    return (
        fd.is_repeated
        and fd.type == _T.TYPE_MESSAGE
        and fd.message_type is not None
        and fd.message_type.GetOptions().map_entry
    )


def is_list_field(fd: FieldDescriptor) -> bool:
    """判断字段是否为 repeated（不含 map）。"""
    return fd.is_repeated and not is_map_field(fd)


def has_field(message: Message, fd: FieldDescriptor) -> bool:
    """字段是否"已设置"，语义与 Go 的 protoreflect Has() 逐类对齐。

    - repeated / map：非空即已设置
    - message：HasField（天然两态）
    - 有 presence 的标量（proto3 optional / proto2）：HasField
    - proto3 无 presence 的标量：非零值即已设置
    """
    if fd.is_repeated:
        return len(getattr(message, fd.name)) > 0
    if fd.has_presence:
        return message.HasField(fd.name)
    return getattr(message, fd.name) != fd.default_value


def format_float(value: float, bit_size: int, field_name: str = "") -> str:
    """按 Go 的 strconv.FormatFloat(f, 'f', -1, bitSize) 格式化浮点数。

    两条硬要求，缺一条就和 Go 版产生不同的 SQL 字面量：

    1. **精度 -1**：用能唯一还原该 bitSize 浮点值的最少位数。float32 字段必须按
       32 位判定往返，否则 0.1 会写成 0.10000000149011612。
    2. **'f' 格式**：一律十进制，不用指数。Python 的 repr(1e20) 是 '1e+20'，
       Go 是 '100000000000000000000'——直接下发的话两边入库字面量不同。
       这里用 Decimal 把最短表示还原成定点写法（Decimal 从十进制字符串构造是精确的）。

    NaN/±Inf 直接报错而不是交给 MySQL：MySQL 的 FLOAT/DOUBLE 没有它们的表示，
    STRICT 模式下报 Error 1265 Data truncated（完全看不出根因），非 STRICT 模式下
    悄悄存成 0——那是静默的数据损坏。这里 fail-closed。
    """
    if math.isnan(value) or math.isinf(value):
        raise NonFiniteFloatError(
            "non-finite float value (NaN/Inf) cannot be stored in MySQL: "
            f"field {field_name} = {value}"
        )

    target = _coerce_bits(value, bit_size)

    if bit_size == 64:
        # CPython 的 repr 自 3.1 起就是"能唯一还原该 double 的最短十进制"，
        # 与 Go 精度 -1 求的是同一个东西，直接用，不必搜。
        shortest = repr(target)
    else:
        # float32 没有这样的现成表示：repr 给的是 double 的最短形式（0.1 会变成
        # 0.10000000149011612）。只能逐位加精度试到能按 **32 位** 往返为止。
        # 9 位有效数字必然唯一确定一个 float32，所以循环一定在 9 之前收敛。
        shortest = repr(target)
        for precision in range(1, 10):
            candidate = f"{target:.{precision}g}"
            if _coerce_bits(float(candidate), 32) == target:
                shortest = candidate
                break

    return _to_plain_decimal(shortest)


def _to_plain_decimal(text: str) -> str:
    """把最短表示渲染成 Go 'f' 格式：定点、不带指数、整数不带 .0。

    Decimal 从十进制字符串构造是精确的，所以展开指数不会引入误差。
    只有真带指数时才走 Decimal——那条路比字符串切片贵得多。
    """
    if "e" in text or "E" in text:
        return format(Decimal(text), "f")
    if text.endswith(".0"):
        return text[:-2]  # Go 的 'f' 对整数值不输出小数部分：1.0 -> "1"，-0.0 -> "-0"
    return text


def _coerce_bits(value: float, bit_size: int) -> float:
    """把 float 收敛到指定位宽（32 位时经 struct 走一遍 float32）。

    超出 float32 范围时 struct 会抛 OverflowError，而 C 的语义是溢出成 ±Inf。
    这里按 C 语义返回 ±Inf：接近 float32 上限的值（如 3.4028235e38）在搜索最短表示时，
    中间候选可能刚好溢出，抛异常会让整个格式化炸掉，返回 Inf 则只是让这一轮候选不匹配、
    继续加精度——那正是想要的行为。
    """
    if bit_size == 32:
        try:
            return struct.unpack("<f", struct.pack("<f", value))[0]
        except OverflowError:
            return math.inf if value > 0 else -math.inf
    return value


def serialize_field_value(message: Message, fd: FieldDescriptor) -> Any:
    """把字段序列化为可直接下发给 MySQL 的参数值。

    与 serialize_field_as_text 的唯一区别：**未设置的 Timestamp 返回 None（SQL NULL）
    而不是空串**。

    为什么必须区分：DATETIME 列没有"零值"可写——NO_ZERO_DATE 下 '0000-00-00' 非法，
    空串在 STRICT_TRANS_TABLES 下直接被拒（Error 1292 Incorrect datetime value）。
    也就是说只要消息里有一个 Timestamp 字段没赋值，整行就插不进去。
    唯一正确的表示是 NULL，而字符串表达不了 NULL，所以生成 SQL 参数一律走本函数。

    其余字段（含未设置的嵌套消息 / 空容器）仍返回空值：它们的列是 BLOB 系且 NOT NULL，
    空串能正确往返，不需要 NULL。
    """
    return value_encoder(fd)(message)


def serialize_field_as_text(message: Message, fd: FieldDescriptor) -> Any:
    """把单个字段序列化成字符串 / 裸字节。

    - Timestamp                     -> "2006-01-02 15:04:05.000000"（未设置时空串）
    - map / list / bytes / 嵌套消息  -> proto wire 格式**裸字节**（bytes）
    - 标量                          -> 十进制 / 布尔字符串

    ⚠️ 生成 SQL 参数请用 serialize_field_value：本函数表达不了 SQL NULL，
    未设置的 Timestamp 会得到空串，直接下发会被 MySQL 拒绝。

    二进制字段不做 Base64：目标列是 MEDIUMBLOB，本身二进制安全，编码只会白白多占 33%
    体积并在每次读写上加一次编解码。要在 SQL 控制台查看，用 MySQL 自带的 TO_BASE64(列)。

    ⚠️ 这些字段对应的列必须是 BLOB 系。裸字节写进 utf8mb4 的 TEXT/VARCHAR 列会因
    非法 UTF-8 被拒或损坏——本库建表时统一映射为 MEDIUMBLOB，只有手工建的表才会踩到。
    """
    return text_encoder(fd)(message)


# ── 逐字段预编译的编码闭包 ──────────────────────────────────────────────
#
# 为什么不直接写一条 if/elif 链：那样每写一行、每个字段都要重跑一遍类型判定
# （is_timestamp_field 还要比字符串全名）。实测 10 列的一行 INSERT 建参，
# 逐字段判定约 4.9µs，预编译闭包约 1.1µs——4× 差距全在这条链上。
#
# 关键是**只有一份语义**：闭包就是把下面这条链的分支结果提前定死，
# 不存在"快路径和慢路径两套实现哪天走偏了"的问题。
#
# 缓存以 FieldDescriptor 为键：同一个 pool 里描述符是单例，可哈希且生命周期与进程一致。

_TEXT_ENCODERS: dict[FieldDescriptor, Any] = {}
_VALUE_ENCODERS: dict[FieldDescriptor, Any] = {}


def text_encoder(fd: FieldDescriptor):
    """取（并缓存）该字段的文本/字节编码闭包。"""
    enc = _TEXT_ENCODERS.get(fd)
    if enc is None:
        enc = _TEXT_ENCODERS[fd] = _make_text_encoder(fd)
    return enc


def value_encoder(fd: FieldDescriptor):
    """取（并缓存）该字段的 SQL 参数编码闭包（未设置的 Timestamp -> None）。"""
    enc = _VALUE_ENCODERS.get(fd)
    if enc is None:
        enc = _VALUE_ENCODERS[fd] = _make_value_encoder(fd)
    return enc


def _make_value_encoder(fd: FieldDescriptor):
    text = text_encoder(fd)
    if not is_timestamp_field(fd):
        return text

    def encode_timestamp_or_null(message: Message):
        value = text(message)
        return None if value == "" else value

    return encode_timestamp_or_null


def _make_text_encoder(fd: FieldDescriptor):
    name = fd.name

    if is_timestamp_field(fd):
        return lambda m: _serialize_timestamp(m, fd)
    if fd.is_repeated:  # map 和 list 都走容器序列化
        return lambda m: _serialize_container(m, fd)

    ftype = fd.type
    if ftype in _INT_TYPES or ftype in _UINT_TYPES:
        return lambda m: str(getattr(m, name))
    if ftype == _T.TYPE_FLOAT:
        return lambda m: format_float(getattr(m, name), 32, name)
    if ftype == _T.TYPE_DOUBLE:
        return lambda m: format_float(getattr(m, name), 64, name)
    if ftype == _T.TYPE_STRING:
        return lambda m: getattr(m, name)
    if ftype == _T.TYPE_BOOL:
        # 必须是 "1"/"0" 而不是 "true"/"false"：目标列是 tinyint(1)，
        # 在 STRICT_TRANS_TABLES 下写 'true' 会被拒（Error 1366 Incorrect integer value）。
        # 读侧同时吃 "1"/"0" 与 "true"/"false"，所以对存量行向后兼容。
        return lambda m: "1" if getattr(m, name) else "0"
    if ftype == _T.TYPE_ENUM:
        return lambda m: str(int(getattr(m, name)))
    if ftype == _T.TYPE_BYTES:
        return lambda m: getattr(m, name)
    if ftype == _T.TYPE_MESSAGE:
        # 未设置时返回 b"" 而不是 ""：目标列是 MEDIUMBLOB，同一个字段不该因为"有没有值"
        # 就在 str 和 bytes 之间来回横跳（调用方一做 isinstance 判断就会被坑）。
        # Go 侧这里是 ""，但 Go 的 string 本来就承载裸字节，落库结果与 b"" 逐字节相同。
        return lambda m: (
            getattr(m, name).SerializeToString() if m.HasField(name) else b""
        )

    def unsupported(_m: Message):
        raise InvalidFieldKindError(f"invalid field kind: {ftype} (field: {name})")

    return unsupported


def _serialize_timestamp(message: Message, fd: FieldDescriptor) -> str:
    """把 Timestamp 字段格式化成 MySQL DATETIME 字符串（未设置或零值返回空串）。"""
    if not message.HasField(fd.name):
        return ""
    ts = getattr(message, fd.name)
    if ts.seconds == _GO_ZERO_TIME_SECONDS and ts.nanos == 0:
        return ""

    # 不用 strftime：Windows 的 strftime 对公元 1000 年之前的年份不补零（甚至报错），
    # 而 Go 的 "2006" 恒定输出 4 位。手工拼接避开平台差异。
    dt = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(
        seconds=ts.seconds,
        microseconds=ts.nanos // 1000,  # 截断到微秒，与 Go 的 Format 一致（不是四舍五入）
    )
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} "
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{dt.microsecond:06d}"
    )


def _serialize_container(message: Message, fd: FieldDescriptor) -> bytes:
    """序列化 map/list 字段：把字段放进一个同类型的空消息里，用标准 wire 格式编码。

    与 _parse_container 对称可逆。空容器返回 b""（对应 Go 的空串）。
    """
    src = getattr(message, fd.name)
    if len(src) == 0:
        return b""
    holder = message.__class__()
    getattr(holder, fd.name).MergeFrom(src)
    return holder.SerializeToString()


# ── 读回 ────────────────────────────────────────────────────────────────


def parse_from_row(message: Message, row) -> None:
    """按字段声明顺序，把一行查询结果反序列化到消息里。

    row[i] 对应消息的第 i 个字段，与 serialize_field_as_text 生成的格式对称
    （SELECT 的列顺序由 MessageTable 按同一顺序生成，两边必须同源）。
    """
    fields = message.DESCRIPTOR.fields
    count = min(len(fields), len(row))
    for i in range(count):
        set_field_from_raw(message, fields[i], row[i])


def set_field_from_raw(message: Message, fd: FieldDescriptor, raw: Any) -> None:
    """把数据库返回的单个列值写进消息字段。

    Go 侧把每列都扫进 []byte 再转 string，所以只需处理字符串。Python 的 DB-API 驱动
    按列类型返回 int / float / str / bytes / datetime / Decimal / None，
    这里先归一再走与 Go 相同的分支——比 Go 少一次"数字转字符串再转回数字"。
    """
    if is_timestamp_field(fd):
        _parse_timestamp(message, fd, raw)
        return
    if fd.is_repeated:
        _parse_container(message, fd, raw)
        return

    if raw is None or raw == "" or raw == b"":
        _set_scalar_default(message, fd)
        return

    ftype = fd.type
    name = fd.name

    if ftype == _T.TYPE_BYTES:
        setattr(message, name, _as_bytes(raw))
        return
    if ftype == _T.TYPE_MESSAGE:
        # 出错时不打 raw：里面是 proto 裸字节，直接进日志会喷控制字符
        data = _as_bytes(raw)
        try:
            getattr(message, name).ParseFromString(data)
        except Exception as exc:
            raise ValueError(
                f"unmarshal sub-message field {name}: {exc} ({len(data)} bytes)"
            ) from exc
        return
    if ftype == _T.TYPE_STRING:
        setattr(message, name, _as_text(raw))
        return

    text = _as_text(raw)

    if ftype in _INT_BITS:
        signed = ftype in _INT_TYPES
        bits = _INT_BITS[ftype]
        kind = ("int" if signed else "uint") + str(bits)
        try:
            value = int(Decimal(text)) if _looks_decimal(text) else int(text, 10)
        except (ValueError, ArithmeticError) as exc:
            raise ValueError(f"parse {kind} field {name}: {exc} (value: {text})") from exc
        _check_int_range(value, bits, signed, name, text)
        setattr(message, name, value)
        return
    if ftype in (_T.TYPE_FLOAT, _T.TYPE_DOUBLE):
        kind = "float" if ftype == _T.TYPE_FLOAT else "double"
        try:
            setattr(message, name, float(text))
        except ValueError as exc:
            raise ValueError(f"parse {kind} field {name}: {exc} (value: {text})") from exc
        return
    if ftype == _T.TYPE_BOOL:
        setattr(message, name, _parse_bool(text, name))
        return
    if ftype == _T.TYPE_ENUM:
        try:
            setattr(message, name, int(text, 10))
        except ValueError as exc:
            raise ValueError(f"parse enum field {name}: {exc} (value: {text})") from exc
        return

    raise InvalidFieldKindError(f"invalid field kind: {ftype} (field: {name})")


def _parse_timestamp(message: Message, fd: FieldDescriptor, raw: Any) -> None:
    """解析 MySQL 时间到 Timestamp 字段。

    空值必须**显式清除**字段：查询接口都是"传入实例、写回同一实例"，调用方可能复用，
    不清的话上一次查询的 Timestamp 会残留，读到 NULL 反而看见旧值。
    """
    if raw is None or raw == "" or raw == b"":
        message.ClearField(fd.name)
        return

    if isinstance(raw, _dt.datetime):
        dt = raw
    elif isinstance(raw, _dt.date):
        dt = _dt.datetime(raw.year, raw.month, raw.day)
    else:
        dt = _parse_datetime_text(_as_text(raw), fd.name)

    if dt.tzinfo is None:
        # MySQL 的 DATETIME 不带时区。Go 侧 time.Parse 无时区时按 UTC 解析，
        # 这里保持一致——按本地时区解析会让同一行数据在不同机器上读出不同的秒数。
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    delta = dt - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    total_us = delta.days * 86400 * 10**6 + delta.seconds * 10**6 + delta.microseconds
    ts = getattr(message, fd.name)
    # 用 // 和 % 而不是 divmod 手写符号处理：Python 的 // 对负数向下取整，
    # 正好给出 proto Timestamp 要求的 0 <= nanos < 1e9。
    ts.seconds = total_us // 10**6
    ts.nanos = (total_us % 10**6) * 1000


def _parse_datetime_text(text: str, field_name: str) -> _dt.datetime:
    for fmt in _TIMESTAMP_PARSE_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:  # 最后兜底：ISO 8601（Python 3.11+ 的 fromisoformat 覆盖面很宽）
        return _dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"parse timestamp field {field_name}: {exc} (value: {text})") from exc


def _parse_container(message: Message, fd: FieldDescriptor, raw: Any) -> None:
    """反序列化 map/list 字段（_serialize_container 的逆操作）。"""
    if raw is None or raw == "" or raw == b"":
        return
    data = _as_bytes(raw)
    holder = message.__class__()
    try:
        holder.ParseFromString(data)
    except Exception as exc:
        raise ValueError(f"parse field {fd.name}: {exc} ({len(data)} bytes)") from exc

    src = getattr(holder, fd.name)
    if len(src) == 0:
        return
    dst = getattr(message, fd.name)
    if is_map_field(fd):
        dst.MergeFrom(src)
        return
    del dst[:]  # 与 Go 的 Truncate(0) 一致：覆盖而不是追加
    dst.MergeFrom(src)


#: NULL / 空串列在读回时要重置成默认值的标量类型。
#: 这些都能用 setattr 赋默认值；TYPE_MESSAGE 不行，单独用 ClearField。
_RESETTABLE_SCALARS = frozenset(
    {
        _T.TYPE_INT32,
        _T.TYPE_INT64,
        _T.TYPE_UINT32,
        _T.TYPE_UINT64,
        _T.TYPE_FLOAT,
        _T.TYPE_DOUBLE,
        _T.TYPE_BOOL,
        _T.TYPE_STRING,
        # ↓ 这三类原先被漏掉，导致跨行串位。见函数注释。
        _T.TYPE_BYTES,
        _T.TYPE_ENUM,
    }
)


def _set_scalar_default(message: Message, fd: FieldDescriptor) -> None:
    """列值为 NULL / 空时，把字段重置为默认值。

    **必须覆盖所有类型，否则会跨行串位。** 原先这里只处理 8 种标量，
    刻意跳过 bytes / enum / message（"与 Go 版一致"）。后果是：

        out = golang_test(id=1); db.find_one_by_pk(out)   # payload = b"alice-data"
        out = golang_test(id=2); db.find_one_by_pk(out)   # 第 2 行 payload 是 NULL
        # → out.payload 还是 b"alice-data"，**bob 拿到了 alice 的数据**

    而 ``find_one_by_pk(out)`` 的 out 既是入参（主键）又是出参，复用同一个 message
    正是这个 API 的天然用法，所以这不是"误用"。同样的洞在 Go 侧 scanOneProtoRow 里
    一模一样，两边一起修。

    用 setattr 而不是 ClearField：对 proto3 optional 字段，Go 的 Set(fd, fd.Default())
    会把字段标记为**已设置**，ClearField 则是未设置——差一个 has 位，
    再写回数据库时 insert_set_fields 就会少一列。TYPE_MESSAGE 没有可 setattr 的
    默认值，只能 ClearField（子消息本来就没有"零值已设置"这一说）。
    """
    if fd.type in _RESETTABLE_SCALARS:
        setattr(message, fd.name, fd.default_value)
    elif fd.type == _T.TYPE_MESSAGE:
        message.ClearField(fd.name)


def _as_text(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    if isinstance(raw, bytearray):
        return bytes(raw).decode("utf-8")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        return "1" if raw else "0"
    return str(raw)


def _as_bytes(raw: Any) -> bytes:
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    if isinstance(raw, str):
        # 驱动按 utf8mb4 解码过的列。BLOB 列不会走到这里（PyMySQL 对 binary 列返回 bytes），
        # 但手工建成 TEXT 的列会——那种表本来就存不住裸字节，见 serialize 的告警。
        return raw.encode("utf-8")
    raise TypeError(f"cannot use {type(raw).__name__} as bytes")


def _looks_decimal(text: str) -> bool:
    return "." in text or "e" in text or "E" in text


def _check_int_range(value: int, bits: int, signed: bool, name: str, text: str) -> None:
    if signed:
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        kind = f"int{bits}"
    else:
        lo, hi = 0, (1 << bits) - 1
        kind = f"uint{bits}"
    if not lo <= value <= hi:
        raise ValueError(f"parse {kind} field {name}: value out of range (value: {text})")


_TRUE_LITERALS = {"1", "t", "T", "true", "TRUE", "True"}
_FALSE_LITERALS = {"0", "f", "F", "false", "FALSE", "False"}


def _parse_bool(text: str, name: str) -> bool:
    """按 Go 的 strconv.ParseBool 接受同一组字面量。

    存量行可能是旧版本写的 "true"/"false"，不能只认 "1"/"0"。
    """
    if text in _TRUE_LITERALS:
        return True
    if text in _FALSE_LITERALS:
        return False
    raise ValueError(f"parse bool field {name}: invalid syntax (value: {text})")
