"""proto2mysql 的错误类型。

对齐 Go 版的 sentinel error：Go 用 errors.Is 判定，Python 用 except 捕获类型。
每个 Go 的 errors.New(...) 对应这里一个异常类，父类统一为 Proto2MySQLError，
调用方可以只捕获父类。
"""

from __future__ import annotations


class Proto2MySQLError(Exception):
    """本库所有错误的基类。"""


class TableNotFoundError(Proto2MySQLError):
    """消息未 register_table，或表名查不到。对应 Go 的 ErrTableNotFound。"""


class NoRepeatedFieldError(Proto2MySQLError):
    """列表消息里没有 repeated 字段。对应 Go 的 ErrNoRepeatedField。"""


class MultipleRepeatedFieldError(Proto2MySQLError):
    """列表消息里有多个 repeated 字段，无法确定用哪个。对应 Go 的 ErrMultipleRepeated。"""


class PrimaryKeyNotFoundError(Proto2MySQLError):
    """表没有声明主键，或主键字段在消息里不存在。对应 Go 的 ErrPrimaryKeyNotFound。"""


class FieldNotFoundError(Proto2MySQLError):
    """列名不属于该消息。对应 Go 的 ErrFieldNotFound。"""


class MultipleRowsFoundError(Proto2MySQLError):
    """期望单行却查到多行。对应 Go 的 ErrMultipleRowsFound。"""


class NoRowsFoundError(Proto2MySQLError):
    """期望至少一行却查到零行。对应 Go 的 ErrNoRowsFound。"""


class DuplicateKeyError(Proto2MySQLError):
    """唯一键冲突（MySQL 1062）。对应 Go 的 ErrDuplicateKey。"""


class BatchSizeExceededError(Proto2MySQLError):
    """批量条数超过 BATCH_INSERT_MAX_SIZE。对应 Go 的 ErrBatchSizeExceeded。"""


class EmptyWhereClauseError(Proto2MySQLError):
    """UPDATE / DELETE 收到空 WHERE。

    这不是"顺手加的校验"，而是 SQLBuilder 的硬契约：空条件会静默退化成整表操作。
    确需全表时必须显式写 "1=1"，让危险意图在代码评审里看得见。
    对应 Go 的 ErrEmptyWhereClause。
    """


class InvalidFieldKindError(Proto2MySQLError):
    """字段类型不受支持（group 等）。对应 Go pbconv 的 ErrInvalidFieldKind。"""


class NonFiniteFloatError(Proto2MySQLError):
    """浮点字段是 NaN / ±Inf。

    MySQL 的 FLOAT/DOUBLE 没有 NaN/Inf 的表示：STRICT 模式下报
    Error 1265 Data truncated（看不出根因），非 STRICT 模式下悄悄存成 0——
    那是静默的数据损坏。这里 fail-closed，把问题挡在写库之前。
    对应 Go pbconv 的 ErrNonFiniteFloat。
    """


class CacheMissError(Proto2MySQLError):
    """缓存未命中，Cache.get 实现应抛出它。对应 Go 的 ErrCacheMiss。"""
