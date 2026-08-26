# 表结构怎么安全地演进

> 面向所有人。不需要懂 protobuf 内部，也不需要懂 MySQL 的 DDL 细节。

## 一、先建立一个直觉：为什么这件事很难

你在 `.proto` 里加了一个字段，重启服务，库自动帮你 `ALTER TABLE` 加了一列。
听起来天经地义，对吧？

**问题在于"重启服务"这四个字。** 现代服务几乎都不是一次性全部重启的，
而是**一台一台换**（滚动发布），或者**先换一小部分观察**（金丝雀发布）。
于是有一段时间，新旧两个版本的进程**同时在线**。

而这个库的自动建表逻辑是这样的：

```
每个进程启动时：
    读线上表的实际结构
    对比自己 .proto 里写的结构
    不一样 → 发一条 ALTER，把线上改成自己这份
```

注意最后一句：**把线上改成"自己这份"**。

它没有"我是新版本 / 我是旧版本"的概念，也没有 schema 版本号。
在每个进程眼里，自己的 .proto 就是唯一正确的答案。

于是新旧两个版本同时在线时，就变成了两个人抢方向盘。

## 二、三个真实的事故场景

### 场景 1：改个字段名，两个版本来回改（已修复）

你把 `.proto` 里的 `ip` 改名成 `addr`（字段号还是 2，没变）：

```protobuf
// v1
string ip   = 2;
// v2
string addr = 2;
```

库能靠字段号认出这是改名，生成 `CHANGE COLUMN`，数据保留——这是本库的招牌特性，很好用。

但滚动发布时会变成这样：

```
时刻 1  v2 的实例启动  → CHANGE COLUMN `ip` → `addr`     ✅ 数据保留，符合预期
时刻 2  v1 的某个实例重启（扩容 / 被调度器挪了 / OOM 拉起）
        → 它看到线上有一列注释是 pb:2 但名字不叫 ip
        → CHANGE COLUMN `addr` → `ip`                    ❌ 改回去了
时刻 3  v2 的所有 SQL 立刻报 Error 1054 Unknown column 'addr'
时刻 4  v2 重启 → 又改成 addr → v1 再改回 ip → ...
```

**注意时刻 2 的可怕之处：v1 什么数据都没写，它只是重启了一下。**
这不是"回滚"，是**乒乓球**——每次有 pod 重启就翻一次面。

### 场景 2：把 uint32 拓宽成 uint64，被旧版本改回去（已修复）

```protobuf
// v1
uint32 port = 3;   →  MySQL: int unsigned
// v2
uint64 port = 3;   →  MySQL: bigint unsigned
```

```
v2 启动 → MODIFY COLUMN `port` bigint unsigned    ✅ 拓宽
v1 重启 → MODIFY COLUMN `port` int unsigned       ❌ 收窄回去，超过 42 亿的值全部截断
```

同样只需要 v1 重启一下，不需要写任何数据。

**同类的还有文本列。** 这条在真实项目里发生过：

> 2026-08-19：某个库的 `nickname` 列本来是 `MEDIUMTEXT`，被另一侧的服务按
> `varchar(255)` 重建了。结果同一个玩家的同一次改名，**落到宽列副本就成功、
> 落到窄列副本就报 Error 1406**，而且**不可复现**——因为取决于请求打到了哪台机器。

### 场景 3：字段号复用，数据被隐式类型转换吃掉（已修复）

```protobuf
// v1
string legacy_note = 4;      // 后来这个字段不要了，从 proto 里删掉

// v2 —— 有人把 4 号让给了新字段
int64  score       = 4;      // ← 复用了 4 号
```

库按字段号认列，看到线上有一列注释是 `pb:4`、名字不叫 `score`，
于是认为"这是改名"，生成：

```sql
CHANGE COLUMN `legacy_note` `score` bigint NOT NULL DEFAULT 0
```

MySQL 会做**隐式类型转换**：把一列文本转成 bigint —— 转不动的全变成 `0`。
整列数据没了。

**「本库从不 DROP COLUMN」的保护在这里完全帮不上忙**，因为丢的不是列，是列里的内容。

> **铁律：protobuf 的字段号是身份证，永不复用。**
> 删字段要用 `reserved` 把编号占住：
> ```protobuf
> reserved 4;
> reserved "legacy_note";
> ```

