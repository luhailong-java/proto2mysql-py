"""DB 层测试：用假 DB-API 连接断言"发出去的语句"，不需要真 MySQL。

真库集成测试见 test_integration_mysql.py（需要 PROTO2MYSQL_DSN 才会跑）。
"""

from __future__ import annotations

import pytest

from fakedb import FakeConnection, FakeIntegrityError

from proto2mysql import DB, DictCache
from proto2mysql.cache import decode_entry, encode_entry
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


@pytest.fixture()
def compositepb():
    """两段字符串复合主键 + 列表包装，专测分隔符与 tuple-IN。"""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.name = "db_composite_probe.proto"
    fdp.package = "dbprobe"
    fdp.syntax = "proto3"

    row = fdp.message_type.add()
    row.name = "pair_row"
    for number, (name, kind) in enumerate(
        (
            ("left", descriptor_pb2.FieldDescriptorProto.TYPE_STRING),
            ("right", descriptor_pb2.FieldDescriptorProto.TYPE_STRING),
            ("score", descriptor_pb2.FieldDescriptorProto.TYPE_INT64),
        ),
        1,
    ):
        field = row.field.add()
        field.name = name
        field.number = number
        field.type = kind
        field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

    rows = fdp.message_type.add()
    rows.name = "pair_rows"
    field = rows.field.add()
    field.name = "items"
    field.number = 1
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    field.type_name = ".dbprobe.pair_row"
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED

    file_desc = descriptor_pool.DescriptorPool().Add(fdp)
    return (
        message_factory.GetMessageClass(file_desc.message_types_by_name["pair_row"]),
        message_factory.GetMessageClass(file_desc.message_types_by_name["pair_rows"]),
    )


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


def test_open_db_publishes_new_binding_only_after_use_succeeds(conn, testpb):
    d = DB(conn, "old_db")
    d.register_table(testpb.golang_test)
    other = FakeConnection()
    other.raise_on_sql = ("USE", RuntimeError("cannot select database"))

    with pytest.raises(RuntimeError, match="cannot select"):
        d.open_db(other, "new_db")

    assert d.connection is conn
    assert d.dbname == "old_db"


def test_open_db_clears_database_scoped_schema_caches(conn, testpb):
    d = DB(conn, "db_a")
    d.register_table(testpb.golang_test)
    conn.queue_rows([(1,)])
    assert d.is_table_exists("golang_test") is True

    other = FakeConnection()
    d.open_db(other, "db_b")
    other.queue_rows([(0,)])

    assert d.is_table_exists("golang_test") is False
    assert any("INFORMATION_SCHEMA.TABLES" in sql for sql, _ in other.executed)


def test_bound_databases_do_not_share_column_metadata_cache(conn, testpb):
    root = DB(None, "testdb")
    root.register_table(testpb.golang_test)
    first = root.bind(conn)
    other_conn = FakeConnection()
    second = root.bind(other_conn)
    conn.queue_rows([("id", "int unsigned")])
    other_conn.queue_rows([("id", "bigint unsigned")])

    assert first.get_table_columns(testpb.golang_test.DESCRIPTOR.full_name)["id"] == "int unsigned"
    assert second.get_table_columns(testpb.golang_test.DESCRIPTOR.full_name)["id"] == "bigint unsigned"


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


def test_find_all_by_composite_pk_uses_full_tuple(conn, compositepb):
    from proto2mysql import with_primary_key, with_table_name

    row_type, list_type = compositepb
    d = DB(conn, "testdb")
    d.register_table(
        row_type,
        with_table_name("pair_row"),
        with_primary_key("left", "right"),
    )
    conn.queue_rows([("a", "b", 1), ("c", "d", 2)])
    out = list_type()

    d.find_all_by_pk_in(out, [("a", "b"), ("c", "d")])

    assert conn.last_sql().endswith(
        "WHERE (`left`, `right`) IN ((%s, %s), (%s, %s))"
    )
    assert conn.last_args() == ("a", "b", "c", "d")
    assert [(row.left, row.right) for row in out.items] == [("a", "b"), ("c", "d")]


def test_find_all_by_composite_pk_rejects_scalar_values(conn, compositepb):
    from proto2mysql import with_primary_key, with_table_name

    row_type, list_type = compositepb
    d = DB(conn, "testdb")
    d.register_table(
        row_type,
        with_table_name("pair_row"),
        with_primary_key("left", "right"),
    )

    with pytest.raises(Proto2MySQLError, match="必须是等长的元组"):
        d.find_all_by_pk_in(list_type(), ["a:b"])

    assert not conn.executed


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


def test_batch_delete_invalidates_each_chunk_even_when_later_chunk_fails(
    db, conn, testpb
):
    rows = [testpb.golang_test(id=i) for i in range(1, 1002)]
    cache = DictCache()
    db.enable_cache(cache)
    cache.set(db.cache_key(rows[0]), b"old-first", None)
    cache.set(db.cache_key(rows[-1]), b"old-last", None)
    conn.raise_on_execute_number = (2, RuntimeError("second chunk failed"))

    with pytest.raises(RuntimeError, match="second chunk"):
        db.batch_delete(rows)

    assert len(cache) == 0


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


def test_transaction_starts_even_when_connection_has_no_begin_method(db, conn):
    """DB-API 不保证有 begin()；不能静默退化成每句自动提交。"""
    conn.begin = None

    with db.transaction():
        pass

    assert conn.executed[0] == ("START TRANSACTION", ())


def test_transaction_rolls_back(db, conn, testpb):
    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            tx.insert(testpb.golang_test(id=1))
            raise RuntimeError("boom")
    assert conn.rollbacks == 1 and conn.commits == 0


def test_commit_ack_loss_still_invalidates_transaction_cache(db, conn, testpb):
    """COMMIT 抛错不等于服务端没提交；旧缓存必须保守删除。"""
    cache = DictCache()
    db.enable_cache(cache)
    row = testpb.golang_test(id=9, ip="new")
    cache.set(db.cache_key(row), b"stale", None)
    def lose_ack_and_connection_state():
        conn.commits += 1
        # 模拟断线后驱动连 autocommit 状态也读不出来；删缓存不能依赖它。
        conn.autocommit = None
        raise RuntimeError("commit ACK lost")

    conn.commit = lose_ack_and_connection_state

    with pytest.raises(RuntimeError, match="ACK lost"):
        with db.transaction() as tx:
            tx.update(row)

    assert len(cache) == 0


def test_nested_transaction_rejected(db):
    with db.transaction() as tx:
        with pytest.raises(Proto2MySQLError):
            with tx.transaction():
                pass


def test_nested_transaction_through_original_db_is_rejected(db, conn):
    """事务属于连接；绕回父 wrapper 不能再次 BEGIN 并提前提交外层。"""
    with db.transaction():
        with pytest.raises(Proto2MySQLError, match="nested transaction"):
            with db.transaction():
                pass

    assert conn.commits == 1


def test_transaction_rejects_raw_external_transaction(db, conn):
    """autocommit=True 上直接 conn.begin() 后再 begin 会隐式提交外部事务。"""
    conn.begin()

    with pytest.raises(Proto2MySQLError, match="外部事务"):
        with db.transaction():
            pass

    assert conn.begins == 1
    assert conn.commits == 0
    conn.rollback()


def test_managed_transaction_rejects_manual_commit_and_rollback(db, conn):
    with db.transaction() as tx:
        for wrapper, method in (
            (db, "commit"),
            (tx, "commit"),
            (db, "rollback"),
            (tx, "rollback"),
        ):
            with pytest.raises(Proto2MySQLError, match="managed transaction"):
                getattr(wrapper, method)()

    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_managed_transaction_rejects_rebinding_or_closing_connection(db, conn):
    other = FakeConnection()

    with db.transaction() as tx:
        for wrapper in (db, tx):
            with pytest.raises(Proto2MySQLError, match="managed transaction"):
                wrapper.open_db(other, "otherdb")
            with pytest.raises(Proto2MySQLError, match="managed transaction"):
                wrapper.close()

    assert db.connection is conn
    assert conn.closed is False
    assert other.executed == []


def test_managed_transaction_rejects_enabling_or_switching_cache(db, conn):
    """事务中换 cache backend 会让 pending 在提交时被错误 wrapper 冲刷。"""
    first = DictCache()
    second = DictCache()
    db.enable_cache(first)

    with db.transaction() as tx:
        for wrapper, cache in ((db, second), (tx, second)):
            with pytest.raises(Proto2MySQLError, match="managed transaction"):
                wrapper.enable_cache(cache)

    assert db._cache is first


