"""现场把 ``tests/proto/*.proto`` 生成成 ``_pb2`` 模块。

不把生成产物提交进仓库，理由和 Go 版一样：生成器版本一变，产物就和源 proto 漂移，
而漂移是静默的——测试照跑，只是跑的不是当前 proto。每次现场生成就没有这个问题。

**单独成一个模块**是为了让 pytest 之外的入口也能用同一份逻辑：
``tools/parity_emit.py`` 是刻意可以脱离 pytest 直接跑的（parity_run.py 就这么跑，
docs/testing.md 的分步命令也是），而 ``tests/gen`` 被 .gitignore 掉了——
干净检出上第一条命令就是 ``ModuleNotFoundError: testpb_pb2``，整条对拍链起不来。
两边各写一套 protoc 参数迟早会漂，所以只有这一份。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).parent
_PROTO_DIR = _TESTS_DIR / "proto"
_GEN_DIR = _TESTS_DIR / "gen"
_OPTION_PROTO_DIR = _TESTS_DIR.parent / "src" / "proto2mysql" / "proto"


def generate_stubs() -> None:
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



def ensure_stubs() -> None:
    """缺了才生成，并把 ``tests/gen`` 挂上 sys.path。

    与 pytest 那条"每次都生成"不同：这条用于 pytest 之外的入口，
    不该在一次 pytest 会话里再叠一遍 protoc 的开销。``tests/gen`` 只会由
    generate_stubs() 产生，所以"存在即当前"这个假设成立。
    """
    if not (_GEN_DIR / "testpb_pb2.py").exists():
        generate_stubs()
    if str(_GEN_DIR) not in sys.path:
        sys.path.insert(0, str(_GEN_DIR))
