"""MessageTable / SQLBuilder 的数据安全回归。只走公开接口。"""

from __future__ import annotations

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from proto2mysql import (
    MessageTable,
    SQLBuilder,
    add_col,
    with_primary_key,
    with_table_name,
)
from proto2mysql.errors import PrimaryKeyNotFoundError, Proto2MySQLError
from proto2mysql.table import Statement


def test_all_builtin_on_duplicate_paths_leave_primary_key_unchanged(accountpb):
    """二级 UNIQUE 冲突时，ODKU 子句也绝不能把既存行主键改成入参主键。"""
    row = accountpb.account(id=99, name="new", email="same@example.test")
    table = MessageTable.from_message(accountpb.account)

    save = table.get_save_sql(row)
    partial = table.get_insert_on_dup_update_sql(row)
    lock_only = table.get_insert_on_dup_key_for_primary_key_sql(row)

    save_update = save.sql.partition(" ON DUPLICATE KEY UPDATE ")[2]
    partial_update = partial.sql.partition(" ON DUPLICATE KEY UPDATE ")[2]
    lock_update = lock_only.sql.partition(" ON DUPLICATE KEY UPDATE ")[2]
    guard = "`id` <=> VALUES(`id`)"
    assert save_update == ", ".join(
        f"`{col}` = IF({guard}, VALUES(`{col}`), `{col}`)"
        for col in ("name", "email", "level", "vip")
    )
    assert partial_update == ", ".join(
        f"`{col}` = IF({guard}, VALUES(`{col}`), `{col}`)"
        for col in ("name", "email")
    )
    assert lock_update == "`id` = `id`"
    # lock-only 不应在 INSERT 参数之后再追加一次待写入主键。
    assert lock_only.args == table.get_insert_sql(row).args


def test_save_upserts_guard_every_value_update_with_the_full_composite_identity(kitchenpb):
    row = kitchenpb.combo_key(user_id=7, provider="github", score=9)
    table = MessageTable.from_message(kitchenpb.combo_key)
    guard = (
        "`user_id` <=> VALUES(`user_id`) AND "
        "`provider` <=> VALUES(`provider`)"
    )
    update = f"`score` = IF({guard}, VALUES(`score`), `score`)"

    save = table.get_save_sql(row)
    batch = table.get_batch_save_sql([row])
    partial = table.get_insert_on_dup_update_sql(row)

    assert save.sql.endswith(f"ON DUPLICATE KEY UPDATE {update}")
    assert batch.sql.endswith(f"ON DUPLICATE KEY UPDATE {update}")
    assert partial.sql.endswith(f"ON DUPLICATE KEY UPDATE {update}")
    assert save.args == ["7", "github", "9"]
    assert batch.args == ["7", "github", "9"]
    assert partial.args == ["7", "github", "9"]


def test_save_update_sql_updates_non_keys_and_uses_the_full_composite_key(kitchenpb):
    row = kitchenpb.combo_key(user_id=7, provider="github", score=9)
    table = MessageTable.from_message(kitchenpb.combo_key)

    stmt = table.get_save_update_sql(row)

    assert stmt.sql == (
        "UPDATE `combo_key` SET `score` = ? "
        "WHERE `user_id` = ? AND `provider` = ?"
    )
    assert stmt.args == ["9", "7", "github"]


def test_save_update_sql_supports_full_and_only_set_field_modes(accountpb):
    row = accountpb.account(id=7, name="Alice")
    table = MessageTable.from_message(accountpb.account)

    full = table.get_save_update_sql(row)
    partial = table.get_save_update_sql(row, only_set_fields=True)
    partial_no_values = table.get_save_update_sql(
        accountpb.account(id=7), only_set_fields=True
    )

    assert full.sql == (
        "UPDATE `account` SET `name` = ?, `email` = ?, `level` = ?, `vip` = ? "
        "WHERE `id` = ?"
    )
    assert full.args == ["Alice", "", "0", "0", "7"]
    assert partial.sql == "UPDATE `account` SET `name` = ? WHERE `id` = ?"
    assert partial.args == ["Alice", "7"]
    assert partial_no_values.sql == (
        "UPDATE `account` SET `id` = `id` WHERE `id` = ?"
    )
    assert partial_no_values.args == ["7"]


def test_save_update_sql_uses_noop_assignment_for_an_all_primary_key_table(kitchenpb):
    row = kitchenpb.keyword_col(id=1, key="k", text="v")
    table = MessageTable.from_message(
        kitchenpb.keyword_col,
        [with_primary_key("id", "key", "text")],
    )

    stmt = table.get_save_update_sql(row)

    assert stmt.sql == (
        "UPDATE `keyword_col` SET `id` = `id` "
        "WHERE `id` = ? AND `key` = ? AND `text` = ?"
    )
    assert stmt.args == ["1", "k", "v"]


