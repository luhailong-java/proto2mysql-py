"""离线工具 proto2sql：吃 .proto 源文件、产出建表 SQL，不连库、不需要先生成 _pb2。

这条链路是**唯一**会遇到"扩展未注册"的路径（描述符来自独立 DescriptorPool），
所以它同时也是 options 兜底读法的端到端验证。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from proto2mysql.tools import proto2sql

TESTS = Path(__file__).parent
PROTO_DIR = TESTS / "proto"
OPTION_DIR = TESTS.parent / "src" / "proto2mysql" / "proto"


def generate(*files, **kwargs):
    return proto2sql.generate(
        [str(PROTO_DIR / f) for f in files], [str(OPTION_DIR)], **kwargs
    )


def test_generate_account():
    tables = generate("account.proto")
    assert [t.name for t in tables] == ["account"]

    sql = tables[0].sql
    for fragment in (
        "CREATE TABLE IF NOT EXISTS `account`",
        "`id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT 'pb:1'",
        "`email` MEDIUMTEXT COMMENT 'pb:3'",
        "PRIMARY KEY (`id`)",
        "INDEX `idx_account_0` (`name`(191))",
        "UNIQUE KEY `uk_account` (`email`(191))",
    ):
        assert fragment in sql, f"缺少 {fragment!r}\n{sql}"


def test_offline_matches_runtime(accountpb):
    """离线工具与运行时库必须产出**同一份** DDL——两条路径共用 MessageTable，不该有第二套规则。"""
    from proto2mysql import generate_create_table_sql

    assert generate("account.proto")[0].sql == generate_create_table_sql(accountpb.account)


def test_message_without_table_name_skipped():
    """account.proto 里的 plain 消息没声明 table_name，不该建表。"""
    assert [t.name for t in generate("account.proto")] == ["account"]


def test_require_db_option_filters_files(tmp_path):
    """没有文件级 db 选项的文件，在 --require-db-option 下整体跳过。"""
    src = tmp_path / "nodb.proto"
    src.write_text(
        'syntax = "proto3";\n'
        'import "proto2mysql_option.proto";\n'
        "message solo {\n"
        '  option (proto2mysql.table_name) = "solo";\n'
        "  int64 id = 1;\n"
        "}\n",
        encoding="utf-8",
    )
    files = [str(src)]
    assert [t.name for t in proto2sql.generate(files, [str(OPTION_DIR)])] == ["solo"]
    assert proto2sql.generate(files, [str(OPTION_DIR)], require_db_option=True) == []


def test_drop_prefix():
    tables = generate("account.proto", drop=True)
    assert tables[0].sql.startswith("DROP TABLE IF EXISTS `account`;\nCREATE TABLE")


def test_sorted_output_is_stable():
    """按表名排序：schema.sql 进版本库不能每次生成都有假 diff。"""
    names = [t.name for t in generate("kitchen.proto")]
    assert names == sorted(names)
    # 不写死全集：kitchen.proto 会随测试增长，这里只钉"排序稳定 + 该有的都在"
    assert set(names) >= {"combo_key", "keyword_col", "kitchen_sink", "rename_demo"}
    assert len(names) == len(set(names)), "同名表必须按 seen 去重"


def test_nested_messages_are_scanned(tmp_path):
    """嵌套 message 也能是一张表（与运行时 register_all_tables 的递归一致）。"""
    src = tmp_path / "nest.proto"
    src.write_text(
        'syntax = "proto3";\n'
        'import "proto2mysql_option.proto";\n'
        "message outer {\n"
        "  int64 x = 1;\n"
        "  message inner {\n"
        '    option (proto2mysql.table_name) = "inner_tbl";\n'
        "    int64 id = 1;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    assert [t.name for t in proto2sql.generate([str(src)], [str(OPTION_DIR)])] == ["inner_tbl"]


def test_cli_writes_single_file(tmp_path, capsys):
    out = tmp_path / "schema.sql"
    rc = proto2sql.main(
        [str(PROTO_DIR / "account.proto"), "-I", str(OPTION_DIR), "-o", str(out)]
    )
    assert rc == 0
    assert "CREATE TABLE IF NOT EXISTS `account`" in out.read_text(encoding="utf-8")


def test_cli_writes_per_table_files(tmp_path):
    rc = proto2sql.main(
        [
            str(PROTO_DIR / "kitchen.proto"),
            "-I",
            str(OPTION_DIR),
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    written = {p.name for p in tmp_path.glob("*.sql")}
    assert written >= {
        "kitchen_sink.sql",
        "combo_key.sql",
        "keyword_col.sql",
        "rename_demo.sql",  # rename_before/rename_after 同名表，按 seen 去重成一张
    }


def test_protoc_failure_surfaces(tmp_path):
    bad = tmp_path / "bad.proto"
    bad.write_text("syntax = \"proto3\"; message {", encoding="utf-8")
    with pytest.raises(RuntimeError, match="protoc failed"):
        proto2sql.generate([str(bad)], [str(OPTION_DIR)])


# ── --drop 的警告必须跟着生成物走（P2-2） ────────────────────────────────


def test_drop_banner_is_written_into_generated_sql(tmp_path):
    """光在 CLI 帮助和文档里写警告是不够的。

    真正的风险是**生成出来的 .sql 文件被别人拿去执行**：它看起来就是一份普通的
    建表脚本，`mysql < schema.sql` 一敲，整库蒸发。所以警告必须跟着文件走。
    """
    from proto2mysql.tools.proto2sql import DROP_MODE_BANNER, main

    out = tmp_path / "schema.sql"
    rc = main([str(PROTO_DIR / "account.proto"), "-I", str(PROTO_DIR), "-I", str(OPTION_DIR),
               "-o", str(out), "--drop"])
    assert rc == 0

    body = out.read_text(encoding="utf-8")
    assert body.startswith(DROP_MODE_BANNER), "危险警告必须在文件最开头"
    assert "DROP TABLE IF EXISTS" in body
    assert "切勿" in DROP_MODE_BANNER


def test_no_banner_without_drop(tmp_path):
    """不开 --drop 时不该有这段噪音。"""
    from proto2mysql.tools.proto2sql import main

    out = tmp_path / "schema.sql"
    assert main([str(PROTO_DIR / "account.proto"), "-I", str(PROTO_DIR), "-I", str(OPTION_DIR),
                 "-o", str(out)]) == 0

    body = out.read_text(encoding="utf-8")
    assert "DROP TABLE" not in body
    assert "危险" not in body


# ── 落盘安全与输入解析 ───────────────────────────────────────────────────


def test_out_path_refuses_to_escape_out_dir(tmp_path):
    """表名来自 proto 选项，是**输入数据**不是常量。

    带 `../` 或盘符就能把 .sql 写到 --out-dir 之外，而 `Path.__truediv__`
    两种都不拦——`out_dir / "../../x.sql"` 会老老实实往上走。
    """
    from proto2mysql.tools.proto2sql import _out_path

    assert _out_path(tmp_path, "account") == (tmp_path / "account.sql").resolve()
    for bad in ("../evil", "a/b", "C:evil", "..", ""):
        with pytest.raises(ValueError):
            _out_path(tmp_path, bad)


def test_cli_refuses_table_name_that_would_escape_out_dir(tmp_path):
    src = tmp_path / "escape.proto"
    src.write_text(
        '''syntax = "proto3";
import "proto2mysql_option.proto";
message escape_probe {
  option (proto2mysql.table_name) = "../evil";
  uint64 id = 1;
}
''',
        encoding="utf-8",
    )
    out_dir = tmp_path / "sql"

    rc = proto2sql.main(
        ["--out-dir", str(out_dir), "-I", str(OPTION_DIR), str(src)]
    )

    assert rc == 1
    assert not (tmp_path / "evil.sql").exists()
    assert not out_dir.exists(), "路径校验失败时必须一张文件都不落"


def test_duplicate_basenames_are_refused(tmp_path):
    """两个同名输入只有靠前那个会被 protoc 编译，另一个静默漏掉。

    整条命令返回 0，产出的 schema 里少几张表，没有任何提示——
    只有下次建表时报 Unknown table。
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "user.proto").write_text("syntax = \"proto3\";\n", encoding="utf-8")
    (tmp_path / "b" / "user.proto").write_text("syntax = \"proto3\";\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc:
        proto2sql.generate(
            [str(tmp_path / "a" / "user.proto"), str(tmp_path / "b" / "user.proto")], []
        )
    assert "遮蔽" in str(exc.value)


def test_output_and_out_dir_are_mutually_exclusive(tmp_path):
    """两个都给时，早先是 --out-dir 静默胜出、-o 的文件一个字都不写，退出码还是 0。"""
    from proto2mysql.tools.proto2sql import main

    with pytest.raises(SystemExit) as exc:
        main(["-o", str(tmp_path / "x.sql"), "--out-dir", str(tmp_path), "dummy.proto"])
    assert exc.value.code == 2


def test_filename_relative_to_include_dir_is_accepted(tmp_path):
    """protoc 的标准写法：文件名**相对 -I 目录**，本身不是一条可用路径。

    `proto2sql -I proto account.proto` 是最常见的调用形式。
    早先的同名遮蔽检查拿 `Path(pf).resolve()` 直接比，把这种写法一并拒了——
    报的还是"会被遮蔽"，方向完全错。
    """
    from proto2mysql.tools.proto2sql import _resolve_inputs

    (tmp_path / "proto").mkdir()
    (tmp_path / "proto" / "user.proto").write_text('syntax = "proto3";\n', encoding="utf-8")
    paths, names = _resolve_inputs(["user.proto"], [str(tmp_path / "proto")])
    assert names == ["user.proto"]
    assert str(tmp_path / "proto") in paths


def test_base_install_includes_the_compiler_needed_by_installed_cli():
    """基础安装会注册 proto2sql，因此它需要的 grpcio-tools 也必须在基础依赖里。"""
    import re

    # 项目声明支持 Python 3.10；tomllib 是 3.11 才进入标准库，测试自身不能偷偷
    # 抬高最低版本。这里只需守住基础依赖项，限定读取 dependencies 数组即可。
    pyproject = (TESTS.parent / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)^\]", pyproject)
    assert match is not None
    assert re.search(r"['\"]grpcio-tools(?:[^'\"]*)['\"]", match.group(1), re.IGNORECASE)


def test_help_survives_legacy_windows_stdout_encoding():
    """argparse 的中文帮助也不能在 cp1252/cp936 控制台先于业务逻辑崩掉。"""
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    proc = subprocess.run(
        [sys.executable, "-m", "proto2mysql.tools.proto2sql", "--help"],
        cwd=TESTS.parent,
        env=env,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("ascii", errors="replace")
    assert b"UnicodeEncodeError" not in proc.stderr