## 三、业界标准做法：Expand → Migrate → Contract

这套方法有个名字，叫 **Expand-Migrate-Contract**（也叫 Parallel Change）。
它的全部道理只有一句话：

> **代码能回滚，schema 不能回滚。**

代码回滚是秒级的、无损的；而 schema 回滚意味着 `DROP COLUMN`，
那会**永久删掉**新版本期间写入的数据。所以正确的姿势不是"schema 也能回滚"，
而是——**让 schema 只前进，并且前进的每一步都对旧版本无害**。

三个阶段：

| 阶段 | 能做什么 | 不能做什么 |
|---|---|---|
| **Expand（扩展）** | 加列（必须可空或有默认值）、加索引、加表 | 任何删除或收紧 |
| **Migrate（迁移）** | 双写、把老数据回填到新列 | — |
| **Contract（收缩）** | 删列、改类型、加 NOT NULL 约束、删表 | — |

**铁律：一次发布只能做 Expand 或 Contract 中的一种，永远不能同时做。**
Contract 必须滞后至少一个完整发布周期——要等到你确定不会再回滚到旧版本。

### 举个完整的例子：把 `ip` 改名成 `addr`

❌ **错误做法**（一步到位）：直接把 proto 里的 `ip` 改成 `addr`。
→ 就是上面的场景 1，乒乓球。

✅ **正确做法**（三次发布）：

```protobuf
// 第 1 次发布（Expand）：加新列，旧列留着
string ip   = 2;      // 老列，还在写
string addr = 7;      // 新列，用一个没用过的字段号
```
代码同时写 `ip` 和 `addr`，读的时候优先 `addr`、回退 `ip`。
此时 v1（只认识 ip）和 v2（两个都认识）**都能正常跑**。

```
// 第 2 次发布（Migrate）：跑一次回填，把历史行的 ip 拷进 addr
// 确认 addr 已经全量有值，且所有实例都是新版本
```

```protobuf
// 第 3 次发布（Contract）：删掉旧列
reserved 2;
reserved "ip";
string addr = 7;
```
（本库不会自动 DROP，`ip` 那列会留成孤儿列，需要人工执行 `ALTER TABLE ... DROP COLUMN ip`。）

麻烦吗？麻烦。但这是**唯一**能在不停机的前提下安全改名的办法。

## 四、这个库现在提供的三道闸

### 闸 1：`expand_only` —— 滚动发布必须打开

```python
db = DB(connection, "mydb", expand_only=True)
db.sync_all_tables()
```

打开之后，任何**不是纯新增**的结构变更都会被拒绝，并把具体语句打印出来：

```
ExpandOnlyViolationError: 表 player 的本次对齐含非「纯新增」变更，expand_only 下拒绝执行：
  MODIFY COLUMN `port` bigint unsigned NOT NULL DEFAULT 0
  CHANGE COLUMN `ip` `addr` MEDIUMTEXT COMMENT 'pb:2'
  这些语句在滚动发布下会被新旧副本来回执行。请改成 expand→migrate→contract 三步，
  或人工审核后单独执行。
```

**为什么 `ADD COLUMN` 可以放行、`MODIFY`/`CHANGE` 不行？**

因为旧版本的 SQL 里**根本不会出现新列的名字**。库生成的所有 SQL 都是按
自己 .proto 的字段列显式列出来的，从不用 `SELECT *`：

```sql
-- v1 发出的语句，它压根不知道有 foo 这一列
SELECT `id`, `name`, `gold` FROM `player` WHERE `id` = ?
UPDATE `player` SET `name` = ?, `gold` = ? WHERE `id` = ?
```

所以 v2 加的列，对 v1 是**完全不可见**的——既不会读到，也不会被覆盖。
这是"加列绝对安全"的机制保证，不是运气。

**默认是关的**，因为单进程部署时改名/改类型是完全正当的操作，
关掉它会毁掉本库最好用的特性之一。

### 闸 2：不再收窄（无需配置，永远生效）

现在库对四类「同族类型」做了容量排序，**线上比目标宽时一律不动它**：

```
整数    tinyint < smallint < mediumint < int < bigint
文本    char < varchar < tinytext < text < mediumtext < longtext
二进制  binary < varbinary < tinyblob < blob < mediumblob < longblob
浮点    float < double
```

