# nestedcol — 面向嵌套数据的小型列页格式与扫描器

纯 Python 标准库实现，不依赖 Arrow / Parquet / 任何列式数据库。输入是
JSONL；核心目标是**只读取被选叶路径**的数据，同时完整区分父对象缺失、
null、空列表与列表中的 null 元素。

* 格式字节级规范：见 [FORMAT.md](FORMAT.md)（足以让另一段程序独立读写）
* 全量可复做验证：`./run_checks.sh`（约 1 分钟，全部产物写入 `./out`）

## 特性

* schema 以唯一字段编号定义 nullable struct / nullable list / int64 /
  text / bool 叶；同名字段只按（所属 struct，编号）识别
* 缺失 ≠ 显式 null；非 nullable 字段只允许存在且有值；列表元素有独立
  nullable 声明；允许列表嵌套列表
* 构建时拒绝：额外字段、重复 JSON 成员、不合类型、int64 越界；文本原样
  保留；列表顺序与重复项原样保存
* 叶值按页独立存放；结构流只含存在性 / null / 长度 / 边界信息
* 单行可跨任意多个页；构建与扫描都按受限缓冲推进，唯一公开大小限制是
  单个标量（`--max-scalar`，默认 64 MiB）
* 谓词匹配单位是根行；筛选后输出该路径的**完整投影**（保留全部元素与
  缺失/null/空结构、源根行号、原列表下标）
* 窄投影只读被选列的页；真实页面读取日志（非估计）；原库只读

## 运行环境

Python ≥ 3.9，仅标准库。无需安装，直接在项目根目录运行：

```bash
python3 -m nestedcol <命令> ...
```

## 命令

### build — JSONL 构建归档

```bash
python3 -m nestedcol build --schema schemas/event.schema.json \
    --input events.jsonl --output events.ncol \
    --page-size 65536 --rows-per-group 100000 --max-scalar 67108864
```

构建器以 SAX 方式流式解析 JSON（64 KiB 块），列流缓冲到页容量即冲刷，
因此单行再大也不会被整体装入内存。`--memstats` 打印峰值 RSS。

### scan-path — 选择一个叶路径投影

```bash
python3 -m nestedcol scan-path events.ncol items.x [--eq 7 | --is-null] \
    [--assemble] [--page-log scan.log] [--memstats]
```

* 默认输出**事件流**（每行一个 JSON）：`row_start` / `missing` / `null` /
  `empty` / `list_start` / `struct_start` / `value`（带 `pos` 原列表下标
  栈）/ `list_end` / `struct_end` / `row_end` / `scan_end`。下游无需缓存
  巨行即可消费。
* `--assemble`：每行输出一个投影 JSON（流式写出，不缓存行）；缺失渲染为
  `{"$missing":true}`。
* 最后一行恒为 `{"t":"scan_end","status":"ok",...}`（成功结束标记）；
  失败时不出现。
* 谓词语义（匹配单位 = 根行，见 FORMAT.md 第 14 节）：
  * `is_null`：行内至少一个**实际 null 叶位置**（null 叶值，或到达该
    路径的 null 列表元素）。缺失祖先、null 祖先、空列表**不**产生实际
    位置，不匹配。
  * `--eq c`：至少一个实际叶位置的值等于 `c`（按叶类型解释）。
  * 不带谓词：包括所有根行。
  * 命中后输出该行的**完整**路径投影，不是仅命中元素。

### check-pages — 页检查器

```bash
python3 -m nestedcol check-pages events.ncol
```

读取全部页并校验 CRC；重解码每条 struct 流，逐行重算边界并与行索引核对；
核对 VALUE 令牌数与 values 流实际解码出的叶值个数；核对行组铺砌完整。

### project — 独立行式投影器（小例预期）

```bash
python3 -m nestedcol project --schema schemas/event.schema.json \
    --input events.jsonl --path items.x [--eq 7 | --is-null]
```

独立的内存版参考实现（`json.loads` 逐行解析），与列式实现**不共享层级
编码**，用于交叉核对小例子的预期输出。不要用于大行。

### dump-index / corrupt — 辅助

```bash
python3 -m nestedcol dump-index events.ncol          # 根行组索引与页目录
python3 -m nestedcol corrupt --input a --output b --offset N [--xor 0xFF]
python3 -m nestedcol corrupt --input a --output b --truncate N
```

## Python API

包可直接在进程内使用（测试 `TestProgrammaticAPI` 有完整示例）：

```python
import json
from nestedcol.schema import Schema
from nestedcol.builder import build
from nestedcol.reader import Archive
from nestedcol.scanner import Predicate, scan_path

schema = Schema.from_dict(json.load(open("schemas/event.schema.json")))
with open("events.jsonl", "rb") as inf, open("events.ncol", "wb") as outf:
    build(schema, inf, outf, page_size=65536, rows_per_group=100000)

import sys
with Archive("events.ncol", log=lambda line: print(line, file=sys.stderr)) as a:
    scan_path(a, "items.x", Predicate(is_null=True), sys.stdout, assemble=True)
```

要点：`Archive` 以只读方式打开归档；`log` 回调逐条收到真实页面读取记
录；`scan_path` 返回 `(总行数, 命中数)`，事件/投影写入给定输出流。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（有成功结束标记） |
| 2 | 命令行用法错误 |
| 3 | 索引/元数据损坏或不是归档（header/footer/行组 meta/行索引），拒绝 |
| 4 | 实际访问到的数据页损坏（校验和、短读、流不可解码），无成功标记 |
| 5 | schema / 路径 / 谓词错误 |
| 6 | 构建时输入数据非法 |

## 跨页父行的一致性

谓词结论是该行 struct/values 字节区间的纯函数：区间由行索引划定、内容
不可变且受 CRC 保护。第一遍流式求值（命中即提前终止——"存在性"析取是
单调的，提前终止不改变结论），第二遍重读同一字节区间流式输出。因此无论
一行跨多少页，筛选结论始终唯一，输出顺序即流顺序（= 源文档顺序）。

## 复做验证

```bash
./run_checks.sh
```

覆盖：固定五行语义、两层嵌套列表 + nullable struct、跨 ≥3 页父行、根行
边界恰逢换页、负数/int64 极值/长文本、空文件、截断页、损坏隔离（改坏
payload 页窄投影仍成功）、100 万元素大行的峰值内存（约 11 MB，与行大小
无关）、49 个单元/端到端测试。

## 目录

```
nestedcol/          实现包
  schema.py         schema 模型、叶列枚举、层级推导
  jsonsax.py        流式 JSON 解析器（SAX 事件）
  builder.py        构建器：事件编码 + 页冲刷 + 行组/页脚
  reader.py         归档读取：页目录、校验、跨页流、页面日志
  scanner.py        scan-path：层级解码机、谓词、事件/投影输出
  projector.py      独立行式投影器（参考实现）
  checker.py        页检查器
  cli.py            命令行
schemas/            示例 schema（event / strict）
examples/           夹具生成器（确定性）
tests/              测试套件（unittest）
FORMAT.md           字节级格式规范
run_checks.sh       全量可复做验证
```
