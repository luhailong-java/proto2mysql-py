"""proto2mysql —— 把 Protobuf 消息自动映射成 MySQL 表结构，并提供参数化的 CRUD / SQL 生成。

Go 版 https://github.com/luyuancpp/proto2mysql 的 Python 实现，生成的 SQL 与其逐字节一致。

三层用法，按需要的耦合度选：

1. **只要 DDL**（不连库）::

       from proto2mysql import generate_create_table_sql
       print(generate_create_table_sql(pb.User))

2. **只要 DML**（不连库、不执行）::

       from proto2mysql import SQLBuilder
       b = SQLBuilder.from_message(pb.User)
       stmt = b.upsert_add(row, "gold")
       cur.execute(*stmt.for_paramstyle("format"))

3. **连库执行**::

       from proto2mysql import DB
       db = DB(pymysql.connect(...), "testdb")
       db.register_all_tables()
       db.sync_all_tables()
       db.insert(user)

表配置（表名/主键/自增/索引/唯一键/可空）写在 .proto 的 option 里，见
``proto2mysql.proto_include_path()`` 指向的 proto2mysql_option.proto。
"""

from __future__ import annotations

from pathlib import Path

from .cache import Cache, DictCache
from .db import DB, MultiQuery, get_element_table_name
from .errors import (
    BatchSizeExceededError,
    CacheMissError,
    DuplicateKeyError,
    EmptyWhereClauseError,
    ExpandOnlyViolationError,
    FieldNotFoundError,
    FieldNumberReusedError,
    InvalidFieldKindError,
    MultipleRepeatedFieldError,
    MultipleRowsFoundError,
    NoRepeatedFieldError,
    NoRowsFoundError,
    NonFiniteFloatError,
    PrimaryKeyNotFoundError,
    Proto2MySQLError,
    TableNotFoundError,
)
from .options import (
    TableOption,
    file_has_db_option,
    table_name_from_descriptor,
    table_options_from_descriptor,
    with_auto_increment_key,
    with_indexes,
    with_nullable_fields,
    with_primary_key,
    with_table_name,
    with_unique_key,
)
from .registry import iter_file_descriptors
from .sqlbuilder import (
    Assign,
    EmptyValuesError,
    NoAssignsError,
    NoFieldsSetError,
    QueryOptions,
    SQLBuilder,
    add_col,
    add_new,
    keep_old,
    max_new,
    min_new,
    set_col,
    set_col_expr,
    set_new,
    set_new_if_zero,
    sub_col,
)
from .sqlgen import (
    dump_create_table_sql_file,
    generate_create_table_sql,
    write_create_table_sql,
)
from .table import (
    BATCH_INSERT_MAX_SIZE,
    MYSQL_FIELD_TYPES,
    ColumnMeta,
    MessageTable,
    Statement,
    escape_mysql_name,
)

__version__ = "0.1.0"


def proto_include_path() -> str:
    """随包分发的 proto2mysql_option.proto 所在目录，用于 protoc 的 -I。

        protoc -I$(python -c "import proto2mysql;print(proto2mysql.proto_include_path())") ...

    本库运行期**不 import** 生成的 option stub（按字段号读扩展），
    所以这个目录只在你编译自己的 .proto 时需要。
    """
    return str(Path(__file__).parent / "proto")


__all__ = [
    "__version__",
    "proto_include_path",
    # 核心
    "DB",
    "SQLBuilder",
    "MessageTable",
    "Statement",
    "QueryOptions",
    "ColumnMeta",
    "Cache",
    "DictCache",
    "BATCH_INSERT_MAX_SIZE",
    "MYSQL_FIELD_TYPES",
    "escape_mysql_name",
    "iter_file_descriptors",
    # DDL
    "generate_create_table_sql",
    "write_create_table_sql",
    "dump_create_table_sql_file",
    "MultiQuery",
    "get_element_table_name",
    # 表选项
    "TableOption",
    "with_table_name",
    "with_primary_key",
    "with_indexes",
    "with_unique_key",
    "with_auto_increment_key",
    "with_nullable_fields",
    "file_has_db_option",
    "table_name_from_descriptor",
    "table_options_from_descriptor",
    # 赋值子句
    "Assign",
    "set_col",
    "add_col",
    "sub_col",
    "set_col_expr",
    "set_new",
    "add_new",
    "min_new",
    "max_new",
    "set_new_if_zero",
    "keep_old",
    # 错误
    "Proto2MySQLError",
    "TableNotFoundError",
    "NoRepeatedFieldError",
    "MultipleRepeatedFieldError",
    "PrimaryKeyNotFoundError",
    "FieldNotFoundError",
    "FieldNumberReusedError",
    "MultipleRowsFoundError",
    "NoRowsFoundError",
    "DuplicateKeyError",
    "BatchSizeExceededError",
    "EmptyWhereClauseError",
    "ExpandOnlyViolationError",
    "InvalidFieldKindError",
    "NonFiniteFloatError",
    "CacheMissError",
    "NoFieldsSetError",
    "NoAssignsError",
    "EmptyValuesError",
]
