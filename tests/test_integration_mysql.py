"""真库集成测试：建表 → 读写 → 迁移，全部打在真实 MySQL 上。

默认跳过。给一个 DSN 就会跑：

    PROTO2MYSQL_DSN=mysql://root@127.0.0.1:3306/proto2mysql_test pytest tests/test_integration_mysql.py

库不存在会自动建；每个用例自带 DROP TABLE，跑完不留残渣。
**别指向有真实数据的库**——用例会 DROP 它建的那几张表。

这些用例覆盖的是纯离线测试证明不了的东西：DATETIME(6) 的微秒到底有没有落库、
裸字节进 MEDIUMBLOB 有没有被 charset 破坏、唯一键冲突抛不抛得对、
以及按字段号 CHANGE COLUMN 改名之后**数据还在不在**。
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest

from proto2mysql import DB, DictCache
from proto2mysql.errors import DuplicateKeyError, NoRowsFoundError, Proto2MySQLError

pymysql = pytest.importorskip("pymysql")

DSN = os.environ.get("PROTO2MYSQL_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="需要 PROTO2MYSQL_DSN 才跑真库集成测试")


def _connect(database: str | None):
    url = urlparse(DSN)
    return pymysql.connect(
        host=url.hostname or "127.0.0.1",
        port=url.port or 3306,
        user=url.username or "root",
        password=url.password or "",
        database=database,
        charset="utf8mb4",
        autocommit=True,
    )


@pytest.fixture(scope="session")
def dbname():
    name = (urlparse(DSN).path or "/proto2mysql_test").lstrip("/")
    conn = _connect(None)
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` DEFAULT CHARSET utf8mb4")
    conn.close()
    return name


def is_tidb(conn) -> bool:
    """后端是不是 TiDB。

    按 VERSION() 判定而不是靠配置——TiDB 会把自己报成 "8.0.11-TiDB-v8.5.1"，
    也就是说它**声称自己是 MySQL 8.0**，光看主版本号分不出来。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION()")
        return "tidb" in str(cur.fetchone()[0]).lower()


@pytest.fixture()
def conn(dbname):
    c = _connect(dbname)
    yield c
    c.close()


@pytest.fixture()
def db(conn, dbname, kitchenpb, testpb):
    d = DB(conn, dbname)
    d.register_all_tables(modules=[kitchenpb, testpb])
    return d


def drop(conn, *tables):
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(f"DROP TABLE IF EXISTS `{t}`")


# ── 建表 ────────────────────────────────────────────────────────────────


def test_create_table_matches_proto(db, conn, kitchenpb, dbname):
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT, EXTRA "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
            "ORDER BY ORDINAL_POSITION",
            (dbname, "kitchen_sink"),
        )
        cols = {r[0]: r for r in cur.fetchall()}

    # 列顺序 = proto 声明顺序
    assert list(cols) == [f.name for f in kitchenpb.kitchen_sink.DESCRIPTOR.fields]
    # 每列都带 pb:N 注释——按字段号迁移全靠它
    assert cols["id"][3] == "pb:1"
    assert cols["created_at"][3] == "pb:11"
    # Timestamp 必须是 DATETIME(6) 且可空
    assert cols["created_at"][1] == "datetime(6)"
    assert cols["created_at"][2] == "YES"
    # nullable 选项生效
    assert cols["zone_id"][2] == "YES"
    assert cols["name"][1] == "mediumtext"
    assert cols["payload"][1] == "mediumblob"
    assert "auto_increment" in cols["id"][4]


def test_sync_is_idempotent(db, conn, kitchenpb):
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)
    db.clear_column_cache("kitchen.kitchen_sink")
    # 第二次同步不该产生任何 ALTER
    assert db.generate_migration_sql(kitchenpb.kitchen_sink) == ""


def test_same_name_non_unique_index_fails_closed(db, conn, kitchenpb):
    """同名不等于同结构；唯一约束漂移时不能让服务带病继续启动。"""
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)
    with conn.cursor() as cur:
        # TiDB 不接受同一条 ALTER 里 DROP+ADD 同名索引，拆开保持两端可执行。
        cur.execute("ALTER TABLE `kitchen_sink` DROP INDEX `uk_kitchen_sink`")
        cur.execute(
            "ALTER TABLE `kitchen_sink` "
            "ADD INDEX `uk_kitchen_sink` (`name`(191), `zone_id`)"
        )

    with pytest.raises(Proto2MySQLError, match="uk_kitchen_sink"):
        db.generate_migration_sql(kitchenpb.kitchen_sink)


