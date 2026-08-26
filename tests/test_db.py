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


# ── 建表 / 迁移 ─────────────────────────────────────────────────────────


def test_save_preserves_unknown_columns(db, conn, testpb):
    """save 必须走 ODKU，不能走 REPLACE INTO。

    REPLACE 是 DELETE+INSERT，语句里没提到的列会**回到默认值**；而列清单来自本进程的
    descriptor，所以滚动发布时旧版本 save 一次，新版本刚写进去的列就没了，且零报错。
    ODKU 只动子句里点名的列，别的原样保留。
    """
    db.save(testpb.golang_test(id=7, ip="a"))
    sql = conn.last_sql()
    assert sql.startswith("INSERT INTO `golang_test`")
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "REPLACE" not in sql
    # 零值也要写进去——save 的语义是「整行落库」，不是「只写非零字段」
    assert "`port` = VALUES(`port`)" in sql


def test_batch_save_preserves_unknown_columns(db, conn, testpb):
    db.batch_save([testpb.golang_test(id=1), testpb.golang_test(id=2)])
    sql = conn.last_sql()
    assert sql.startswith("INSERT INTO `golang_test`")
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "REPLACE" not in sql


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
    assert db.cache_key(testpb.golang_test(id=9)) == "pb:golang_test:9"


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


ALIGNED_COLS = [
    ("id", "int unsigned", "pb:1"),
    ("ip", "mediumtext", "pb:2"),
    ("port", "int unsigned", "pb:3"),
    ("group_id", "int unsigned", "pb:4"),
    ("player", "mediumblob", "pb:5"),
    ("player_id", "bigint unsigned", "pb:6"),
]


def test_sync_creates_table_when_missing(db, conn, testpb):
    conn.queue_rows(
        [(0,)],  # information_schema: 表不存在
        [],  # CREATE 自身
        ALIGNED_COLS,  # 建完之后回读，结构已经对齐
        [(1,)],  # 有主键
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
        [(1,)],  # 有主键
    )
    db.create_or_update_table(testpb.golang_test)
    alters = [s for s, _ in conn.executed if s.startswith("ALTER TABLE")]
    assert len(alters) == 1
    assert "ADD COLUMN `player_id` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:6'" in alters[0]


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
    # ALTER 之后还会有一条就绪回读（TiDB 的 DDL 是异步的），所以不能假设它是最后一条
    alter = next(s for s, _ in conn.executed if s.startswith("ALTER TABLE `golang_test` "))
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


# ── DDL 咨询锁（P2-1） ───────────────────────────────────────────────────


def test_sync_all_tables_takes_advisory_lock(db, conn, testpb):
    """结构同步全程持一把 GET_LOCK，避免多副本同时 ALTER 撞 Error 1060。"""
    conn.queue_rows(
        [(1,)],  # GET_LOCK 返回 1
        [(1,)],  # 表存在
        ALIGNED_COLS,  # 结构已对齐
        [(1,)],  # 有主键
        [(1,)],  # 表存在（第二张表 golang_test_list）
        [],  # 它的列（走不到实际对齐，返回空即可）
        [(1,)],
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
        [(1,)],
        [(1,)],
        [],
        [(1,)],
    )
    with caplog.at_level("WARNING", logger="proto2mysql"):
        db.sync_all_tables()
    assert any("未取得" in r.getMessage() for r in caplog.records)
    # 没拿到锁就不该去释放
    assert not any("RELEASE_LOCK" in s for s, _ in conn.executed)


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
        [(1,)],  # 有主键
    )
    indexed_db.create_or_update_table(testpb.golang_test)
    alter = next(s for s, _ in conn.executed if s.startswith("ALTER TABLE `golang_test`"))
    assert "ADD INDEX `idx_golang_test_0` (`player_id`)" in alter
    # ip 是 MEDIUMTEXT，索引必须带前缀长度，否则 MySQL 报 Error 1170
    assert "ADD UNIQUE KEY `uk_golang_test` (`ip`(191))" in alter


def test_sync_skips_existing_indexes(indexed_db, conn, testpb):
    """线上已有的索引不重复加；线上多出来的索引也不删（可能是 DBA 手工加的）。"""
    conn.queue_rows(
        [(1,)],
        ALIGNED_COLS,
        [("idx_golang_test_0",), ("uk_golang_test",), ("idx_dba_added_by_hand",)],
        [(1,)],
    )
    indexed_db.create_or_update_table(testpb.golang_test)
    assert not [s for s, _ in conn.executed if s.startswith("ALTER TABLE")], "全都在，不该发 ALTER"


def test_sync_skips_index_query_when_none_declared(db, conn, testpb):
    """proto 里没声明索引时，不必去查 information_schema。"""
    conn.queue_rows([(1,)], ALIGNED_COLS, [(1,)])
    db.create_or_update_table(testpb.golang_test)
    assert not [s for s, _ in conn.executed if "INFORMATION_SCHEMA.STATISTICS" in s]


# ── TiDB 异步 DDL 就绪探测（P2-6） ───────────────────────────────────────


def test_await_schema_visible_only_waits_for_added_columns(db):
    """只等**本次新增的列**。

    MODIFY / CHANGE 改的是已有列，回读时本来就看得见，等它们既没意义又拖长探测。
    """
    f = db._added_column_names
    assert f(["ADD COLUMN `foo` bigint COMMENT 'pb:9'"]) == {"foo"}
    assert f(["MODIFY COLUMN `ip` MEDIUMTEXT COMMENT 'pb:2'"]) == set()
    assert f(["CHANGE COLUMN `a` `b` MEDIUMTEXT COMMENT 'pb:2'"]) == set()
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
        [(1,)],  # 有主键
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
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
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
    conn.queue_rows([(1,)], ALIGNED_COLS, [(1,)])
    table = db.tables[testpb.golang_test.DESCRIPTOR.full_name]
    assert table.has_explicit_table_name is True

    with caplog.at_level("WARNING", logger="proto2mysql"):
        db._sync_table_schema(testpb.golang_test.DESCRIPTOR.full_name, table)
    assert not [r for r in caplog.records if "table_name" in r.getMessage()]


# ── 并发边界（docs/concurrency.md） ──────────────────────────────────────
#
# 这一组测的是「P2MC 管什么、不管什么」。其中"丢失更新"那条是**故意断言坏行为**：
# 它不是 bug，是整行 save 的固有语义；写成测试是为了把这个失败模式钉死在代码里，
# 免得有人以为加了 ODKU 就万事大吉。


def test_save_does_not_touch_columns_this_process_does_not_know(db, conn, testpb):
    """v2 独有的列不会被 v1 的 save 清零——ODKU 只更新点名的列。"""
    db.save(testpb.golang_test(id=7, ip="a"))
    sql = conn.last_sql()
    assert "ON DUPLICATE KEY UPDATE" in sql
    # 本进程认识的列都在 SET 子句里
    for col in ("id", "ip", "port", "group_id", "player", "player_id"):
        assert f"`{col}` = VALUES(`{col}`)" in sql
    # 而"本进程不认识的列"根本不会出现——列清单来自 descriptor，不是 information_schema
    assert "level" not in sql


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
    assert "`port` = VALUES(`port`)" in sql
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