| 线上是 | proto 要 | 行为 |
|---|---|---|
| `bigint` | `int` | 不动（收窄会丢数据） |
| `int` | `bigint` | ALTER 拓宽 |
| `mediumtext` | `varchar(255)` | 不动 |
| `varchar(255)` | `mediumtext` | ALTER 拓宽 |

确实需要收窄（比如为了省空间）？请人工写 `ALTER`，库不替你做这个决定。

上面那张表讲的是**列名对得上**的情形——那种情况下库什么都不发，数据原地不动。
**改名**是另一回事：它必须发一条 `CHANGE COLUMN`，而 MySQL 会借着这条语句
把整列数据真的搬到新类型上。所以改名问的方向**恰好相反**——不是"线上装不装得下目标"，
而是"**目标**装不装得下线上"：

做法是「**换类型本体、留目标属性**」：目标比线上窄时，`CHANGE COLUMN` 里写的是
**线上那个更宽的类型本体**，而 `NOT NULL` / `DEFAULT` / `AUTO_INCREMENT` 这些属性
仍然来自 proto（`COLUMN_TYPE` 里根本没有它们）。改名照常发生，数据一个字节不动。

| 线上是（旧列名） | proto 要（新列名） | CHANGE COLUMN 里写的类型 |
|---|---|---|
| `int unsigned` | `bigint unsigned NOT NULL DEFAULT 0` | `bigint unsigned NOT NULL DEFAULT 0`（拓宽） |
| `bigint unsigned` | `int unsigned NOT NULL DEFAULT 0` | `bigint unsigned NOT NULL DEFAULT 0`（**不收窄**） |
| `varchar(64)` | `MEDIUMTEXT` | `MEDIUMTEXT`（拓宽） |
| `mediumtext` | `varchar(255)` | `mediumtext`（**不收窄**） |

早先改名这条路**只判同族、不判方向**：同一个 proto 改动，顺手改个名，
就从"什么都不做"变成把 5000000000 静默截成 4294967295（非严格模式），
或者整条 ALTER 失败让服务起不来（严格模式）。

同一套判断还盖住另外两条会顺手收窄的路：**回填 `pb:N` 注释**
（类型本来兼容、只是注释缺了）和**补属性**（线上丢了 AUTO_INCREMENT）——
它们都是"因为别的原因"要发 MODIFY 的场合，早先都会带着 proto 的窄类型一起下去。

### 闸 2.5：属性漂移（NULL / DEFAULT / AUTO_INCREMENT）

`COLUMN_TYPE` 里**看不出**这三样——`int unsigned` 这个字符串既不告诉你可空不可空，
也不告诉你有没有 AUTO_INCREMENT。所以只比类型串的话，下面这些漂移是完全隐形的。

| 漂移 | 行为 | 为什么 |
|---|---|---|
| 线上丢了 `AUTO_INCREMENT` | **自动 MODIFY 补回来** | 不带主键值的 insert 会全写 0，第二条就撞 1062 |
| 线上 `NOT NULL`、proto 要可空 | **自动 MODIFY 放宽** | 无损。Timestamp 列必然是这一类，线上被改成 NOT NULL 后没赋值该字段的行全部插不进去 |
| 线上可空、proto 要 `NOT NULL` | 只告警不改 | 线上很可能已经有 NULL 行：严格模式整条 ALTER 失败，非严格模式把 NULL 静默改成 0 |
| `DEFAULT` 不一致 | 只告警不改 | 默认值只影响不点名该列的 INSERT，改它要重写表定义，收益远小于风险 |
| 线上是 `TIMESTAMP`、proto 映射 `DATETIME` | **fail-closed** | 自动改是一次全表重建 + 时区语义翻面；TIMESTAMP 按 UTC 存、按会话时区取，上限只到 2038-01-19。错误会要求先核对 `@@session.time_zone`，人工回填并执行 ALTER |

### 闸 2.6：索引漂移

索引比对**不只比名字**：唯一性、列、列序、前缀长度都比。
最要命的形态是「线上有个同名的**非唯一**索引」——只比名字的话会被判成
"唯一键已经有了、无需补"，而唯一约束事实上根本不存在，业务却正按"有唯一键"在写。

