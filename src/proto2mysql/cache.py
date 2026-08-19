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

from typing import Protocol, runtime_checkable

from .errors import CacheMissError


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
