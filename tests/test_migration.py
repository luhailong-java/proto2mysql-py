"""按 proto 字段号迁移：改名保留数据、类型对齐、DATETIME 精度补齐。

这是本库最有价值也最容易写错的一块——错了不会报错，只会让线上表悄悄留在旧结构上。
"""

from __future__ import annotations

import pytest

from proto2mysql import ColumnMeta, MessageTable
from proto2mysql.errors import ExpandOnlyViolationError, FieldNumberReusedError
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
        # 统一口径：线上装得下目标就不动它。下面四条原先方向是反的
        # （线上窄却判兼容不拓宽、线上宽反而去 ALTER 收窄），见 table.py 的 _INT_RANK 注释。
        ("varchar(32)", "varchar(64)", False),  # 线上更窄，必须拓宽
        ("varchar(64)", "varchar(32)", True),  # 线上更宽，不动它（收窄会截断已有数据）
        ("float", "double", False),  # 线上更窄，必须拓宽
        ("double", "float", True),  # 线上更宽，不动它
        ("mediumtext", "MEDIUMTEXT", True),  # 大小写无关
        ("timestamp", "datetime(6)", False),  # 精度不足且时间语义不同
        ("timestamp(6)", "datetime(6)", False),  # 同精度仍是不同时间/时区语义
        ("datetime", "DATETIME(6)", False),  # DATETIME(0) 必须升到 (6)
        ("datetime(6)", "DATETIME(6)", True),
        ("datetime(6)", "DATETIME(3)", True),  # 线上精度更高时不动它（降精度会丢数据）
        ("tinyint(1)", "tinyint(1) NOT NULL DEFAULT 0", True),
    ],
)
def test_is_type_match(current, target, match):
    assert is_type_match(current, target) is match


@pytest.mark.parametrize(
    ("current", "target", "match"),
    [
        # ── 整数族：int 与 bigint 是不同的 base_type，修复前两个方向都会 ALTER ──
        ("bigint unsigned", "int unsigned NOT NULL DEFAULT 0", True),  # 线上更宽，不收窄
        ("int unsigned", "bigint unsigned NOT NULL DEFAULT 0", False),  # 线上更窄，要拓宽
        ("bigint", "tinyint NOT NULL DEFAULT 0", True),
        ("tinyint", "bigint NOT NULL DEFAULT 0", False),
        # 有无符号决定值域方向，不是宽窄问题，两边都装不下对方 → 一律 ALTER
        ("bigint unsigned", "int NOT NULL DEFAULT 0", False),
        ("bigint", "int unsigned NOT NULL DEFAULT 0", False),
        # ── 文本族：2026-08-19 实测事故——线上 mediumtext 被 varchar(255) 一侧重建，
        #    同一条写入在宽列副本成功、窄列副本报 1406，且不可复现 ──
        ("mediumtext", "varchar(255)", True),  # 线上更宽，不收窄
        ("varchar(255)", "MEDIUMTEXT", False),  # 线上更窄，要拓宽
        ("longtext", "MEDIUMTEXT", True),
        ("text", "MEDIUMTEXT", False),
        # ── 二进制族 ──
        ("mediumblob", "varbinary(255)", True),
        ("varbinary(255)", "MEDIUMBLOB", False),
        # ── 跨族没有可比性，一律判不兼容 ──
        ("int", "MEDIUMTEXT", False),
        ("mediumtext", "bigint NOT NULL DEFAULT 0", False),
    ],
)
def test_is_type_match_never_narrows(current, target, match):
    """跨类型的同族变更只许拓宽，绝不许收窄。

    修复前：整数族没有方向判断（int/bigint 是不同 base_type，直接判不兼容），
    文本族同理，于是滚动发布时 v1 副本一重启就把 v2 拓宽过的列 MODIFY 回去。
    """
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


# ── 改名 / 字段号复用 / expand_only ──────────────────────────────────────


def _aligned_cols(t) -> dict[str, ColumnMeta]:
    return {fd.name: ColumnMeta(t.get_mysql_field_type(fd), fd.number) for fd in t.fields}