本库仍然**只加不删**（线上那个索引可能是 DBA 按查询模式手工建的），
所以漂移时不动它，并会带完整签名 **fail-closed 抛错**，阻止服务在缺失唯一约束或
错误查询索引的结构上继续启动。要对齐请人工 `DROP INDEX` 后重跑。

### 闸 3：字段号复用直接拒绝（无需配置，无法关闭）

线上那列的类型与新字段**跨族**（如 `mediumtext` vs `bigint`），或者同族但值域
发生不安全翻面（如 unsigned → signed）时，库判定这不能作为安全改名，直接报错：

```
FieldNumberReusedError: 表 player 的列 legacy_note（mediumtext，pb:4）与新字段
score（bigint NOT NULL DEFAULT 0，pb:4）类型跨族，无法当作改名处理。
  字段号是 protobuf 的身份，**永不复用**：删字段请用 reserved，新字段另取一个没用过的编号。
  若确实要把这一列的数据转成新类型，请人工写 ALTER 并自行确认转换语义。
```

这条**没有开关**，因为字段号复用没有任何正当用途。

## 五、DDL 由谁来执行

大厂的标准做法是：**DDL 不由业务进程在启动时执行**。理由有四条：

1. N 个副本同时启动 = N 条并发 `ALTER`
2. 大表 `ALTER` 会让服务起不来，撞健康检查，把滚动发布卡死
3. 回滚代码不会回滚 schema
4. 每个业务进程都有改表权限，本身是个安全问题

标准形态是：一个**独立的迁移步骤**（k8s Job / CI 流水线的一环 / 人工闸门），
跑在服务发布**之前**，成功了才允许滚动业务进程。

### 如果你不想写迁移脚本

本库提供一条折中路线：**让它生成迁移 SQL，但不执行**。

```python
# 只产出 SQL，不连库执行——交人工/CI 审核后再由迁移步骤执行
sql = db.generate_migration_sql(pb.Player)
db.dump_migration_sql_file("migrations/0007_add_addr.sql", pb.Player, pb.Guild)
```

你依然不用手写迁移脚本（脚本是**生成**的），但拿回了三样东西：
**唯一执行者、可控时机、出错时能在发布前拦下来**。

`generate_migration_sql()` 与启动时自动同步走的是**同一份差异计算**
（`_plan_schema_changes`），所以生成的 SQL 就是自动同步会发的那几条：
列对齐、补索引、补主键都在里面。所有普通 ADD/CHANGE 先落地，任何依赖新列名的索引
与 `ADD PRIMARY KEY` 放到第二阶段；只有 `AUTO_INCREMENT` 列子句必须与主键同句
（否则 MySQL Error 1075，而且 TiDB 上这条仍可能单独失败）。所以返回值可能是
**两条**以换行分隔的语句。

> 早先这两条路径各算各的，离线那条只算了列——库里缺唯一键、缺主键时它返回**空串**，
> 而空串的意思是"无需迁移"。审核者拿着空的迁移文件签字，缺的约束就这么上线了。

### 如果你就是要在启动时自动同步

也可以，但请把 DDL **收敛到一个进程**：

```
进程表：
  migrator   ← 排第一，只有它连的账号有 DDL 权限，跑完 sync_all_tables 就退出
  ↓ 等它成功退出
  服务 A / 服务 B / ...  ← 并行起，用只有 DML 权限的账号
```

库自己也加了一层保护：`sync_all_tables()` 全程持一把 MySQL 咨询锁
（`GET_LOCK`），同一时刻只有一个进程在改结构。拿不到锁**不阻断**，
只降级为无锁执行 + 一条告警——因为 TiDB 等兼容实现不一定支持这个函数，
而"因为拿不到锁就拒绝启动"是把并发问题升级成了可用性事故。

## 六、上线前检查清单

- [ ] 滚动 / 金丝雀发布 → `expand_only=True` 已打开
- [ ] 本次 proto 改动只有**加字段**，没有改名 / 改类型 / 删字段
- [ ] 删掉的字段都用 `reserved` 占住了编号
- [ ] 没有复用过任何历史字段号
- [ ] 代码里没有用 `SQLBuilder.replace` / `batch_replace`（见 [api-safety.md](api-safety.md)）
- [ ] 开了缓存的话，`ttl` 是有限值，不是永不过期（见 [cache.md](cache.md)）
- [ ] DDL 只由一个进程执行（migrator / 迁移 Job），或至少确认锁生效