# ── 读写往返 ────────────────────────────────────────────────────────────


def test_full_type_roundtrip(db, conn, kitchenpb):
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)

    row = kitchenpb.kitchen_sink(
        id=1, name="张三", zone_id=7, u32=4294967295, u64=2**64 - 1,
        f32=0.1, f64=0.1, flag=True, payload=bytes(range(256)),
        tier=kitchenpb.GRADE_GOLD, opt_score=0,
    )
    row.tags.extend([10, 20, 30])
    row.attrs["a"] = 1
    row.sub.a = 5
    row.sub.b = "嵌套"
    row.created_at.seconds = 1700000000
    row.created_at.nanos = 123456000
    db.insert(row)

    out = kitchenpb.kitchen_sink(id=1)
    db.find_one_by_pk(out)

    assert out.name == "张三"
    assert out.u32 == 4294967295
    assert out.u64 == 2**64 - 1
    assert out.f32 == row.f32  # float32 最短表示往返，不是近似值
    assert out.f64 == 0.1
    assert out.flag is True
    assert out.payload == bytes(range(256))  # 裸字节，没被 charset 破坏
    assert out.tier == kitchenpb.GRADE_GOLD
    assert list(out.tags) == [10, 20, 30]
    assert dict(out.attrs) == {"a": 1}
    assert out.sub.a == 5 and out.sub.b == "嵌套"
    assert out.created_at.seconds == 1700000000
    assert out.created_at.nanos == 123456000  # 微秒精度真的落库了


def test_blob_is_not_base64(db, conn, kitchenpb):
    """落库的必须是 proto 裸字节，与手写 SerializeToString() 逐字节相同。"""
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)

    row = kitchenpb.kitchen_sink(id=1)
    row.sub.a = 7
    row.sub.b = "x"
    db.insert(row)

    with conn.cursor() as cur:
        cur.execute("SELECT `sub` FROM `kitchen_sink` WHERE `id` = 1")
        stored = cur.fetchone()[0]
    assert stored == row.sub.SerializeToString()


def test_unset_timestamp_is_sql_null(db, conn, kitchenpb):
    """未设置的 Timestamp 落 NULL；若落空串，STRICT 模式下整行都插不进去。"""
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)
    db.insert(kitchenpb.kitchen_sink(id=1, name="no-time"))

    with conn.cursor() as cur:
        cur.execute("SELECT `created_at` FROM `kitchen_sink` WHERE `id` = 1")
        assert cur.fetchone()[0] is None

    out = kitchenpb.kitchen_sink(id=1)
    db.find_one_by_pk(out)
    assert not out.HasField("created_at")


def test_duplicate_key_raises(db, conn, kitchenpb):
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)
    db.insert(kitchenpb.kitchen_sink(id=1, name="dup", zone_id=1))
    with pytest.raises(DuplicateKeyError):
        db.insert(kitchenpb.kitchen_sink(id=2, name="dup", zone_id=1))  # 撞唯一键


def test_insert_returning_id(db, conn, kitchenpb):
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)
    first = db.insert_returning_id(kitchenpb.kitchen_sink(name="a"))
    second = db.insert_returning_id(kitchenpb.kitchen_sink(name="b"))
    assert second == first + 1


def test_update_and_delete(db, conn, kitchenpb):
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)
    db.insert(kitchenpb.kitchen_sink(id=1, name="before", u32=5))

    db.update(kitchenpb.kitchen_sink(id=1, name="after"))
    out = kitchenpb.kitchen_sink(id=1)
    db.find_one_by_pk(out)
    assert out.name == "after"
    assert out.u32 == 5, "update 只写已赋值字段，不该把没提到的列清掉"

    # 写零值必须走 update_fields_by_pk
    db.update_fields_by_pk(kitchenpb.kitchen_sink(id=1), "u32")
    out2 = kitchenpb.kitchen_sink(id=1)
    db.find_one_by_pk(out2)
    assert out2.u32 == 0

    db.delete(kitchenpb.kitchen_sink(id=1))
    with pytest.raises(NoRowsFoundError):
        db.find_one_by_pk(kitchenpb.kitchen_sink(id=1))


def test_batch_insert_and_find_all(db, conn, testpb):
    drop(conn, "golang_test")
    db.create_or_update_table(testpb.golang_test)
    rows = [testpb.golang_test(id=i, ip=f"10.0.0.{i}") for i in range(1, 51)]
    db.batch_insert(rows)

    out = testpb.golang_test_list()
    db.find_all(out)
    assert len(out.test_list) == 50
    assert {r.ip for r in out.test_list} == {f"10.0.0.{i}" for i in range(1, 51)}