def test_rename_still_supported_by_default(testpb, caplog):
    """改名保留数据是本库的招牌特性，默认行为不变——只是多打一条告警。"""
    t = table(testpb)
    current = _aligned_cols(t)
    current["old_ip"] = current.pop("ip")  # 线上还叫 old_ip，proto 已改名为 ip

    with caplog.at_level("WARNING", logger="proto2mysql"):
        clauses = t.build_alter_clauses(current)

    assert clauses == ["CHANGE COLUMN `old_ip` `ip` MEDIUMTEXT COMMENT 'pb:2'"]
    # 库无法判断谁新谁旧，滚动发布时新旧副本会来回改名，必须留痕。
    assert any("pb:2" in r.getMessage() for r in caplog.records)


def test_field_number_reuse_is_refused(testpb):
    """字段号被复用（跨族类型）一律拒绝，不设开关——这没有任何正当用途。

    修复前会静默生成 CHANGE COLUMN，MySQL 的隐式类型转换把旧列内容整列吃掉，
    而本库「永不 DROP COLUMN」的保护在这里完全帮不上忙。
    """
    t = table(testpb)
    current = _aligned_cols(t)
    # 线上 pb:6 是一列文本（旧字段留下的），proto 里 pb:6 已经是 bigint 了
    current.pop("player_id")
    current["legacy_note"] = ColumnMeta("mediumtext", 6)

    with pytest.raises(FieldNumberReusedError) as exc:
        t.build_alter_clauses(current)
    assert "legacy_note" in str(exc.value)
    assert "reserved" in str(exc.value)


def test_expand_only_allows_pure_additions(testpb):
    """纯新增在 expand_only 下照常放行——旧版本的 SQL 里根本不会出现新列名。"""
    t = table(testpb)
    current = _aligned_cols(t)
    del current["player_id"]

    clauses = t.build_alter_clauses(current, expand_only=True)
    assert clauses == [
        "ADD COLUMN `player_id` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:6'"
    ]


@pytest.mark.parametrize(
    ("mutate", "offender"),
    [
        # 类型不兼容 → MODIFY
        (lambda c: c.__setitem__("ip", ColumnMeta("int", 2)), "MODIFY COLUMN"),
        # 改名 → CHANGE
        (lambda c: c.__setitem__("old_ip", c.pop("ip")), "CHANGE COLUMN"),
    ],
)
def test_expand_only_refuses_modify_and_change(testpb, mutate, offender):
    """expand_only 拦下 MODIFY / CHANGE —— 滚动发布时它们会被新旧副本来回执行。"""
    t = table(testpb)
    current = _aligned_cols(t)
    mutate(current)

    with pytest.raises(ExpandOnlyViolationError) as exc:
        t.build_alter_clauses(current, expand_only=True)
    assert offender in str(exc.value)
    # 关掉开关就是既有行为，照常生成。
    assert any(c.startswith(offender) for c in t.build_alter_clauses(current))


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


# ── 收窄抑制必须留痕 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "target", "suppressed"),
    [
        ("bigint unsigned", "int unsigned NOT NULL DEFAULT 0", True),
        ("mediumtext", "varchar(255)", True),
        ("varchar(64)", "varchar(32)", True),
        ("double", "float NOT NULL DEFAULT 0", True),
        ("datetime(6)", "DATETIME(3)", True),
        # 下面这些不是"挡下收窄"，是本来就一样 / 或者需要拓宽
        ("int unsigned", "int unsigned NOT NULL DEFAULT 0", False),
        ("int unsigned", "bigint unsigned NOT NULL DEFAULT 0", False),
        ("mediumtext", "MEDIUMTEXT", False),
        # 有无符号是值域方向，不是宽窄
        ("bigint unsigned", "int NOT NULL DEFAULT 0", False),
    ],
)
def test_narrowing_suppressed_detection(current, target, suppressed):
    """区分"挡下了一次收窄"和"本来就一样"——只有前者该打日志。"""
    from proto2mysql.table import narrowing_suppressed

    assert narrowing_suppressed(current, target) is suppressed


def test_suppressed_narrowing_leaves_a_trace(testpb, caplog):
    """收窄抑制是本库唯一「什么都不做、也什么都不说」的分支，必须留痕。

    改名有 WARNING、expand_only 违规有带语句清单的报错，唯独这里一声不吭——
    于是有人在 proto 里把 bigint 改回 int、期待列跟着变窄，结果什么也没发生，
    也没有任何线索告诉他为什么。
    """
    t = table(testpb)
    current = _aligned_cols(t)
    current["player_id"] = ColumnMeta("bigint unsigned", 6)  # 与 proto 一致
    current["port"] = ColumnMeta("bigint unsigned", 3)  # 线上更宽（proto 是 uint32）

    with caplog.at_level("INFO", logger="proto2mysql"):
        clauses = t.build_alter_clauses(current)

    assert clauses == [], "线上更宽时不该生成任何 ALTER"
    traces = [r.getMessage() for r in caplog.records if "保持线上的不动" in r.getMessage()]
    assert traces, "挡下收窄必须留一条日志"
    assert "port" in traces[0] and "bigint unsigned" in traces[0]


