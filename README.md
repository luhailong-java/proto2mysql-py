# proto2mysql（Python 版）

把 Protobuf 消息自动映射成 MySQL 表结构，并提供参数化的 CRUD 与 SQL 生成，不用手写 SQL。

Go 版 [luyuancpp/proto2mysql](https://github.com/luyuancpp/proto2mysql) 的 Python 实现。
**生成的 SQL 与 Go 版逐字节一致**（唯一例外见 [与 Go 版的差异](#与-go-版的差异)，
那一条是修 Go 版一个会让建表直接失败的 bug）——同一份 `.proto` 在两边产出同一份 DDL/DML，
所以 Go 服务与 Python 服务可以读写同一张表、共用同一份迁移脚本，一个服务一个服务地灰度迁移。

一致性不是靠"照着翻"保证的：`tests/test_golden_sql.py` 里的期望字符串逐条搬自 Go 仓库的
`sqlbuilder_test.go`，跑 `pytest` 就是在验证两边产出同一份 SQL。另有 19 个用例打在真实
MySQL 8.4 上，覆盖微秒时间戳落库、裸字节 BLOB、唯一键冲突、事务回滚、
以及**按字段号改名后数据还在不在**。

## 功能特点

- **自动映射**：Protobuf 消息与 MySQL 表结构自动映射，包括字段类型转换
- **配置写在 proto 里**：表名/主键/自增/索引/唯一键/可空字段都是 message option，代码里不用重复一遍
- **结构管理**：自动建表；已有表按 **proto 字段号**对齐（改名保留数据）
- **安全操作**：全部参数化，值一律走占位符
- **类型处理**：内置 Timestamp、map/repeated/嵌套消息、bytes 的正确落库形态
- **三层用法**：只要 DDL / 只要 SQL 不执行 / 连库执行，按需要的耦合度选
- **零驱动绑定**：不依赖任何具体 MySQL 驱动，给个 DB-API 连接就能跑

## 安装

```bash
pip install proto2mysql
```

带驱动一起装：

```bash
pip install "proto2mysql[mysql]"
```

## 快速开始

### 1. 定义 Protobuf 消息（表配置直接写在 proto 里）

```protobuf
syntax = "proto3";
package example;

import "google/protobuf/timestamp.proto";
import "proto2mysql_option.proto";  // 随包分发，见下方"编译 proto"

option (proto2mysql.db) = true;  // 文件级标识：本文件用于 proto2mysql 建表（供自动扫描识别）

message User {
  option (proto2mysql.table_name)         = "user";
  option (proto2mysql.primary_key)        = "id";
  option (proto2mysql.auto_increment_key) = "id";
  option (proto2mysql.unique_key)         = "email";        // 逗号分隔 = 联合唯一键
  option (proto2mysql.index)              = "name;age";     // 分号分隔多个索引，索引内逗号 = 联合索引

  int64  id         = 1;
  string name       = 2;
  string email      = 3;
  int32  age        = 4 [(proto2mysql.nullable) = true];    // 该列允许为 NULL
  google.protobuf.Timestamp create_time = 5;
}

message UserList {
  repeated User items = 1;  // 用于批量查询
}
```

### 2. 编译 proto

`proto2mysql_option.proto` 随包分发，用 `proto2mysql.proto_include_path()` 拿到它的目录：

```bash
python -m grpc_tools.protoc \
  -I. -I"$(python -c 'import proto2mysql; print(proto2mysql.proto_include_path())')" \
  --python_out=. example.proto
```

> 本库运行期**不 import** 生成的 option stub，只按字段号读扩展。
> 所以你工程里已经有一份自己生成的 `proto2mysql_option_pb2.py` 也不冲突——
> 这一点是刻意的：往默认 descriptor pool 里重复注册同一个扩展会直接抛
> "extension already registered"，而按字段号读根本不需要注册。
>
> 如果你的 proto 写的是带目录的 `import "proto2mysql/proto2mysql_option.proto";`，
> 那就把 `proto2mysql_option.proto` 放进自己的 `proto/proto2mysql/` 目录（或建个软链），
> 两种写法本库都支持——它只认字段号，不认文件路径。

### 3. 用起来

```python
import pymysql
from proto2mysql import DB
import example_pb2 as pb

conn = pymysql.connect(host="localhost", user="root", password="...", database="testdb",
                       charset="utf8mb4")

db = DB(conn, "testdb")

# 注册：表配置自动从 proto option 读，不用传参
db.register_table(pb.User)

# 建表 / 对齐字段
db.create_or_update_table(pb.User)

# 增
user = pb.User(name="张三", email="zhangsan@example.com", age=30)
user.create_time.GetCurrentTime()
db.insert(user)

# 查
result = pb.User()
db.find_one_by_kv(result, "email", "zhangsan@example.com")

# 改
result.age = 31
db.update(result)

# 批量查
users = pb.UserList()
db.find_all_by_where(users, "`age` > ?", [20])

# 删
db.delete(result)
```

### 4. 自动注册（不用逐个 register_table）

```python
import example_pb2  # noqa: F401 —— 必须 import，描述符才会进 descriptor pool

db = DB(conn, "testdb")
registered = db.register_all_tables()   # 返回被注册的表名（proto full name）
db.sync_all_tables()                    # 一次性建表 / 对齐字段
```

规则与 Go 版相同：文件声明了 `option (proto2mysql.db) = true;` **且** message 声明了
`table_name` 的才会被注册。只有 `db` 没有 `table_name` 的消息（列表消息、内嵌子消息）会跳过。

> **与 Go 版唯一的机制差异在这里。** Go 靠 `protoregistry.GlobalFiles` 遍历全局描述符表；
> Python 的 `DescriptorPool` 没有"列出全部文件"的公开 API，所以本库扫 `sys.modules` 里
> 所有 `DESCRIPTOR` 是 `FileDescriptor` 的模块。前提同样是"模块得被 import 过"，
> 与 Go 侧"包得链接进二进制"是同一个前提。
> 不想依赖 import 副作用（linter 容易删掉只为副作用的 import）时，显式点名：
>
> ```python
> import example_pb2, order_pb2
> db.register_all_tables(modules=[example_pb2, order_pb2])
> ```

## 只生成 SQL、不执行（SQLBuilder）

`SQLBuilder` 从一个消息直接产出参数化的 **DML**（INSERT / SELECT / UPDATE / DELETE），
**不连库、不需要 register_table、不执行任何语句**。适合把语句交给已有的连接池 / SQLAlchemy /
自家 data 层执行、在事务里和手写 SQL 混用、或先打日志再执行。

```python
from proto2mysql import SQLBuilder, set_col_expr, set_new, set_new_if_zero

b = SQLBuilder.from_message(pb.PlayerCurrency)

stmt = b.upsert_add(row, "gold")     # 插入或累加
# stmt.sql  = INSERT INTO `player_currency` (...) VALUES (?, ?)
#             ON DUPLICATE KEY UPDATE `gold` = `gold` + VALUES(`gold`)
# stmt.args = [...]

cur.execute(*stmt.for_paramstyle("format"))
```

### 占位符：为什么是 `?` 而不是 `%s`

生成的 SQL 一律用 `?`，和 Go 的 `database/sql` 一致——这样两边的黄金用例可以逐字节比对，
跨语言迁移才有可验证的基准。执行前用 `stmt.for_paramstyle()` 转成驱动要的形式：

| paramstyle | 产出 | 用于 |
|---|---|---|
| `"qmark"` | 原样 `?` | SQLite；与 Go 版比对 |
| `"format"`（默认） | `?` → `%s`，且已有的 `%` → `%%` | PyMySQL / aiomysql / mysqlclient |

**别自己写 `sql.replace("?", "%s")`**：`%` 字面量必须同时转义，漏了会让
`LIKE '%x%'` 这类 where 子句在 PyMySQL 里抛 `unsupported format character`。
`DB` 层已经自动转换，只有直接拿 `SQLBuilder` 的语句去执行时才需要手动调用。

### 接口一览

| 分类 | 方法 | 产出 |
|------|------|------|
| INSERT | `insert` | 全字段插入 |
| | `insert_set_fields` | 只插已赋值字段，其余交给列默认值（自增 id / `DEFAULT CURRENT_TIMESTAMP`） |
| | `insert_ignore` / `insert_ignore_set_fields` | `INSERT IGNORE`，冲突跳过 |
| | `replace` | `REPLACE INTO`（先删后插） |
| | `batch_insert` / `batch_insert_ignore` / `batch_replace` | 多行 VALUES |
| UPSERT | `upsert(m, *cols)` | `ON DUPLICATE KEY UPDATE c = VALUES(c)`，覆盖 |
| | `upsert_add(m, *cols)` | `c = c + VALUES(c)`，累加计数器 |
| | `upsert_keep_old(m)` | `pk = pk`，插入或只加行锁不改数据 |
| | `upsert_with(m, *assigns)` | 自定义冲突合并语义 |
| | `batch_upsert` / `batch_upsert_with` | 批量版 |
| SELECT | `select_by_pk` / `select_by_pk_for_update` | 按主键查整行（后者带 `FOR UPDATE` 行锁） |
| | `select_where(where, args, opts)` | 条件查询 + `ORDER BY` / `LIMIT` / `OFFSET` / `FOR UPDATE` |
| | `select_columns(cols, ...)` | 只查指定列（列名校验+转义） |
| | `select_by_kv_in` / `select_by_pk_in` | `IN (?, ?, ...)`，占位符按取值个数展开 |
| | `count` / `exists` / `exists_by_pk_for_update` | `COUNT(*)` / `SELECT 1 ... LIMIT 1` |
| UPDATE | `update_by_pk` / `update_where` | 更新已赋值字段 |
| | `update_by_pk_if(m, guard, args)` | CAS：`WHERE pk = ? AND <guard>` |
| | `update_fields_by_pk(m, *cols)` | 只更新指定列（零值也照写，用于清零） |
| | `update_assigns_by_pk` / `update_assigns_where` | 表达式更新，如 `gold = gold + ?` |
| | `incr_by_pk` / `decr_by_pk_if_enough` | 原子加 / 够才扣（`AND col >= ?`，防负数） |
| DELETE | `delete_by_pk` / `delete_by_pk_if` / `delete_where` | 按主键 / 带守卫 / 按条件删除 |
| | `delete_where_limit(where, args, order_by, n)` | 有界批删，保留期清理用 |
| | `delete_by_kv_in` / `delete_by_pk_in` | `IN (?, ?, ...)` 批删 |
| 其它 | `table_name` / `table` / `create_table` / `primary_key_where` | 表名 / 底层映射 / 建表语句 / 主键 WHERE 片段 |

### 赋值子句 Assign

`upsert_with` / `update_assigns_by_pk` / `update_assigns_where` 的更新部分由 `Assign` 描述。

通用（UPDATE 和 UPSERT 都可用）：

| 构造 | 产出 |
|------|------|
| `set_col(col, val)` | `col = ?` |
| `add_col(col, delta)` | `col = col + ?` |
| `sub_col(col, delta)` | `col = col - ?` |
| `set_col_expr(col, expr, *args)` | `col = <expr>`，如 `"NOW()"` |

仅用于 UPSERT（`VALUES(col)` = 本次本该插入的新值）：

| 构造 | 产出 | 语义 |
|------|------|------|
| `set_new(col)` | `col = VALUES(col)` | 覆盖 |
| `add_new(col)` | `col = col + VALUES(col)` | 累加 |
| `min_new(col)` / `max_new(col)` | `LEAST` / `GREATEST` | 取最值（退避时间取更早 / 水位只增不减） |
| `set_new_if_zero(col)` | `col = IF(col = 0, VALUES(col), col)` | 首写生效，已写过不覆盖 |
| `keep_old(col)` | `col = col` | 不改数据，只拿行锁 |

```python
# 冲突时：代次 +1、jti 换新、首次落的时间戳不被覆盖
stmt = b.upsert_with(row,
    set_col_expr("generation", "`generation` + 1"),
    set_new("sess_jti"),
    set_new_if_zero("first_seen_ms"))

# 余额够才扣，靠 rowcount 判定成败
stmt = b.decr_by_pk_if_enough(row, "gold", 100)

# 保留期清理：小批量循环删到 rowcount < limit
stmt = b.delete_where_limit("`created_at` < ?", [cutoff], limit=1000)
```

### 注意事项

- 返回的 SQL **不带结尾分号**，`args` 与 `?` 一一对应；
- 列名一律校验存在于该 message 并加反引号转义，未知列抛 `FieldNotFoundError`；
- **UPDATE / DELETE 不接受空条件**：`update_where` / `update_assigns_where` / `delete_where` /
  `delete_where_limit` 传空串会抛 `EmptyWhereClauseError`，而不是悄悄退化成整表操作。
  确需操作全表时必须显式传 `"1=1"`，让危险意图在代码评审里看得见。
  （SELECT 侧的 `select_where` / `count` / `exists` 没有这个限制，空条件仍按全表查询处理。）
- `where_clause`、`QueryOptions.order_by`、`delete_where_limit` 的 `order_by`、
  `update_by_pk_if` / `delete_by_pk_if` 的 guard、以及 `set_col_expr` 的表达式都是
  **原样拼接的裸 SQL**，只能来自代码常量，取值一律走 args；
- `upsert_add` 必须显式指定数值列；不传列或传入 string/BLOB 等非数值列会报错，
  防止 MySQL 隐式数值转换写坏数据；
- **proto3 零值 = 未赋值**：`insert_set_fields` / `update_by_pk` 只写"已赋值"的字段，
  标量字段值为 `0` / `""` / `False` 时会被跳过。要显式写零值，把字段声明为 `optional`，
  或改用 `insert` / `update_fields_by_pk`；
- `FOR UPDATE` 只在事务内有意义（事务外单句自动提交，锁立即释放）；
- CAS 类语句执行后应检查 `cursor.rowcount`。正数扣减/删除返回 0 表示条件未命中；
  `update_by_pk_if` 在默认 MySQL 配置下返回 0 还可能表示"条件命中但新旧值相同"。必须区分时，
  让语句同时递增版本列，或用 `CLIENT_FOUND_ROWS` 连接标志后按匹配行数判断；
- `VALUES()` 在 MySQL 8.0.20 起被标记 deprecated（官方建议改 `AS new` 行别名），至今仍可用，
  本库沿用它以兼容 5.7。

## 类型映射

| Protobuf 类型 | MySQL 类型 | 说明 |
|--------------|-----------|------|
| int32        | int NOT NULL DEFAULT 0 | - |
| uint32       | int unsigned NOT NULL DEFAULT 0 | - |
| int64        | bigint NOT NULL DEFAULT 0 | - |
| uint64       | bigint unsigned NOT NULL DEFAULT 0 | - |
| float        | float NOT NULL DEFAULT 0 | - |
| double       | double NOT NULL DEFAULT 0 | - |
| bool         | tinyint(1) NOT NULL DEFAULT 0 | - |
| string       | MEDIUMTEXT | - |
| bytes        | MEDIUMBLOB | 原样存储，不做编码 |
| enum         | int NOT NULL DEFAULT 0 | 存储枚举值的数字表示 |
| message      | MEDIUMBLOB | proto wire 格式**裸字节** |
| map          | MEDIUMBLOB | proto wire 格式**裸字节** |
| repeated     | MEDIUMBLOB | proto wire 格式**裸字节** |
| Timestamp    | DATETIME(6) **（恒定可空）** | 保留到微秒；未设置时落 SQL NULL |

`sint32` / `sfixed64` / `fixed32` 等变体**不支持**（建表落 `TEXT`、写入报错）。
这是 Go 版的既有限制，本库保持一致而不是悄悄修掉——修了两边就产出不同的 DDL。
用 `int32` / `int64` / `uint32` / `uint64` 即可。

### Timestamp 的两条硬约束

**① 列类型是 `DATETIME(6)`，不是 `DATETIME`。**
`DATETIME` 等价 `DATETIME(0)`，写入带毫秒/纳秒的时间会被**静默**截断到整秒——不报错、无警告。
`DATETIME(6)` 保留到微秒；proto `Timestamp` 的纳秒位仍会丢，那是 MySQL 时间类型的硬上限。

存量表（停在 `DATETIME(0)` 的）会由 `update_table_field` / `sync_all_tables` 自动生成
`MODIFY COLUMN ... DATETIME(6)` 升上来。

**② Timestamp 列恒定允许 NULL，不受 `nullable` 选项影响。**
proto 的 message 字段天然是"有/无"两态，而 DATETIME 没有可用的零值——`'0000-00-00'` 在
`NO_ZERO_DATE` 下非法，空串在 `STRICT_TRANS_TABLES` 下直接被拒
（`Error 1292 Incorrect datetime value: ''`）。若声明成 `NOT NULL`，凡是没给该字段赋值的行
**整行都插不进去**，等于把这列变成必填。因此未设置的 Timestamp 一律下发 SQL `NULL`。

### 浮点数：NaN / ±Inf 会被拒绝

MySQL 的 `FLOAT`/`DOUBLE` 没有 NaN/Inf 的表示。写入时本库直接抛 `NonFiniteFloatError`，
不把问题丢给 MySQL——在 `STRICT` 模式下它只会报 `Error 1265 Data truncated`（完全看不出根因），
非 `STRICT` 模式下更糟：悄悄存成 `0`，成为静默的数据损坏。

float32 字段按 **32 位**求最短往返表示（`0.1` 就写 `0.1`，不是 `0.10000000149011612`），
并且一律用定点写法不用指数——与 Go 的 `strconv.FormatFloat(f, 'f', -1, 32)` 逐字符一致。

### 二进制字段的存储格式（裸字节，不是 Base64）

`bytes` / 嵌套 message / `map` / `repeated` 一律以 **proto wire 格式的裸字节**落库，
与手写 `msg.SerializeToString()` 后直接执行 `INSERT` 的结果**逐字节相同**。因此这些列可以和
不经本库的手写 SQL 混用，也和 Go 版写的行互通。

不做 Base64 的原因：目标列是 `MEDIUMBLOB`，本身二进制安全，Base64 只会白白多占 33% 体积。
要在 SQL 控制台查看内容，用 MySQL 自带的 `TO_BASE64()`：

```sql
SELECT TO_BASE64(`player`) FROM `golang_test` WHERE `id` = 1;
```

> ⚠️ **列类型必须是 BLOB 系**。裸字节写进 utf8mb4 的 `TEXT` / `VARCHAR` 列会因非法 UTF-8
> 被拒或损坏。本库建表时这几类字段统一映射为 `MEDIUMBLOB`，只有手工建的表才可能踩到。

## 按 proto 字段号（Field id）迁移，改名/改类型保留数据

建表时每列都会写入注释 `COMMENT 'pb:<字段号>'`，记录该列对应的 proto 字段号。之后
`update_table_field` / `sync_all_tables` / `generate_migration_sql` 同步结构时，会先读
`information_schema` 的列类型与注释，按如下优先级对齐：

1. **列名相同**：类型不兼容时 `MODIFY COLUMN` 对齐类型（并回填字段号注释）；
2. **列名不同但字段号相同**（即 proto 里把该字段改了名）：用
   `CHANGE COLUMN 旧列名 新列名 新类型 COMMENT 'pb:N'` 改名并对齐类型，**原有数据保留**；
3. **找不到对应列**：`ADD COLUMN` 新增。

主键只补"从无到有"，**绝不自动 DROP/改写已有主键**；补主键与列变更放在同一条 ALTER 里
（分开会撞 `Error 1075 auto column must be defined as a key`）。

## 连库执行（DB）

```python
db = DB(conn, "testdb", paramstyle="format")
```

### 与 Go 版的三处结构性差异

1. **连接不是池。** Go 的 `*sql.DB` 本身是并发安全的连接池；Python 的 DB-API 连接是单连接且
   **不是线程安全**的。所以 `DB` 绑定一个连接，多线程/多请求各自 `db.bind(conn)` 一份
   （表注册表共享，不重复解析描述符）。
2. **占位符**见上文。
3. **没有单独的 GormDB。** Go 版为 gorm 复制了一整套 CRUD（949 行）。Python 侧不需要：
   本层只要求"能给出 DB-API cursor"的对象，SQLAlchemy 传 `engine.raw_connection()` 即可。

### 事务

```python
with db.transaction() as tx:
    if not tx.decr_by_pk_if_enough(wallet, "gold", 100):
        raise RuntimeError("余额不足")     # 抛异常 → 自动回滚
    tx.insert(order)
# 正常退出 → 提交
```

启用缓存时，事务内的缓存失效**延迟到提交成功之后**执行（回滚不删缓存）。

### 缓存（cache-aside，可选）

```python
db.enable_cache(my_redis_cache, ttl=300)
```

- 读（`find_one_by_pk` / `find_or_create` 命中路径）：先查缓存，未命中读 DB 后回填；
- 写（按主键的 save/update/delete/incr 等）：先写 DB，成功后删缓存；
- 降级：缓存出错**仅记日志**，不影响 DB 结果（弱依赖）。

按 WHERE 条件的更新/删除定位不到受影响主键，**不做缓存失效**——缓存表请优先用按主键的接口，
或调用 `invalidate_cache()` 手动失效。

## 离线生成 SQL（proto2sql 命令）

不连库、不写 Python 代码，直接从 `.proto` 生成建表语句：

```bash
proto2sql -I proto -o schema.sql proto/account.proto
```

只处理声明了 `option (proto2mysql.db) = true;` 的文件里、且带 `table_name` 的 message。

## 与 Go 版的差异

全部是刻意的，逐条说明。除第 1 条外都不影响生成的 SQL 文本。

### 1. TEXT/BLOB 列上的索引会补前缀长度（**这是修 Go 版的 bug**）

`string` 按 Go 的映射规则落在 `MEDIUMTEXT` 上，而 MySQL **不允许**对 TEXT/BLOB 列建
不带前缀长度的索引：

```
ERROR 1170 (42000): BLOB/TEXT column 'name' used in key specification without a key length
```

也就是说只要 proto 里对 string 列声明了 `index` / `unique_key` / `primary_key`，
Go 版产出的建表语句 MySQL 会直接拒绝。这条至今没暴露，是因为 Go 侧的测试只比对 SQL
字符串、从不真的执行——拿 Go 仓库自带的 `tools/proto2sql/testdata/account.proto`
生成 DDL 打到 MySQL 8.4 上就是上面这个错。

本库补上前缀长度，让产出的 DDL 真的能建表：

```sql
INDEX `idx_account_0` (`name`(191)),
UNIQUE KEY `uk_account` (`email`(191))
```

191 是 utf8mb4 下的经典安全值。**唯一键/主键落在 TEXT 列上时唯一性只按前 191 个字符判定**，
这一点会打一条 WARNING 日志（logger 名 `proto2mysql`）。要退回与 Go 逐字节一致的输出：

```python
import proto2mysql.table
proto2mysql.table.TEXT_INDEX_PREFIX_LENGTH = 0   # 输出与 Go 相同，但那条 DDL 建不了表
```

列类型本身**没有**改（仍是 `MEDIUMTEXT`），所以两个实现对同一张表做 `sync` 不会互相
把列改来改去。

### 2. 二进制列的参数是 `bytes`，不是 `str`

Go 的 `string` 可以承载任意字节，所以 Go 版把裸 wire 字节装在 `string` 里下发。
Python 的 `str` 是 Unicode，装不了裸字节（PyMySQL 会按连接 charset 编码，非法 UTF-8 直接炸），
所以 `bytes` / 嵌套 message / `map` / `repeated` 列的参数是 `bytes`。
**落到 MySQL 的字节完全相同**，只是 `stmt.args` 里的 Python 类型不同。

### 3. 占位符

见上文「占位符：为什么是 `?` 而不是 `%s`」一节。生成的 SQL 保持 `?`，执行前用
`stmt.for_paramstyle()` 转换（`DB` 层已自动处理）。

### 4. 连接不是池

Go 的 `*sql.DB` 是并发安全的连接池；Python 的 DB-API 连接是单连接且**不是线程安全**的
（多线程共用一条 PyMySQL 连接会出 `Packet sequence number wrong` 甚至读到别人的行）。
所以 `DB` 绑定一条连接，多线程/多请求各自 `db.bind(conn)` 一份。

### 5. 没有单独的 GormDB

Go 版为 gorm 复制了一整套 CRUD（`proto2gorm.go`，949 行）。Python 侧不需要：
`DB` 只要求一个"能给出 DB-API cursor"的对象，SQLAlchemy 传 `engine.raw_connection()` 即可。

### 6. map 列的字节序两边不同（不是 bug，但别拿来做校验）

Go 的 map 序列化顺序是随机的（同一份数据多次 Marshal 会得到不同字节），
Python(upb) 是稳定的。两边写出来的 blob **都能被对方正确解析**，但
**不要对 map/repeated 列的 blob 做跨语言的字节比对或 checksum**——那是必然的假红。
要比就比反序列化后的 message。

### 7. `sint32` / `fixed64` 等变体不支持（与 Go 版一致的限制）

见[类型映射](#类型映射)。本库保持与 Go 相同的行为而不是悄悄修掉——修了两边就产出不同的 DDL。

## 测试

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

离线用例（126 个）不需要任何外部依赖，`.proto` 会在测试启动时现场编译。

真库集成用例（19 个）默认跳过，给一个 DSN 就会跑：

```bash
PROTO2MYSQL_DSN=mysql://root@127.0.0.1:3306/proto2mysql_test pytest tests/test_integration_mysql.py
```

> ⚠️ 集成用例会 `DROP TABLE` 它自己建的那几张表，**别指向有真实数据的库**。

## 性能

序列化热路径在注册期就把每列编译成闭包，不在每行上重查描述符：

| | Python | Go |
|---|---|---|
| 单行建参（6 列） | 732 ns | 679 ns |
| 批量建参（1000 行） | 0.78 ms | 0.78 ms |

（同机实测。真实开销的大头是 MySQL 往返的 0.2–1 ms，这一层不是瓶颈。）

如果你在 asyncio 里用同步驱动，记得 `asyncio.to_thread` 包一层——
一次 0.5s 的查询会让整个事件循环停摆。

## 许可证

MIT
