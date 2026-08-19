"""DB 层测试：用假 DB-API 连接断言"发出去的语句"，不需要真 MySQL。

真库集成测试见 test_integration_mysql.py（需要 PROTO2MYSQL_DSN 才会跑）。
"""

from __future__ import annotations

import pytest

from fakedb import FakeConnection, FakeIntegrityError

from proto2mysql import DB, DictCache
from proto2mysql.errors import (
    DuplicateKeyError,
    MultipleRowsFoundError,
    NoRowsFoundError,
    Proto2MySQLError,
    TableNotFoundError,
)


@pytest.fixture()
def conn():
    return FakeConnection()


@pytest.fixture()
def db(conn, testpb):
    d = DB(conn, "testdb")
    d.register_table(testpb.golang_test)
    d.register_table(testpb.golang_test_list)
    return d


# ── 占位符转换 ──────────────────────────────────────────────────────────


def test_paramstyle_conversion(db, conn, testpb):
    """生成时是 ?，下发驱动时必须是 %s。"""
    db.insert(testpb.golang_test(id=7, ip="a"))
    sql, args = conn.executed[-1]
    assert "%s" in sql and "?" not in sql
    assert args == ("7", "a", "0", "0", b"", "0")


def test_percent_literal_escaped(db, conn, testpb):
    """where 里的 % 字面量必须转义成 %%，否则 PyMySQL 会把它当格式符炸掉。"""
    out = testpb.golang_test()
    conn.queue_rows([(1, "x", 0, 0, b"", 0)])
    db.find_one_by_where(out, "`ip` LIKE '%.0.0.%'")
    assert "'%%.0.0.%%'" in conn.last_sql()


def test_qmark_paramstyle_passthrough(conn, testpb):
    db = DB(conn, "testdb", paramstyle="qmark")
    db.register_table(testpb.golang_test)
    db.insert(testpb.golang_test(id=7))
    assert "?" in conn.last_sql() and "%s" not in conn.last_sql()


# ── CRUD ────────────────────────────────────────────────────────────────


def test_unregistered_table_raises(conn, testpb):
    db = DB(conn, "testdb")
    with pytest.raises(TableNotFoundError):
        db.insert(testpb.golang_test(id=1))


def test_duplicate_key_wrapped(db, conn, testpb):
    conn.raise_on_execute = FakeIntegrityError()
    with pytest.raises(DuplicateKeyError):
        db.insert(testpb.golang_test(id=1))


def test_batch_insert_chunks(db, conn, testpb):
    msgs = [testpb.golang_test(id=i + 1) for i in range(2500)]
    db.batch_insert(msgs)
    # 1000 + 1000 + 500 → 三条语句
    assert len(conn.executed) == 3
    assert conn.executed[0][0].count("(%s, %s, %s, %s, %s, %s)") == 1000
    assert conn.executed[2][0].count("(%s, %s, %s, %s, %s, %s)") == 500


def test_find_one_by_pk(db, conn, testpb):
    conn.queue_rows([(9, "10.0.0.9", 80, 3, b"", 42)])
    out = testpb.golang_test(id=9)
    db.find_one_by_pk(out)
    assert (out.ip, out.port, out.group_id, out.player_id) == ("10.0.0.9", 80, 3, 42)
    assert "WHERE `id` = %s" in conn.last_sql()


def test_find_one_no_rows(db, conn, testpb):
    conn.queue_rows([])
    with pytest.raises(NoRowsFoundError):
        db.find_one_by_pk(testpb.golang_test(id=1))


def test_find_one_multiple_rows(db, conn, testpb):
    conn.queue_rows([(1, "a", 0, 0, b"", 0), (2, "b", 0, 0, b"", 0)])
    with pytest.raises(MultipleRowsFoundError):
        db.find_one_by_pk(testpb.golang_test(id=1))


def test_find_all_into_list(db, conn, testpb):
    conn.queue_rows([(1, "a", 0, 0, b"", 0), (2, "b", 0, 0, b"", 0)])
    out = testpb.golang_test_list()
    db.find_all(out)
    assert [r.id for r in out.test_list] == [1, 2]
    assert [r.ip for r in out.test_list] == ["a", "b"]


