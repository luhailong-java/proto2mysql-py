"""proto2sql —— 离线从 .proto 源文件生成 MySQL 建表 SQL，不连库、不需要生成 Python 代码。

对应 Go 仓库的 tools/proto2sql。分工也一样：运行时库吃编译好的消息类、连库执行；
本工具吃 .proto 源文件、产出 .sql 文件，**类型映射规则只有一份**（复用 MessageTable），不会漂移。

与 Go 版的实现差异：Go 用 protocompile 在进程内编译 .proto；Python 没有等价的纯 Python
编译器，改为调 protoc 产出 FileDescriptorSet 再加载。protoc 优先用 ``grpcio-tools``
自带的那个（``pip install grpcio-tools`` 即可，不必系统装 protoc），找不到再退回 PATH 上的
``protoc``。

**这条路径上扩展是没注册的**：描述符来自独立的 DescriptorPool，proto2mysql 的 option
不在其中，option 值会落进 unknown fields。所以 options 模块必须能按字段号扫裸字节
（见 ``proto2mysql._wire``）——这不是绕路，是这条链路唯一可用的读法。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from google.protobuf import descriptor_pb2, descriptor_pool

from ..options import table_name_from_descriptor
from ..options import file_has_db_option as _file_has_db_option
from ..table import MessageTable, escape_mysql_name


@dataclass
class Table:
    """单张表的生成结果。"""

    name: str  # 表名（来自 table_name 选项）
    sql: str  # 建表 SQL（含末尾分号；drop 开启时含前置 DROP 语句）


def _force_utf8_stdio() -> None:
    """让 Windows 旧代码页也能打印中文帮助和生成结果。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _protoc_argv() -> list[str]:
    """优先用 grpcio-tools 自带的 protoc，退回 PATH 上的 protoc。"""
    try:
        import grpc_tools.protoc  # noqa: F401

        return [sys.executable, "-m", "grpc_tools.protoc"]
    except ImportError:
        pass

    exe = shutil.which("protoc")
    if exe is None:
        # grpcio-tools 是基础依赖；还能走到这里说明安装被裁剪/损坏，或者有人直接
        # 从源码目录运行但没装项目依赖。仍给可操作的修复提示，别让 subprocess
        # 抛一个裸的 FileNotFoundError。
        raise RuntimeError(
            "proto2sql 需要 protoc。装法二选一：\n"
            '  pip install --upgrade proto2mysql grpcio-tools\n'
            "  或把系统的 protoc 放进 PATH"
        )
    return [exe]


def _resolve_inputs(
    proto_files: Sequence[str], import_paths: Sequence[str]
) -> tuple[list[str], list[str]]:
    """算出传给 protoc 的 ``-I`` 列表与文件名列表，并校验**每个输入都指向它自己**。

    protoc 只吃"相对某个 -I 的文件名"，所以这里把每个输入拆成「父目录进 -I、
    basename 进参数」。问题是 basename 会碰撞：``a/user.proto`` 与 ``b/user.proto``
    都变成 ``user.proto``，protoc 按 -I 顺序解析、两次都拿到 ``a/user.proto``——
    第二个文件**根本没被编译**，而整条命令返回 0，产出的 schema 里就少了那几张表。
    没有任何提示，只有下次建表时报 Unknown table。

    同理，某个 ``-I`` 目录里恰好有个同名文件也会把输入遮蔽掉。两种都在这里 fail-closed。
    """
    paths: list[str] = []
    for p in import_paths:
        if p not in paths:
            paths.append(p)
    names: list[str] = []
    for pf in proto_files:
        path = Path(pf)
        parent = str(path.parent) or "."
        if parent not in paths:
            paths.append(parent)
        names.append(path.name)

    for pf, name in zip(proto_files, names):
        given = Path(pf)
        hit = next((Path(d) / name for d in paths if (Path(d) / name).is_file()), None)
        if hit is None:
            raise RuntimeError(f"找不到输入文件: {pf}")
        if not given.is_file():
            # protoc 的标准写法：文件名是**相对某个 -I 目录**的，它本身不是一条
            # 可用路径（`proto2sql -I proto account.proto`）。这种情况下 hit 就是
            # 它自己，不算遮蔽——早先在这里直接拒掉，把最常见的调用形式给挡了。
            continue
        if hit.resolve() != given.resolve():
            raise RuntimeError(
                f"输入 {pf} 会被 {hit} 遮蔽——protoc 只按文件名 {name} 解析，"
                f"两个同名文件里只有靠前的那个会被编译，另一个静默漏掉。\n"
                f"  请把它们改成不同的文件名，或者分两次调用。"
            )
    return paths, names