def test_percent_literal_in_where(db, conn, testpb):
    """WHERE 里的字面 % 必须能用——这是 pyformat 驱动最容易炸的地方。"""
    drop(conn, "golang_test")
    db.create_or_update_table(testpb.golang_test)
    db.batch_insert([testpb.golang_test(id=1, ip="10.0.0.1"), testpb.golang_test(id=2, ip="192.168.1.1")])

    out = testpb.golang_test_list()
    db.find_all_by_where(out, "`ip` LIKE '10.0.0.%'")
    assert [r.id for r in out.test_list] == [1]


def test_decr_guard_prevents_negative(db, conn, testpb):
    drop(conn, "golang_test")
    db.create_or_update_table(testpb.golang_test)
    db.insert(testpb.golang_test(id=1, port=100))

    assert db.decr_by_pk_if_enough(testpb.golang_test(id=1), "port", 60) is True
    assert db.decr_by_pk_if_enough(testpb.golang_test(id=1), "port", 60) is False, "余额不足不该扣"

    out = testpb.golang_test(id=1)
    db.find_one_by_pk(out)
    assert out.port == 40


def test_update_if_version_cas(db, conn, testpb):
    drop(conn, "golang_test")
    db.create_or_update_table(testpb.golang_test)
    db.insert(testpb.golang_test(id=1, ip="v1", group_id=1))

    assert db.update_if_version(testpb.golang_test(id=1, ip="v2", group_id=1), "group_id") is True
    # 拿着旧版本号再来一次必须失败
    assert db.update_if_version(testpb.golang_test(id=1, ip="v3", group_id=1), "group_id") is False

    out = testpb.golang_test(id=1)
    db.find_one_by_pk(out)
    assert out.ip == "v2" and out.group_id == 2


# ── 事务 ────────────────────────────────────────────────────────────────


def test_transaction_rollback_discards(db, conn, testpb):
    drop(conn, "golang_test")
    db.create_or_update_table(testpb.golang_test)

    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            tx.insert(testpb.golang_test(id=1, ip="rolled-back"))
            raise RuntimeError("boom")

    assert db.count(testpb.golang_test()) == 0


def test_transaction_commit_persists(db, conn, testpb):
    drop(conn, "golang_test")
    db.create_or_update_table(testpb.golang_test)
    with db.transaction() as tx:
        tx.insert(testpb.golang_test(id=1, ip="committed"))
    assert db.count(testpb.golang_test()) == 1


def test_original_db_cannot_open_nested_transaction(db, conn, testpb):
    """绕回父 DB 再 BEGIN 不能把外层未提交写隐式提交掉。"""
    drop(conn, "golang_test")
    db.create_or_update_table(testpb.golang_test)

    with pytest.raises(Proto2MySQLError, match="nested transaction"):
        with db.transaction() as tx:
            tx.insert(testpb.golang_test(id=1, ip="outer"))
            with db.transaction():
                pass

    assert db.count(testpb.golang_test()) == 0


def test_managed_transaction_rejects_raw_external_begin(db, conn):
    """同一连接已有外部事务时，不能再次 BEGIN 并隐式提交它。"""
    conn.begin()
    try:
        with pytest.raises(Proto2MySQLError, match="外部事务"):
            with db.transaction():
                pass
    finally:
        conn.rollback()


def test_for_update_inside_transaction(db, conn, testpb):
    drop(conn, "golang_test")
    db.create_or_update_table(testpb.golang_test)
    db.insert(testpb.golang_test(id=1, port=10))

    with db.transaction() as tx:
        row = testpb.golang_test(id=1)
        tx.find_one_by_pk_for_update(row)
        assert row.port == 10
        tx.incr_by_pk(row, "port", 5)

    out = testpb.golang_test(id=1)
    db.find_one_by_pk(out)
    assert out.port == 15


# ── 迁移：按字段号改名保留数据（本库的招牌能力）────────────────────────


