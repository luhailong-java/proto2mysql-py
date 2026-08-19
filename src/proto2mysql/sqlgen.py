"""离线 DDL 生成：不连库就能产出 CREATE TABLE；连库时可产出迁移用的 ALTER TABLE。

对应 Go 版的 sqlgen.go。迁移（generate_migration_sql / write_migration_sql）需要读
information_schema 比对线上结构，所以挂在 :class:`proto2mysql.db.DB` 上；
这里只放不连库的那部分。
"""

from __future__ import annotations

from typing import IO, Iterable

from google.protobuf.message import Message

from .options import TableOption
from .table import MessageTable


def generate_create_table_sql(
    m: Message | type[Message], *opts: TableOption
) -> str:
    """由消息（实例或类）直接生成 CREATE TABLE 语句，不需要注册也不需要连库。

        sql = generate_create_table_sql(pb.Player, with_primary_key("id"))
    """
    return MessageTable.from_message(m, opts).get_create_table_sql()


def write_create_table_sql(w: IO[str], tables: Iterable[MessageTable]) -> None:
    """把若干表的 CREATE TABLE 写入 w，**按表名排序**，输出稳定。

    排序不是审美问题：schema.sql 一般进版本库，顺序不稳定会让每次生成都产生假 diff。
    Go 版同样显式排序（Go 的 map 遍历顺序本身就是随机的）。
    """
    for table in sorted(tables, key=lambda t: t.table_name):
        w.write(table.get_create_table_sql())
        w.write("\n\n")


def dump_create_table_sql_file(path: str, tables: Iterable[MessageTable]) -> None:
    """把若干表的建表语句写到文件（覆盖写）。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        write_create_table_sql(f, tables)
