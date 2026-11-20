# 变异归一化汇总平台

同一次插入/缺失在重复序列中可能被不同样本写成不同坐标。本平台接收参考序列与
逐样本 TSV 变异文件，将等价变异按参考**左对齐归一化并去冗余**，再按规范变异
汇总多样本检出情况。

技术栈：Flask + SQLite（WAL），Docker Compose 一键启动。

## 快速启动

```bash
docker compose up -d --build
# 打开 http://localhost:8000
```

SQLite 数据文件存放在命名卷 `variant-data`（容器内 `/data/variants.db`），
重建容器不丢数据；`SECRET_KEY` 请在 `docker-compose.yml` 中替换为生产密钥。

本地开发：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATABASE_PATH=/tmp/dev.db flask --app 'app:create_app()' run --debug
```

## 使用流程

1. **先上传参考序列**：一条仅含大写 `A/C/G/T` 的序列，支持纯文本或单记录 FASTA。
2. **逐样本上传 TSV**：Tab 分隔、1-based 坐标，可带表头：

   ```tsv
   POS	REF	ALT	GT
   3	T	TT	0/1
   8	A	C	1/1
   ```

   - 文件开头允许 UTF-8 BOM（参考序列文件同样允许）
   - 无表头时首行直接是数据；`POS` 写成 `0` 等非法值会按原文件行号报错，不会被当成表头跳过
   - 只有**首个非空行**恰好是 `POS REF ALT GT` 四列（顺序、大小写固定）时才作为表头跳过；
     其他首行一律按数据行解析并报错，不会被静默忽略
   - 仅接收：单碱基替换、带锚碱基的纯插入（如 `T>TT`）、带锚碱基的纯缺失（如 `TT>T`）
   - GT 仅允许 `0/1` 或 `1/1`
   - 每行先核对 REF 与当前参考是否一致，再做左对齐归一化
   - 任何坏行都会报出**原文件真实行号**并拒绝整份文件；同名旧样本原样保留
3. 首页按**规范变异**列出检出样本数；点详情查看各样本基因型与原始位点。

## 关键规则

### 归一化（参考左对齐）

采用经典 left-align 流程（与 `bcftools norm` / `vt normalize` 等价）：

1. 去除 REF/ALT 共同后缀；
2. 无法去尾时整体左移一位并补参考前缀碱基，循环直到不能移动；
3. 在参考起点出现空等位基因时回退到最后一个合法带锚状态；
4. 去除共同前缀（每个等位基因至少保留 1 个锚碱基）。

因此重复序列中的等价写法（如 `GCTTTGC` 上 `(3,T,TT)`、`(4,T,TT)`、`(5,T,TT)`）
都会归一化为同一个规范键 `(2,C,CT)`，从而被识别为同一变异。

### 事务与一致性

- 同一样本归一化后出现重复变异（同一规范键）→ **整份文件拒绝**，不写入任何数据。
- 任何校验错误（REF 不符、非法 GT、类型不支持、格式错误等）都在事务内失败回滚，
  **错误上传不改变当前结果**。
- 同名样本重传 = 替换：新文件完整校验通过后，旧变体在同一事务中删除，旧变异不再计数。
- 更换参考 = 在一个写事务内插入新参考并**重验全部样本**：
  REF 不符或归一化后重复的样本标记为 `NEEDS_REUPLOAD`（待重传）、变体删除、
  从汇总剔除；修正后重新上传即恢复。
- 所有写事务使用 `BEGIN IMMEDIATE` 立即取 SQLite 写锁，使样本上传与参考更新
  严格串行；页面读取使用快照事务，因此样本上传与参考更新交错时，页面只会展示
  **同一版参考下的完整汇总**，不会出现跨版本混算。

### 旧库升级（数据修复）

旧版本解析有缺陷：无表头且带 BOM 的 TSV 会漏掉首条变异；首条记录的 `POS`
写成 `0` 时整行被当成表头跳过；首行任何非数字内容都会被静默忽略。
升级到本版本后，首次访问会在写事务中自动修复旧库：

- 从库里保留的**原始 TSV**（`samples.raw_tsv`，升级不会改写）重新解析并校验；
- 按**当前参考**重验这些样本的**全部**记录：通过的补齐检出结果，失败的标记为
  `NEEDS_REUPLOAD`，记录出错的原文件行号，从汇总与详情中剔除，并在样本列表显示；
- 以样本为处理单位、批间提交，内存占用不随样本总数增长；
- 进度写入 `app_state`，**中断后重启会从上次提交处安全续跑**，重复执行结果不变；
- **修复全部完成前不开放汇总与详情**，页面只显示升级提示；
  Docker 启动脚本会先完成修复再对外服务，页面读取也会分片续跑作为兜底。

## 目录结构

```
app/
  __init__.py        # Flask 工厂与路由
  normalization.py   # 纯函数：解析、校验、左对齐归一化（无 Flask 依赖）
  db.py              # SQLite 连接、WAL、事务、建表与补列迁移
  services.py        # 上传/重验/替换/汇总/旧库升级业务逻辑
  templates/         # Jinja2 页面
  static/style.css
tests/               # unittest：归一化、随机性质、端到端、升级回归、并发测试
docker/entrypoint.sh
Dockerfile
docker-compose.yml
```

## 测试

```bash
python -m unittest discover -s tests -v
```

- `test_normalization.py`：解析、边界、等价变异左对齐、重复拒绝等
- `test_properties.py`：4000 个随机变异，验证「单倍型保持」「同事件同规范键」
- `test_app.py`：Flask 端到端流程（上传、替换、换参考重验、错误隔离、删除）
- `test_upgrade.py`：BOM/表头/首行 POS=0 回归、原文件行号报错、旧库升级修复与断点续跑
- `test_concurrency.py`：上传与换参考交错时的一致性不变量

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 参考信息、上传表单、样本列表、规范变异汇总 |
| POST | `/reference` | 上传/更换参考（multipart 字段 `reference`） |
| POST | `/samples` | 上传/重传样本（字段 `name`、`sample`） |
| POST | `/samples/<id>/delete` | 删除样本 |
| GET | `/variants/<pos>/<REF>><ALT>` | 变异详情：样本、GT、原始位点 |
| GET | `/healthz` | 健康检查 |