def build_descriptor_set(
    proto_files: Sequence[str], import_paths: Sequence[str] = ()
) -> descriptor_pb2.FileDescriptorSet:
    """调 protoc 把 .proto 编译成 FileDescriptorSet。

    每个输入文件所在目录会自动加入 import 搜索路径（与 Go 版 resolveInputs 一致）。
    """
    paths, names = _resolve_inputs(proto_files, import_paths)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "descriptor.pb"
        cmd = [
            *_protoc_argv(),
            *(f"-I{p}" for p in paths),
            f"--descriptor_set_out={out}",
            "--include_imports",  # 缺了它，被 import 的文件不在集合里，加载会失败
            "--include_source_info",
            *names,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"protoc failed:\n{result.stdout}\n{result.stderr}")
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(out.read_bytes())
        return fds


def _load_pool(fds: descriptor_pb2.FileDescriptorSet) -> tuple[descriptor_pool.DescriptorPool, list[str]]:
    """把 FileDescriptorSet 装进**独立的** pool，返回 (pool, 输入文件名列表)。

    用独立 pool 而不是默认 pool：本工具可能被当库调用（比如在构建脚本里连着跑几批
    proto），往默认 pool 里塞会和进程里已 import 的 _pb2 撞出
    "duplicate file name" 或 "extension already registered"。

    protoc 的输出已按依赖拓扑排序，依次 Add 即可。
    """
    pool = descriptor_pool.DescriptorPool()
    names: list[str] = []
    for fd_proto in fds.file:
        pool.Add(fd_proto)
        names.append(fd_proto.name)
    return pool, names


def generate(
    proto_files: Sequence[str],
    import_paths: Sequence[str] = (),
    *,
    drop: bool = False,
    require_db_option: bool = False,
) -> list[Table]:
    """编译 proto_files，为每个带 ``table_name`` 选项的 message 生成建表 SQL。

    结果按表名排序（输出稳定，生成的 schema.sql 进版本库不会有假 diff）。

    :param drop: 每条 CREATE TABLE 前加 ``DROP TABLE IF EXISTS``。
        ⚠️ **破坏性**：DROP 会删掉整张表及全部数据。仅用于空库初始化 / 测试库重建，
        切勿用于生产库或服务启动流程。要在保留数据的前提下演进结构，用运行时库的
        ``DB.sync_all_tables`` / ``DB.generate_migration_sql``（走 ALTER，不删数据）。
    :param require_db_option: 只处理声明了文件级 ``option (proto2mysql.db) = true;``
        的文件（与运行时 ``register_all_tables`` 的筛选规则一致）。
    """
    fds = build_descriptor_set(proto_files, import_paths)
    pool, names = _load_pool(fds)

    # 只处理用户显式传入的文件，不处理被 import 进来的依赖
    wanted = {Path(pf).name for pf in proto_files}
    tables: list[Table] = []
    seen: set[str] = set()

    for name in names:
        if Path(name).name not in wanted:
            continue
        file_descriptor = pool.FindFileByName(name)
        if require_db_option and not _file_has_db_option(file_descriptor):
            continue
        _collect_tables(file_descriptor.message_types_by_name.values(), drop, seen, tables)

    tables.sort(key=lambda t: t.name)
    return tables


def _collect_tables(msgs: Iterable, drop: bool, seen: set[str], out: list[Table]) -> None:
    """遍历消息（含嵌套消息），把带表选项的消息生成建表 SQL 追加到 out。"""
    for md in msgs:
        name, ok = table_name_from_descriptor(md)
        if ok and name not in seen:
            seen.add(name)
            out.append(Table(name=name, sql=_build_table_sql(md, name, drop)))
        _collect_tables(md.nested_types, drop, seen, out)


#: --drop 生成物的文件头警告。
#:
#: 光在 CLI 帮助和函数文档里写警告是不够的——真正的风险是**生成出来的 .sql 文件被别人
#: 拿去执行**：它看起来就是一份普通的建表脚本，``mysql < schema.sql`` 一敲，整库蒸发。
#: 所以警告必须**跟着文件走**，谁打开都能第一眼看见。
DROP_MODE_BANNER = """\
-- ############################################################################
-- ##  危险：本文件由 proto2sql --drop 生成，每张表前都有 DROP TABLE IF EXISTS
-- ##
-- ##  执行它会删掉这些表及其全部数据，且不可恢复。
-- ##  仅用于空库初始化 / 测试库重建，切勿用于生产库或服务启动流程。
-- ##
-- ##  要在保留数据的前提下演进结构，请改用运行时库：
-- ##      DB.generate_migration_sql()   只产出 ALTER，不删数据（推荐：交人工/CI 审核）
-- ##      DB.sync_all_tables()          直接执行 ALTER
-- ############################################################################
"""


