"""按 proto 字段号迁移：改名保留数据、类型对齐、DATETIME 精度补齐。

这是本库最有价值也最容易写错的一块——错了不会报错，只会让线上表悄悄留在旧结构上。
"""

from __future__ import annotations

import pytest

from proto2mysql import ColumnMeta, MessageTable
from proto2mysql.table import is_type_match, parse_mysql_type


def table(testpb):
    return MessageTable.from_message(testpb.golang_test)


# ── 类型解析与兼容判定 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "base", "length", "decimal", "unsigned"),
    [
        ("bigint(20) unsigned", "bigint", 20, 0, True),
        ("int", "int", 0, 0, False),
        ("decimal(10,2)", "decimal", 10, 2, False),
        ("DATETIME(6)", "datetime", 6, 0, False),
        ("varchar(64)", "varchar", 64, 0, False),
    ],
)
def test_parse_mysql_type(text, base, length, decimal, unsigned):
    info = parse_mysql_type(text)
    assert (info.base_type, info.length, info.decimal, info.unsigned) == (
        base,
        length,
        decimal,
        unsigned,
    )


@pytest.mark.parametrize(
    ("current", "target", "match"),
    [
        ("int unsigned", "int unsigned NOT NULL DEFAULT 0", True),
        ("int", "int unsigned NOT NULL DEFAULT 0", False),  # 有无符号必须一致
        ("varchar(32)", "varchar(64)", True),  # 放大兼容
        ("varchar(64)", "varchar(32)", False),  # 缩小不兼容
        ("mediumtext", "MEDIUMTEXT", True),  # 大小写无关
        ("timestamp", "datetime(6)", False),  # 精度不足必须 ALTER
        ("datetime", "DATETIME(6)", False),  # DATETIME(0) 必须升到 (6)
        ("datetime(6)", "DATETIME(6)", True),
        ("datetime(6)", "DATETIME(3)", True),  # 线上精度更高时不动它（降精度会丢数据）
        ("tinyint(1)", "tinyint(1) NOT NULL DEFAULT 0", True),
    ],
)
def test_is_type_match(current, target, match):
    assert is_type_match(current, target) is match


# ── ALTER 子句生成 ──────────────────────────────────────────────────────


def test_add_column_when_missing(testpb):
    t = table(testpb)
    current = {
        "id": ColumnMeta("int unsigned", 1),
        "ip": ColumnMeta("mediumtext", 2),
        "port": ColumnMeta("int unsigned", 3),
        "group_id": ColumnMeta("int unsigned", 4),
        "player": ColumnMeta("mediumblob", 5),
    }
    assert t.build_alter_clauses(current) == [
        "ADD COLUMN `player_id` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:6'"
    ]


def test_rename_by_field_number_preserves_data(testpb):
    """列名变了但字段号一致 → CHANGE COLUMN 改名，**不是** DROP+ADD（那会丢数据）。"""
    t = table(testpb)
    current = {
        "id": ColumnMeta("int unsigned", 1),
        "old_ip_name": ColumnMeta("mediumtext", 2),  # proto 里字段 2 现在叫 ip
        "port": ColumnMeta("int unsigned", 3),
        "group_id": ColumnMeta("int unsigned", 4),
        "player": ColumnMeta("mediumblob", 5),
        "player_id": ColumnMeta("bigint unsigned", 6),
    }
    clauses = t.build_alter_clauses(current)
    assert clauses == [
        "CHANGE COLUMN `old_ip_name` `ip` MEDIUMTEXT COMMENT 'pb:2'"
    ]
    assert not any(c.startswith("DROP") for c in clauses)


def test_backfill_comment_on_legacy_table(testpb):
    """老表列上没有 pb:N 注释：同名列要 MODIFY 一次把注释补回去，之后改名识别才生效。"""
    t = table(testpb)
    current = {
        "id": ColumnMeta("int unsigned", 0),  # 0 = 无注释
        "ip": ColumnMeta("mediumtext", 0),
        "port": ColumnMeta("int unsigned", 0),
        "group_id": ColumnMeta("int unsigned", 0),
        "player": ColumnMeta("mediumblob", 0),
        "player_id": ColumnMeta("bigint unsigned", 0),
    }
    clauses = t.build_alter_clauses(current)
    assert len(clauses) == 6
    assert all(c.startswith("MODIFY COLUMN") for c in clauses)
    assert "COMMENT 'pb:1'" in clauses[0]


def test_no_change_when_aligned(testpb):
    t = table(testpb)
    current = {
        "id": ColumnMeta("int unsigned", 1),
        "ip": ColumnMeta("mediumtext", 2),
        "port": ColumnMeta("int unsigned", 3),
        "group_id": ColumnMeta("int unsigned", 4),
        "player": ColumnMeta("mediumblob", 5),
        "player_id": ColumnMeta("bigint unsigned", 6),
    }
    assert t.build_alter_clauses(current) == []


def test_datetime_precision_upgrade(kitchenpb):
    """存量 DATETIME(0) 必须升到 DATETIME(6)，否则写入的毫秒被静默丢掉。"""
    t = MessageTable.from_message(kitchenpb.kitchen_sink)
    current = {fd.name: ColumnMeta(t.get_mysql_field_type(fd), fd.number) for fd in t.fields}
    current["created_at"] = ColumnMeta("datetime", 11)  # 老表停在 DATETIME(0)

    clauses = t.build_alter_clauses(current)
    assert clauses == ["MODIFY COLUMN `created_at` DATETIME(6) COMMENT 'pb:11'"]