def test_find_all_clears_previous_rows(db, conn, testpb):
    """复用列表消息查两次不能翻倍。"""
    out = testpb.golang_test_list()
    conn.queue_rows([(1, "a", 0, 0, b"", 0)])
    db.find_all(out)
    conn.queue_rows([(2, "b", 0, 0, b"", 0)])
    db.find_all(out)
    assert [r.id for r in out.test_list] == [2]


def test_find_or_create_inserts_when_missing(db, conn, testpb):
    conn.queue_rows([])  # select 无结果
    created = db.find_or_create(testpb.golang_test(id=5, ip="new"))
    assert created is True
    assert conn.executed[-1][0].startswith("INSERT INTO `golang_test`")


def test_update_only_writes_set_fields(db, conn, testpb):
    db.update(testpb.golang_test(id=7, ip="x"))
    sql = conn.last_sql()
    assert sql == "UPDATE `golang_test` SET `id` = %s, `ip` = %s WHERE `id` = %s"


def test_update_fields_by_pk_writes_zero(db, conn, testpb):
    """update 会跳过零值；update_fields_by_pk 必须照写（清零场景）。"""
    db.update_fields_by_pk(testpb.golang_test(id=7), "port")
    assert conn.last_sql() == "UPDATE `golang_test` SET `port` = %s WHERE `id` = %s"
    assert conn.last_args() == ("0", "7")


def test_incr_and_decr(db, conn, testpb):
    msg = testpb.golang_test(id=7)
    db.incr_by_pk(msg, "port", 5)
    assert conn.last_sql() == "UPDATE `golang_test` SET `port` = `port` + %s WHERE `id` = %s"

    conn.next_rowcount = 0
    assert db.decr_by_pk_if_enough(msg, "port", 5) is False
    assert conn.last_sql() == (
        "UPDATE `golang_test` SET `port` = `port` - %s WHERE `id` = %s AND `port` >= %s"
    )


def test_update_if_version(db, conn, testpb):
    msg = testpb.golang_test(id=7, ip="x", group_id=3)
    conn.next_rowcount = 1
    assert db.update_if_version(msg, "group_id") is True
    assert conn.last_sql() == (
        "UPDATE `golang_test` SET `ip` = %s, `group_id` = `group_id` + 1 "
        "WHERE `id` = %s AND `group_id` = %s"
    )
    assert conn.last_args() == ("x", "7", "3")

    conn.next_rowcount = 0
    assert db.update_if_version(msg, "group_id") is False


def test_batch_delete_tuple_in(db, conn, testpb):
    db.batch_delete([testpb.golang_test(id=1), testpb.golang_test(id=2)])
    assert conn.last_sql() == "DELETE FROM `golang_test` WHERE (`id`) IN ((%s), (%s))"
    assert conn.last_args() == ("1", "2")


def test_count_and_exists(db, conn, testpb):
    conn.queue_rows([(3,)])
    assert db.count(testpb.golang_test()) == 3
    assert conn.executed[-1][0] == "SELECT COUNT(*) FROM `golang_test` WHERE 1=1"

    conn.queue_rows([(1,)])
    assert db.exists(testpb.golang_test(), "`ip` = ?", ["a"]) is True
    conn.queue_rows([])
    assert db.exists(testpb.golang_test(), "`ip` = ?", ["a"]) is False


def test_count_accepts_list_message(db, conn, testpb):
    """行消息和列表消息都能解析到同一张表。"""
    conn.queue_rows([(7,)])
    assert db.count(testpb.golang_test_list()) == 7


def test_find_page_by_cursor(db, conn, testpb):
    conn.queue_rows([])
    db.find_page_by_cursor(testpb.golang_test_list(), "", None, "id", 100, 20)
    assert conn.last_sql() == (
        "SELECT `id`, `ip`, `port`, `group_id`, `player`, `player_id` FROM `golang_test` "
        "WHERE (1=1) AND `id` > %s ORDER BY `id` ASC LIMIT 20"
    )


# ── 事务 ────────────────────────────────────────────────────────────────


def test_transaction_commits(db, conn, testpb):
    with db.transaction() as tx:
        tx.insert(testpb.golang_test(id=1))
    assert conn.commits == 1 and conn.rollbacks == 0


def test_transaction_rolls_back(db, conn, testpb):
    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            tx.insert(testpb.golang_test(id=1))
            raise RuntimeError("boom")
    assert conn.rollbacks == 1 and conn.commits == 0