def test_rename_by_field_number_preserves_real_data(conn, dbname, kitchenpb):
    from proto2mysql import with_indexes

    drop(conn, "rename_demo")

    old = DB(conn, dbname)
    old.register_table(kitchenpb.rename_before)
    old.create_or_update_table(kitchenpb.rename_before)
    old.insert(kitchenpb.rename_before(id=1, old_name="要保留的值", score=42))

    # 换成新版 proto：字段 2 改了名、多了字段 4
    new = DB(conn, dbname)
    new.register_table(kitchenpb.rename_after, with_indexes("new_name"))
    migration = new.generate_migration_sql(kitchenpb.rename_after)
    assert "CHANGE COLUMN `old_name` `new_name`" in migration
    assert "ADD COLUMN `added`" in migration

    new.create_or_update_table(kitchenpb.rename_after)

    out = kitchenpb.rename_after(id=1)
    new.find_one_by_pk(out)
    assert out.new_name == "要保留的值", "改名后原有数据必须还在"
    assert out.score == 42
    assert out.added == 0

    with conn.cursor() as cur:
        cur.execute("SHOW INDEX FROM `rename_demo`")
        assert "idx_rename_demo_0" in {row[2] for row in cur.fetchall()}

    # 再同步一次应无差异
    new.clear_column_cache("kitchen.rename_after")
    assert new.generate_migration_sql(kitchenpb.rename_after) == ""


def test_renamed_primary_key_precedes_dependent_index(
    conn, dbname, kitchenpb
):
    """先改名再补主键/索引，兼容 TiDB 不解析同句新列名的限制。"""
    from proto2mysql import with_indexes

    drop(conn, "keyword_col")
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE `keyword_col` ("
            "`old_id` bigint NOT NULL DEFAULT 0 COMMENT 'pb:1', "
            "`key` MEDIUMTEXT COMMENT 'pb:2', "
            "`text` MEDIUMTEXT COMMENT 'pb:3')"
        )

    db = DB(conn, dbname)
    db.register_table(kitchenpb.keyword_col, with_indexes("id"))
    db.create_or_update_table(kitchenpb.keyword_col)

    with conn.cursor() as cur:
        cur.execute("SHOW INDEX FROM `keyword_col`")
        index_names = {row[2] for row in cur.fetchall()}
        cur.execute("SHOW COLUMNS FROM `keyword_col`")
        column_names = {row[0] for row in cur.fetchall()}
    assert "old_id" not in column_names and "id" in column_names
    assert {"PRIMARY", "idx_keyword_col_0"} <= index_names


def test_add_column_on_existing_table(conn, dbname, testpb):
    """存量表加列：ALTER 后老行的新列取默认值，老数据不动。"""
    drop(conn, "golang_test")
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE `golang_test` ("
            "`id` int unsigned NOT NULL AUTO_INCREMENT, `ip` MEDIUMTEXT, PRIMARY KEY(`id`))"
        )
        cur.execute("INSERT INTO `golang_test` (`ip`) VALUES ('legacy')")

    db = DB(conn, dbname)
    db.register_table(testpb.golang_test)
    db.create_or_update_table(testpb.golang_test)

    out = testpb.golang_test(id=1)
    db.find_one_by_pk(out)
    assert out.ip == "legacy"
    assert out.port == 0 and out.player_id == 0


def test_missing_primary_key_is_added(conn, dbname, testpb):
    """老表没主键时补上。

    补主键与列对齐是**两条**独立的 ALTER：主键列常带 AUTO_INCREMENT，
    而 MySQL 要求"自增列必须是键"（Error 1075），所以那条 MODIFY 必须与
    ADD PRIMARY KEY 同句；但它俩绝不能再跟 ADD COLUMN 挤在一条里——
    2026-08-26 在真 TiDB v8.5.1 上实测过，那样会让列也一起加不上。

    TiDB 上跳过：它**根本不支持给已存在的列加 AUTO_INCREMENT**（Error 8200），
    合并一条、拆成两条都一样。这不是本库能修的，是 TiDB 的能力边界。
    """
    if is_tidb(conn):
        pytest.skip("TiDB 不支持给已存在的列加 AUTO_INCREMENT（Error 8200）")
    drop(conn, "golang_test")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE `golang_test` (`id` int unsigned NOT NULL, `ip` MEDIUMTEXT)")

    db = DB(conn, dbname)
    db.register_table(testpb.golang_test)
    db.create_or_update_table(testpb.golang_test)

    assert db.table_has_primary_key("golang_test") is True


def test_missing_auto_increment_primary_key_column_is_added_with_constraint(
    conn, dbname, testpb
):
    """主键列本身也不存在时，ADD COLUMN 与 ADD PRIMARY KEY 必须同句。"""
    if is_tidb(conn):
        pytest.skip("TiDB 的 AUTO_INCREMENT ALTER 能力与 MySQL 不同")
    drop(conn, "golang_test")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE `golang_test` (`ip` MEDIUMTEXT)")

    db = DB(conn, dbname)
    db.register_table(testpb.golang_test)
    db.create_or_update_table(testpb.golang_test)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, EXTRA FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='golang_test' AND COLUMN_NAME='id'",
            (dbname,),
        )
        assert cur.fetchone() == ("id", "auto_increment")
    assert db.table_has_primary_key("golang_test") is True