def test_alter_does_not_mutate_input(testpb):
    """不能改调用方传进来的字典。"""
    t = table(testpb)
    current = {"id": ColumnMeta("int unsigned", 1)}
    snapshot = dict(current)
    t.build_alter_clauses(current)
    assert current == snapshot


# ── 建表 DDL 的字段类型 ─────────────────────────────────────────────────


def test_nullable_option_removes_not_null(kitchenpb):
    t = MessageTable.from_message(kitchenpb.kitchen_sink)
    zone = t.descriptor.fields_by_name["zone_id"]
    assert t.get_mysql_field_type(zone) == "int DEFAULT 0"


def test_timestamp_always_nullable_regardless_of_option(kitchenpb):
    """Timestamp 恒定可空，不受 nullable 选项影响——声明成 NOT NULL 会让整行插不进去。"""
    t = MessageTable.from_message(kitchenpb.kitchen_sink)
    created = t.descriptor.fields_by_name["created_at"]
    assert t.get_mysql_field_type(created) == "DATETIME(6)"


def test_auto_increment_drops_default(kitchenpb):
    """自增列不能带 DEFAULT 0，否则 MySQL 报 Error 1067。"""
    t = MessageTable.from_message(kitchenpb.kitchen_sink)
    pk = t.descriptor.fields_by_name["id"]
    assert t.get_mysql_field_type(pk) == "bigint NOT NULL AUTO_INCREMENT"


def test_containers_are_mediumblob(kitchenpb):
    t = MessageTable.from_message(kitchenpb.kitchen_sink)
    for name in ("tags", "attrs", "payload", "sub"):
        assert t.get_mysql_field_type(t.descriptor.fields_by_name[name]) == "MEDIUMBLOB"


def test_composite_key_ddl(kitchenpb):
    sql = MessageTable.from_message(kitchenpb.combo_key).get_create_table_sql()
    assert "PRIMARY KEY (`user_id`,`provider`(191))" in sql


def test_composite_index_ddl(kitchenpb):
    sql = MessageTable.from_message(kitchenpb.kitchen_sink).get_create_table_sql()
    assert "INDEX `idx_kitchen_sink_0` (`name`(191))" in sql
    assert "INDEX `idx_kitchen_sink_1` (`zone_id`,`created_at`)" in sql
    assert "UNIQUE KEY `uk_kitchen_sink` (`name`(191),`zone_id`)" in sql


def test_backtick_in_identifier_is_escaped():
    """表名里的反引号必须双写，否则能拼出越界 SQL。"""
    from proto2mysql import escape_mysql_name

    assert escape_mysql_name("a`b") == "`a``b`"


def test_keyword_column_logs(kitchenpb, caplog):
    """列名撞 MySQL 关键字要记日志，但**不改写列名**——改写会让列对不上线上表。"""
    import logging

    t = MessageTable.from_message(kitchenpb.keyword_col)
    with caplog.at_level(logging.WARNING, logger="proto2mysql"):
        clauses = t.build_alter_clauses({})
    assert "conflicts with MySQL keyword" in caplog.text
    # 告警归告警，列还是原名（加反引号转义即可安全）
    assert any("ADD COLUMN `key` MEDIUMTEXT" in c for c in clauses)
    assert any("ADD COLUMN `text` MEDIUMTEXT" in c for c in clauses)


def test_column_order_follows_declaration_not_field_number(kitchenpb):
    """列顺序跟声明顺序，不跟字段号。

    Python 有两个"字段列表"且顺序不同：``DESCRIPTOR.fields`` 是声明序，
    ``msg.ListFields()`` 是字段号升序。SELECT 的列顺序、INSERT 的占位符顺序、
    以及读回时按下标对位的 parse_from_row 必须同源——用错那个会整行错列，
    而且不报错、不报警，只是数据串了。
    """
    t = MessageTable.from_message(kitchenpb.order_demo)
    assert [f.name for f in t.fields] == ["id", "a", "b"]  # 声明序
    assert [f.number for f in t.fields] == [10, 3, 1]  # 字段号是乱的

    sql = t.get_create_table_sql()
    assert sql.index("`id`") < sql.index("`a`") < sql.index("`b`")
    assert "`id` bigint NOT NULL DEFAULT 0 COMMENT 'pb:10'" in sql
    assert t.select_fields_sql.endswith("`id`, `a`, `b` FROM `order_demo`")


def test_row_roundtrip_with_scrambled_field_numbers(kitchenpb):
    """按声明序写出去、按同一顺序读回来，值不能串位。"""
    from proto2mysql import pbconv

    m = kitchenpb.order_demo(id=7, a="x", b=9)
    t = MessageTable.from_message(kitchenpb.order_demo)
    row = t.all_field_args(m)
    assert row == ["7", "x", "9"]

    back = kitchenpb.order_demo()
    pbconv.parse_from_row(back, row)
    assert (back.id, back.a, back.b) == (7, "x", 9)
