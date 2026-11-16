# nestedcol 格式规范（FORMAT v1）

本文档完整描述 nestedcol 归档的字节级格式，足以让另一段程序独立实现读写。
除特别说明外，所有多字节整数为**小端序**，JSON 文本为 UTF-8。

## 1. 原语

| 名称 | 定义 |
|---|---|
| `u8` | 1 字节无符号整数 |
| `u32` | 4 字节小端无符号整数 |
| `uvarint` | LEB128 无符号变长整数：每字节低 7 位为数据，最高位为续位标记 |
| `zigzag` | 有符号 int64 → 无符号：`(n << 1) ^ (n >> 63)`，再按 uvarint 存储 |
| `crc32` | IEEE CRC-32（即 `zlib.crc32`），存储为 u32 |

## 2. 文件总体布局

```
MAGIC_H            8 字节   = "NCOL0001"（含格式版本）
u32 header_len
header_json        header_len 字节
u32 header_crc32                = crc32(header_json)

row group 0
row group 1
...

footer_json        footer_json_len 字节
u32 footer_crc32                = crc32(footer_json)
u32 footer_json_len
MAGIC_F            8 字节   = "NCOLEND1"
```

读取器必须先校验两个 magic、header 与 footer 的 CRC、以及 footer 中的
`schema_crc32` 与 header 内 schema 的规范 JSON（键排序、紧凑分隔符）的
CRC 是否一致；任何失败都视为**索引损坏**，必须拒绝打开（退出码 3）。

## 3. header_json

```json
{
  "format": 1,
  "tool": "nestedcol/1",
  "schema": { ...schema JSON... },
  "columns": [{"id": 0, "path": "items.x", "leaf": "int64"}, ...]
}
```

`columns` 按路径字典序列出全部叶列；`id` 即列在该数组中的下标。

## 4. schema JSON

```json
{
  "format": 1,
  "root": "<根 struct 名>",
  "structs": {
    "<struct 名>": {"fields": [
      {"num": <正整数>, "name": "<字段名>", "type": <类型节点>}
    ]}
  }
}
```

类型节点（每个节点都带 `nullable` 布尔）：

```json
{"kind": "int64"|"text"|"bool", "nullable": bool}
{"kind": "struct", "struct": "<struct 名>", "nullable": bool}
{"kind": "list", "nullable": bool, "elem": <类型节点>}
```

规则：

* 字段编号 `num` 在其所属 struct 内唯一；字段名在其所属 struct 内唯一。
  同名字段只按（所属 struct，编号）识别，不同 struct 之间互不相关。
* struct 引用必须存在且不得成环；struct 至少有一个字段。
* 可空（nullable=true）字段在 JSON 对象中可以**缺失**、**显式 null** 或
  **有值**，缺失与 null 是两种不同状态；非 nullable 字段只允许存在且有值。
* 列表元素的可空性由 `elem` 类型节点自己的 `nullable` 声明。
* 构建时必须拒绝：schema 之外的额外字段、同一对象内重复的 JSON 成员、
  与 schema 不合的类型、int64 越界（<-2^63 或 >2^63-1）、非整数字面量
  写入 int64 叶。文本按 UTF-8 原样保留，不做任何规范化。

## 5. 叶列与层级（level）

从根 struct 出发，每条到达叶类型的路径构成一个**叶列**。路径用点分字段名
表示，列表层级在路径中透明（不占段）。例如：

| 路径 | 层级序列（kind/target） |
|---|---|
| `items.x` | field(items→list) → elem(→struct) → field(x→leaf) |
| `matrix.v` | field(matrix→list) → elem(→list) → elem(→struct) → field(v→leaf) |
| `meta.flag` | field(meta→struct) → field(flag→leaf) |
| `payload` | field(payload→leaf) |

每个层级有三种属性：`kind`（`field` 或 `elem`）、`nullable`、
`target`（`struct`/`list`/`leaf`，即该层级"存在"时后继的类型）。

## 6. 结构令牌（struct stream）

每个叶列拥有两条逻辑字节流：**struct 流**（结构令牌）与 **values 流**
（叶值）。struct 流是每个根行一组的令牌序列，令牌为单个 uvarint：

| 值 | 名称 | 含义 |
|---|---|---|
| 0 | MISS | 字段在对象中缺失（仅 field 层级、仅可空） |
| 1 | NULL | 该层级为显式 null（仅可空） |
| 2 | PRESENT | struct 存在，继续下降（target=struct） |
| 3 | EMPTY | 列表存在且为空（target=list） |
| 4 | BEGIN | 列表非空，随后是各元素的子层级，最后以 END 结束 |
| 5 | END | 非空列表元素结束标记 |
| 6 | VALUE | 叶有值，值按序追加在 values 流中 |

各层级合法令牌集（`?` 表示仅 nullable 时出现）：

| kind | target | 合法令牌 |
|---|---|---|
| field | struct | MISS? NULL? PRESENT |
| field | list | MISS? NULL? EMPTY / BEGIN…END |
| field | leaf | MISS? NULL? VALUE |
| elem | struct | NULL? PRESENT |
| elem | list | NULL? EMPTY / BEGIN…END |
| elem | leaf | NULL? VALUE |

一个根行的令牌由如下递归机产生/消费（也是解码器算法）：

```
walk(level i, token t):
  field 且 t=MISS      -> 该字段缺失，子树结束
  t=NULL               -> 该层级为 null，子树结束
  target=leaf          -> 要求 t=VALUE，从 values 流读一个值
  target=struct        -> 要求 t=PRESENT，walk(i+1, 下一令牌)
  target=list          -> t=EMPTY 则结束；要求 t=BEGIN，
                          循环读令牌：END 结束，否则作为元素 walk(i+1, 该令牌)
```

