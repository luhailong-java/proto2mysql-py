# API 安全速查表

> 写代码时当字典查。每条都标了「什么时候会出事」。

## 一、写数据：几组语义，别选错

### `save` —— 整行落库（推荐）

```python
db.save(player)          # 有则更新、无则插入
db.batch_save([...])
```

高层 `DB` 不再把任意唯一键冲突都当成“这就是要更新的那一行”，而是执行：

```
按完整主键 UPDATE
  ├─ 命中：完成
  └─ 未命中：INSERT
       ├─ 成功：完成
       └─ Error 1062：按同一主键重试 UPDATE
            ├─ 命中或该主键已经存在：完成
            └─ 主键不存在：二级 UNIQUE 撞了另一行，抛 DuplicateKeyError
```

`save` 更新本进程认识的**全部非主键列**（零值也写），所以旧版本不认识的列保持原样。
`batch_save` 逐行走同一个状态机；默认不是一个原子批次，后面的行失败时，前面的行可能已经
成功。需要全成全败时显式包事务：

```python
with db.transaction() as tx:
    tx.batch_save(players)
```

`insert_on_dup_update` 同样按完整主键定位，但只更新消息中已赋值的字段。只有主键、没有
可更新字段时会做主键自赋值，不会退化成无法判断结果的裸插入。

### `insert_ignore` —— 只忽略重复键

```python
inserted = db.insert_ignore(player)
```

高层接口发的是普通 `INSERT`，只捕获 MySQL 1062。重复主键或唯一键返回“未插入”；
类型错误、截断、`NOT NULL`、外键等错误照常抛出。不要用裸 `INSERT IGNORE` 代替——
它会把多类数据错误降成 warning，调用方可能把被截断或被改写的数据当成成功。

低层 SQL 生成器无法执行多语句分类状态机，因此采用不同但 fail-safe 的形态：

- `SQLBuilder.insert_ignore*` / `batch_insert_ignore` 生成普通 `INSERT` + no-op ODKU，
  只消化重复键；
- `MessageTable` 的低层 save/upsert ODKU 对每个更新列加完整复合主键的
  NULL-safe `IF` guard。二级 `UNIQUE` 撞到不同主键时不会改 owner，但低层调用方也
  得不到高层 `DB` 的精确冲突分类；需要区分时使用高层接口。

### `update` —— 只写已赋值的字段

```python
db.update(Player(id=7, nickname="新名字"))
# UPDATE player SET id = ?, nickname = ? WHERE id = ?
```

⚠️ **proto3 的零值等于"没赋值"，会被跳过。**

```python
db.update(Player(id=7, gold=0))     # ← gold 不在生成的 SQL 里！金币清不了零
```

要写零值，用 `update_fields_by_pk`：

```python
db.update_fields_by_pk(Player(id=7), "gold")
# UPDATE player SET gold = ? WHERE id = ?    ← 会写 0
```

### 数值增减 —— 一律用原子语句，不要读-改-写

```python
# ❌ 读出来算好再写回去：有并发竞态，而且扣到 0 会被上面那条规则跳过
player.gold = player.gold - 100
db.update(player)

# ✅ 一条语句同时完成「够不够」和「扣」，rowcount == 0 就是不够
db.decr_by_pk_if_enough(player, "gold", 100)

# ✅ 单纯加减
db.incr_by_pk(player, "gold", 100)
```

原子增减**只能用在数值列上**，非数值列在构造期就会被拒。
不拦的话 MySQL 会做隐式转换：非严格 sql_mode 下 `'abc' + 1` 算成 `1`，
一次"加 1 金币"就把整列原值抹掉，而且不报错、不告警。

走同一道校验的有：`add_col` / `sub_col` / `add_new`、
`min_new` / `max_new`（`LEAST` / `GREATEST` 同样先按数值解析，
文本列上的"只增不减"水位会静默倒退）、以及 `set_new_if_zero`
（它拿 0 当哨兵，而 `'abc' = 0` 在非严格模式下为**真**，
于是一个早就写过的文本列会被判成"还没写过"）。

`repeated` / `map` 列也一并拒掉：它们的 proto 类型是**元素**类型
（`repeated int32` 就是 int32），只看类型会放行，而这两类落的是 MEDIUMBLOB。

## 二、危险方法：会毁掉旧代码不认识的列

| 方法 | 危险在哪 | 用什么代替 |
|---|---|---|
| `SQLBuilder.replace` | `REPLACE INTO` = DELETE + INSERT，**语句里没提到的列回到默认值** | `db.save()` |
| `SQLBuilder.batch_replace` | 同上 | `db.batch_save()` |

