"""跨语言对拍：Python 侧的语料发射器。

「Go 版与 Python 版产出逐字节相同的 SQL」是本库的核心契约——两边跑同一份 .proto
必须产出同一份 DDL/DML，否则"并存迁移"就没有可验证的基准。

但这条契约在此之前**全靠人手把 Go 的字符串抄进 Python 的测试文件**，
没有任何自动化在守。抄漏一条、抄错一个反引号，谁也不会知道——
而 2026-08 那一轮就实测到了两处真实分叉（TEXT 索引前缀长度、sqlgen 排序键）。

用法::

    python tools/parity_emit.py -o parity.py.json

然后与 Go 侧的产物比对::

    python tools/parity_diff.py parity.go.json parity.py.json

⚠️ 语料的用例清单是**两边共同的规格**：新增/删除用例必须两边同步改
（Go 侧在 parity_emit_test.go），否则对拍器会报"用例集不一致"——那正是它该报的。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "gen"))

import testpb_pb2 as testpb  # noqa: E402

from proto2mysql import (  # noqa: E402
    ColumnMeta,
    MessageTable,
    QueryOptions,
    SQLBuilder,
    with_auto_increment_key,
    with_indexes,
    with_nullable_fields,
    with_primary_key,
    with_unique_key,
)

CORPUS_VERSION = 1


def _is_printable_utf8(raw: bytes) -> bool:
    """是不是「合法 UTF-8 且不含控制字符」。

    判据必须与 Go 侧 isPrintableUTF8 逐字一致，否则同一个值两边会落进不同分支。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(ord(ch) >= 0x20 and ord(ch) != 0x7F for ch in text)


def parity_arg(v) -> str:
    """把一个参数归一成**跨语言可比**的字符串。

    两边的参数是不同语言的原生值，直接比没有意义。而且同一个逻辑值在两边的类型
    还不一样：子消息的序列化字节，Go 那边是 str（Go 的 string 能装任意字节），
    Python 这边是 bytes——第一次跑对拍就是被这个绊了 10 条。

    所以归一规则必须**先落到原始字节**，再按同一套判据决定怎么写。
    两边逐字一致：

        None / nil                      → "<nil>"
        原始字节是可打印的 UTF-8 文本    → 原样保留（diff 可读）
        否则                            → "0x" + 小写十六进制
    """
    if v is None:
        return "<nil>"
    if isinstance(v, (bytes, bytearray)):
        raw = bytes(v)
    elif isinstance(v, str):
        raw = v.encode("utf-8")
    else:
        return str(v)
    return raw.decode("utf-8") if _is_printable_utf8(raw) else "0x" + raw.hex()