# ── oneof / repeated Timestamp（建表期 fail-closed） ──────────────────────


def test_oneof_message_is_rejected_at_registration(kitchenpb):
    """含真 oneof 的消息一律拒绝建表——本库表达不了它。

    每个成员各占一列，而行里**没有判别位**：回读逐列 set，最后一个非默认列胜出，
    于是写进去的激活成员会被静默换成别的成员（真库实测：写 b='hello' 读回 c）。
    激活成员的值恰好等于默认值时更是谁也还原不出来。

    与拒绝 sint32 / fixed32 同一个立场：不存在"既有正确用法"，
    每一次回读都在损坏数据，宁可在注册时就拦下。
    """
    from proto2mysql import MessageTable
    from proto2mysql.errors import InvalidFieldKindError

    with pytest.raises(InvalidFieldKindError) as exc:
        MessageTable.from_message(kitchenpb.one_of_probe)
    assert "oneof" in str(exc.value)


def test_proto3_optional_is_not_rejected(kitchenpb):
    """proto3 的 optional 走的是**合成** oneof（只有一个成员、名字是 _<字段名>），
    不能跟真 oneof 一起拒——那会把整张 kitchen_sink 挡在门外。
    """
    from proto2mysql import MessageTable

    table = MessageTable.from_message(kitchenpb.kitchen_sink)
    assert table.has_field("opt_score")


def test_repeated_timestamp_is_a_blob_not_datetime(kitchenpb):
    """`repeated Timestamp` 必须建成 MEDIUMBLOB。

    早先 Timestamp 判定写在 is_repeated 之前，于是它被建成 DATETIME(6)；
    而写入侧 pbconv.is_timestamp_field 带着 `not fd.is_repeated`，同一个字段
    在那边算容器、写下去的是 proto wire 裸字节——STRICT 下每次写入都被 1292 拒。
    """
    from proto2mysql import MessageTable

    table = MessageTable.from_message(kitchenpb.stamped_probe)
    assert table.get_mysql_field_type(table.field("stamps")) == "MEDIUMBLOB"
    assert table.get_mysql_field_type(table.field("stamp_map")) == "MEDIUMBLOB"
    # 单个 Timestamp 不受影响
    assert table.get_mysql_field_type(table.field("single")) == "DATETIME(6)"


# ── 列属性漂移（COLUMN_TYPE 里看不见的那三样） ────────────────────────────


def _aligned(testpb, **overrides):
    """golang_test 完全对齐时的线上列元信息，可按列名覆盖其中一条。"""
    cols = {
        "id": ColumnMeta("int unsigned", 1, nullable=False, auto_increment=True),
        "ip": ColumnMeta("mediumtext", 2, nullable=True),
        "port": ColumnMeta("int unsigned", 3, nullable=False, default="0"),
        "group_id": ColumnMeta("int unsigned", 4, nullable=False, default="0"),
        "player": ColumnMeta("mediumblob", 5, nullable=True),
        "player_id": ColumnMeta("bigint unsigned", 6, nullable=False, default="0"),
    }
    cols.update(overrides)
    return cols


def test_aligned_columns_produce_no_clauses(testpb):
    assert table(testpb).build_alter_clauses(_aligned(testpb)) == []


def test_missing_auto_increment_is_detected(testpb):
    """线上列丢了 AUTO_INCREMENT，COLUMN_TYPE 一个字都看不出来。

    后果很具体：不带主键值的 insert 会全部写 0，第二条就撞 Error 1062。
    """
    cols = _aligned(testpb, id=ColumnMeta("int unsigned", 1, nullable=False, auto_increment=False))
    clauses = table(testpb).build_alter_clauses(cols)
    assert clauses == ["MODIFY COLUMN `id` int unsigned NOT NULL AUTO_INCREMENT COMMENT 'pb:1'"]


