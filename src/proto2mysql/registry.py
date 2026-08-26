"""枚举"本进程已加载的全部 .proto 描述符"，用于自动注册所有建表消息。

对应 Go 的 ``protoregistry.GlobalFiles.RangeFiles``。**这是整个移植里唯一没有直接等价物
的机制**，值得把差异写清楚：

Go 侧
    每个生成的 ``.pb.go`` 在 ``init()`` 里把自己的 FileDescriptor 注册进全局表
    ``protoregistry.GlobalFiles``，而这个表有公开的 ``RangeFiles`` 可以遍历。
    只要包被链接进二进制（哪怕只是 ``import _``），描述符就在表里。

Python 侧
    生成的 ``_pb2.py`` 在 import 时同样会把文件加进
    ``descriptor_pool.Default()``，但 **DescriptorPool 没有"列出全部文件"的公开 API**
    ——只有 ``FindFileByName`` / ``FindMessageTypeByName`` 这类按名字查的方法
    （upb 实现下私有属性也拿不到）。

    所以这里换一条同样可靠的路：扫 ``sys.modules``，凡是模块级 ``DESCRIPTOR`` 是
    ``FileDescriptor`` 的就算数。可靠性来自一个事实——**要用某个消息类，就必须 import
    它所在的 _pb2 模块**，而 import 过的模块必然在 ``sys.modules`` 里。
    这与 Go 侧"包必须被链接进来"是同一个前提，不是更弱的假设。

    真正的差别只有一个：Go 里 ``import _ "xxx/pb"`` 这种"只为副作用的空导入"很常见，
    Python 里对应的写法是 ``import xxx_pb2  # noqa: F401``，容易被 linter 删掉。
    所以 :func:`iter_file_descriptors` 额外接受 ``modules`` 参数，
    让调用方可以显式点名，不依赖导入副作用。
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Iterable, Iterator

from google.protobuf.descriptor import Descriptor, FileDescriptor


def _module_descriptor(mod: object):
    """取模块的 ``DESCRIPTOR``——**不能用 getattr**。

    PEP 562 允许模块定义 ``__getattr__``，很多库拿它做惰性导入。
    ``getattr(mod, "DESCRIPTOR", None)`` 只吞 AttributeError：那个钩子抛别的
    （可选依赖没装时的 ImportError 是最常见的形态）就会直接穿透，把整轮遍历打断——
    而 register_all_tables 在启动路径上，肇事模块跟 proto 毫无关系。

    生成的 ``_pb2.py`` 里 DESCRIPTOR 一定是真正的模块全局，走 ``__dict__``
    一个都不会漏。调用方显式点名传进来的可以不是真 module（测试替身 / 惰性代理），
    那条路保留 getattr 语义，但同样不让它掀翻遍历。
    """
    if isinstance(mod, ModuleType):
        return mod.__dict__.get("DESCRIPTOR")
    try:
        return getattr(mod, "DESCRIPTOR", None)
    except Exception:  # noqa: BLE001 - 代理对象的 __getattr__ 可能抛任何东西
        return None


def iter_file_descriptors(
    modules: Iterable[ModuleType] | None = None,
) -> Iterator[FileDescriptor]:
    """遍历本进程可见的 FileDescriptor（同一文件只产出一次）。

    :param modules: 显式给定的模块列表（通常是若干 ``xxx_pb2``）。
        为 None 时扫描 ``sys.modules`` 全量。
    """
    seen: set[str] = set()
    source = list(modules) if modules is not None else list(sys.modules.values())
    for mod in source:
        descriptor = _module_descriptor(mod)
        if not isinstance(descriptor, FileDescriptor):
            continue
        if descriptor.name in seen:
            continue
        seen.add(descriptor.name)
        yield descriptor


def iter_messages(descriptor: FileDescriptor | Descriptor) -> Iterator[Descriptor]:
    """递归遍历文件（或消息）里的全部消息描述符，含嵌套消息。

    与 Go 的 registerTablesInMessages 一样递归：嵌套 message 也可以是一张表。
    """
    container = descriptor.message_types_by_name.values() if isinstance(
        descriptor, FileDescriptor
    ) else descriptor.nested_types
    for md in container:
        yield md
        yield from iter_messages(md)
