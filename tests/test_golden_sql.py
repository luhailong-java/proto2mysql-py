"""黄金用例：期望字符串逐条搬自 Go 仓库的 sqlbuilder_test.go / generator_test.go。

跑这个文件就是在验证"Python 版和 Go 版对同一份 .proto 产出同一份 SQL"。
**改动这些期望值前先确认 Go 侧也改了**，否则两边的表就悄悄分叉了。

Go 侧参数是 Go string（可承载裸字节），Python 侧二进制列是 bytes，
所以 Go 里写 `""` 的 message 列在这里是 `b""`——落库字节相同，见 pbconv 的注释。
"""

from __future__ import annotations

import pytest

from proto2mysql import (
    SQLBuilder,
    QueryOptions,
    add_col,
    generate_create_table_sql,
    set_col_expr,
    set_new,
    set_new_if_zero,
    sub_col,
)
from proto2mysql.errors import EmptyWhereClauseError, FieldNotFoundError
from proto2mysql.sqlbuilder import NoAssignsError, NoFieldsSetError

ALL_COLS = "`id`, `ip`, `port`, `group_id`, `player`, `player_id`"
SELECT_ALL = f"SELECT {ALL_COLS} FROM `golang_test`"
INSERT_PART = f"INSERT INTO `golang_test` ({ALL_COLS}) VALUES (?, ?, ?, ?, ?, ?)"
INSERT_ARGS = ["7", "10.0.0.1", "8080", "0", b"", "0"]


@pytest.fixture()
def b(testpb):
    return SQLBuilder.from_message(testpb.golang_test)


@pytest.fixture()
def msg(testpb):
    return testpb.golang_test(id=7, ip="10.0.0.1", port=8080)


def check(stmt, want_sql, want_args):
    assert stmt.sql == want_sql
    assert stmt.args == want_args


# ── INSERT ──────────────────────────────────────────────────────────────


def test_insert_variants(b, msg, testpb):
    check(b.insert(msg), INSERT_PART, INSERT_ARGS)

    # 列子集：proto3 零值字段（group_id/player_id）视为未赋值，交给列默认值
    check(
        b.insert_set_fields(msg),
        "INSERT INTO `golang_test` (`id`, `ip`, `port`) VALUES (?, ?, ?)",
        ["7", "10.0.0.1", "8080"],
    )

    check(
        b.insert_ignore(msg),
        f"INSERT IGNORE INTO `golang_test` ({ALL_COLS}) VALUES (?, ?, ?, ?, ?, ?)",
        INSERT_ARGS,
    )

    check(
        b.insert_ignore_set_fields(msg),
        "INSERT IGNORE INTO `golang_test` (`id`, `ip`, `port`) VALUES (?, ?, ?)",
        ["7", "10.0.0.1", "8080"],
    )

    # 自增主键未赋值时，列子集写法会整列省略，由 MySQL 发号
    check(
        b.insert_set_fields(testpb.golang_test(ip="10.0.0.2")),
        "INSERT INTO `golang_test` (`ip`) VALUES (?)",
        ["10.0.0.2"],
    )

    with pytest.raises(NoFieldsSetError):
        b.insert_set_fields(testpb.golang_test())


def test_batch_insert(b, testpb):
    msgs = [testpb.golang_test(id=1, ip="a"), testpb.golang_test(id=2, ip="b")]
    both = ["1", "a", "0", "0", b"", "0", "2", "b", "0", "0", b"", "0"]

    check(
        b.batch_insert_ignore(msgs),
        f"INSERT IGNORE INTO `golang_test` ({ALL_COLS}) "
        "VALUES (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?)",
        both,
    )

    check(
        b.batch_upsert(msgs, "ip"),
        f"INSERT INTO `golang_test` ({ALL_COLS}) VALUES (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?)"
        " ON DUPLICATE KEY UPDATE `ip` = VALUES(`ip`)",
        both,
    )


# ── UPSERT ──────────────────────────────────────────────────────────────