def test_online_not_null_where_proto_wants_nullable_is_widened(testpb):
    """线上 NOT NULL 而 proto 要可空——放宽值域，无损，自动改。

    Timestamp 列必然落在这一类：get_mysql_field_type 恒返回不带 NOT NULL 的
    DATETIME(6)，线上一旦被谁改成 NOT NULL，凡是没赋值该字段的行就全部插不进去。
    """
    cols = _aligned(testpb, ip=ColumnMeta("mediumtext", 2, nullable=False))
    assert table(testpb).build_alter_clauses(cols) == [
        "MODIFY COLUMN `ip` MEDIUMTEXT COMMENT 'pb:2'"
    ]


def test_online_nullable_where_proto_wants_not_null_is_reported_not_changed(testpb, caplog):
    """反方向是**收紧**：线上很可能已经有 NULL 行，只报不改。"""
    cols = _aligned(testpb, port=ColumnMeta("int unsigned", 3, nullable=True, default="0"))
    with caplog.at_level("WARNING"):
        assert table(testpb).build_alter_clauses(cols) == []
    assert any("NOT NULL" in r.getMessage() for r in caplog.records)


def test_default_drift_is_reported_not_changed(testpb, caplog):
    cols = _aligned(testpb, port=ColumnMeta("int unsigned", 3, nullable=False, default="7"))
    with caplog.at_level("WARNING"):
        assert table(testpb).build_alter_clauses(cols) == []
    assert any("默认值" in r.getMessage() for r in caplog.records)


def test_unwanted_live_default_is_reported_not_changed(kitchenpb, caplog):
    """目标没有 DEFAULT、线上却有表达式默认值，也属于可见漂移。"""
    from proto2mysql import MessageTable

    t = MessageTable.from_message(kitchenpb.kitchen_sink)
    live = {
        "created_at": ColumnMeta(
            "datetime(6)", 11, nullable=True, default="CURRENT_TIMESTAMP(6)"
        )
    }

    with caplog.at_level("WARNING"):
        clauses = t.build_alter_clauses(live)
    assert not any("`created_at`" in clause for clause in clauses)
    assert any(
        "默认值" in record.getMessage() and "CURRENT_TIMESTAMP(6)" in record.getMessage()
        for record in caplog.records
    )


def test_timestamp_to_datetime_semantics_drift_fails_closed(kitchenpb):
    """同精度也不能把线上 TIMESTAMP 静默视作目标 DATETIME 已对齐。

    两者的时区转换、2038 上限和存储语义不同。自动 MODIFY 会在当前 session
    time_zone 下转换历史值，库无法证明安全；空 migration 又会让漂移永久存在。
    公共 schema diff 必须直接拒绝并要求人工迁移。
    """
    from proto2mysql import MessageTable
    from proto2mysql.errors import Proto2MySQLError

    t = MessageTable.from_message(kitchenpb.kitchen_sink)
    live = {
        fd.name: ColumnMeta(t.get_mysql_field_type(fd), fd.number)
        for fd in t.fields
    }
    live["created_at"] = ColumnMeta("timestamp(6)", 11, nullable=True)

    with pytest.raises(Proto2MySQLError, match="TIMESTAMP.*DATETIME|DATETIME.*TIMESTAMP") as exc:
        t.build_alter_clauses(live)
    assert "人工" in str(exc.value)
    assert "time_zone" in str(exc.value)


def test_column_meta_without_extended_info_skips_attribute_checks(testpb):
    """两参构造的 ColumnMeta（跨语言对拍语料、老调用方）一律不参与属性比较。

    默认成"猜一个值"的话，语料里每一列都会被判成属性漂移、凭空长出 MODIFY 子句，
    对拍当场崩——所以这三项必须是三态，None 表示"不知道，别比"。
    """
    cols = {
        "id": ColumnMeta("int unsigned", 1),
        "ip": ColumnMeta("mediumtext", 2),
        "port": ColumnMeta("int unsigned", 3),
        "group_id": ColumnMeta("int unsigned", 4),
        "player": ColumnMeta("mediumblob", 5),
        "player_id": ColumnMeta("bigint unsigned", 6),
    }
    assert table(testpb).build_alter_clauses(cols) == []


# ── 改名 / 注释回填 / 属性补齐都不许顺手收窄（P0） ──────────────────────


