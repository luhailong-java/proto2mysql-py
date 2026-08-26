# API 安全速查表

> 写代码时当字典查。每条都标了「什么时候会出事」。

## 一、写数据：三组语义，别选错

### `save` —— 整行落库（推荐）

```python
db.save(player)          # 有则更新、无则插入
db.batch_save([...])
```

生成的是 `INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col), ...`，
**只更新本进程认识的列**，别的列原样保留。

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

## 二、危险方法：会毁掉旧代码不认识的列

| 方法 | 危险在哪 | 用什么代替 |
|---|---|---|
| `SQLBuilder.replace` | `REPLACE INTO` = DELETE + INSERT，**语句里没提到的列回到默认值** | `db.save()` |
| `SQLBuilder.batch_replace` | 同上 | `db.batch_save()` |

`db.save` / `db.batch_save` 本身**已经改走 ODKU 了**，是安全的。
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
| **`sint32` `sint64` `fixed32` `fixed64` `sfixed32` `sfixed64`** | ❌ **不支持** |

不支持的那六个在**建表时就会报错**（`InvalidFieldKindError`）。
它们的 zigzag / 定长编码没有直接对应的 MySQL 列类型。
换成 `int32` / `int64` / `uint32` / `uint64` 即可——**取值范围完全一样**，
只是 protobuf 线上编码方式不同。

> 早先这六个类型会静默回落成 `TEXT`：建表一路成功，跑到**第一次写入**才抛异常，
> 而那时列已经建出来了、可能还上了线。现在改成建表时就 fail-fast。

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