`db.save` / `db.batch_save` 本身使用完整主键状态机，不会通过二级唯一键修改另一行，
也不会重置旧代码不认识的列。
`SQLBuilder.replace` 保留下来只是作为**显式逃生口**——
确实需要"整行推倒重来、未提及的列一律归位"时才用。

### 为什么 REPLACE 这么危险

列清单来自**本进程的 descriptor**。滚动发布时：

```
v2 加了 foo 列，写进 foo = 42
v1 执行一次 replace(player)
  → REPLACE INTO player (id, name, gold) VALUES (?, ?, ?)
  → 先 DELETE 整行，再 INSERT 这三列
  → foo 回到默认值 0                                  ← 数据没了，零报错
```

而且 REPLACE 的 DELETE 会**触发外键级联删除**。

**建议在 CI 里加一条 grep 门禁**，把这两个方法拦在代码评审之前：

```bash
grep -rn "\.replace(\|\.batch_replace(" your_service/ && exit 1
```

## 三、查询：`where_clause` 是裸 SQL

```python
builder.select_where("`level` >= ? AND `zone` = ?", [10, "cn"])   # ✅ 取值走 args
builder.select_where(f"`name` = '{user_input}'")                  # ❌ SQL 注入
```

`where_clause` 和 `order_by` 是**原样拼接进 SQL 的**，
只能来自代码里的常量，**取值一律走 args**。这是硬约束，不是建议。

同理，`update_by_pk_if` 的 guard 和 `set_col_expr` 的表达式也是裸 SQL。

## 四、空 WHERE 会被硬拒绝

```python
builder.update_where(msg, "")     # ❌ 抛 EmptyWhereClauseError
builder.update_where(msg, "1=1")  # ✅ 确实要全表就显式写出来
```

这不是"顺手加的校验"，是刻意的契约：空条件会静默退化成整表操作。
要求你写 `1=1`，是为了让这个危险意图在代码评审里**看得见**。

## 五、类型限制

| proto 类型 | 支持吗 |
|---|---|
| `int32` `int64` `uint32` `uint64` `float` `double` `bool` `string` `bytes` `enum` | ✅ |
| 嵌套 message / `map` / `repeated` | ✅ 存成 `MEDIUMBLOB`（protobuf 裸字节） |
| `google.protobuf.Timestamp` | ✅ 存成 `DATETIME(6)`，恒定可空 |
| `repeated` / `map` 的 `Timestamp` | ✅ 存成 `MEDIUMBLOB`（它们是容器，不是时间点） |
| `optional`（proto3 显式 presence） | ✅ 它生成的是**合成** oneof，不受下面那条限制 |
| **`sint32` `sint64` `fixed32` `fixed64` `sfixed32` `sfixed64`** | ❌ **不支持** |
| **真 `oneof`** | ❌ **不支持** |

不支持的那六个类型和 `oneof` 都在**建表时就会报错**（`InvalidFieldKindError`）。
它们的 zigzag / 定长编码没有直接对应的 MySQL 列类型。
换成 `int32` / `int64` / `uint32` / `uint64` 即可——**取值范围完全一样**，
只是 protobuf 线上编码方式不同。

> 早先这六个类型会静默回落成 `TEXT`：建表一路成功，跑到**第一次写入**才抛异常，
> 而那时列已经建出来了、可能还上了线。现在改成建表时就 fail-fast。

### 为什么 `oneof` 只能拒

每个成员各占一列，而行里**没有判别位**。回读时逐列 set，最后一个非默认值的列胜出：

```
写 body.b = "hello"
库里 (id=1, a=0, b='hello', c=0)
读回 WhichOneof() == 'c'，b 被清空          # 写进去的激活成员被静默换掉
```

激活成员的值恰好等于默认值时更是**谁也还原不出来**——整行成员列都是默认值。
写侧也修不好：`update` 只写"已赋值"的字段，切换激活成员时旧成员那一列仍留着旧值，
回读照样取到旧的那个。

所以这里 fail-closed，与拒绝 `sint32` 是同一个立场：**不存在"既有正确用法"**，
每一次回读都在损坏数据。改法是把 `oneof` 拆成普通字段 + 一个显式的 kind 枚举列，
由业务自己判别。

## 六、`repeated` 别嵌在表消息里