def test_managed_transaction_rejects_other_wrapper_with_different_cache(
    db, conn, testpb
):
    """直接构造同连接 wrapper 也不能把失效项交给错误 backend。"""
    first = DictCache()
    db.enable_cache(first)
    other_cache = DB(conn, "testdb", tables=db.tables, cache=DictCache())
    no_cache = DB(conn, "testdb", tables=db.tables)

    with db.transaction():
        for wrapper in (other_cache, no_cache):
            with pytest.raises(Proto2MySQLError, match="cache backend"):
                wrapper.update(testpb.golang_test(id=9, ip="new"))


def test_for_update_requires_transaction(db, testpb):
    with pytest.raises(Proto2MySQLError):
        db.find_one_by_pk_for_update(testpb.golang_test(id=1))


def test_for_update_accepts_original_db_while_same_connection_is_in_transaction(
    db, conn, testpb
):
    """父 wrapper 与 tx 共用连接，父 wrapper 发出的锁查询也处于同一事务。"""
    conn.queue_rows([(9, "locked", 0, 0, b"", 0)])

    with db.transaction():
        out = testpb.golang_test(id=9)
        db.find_one_by_pk_for_update(out)

    assert out.ip == "locked"
    assert "FOR UPDATE" in conn.last_sql()


def test_query_options_for_update_is_rejected_outside_transaction(db, conn, testpb):
    from proto2mysql import QueryOptions

    opts = QueryOptions(for_update=True)
    with pytest.raises(Proto2MySQLError, match="FOR UPDATE"):
        db.find_one_with_options(testpb.golang_test(), "`id` = ?", [9], opts)
    with pytest.raises(Proto2MySQLError, match="FOR UPDATE"):
        db.find_all_with_options(testpb.golang_test_list(), "1=1", None, opts)

    assert not conn.executed


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
    cache.set(db.cache_key(testpb.golang_test(id=9)), b"x", None)

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


def test_update_ack_loss_still_invalidates_cache(db, conn, testpb):
    cache = DictCache()
    db.enable_cache(cache)
    row = testpb.golang_test(id=9, ip="new")
    cache.set(db.cache_key(row), b"stale", None)
    conn.raise_on_sql = ("UPDATE", RuntimeError("statement ACK lost"))

    with pytest.raises(RuntimeError, match="ACK lost"):
        db.update(row)

    assert len(cache) == 0


def test_manual_cache_invalidation_survives_database_connection_failure(
    db, conn, testpb
):
    cache = DictCache()
    db.enable_cache(cache)
    row = testpb.golang_test(id=9)
    cache.set(db.cache_key(row), b"stale", None)
    conn.autocommit = None  # 模拟断线后驱动状态不可读

    db.invalidate_cache(row)

    assert len(cache) == 0


def test_manual_cache_invalidation_inside_transaction_waits_for_commit(
    db, conn, testpb
):
    """手动失效也必须遵守事务边界，不能给旧值回填留下永久脏缓存窗口。"""
    cache = DictCache()
    db.enable_cache(cache)
    row = testpb.golang_test(id=9)
    cache.set(db.cache_key(row), b"stale", None)

    with db.transaction() as tx:
        tx.invalidate_cache(row)
        assert len(cache) == 1, "COMMIT 前不能删除"

    assert len(cache) == 0, "提交尝试后才删除"


def test_manual_cache_invalidation_inside_transaction_is_discarded_on_rollback(
    db, conn, testpb
):
    """同连接父 wrapper 发起的手动失效也要挂到事务队列，回滚时保留。"""
    cache = DictCache()
    db.enable_cache(cache)
    row = testpb.golang_test(id=9)
    cache.set(db.cache_key(row), b"still-valid", None)

    with pytest.raises(RuntimeError, match="rollback"):
        with db.transaction():
            db.invalidate_cache(row)
            assert len(cache) == 1, "事务内父 wrapper 也不能提前删除"
            raise RuntimeError("rollback")

    assert len(cache) == 1


# ── 建表 / 迁移 ─────────────────────────────────────────────────────────


def test_insert_ignore_only_swallows_duplicate_key(db, conn, testpb):
    """幂等插入只吞 1062；不能用会吞截断/越界/NOT NULL 错误的 INSERT IGNORE。"""
    row = testpb.golang_test(id=7)

    conn.raise_on_execute = FakeIntegrityError(1062, "duplicate")
    assert db.insert_ignore(row) is False
    assert conn.executed[-1][0].startswith("INSERT INTO ")
    assert "INSERT IGNORE" not in conn.executed[-1][0]

    conn.raise_on_execute = FakeIntegrityError(1406, "data too long")
    with pytest.raises(FakeIntegrityError, match="data too long"):
        db.insert_ignore(row)

    conn.next_rowcount = 1
    assert db.insert_ignore(testpb.golang_test(id=8)) is True


def test_save_updates_by_primary_key_without_replace_or_odku(db, conn, testpb):
    """现存主键走精确 UPDATE，既不 REPLACE，也不让备用 UNIQUE 劫持 ODKU。

    REPLACE 是 DELETE+INSERT，语句里没提到的列会**回到默认值**；而列清单来自本进程的
    descriptor，所以滚动发布时旧版本 save 一次，新版本刚写进去的列就没了，且零报错。
    普通 ODKU 又会被任意二级 UNIQUE 触发，命中另一主键行时改错对象。
    """
    db.save(testpb.golang_test(id=7, ip="a"))
    sql = conn.last_sql()
    assert sql.startswith("UPDATE `golang_test` SET ")
    assert "WHERE `id` = %s" in sql
    assert "ON DUPLICATE KEY UPDATE" not in sql
    assert "REPLACE" not in sql
    # 零值也要写进去——save 的语义是「整行落库」，不是「只写非零字段」
    assert "`port` = %s" in sql


def test_save_inserts_when_primary_key_is_missing(db, conn, testpb):
    conn.queue_rowcounts(0, 1)

    db.save(testpb.golang_test(id=7, ip="a"))

    assert [sql.split(" ", 1)[0] for sql, _ in conn.executed] == ["UPDATE", "INSERT"]
    assert "ON DUPLICATE KEY UPDATE" not in conn.executed[1][0]


def test_save_retries_update_after_concurrent_same_primary_key_insert(db, conn, testpb):
    conn.queue_rowcounts(0, 1)
    conn.raise_on_execute_number = (2, FakeIntegrityError(1062, "concurrent same pk"))

    db.save(testpb.golang_test(id=7, ip="a"))

    assert [sql.split(" ", 1)[0] for sql, _ in conn.executed] == ["UPDATE", "INSERT", "UPDATE"]


def test_save_accepts_unchanged_existing_row_after_zero_rowcount(db, conn, testpb):
    """默认 clientFoundRows=False 时，同值 UPDATE 返回 0 不能误报冲突。"""
    conn.queue_rowcounts(0, 0)
    conn.queue_rows([], [], [(1,)])
    conn.raise_on_execute_number = (2, FakeIntegrityError(1062, "same pk"))

    db.save(testpb.golang_test(id=7, ip="same"))

    assert conn.executed[-1][0].startswith("SELECT 1 ")


def test_save_rejects_secondary_unique_conflict_instead_of_updating_owner(
    db, conn, testpb
):
    conn.queue_rowcounts(0, 0)
    conn.queue_rows([], [], [], [])
    conn.raise_on_execute_number = (2, FakeIntegrityError(1062, "secondary unique"))

    with pytest.raises(DuplicateKeyError, match="secondary unique"):
        db.save(testpb.golang_test(id=8, ip="owned"))

    assert [sql.split(" ", 1)[0] for sql, _ in conn.executed] == [
        "UPDATE", "INSERT", "UPDATE", "SELECT"
    ]


def test_batch_save_uses_primary_key_state_machine_per_row(db, conn, testpb):
    db.batch_save([testpb.golang_test(id=1), testpb.golang_test(id=2)])
    assert len(conn.executed) == 2
    assert all(sql.startswith("UPDATE `golang_test`") for sql, _ in conn.executed)
    assert all("ON DUPLICATE KEY UPDATE" not in sql for sql, _ in conn.executed)


def test_batch_save_invalidates_each_attempted_row_even_when_later_row_fails(
    db, conn, testpb
):
    """事务外逐行非原子：前一行已落库，失败行结果不明，两者都保守失效。"""
    rows = [testpb.golang_test(id=i) for i in range(1, 4)]
    cache = DictCache()
    db.enable_cache(cache)
    for row in rows:
        cache.set(db.cache_key(row), b"old", None)
    conn.raise_on_execute_number = (2, RuntimeError("second row failed"))

    with pytest.raises(RuntimeError, match="second row"):
        db.batch_save(rows)

    assert cache.get(db.cache_key(rows[2])) == b"old"
    assert len(cache) == 1


