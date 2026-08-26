"""极小的 protobuf wire 格式扫描器，只用于"按字段号读取已设置的扩展"。

为什么需要它？
    读 option 的首选路径是 ``options.ListFields()``——只要用户的 ``.proto`` import 了
    ``proto2mysql_option.proto``，生成的 ``_pb2`` 模块会把扩展注册进 descriptor pool，
    ListFields() 就能按字段号把扩展列出来（与 Go 版 rangeExtensions 的语义一致）。

    但有一条路径拿不到注册好的扩展：**离线工具从 FileDescriptorSet 加载描述符**时，
    扩展类型可能根本没进当前进程的 pool。这时候扩展值会落进 unknown fields，
    而 upb 实现（protobuf 5.x/6.x/7.x 的默认实现）的 ``UnknownFields()`` 直接抛
    ``NotImplementedError: unknown field accessor``——Python 侧没有等价 API 可用。

    于是退回到最底层的事实：options 消息本身可以 ``SerializeToString()``，
    裸字节里的字段号是确定的。本模块就扫这段字节。

这和 Go 版的取舍是同一个：Go 的 rangeExtensions 按**字段号**匹配而不是按扩展类型匹配，
注释里写明了原因——动态描述符与生成代码的扩展类型标识不同，但字段号一致。
"""

from __future__ import annotations

_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_BYTES = 2
_WIRE_START_GROUP = 3
_WIRE_END_GROUP = 4
_WIRE_FIXED32 = 5


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """从 pos 读一个 varint，返回 (值, 新位置)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def scan_fields(buf: bytes) -> dict[int, object]:
    """扫描一段 protobuf 裸字节，返回 {字段号: 值}。

    只解出本库 option 用得到的两种形态：varint（bool/int）与 length-delimited（string）。
    fixed32/fixed64 原样返回字节，group 直接跳过——option 里不会出现，
    但跳过逻辑必须正确，否则后面的字段号会全部错位。

    重复字段取最后一次出现的值，与 protobuf 合并语义一致。

    **任何解析不下去的字节都抛 ValueError，绝不返回"解了一半"的结果。**
    判断"要不要因此失败"是**调用方**的事：options.range_extensions 在运行期
    选择吞掉这个异常并降级（打一条 warning），因为一份坏 option 不该打断
    register_all_tables 的整轮注册。但本函数自己绝不静默——静默截断的后果是
    option 被读成空 → 表名退化成 proto full name、主键凭空消失，一声不吭。
    """
    out: dict[int, object] = {}
    pos = 0
    end = len(buf)
    depth = 0

    while pos < end:
        tag, pos = _read_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 0:
            raise ValueError("invalid field number 0")

        if wire_type == _WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
            if depth == 0:
                out[field_num] = value
        elif wire_type == _WIRE_BYTES:
            length, pos = _read_varint(buf, pos)
            if pos + length > end:
                raise ValueError("truncated length-delimited field")
            raw = buf[pos : pos + length]
            pos += length
            if depth == 0:
                out[field_num] = raw
        elif wire_type == _WIRE_FIXED64:
            # 必须显式查长度：Python 的切片越界只会**悄悄给短一截**，
            # 随后 pos += 8 直接跨过 end，while 条件不成立、循环正常结束，
            # 于是一段被截断的字节看起来解析成功了。
            if pos + 8 > end:
                raise ValueError("truncated fixed64")
            if depth == 0:
                out[field_num] = buf[pos : pos + 8]
            pos += 8
        elif wire_type == _WIRE_FIXED32:
            if pos + 4 > end:
                raise ValueError("truncated fixed32")
            if depth == 0:
                out[field_num] = buf[pos : pos + 4]
            pos += 4
        elif wire_type == _WIRE_START_GROUP:
            depth += 1
        elif wire_type == _WIRE_END_GROUP:
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced group")
        else:
            raise ValueError(f"unknown wire type {wire_type}")

    if depth != 0:
        # 没闭合的 group 意味着后面所有字段都被当成"组内"丢掉了，
        # 而丢掉是静默的——返回的 dict 看起来只是"这些 option 没设置"。
        raise ValueError("unclosed group")

    return out