def test_all_primary_key_upserts_fall_back_to_a_noop_assignment(kitchenpb):
    row = kitchenpb.keyword_col(id=1, key="k", text="v")
    table = MessageTable.from_message(
        kitchenpb.keyword_col,
        [with_primary_key("id", "key", "text")],
    )

    for stmt in (
        table.get_save_sql(row),
        table.get_batch_save_sql([row]),
        table.get_insert_on_dup_update_sql(row),
    ):
        assert stmt.sql.endswith("ON DUPLICATE KEY UPDATE `id` = `id`")


@pytest.mark.parametrize(
    "primary_key",
    [(), ("missing",), ("id", "missing")],
)
def test_save_sql_builders_fail_closed_without_a_complete_valid_primary_key(
    accountpb, primary_key
):
    row = accountpb.account(id=7, name="Alice")
    table = MessageTable.from_message(
        accountpb.account,
        [with_primary_key(*primary_key)],
    )

    for build in (
        lambda: table.get_save_sql(row),
        lambda: table.get_batch_save_sql([row]),
        lambda: table.get_insert_on_dup_update_sql(row),
        lambda: table.get_save_update_sql(row),
    ):
        with pytest.raises(PrimaryKeyNotFoundError, match="primary key"):
            build()


def test_insert_ignore_uses_a_real_column_anchor_without_a_valid_primary_key(
    accountpb, kitchenpb
):
    no_pk = SQLBuilder.from_message(kitchenpb.stamped_probe)
    invalid_pk = SQLBuilder.from_message(
        accountpb.account,
        with_primary_key("missing"),
    )

    assert no_pk.insert_ignore(kitchenpb.stamped_probe(id=1)).sql.endswith(
        "ON DUPLICATE KEY UPDATE `id` = `id`"
    )
    assert invalid_pk.insert_ignore(accountpb.account(id=1)).sql.endswith(
        "ON DUPLICATE KEY UPDATE `id` = `id`"
    )


def test_insert_ignore_fails_closed_for_an_empty_message():
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="empty_insert_ignore.proto",
        package="safety",
        syntax="proto3",
    )
    file_proto.message_type.add(name="Empty")
    descriptor = descriptor_pool.DescriptorPool().Add(file_proto)
    empty_type = message_factory.GetMessageClass(
        descriptor.message_types_by_name["Empty"]
    )
    builder = SQLBuilder.from_message(empty_type)

    with pytest.raises(Proto2MySQLError, match="empty message"):
        builder.insert_ignore(empty_type())


def test_custom_upsert_cannot_mutate_primary_key(accountpb):
    row = accountpb.account(id=99, email="same@example.test")
    builder = SQLBuilder.from_message(accountpb.account)

    with pytest.raises(Proto2MySQLError, match="primary key|主键"):
        builder.upsert(row, "id")
    with pytest.raises(Proto2MySQLError, match="primary key|主键"):
        builder.upsert_with(row, add_col("id", 1))

    # 专门的 lock-only API 仍允许主键自赋值，因为它不改变值。
    stmt = builder.upsert_keep_old(row)
    assert stmt.sql.endswith("ON DUPLICATE KEY UPDATE `id` = `id`")


@pytest.mark.parametrize("method", ["incr_by_pk", "decr_by_pk_if_enough"])
def test_numeric_mutations_reject_text_columns(accountpb, method):
    builder = SQLBuilder.from_message(accountpb.account)
    row = accountpb.account(id=1)

    with pytest.raises(Proto2MySQLError, match="not numeric"):
        getattr(builder, method)(row, "name", 1)


def test_format_paramstyle_only_rewrites_real_placeholders():
    stmt = Statement(
        "SELECT '?', JSON_EXTRACT(`doc`, '$.a?b'), 10 % 3 "
        "/* block ? 20% */ -- line ? 30%\n"
        "FROM `t?` WHERE `id` = ? # tail ? 40%\n",
        [7],
    )

    sql, args = stmt.for_paramstyle("format")
    assert sql == (
        "SELECT '?', JSON_EXTRACT(`doc`, '$.a?b'), 10 %% 3 "
        "/* block ? 20%% */ -- line ? 30%%\n"
        "FROM `t?` WHERE `id` = %s # tail ? 40%%\n"
    )
    assert args == (7,)


def test_ddl_comments_use_mode_independent_quote_escaping(accountpb):
    table = MessageTable.from_message(
        accountpb.account, [with_table_name("player's\\table")]
    )
    sql = table.get_create_table_sql()

    assert "COMMENT='player''s\\\\table';" in sql
    assert "\\'" not in sql