def test_save_ack_loss_still_invalidates_candidate_cache(conn, testpb):
    """UPDATE/INSERT 的 ACK 丢失时，至少候选主键必须保守失效。"""
    d = DB(conn, "testdb")
    d.register_table(testpb.golang_test)
    cache = DictCache()
    d.enable_cache(cache)
    candidate = testpb.golang_test(id=8, ip="new")
    cache.set(d.cache_key(candidate), b"old-candidate", None)
    conn.raise_on_sql = ("UPDATE", RuntimeError("statement ACK lost"))

    with pytest.raises(RuntimeError, match="ACK lost"):
        d.save(candidate)

    assert len(cache) == 0


def test_replace_remains_available_as_escape_hatch(db, testpb):
    """整行推倒重来的旧语义保留为显式逃生口，只是不再是 save 的默认。"""
    table = db.tables[testpb.golang_test.DESCRIPTOR.full_name]
    assert table.get_replace_sql(testpb.golang_test(id=7)).sql.startswith("REPLACE INTO `golang_test`")


def test_cache_key_has_no_schema_version(db, testpb):
    """schema 版本刻意**不进 key**。

    把指纹拼进 key 看起来更省事，但失效路径与读路径共用 key 生成器：
    v1 写库后删的是自己那个指纹的 key，v2 缓存的那条永远没人删，
    于是「读到残缺数据」被升级成「跨版本永久脏读」。版本信息放在 value 头部。
    """
    key = db.cache_key(testpb.golang_test(id=9))
    assert key == "pb:testdb:golang_test:9"
    assert "P2MC" not in key  # 信封（含字段集指纹）只在 value 里


def test_cache_key_is_namespaced_by_database_by_default(testpb, conn):
    """共享一套缓存的两个数据库，默认不能让同表同主键串号。"""
    from proto2mysql import DB

    d1 = DB(conn, "game_s1")
    d1.register_table(testpb.golang_test)
    d2 = DB(conn, "game_s2")
    d2.register_table(testpb.golang_test)
    row = testpb.golang_test(id=1)

    assert d1.cache_key(row) == "pb:game_s1:golang_test:1"
    assert d1.cache_key(row) != d2.cache_key(row)


def test_unsafe_legacy_cache_key_mode_requires_explicit_opt_in(
    db, testpb, monkeypatch
):
    """旧 key 只作为显式迁移逃生口，绝不能重新成为默认值。"""
    import proto2mysql.db as db_module

    assert db_module.CACHE_KEY_NAMESPACED is True
    monkeypatch.setattr(db_module, "CACHE_KEY_NAMESPACED", False)

    assert db.cache_key(testpb.golang_test(id=9)) == "pb:golang_test:9"


def test_namespaced_cache_write_also_deletes_legacy_key(db, conn, testpb):
    """新版本只读安全 key，但写后双删旧 key，避免滚动升级留下永久脏条目。"""
    cache = DictCache()
    db.enable_cache(cache)
    row = testpb.golang_test(id=9, ip="new")
    cache.set(db.cache_key(row), b"new-format", None)
    cache.set("pb:golang_test:9", b"legacy-format", None)

    db.update(row)

    assert len(cache) == 0


def test_enable_cache_refuses_implicit_transaction_connection(db, conn):
    """外部 ``conn.commit()`` 无法被包装器观察，不能承诺提交后的二次失效。"""
    conn.autocommit = False

    with pytest.raises(Proto2MySQLError, match="autocommit=True"):
        db.enable_cache(DictCache())


def test_enable_cache_requires_database_namespace_on_bound_connection(conn, testpb):
    d = DB(conn, "")
    d.register_table(testpb.golang_test)

    with pytest.raises(Proto2MySQLError, match="dbname"):
        d.enable_cache(DictCache())


def test_bind_refuses_implicit_transaction_connection_when_cache_is_configured(
    conn, testpb
):
    """先在无连接注册表上配缓存、后 bind 的推荐顺序也必须守同一门禁。"""
    root = DB(None, "testdb")
    root.register_table(testpb.golang_test)
    root.enable_cache(DictCache())
    conn.autocommit = False

    with pytest.raises(Proto2MySQLError, match="autocommit=True"):
        root.bind(conn)


def test_open_db_refuses_implicit_transaction_connection_when_cache_is_configured(
    conn, testpb
):
    root = DB(None, "")
    root.register_table(testpb.golang_test)
    root.enable_cache(DictCache())
    conn.autocommit = False

    with pytest.raises(Proto2MySQLError, match="autocommit=True"):
        root.open_db(conn, "testdb")

    assert not conn.executed


def test_cache_path_refuses_connection_switched_to_implicit_transaction(
    db, conn, testpb
):
    """连接在 enable_cache 后切换模式时也不能绕过配置期门禁。"""
    db.enable_cache(DictCache())
    conn.autocommit = False

    with pytest.raises(Proto2MySQLError, match="autocommit=True"):
        db.find_one_by_pk(testpb.golang_test(id=9))

    assert not conn.executed


def test_cache_path_refuses_raw_transaction_with_autocommit_flag_still_true(
    db, conn, testpb
):
    """``conn.begin()`` 不会切换 PyMySQL 的 autocommit 标志，也不能绕过门禁。"""
    db.enable_cache(DictCache())
    # MySQL 协议 SERVER_STATUS_IN_TRANS=1、SERVER_STATUS_AUTOCOMMIT=2。
    # PyMySQL 在 autocommit=True 连接上 begin() 后正是这个状态。
    conn.server_status = 3

    with pytest.raises(Proto2MySQLError, match="外部事务"):
        db.find_one_by_pk(testpb.golang_test(id=9))

    assert not conn.executed


def test_shared_implicit_connection_cannot_bypass_cache_gate_via_direct_wrappers(
    conn, testpb
):
    """多个 wrapper 也不能把 pending 挂错后由另一个 wrapper 漏删。

    直接给构造器传 ``cache=`` 会绕过 ``enable_cache`` / ``bind`` 的配置期检查，
    所以执行入口还必须 fail-closed；这样在隐式事务上根本不会发出缓存写操作，
    也就不存在“谁 commit、谁冲刷 pending”的不可靠状态。
    """
    conn.autocommit = False
    cache = DictCache()
    tables = {}
    first = DB(conn, "testdb", tables=tables, cache=cache)
    first.register_table(testpb.golang_test)
    second = DB(conn, "testdb", tables=tables, cache=cache)

    for wrapper in (first, second):
        with pytest.raises(Proto2MySQLError, match="autocommit=True"):
            wrapper.update(testpb.golang_test(id=9, ip="new"))

    assert not conn.executed


def test_composite_cache_key_is_unambiguous(conn, compositepb):
    """复合主键的值里带冒号时不能撞 key。

    旧格式裸冒号拼接：``("a:b", "c")`` 与 ``("a", "b:c")`` 产出同一个
    ``pb:combo_key:a:b:c``。撞上之后读回来的**整行**（含主键）是另一条记录，
    而且零日志——调用方只会看到"这个玩家的数据不对"。
    """
    from proto2mysql import with_primary_key, with_table_name

    row_type, _ = compositepb
    d = DB(conn, "testdb")
    d.register_table(
        row_type,
        with_table_name("pair_row"),
        with_primary_key("left", "right"),
    )
    a = row_type(left="a:b", right="c")
    b = row_type(left="a", right="b:c")
    assert d.cache_key(a) != d.cache_key(b)


def test_cache_invalidation_deletes_truly_legacy_unescaped_composite_key(
    conn, compositepb
):
    from proto2mysql import with_primary_key, with_table_name

    row_type, _ = compositepb
    d = DB(conn, "testdb")
    d.register_table(
        row_type,
        with_table_name("pair_row"),
        with_primary_key("left", "right"),
    )
    cache = DictCache()
    d.enable_cache(cache)
    row = row_type(left="a:b", right="c", score=1)
    cache.set(d.cache_key(row), b"new-format", None)
    # CACHE_KEY_NAMESPACED=False 的迁移窗口产出的中间格式：无库名、但已转义。
    cache.set("pb:pair_row:a%3Ab:c", b"compat-escaped", None)
    cache.set("pb:pair_row:a:b:c", b"legacy-unescaped", None)

    d.update(row)

    assert len(cache) == 0