def test_upsert_semantics(b, msg):
    # 缺省覆盖全部非主键列
    check(
        b.upsert(msg),
        INSERT_PART + " ON DUPLICATE KEY UPDATE `ip` = VALUES(`ip`), `port` = VALUES(`port`),"
        " `group_id` = VALUES(`group_id`), `player` = VALUES(`player`),"
        " `player_id` = VALUES(`player_id`)",
        INSERT_ARGS,
    )

    check(
        b.upsert(msg, "ip", "port"),
        INSERT_PART + " ON DUPLICATE KEY UPDATE `ip` = VALUES(`ip`), `port` = VALUES(`port`)",
        INSERT_ARGS,
    )

    # 累加语义：冲突时把本次的值加到旧值上
    check(
        b.upsert_add(msg, "port"),
        INSERT_PART + " ON DUPLICATE KEY UPDATE `port` = `port` + VALUES(`port`)",
        INSERT_ARGS,
    )
    with pytest.raises(NoAssignsError):
        b.upsert_add(msg)

    # 非数值列不许累加：防止 MySQL 隐式转换把数据写坏
    with pytest.raises(Exception):
        b.upsert_add(msg, "ip")

    # 只拿行锁不改数据
    check(
        b.upsert_keep_old(msg),
        INSERT_PART + " ON DUPLICATE KEY UPDATE `id` = `id`",
        INSERT_ARGS,
    )

    # 自定义合并语义
    check(
        b.upsert_with(
            msg,
            set_col_expr("group_id", "`group_id` + 1"),
            set_new("ip"),
            set_new_if_zero("player_id"),
        ),
        INSERT_PART + " ON DUPLICATE KEY UPDATE `group_id` = `group_id` + 1,"
        " `ip` = VALUES(`ip`),"
        " `player_id` = IF(`player_id` = 0, VALUES(`player_id`), `player_id`)",
        INSERT_ARGS,
    )


# ── SELECT ──────────────────────────────────────────────────────────────


def test_select(b, msg):
    check(b.select_by_pk(msg), f"{SELECT_ALL} WHERE `id` = ?", ["7"])
    check(
        b.select_by_pk_for_update(msg),
        f"{SELECT_ALL} WHERE `id` = ? FOR UPDATE",
        ["7"],
    )
    check(
        b.select_columns(["port"], "`id` = ?", ["7"], QueryOptions(for_update=True)),
        "SELECT `port` FROM `golang_test` WHERE `id` = ? FOR UPDATE",
        ["7"],
    )
    check(
        b.select_where("`group_id` = ?", [3], QueryOptions(order_by="`id` DESC", limit=10, offset=20)),
        f"{SELECT_ALL} WHERE `group_id` = ? ORDER BY `id` DESC LIMIT 10 OFFSET 20",
        [3],
    )
    check(
        b.select_by_pk_in([1, 2, 3]),
        f"{SELECT_ALL} WHERE `id` IN (?, ?, ?)",
        [1, 2, 3],
    )
    check(b.count(), "SELECT COUNT(*) FROM `golang_test` WHERE 1=1", [])
    check(
        b.exists("`ip` = ?", ["x"]),
        "SELECT 1 FROM `golang_test` WHERE `ip` = ? LIMIT 1",
        ["x"],
    )
    check(
        b.exists_by_pk_for_update(msg),
        "SELECT 1 FROM `golang_test` WHERE `id` = ? LIMIT 1 FOR UPDATE",
        ["7"],
    )

    with pytest.raises(FieldNotFoundError):
        b.select_columns(["nope"], "`id` = ?", ["7"])


# ── UPDATE ──────────────────────────────────────────────────────────────


def test_update(b, msg):
    check(
        b.update_by_pk(msg),
        "UPDATE `golang_test` SET `id` = ?, `ip` = ?, `port` = ? WHERE `id` = ?",
        ["7", "10.0.0.1", "8080", "7"],
    )
    check(
        b.update_by_pk_if(msg, "`group_id` = ?", [5]),
        "UPDATE `golang_test` SET `id` = ?, `ip` = ?, `port` = ? WHERE `id` = ? AND `group_id` = ?",
        ["7", "10.0.0.1", "8080", "7", 5],
    )
    check(
        b.update_fields_by_pk(msg, "port", "group_id"),
        "UPDATE `golang_test` SET `port` = ?, `group_id` = ? WHERE `id` = ?",
        ["8080", "0", "7"],
    )
    check(
        b.update_assigns_by_pk(msg, add_col("port", 1), set_col_expr("ip", "CONCAT(`ip`, ?)", "-x")),
        "UPDATE `golang_test` SET `port` = `port` + ?, `ip` = CONCAT(`ip`, ?) WHERE `id` = ?",
        [1, "-x", "7"],
    )
    check(
        b.incr_by_pk(msg, "port", 1),
        "UPDATE `golang_test` SET `port` = `port` + ? WHERE `id` = ?",
        [1, "7"],
    )
    check(
        b.decr_by_pk_if_enough(msg, "port", 10),
        "UPDATE `golang_test` SET `port` = `port` - ? WHERE `id` = ? AND `port` >= ?",
        [10, "7", 10],
    )
    check(
        b.update_assigns_where([sub_col("port", 10)], "`group_id` = ? AND `port` >= ?", [3, 10]),
        "UPDATE `golang_test` SET `port` = `port` - ? WHERE `group_id` = ? AND `port` >= ?",
        [10, 3, 10],
    )
    check(
        b.update_where(msg, "`group_id` = ?", [3]),
        "UPDATE `golang_test` SET `id` = ?, `ip` = ?, `port` = ? WHERE `group_id` = ?",
        ["7", "10.0.0.1", "8080", 3],
    )


