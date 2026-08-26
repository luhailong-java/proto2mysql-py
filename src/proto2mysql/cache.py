"""cache-aside 缓存抽象。

对应 Go 版的 cache.go。库不直接依赖任何 Redis 客户端，由业务方注入实现，保持弱依赖：
**任何缓存错误都不会影响数据库操作，只会降级为直读 DB**。

redis-py 适配示例::

    import redis
    from proto2mysql import Cache, CacheMissError

    class RedisCache(Cache):
        def __init__(self, client): self.c = client
        def get(self, key):
            v = self.c.get(key)
            if v is None:
                raise CacheMissError(key)
            return v
        def set(self, key, value, ttl):
            self.c.set(key, value, ex=int(ttl) if ttl and ttl > 0 else None)
        def delete(self, *keys):
            if keys:
                self.c.delete(*keys)
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .errors import CacheMissError

# ── 缓存条目的信封 ──────────────────────────────────────────────────────
#
# 存进缓存的**不是**裸 pb 字节，而是「写入方认识哪些字段」+ pb 字节。
#
# 为什么必须记这个：缓存里的 message 是**按列从 MySQL 读出来再填进去的**，
# 不是从 pb 字节 parse 的——所以 protobuf 的 unknown-fields 保留机制在这里
# 完全不适用。旧版本进程的 message 里根本没有新字段，它写进缓存的是一条
# **残缺**记录。滚动发布时 v1 写、v2 读，v2 就拿到新字段的零值，而 MySQL 里
# 是有值的；v2 写完只 del key，v1 下次读又投毒一次，表现为**读到的值随机闪烁**。
# 最阴的是 MySQL 里的数据自始至终是对的，零日志零异常，只能靠对账发现。
#
# 判定规则是**超集**：写入方的字段集 ⊇ 读取方的字段集时才采用。
#   * v1 读 v2 写的条目 → v2 认识得更多，采用（多出来的字段对 v1 就是 unknown fields）
#   * v2 读 v1 写的条目 → v1 认识得更少，**当未命中回源**
# 所以 miss 是单向的，滚动发布期间的雪崩面比"换 key"小得多。
#
# 刻意**不把版本拼进 key**：失效路径与读路径共用 key 生成器，指纹进 key 会让
# v1 的写永远失效不掉 v2 的条目，把"残缺数据"升级成"跨版本永久脏读"。
#
# 布局（两语言逐字节一致，可共用同一个 Redis）：
#     magic   4B   b"P2MC"
#     version 1B   0x01
#     count   varint          字段号个数
#     fields  count 个 varint  字段号，**升序**
#     payload 剩余全部          pb 序列化字节
#
# 老条目（裸 pb 字节，没有 magic）读到就当未命中——安全，且会被下一次写覆盖。
CACHE_ENTRY_MAGIC = b"P2MC"
CACHE_ENTRY_VERSION = 1


def _put_varint(out: bytearray, value: int) -> None:
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def _get_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("varint 截断")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def encode_entry(field_numbers: Sequence[int], payload: bytes) -> bytes:
    """把「写入方认识的字段号集合」和 pb 字节打包成一条缓存条目。"""
    out = bytearray(CACHE_ENTRY_MAGIC)
    out.append(CACHE_ENTRY_VERSION)
    nums = sorted(field_numbers)
    _put_varint(out, len(nums))
    for num in nums:
        _put_varint(out, num)
    out += payload
    return bytes(out)


def decode_entry(data: bytes) -> tuple[frozenset[int], bytes] | None:
    """拆包；不是本库写的条目（无 magic / 版本不认 / 截断）一律返回 None。"""
    if not data.startswith(CACHE_ENTRY_MAGIC):
        return None
    pos = len(CACHE_ENTRY_MAGIC)
    if pos >= len(data) or data[pos] != CACHE_ENTRY_VERSION:
        return None
    pos += 1
    try:
        count, pos = _get_varint(data, pos)
        nums = []
        for _ in range(count):
            num, pos = _get_varint(data, pos)
            nums.append(num)
    except ValueError:
        return None
    return frozenset(nums), data[pos:]


@runtime_checkable
class Cache(Protocol):
    """缓存接口。未命中时 :meth:`get` **必须**抛 :class:`CacheMissError`。"""

    def get(self, key: str) -> bytes:
        """返回缓存值；未命中必须抛 CacheMissError。"""
        ...

    def set(self, key: str, value: bytes, ttl: float | None) -> None:
        """写入缓存，ttl 为 None 或 <=0 表示不过期。"""
        ...

    def delete(self, *keys: str) -> None:
        """删除一个或多个 key。"""
        ...


class DictCache:
    """进程内字典缓存，只用于测试和单进程小工具。

    **不要在多副本服务里用**：它不跨进程，会让不同副本读到互相看不见的旧值。
    不实现 TTL 过期（ttl 参数收下但忽略）——测试里需要过期语义请自己删。
    """

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def get(self, key: str) -> bytes:
        try:
            return self._data[key]
        except KeyError:
            raise CacheMissError(key) from None

    def set(self, key: str, value: bytes, ttl: float | None = None) -> None:  # noqa: ARG002
        self._data[key] = value

    def delete(self, *keys: str) -> None:
        for key in keys:
            self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