def test_cache_key_escapes_separator_in_values(db, testpb):
    """只在**含分隔符**的主键上换 key，其余逐字节不变。

    不转义的话 ``("x:y","z")`` 与 ``("x","y:z")`` 会落到同一个 key 上，
    命中时返回的是另一行的整条 protobuf（含主键），而且两边互相投毒——
    静默的跨行脏读，库里数据自始至终是对的，零日志零异常。
    """
    from proto2mysql.db import _escape_cache_key_part

    assert _escape_cache_key_part("10086") == "10086"       # 绝大多数 key 一字不变
    assert _escape_cache_key_part("a:b") == "a%3Ab"
    assert _escape_cache_key_part("100%") == "100%25"
    # % 必须先转，否则 ':' 转出来的 %3A 会被二次处理
    assert _escape_cache_key_part("%:") == "%25%3A"
    assert _escape_cache_key_part("x:y") != _escape_cache_key_part("x") + ":y"


def test_cache_entry_from_older_schema_is_a_miss(db, conn, testpb):
    """写入方认识的字段比我少 → 当未命中回源，而不是把新字段静默读成零值。"""
    cache = DictCache()
    db.enable_cache(cache)
    table = db.tables[testpb.golang_test.DESCRIPTOR.full_name]

    # 模拟旧版本进程写的条目：它不认识 player_id(pb:6)
    old_fields = table.field_numbers - {6}
    stale = testpb.golang_test(id=9, ip="from-old-writer")
    cache.set(db.cache_key(stale), encode_entry(old_fields, stale.SerializeToString()), None)

    conn.queue_rows([(9, "from-db", 0, 0, b"", 77)])
    out = testpb.golang_test(id=9)
    db.find_one_by_pk(out)
    assert out.ip == "from-db", "残缺条目必须回源，不能直接采用"
    assert out.player_id == 77


def test_cache_entry_from_newer_schema_is_usable(db, conn, testpb):
    """写入方认识得更多 → 可以直接用（多出来的字段对我就是 unknown fields）。

    所以 miss 是**单向**的，滚动发布期间的雪崩面比"把指纹拼进 key"小得多。
    """
    cache = DictCache()
    db.enable_cache(cache)
    table = db.tables[testpb.golang_test.DESCRIPTOR.full_name]

    fresh = testpb.golang_test(id=9, ip="from-newer-writer")
    newer_fields = table.field_numbers | {999}
    cache.set(db.cache_key(fresh), encode_entry(newer_fields, fresh.SerializeToString()), None)

    # 没给 conn 排任何结果集：一旦回源就会抛 NoRowsFoundError，所以能读到值即证明命中缓存。
    out = testpb.golang_test(id=9)
    db.find_one_by_pk(out)
    assert out.ip == "from-newer-writer"


def test_corrupt_cache_entry_does_not_clobber_primary_key(db, conn, testpb):
    """解析失败不能把调用方 message 的主键清成 0。

    原先是先 Clear() 再 ParseFromString()：解析一失败，主键已经没了，
    上层拿着 id=0 回去查库，于是查错行/查不到，且完全看不出根因。
    """
    cache = DictCache()
    db.enable_cache(cache)
    table = db.tables[testpb.golang_test.DESCRIPTOR.full_name]
    cache.set(db.cache_key(testpb.golang_test(id=9)), encode_entry(table.field_numbers, b"\xff\xff\xff"), None)

    conn.queue_rows([(9, "from-db", 0, 0, b"", 0)])
    out = testpb.golang_test(id=9)
    db.find_one_by_pk(out)
    assert out.id == 9, "解析失败后主键必须还在"
    assert out.ip == "from-db"


def test_legacy_bare_proto_entry_is_a_miss(db, conn, testpb):
    """老版本写的裸 pb 字节（无信封）当未命中——安全，且会被下次写覆盖。"""
    cache = DictCache()
    db.enable_cache(cache)
    stale = testpb.golang_test(id=9, ip="bare-bytes")
    cache.set(db.cache_key(stale), stale.SerializeToString(), None)

    conn.queue_rows([(9, "from-db", 0, 0, b"", 0)])
    out = testpb.golang_test(id=9)
    db.find_one_by_pk(out)
    assert out.ip == "from-db"


def test_cache_entry_roundtrip():
    """信封编解码；两语言必须逐字节一致（可能共用同一个 Redis）。"""
    payload = b"\x08\x2a"
    blob = encode_entry([3, 1, 2], payload)
    assert blob.startswith(b"P2MC\x01")
    fields, out = decode_entry(blob)
    assert fields == frozenset({1, 2, 3})
    assert out == payload
    # 字段号必须升序写入，保证同一集合产出同一份字节
    assert encode_entry([3, 1, 2], payload) == encode_entry([1, 2, 3], payload)
    assert decode_entry(b"not-an-envelope") is None
    assert decode_entry(b"P2MC\x99") is None  # 版本不认


def col_row(name, col_type, pb, *, nullable="NO", extra="", default=None):
    """构造 get_table_column_meta 那条 SELECT 的一行。

    形状是 ``(COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, IS_NULLABLE, EXTRA, COLUMN_DEFAULT)``。
    后三列是 2026-08 补上的：COLUMN_TYPE 里看不出可空不可空、有没有 AUTO_INCREMENT，
    只比 COLUMN_TYPE 会把这几类漂移全漏掉（见 table.attribute_drift）。
    """
    return (name, col_type, pb, nullable, extra, default)


#: golang_test 完全对齐时线上应有的列。属性要与 get_mysql_field_type 的产出对得上，
#: 否则会被判成属性漂移、凭空长出 MODIFY 子句：
#:   id        -> int unsigned NOT NULL AUTO_INCREMENT
#:   ip        -> MEDIUMTEXT（不带 NOT NULL，所以线上必须是可空的）
#:   port 等   -> int unsigned NOT NULL DEFAULT 0
ALIGNED_COLS = [
    col_row("id", "int unsigned", "pb:1", extra="auto_increment"),
    col_row("ip", "mediumtext", "pb:2", nullable="YES"),
    col_row("port", "int unsigned", "pb:3", default="0"),
    col_row("group_id", "int unsigned", "pb:4", default="0"),
    col_row("player", "mediumblob", "pb:5", nullable="YES"),
    col_row("player_id", "bigint unsigned", "pb:6", default="0"),
]
PRIMARY_ID = [("id", None)]
NO_PRIMARY = []


def test_sync_creates_table_when_missing(db, conn, testpb):
    conn.queue_rows(
        [(0,)],  # information_schema: 表不存在
        [],  # CREATE 自身
        ALIGNED_COLS,  # 建完之后回读，结构已经对齐
        PRIMARY_ID,  # 有主键
    )
    db.create_or_update_table(testpb.golang_test)
    creates = [s for s, _ in conn.executed if s.startswith("CREATE TABLE IF NOT EXISTS `golang_test`")]
    assert len(creates) == 1
    # 建完就对齐了，不应再发 ALTER。
    assert not [s for s, _ in conn.executed if s.startswith("ALTER TABLE")]


def test_sync_still_aligns_when_create_was_a_noop(db, conn, testpb):
    """并发冷启动：CREATE TABLE IF NOT EXISTS 整条是 no-op，仍必须补列。

    时序是两个版本的进程同时冷启动到空库：本进程检查时表还不存在，检查与 CREATE
    之间另一个进程按**它自己的（较旧的）** proto 建好了表。本进程这条 CREATE
    只会拿到一条 Warning 1050——不报错、不改结构。

    早先这里建完直接 return，于是本进程独有的新列从未被添加，而进程**启动成功、
    零异常**，一直到第一条 SELECT 才报 Error 1054 Unknown column；且表存在性缓存
    已置 True，重启也不重新对齐，不自愈。
    """
    conn.queue_rows(
        [(0,)],  # 检查时表还不存在
        [],  # CREATE 执行（实际是 no-op，因为已被别人建走）
        ALIGNED_COLS[:-1],  # 回读到别人建的旧结构：缺 player_id
        PRIMARY_ID,  # 有主键
    )
    db.create_or_update_table(testpb.golang_test)
    alters = [s for s, _ in conn.executed if s.startswith("ALTER TABLE")]
    assert len(alters) == 1
    assert "ADD COLUMN `player_id` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:6'" in alters[0]


def test_sync_alters_when_column_missing(db, conn, testpb):
    conn.queue_rows(
        [(1,)],  # 表存在
        [  # 现有列：缺 player_id，且 ip 类型不兼容
            col_row("id", "int unsigned", "pb:1", extra="auto_increment"),
            col_row("ip", "int", "pb:2", nullable="YES"),
            col_row("port", "int unsigned", "pb:3", default="0"),
            col_row("group_id", "int unsigned", "pb:4", default="0"),
            col_row("player", "mediumblob", "pb:5", nullable="YES"),
        ],
        PRIMARY_ID,  # 有主键
    )
    db.create_or_update_table(testpb.golang_test)
    # ALTER 之后还会有一条就绪回读（TiDB 的 DDL 是异步的），所以不能假设它是最后一条
    alter = next(s for s, _ in conn.executed if s.startswith("ALTER TABLE `golang_test` "))
    assert "MODIFY COLUMN `ip` MEDIUMTEXT COMMENT 'pb:2'" in alter
    assert "ADD COLUMN `player_id` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:6'" in alter