@pytest.mark.parametrize(
    ("current", "target", "aligned"),
    [
        # 目标更窄 -> 保留线上的**类型本体**，但带上目标的属性
        ("bigint unsigned", "int unsigned NOT NULL DEFAULT 0",
         "bigint unsigned NOT NULL DEFAULT 0"),
        ("mediumtext", "varchar(255)", "mediumtext"),
        ("varchar(64)", "varchar(32)", "varchar(64)"),
        ("double", "float NOT NULL DEFAULT 0", "double NOT NULL DEFAULT 0"),
        ("datetime(6)", "DATETIME(0)", "datetime(6)"),
        # 目标更宽或同宽 -> 原样用目标
        ("int unsigned", "bigint unsigned NOT NULL DEFAULT 0",
         "bigint unsigned NOT NULL DEFAULT 0"),
        ("varchar(64)", "MEDIUMTEXT", "MEDIUMTEXT"),
        ("float", "double NOT NULL DEFAULT 0", "double NOT NULL DEFAULT 0"),
        ("int unsigned", "int unsigned NOT NULL DEFAULT 0", "int unsigned NOT NULL DEFAULT 0"),
        # 有无符号翻面不是宽窄问题，照目标走
        ("int", "int unsigned NOT NULL DEFAULT 0", "int unsigned NOT NULL DEFAULT 0"),
    ],
)
def test_aligned_column_type(current, target, aligned):
    """属性必须来自**目标**（NOT NULL / DEFAULT / AUTO_INCREMENT 是 proto 侧的决定，
    COLUMN_TYPE 里根本没有它们），类型本体在目标更窄时来自**线上**。

    紧跟类型本体的那个 unsigned 要一起丢掉，否则会拼出 `bigint unsigned unsigned`。
    """
    from proto2mysql.table import aligned_column_type

    assert aligned_column_type(current, target) == aligned


def test_comment_backfill_never_narrows(testpb):
    """回填 `pb:N` 注释是"因为别的原因"要发 MODIFY 的典型场合。

    线上被 DBA 拓宽成 bigint、类型本来兼容（`is_type_match` 为真），只是注释缺了——
    早先这条 MODIFY 会带着 proto 的 `int unsigned` 一起下去，把列**收窄**回去。
    """
    cols = _aligned(testpb, player_id=ColumnMeta("bigint unsigned", 0, nullable=False, default="0"))
    cols["port"] = ColumnMeta("bigint unsigned", 0, nullable=False, default="0")
    clauses = table(testpb).build_alter_clauses(cols)
    assert clauses == [
        "MODIFY COLUMN `port` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:3'",
        "MODIFY COLUMN `player_id` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:6'",
    ]


def test_attribute_fix_never_narrows(testpb):
    """补属性同理：线上 id 被拓宽成 bigint 且丢了 AUTO_INCREMENT，
    补回自增不能顺手把 bigint 写成 int。"""
    cols = _aligned(
        testpb, id=ColumnMeta("bigint unsigned", 1, nullable=False, auto_increment=False)
    )
    assert table(testpb).build_alter_clauses(cols) == [
        "MODIFY COLUMN `id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT 'pb:1'"
    ]


def test_rename_never_narrows(testpb):
    """改名必须发 CHANGE COLUMN，MySQL 会借着它把整列数据真的搬到新类型上。

    早先改名这条路**只判同族、不判方向**：同一个 proto 改动顺手改个名，
    就从"什么都不做"变成把 5000000000 静默截成 4294967295（非严格模式），
    或者整条 ALTER 失败让服务起不来（严格模式）。
    """
    cols = _aligned(testpb)
    cols.pop("group_id")
    # pb:4 在线上叫 legacy 且被拓宽成了 bigint，proto 要的是 int
    cols["legacy"] = ColumnMeta("bigint unsigned", 4, nullable=False, default="0")
    assert table(testpb).build_alter_clauses(cols) == [
        "CHANGE COLUMN `legacy` `group_id` bigint unsigned NOT NULL DEFAULT 0 COMMENT 'pb:4'"
    ]


@pytest.mark.parametrize("current_type", ["int unsigned", "bigint unsigned"])
def test_rename_by_field_number_rejects_signedness_changes(kitchenpb, current_type):
    """字段号相同也不能把 unsigned 列顺手改成 signed。

    ``kitchen_sink.zone_id`` 是 signed int、字段号 3。线上 ``old_port`` 若是
    unsigned，生成 ``CHANGE ... int`` 会同时翻转值域；其中 bigint unsigned → int
    还会收窄。两种都必须 fail-closed，不能把现存数据交给 MySQL 隐式转换。
    """
    from proto2mysql import ColumnMeta, MessageTable
    from proto2mysql.errors import FieldNumberReusedError

    target = MessageTable.from_message(kitchenpb.kitchen_sink)
    with pytest.raises(FieldNumberReusedError, match="符号|unsigned|转换"):
        target.build_alter_clauses({"old_port": ColumnMeta(current_type, 3)})


