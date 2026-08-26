"""测试夹具：把 tests/proto/*.proto 现场生成成 _pb2 模块并挂上 sys.path。

生成逻辑本身在 tests/_gen_stubs.py —— 那份被 tools/parity_emit.py 共用
（对拍链刻意可以脱离 pytest 跑，而 tests/gen 是 .gitignore 掉的）。
两边各写一套 protoc 参数迟早会漂，所以只有一份。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _gen_stubs import _GEN_DIR, generate_stubs  # noqa: E402


def pytest_configure(config) -> None:  # noqa: ARG001 - pytest 钩子签名
    generate_stubs()
    if str(_GEN_DIR) not in sys.path:
        sys.path.insert(0, str(_GEN_DIR))


@pytest.fixture(scope="session")
def testpb():
    import testpb_pb2

    return testpb_pb2


@pytest.fixture(scope="session")
def accountpb():
    import account_pb2

    return account_pb2


@pytest.fixture(scope="session")
def kitchenpb():
    """覆盖全部字段类型的样本表（timestamp / optional / repeated / map / bytes / 浮点）。"""
    import kitchen_pb2

    return kitchen_pb2