def test_sync_restores_missing_auto_increment_attribute(db, conn, testpb):
    drifted = list(ALIGNED_COLS)
    drifted[0] = col_row("id", "int unsigned", "pb:1", extra="")
    conn.queue_rows([(1,)], drifted, PRIMARY_ID)

    db.create_or_update_table(testpb.golang_test)

    alter = next(s for s, _ in conn.executed if s.startswith("ALTER TABLE"))
    assert "MODIFY COLUMN `id` int unsigned NOT NULL AUTO_INCREMENT" in alter


def test_sync_restores_nullable_attribute(db, conn, testpb):
    drifted = list(ALIGNED_COLS)
    drifted[1] = col_row("ip", "mediumtext", "pb:2", nullable="NO")
    conn.queue_rows([(1,)], drifted, PRIMARY_ID)

    db.create_or_update_table(testpb.golang_test)

    alter = next(s for s, _ in conn.executed if s.startswith("ALTER TABLE"))
    assert "MODIFY COLUMN `ip` MEDIUMTEXT COMMENT 'pb:2'" in alter


def test_generate_migration_sql_returns_empty_when_aligned(db, conn, testpb):
    conn.queue_rows(
        [(1,)],  # 表存在
        ALIGNED_COLS,
        PRIMARY_ID,  # 有主键
    )
    assert db.generate_migration_sql(testpb.golang_test) == ""


def test_generate_migration_sql_fails_closed_on_timestamp_datetime_drift(
    conn, kitchenpb
):
    """公共迁移入口不能把同精度 TIMESTAMP / DATETIME 漂移报告成空串。

    两者的时区转换和取值范围语义不同；若 ``generate_migration_sql`` 返回空串，
    调用方会把它解释成“结构已经对齐”，从而把危险漂移永久留在线上。
    """
    d = DB(conn, "testdb")
    d.register_table(kitchenpb.kitchen_sink)
    conn.queue_rows(
        [(1,)],
        [col_row("created_at", "timestamp(6)", "pb:11", nullable="YES")],
    )

    with pytest.raises(
        Proto2MySQLError, match="TIMESTAMP.*DATETIME|DATETIME.*TIMESTAMP"
    ):
        d.generate_migration_sql(kitchenpb.kitchen_sink)


def test_generate_migration_sql_covers_index_and_primary_key(indexed_db, conn, testpb):
    """离线迁移 SQL 必须和在线路径**算同一份差异**。

    早先 generate_migration_sql 只调了 build_alter_clauses：索引和主键一个都不算，
    于是「库里缺唯一键、缺主键」时它返回**空串**——而空串的意思是"无需迁移"。
    审核者拿着这份空的迁移文件签字，缺的约束就这么上线了。
    """
    conn.queue_rows(
        [(1,)],  # 表存在
        ALIGNED_COLS,
        [],      # 线上一个索引都没有
        NO_PRIMARY,  # 也没有主键
    )
    sql = indexed_db.generate_migration_sql(testpb.golang_test)
    assert "ADD INDEX `idx_golang_test_0`" in sql
    assert "ADD UNIQUE KEY `uk_golang_test`" in sql
    # 补主键单独成一条：自增列必须与 ADD PRIMARY KEY 同句，且它可能单独失败
    assert "ADD PRIMARY KEY (`id`);" in sql
    assert len([line for line in sql.splitlines() if line.startswith("ALTER TABLE")]) == 2


def test_wrong_primary_key_signature_fails_closed(db, conn, testpb):
    """PRIMARY 存在不代表正确；列或顺序漂移时不能返回空迁移。"""
    conn.queue_rows(
        [(1,)],
        ALIGNED_COLS,
        [("ip", 191)],  # 线上 PRIMARY 错建在 ip，而 proto 要 id
    )

    with pytest.raises(Proto2MySQLError, match="PRIMARY.*ip.*id"):
        db.generate_migration_sql(testpb.golang_test)


# ── DDL 咨询锁（P2-1） ───────────────────────────────────────────────────


def test_sync_all_tables_takes_advisory_lock(db, conn, testpb):
    """结构同步全程持一把 GET_LOCK，避免多副本同时 ALTER 撞 Error 1060。"""
    conn.queue_rows(
        [(1,)],  # GET_LOCK 返回 1
        [(1,)],  # 表存在
        ALIGNED_COLS,  # 结构已对齐
        PRIMARY_ID,  # 有主键
        [(1,)],  # 表存在（第二张表 golang_test_list）
        [],  # 它的列（走不到实际对齐，返回空即可）
        PRIMARY_ID,
        [(1,)],  # RELEASE_LOCK
    )
    db.sync_all_tables()
    sqls = [s for s, _ in conn.executed]
    assert any("GET_LOCK" in s for s in sqls), "必须先抢锁"
    assert any("RELEASE_LOCK" in s for s in sqls), "必须释放锁"
    assert sqls.index(next(s for s in sqls if "GET_LOCK" in s)) == 0, "抢锁要在最前"


def test_sync_degrades_when_lock_unavailable(db, conn, testpb, caplog):
    """拿不到锁只降级 + 告警，不阻断——TiDB 等兼容实现不一定支持 GET_LOCK。

    可用性比"锁一定要拿到"更重要：拿不到锁最坏是撞 Error 1060、重启自愈；
    而因为拿不到锁就拒绝启动，是把一个并发问题升级成可用性事故。
    """
    conn.queue_rows(
        [(0,)],  # GET_LOCK 返回 0 = 等超时了
        [(1,)],  # 表存在
        ALIGNED_COLS,
        PRIMARY_ID,
        [(1,)],
        [],
        [(1,)],
    )
    with caplog.at_level("WARNING", logger="proto2mysql"):
        db.sync_all_tables()
    assert any("未取得" in r.getMessage() for r in caplog.records)
    # 没拿到锁就不该去释放
    assert not any("RELEASE_LOCK" in s for s, _ in conn.executed)


def test_sync_rechecks_after_concurrent_duplicate_column(db, conn, testpb):
    """无锁并发下另一副本先补完列，Error 1060 应回读自愈。"""
    conn.queue_rows(
        [(1,)],
        ALIGNED_COLS[:-1],
        PRIMARY_ID,
        ALIGNED_COLS,  # 1060 后回读：另一副本已经补齐
        PRIMARY_ID,
    )
    conn.raise_on_sql = ("ALTER TABLE", FakeIntegrityError(1060, "Duplicate column"))

    db.create_or_update_table(testpb.golang_test)

    assert len([sql for sql, _ in conn.executed if sql.startswith("ALTER TABLE")]) == 1


# ── 索引补齐（P2-5） ─────────────────────────────────────────────────────


@pytest.fixture()
def indexed_db(conn, testpb):
    """注册一张声明了普通索引与唯一键的表。"""
    from proto2mysql import with_indexes, with_unique_key

    d = DB(conn, "testdb")
    d.register_table(testpb.golang_test, with_indexes("player_id"), with_unique_key("ip"))
    return d


def test_sync_backfills_missing_indexes(indexed_db, conn, testpb):
    """早先索引只出现在 CREATE TABLE 分支。

    表一旦建成，之后在 .proto 里新加 index / unique_key **完全不生效**，且零提示——
    查询照常能跑，只是走全表扫描，数据量上来才表现为"莫名其妙变慢"。
    """
    conn.queue_rows(
        [(1,)],  # 表存在
        ALIGNED_COLS,  # 列已对齐
        [],  # 既有索引：一个都没有
        PRIMARY_ID,  # 有主键
    )
    indexed_db.create_or_update_table(testpb.golang_test)
    alter = next(s for s, _ in conn.executed if s.startswith("ALTER TABLE `golang_test`"))
    assert "ADD INDEX `idx_golang_test_0` (`player_id`)" in alter
    # ip 是 MEDIUMTEXT，索引必须带前缀长度，否则 MySQL 报 Error 1170
    assert "ADD UNIQUE KEY `uk_golang_test` (`ip`(191))" in alter


def test_sync_rechecks_after_concurrent_duplicate_index(indexed_db, conn, testpb):
    """无锁并发下另一副本先补完索引，Error 1061 也应回读自愈。"""
    conn.queue_rows(
        [(1,)],
        ALIGNED_COLS,
        [],
        PRIMARY_ID,
        ALIGNED_COLS,
        LIVE_INDEXES,
        PRIMARY_ID,
    )
    conn.raise_on_sql = ("ALTER TABLE", FakeIntegrityError(1061, "Duplicate key name"))

    indexed_db.create_or_update_table(testpb.golang_test)

    assert len([sql for sql, _ in conn.executed if sql.startswith("ALTER TABLE")]) == 1


