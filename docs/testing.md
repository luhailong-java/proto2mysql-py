# 怎么跑测试

## 一、日常（不需要数据库）

```bash
python -m pytest tests/ -q
```

用假 DB-API 连接（`tests/fakedb.py`）断言"发出去的语句长什么样"，秒级完成。

## 二、真库集成测试

**默认是跳过的**——不给 DSN 就 `skipped`，所以「全绿」不等于「都跑了」。
判据应该是**全绿且零跳过**。

### 起库

```bash
# MySQL 8.4
docker run -d --name proto2mysql-it -p 13307:3306 \
  -e MYSQL_ROOT_PASSWORD=p2m_test_root \
  -e MYSQL_DATABASE=proto2mysql_test \
  mysql:8.4 --sql-mode="STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION"

# TiDB（单容器 standalone）
docker run -d --name proto2mysql-tidb -p 14000:4000 pingcap/tidb:v8.5.1
```

> **别用 `mysqladmin ping` 判断 MySQL 就绪**——初始化期间它就会应答，是个假阳性
> （实测第一次探测就"成功"，然后连接被 Access denied）。
> 判据用日志里的 `ready for connections. Version: '8.4`。

### 跑

```bash
# MySQL
PROTO2MYSQL_DSN="mysql://root:p2m_test_root@127.0.0.1:13307/proto2mysql_test" \
  python -m pytest tests/ -q -rs

# TiDB
PROTO2MYSQL_DSN="mysql://root:@127.0.0.1:14000/proto2mysql_test" \
  python -m pytest tests/ -q -rs
```

`-rs` 会把跳过的用例和原因列出来——**一定要看**，跳过不等于通过。

### 期望结果

| 后端 | 结果 |
|---|---|
| 不给 DSN | 255 passed, 19 skipped |
| MySQL 8.4 | **274 passed, 0 skipped** |
| TiDB v8.5.1 | 273 passed, 1 skipped（见下） |

## 三、TiDB 的能力边界

TiDB 把自己报成 `8.0.11-TiDB-v8.5.1`——**它声称自己是 MySQL 8.0**，
光看主版本号分不出来，所以测试里靠 `VERSION()` 里有没有 `tidb` 判定。

已知差异：

| 行为 | MySQL 8.4 | TiDB v8.5.1 |
|---|---|---|
| 给**已存在**的列加 `AUTO_INCREMENT` | ✅ | ❌ **Error 8200**，合并一条/拆两条都不行 |
| 建表时就带 `AUTO_INCREMENT` | ✅ | ✅ |
| 建完表后 `ADD COLUMN` | ✅ | ✅ |
| `GET_LOCK` / `RELEASE_LOCK` | ✅ | 视版本而定——本库拿不到锁会降级，不阻断 |
| DDL 生效时机 | 语句返回即生效 | **异步**：按 lease（默认 45s）分批加载，本库会回读到可见为止 |
| 默认事务模式 | — | `pessimistic`（v4.0 起），`FOR UPDATE` 会立即加锁 |
| 默认隔离级别 | REPEATABLE READ | REPEATABLE-READ（实为快照隔离） |

### 那条 Error 8200 意味着什么

**如果你的表已经上线、且没有主键，那么在 TiDB 上永远补不上自增主键**——
只能重建表，或者去掉 `auto_increment_key` 选项改用别的主键生成方式（如雪花 ID）。

本库能做的已经做了：**把补主键拆成独立的第二条 ALTER**。
这样补主键失败时**列已经对齐好了**，服务能正常跑，运维再单独处理主键。
早先它俩挤在一条里，一失败连列都加不上——服务启动成功、第一条 SELECT 就
`Error 1054 Unknown column`，而根因埋在一条看起来只是"补主键"的语句里。

> 这条是 2026-08-26 第一次拿真 TiDB 跑集成测试时发现的，
> **一个根因产生了 6 个测试失败**。之前它一直藏着，因为集成测试从来没连过 TiDB。

## 五、跨语言逐字节对拍

「Go 版与 Python 版产出**逐字节相同**的 SQL」是本库的核心契约——两边跑同一份
`.proto` 必须产出同一份 DDL/DML，否则"并存迁移"就没有可验证的基准。

但这条契约长期**全靠人手把 Go 的字符串抄进 Python 的测试文件**，没有任何自动化在守。
抄漏一条、抄错一个反引号，谁也不会知道——而 2026-08 那一轮就实测到了两处真实分叉
（TEXT 索引前缀长度、`tools/proto2sql` 的排序键）。

### 一键跑

```bash
cd proto2mysql-py
python tools/parity_run.py --go-repo ../proto2mysql
```

退出码 0 = 完全一致，可直接接进 CI。

### 分步跑（排查时用）

```bash
# Go 侧发射语料
cd proto2mysql
PARITY_OUT=/tmp/parity.go.json go test -count=1 -run TestEmitParityCorpus .

# Python 侧发射语料
cd ../proto2mysql-py
python tools/parity_emit.py -o /tmp/parity.py.json

# 比对
python tools/parity_diff.py /tmp/parity.go.json /tmp/parity.py.json
```