示例（`items.x`，`{"items":[{"x":null},{"x":7},{"x":7}]}`）：

```
BEGIN, PRESENT, NULL, PRESENT, VALUE, PRESENT, VALUE, END
                              values 流: zigzag(7), zigzag(7)
```

五种关键行形态（`items.x`）：缺失→`MISS`；`items:null`→`NULL`；
`items:[]`→`EMPTY`；`items:[null]`→`BEGIN,NULL,END`；
`items:[{"x":7}]`→`BEGIN,PRESENT,VALUE,END`。

## 7. 叶值编码（values 流）

按 VALUE 令牌的出现顺序依次编码，无额外分隔：

| 叶类型 | 编码 |
|---|---|
| int64 | zigzag + uvarint |
| bool | 1 字节（0/1） |
| text | uvarint 字节长度 + UTF-8 字节（原样保留） |

值为 null 时不占 values 流字节（由 NULL 令牌表达）。

## 8. 行组（row group）布局

```
各列的页记录（kind=1 struct / kind=2 values），物理上按冲刷顺序交错排列
每列一个行索引记录（kind=3 页记录）
u32 meta_json_len
meta_json
u32 meta_crc32 = crc32(meta_json)
```

构建器在流缓冲达到页容量时立即冲刷页，因此页在文件中是交错的；
meta_json 中的页目录把每条逻辑流映射到它的页序列。一个行组的所有记录
恰好铺满 `[rg.offset, rg.meta_offset)` 区间。

## 9. 页记录

```
u8  stream_kind     1=struct 2=values 3=row_index
u32 page_index      流内 0 起始页号
u32 payload_len
u32 crc32           = crc32(kind||page_index||payload_len||payload)
payload             payload_len 字节
```

页头 13 字节。一条逻辑流是其全部页 payload 的拼接；页边界可以落在任意
字节处（令牌、值、甚至根行边界都可以跨页/恰在页边界）。读取逻辑区间
`[a,b)` 时，按页目录定位覆盖页，顺序读页并校验 CRC。

## 10. meta_json（行组元数据）

```json
{
  "index": 0,
  "row_count": 15,
  "columns": {
    "items.x": {
      "id": 0,
      "struct": {"bytes": 39, "pages": [[off, len], ...]},
      "values": {"bytes": 23, "count": 5, "pages": [[off, len], ...]},
      "row_index": [off, len]
    }
  }
}
```

`bytes` 为逻辑流总长（= 各页 payload 长度之和）；`count` 为叶值个数；
页 `off/len` 为文件绝对偏移与整条页记录长度（含 13 字节页头）。

## 11. 行索引记录（kind=3 的 payload）

```
uvarint row_count
row_count 组:  uvarint delta_struct_off, uvarint delta_value_off
```

第 i 行的 (struct 偏移, values 偏移) = 前 i 项 delta 的前缀和。第 i 行的
字节区间为 `[off[i], off[i+1])`，最后一行到流总长。行索引只含**边界**
信息：存在性、null、长度与边界由 struct 流表达，业务值只在 values 流。

## 12. footer_json

```json
{
  "format": 1,
  "total_rows": 15,
  "row_groups": [
    {"index": 0, "offset": 1075, "row_count": 15,
     "meta_offset": 82655, "meta_len": 1748}
  ],
  "schema_crc32": 1234567890
}
```

这是根行组索引；读取器凭它直接定位任意行组元数据，无需扫描全文件。

## 13. scan-path 读取算法

1. 校验 header/footer（索引坏则拒绝，退出码 3）。
2. 路径解析到叶列（点分段逐 struct 解析字段，列表层级透明；路径必须
   落在叶上，否则退出码 5）。
3. 对每个行组：读 meta 与**该列**的行索引（允许读取的索引）；对每行：
   * 第一遍：在该行的 struct 区间上运行层级机求值谓词（见下）；`eq`
     谓词同时读 values 区间，`is_null` 只读 struct 流。命中即可提前终止
     （谓词是单调"存在性"析取，提前终止不改变结论）。
   * 若命中，第二遍：重读同一区间，流式发出该路径的完整投影（事件或
     组装 JSON），保留全部元素与缺失/null/空结构及原列表下标。
   行的字节区间不可变且受 CRC 保护，两遍解码的是同一字节，因此跨页父行
   的筛选结论始终一致；行按行索引顺序（= 源顺序）、行内按流顺序输出。
4. 只读取被选列的页；未选列的数据页一律不读。访问到的页 CRC 失败 →
   退出码 4，且不输出成功结束标记；未访问页的损坏不影响本次投影。

## 14. 谓词语义（匹配单位 = 根行）

* **实际叶位置**：从根沿路径下降时，经过的每个**列表元素槽位**（元素
  实际存在，因为列表非空）以及最终**叶槽位**。
* null 列表元素（任意嵌套深度）与 null 叶值是实际的 null 位置，
  满足 `is_null`。
* 缺失祖先、null 祖先（null 的 struct/list 字段）、空列表不产生任何
  实际位置，不满足 `is_null`。
* `eq c`：任一实际叶位置的值等于常量 c（按叶类型解释）。
* 筛选后输出该行的**完整**路径投影，而非仅命中元素。

## 15. 限制

* 唯一公开的大小限制是**单个标量**（字符串/数字字面量）的字节数
  （构建参数 `--max-scalar`，默认 64 MiB）。列表长度、行大小、嵌套内
  元素个数均无限制：单行可以跨任意多个页。
* 解析器另有 1,000,000 层的内部递归防护（合法数据受 schema 深度约束，
  远达不到该值；它不是数据限制）。