def test_generated_index_names_fit_mysql_identifier_limit(testpb):
    """合法的 64 字符表名不能因为自动添加 idx_/uk_ 前缀而生成非法 DDL。"""
    import re

    from proto2mysql import MessageTable, with_indexes, with_primary_key, with_table_name, with_unique_key

    long_table_name = "t" * 64
    opts = [
        with_table_name(long_table_name),
        with_primary_key("id"),
        with_indexes("port", "group_id"),
        with_unique_key("ip"),
    ]
    sql = MessageTable.from_message(testpb.golang_test, opts).get_create_table_sql()
    names = re.findall(r"(?:INDEX|UNIQUE KEY) `([^`]+)`", sql)

    assert len(names) == 3
    assert len(set(names)) == 3
    assert all(len(name) <= 64 for name in names)
    # 名称必须稳定，不能用 Python 的随机化 hash()。
    assert sql == MessageTable.from_message(testpb.golang_test, opts).get_create_table_sql()


# ── 主键列的属性判定（自审发现的 P0） ────────────────────────────────────


def test_primary_key_column_never_demands_nullable(kitchenpb):
    """**主键列在 MySQL 里恒为 NOT NULL**，不能拿"proto 要可空"去要求它。

    combo_key 的主键含 string 列 provider，映射成不带 NOT NULL 的 MEDIUMTEXT。
    早先会判成"线上 NOT NULL 而 proto 要可空"→ 发一条 MODIFY → MySQL 照旧保持
    NOT NULL → 下次启动再发一遍……**无休止**。开了 expand_only 更糟：
    每次启动都抛 ExpandOnlyViolationError，服务永远起不来。
    """
    from proto2mysql import MessageTable

    t = MessageTable.from_message(kitchenpb.combo_key)
    live = {
        "user_id": ColumnMeta("bigint unsigned", 1, nullable=False, default="0"),
        # 主键列，MySQL 强制 NOT NULL —— 而 proto 的目标类型是裸 MEDIUMTEXT
        "provider": ColumnMeta("mediumtext", 2, nullable=False),
        "score": ColumnMeta("bigint", 3, nullable=False, default="0"),
    }
    assert t.build_alter_clauses(live) == []
    # 非主键的同型列则照常要求可空
    assert "MODIFY COLUMN `score`" not in "".join(t.build_alter_clauses(live))


def test_timestamp_attribute_drift_is_not_used_to_bypass_fail_closed(kitchenpb):
    """即使还有 NULL 属性漂移，也不能借 MODIFY 顺手翻转时间语义。"""
    from proto2mysql import MessageTable
    from proto2mysql.errors import Proto2MySQLError

    t = MessageTable.from_message(kitchenpb.kitchen_sink)
    live = {"created_at": ColumnMeta("timestamp(6)", 11, nullable=False)}

    with pytest.raises(Proto2MySQLError, match="拒绝自动迁移"):
        t.build_alter_clauses(live)


# ── 合成 oneof 的判据：只能看 proto3_optional，不能看名字 ─────────────────


def test_synthetic_oneof_with_colliding_name_is_allowed(kitchenpb):
    """`_<字段名>` 与消息里已有的名字冲突时，protoc 把合成 oneof 改名成 `X_<字段名>`。

    按名字判的话，这份**完全合法**的 proto3 消息会被 fail-closed 的闸硬拒——
    一份改动前能正常注册的 proto，升级之后建不了表了。
    """
    from proto2mysql import MessageTable

    md = kitchenpb.oneof_name_collision.DESCRIPTOR
    assert [o.name for o in md.oneofs] == ["X_x"], "前提：protoc 确实改名了"
    table = MessageTable.from_message(kitchenpb.oneof_name_collision)
    assert table.has_field("x") and table.has_field("_x")


def test_real_oneof_named_like_a_synthetic_one_is_still_rejected(kitchenpb):
    """反方向：真写一个 `oneof _x { int32 x = 2; }`，名字恰好长得像合成的。

    按名字判会把它放过去，而它每次回读都在损坏数据。
    """
    from proto2mysql import MessageTable
    from proto2mysql.errors import InvalidFieldKindError

    with pytest.raises(InvalidFieldKindError):
        MessageTable.from_message(kitchenpb.fake_synthetic_oneof)