def build_corpus() -> dict:
    cases: list[dict] = []

    def add(name: str, sql: str, args=()) -> None:
        cases.append({"name": name, "sql": sql, "args": [parity_arg(a) for a in args]})

    def emit(name: str, stmt) -> None:
        add(name, stmt.sql, stmt.args)

    # ── 样本消息（两边必须逐字一致）────────────────────────────────────
    full = testpb.golang_test(
        id=7, ip="10.0.0.1", port=3306, group_id=42,
        player=testpb.player(player_id=99, name="alice"), player_id=1001,
    )
    sparse = testpb.golang_test(id=7, ip="x")  # 只赋两个字段，验 proto3 零值语义
    pk_only = testpb.golang_test(id=7)
    batch = [testpb.golang_test(id=1, ip="a"), testpb.golang_test(id=2, ip="b")]

    def builder(*opts) -> SQLBuilder:
        # 注意两个 from_message 的签名不一样：SQLBuilder 是 *opts 变参，
        # MessageTable 是可迭代——这是既有 API 的不一致，别踩。
        return SQLBuilder.from_message(testpb.golang_test, *opts)

    # ── DDL ────────────────────────────────────────────────────────────
    add("ddl/create/plain", builder().create_table())
    add("ddl/create/pk_autoinc",
        builder(with_primary_key("id"), with_auto_increment_key("id")).create_table())
    add("ddl/create/index_unique",
        builder(with_primary_key("id"), with_indexes("player_id,group_id"),
                with_unique_key("ip")).create_table())
    add("ddl/create/nullable",
        builder(with_primary_key("id"), with_nullable_fields("port")).create_table())

    # ── schema.sql 的**文件级顺序**（曾经的真实分叉）─────────────────────
    #
    # Go 早先按注册键（proto full name）排、Python 按 table_name 排。两者在没声明
    # table_name 时恰好相同，所以长期看不出来；一旦用了 with_table_name，
    # 同一批语句会以不同顺序落进 schema.sql，逐字节就不一致了。
    # 逐表断言的 golden 盖不到这个，只有这条对拍能守住。
    import io as _io

    from proto2mysql import with_table_name, write_create_table_sql

    schema_tables = [
        MessageTable.from_message(testpb.golang_test,
                                  [with_table_name("zzz_first"), with_primary_key("id")]),
        MessageTable.from_message(testpb.golang_test1,
                                  [with_table_name("aaa_second"), with_primary_key("id")]),
    ]
    buf = _io.StringIO()
    write_create_table_sql(buf, schema_tables)
    add("ddl/schema_file/multi_table_order", buf.getvalue())

    # ── ALTER（本轮修复集中的地方）─────────────────────────────────────
    alter_table = MessageTable.from_message(testpb.golang_test, [with_primary_key("id")])
    ip_type = alter_table.get_mysql_field_type(alter_table.descriptor.fields_by_name["ip"])
    alter_cases = [
        ("alter/empty_table", {}),
        ("alter/missing_one_column", {
            "id": ColumnMeta("int unsigned", 1), "ip": ColumnMeta("mediumtext", 2),
            "port": ColumnMeta("int unsigned", 3), "group_id": ColumnMeta("int unsigned", 4),
            "player": ColumnMeta("mediumblob", 5),
        }),
        ("alter/rename_by_field_number", {
            "id": ColumnMeta("int unsigned", 1), "old_ip": ColumnMeta(ip_type, 2),
            "port": ColumnMeta("int unsigned", 3), "group_id": ColumnMeta("int unsigned", 4),
            "player": ColumnMeta("mediumblob", 5), "player_id": ColumnMeta("bigint unsigned", 6),
        }),
        ("alter/backfill_comment", {
            "id": ColumnMeta("int unsigned", 0), "ip": ColumnMeta("mediumtext", 0),
            "port": ColumnMeta("int unsigned", 0), "group_id": ColumnMeta("int unsigned", 0),
            "player": ColumnMeta("mediumblob", 0), "player_id": ColumnMeta("bigint unsigned", 0),
        }),
        ("alter/wider_online_column_untouched", {
            "id": ColumnMeta("bigint unsigned", 1), "ip": ColumnMeta("longtext", 2),
            "port": ColumnMeta("bigint unsigned", 3), "group_id": ColumnMeta("bigint unsigned", 4),
            "player": ColumnMeta("longblob", 5), "player_id": ColumnMeta("bigint unsigned", 6),
        }),
    ]
    for name, cols in alter_cases:
        clauses = alter_table.build_alter_clauses(cols)
        for i, clause in enumerate(clauses):
            add(f"{name}/#{i}", clause)
        if not clauses:
            add(f"{name}/#none", "")

    # ── DML ────────────────────────────────────────────────────────────
    b = builder(with_primary_key("id"))
    emit("dml/insert", b.insert(full))
    emit("dml/insert_set_fields", b.insert_set_fields(sparse))
    emit("dml/insert_ignore", b.insert_ignore(full))
    emit("dml/replace", b.replace(full))
    emit("dml/save", b.table.get_save_sql(full))
    emit("dml/batch_insert", b.batch_insert(batch))
    emit("dml/batch_replace", b.batch_replace(batch))
    emit("dml/batch_save", b.table.get_batch_save_sql(batch))
    emit("dml/upsert", b.upsert(full, "ip", "port"))
    emit("dml/upsert_add", b.upsert_add(full, "port"))
    emit("dml/upsert_keep_old", b.upsert_keep_old(full))

    emit("dml/update_by_pk", b.update_by_pk(sparse))
    emit("dml/update_by_pk_if", b.update_by_pk_if(sparse, "`group_id` = ?", [3]))
    emit("dml/update_fields_by_pk", b.update_fields_by_pk(pk_only, "port"))
    emit("dml/incr_by_pk", b.incr_by_pk(pk_only, "port", 5))
    emit("dml/decr_by_pk_if_enough", b.decr_by_pk_if_enough(pk_only, "port", 5))

    emit("dml/select_by_pk", b.select_by_pk(pk_only))
    emit("dml/select_by_pk_for_update", b.select_by_pk_for_update(pk_only))
    add("dml/select_where_plain", b.select_where("`port` > ?", [100]).sql, [100])
    add("dml/select_where_paged",
        b.select_where("`port` > ?", [100],
                       QueryOptions(order_by="`id` DESC", limit=20, offset=40)).sql, [100])
    add("dml/count", b.count("`port` > ?", [100]).sql, [100])
    add("dml/exists", b.exists("`port` > ?", [100]).sql, [100])
    emit("dml/delete_by_pk", b.delete_by_pk(pk_only))

    # ── 补齐其余公开方法（覆盖闸会强制这里不许漏）─────────────────────
    from proto2mysql import add_col, add_new, set_col, set_new, sub_col

    emit("dml/insert_ignore_set_fields", b.insert_ignore_set_fields(sparse))
    emit("dml/batch_insert_ignore", b.batch_insert_ignore(batch))
    emit("dml/batch_upsert", b.batch_upsert(batch, "ip"))
    emit("dml/batch_upsert_with", b.batch_upsert_with(batch, add_new("port")))
    emit("dml/upsert_with", b.upsert_with(full, set_new("ip"), add_new("port")))
    add("dml/create_table", b.create_table())

    pk_where, pk_args = b.primary_key_where(pk_only)
    add("dml/primary_key_where", pk_where, pk_args)
    add("dml/table_name", b.table_name)

    emit("dml/select_columns", b.select_columns(["id", "ip"], "`port` > ?", [100],
                                                QueryOptions(order_by="`id` ASC", limit=5)))
    emit("dml/select_by_kv_in", b.select_by_kv_in("port", [80, 443]))
    emit("dml/select_by_pk_in", b.select_by_pk_in([1, 2, 3]))
    emit("dml/exists_by_pk_for_update", b.exists_by_pk_for_update(pk_only))

    emit("dml/update_where", b.update_where(sparse, "`port` > ?", [100]))
    emit("dml/update_assigns_by_pk", b.update_assigns_by_pk(pk_only, add_col("port", 5), set_col("ip", "z")))
    emit("dml/update_assigns_where", b.update_assigns_where([sub_col("port", 3)], "`id` = ?", [7]))

    emit("dml/delete_by_pk_if", b.delete_by_pk_if(pk_only, "`port` = ?", [0]))
    emit("dml/delete_by_pk_in", b.delete_by_pk_in([1, 2]))
    emit("dml/delete_by_kv_in", b.delete_by_kv_in("port", [80, 443]))
    emit("dml/delete_where", b.delete_where("`port` = ?", [0]))
    emit("dml/delete_where_limit", b.delete_where_limit("`port` = ?", [0], "`id` ASC", 10))

    return {"corpus_version": CORPUS_VERSION, "lang": "py", "cases": cases}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="发射跨语言对拍语料（Python 侧）")
    ap.add_argument("-o", "--output", required=True, help="输出的 JSON 文件")
    args = ap.parse_args(argv)

    corpus = build_corpus()
    Path(args.output).write_text(
        json.dumps(corpus, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(f"已发射 {len(corpus['cases'])} 条用例 -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