# ── 缓存 ────────────────────────────────────────────────────────────────


def test_cache_rejects_implicit_transaction_connection(db, conn):
    conn.autocommit(False)
    with pytest.raises(Proto2MySQLError, match="autocommit=True"):
        db.enable_cache(DictCache())


def test_cache_rejects_raw_begin_on_autocommit_connection(db, conn, testpb):
    """PyMySQL begin() 后 autocommit 标志仍为 True，但共享缓存仍必须绕过。"""
    db.enable_cache(DictCache())
    conn.begin()
    try:
        assert conn.get_autocommit() is True
        with pytest.raises(Proto2MySQLError, match="外部事务"):
            db.find_one_by_pk(testpb.golang_test(id=1))
    finally:
        conn.rollback()


def test_cache_aside_against_real_db(db, conn, testpb):
    drop(conn, "golang_test")
    db.create_or_update_table(testpb.golang_test)
    db.insert(testpb.golang_test(id=1, ip="v1"))

    cache = DictCache()
    db.enable_cache(cache, ttl=60)

    out = testpb.golang_test(id=1)
    db.find_one_by_pk(out)
    assert len(cache) == 1

    # 绕过库直接改数据库，缓存仍应命中旧值（证明确实走了缓存）
    with conn.cursor() as cur:
        cur.execute("UPDATE `golang_test` SET `ip` = 'v2' WHERE `id` = 1")
    cached = testpb.golang_test(id=1)
    db.find_one_by_pk(cached)
    assert cached.ip == "v1"

    # 经本库写会失效缓存，之后读到新值
    db.update_kv_by_pk(testpb.golang_test(id=1), "ip", "v3")
    fresh = testpb.golang_test(id=1)
    db.find_one_by_pk(fresh)
    assert fresh.ip == "v3"


@pytest.mark.parametrize("method", ["save", "insert_on_dup_update"])
def test_upsert_secondary_unique_conflict_does_not_modify_owner(
    db, conn, kitchenpb, method
):
    """同 UNIQUE、不同 PK 必须报 1062，不能把候选数据写进 owner。"""
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)
    db.insert(kitchenpb.kitchen_sink(id=7, name="same", zone_id=1, u32=10))

    cache = DictCache()
    db.enable_cache(cache, ttl=60)
    cached = kitchenpb.kitchen_sink(id=7)
    db.find_one_by_pk(cached)
    assert len(cache) == 1

    candidate = kitchenpb.kitchen_sink(id=8, name="same", zone_id=1, u32=99)
    with pytest.raises(DuplicateKeyError):
        getattr(db, method)(candidate)

    # owner 没被写，原缓存也仍然有效；候选 PK 的不确定写路径已保守失效。
    assert len(cache) == 1

    with conn.cursor() as cur:
        cur.execute("SELECT `id`, `u32` FROM `kitchen_sink` ORDER BY `id`")
        assert cur.fetchall() == ((7, 10),)

    fresh = kitchenpb.kitchen_sink(id=7)
    db.find_one_by_pk(fresh)
    assert fresh.u32 == 10
    with pytest.raises(NoRowsFoundError):
        db.find_one_by_pk(kitchenpb.kitchen_sink(id=8))


def test_batch_save_can_be_made_atomic_with_transaction(db, conn, kitchenpb):
    """逐行 batch_save 遇到二级唯一键冲突时，显式事务会回滚先前行。"""
    drop(conn, "kitchen_sink")
    db.create_or_update_table(kitchenpb.kitchen_sink)
    db.insert(kitchenpb.kitchen_sink(id=7, name="same", zone_id=1, u32=10))

    rows = [
        kitchenpb.kitchen_sink(id=8, name="new", zone_id=1, u32=20),
        kitchenpb.kitchen_sink(id=9, name="same", zone_id=1, u32=99),
    ]
    with pytest.raises(DuplicateKeyError):
        with db.transaction() as tx:
            tx.batch_save(rows)

    with conn.cursor() as cur:
        cur.execute("SELECT `id`, `u32` FROM `kitchen_sink` ORDER BY `id`")
        assert cur.fetchall() == ((7, 10),)