#: INFORMATION_SCHEMA.STATISTICS 的行形状：
#: (INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART)
LIVE_INDEXES = [
    ("idx_golang_test_0", 1, 1, "player_id", None),
    ("uk_golang_test", 0, 1, "ip", 191),
    ("idx_dba_added_by_hand", 1, 1, "port", None),
]


def test_sync_skips_existing_indexes(indexed_db, conn, testpb):
    """线上已有的索引不重复加；线上多出来的索引也不删（可能是 DBA 手工加的）。"""
    conn.queue_rows([(1,)], ALIGNED_COLS, LIVE_INDEXES, PRIMARY_ID)
    indexed_db.create_or_update_table(testpb.golang_test)
    assert not [s for s, _ in conn.executed if s.startswith("ALTER TABLE")], "全都在，不该发 ALTER"


def test_same_name_non_unique_index_fails_closed(indexed_db, conn, testpb):
    """线上有个**同名但非唯一**的索引时，不能判成"唯一键已经有了"。

    早先只比索引名：名字对上就 continue。于是唯一约束事实上根本不存在，
    而业务正按「有唯一键」在写，一直到某天发现重复数据才会暴露。

    本库「只加不删」，不擅自 DROP DBA 的索引；但必须阻断启动，
    不能让业务在「以为有唯一约束」的错误前提下继续写入。
    """
    drifted = [
        ("idx_golang_test_0", 1, 1, "player_id", None),
        ("uk_golang_test", 1, 1, "ip", 191),  # ← 非唯一，冒充唯一键
    ]
    conn.queue_rows([(1,)], ALIGNED_COLS, drifted, PRIMARY_ID)
    with pytest.raises(Proto2MySQLError, match="uk_golang_test"):
        indexed_db.create_or_update_table(testpb.golang_test)
    assert not [s for s, _ in conn.executed if s.startswith("ALTER TABLE")]


def test_same_name_index_on_different_columns_fails_closed(indexed_db, conn, testpb):
    """同名但列不同 / 列序不同，同样是漂移——名字对得上不代表索引是对的。"""
    drifted = [
        ("idx_golang_test_0", 1, 1, "group_id", None),  # ← proto 要的是 player_id
        ("uk_golang_test", 0, 1, "ip", 191),
    ]
    conn.queue_rows([(1,)], ALIGNED_COLS, drifted, PRIMARY_ID)
    with pytest.raises(Proto2MySQLError, match="idx_golang_test_0"):
        indexed_db.create_or_update_table(testpb.golang_test)


def test_migration_propagates_index_metadata_query_failure(indexed_db, conn, testpb):
    """审计入口不能把“索引无法验证”伪装成空迁移。"""
    conn.queue_rows([(1,)], ALIGNED_COLS, PRIMARY_ID)
    conn.raise_on_sql = (
        "SELECT INDEX_NAME",
        RuntimeError("information_schema permission denied"),
    )

    with pytest.raises(Proto2MySQLError, match="permission denied"):
        indexed_db.generate_migration_sql(testpb.golang_test)


def test_index_signature_projects_same_alter_column_rename(conn, kitchenpb):
    """CHANGE COLUMN 会同步改索引引用，不能把旧列名误报成索引漂移。"""
    from proto2mysql import with_indexes

    d = DB(conn, "testdb")
    d.register_table(kitchenpb.rename_after, with_indexes("new_name"))
    conn.queue_rows(
        [(1,)],
        [
            col_row("id", "bigint", "pb:1", default="0"),
            col_row("old_name", "mediumtext", "pb:2", nullable="YES"),
            col_row("score", "bigint", "pb:3", default="0"),
        ],
        [("idx_rename_demo_0", 1, 1, "old_name", 191)],
        PRIMARY_ID,
    )

    migration = d.generate_migration_sql(kitchenpb.rename_after)

    assert "CHANGE COLUMN `old_name` `new_name`" in migration
    assert "ADD INDEX" not in migration


def test_new_index_on_renamed_non_primary_column_runs_after_change(
    conn, kitchenpb
):
    """TiDB 不能在 CHANGE 同句用目标新列名建索引，必须拆成第二阶段。"""
    from proto2mysql import with_indexes

    d = DB(conn, "testdb")
    d.register_table(kitchenpb.rename_after, with_indexes("new_name"))
    conn.queue_rows(
        [(1,)],
        [
            col_row("id", "bigint", "pb:1", default="0"),
            col_row("old_name", "mediumtext", "pb:2", nullable="YES"),
            col_row("score", "bigint", "pb:3", default="0"),
        ],
        [],
        PRIMARY_ID,
    )

    statements = d.generate_migration_sql(kitchenpb.rename_after).splitlines()

    assert len(statements) == 2
    assert "CHANGE COLUMN `old_name` `new_name`" in statements[0]
    assert "ADD COLUMN `added`" in statements[0]
    assert "ADD INDEX" not in statements[0]
    assert "ADD INDEX `idx_rename_demo_0` (`new_name`(191))" in statements[1]


def test_migration_uses_same_bounded_index_names_as_create(conn, testpb):
    """64 字符表名不能让 ALTER 重新生成超长索引名。"""
    from proto2mysql import DB, with_indexes, with_table_name, with_unique_key
    from proto2mysql.table import index_name_for, unique_key_name_for

    table_name = "t" * 64
    d = DB(conn, "testdb")
    table = d.register_table(
        testpb.golang_test,
        with_table_name(table_name),
        with_indexes("player_id"),
        with_unique_key("ip"),
    )
    conn.queue_rows([(1,)], ALIGNED_COLS, [], PRIMARY_ID)

    migration = d.generate_migration_sql(testpb.golang_test)
    create = table.get_create_table_sql()
    index_name = index_name_for(table_name, 0)
    unique_name = unique_key_name_for(table_name)

    assert f"ADD INDEX `{index_name}`" in migration
    assert f"ADD UNIQUE KEY `{unique_name}`" in migration
    assert f"INDEX `{index_name}`" in create
    assert f"UNIQUE KEY `{unique_name}`" in create


def test_sync_skips_index_query_when_none_declared(db, conn, testpb):
    """proto 里没声明二级索引时，只查询 PRIMARY 签名。"""
    conn.queue_rows([(1,)], ALIGNED_COLS, PRIMARY_ID)
    db.create_or_update_table(testpb.golang_test)
    assert not [s for s, _ in conn.executed if s.startswith("SELECT INDEX_NAME")]


# ── TiDB 异步 DDL 就绪探测（P2-6） ───────────────────────────────────────


def test_await_schema_visible_waits_for_added_and_renamed_targets(db):
    """等本次新增列和 CHANGE 后的目标新列名；MODIFY 不改变名字。"""
    f = db._newly_visible_column_names
    assert f(["ADD COLUMN `foo` bigint COMMENT 'pb:9'"]) == {"foo"}
    assert f(["MODIFY COLUMN `ip` MEDIUMTEXT COMMENT 'pb:2'"]) == set()
    assert f(["CHANGE COLUMN `a` `b` MEDIUMTEXT COMMENT 'pb:2'"]) == {"b"}
    assert f(["ADD INDEX `idx_x` (`a`)"]) == set()
    assert f([
        "MODIFY COLUMN `ip` MEDIUMTEXT COMMENT 'pb:2'",
        "ADD COLUMN `bar` int COMMENT 'pb:8'",
    ]) == {"bar"}


def test_await_schema_visible_gives_up_on_empty_read(db, conn, testpb, monkeypatch):
    """一列都读不到时立即放弃，不白等到 60 秒超时。

    真表不可能零列，读不到说明是权限/库名的问题——继续轮询只会把启动拖满。
    """
    monkeypatch.setattr(DB, "SCHEMA_SETTLE_TIMEOUT", 30.0)
    table = db.tables[testpb.golang_test.DESCRIPTOR.full_name]
    conn.queue_rows([])  # 回读返回空

    import time as _time

    start = _time.monotonic()
    db._await_schema_visible(
        testpb.golang_test.DESCRIPTOR.full_name, table,
        ["ADD COLUMN `foo` bigint COMMENT 'pb:9'"],
    )
    assert _time.monotonic() - start < 2.0, "读不到任何列时应立即返回"