### 它比什么

当前语料 **42 条**，覆盖：

| 类别 | 覆盖 |
|---|---|
| DDL | 建表：无选项 / 主键+自增 / 索引+唯一键 / 可空列 |
| ALTER | 空表 / 缺一列 / 按字段号改名 / 回填 pb:N 注释 / **线上列更宽时不动它** |
| DML 写 | insert、insert_set_fields、insert_ignore、replace、**save(ODKU)**、三个 batch、三个 upsert |
| DML 改 | update_by_pk、update_by_pk_if、update_fields_by_pk、incr、decr_if_enough |
| DML 读 | select_by_pk、for_update、select_where（含分页）、count、exists、delete_by_pk |

会报三类问题，**任何一类都算失败**：

1. **用例集不一致** —— 一边有、另一边没有。多半是有人只在一边加了用例。
2. **SQL 不一致** —— 逐字符定位第一个差异点并打上下文。一个反引号、一个空格都算。
3. **参数不一致** —— 参数按同一套规则归一成字符串后再比。

### 加用例时

语料的用例清单是**两边共同的规格**，必须同步改：

- Go 侧：`parity_emit_test.go` 的 `TestEmitParityCorpus`
- Python 侧：`tools/parity_emit.py` 的 `build_corpus()`

只改一边，对拍器会报「用例集不一致」——那正是它该报的。

### 一个已经踩过的坑

参数的归一化规则**必须两边逐字一致**。第一次跑对拍时 42 条 SQL 全部相同，
却报了 10 处参数差异——根因是同一个逻辑值在两边的**类型不一样**：
子消息的序列化字节，Go 那边是 `string`（Go 的 string 能装任意字节），
Python 这边是 `bytes`，于是落进了归一化器的不同分支。

现在的规则是：**先落到原始字节，再按同一套判据决定怎么写**——
可打印的 UTF-8 就保留文本（diff 可读），否则转十六进制。

---

## 七、多节点 TiDB 集群

单容器 standalone **测不出异步 DDL 的跨节点窗口**——只有一个节点时，
"其它节点还没加载新 schema"这个状态根本不存在。

```bash
docker compose -f tidb-cluster.yml up -d      # PD + TiKV + tidb0(14001) + tidb1(14002)
```

```bash
PROTO2MYSQL_INTEGRATION=1 PROTO2MYSQL_TEST_DSN="root:@tcp(127.0.0.1:14001)/proto2mysql_go_test" PROTO2MYSQL_TEST_DSN2="root:@tcp(127.0.0.1:14002)/proto2mysql_go_test"   go test -count=1 ./...
```

多节点才跑得起来的两条：

| 用例 | 验什么 |
|---|---|
| `TestTiDBClusterSchemaVisibleOnOtherNode` | 节点 0 改完结构，节点 1 **立刻**能按新结构读写 |
| `TestTiDBClusterConcurrentSyncAcrossNodes` | 8 个副本分布在**两个 TiDB 实例**上同时同步，DDL 要过 PD 排队 |

## 八、并发与负向验证

**一条只会报绿的测试毫无价值。** 所以每个"防护"都配了一条证明它在干活的测试：

| 防护 | 正向 | 负向（关掉防护后必须红） |
|---|---|---|
| DDL 咨询锁 | `TestConcurrentSyncAllTables` 8 副本全成功 | `TestConcurrentSyncFailsWithoutLock` **7/8 撞 Error 1060** |
| 对拍语料覆盖 | `TestParityCorpusCoversEveryPublicAPI` | 删掉任一用例即点名报缺 |
| 对拍比对 | `parity_diff.py` 退出码 0 | 注入空格/改参数/删用例，三类全抓 |

关掉咨询锁的实测数字值得记住：**8 个副本里 7 个起不来**。而它们重启后又能成功
（列已存在、对齐结果为空），所以整件事表现为"偶发的启动 flake"——
这正是最难被认真对待的那种故障形态。

> 关锁的开关是**包内私有变量** `disableSyncLockForTest`，刻意不做成环境变量：
> 那等于给生产环境留一个能关掉安全机制的开关。

**实测结论：TiDB v8.5.1 支持 `GET_LOCK`**，两个后端的锁行为一致
（开锁 8/8 过、关锁 7/8 撞 1060）。

## 九、期望数字总表

| 组合 | 结果 |
|---|---|
| Go 假驱动 | ok |
| Go tools module（独立 module，别漏） | ok |
| Go @ MySQL 8.4 | ok |
| Go @ TiDB standalone | ok |
| Go @ TiDB 集群（2 节点） | ok |
| Python 假驱动 | 255 passed / 19 skipped |
| Python @ MySQL 8.4 | **274 passed / 0 skipped** |
| Python @ TiDB standalone | 273 passed / 1 skipped |
| Python @ TiDB 集群 | 273 passed / 1 skipped |
| 跨语言对拍 | ✅ 63 条逐字节一致 |

---

## 十、清理

```bash
docker rm -f proto2mysql-it proto2mysql-tidb
docker compose -f tidb-cluster.yml down -v
```