```protobuf
// ❌ friends 会变成一个 MEDIUMBLOB 列：不能查、不能 COUNT、不能建索引
message Player {
  int64 id = 1;
  repeated Friend friends = 2;
}

// ✅ 一行一条关系，全是标量列
message Friendship {
  option (proto2mysql.table_name)  = "friendships";
  option (proto2mysql.primary_key) = "player_id,friend_id";
  option (proto2mysql.index)       = "friend_id";     // 反查"谁加了我"

  int64 player_id = 1;
  int64 friend_id = 2;
  google.protobuf.Timestamp created_at = 3;
}
```

判据很简单：**这张表你是"整条读整条写"，还是"要按条件查、要排序分页、要反查"？**

- 整条读写（玩家档、配置） → 嵌套 / repeated 存 BLOB 完全可以
- 要查询 → **必须一个 message 一行**

## 七、表名一定要显式声明

```protobuf
message Player {
  option (proto2mysql.table_name) = "player";   // ← 别省
  ...
}
```

不写的话表名会退化成 proto 的 **full name（含 package）**。
一旦有人把 package 从 `game.v1` 改成 `game.v2`，表名跟着变，
库会建出一张**全新的空表**，而旧数据留在旧表里——**两边都不报错**，
服务照常起来，玩家数据"凭空消失"。

`register_all_tables` 会强制要求这个选项；手工 `register_table` 不受保护，
但库会在同步结构时打一条告警。

## 八、索引的两个坑

**1. TEXT/BLOB 列上的索引必须带前缀长度。**

MySQL 不允许对 TEXT/BLOB 列建不带长度的索引（Error 1170）。
而本库把 `string` 映射成 `MEDIUMTEXT`，所以只要你在 string 列上声明了
`index` / `unique_key`，库会自动补 `(191)`：

```sql
UNIQUE KEY `uk_player` (`nickname`(191))
```

191 是 utf8mb4 下的经典安全值。**注意这意味着唯一性只覆盖前 191 个字符**——
库会打一条告警提醒你。

**2. 索引现在会自动补齐（新行为）。**

早先索引只出现在 `CREATE TABLE` 里：表一旦建成，之后在 .proto 里新加
`index` / `unique_key` **完全不生效**，而且零提示——查询照常能跑，
只是走全表扫描，数据量上来才表现为"莫名其妙变慢"。

现在同步结构时会补上缺失的索引。**只加不删**：线上多出来的索引一律不动
（可能是 DBA 按查询模式手工加的，库没有立场去删）。

⚠️ 补 `UNIQUE KEY` 时，线上若已有重复行，这条 `ALTER` 会**失败**。
这是预期的 fail-closed 行为，需要先人工去重再重试。

**3. 同名但结构不同的索引只报不改。**

比对不只比索引名：唯一性、列、列序、前缀长度都比。最要命的形态是
「线上有个同名的**非唯一**索引」——只比名字会被判成"唯一键已经有了"，
而唯一约束事实上根本不存在，业务却正按"有唯一键"在写，
直到某天发现重复数据才会暴露。

本库仍然只加不删，所以漂移时**不动它**，但会打一条带完整对比的 WARNING。
要对齐请人工 `DROP INDEX` 后重跑结构同步。

## 九、复合主键的批量查询要传元组

```python
# 单列主键：传值就行
db.find_all_by_pk_in(user_list, [1, 2, 3])

# 复合主键 (user_id, provider)：每一项必须是等长元组
db.find_all_by_pk_in(bind_list, [(1, "wechat"), (2, "apple")])
```

传扁平值会被直接拒（`Proto2MySQLError`）。早先无论主键几列都只用第一列，
于是 `WHERE user_id IN (1, 2)` 会把**只是第一列相同**的行一并捞回来，
多出来的那些被原样塞进结果列表，调用方看不出条件少了一半。

## 十、`where_clause` 里的 `?` 只在代码区被当占位符

`?` → `%s` 的转换会跳过**字符串字面量、反引号标识符和注释**，所以下面这些都能正常跑：

```python
db.find_all_by_where(users, "`nick` = ? AND `note` = 'who?'", [name])
db.find_all_by_where(users, "JSON_EXTRACT(`d`, '$.a?b') = ?", [v])
db.find_all_by_where(users, "`a` = ?  -- 这里有个问号?\nAND `b` = ?", [x, y])
```

早先是朴素逐字符替换：字面量里的 `?` 也被换成 `%s`，占位符个数比参数多，
驱动做 `query % args` 时抛 `TypeError`——**一条合法查询被拒**，
报错还指向驱动内部，看不出是本库替换出来的。

`%` 仍然是全文转义（`LIKE '%x%'` 会变成 `LIKE '%%x%%'`），因为驱动的反转义
也是全文范围的。`/*! ... */` 刻意**不**当注释——那是 MySQL 的版本条件执行块，
里面的内容真的会被执行。