def test_sync_waits_for_added_column_to_become_visible(db, conn, testpb):
    """ALTER 之后必须回读一次，确认新列真的可见了才放行后续 SQL。

    TiDB 的 DDL 是异步 online 的：ALTER 返回时变更只是进了队列，各节点按 lease
    （默认 45s）分批加载。不等就直接按新 proto 发 SQL，连到旧节点就报 Unknown column。
    """
    conn.queue_rows(
        [(1,)],  # 表存在
        ALIGNED_COLS[:-1],  # 缺 player_id
        PRIMARY_ID,  # 有主键
        [],  # ALTER 自身
        ALIGNED_COLS,  # 就绪回读：新列可见了
    )
    db.create_or_update_table(testpb.golang_test)
    col_reads = [s for s, _ in conn.executed if "INFORMATION_SCHEMA.COLUMNS" in s]
    assert len(col_reads) == 2, "ALTER 前读一次对比、ALTER 后再读一次确认可见"


# ── 不支持的字段类型 fail-fast（P2-4） ───────────────────────────────────


def test_unsupported_field_kind_fails_fast_at_ddl(kitchenpb):
    """sint32/fixed64 这类没有 MySQL 映射的类型必须在**建表时**就报错。

    早先它们静默回落成 TEXT：建表一路成功，跑到第一次写入才抛异常，
    而那时列已经建出来了、可能还上了线。
    """
    from google.protobuf import descriptor_pb2, descriptor_pool
    from proto2mysql import MessageTable
    from proto2mysql.errors import InvalidFieldKindError

    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.name = "bad_kinds_probe.proto"
    fdp.package = "badkinds"
    fdp.syntax = "proto3"
    md = fdp.message_type.add()
    md.name = "bad_kinds"
    f1 = md.field.add()
    f1.name, f1.number = "id", 1
    f1.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
    f1.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    f2 = md.field.add()
    f2.name, f2.number = "zigzag", 2
    f2.type = descriptor_pb2.FieldDescriptorProto.TYPE_SINT32
    f2.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

    pool = descriptor_pool.DescriptorPool()
    file_desc = pool.Add(fdp)
    table = MessageTable.from_descriptor(file_desc.message_types_by_name["bad_kinds"])

    with pytest.raises(InvalidFieldKindError) as exc:
        table.get_create_table_sql()
    assert "sint32" in str(exc.value), "错误信息应指出替代方案"


# ── 表名默认值告警（P2-3） ───────────────────────────────────────────────


def test_missing_table_name_option_warns_on_sync(db, conn, testpb, caplog):
    """没声明 table_name 时表名退化成 proto full name（含 package）。

    package 一改表名就跟着变 → 建出一张全新的空表，旧数据留在旧表里，
    而**两边都不报错**，服务照常起来，玩家数据"凭空消失"。
    """
    conn.queue_rows([(1,)], [], [(1,)])
    table = db.tables[testpb.golang_test_list.DESCRIPTOR.full_name]
    assert table.has_explicit_table_name is False

    with caplog.at_level("WARNING", logger="proto2mysql"):
        db._sync_table_schema(testpb.golang_test_list.DESCRIPTOR.full_name, table)
    assert any("table_name" in r.getMessage() for r in caplog.records)


def test_explicit_table_name_does_not_warn(db, conn, testpb, caplog):
    """声明了 table_name 就不该有噪音告警。"""
    conn.queue_rows([(1,)], ALIGNED_COLS, PRIMARY_ID)
    table = db.tables[testpb.golang_test.DESCRIPTOR.full_name]
    assert table.has_explicit_table_name is True

    with caplog.at_level("WARNING", logger="proto2mysql"):
        db._sync_table_schema(testpb.golang_test.DESCRIPTOR.full_name, table)
    assert not [r for r in caplog.records if "table_name" in r.getMessage()]


# ── 并发边界（docs/concurrency.md） ──────────────────────────────────────
#
# 这一组测的是「P2MC 管什么、不管什么」。其中"丢失更新"那条是**故意断言坏行为**：
# 它不是 bug，是整行 save 的固有语义；写成测试是为了把这个失败模式钉死在代码里，
# 免得有人以为换成安全的主键状态机就能同时解决 lost update。


def test_save_does_not_touch_columns_this_process_does_not_know(db, conn, testpb):
    """v2 独有的列不会被 v1 的 save 清零——UPDATE 只写 descriptor 内的列。"""
    db.save(testpb.golang_test(id=7, ip="a"))
    sql = conn.last_sql()
    assert sql.startswith("UPDATE `golang_test` SET ")
    # 本进程认识的**非主键**列都在 SET 子句里
    for col in ("ip", "port", "group_id", "player", "player_id"):
        assert f"`{col}` = %s" in sql
    # 主键只出现在 WHERE，不进 SET。
    assert "SET `id` =" not in sql
    # 而"本进程不认识的列"根本不会出现——列清单来自 descriptor，不是 information_schema
    assert "level" not in sql


def test_insert_on_dup_update_uses_primary_key_exact_partial_update(db, conn, testpb):
    """历史命名不等于允许二级 UNIQUE 劫持：高层 API 只按完整主键更新。"""
    db.insert_on_dup_update(testpb.golang_test(id=7, ip="a"))
    sql = conn.last_sql()
    assert sql == "UPDATE `golang_test` SET `ip` = %s WHERE `id` = %s"


def test_lost_update_on_shared_field_is_not_prevented(db, conn, testpb):
    """**共同字段的丢失更新，本库不防**，这是整行 save 的固有语义。

    时序（docs/concurrency.md 第三节）：

        T1 v1 读到 port=100
        T2 v2 把 port 改成 200 并 save
        T3 v1 只想改 ip，却用了整行 save —— 它手里的 port 还是 100
        → port=200 被盖回 100

    要防必须自己选：update_fields_by_pk / incr_by_pk / 乐观锁 / 行锁。
    """
    stale = testpb.golang_test(id=7, ip="Bob", port=100)  # v1 手里的旧快照
    db.save(stale)
    sql, args = conn.executed[-1]
    # port 确实被写进去了——哪怕调用方只想改 ip
    assert "`port` = %s" in sql
    assert "100" in [str(a) for a in args], "整行 save 会把手里的旧 port 一起写回去"


def test_update_fields_by_pk_avoids_lost_update(db, conn, testpb):
    """只改一两个字段时的正确姿势：不碰别人改过的共同字段。"""
    db.update_fields_by_pk(testpb.golang_test(id=7, ip="Bob"), "ip")
    sql = conn.last_sql()
    assert sql == "UPDATE `golang_test` SET `ip` = %s WHERE `id` = %s"
    assert "port" not in sql, "没点名的列一个都不该出现"


def test_transaction_bypasses_cache_on_read(db, conn, testpb):
    """事务内**完全绕过缓存**——要读到事务内自己刚写的最新值，走缓存就错了。

    这也是"缓存机制本身不会把未提交数据写进缓存"的一半原因，
    另一半是失效延迟到提交成功之后（见 test_cache_invalidation_deferred_until_commit）。
    """
    cache = DictCache()
    db.enable_cache(cache)
    table = db.tables[testpb.golang_test.DESCRIPTOR.full_name]

    # 缓存里先放一条能命中的
    cached = testpb.golang_test(id=9, ip="from-cache")
    cache.set(db.cache_key(cached), encode_entry(table.field_numbers, cached.SerializeToString()), None)

    # 事务外：命中缓存，不查库
    out = testpb.golang_test(id=9)
    db.find_one_by_pk(out)
    assert out.ip == "from-cache"
    assert not [s for s, _ in conn.executed if s.startswith("SELECT `id`")]

    # 事务内：必须绕过缓存去查库
    conn.queue_rows([(9, "from-db-in-tx", 0, 0, b"", 0)])
    with db.transaction() as tx:
        out2 = testpb.golang_test(id=9)
        tx.find_one_by_pk(out2)
    assert out2.ip == "from-db-in-tx", "事务内不该读缓存"
    assert [s for s, _ in conn.executed if s.startswith("SELECT `id`")], "事务内必须真的查库"


def test_transaction_does_not_write_uncommitted_data_into_cache(db, conn, testpb):
    """事务内的读不回填缓存——否则未提交的数据会漏进缓存，回滚也收不回来。"""
    cache = DictCache()
    db.enable_cache(cache)

    conn.queue_rows([(9, "in-tx", 0, 0, b"", 0)])
    with db.transaction() as tx:
        tx.find_one_by_pk(testpb.golang_test(id=9))
    assert len(cache) == 0, "事务内的读不该回填缓存"


def test_for_update_outside_transaction_is_refused(db, testpb):
    """FOR UPDATE 只在事务内有意义：事务外单句自动提交，锁立刻释放，等于没加。

    库对这条做硬校验而不是静默失效——静默失效的锁比没有锁更危险。
    """
    with pytest.raises(Proto2MySQLError):
        db.find_one_by_pk_for_update(testpb.golang_test(id=1))