def _build_table_sql(md, table_name: str, drop: bool) -> str:
    sql = MessageTable.from_descriptor(md).get_create_table_sql()
    if drop:
        return f"DROP TABLE IF EXISTS {escape_mysql_name(table_name)};\n{sql}"
    return sql


#: 不能出现在文件名里的字符。按 Windows 的非法字符集来（它是各平台的超集）。
_FILENAME_UNSAFE = set(r'/\:*?"<>|') | {chr(c) for c in range(32)}


def _out_path(out_dir: Path, table_name: str) -> Path:
    """把表名落成 out_dir 下的文件名；越界或不能当文件名就抛 ValueError。

    表名来自 proto 的 ``table_name`` 选项，是**输入数据**而不是常量：带上
    ``../`` 或盘符就能把 .sql 写到 --out-dir 之外，而 ``Path.__truediv__``
    两种都不拦（``out_dir / "../../x.sql"`` 老老实实往上走）。
    """
    if not table_name or set(table_name) & _FILENAME_UNSAFE or not table_name.strip("."):
        raise ValueError(f"表名 {table_name!r} 不能安全地当文件名；请改用 -o 合并输出")
    path = (out_dir / f"{table_name}.sql").resolve()
    # 真正的闸是"归一化之后父目录相等"：光靠字符白名单挡不住符号链接一类的花样
    if path.parent != out_dir.resolve():
        raise ValueError(f"表名 {table_name!r} 会把文件写到 --out-dir 之外：{path}")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    # 必须在 ArgumentParser 之前；--help 本身就含中文，晚一行都会先编码崩溃。
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="proto2sql",
        description="从 .proto 生成 MySQL 建表 SQL（不连库）",
    )
    parser.add_argument("proto_files", nargs="+", metavar="FILE", help="要编译的 .proto 文件")
    parser.add_argument(
        "-I", "--include", action="append", default=[], metavar="DIR",
        help="import 搜索目录（可重复）",
    )
    # 二选一必须交给 argparse 管：早先只在 help 里写了"与 -o 二选一"，
    # 真的两个都给时 --out-dir 静默胜出、-o 指定的文件一个字都不会写，
    # 而退出码是 0——脚本里看不出任何异常。
    out = parser.add_mutually_exclusive_group()
    out.add_argument(
        "-o", "--output", metavar="FILE",
        help="所有表合并写入该文件；不给则打到标准输出",
    )
    out.add_argument(
        "--out-dir", metavar="DIR",
        help="每张表一个 <表名>.sql，写到该目录（与 -o 二选一）",
    )
    parser.add_argument(
        "--drop", action="store_true",
        help="⚠️ 破坏性：每条 CREATE TABLE 前加 DROP TABLE IF EXISTS（会删表及全部数据，"
             "仅用于空库/测试初始化）",
    )
    parser.add_argument(
        "--require-db-option", action="store_true",
        help="只处理声明了 option (proto2mysql.db) = true; 的文件",
    )
    args = parser.parse_args(argv)

    try:
        tables = generate(
            args.proto_files,
            args.include,
            drop=args.drop,
            require_db_option=args.require_db_option,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not tables:
        print("warning: 未发现带 table_name 选项的表消息，未生成任何内容", file=sys.stderr)
        return 0

    # --drop 的警告必须**跟着生成物走**：光在 CLI 帮助里写没用，
    # 真正的风险是这份 .sql 被别人拿去 `mysql < schema.sql`。
    banner = DROP_MODE_BANNER if args.drop else ""

    if args.out_dir:
        out_dir = Path(args.out_dir)
        try:  # 先把全部路径算完再落盘：宁可一张都不写，也不写一半到目录外
            planned = [(tbl, _out_path(out_dir, tbl.name)) for tbl in tables]
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        out_dir.mkdir(parents=True, exist_ok=True)
        for tbl, path in planned:
            path.write_text(banner + tbl.sql + "\n", encoding="utf-8", newline="\n")
            print(f"生成表 {tbl.name} -> {path}")
        return 0

    body = banner + "".join(f"{t.sql}\n\n" for t in tables)
    if args.output:
        Path(args.output).write_text(body, encoding="utf-8", newline="\n")
        print(f"生成 {len(tables)} 张表 -> {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
