"""SQLBuilder：由 protobuf 消息直接产出各类参数化 SQL，**不连库、不执行**。

对应 Go 版的 sqlbuilder.go。与本包其它模块的分工：

* :mod:`proto2mysql.db`     —— 连库执行（insert / find_one_by_pk / ...），SQL 只是中间产物
* :mod:`proto2mysql.sqlgen` —— 只产 DDL（CREATE TABLE / ALTER TABLE 迁移）
* 本模块                    —— 只产 DML（INSERT / SELECT / UPDATE / DELETE）

适用场景：把语句交给已有的连接/连接池/SQLAlchemy 自己执行、塞进事务里和手写 SQL 混用、
或先打日志再执行。

约定（与 Go 版逐条一致）：

* 返回的 SQL **不带结尾分号**；
* 列名一律转义并校验存在于该 message，未知列抛 :class:`FieldNotFoundError`；
* 取值一律走 pbconv.serialize_field_value（标量以字符串下发由 MySQL 隐式转换，
  未设置的 Timestamp 下发 SQL NULL）；
* ``where_clause`` / ``order_by`` / ``guard`` / ``set_col_expr`` 的表达式是**原样拼接的裸
  SQL**，只能来自代码常量，取值一律走 args。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

from . import pbconv
from .errors import (
    EmptyWhereClauseError,
    PrimaryKeyNotFoundError,
    Proto2MySQLError,
)
from .options import TableOption
from .table import (
    NUMERIC_FIELD_TYPES,
    MessageTable,
    Statement,
    build_placeholders,
    escape_mysql_name,
)


class NoFieldsSetError(Proto2MySQLError):
    """消息里没有任何已赋值字段，无法生成列子集语句。对应 Go 的 ErrNoFieldsSet。"""


class NoAssignsError(Proto2MySQLError):
    """没有提供任何赋值子句。对应 Go 的 ErrNoAssigns。"""


class EmptyValuesError(Proto2MySQLError):
    """传入的取值列表为空（IN (...) 无法生成合法 SQL）。对应 Go 的 ErrEmptyValues。"""


_T = FieldDescriptor

# 数值列判定统一放在 table.NUMERIC_FIELD_TYPES（那边写了为什么只能有一份），
# 这里留个别名，老代码与外部引用不受影响。
_NUMERIC_TYPES = NUMERIC_FIELD_TYPES


@dataclass(frozen=True)
class QueryOptions:
    """查询修饰选项，对应 MySQL 的 ORDER BY / LIMIT / OFFSET / FOR UPDATE。"""

    order_by: str = ""  # 排序表达式，如 "id DESC"（直接拼入 SQL，勿传入不可信输入）
    limit: int = 0  # 返回行数上限，<=0 表示不限制
    offset: int = 0  # 跳过的行数，仅在 limit > 0 时生效
    # for_update 追加 FOR UPDATE 行锁（只在事务内有意义，事务外单句自动提交，锁即刻释放）。
    # 用于"先锁后改"：读到加锁后的最新值，防止并发读-改-写丢更新。
    for_update: bool = False

    def sql_suffix(self) -> str:
        """生成 ORDER BY/LIMIT/OFFSET/FOR UPDATE 后缀（以空格开头，可能为空串）。"""
        parts: list[str] = []
        if self.order_by:
            parts.append(f" ORDER BY {self.order_by}")
        if self.limit > 0:
            parts.append(f" LIMIT {self.limit}")
            if self.offset > 0:
                parts.append(f" OFFSET {self.offset}")
        if self.for_update:
            parts.append(" FOR UPDATE")
        return "".join(parts)


def normalize_where_clause(where_clause: str) -> str:
    """SELECT 侧的空条件退化为全表（与 Go 版一致；UPDATE/DELETE 侧另有硬拒绝）。"""
    return where_clause if where_clause else "1=1"


def require_mutation_where_clause(where_clause: str) -> str:
    """拒绝 UPDATE/DELETE 的隐式全表条件。

    确需操作全表时，调用方必须显式传入 "1=1"，让危险意图在代码审查中可见。
    """
    if not where_clause or not where_clause.strip():
        raise EmptyWhereClauseError("empty where clause")
    return where_clause


# ── 赋值子句：UPDATE 的 SET、以及 ON DUPLICATE KEY UPDATE 的更新部分共用 ──


@dataclass(frozen=True)
class Assign:
    """一条赋值子句（col = 表达式）。

    用 :func:`set_col` / :func:`add_col` / :func:`set_new` 等构造函数创建，别直接构造。
    """

    col: str  # 原始列名，构建时用于校验该列存在于消息
    expr: str  # 完整赋值表达式，列名已转义，如 "`gold` = `gold` + ?"
    args: tuple = ()  # expr 里 ? 对应的参数，按出现顺序
    #: 该表达式对列做算术运算、或拿 0 当哨兵比较。构造期必须确认列是数值型——
    #: 落到文本列上 MySQL 会隐式转换，非严格模式下静默把原值抹掉（见
    #: MessageTable.numeric_field）。校验统一在 _build_assigns 那一个汇聚点做。
    numeric: bool = False


def set_col(col: str, val: Any) -> Assign:
    """设为给定值：``col = ?``"""
    e = escape_mysql_name(col)
    return Assign(col, f"{e} = ?", (val,))


def add_col(col: str, delta: Any) -> Assign:
    """原地累加：``col = col + ?``（货币/经验/计数器，避免读-改-写竞态）"""
    e = escape_mysql_name(col)
    return Assign(col, f"{e} = {e} + ?", (delta,), numeric=True)


def sub_col(col: str, delta: Any) -> Assign:
    """原地扣减：``col = col - ?``（是否允许扣成负数由 WHERE 守卫决定）"""
    e = escape_mysql_name(col)
    return Assign(col, f"{e} = {e} - ?", (delta,), numeric=True)


def set_col_expr(col: str, expr: str, *args: Any) -> Assign:
    """自定义赋值表达式：``col = <expr>``，expr 里的 ? 由 args 依次填充。

    expr 原样拼入 SQL（如 ``"NOW()"``、``"IF(`a` = 0, ?, `a`)"``），**勿传入不可信输入**。
    """
    return Assign(col, f"{escape_mysql_name(col)} = {expr}", tuple(args))


# 下面几个只用于 INSERT ... ON DUPLICATE KEY UPDATE：VALUES(col) 表示
# "本次本该插入的新值"。冲突时按不同语义决定新旧值怎么合并。
#
# 注意：VALUES() 在 MySQL 8.0.20 起被标记 deprecated（官方建议改用 AS new 行别名），
# 但至今仍可用，且兼容 5.7；本库沿用 VALUES() 以覆盖更宽的版本范围。


def set_new(col: str) -> Assign:
    """冲突时覆盖为新值：``col = VALUES(col)``"""
    e = escape_mysql_name(col)
    return Assign(col, f"{e} = VALUES({e})")


def add_new(col: str) -> Assign:
    """冲突时累加新值：``col = col + VALUES(col)``（发奖/加币的原子累加写法）"""
    e = escape_mysql_name(col)
    return Assign(col, f"{e} = {e} + VALUES({e})", numeric=True)


def min_new(col: str) -> Assign:
    """冲突时取较小值：``col = LEAST(col, VALUES(col))``（退避时间取更早的一次）"""
    e = escape_mysql_name(col)
    return Assign(col, f"{e} = LEAST({e}, VALUES({e}))", numeric=True)


def max_new(col: str) -> Assign:
    """冲突时取较大值：``col = GREATEST(col, VALUES(col))``（水位/序号只增不减）"""
    e = escape_mysql_name(col)
    return Assign(col, f"{e} = GREATEST({e}, VALUES({e}))", numeric=True)


def set_new_if_zero(col: str) -> Assign:
    """首写生效：``col = IF(col = 0, VALUES(col), col)``

    已经写过（非 0）就保持不动，用于"第一次落的时间戳/终态不可被覆盖"。

    只能用在数值列上：``'abc' = 0`` 在非严格 sql_mode 下为**真**，
    于是一个早就写过的文本列会被判成"还没写过"，第二次写照样覆盖掉。
    """
    e = escape_mysql_name(col)
    return Assign(col, f"{e} = IF({e} = 0, VALUES({e}), {e})", numeric=True)


def keep_old(col: str) -> Assign:
    """保持原值不变：``col = col``。

    用于 INSERT ... ON DUPLICATE KEY UPDATE 的"插入或加锁"写法：不改任何数据，
    只为在冲突时也持有该行的行锁（相比 INSERT IGNORE 不会静默吞掉真实错误）。
    """
    e = escape_mysql_name(col)
    return Assign(col, f"{e} = {e}")


def _append_guard(where: str, guard: str) -> str:
    """把守卫条件用 AND 接到已有 WHERE 片段后（guard 为空时原样返回）。"""
    if not guard or not guard.strip():
        return where
    return f"{where} AND {guard}"


class SQLBuilder:
    """单张表的 SQL 生成器。构造后只读，可并发共享。"""

    __slots__ = ("_table",)

    def __init__(self, table: MessageTable) -> None:
        self._table = table

    @classmethod
    def from_message(
        cls, message: Message | type[Message], *opts: TableOption
    ) -> "SQLBuilder":
        """由消息（实例或类）直接构造：表配置优先取 proto 里声明的 option，opts 可覆盖。

        不连库、不需要 register_table。

            b = SQLBuilder.from_message(pb.Player)
            stmt = b.upsert(player, "gold")
            cur.execute(*stmt.for_paramstyle("format"))
        """
        return cls(MessageTable.from_message(message, opts))

    # ── 元信息 ──────────────────────────────────────────────────────────

    @property
    def table(self) -> MessageTable:
        """底层表映射，供需要字段描述符等细节的调用方使用。"""
        return self._table

    @property
    def table_name(self) -> str:
        """生成 SQL 时使用的表名（未转义）。"""
        return self._table.table_name

    def create_table(self) -> str:
        """建表语句（等价 generate_create_table_sql，带结尾分号）。"""
        return self._table.get_create_table_sql()

    def primary_key_where(self, m: Message) -> tuple[str, list[Any]]:
        """按主键定位的 WHERE 片段与参数（不含 "WHERE" 关键字）。"""
        return self._table.primary_key_where(m)

    def _check_column(self, col: str) -> FieldDescriptor:
        return self._table.field(col)

    def _check_numeric_column(self, col: str) -> FieldDescriptor:
        return self._table.numeric_field(col)

    # ── INSERT ──────────────────────────────────────────────────────────

    def insert(self, m: Message) -> Statement:
        """全字段插入：``INSERT INTO t (所有列) VALUES (?, ...)``"""
        return self._table.get_insert_sql(m)

    def insert_set_fields(self, m: Message) -> Statement:
        """只插入"已赋值字段"，未赋值的列交给 MySQL 的列默认值。

        （自增主键、DEFAULT CURRENT_TIMESTAMP 的 created_at 等。）

        注意 proto3 语义：非 optional 的标量字段，值为 0/""/false 时视为**未赋值**会被跳过。
        要显式写入零值，把该字段声明为 optional，或改用 :meth:`insert`。
        """
        cols, args = self._set_field_columns(m)
        stmt = (
            f"INSERT INTO {escape_mysql_name(self._table.table_name)} "
            f"({', '.join(cols)}) VALUES ({build_placeholders(len(cols))})"
        )
        return Statement(stmt, args)

    def insert_ignore(self, m: Message) -> Statement:
        """幂等插入：重复键时不改数据，其他错误仍按原样报错。"""
        stmt = self._table.get_insert_sql(m)
        return self._ignore_duplicate(stmt)

    def insert_ignore_set_fields(self, m: Message) -> Statement:
        """列子集版的幂等插入（列选取规则同 :meth:`insert_set_fields`）。"""
        stmt = self.insert_set_fields(m)
        return self._ignore_duplicate(stmt)

    def replace(self, m: Message) -> Statement:
        """整行替换：``REPLACE INTO ...``

        冲突时先删后插，会丢掉未提供的列并触发外键级联，慎用。
        """
        return self._table.get_replace_sql(m)

    def batch_insert(self, msgs: Sequence[Message]) -> Statement:
        """批量插入，条数上限 BATCH_INSERT_MAX_SIZE。"""
        return self._table.get_batch_insert_sql(msgs)

    def batch_insert_ignore(self, msgs: Sequence[Message]) -> Statement:
        stmt = self._table.get_batch_insert_sql(msgs)
        return self._ignore_duplicate(stmt)

    def _ignore_duplicate(self, insert: Statement) -> Statement:
        """用无副作用 ODKU 实现“仅忽略重复键”。

        ``INSERT IGNORE`` 不只忽略重复键，还会把截断、非空等真实数据
        错误降成 warning。自赋值 ODKU 仅吞掉冲突，其他错误仍 fail-closed。
        """
        anchor = next(
            (name for name in self._table.primary_key if self._table.has_field(name)),
            None,
        )
        if anchor is None:
            if not self._table.fields:
                raise Proto2MySQLError(
                    f"cannot ignore duplicate for empty message table {self._table.table_name}"
                )
            anchor = self._table.fields[0].name
        escaped = escape_mysql_name(anchor)
        return Statement(
            f"{insert.sql} ON DUPLICATE KEY UPDATE {escaped} = {escaped}", insert.args
        )

    def batch_replace(self, msgs: Sequence[Message]) -> Statement:
        return self._table.get_batch_replace_sql(msgs)

    # ── UPSERT ──────────────────────────────────────────────────────────

    def upsert(self, m: Message, *cols: str) -> Statement:
        """插入或覆盖：``INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col)``。

        cols 为空时默认覆盖所有非主键列。
        """
        return self._upsert_by(m, list(cols), set_new)

    def upsert_add(self, m: Message, *cols: str) -> Statement:
        """插入或累加：``INSERT ... ON DUPLICATE KEY UPDATE col = col + VALUES(col)``。

        必须显式指定数值列，避免把 string/BLOB 等非数值列交给 MySQL 隐式转换后写坏数据。
        """
        if not cols:
            raise NoAssignsError("no assignments provided")
        for col in cols:
            self._check_numeric_column(col)
        return self._upsert_by(m, list(cols), add_new)

    def upsert_keep_old(self, m: Message) -> Statement:
        """插入或占位：``INSERT ... ON DUPLICATE KEY UPDATE pk = pk``。

        行不存在则建行，存在则不改任何数据但持有行锁——事务里"确保这行存在并锁住它"
        的标准写法。
        """
        if not self._table.primary_key:
            raise PrimaryKeyNotFoundError(
                f"primary key not found: table {self._table.table_name}"
            )
        return self.upsert_with(m, keep_old(self._table.primary_key[0]))

    def upsert_with(self, m: Message, *assigns: Assign) -> Statement:
        """插入或按自定义语义合并：更新部分由 assigns 决定。

        可混用 set_new / add_new / min_new / max_new / set_new_if_zero / set_col / set_col_expr::

            # 冲突时：代次 +1、jti 换成新值、首次写入的时间戳不被覆盖
            b.upsert_with(row,
                set_col_expr("generation", "`generation` + 1"),
                set_new("sess_jti"),
                set_new_if_zero("first_seen_ms"))
        """
        insert = self._table.get_insert_sql(m)
        return self._append_on_duplicate(insert, list(assigns))

    def batch_upsert(self, msgs: Sequence[Message], *cols: str) -> Statement:
        """批量插入或覆盖：多行 VALUES + ``ON DUPLICATE KEY UPDATE col = VALUES(col)``。"""
        insert = self._table.get_batch_insert_sql(msgs)
        return self._append_on_duplicate(insert, self._assigns_for_cols(list(cols), set_new))

    def batch_upsert_with(self, msgs: Sequence[Message], *assigns: Assign) -> Statement:
        """批量插入 + 自定义冲突合并语义。"""
        insert = self._table.get_batch_insert_sql(msgs)
        return self._append_on_duplicate(insert, list(assigns))

    def _upsert_by(
        self, m: Message, cols: list[str], mk: Callable[[str], Assign]
    ) -> Statement:
        return self.upsert_with(m, *self._assigns_for_cols(cols, mk))

    def _assigns_for_cols(
        self, cols: list[str], mk: Callable[[str], Assign]
    ) -> list[Assign]:
        """cols 为空时取所有非主键列，逐列用 mk 构造赋值子句。"""
        if not cols:
            cols = self._non_primary_key_columns()
        if not cols:
            raise NoAssignsError("no assignments provided")
        for col in cols:
            self._check_column(col)
        return [mk(col) for col in cols]

    def _append_on_duplicate(self, insert: Statement, assigns: list[Assign]) -> Statement:
        primary_keys = set(self._table.primary_key)
        for assign in assigns:
            if assign.col not in primary_keys:
                continue
            escaped = escape_mysql_name(assign.col)
            if assign.expr == f"{escaped} = {escaped}" and not assign.args:
                continue  # lock-only：自赋值不改变主键
            raise Proto2MySQLError(
                f"primary key column {assign.col} in table {self._table.table_name} "
                "cannot be updated by ON DUPLICATE KEY UPDATE"
            )
        set_clause, set_args = self._build_assigns(assigns)
        return Statement(
            f"{insert.sql} ON DUPLICATE KEY UPDATE {set_clause}", insert.args + set_args
        )

    def _non_primary_key_columns(self) -> list[str]:
        """按字段声明顺序返回所有非主键列。"""
        pk = set(self._table.primary_key)
        return [f.name for f in self._table.fields if f.name not in pk]

    def _build_assigns(self, assigns: Sequence[Assign]) -> tuple[str, list[Any]]:
        """校验列名并拼接赋值子句。"""
        if not assigns:
            raise NoAssignsError("no assignments provided")
        clauses: list[str] = []
        args: list[Any] = []
        for a in assigns:
            # 算术赋值必须落在数值列上；其余赋值只确认列存在（沿用原行为）。
            # 这里是全部 Assign 的唯一汇聚点，放这一处就覆盖了
            # update_assigns_by_pk / update_assigns_where / upsert_with /
            # batch_upsert_with / incr_by_pk / decr_by_pk_if_enough 全部入口。
            if a.numeric:
                self._check_numeric_column(a.col)
            else:
                self._check_column(a.col)
            clauses.append(a.expr)
            args.extend(a.args)
        return ", ".join(clauses), args

    def _set_field_columns(self, m: Message) -> tuple[list[str], list[Any]]:
        """收集消息里已赋值字段的转义列名与取值。"""
        self._table.validate_message(m)
        cols: list[str] = []
        args: list[Any] = []
        for fd in self._table.fields:
            if not pbconv.has_field(m, fd):
                continue
            cols.append(escape_mysql_name(fd.name))
            args.append(pbconv.serialize_field_value(m, fd))
        if not cols:
            raise NoFieldsSetError("no fields set in message")
        return cols, args

    # ── SELECT ──────────────────────────────────────────────────────────

    def select_by_pk(self, m: Message) -> Statement:
        """按主键查整行。"""
        return self._select_by_pk(m, QueryOptions())

    def select_by_pk_for_update(self, m: Message) -> Statement:
        """按主键加写锁查整行：``... WHERE pk = ? FOR UPDATE``。

        事务里"先锁后改"的标准第一步（读到的是加锁后的最新值，防止并发读-改-写丢更新）。
        """
        return self._select_by_pk(m, QueryOptions(for_update=True))

    def _select_by_pk(self, m: Message, opts: QueryOptions) -> Statement:
        where, args = self._table.primary_key_where(m)
        return Statement(
            f"{self._table.select_fields_sql} WHERE {where}{opts.sql_suffix()}",
            args,
        )

    def select_where(
        self,
        where_clause: str,
        args: Sequence[Any] | None = None,
        opts: QueryOptions | None = None,
    ) -> Statement:
        """按条件查整行，支持 ORDER BY / LIMIT / OFFSET / FOR UPDATE。

        where_clause 原样拼接（``"1 = 1"`` 表示无条件），**勿传入不可信输入**；
        取值一律走 args。
        """
        opts = opts or QueryOptions()
        return Statement(
            f"{self._table.select_fields_sql} "
            f"WHERE {normalize_where_clause(where_clause)}{opts.sql_suffix()}",
            list(args or []),
        )

    def select_columns(
        self,
        cols: Sequence[str],
        where_clause: str,
        args: Sequence[Any] | None = None,
        opts: QueryOptions | None = None,
    ) -> Statement:
        """只查指定列（列名会校验+转义），用于事务里只读一两个字段加锁。

        如 ``SELECT `gold` FROM `player_currency` WHERE player_id = ? FOR UPDATE``。
        cols 为空则退化为查全部列。
        """
        opts = opts or QueryOptions()
        if not cols:
            return self.select_where(where_clause, args, opts)
        escaped = []
        for col in cols:
            self._check_column(col)
            escaped.append(escape_mysql_name(col))
        stmt = (
            f"SELECT {', '.join(escaped)} FROM {escape_mysql_name(self._table.table_name)} "
            f"WHERE {normalize_where_clause(where_clause)}{opts.sql_suffix()}"
        )
        return Statement(stmt, list(args or []))

    def select_by_kv_in(
        self, col: str, vals: Sequence[Any], opts: QueryOptions | None = None
    ) -> Statement:
        """按某列的取值集合批量查：``... WHERE col IN (?, ?, ...)``（占位符按取值个数展开）。"""
        where = self._in_clause(col, len(vals))
        return self.select_where(where, vals, opts)

    def select_by_pk_in(
        self, pk_values: Sequence[Any], opts: QueryOptions | None = None
    ) -> Statement:
        """按主键取值集合批量查（联合主键不适用）。"""
        return self.select_by_kv_in(self._single_primary_key(), pk_values, opts)

    def count(self, where_clause: str = "", args: Sequence[Any] | None = None) -> Statement:
        """计数：``SELECT COUNT(*) FROM t WHERE ...``（空条件表示全表）。"""
        return Statement(
            f"SELECT COUNT(*) FROM {escape_mysql_name(self._table.table_name)} "
            f"WHERE {normalize_where_clause(where_clause)}",
            list(args or []),
        )

    def exists(self, where_clause: str = "", args: Sequence[Any] | None = None) -> Statement:
        """存在性判定：``SELECT 1 FROM t WHERE ... LIMIT 1``。

        比 COUNT(*) 便宜（命中一行即停），只需要"有没有"时优先用它。
        """
        return Statement(
            f"SELECT 1 FROM {escape_mysql_name(self._table.table_name)} "
            f"WHERE {normalize_where_clause(where_clause)} LIMIT 1",
            list(args or []),
        )

    def exists_by_pk_for_update(self, m: Message) -> Statement:
        """按主键判定存在并加写锁。"""
        where, args = self._table.primary_key_where(m)
        return Statement(
            f"SELECT 1 FROM {escape_mysql_name(self._table.table_name)} "
            f"WHERE {where} LIMIT 1 FOR UPDATE",
            args,
        )

    # ── UPDATE ──────────────────────────────────────────────────────────

    def update_by_pk(self, m: Message) -> Statement:
        """按主键更新已赋值字段（proto3 零值视为未赋值，规则同 insert_set_fields）。"""
        return self._table.get_update_sql(m)

    def update_by_pk_if(
        self, m: Message, guard: str, guard_args: Sequence[Any] | None = None
    ) -> Statement:
        """带守卫条件的按主键更新（CAS）：``UPDATE t SET ... WHERE pk = ? AND <guard>``。

        用于 owner_epoch / 版本号 / 状态机流转这类"只有当前状态符合预期才允许写"的场景。
        默认 MySQL 的 rowcount 统计**实际变更**行数，所以 0 既可能是守卫失败，
        也可能是新旧值相同。必须区分时，让更新同时递增版本列，
        或在连接上启用 client_flag CLIENT_FOUND_ROWS 后按匹配行数判断。
        """
        set_clause, set_args = self._table.get_update_set(m)
        if not set_clause:
            raise NoFieldsSetError("no fields set in message")
        where, where_args = self._table.primary_key_where(m)
        return self._update_stmt(
            set_clause, set_args, _append_guard(where, guard), where_args + list(guard_args or [])
        )

    def update_fields_by_pk(self, m: Message, *cols: str) -> Statement:
        """按主键只更新指定列。

        即使这些列当前是 proto3 零值也会写出去，用于"把余额清零"这类必须写零值的场景。
        """
        self._table.validate_message(m)
        if not cols:
            raise NoAssignsError("no assignments provided")
        assigns = []
        for col in cols:
            fd = self._check_column(col)
            assigns.append(set_col(col, pbconv.serialize_field_value(m, fd)))
        return self.update_assigns_by_pk(m, *assigns)

    def update_where(
        self, m: Message, where_clause: str, where_args: Sequence[Any] | None = None
    ) -> Statement:
        """按自定义条件更新已赋值字段。空条件会被拒绝。"""
        where = require_mutation_where_clause(where_clause)
        return self._table.get_update_sql_by_where(m, where, where_args)

    def update_assigns_by_pk(self, m: Message, *assigns: Assign) -> Statement:
        """按主键做表达式更新：``UPDATE t SET gold = gold + ?, updated_at = NOW() WHERE pk = ?``"""
        set_clause, set_args = self._build_assigns(list(assigns))
        where, where_args = self._table.primary_key_where(m)
        return self._update_stmt(set_clause, set_args, where, where_args)

    def update_assigns_where(
        self,
        assigns: Sequence[Assign],
        where_clause: str,
        where_args: Sequence[Any] | None = None,
    ) -> Statement:
        """按自定义条件做表达式更新（最通用的一条，扣减守卫等都由它表达）::

            # 余额够才扣，扣不成 rowcount 为 0
            b.update_assigns_where([sub_col("gold", 100)],
                "`player_id` = ? AND `gold` >= ?", [pid, 100])
        """
        where = require_mutation_where_clause(where_clause)
        set_clause, set_args = self._build_assigns(assigns)
        return self._update_stmt(set_clause, set_args, where, list(where_args or []))

    def incr_by_pk(self, m: Message, col: str, delta: int) -> Statement:
        """按主键累加单列：``UPDATE t SET col = col + ? WHERE pk = ?``

        delta 取负即为扣减，但不做下限保护，会扣成负数；需要下限用 :meth:`decr_by_pk_if_enough`。
        """
        return self.update_assigns_by_pk(m, add_col(col, delta))

    def decr_by_pk_if_enough(self, m: Message, col: str, delta: int) -> Statement:
        """按主键扣减单列，且只有当前值 >= delta 才扣（防止扣成负数）。

        执行后必须据 rowcount 判断是否真的扣到，为 0 表示余额不足或记录不存在。
        delta 必须为正数：0 不产生实际变更，负数则会让扣减变成累加，二者都会破坏结果判定。
        """
        if delta <= 0:
            raise Proto2MySQLError(f"delta must be positive, got {delta}")
        # 扣减本身和 `col >= ?` 这条守卫都要求数值列：文本列上
        # 'abc' >= 100 走的是字符串比较，守卫会给出与直觉相反的结果。
        self._check_numeric_column(col)
        where, where_args = self._table.primary_key_where(m)
        guard = f"{escape_mysql_name(col)} >= ?"
        return self.update_assigns_where(
            [sub_col(col, delta)], _append_guard(where, guard), where_args + [delta]
        )

    def _update_stmt(
        self, set_clause: str, set_args: list[Any], where: str, where_args: list[Any]
    ) -> Statement:
        return Statement(
            f"UPDATE {escape_mysql_name(self._table.table_name)} SET {set_clause} WHERE {where}",
            list(set_args) + list(where_args),
        )

    # ── DELETE ──────────────────────────────────────────────────────────

    def delete_by_pk(self, m: Message) -> Statement:
        return self._table.get_delete_sql(m)

    def delete_by_pk_if(
        self, m: Message, guard: str, guard_args: Sequence[Any] | None = None
    ) -> Statement:
        """带守卫条件的按主键删除：``DELETE FROM t WHERE pk = ? AND <guard>``"""
        where, where_args = self._table.primary_key_where(m)
        return Statement(
            f"DELETE FROM {escape_mysql_name(self._table.table_name)} "
            f"WHERE {_append_guard(where, guard)}",
            where_args + list(guard_args or []),
        )

    def delete_where(self, where_clause: str, args: Sequence[Any] | None = None) -> Statement:
        """按条件删除。条件不得为空；确需全表删除时必须显式传入 "1=1"。"""
        where = require_mutation_where_clause(where_clause)
        return self._table.get_delete_sql_by_where(where, args)

    def delete_where_limit(
        self,
        where_clause: str,
        args: Sequence[Any] | None = None,
        order_by: str = "",
        limit: int = 0,
    ) -> Statement:
        """有界批删：``DELETE FROM t WHERE ... [ORDER BY ...] LIMIT n``。

        保留期清理必须走这条：一次删干净会长时间持锁并撑爆 binlog，
        正确做法是小批量循环删到 rowcount < limit 为止。
        """
        if limit <= 0:
            raise Proto2MySQLError(f"delete limit must be positive, got {limit}")
        where = require_mutation_where_clause(where_clause)
        stmt = f"DELETE FROM {escape_mysql_name(self._table.table_name)} WHERE {where}"
        if order_by:
            stmt += f" ORDER BY {order_by}"
        stmt += f" LIMIT {limit}"
        return Statement(stmt, list(args or []))

    def delete_by_kv_in(self, col: str, vals: Sequence[Any]) -> Statement:
        """按某列的取值集合批删。"""
        return self.delete_where(self._in_clause(col, len(vals)), vals)

    def delete_by_pk_in(self, pk_values: Sequence[Any]) -> Statement:
        """按主键取值集合批删（联合主键不适用）。"""
        return self.delete_by_kv_in(self._single_primary_key(), pk_values)

    # ── 内部工具 ────────────────────────────────────────────────────────

    def _in_clause(self, col: str, n: int) -> str:
        """生成 ``col IN (?, ?, ...)``，校验列存在且取值非空。"""
        self._check_column(col)
        if n == 0:
            raise EmptyValuesError(f"empty value list: column {col}")
        return f"{escape_mysql_name(col)} IN ({build_placeholders(n)})"

    def _single_primary_key(self) -> str:
        """唯一主键列名，联合主键或未声明主键时报错。"""
        if len(self._table.primary_key) != 1:
            raise PrimaryKeyNotFoundError(
                f"primary key not found: table {self._table.table_name} needs exactly one "
                f"primary key column, got {len(self._table.primary_key)}"
            )
        return self._table.primary_key[0]
