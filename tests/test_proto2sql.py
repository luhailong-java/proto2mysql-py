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