def test_nested_transaction_rejected(db):
    with db.transaction() as tx:
        with pytest.raises(Proto2MySQLError):
            with tx.transaction():
                pass


def test_for_update_requires_transaction(db, testpb):
    with pytest.raises(Proto2MySQLError):
        db.find_one_by_pk_for_update(testpb.golang_test(id=1))


# ── 缓存 ────────────────────────────────────────────────────────────────


def test_cache_read_through_and_invalidate(db, conn, testpb):
    cache = DictCache()
    db.enable_cache(cache, ttl=60)

    conn.queue_rows([(9, "cached", 0, 0, b"", 0)])
    out = testpb.golang_test(id=9)
    db.find_one_by_pk(out)
    assert len(cache) == 1
    executed_before = len(conn.executed)

    # 第二次读应命中缓存，不再发 SQL
    out2 = testpb.golang_test(id=9)
    db.find_one_by_pk(out2)
    assert len(conn.executed) == executed_before
    assert out2.ip == "cached"

    # 写后缓存失效
    db.update(testpb.golang_test(id=9, ip="new"))
    assert len(cache) == 0


def test_cache_invalidation_deferred_until_commit(db, conn, testpb):
    """事务内先不删缓存——回滚后删了就是把还有效的缓存误删。"""
    cache = DictCache()
    db.enable_cache(cache)
    cache.set("pb:golang_test:9", b"x", None)

    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            tx.update(testpb.golang_test(id=9, ip="a"))
            assert len(cache) == 1, "事务未提交前不应删缓存"
            raise RuntimeError("boom")
    assert len(cache) == 1, "回滚后缓存必须还在"

    with db.transaction() as tx:
        tx.update(testpb.golang_test(id=9, ip="a"))
    assert len(cache) == 0, "提交后才删"


def test_cache_failure_degrades_to_db(db, conn, testpb):
    """缓存是弱依赖：它炸了只能降级读库，不能让 DB 操作失败。"""

    class BrokenCache:
        def get(self, key):
            raise RuntimeError("redis down")

        def set(self, key, value, ttl):
            raise RuntimeError("redis down")

        def delete(self, *keys):
            raise RuntimeError("redis down")

    db.enable_cache(BrokenCache())
    conn.queue_rows([(9, "from-db", 0, 0, b"", 0)])
    out = testpb.golang_test(id=9)
    db.find_one_by_pk(out)
    assert out.ip == "from-db"
    db.update(testpb.golang_test(id=9, ip="x"))  # 不应抛


# ── 建表 / 迁移 ─────────────────────────────────────────────────────────


def test_sync_creates_table_when_missing(db, conn, testpb):
    conn.queue_rows([(0,)])  # information_schema: 表不存在
    db.create_or_update_table(testpb.golang_test)
    assert conn.executed[-1][0].startswith("CREATE TABLE IF NOT EXISTS `golang_test`")


def test_sync_alters_when_column_missing(db, conn, testpb):
    conn.queue_rows(
        [(1,)],  # 表存在
        [  # 现有列：缺 player_id，且 ip 类型不兼容
            ("id", "int unsigned", "pb:1"),
            ("ip", "int", "pb:2"),
            ("port", "int unsigned", "pb:3"),
            ("group_id", "int unsigned", "pb:4"),
            ("player", "mediumblob", "pb:5"),
        ],
        [(1,)],  # 有主键
    )
    db.create_or_update_table(testpb.golang_test)
    alter = conn.executed[-1][0]
    assert alter.startswith("ALTER TABLE `golang_test` ")
    assert "MODIFY COLUMN `ip` MEDIUMTEXT COMMENT 'pb:2'" in alter
    assert "ADD COLUMN `player_id` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:6'" in alter


def test_generate_migration_sql_returns_empty_when_aligned(db, conn, testpb):
    conn.queue_rows(
        [(1,)],
        [
            ("id", "int unsigned", "pb:1"),
            ("ip", "mediumtext", "pb:2"),
            ("port", "int unsigned", "pb:3"),
            ("group_id", "int unsigned", "pb:4"),
            ("player", "mediumblob", "pb:5"),
            ("player_id", "bigint unsigned", "pb:6"),
        ],
    )
    assert db.generate_migration_sql(testpb.golang_test) == ""
