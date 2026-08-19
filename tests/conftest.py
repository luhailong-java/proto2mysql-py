"""测试夹具：现场把 tests/proto/*.proto 生成成 _pb2 模块并挂上 sys.path。

不把生成产物提交进仓库，理由和 Go 版一样：生成器版本一变，产物就和源 proto 漂移，
而漂移是静默的——测试照跑，只是跑的不是当前 proto。每次现场生成就没有这个问题。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
_PROTO_DIR = _TESTS_DIR / "proto"
_GEN_DIR = _TESTS_DIR / "gen"
_OPTION_PROTO_DIR = _TESTS_DIR.parent / "src" / "proto2mysql" / "proto"


def _generate_stubs() -> None:
    _GEN_DIR.mkdir(exist_ok=True)
    (_GEN_DIR / "__init__.py").unlink(missing_ok=True)  # gen 是普通目录，不做包
    protos = sorted(p.name for p in _PROTO_DIR.glob("*.proto"))
    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{_PROTO_DIR}",
        # option 定义随包分发，用户工程也是这么 -I 的（见 proto2mysql.proto_include_path()）
        f"-I{_OPTION_PROTO_DIR}",
        f"--python_out={_GEN_DIR}",
        *protos,
        # option 本身也要生成：用户 proto 里 import 了它，生成的 _pb2 就会
        # `import proto2mysql_option_pb2`，缺了直接 ModuleNotFoundError。
        # 本库运行期不 import 它（按字段号读扩展），但 protoc 生成的代码会。
        "proto2mysql_option.proto",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"protoc failed:\n{result.stdout}\n{result.stderr}")


def pytest_configure(config) -> None:  # noqa: ARG001 - pytest 钩子签名
    _generate_stubs()
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
