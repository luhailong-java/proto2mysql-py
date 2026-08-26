"""跨语言对拍：一键跑完整条链。

    python tools/parity_run.py --go-repo ../proto2mysql

做三件事：

1. 在 Go 仓库里跑 ``PARITY_OUT=... go test -run TestEmitParityCorpus``
2. 在本仓库跑 ``tools/parity_emit.py``
3. 用 ``tools/parity_diff.py`` 逐字节比对

退出码 0 = 完全一致，可直接接进 CI。

为什么需要它：「Go 版与 Python 版产出逐字节相同的 SQL」是本库的核心契约，
但在此之前**全靠人手把 Go 的字符串抄进 Python 的测试文件**——抄漏一条、
抄错一个反引号，谁也不会知道。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], cwd: Path, env_extra: dict | None = None) -> None:
    import os

    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    if env_extra:
        env.update(env_extra)
    print(f"$ (cd {cwd}) {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(cwd), env=env)
    if proc.returncode != 0:
        raise SystemExit(f"命令失败（退出码 {proc.returncode}）: {' '.join(cmd)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跑跨语言 SQL 对拍")
    ap.add_argument("--go-repo", default="../proto2mysql", help="Go 仓库路径")
    ap.add_argument("--keep", action="store_true", help="保留中间产物便于排查")
    args = ap.parse_args(argv)

    go_repo = Path(args.go_repo).resolve()
    if not (go_repo / "go.mod").exists():
        raise SystemExit(f"{go_repo} 看起来不是 Go 仓库（没有 go.mod）")

    tmp = Path(tempfile.mkdtemp(prefix="proto2mysql-parity-"))
    go_json = tmp / "parity.go.json"
    py_json = tmp / "parity.py.json"

    run(["go", "test", "-count=1", "-run", "TestEmitParityCorpus", "."],
        cwd=go_repo, env_extra={"PARITY_OUT": str(go_json)})
    run([sys.executable, str(ROOT / "tools" / "parity_emit.py"), "-o", str(py_json)], cwd=ROOT)

    print()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "parity_diff.py"), str(go_json), str(py_json)],
        cwd=str(ROOT),
    )
    if args.keep or proc.returncode != 0:
        print(f"\n中间产物保留在: {tmp}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
