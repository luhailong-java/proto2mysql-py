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


class FieldNumberReusedError(Proto2MySQLError):
    """线上某列的 pb:N 与新字段号相同，但类型跨族或值域变化不安全。

    这不是改名，是**字段号被复用**：proto 里删掉一个字段后，把它的编号让给了
    一个类型完全不同、或 signed/unsigned 值域不兼容的新字段。库按字段号识别列，
    若继续自动改名就会生成
    ``CHANGE COLUMN legacy foo bigint``——MySQL 的隐式类型转换会把 mediumtext
    里的内容整列吃掉，而本库「永不 DROP COLUMN」的保护在这里帮不上忙。

    字段号是 protobuf 的身份，**永不复用**：删字段要用 ``reserved``。
    对应 Go 的 ErrFieldNumberReused。
    """


class ExpandOnlyViolationError(Proto2MySQLError):
    """开了 expand_only 之后，本次对齐产生了非「纯新增」的变更。

    滚动 / 金丝雀发布时必须打开这个开关。本库没有 schema 版本概念，每个进程都把
    自己的 proto 当成唯一正确的目标结构，所以只要新旧两版同时在跑，
    MODIFY / CHANGE 就会被两边**来回改**——不需要写任何数据，一次重启就翻一次面。
    ADD COLUMN 没有这个问题：旧版本的 SQL 里根本不会出现新列名。

    对应 Go 的 ErrExpandOnlyViolation。
    """