def test_optimistic_lock_detects_conflict(db, conn, testpb):
    """乐观锁：版本对不上就返回 False，由调用方决定重试还是报错。"""
    msg = testpb.golang_test(id=7, ip="x", group_id=3)
    conn.next_rowcount = 1
    assert db.update_if_version(msg, "group_id") is True

    conn.next_rowcount = 0  # 版本已被别人改走
    assert db.update_if_version(msg, "group_id") is False


def test_atomic_decrement_needs_no_read(db, conn, testpb):
    """计数类走数据库端原子运算，根本不需要先读——从源头上没有丢失更新的窗口。"""
    conn.next_rowcount = 0
    assert db.decr_by_pk_if_enough(testpb.golang_test(id=7), "port", 5) is False
    assert conn.last_sql() == (
        "UPDATE `golang_test` SET `port` = `port` - %s WHERE `id` = %s AND `port` >= %s"
    )
    # 关键：语句里没有任何"先读出来的旧值"
    assert "VALUES(" not in conn.last_sql()


# ── 补主键那条 ALTER 的连坐（自审发现） ──────────────────────────────────


def test_index_on_brand_new_primary_key_column_moves_with_it(conn, testpb):
    """主键列是**本次新增**的时候，引用它的 ADD INDEX 必须跟着搬到第二条 ALTER。

    留在第一条里的话，列还没建出来就先 ADD INDEX，MySQL 报 Error 1072
    （Key column doesn't exist in table），**整条第一条 ALTER 失败**，
    所有 ADD COLUMN 一起废掉——与当初把补主键拆成两条要避免的是同一种连坐。
    """
    from proto2mysql import DB, with_indexes

    d = DB(conn, "testdb")
    d.register_table(testpb.golang_test, with_indexes("id"))
    conn.queue_rows(
        [(1,)],  # 表存在
        # 线上既没有 id（主键列），也没有 player_id（普通列）
        [c for c in ALIGNED_COLS if c[0] not in ("id", "player_id")],
        [],      # 线上没有索引
        NO_PRIMARY,  # 也没有主键
    )
    d.create_or_update_table(testpb.golang_test)
    alters = [s for s, _ in conn.executed if s.startswith("ALTER TABLE")]
    assert len(alters) == 2
    # 第一条只管跟主键无关的列，照常先落地——补主键失败也不该连坐它
    assert "ADD COLUMN `player_id`" in alters[0]
    assert "ADD COLUMN `id`" not in alters[0], "主键列本体必须在第二条"
    assert "idx_golang_test_0" not in alters[0], "引用主键列的索引也必须在第二条"
    assert "ADD COLUMN `id`" in alters[1]
    assert "ADD PRIMARY KEY" in alters[1]
    assert "idx_golang_test_0" in alters[1]


def test_index_on_renamed_primary_key_column_moves_with_it(conn, kitchenpb):
    """先完成主键列改名，再让第二阶段约束引用新列名。"""
    from proto2mysql import DB, with_indexes

    d = DB(conn, "testdb")
    d.register_table(kitchenpb.keyword_col, with_indexes("id"))
    renamed_cols = [
        col_row("old_id", "bigint", "pb:1", default="0"),
        col_row("key", "mediumtext", "pb:2", nullable="YES"),
        col_row("text", "mediumtext", "pb:3", nullable="YES"),
    ]
    conn.queue_rows(
        [(1,)],
        renamed_cols,
        [],
        NO_PRIMARY,
    )

    statements = d.generate_migration_sql(kitchenpb.keyword_col).splitlines()

    assert len(statements) == 2
    assert "CHANGE COLUMN `old_id` `id`" in statements[0]
    assert "idx_keyword_col_0" not in statements[0]
    assert "CHANGE COLUMN" not in statements[1]
    assert "ADD PRIMARY KEY (`id`)" in statements[1]
    assert "ADD INDEX `idx_keyword_col_0` (`id`)" in statements[1]


def test_text_primary_key_gets_index_prefix_length(conn, kitchenpb):
    """TEXT/BLOB 主键列补主键时必须带前缀长度，否则 MySQL 报 Error 1170。

    建表分支一直是带的（走 _index_column），补主键那条早先是裸列名。
    """
    from proto2mysql import DB

    d = DB(conn, "testdb")
    d.register_table(kitchenpb.combo_key)   # 主键 user_id, provider —— provider 是 string
    conn.queue_rows(
        [(1,)],
        [
            col_row("user_id", "bigint unsigned", "pb:1", default="0"),
            col_row("provider", "mediumtext", "pb:2", nullable="YES"),
            col_row("score", "bigint", "pb:3", default="0"),
        ],
        NO_PRIMARY,  # 没有主键
    )
    d.create_or_update_table(kitchenpb.combo_key)
    pk_alter = next(s for s, _ in conn.executed if "ADD PRIMARY KEY" in s)
    assert "ADD PRIMARY KEY (`user_id`,`provider`(191))" in pk_alter


# ── 事务里拿父对象写：失效必须挂进同一份 pending（自审发现的 P0） ─────────


def test_invalidation_from_parent_db_inside_transaction_is_deferred(db, conn, testpb):
    """`with db.transaction()` 块里**拿父对象 db** 写时，缓存失效也要延到提交之后。

    父对象的 `_in_transaction` 一直是 False，早先它走的是"立即删"那条路：
    删发生在 COMMIT **之前**，中间任何一个并发读者都会把旧值回填回来，
    而提交后再也没有第二次删——库是新值、缓存是旧值，一直脏到 TTL。
    """
    cache = DictCache()
    db.enable_cache(cache)
    key = db.cache_key(testpb.golang_test(id=9))
    cache.set(key, b"stale", None)

    with db.transaction():
        db.update(testpb.golang_test(id=9, ip="new"))   # ← 父对象，不是 tx
        assert len(cache) == 1, "提交之前不能删：回滚的话这条缓存还是有效的"
    assert len(cache) == 0, "提交成功之后必须删掉"


def test_invalidation_from_parent_db_survives_rollback(db, conn, testpb):
    """同上，但事务回滚：缓存**不该**被删（删了就是把还有效的条目误删）。"""
    cache = DictCache()
    db.enable_cache(cache)
    cache.set(db.cache_key(testpb.golang_test(id=9)), b"still-valid", None)

    with pytest.raises(RuntimeError):
        with db.transaction():
            db.update(testpb.golang_test(id=9, ip="new"))
            raise RuntimeError("业务失败")
    assert len(cache) == 1


def test_for_update_allowed_on_non_autocommit_connection(db, conn, testpb):
    """`autocommit=False` 的连接本来就一直在事务里，FOR UPDATE 在那儿是**合法**的。

    门禁按对象级 `_in_transaction` 判的话会把它误拒——把一条原本正确的用法拦掉。
    """
    from proto2mysql import QueryOptions

    conn.autocommit = False
    conn.queue_rows([(9, "x", 0, 0, b"", 0)])
    db.find_one_with_options(
        testpb.golang_test(), "`id` = ?", [9], QueryOptions(for_update=True)
    )
    assert "FOR UPDATE" in conn.last_sql()


def test_insert_on_dup_update_with_only_primary_key_still_upserts(db, conn, testpb):
    """只赋了主键列时不能退化成裸 INSERT。

    退化之后语义从"有则更新"悄悄变成"有则报 1062"，调用方拿到的是一个
    它压根没打算处理的 DuplicateKeyError。
    """
    db.insert_on_dup_update(testpb.golang_test(id=7))
    sql = conn.last_sql()
    assert sql == "UPDATE `golang_test` SET `id` = `id` WHERE `id` = %s"


def test_min_max_new_require_numeric_column(testpb):
    """LEAST / GREATEST 也是算术：文本列上的"只增不减"水位会静默倒退。

    MySQL 会先把内容按数值解析（解析不出算 0）再比，于是
    GREATEST('abc', VALUES(col)) 在非严格模式下把一个非空文本当成 0。
    Go 侧的 MinNew / MaxNew 同样标了 numeric。
    """
    from proto2mysql import SQLBuilder, max_new, min_new
    from proto2mysql.errors import Proto2MySQLError

    b = SQLBuilder.from_message(testpb.golang_test)
    msg = testpb.golang_test(id=7, ip="x")
    for mk in (min_new, max_new):
        with pytest.raises(Proto2MySQLError):
            b.upsert_with(msg, mk("ip"))          # ip 是 MEDIUMTEXT
        b.upsert_with(msg, mk("port"))            # port 是 uint32，放行