# ── DELETE ──────────────────────────────────────────────────────────────


def test_delete(b, msg):
    check(b.delete_by_pk(msg), "DELETE FROM `golang_test` WHERE `id` = ?", ["7"])
    check(
        b.delete_by_pk_if(msg, "`group_id` = ?", [3]),
        "DELETE FROM `golang_test` WHERE `id` = ? AND `group_id` = ?",
        ["7", 3],
    )
    check(
        b.delete_where("`port` = ?", [80]),
        "DELETE FROM `golang_test` WHERE `port` = ?",
        [80],
    )
    check(
        b.delete_where_limit("`player_id` < ?", [100], "`id` ASC", 500),
        "DELETE FROM `golang_test` WHERE `player_id` < ? ORDER BY `id` ASC LIMIT 500",
        [100],
    )
    check(
        b.delete_where_limit("`player_id` < ?", [100], "", 500),
        "DELETE FROM `golang_test` WHERE `player_id` < ? LIMIT 500",
        [100],
    )
    check(
        b.delete_by_pk_in([1, 2]),
        "DELETE FROM `golang_test` WHERE `id` IN (?, ?)",
        [1, 2],
    )


def test_mutation_requires_where(b, msg):
    """UPDATE/DELETE 的空条件必须被拒绝，而不是悄悄退化成整表操作。"""
    with pytest.raises(EmptyWhereClauseError):
        b.delete_where("", [])
    with pytest.raises(EmptyWhereClauseError):
        b.delete_where("   ", [])
    with pytest.raises(EmptyWhereClauseError):
        b.update_where(msg, "", [])
    with pytest.raises(EmptyWhereClauseError):
        b.update_assigns_where([add_col("port", 1)], "", [])
    with pytest.raises(EmptyWhereClauseError):
        b.delete_where_limit("", [], "", 10)

    # 显式 1=1 是允许的：危险意图在代码里看得见
    check(
        b.delete_where("1=1", []),
        "DELETE FROM `golang_test` WHERE 1=1",
        [],
    )


# ── DDL（对照 Go 的 tools/proto2sql/generator_test.go）─────────────────


def test_create_table_account(accountpb):
    sql = generate_create_table_sql(accountpb.account)
    for fragment in (
        "CREATE TABLE IF NOT EXISTS `account`",
        "`id` bigint unsigned NOT NULL AUTO_INCREMENT",
        "`email` MEDIUMTEXT",
        "PRIMARY KEY (`id`)",
        "INDEX `idx_account_0` (`name`(191))",
        "UNIQUE KEY `uk_account` (`email`(191))",
    ):
        assert fragment in sql, f"SQL 缺少 {fragment!r}\n--- got ---\n{sql}"


def test_create_table_full_text(testpb):
    """整条 DDL 逐字节比对（含列注释 pb:N 与表尾选项）。"""
    assert generate_create_table_sql(testpb.golang_test) == (
        "CREATE TABLE IF NOT EXISTS `golang_test` (\n"
        "  `id` int unsigned NOT NULL AUTO_INCREMENT COMMENT 'pb:1',\n"
        "  `ip` MEDIUMTEXT COMMENT 'pb:2',\n"
        "  `port` int unsigned NOT NULL DEFAULT 0 COMMENT 'pb:3',\n"
        "  `group_id` int unsigned NOT NULL DEFAULT 0 COMMENT 'pb:4',\n"
        "  `player` MEDIUMBLOB COMMENT 'pb:5',\n"
        "  `player_id` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:6',\n"
        "  PRIMARY KEY (`id`)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci "
        "COMMENT='golang_test';"
    )
