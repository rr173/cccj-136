#!/usr/bin/env bash
# nestedcol 全量可复做验证：构建、谓词语义、窄投影隔离、损坏检测、
# 跨页父行、峰值内存、单元测试。全部输出写入 ./out 。
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
NC=(python3 -m nestedcol)
OUT=out
rm -rf "$OUT"
mkdir -p "$OUT"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "1. 生成夹具（含 1,000,000 元素的大行 + 5MB payload）"
"$PY" examples/make_fixtures.py "$OUT" big

step "2. 构建归档（page-size 4KiB）"
"${NC[@]}" build --schema schemas/event.schema.json --input "$OUT/five.jsonl" \
  --output "$OUT/five.ncol" --page-size 4096 --rows-per-group 100
"${NC[@]}" build --schema schemas/event.schema.json --input "$OUT/events.jsonl" \
  --output "$OUT/events.ncol" --page-size 4096 --rows-per-group 100
"${NC[@]}" build --schema schemas/event.schema.json --input "$OUT/edge.jsonl" \
  --output "$OUT/edge.ncol" --page-size 8 --rows-per-group 1000
"${NC[@]}" build --schema schemas/event.schema.json --input "$OUT/bigrow.jsonl" \
  --output "$OUT/bigrow.ncol" --page-size 65536 --rows-per-group 100 --memstats 2>&1 | grep -E 'built|MEM'

step "3. 页检查器：核对边界与实际叶数"
"${NC[@]}" check-pages "$OUT/five.ncol"   --page-log "$OUT/check_five.log"   | tail -1
"${NC[@]}" check-pages "$OUT/events.ncol" --page-log "$OUT/check_events.log" | tail -2
"${NC[@]}" check-pages "$OUT/edge.ncol"   --page-log "$OUT/check_edge.log"   | tail -1
"${NC[@]}" check-pages "$OUT/bigrow.ncol" --page-log "$OUT/check_big.log"    | tail -1

step "4. 固定五行语义：items.x is_null 只选第 4、5 行（行号 3、4）"
"${NC[@]}" scan-path "$OUT/five.ncol" items.x --is-null --assemble \
  --page-log "$OUT/scan_isnull.log"
echo "--- 无条件投影区分前四行（缺失 / null / 空列表 / [null]）"
"${NC[@]}" scan-path "$OUT/five.ncol" items.x --assemble \
  --page-log /dev/null | head -4

step "5. 独立行式投影器交叉核对（不共享层级编码）"
for p in items.x matrix.v matrix.tag meta.flag payload; do
  "${NC[@]}" scan-path "$OUT/events.ncol" "$p" --assemble --page-log /dev/null \
    | grep -v scan_end > "$OUT/scan.$p.json"
  "${NC[@]}" project --schema schemas/event.schema.json --input "$OUT/events.jsonl" \
    --path "$p" | grep -v scan_end > "$OUT/proj.$p.json"
  python3 - "$OUT/scan.$p.json" "$OUT/proj.$p.json" "$p" <<'EOF'
import json, sys
a = [json.loads(l) for l in open(sys.argv[1])]
b = [json.loads(l) for l in open(sys.argv[2])]
assert a == b, f"projection mismatch on {sys.argv[3]}"
print(f"  agree: {sys.argv[3]} ({len(a)} rows)")
EOF
done

step "6. 窄投影不读未选叶页（大 payload 行，100 万元素）"
"${NC[@]}" scan-path "$OUT/bigrow.ncol" items.x --is-null --memstats \
  --page-log "$OUT/big_isnull.log" > "$OUT/big_isnull.out" 2> "$OUT/big_isnull.mem"
cat "$OUT/big_isnull.mem"
tail -1 "$OUT/big_isnull.out"
echo "  payload 页读取次数: $(grep -c 'col=payload' "$OUT/big_isnull.log" || true)"
echo "  items.x values 页读取次数（is_null 无需读值）: $(grep ' PAGE ' "$OUT/big_isnull.log" | grep -c 'stream=values' || true)"

step "7. 改坏未选 payload 页 -> 窄投影仍成功；检查器能发现"
PAY_OFF=$("${NC[@]}" dump-index "$OUT/bigrow.ncol" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['row_groups'][0]['columns']['payload']['values']['pages'][2][0])")
"${NC[@]}" corrupt --input "$OUT/bigrow.ncol" --output "$OUT/bigrow_corrupt.ncol" \
  --offset $((PAY_OFF + 100))
"${NC[@]}" scan-path "$OUT/bigrow_corrupt.ncol" items.x --is-null \
  --page-log "$OUT/corrupt_scan.log" | tail -1
echo "  corrupt 后窄扫描退出码: $?"
"${NC[@]}" check-pages "$OUT/bigrow_corrupt.ncol" --page-log /dev/null | tail -1 || true

step "8. 改坏被选列页 -> 非零失败且无成功结束标记"
X_OFF=$("${NC[@]}" dump-index "$OUT/bigrow.ncol" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['row_groups'][0]['columns']['items.x']['struct']['pages'][1][0])")
"${NC[@]}" corrupt --input "$OUT/bigrow.ncol" --output "$OUT/bigrow_badx.ncol" \
  --offset $((X_OFF + 30))
set +e
"${NC[@]}" scan-path "$OUT/bigrow_badx.ncol" items.x --is-null --page-log /dev/null > "$OUT/badx.out"
rc=$?
set -e
echo "  退出码: $rc（期望非零）; 成功结束标记: $(grep -c '"status": "ok"' "$OUT/badx.out" || true)（期望 0）"

step "9. 截断与空文件"
"${NC[@]}" corrupt --input "$OUT/events.ncol" --output "$OUT/events_trunc.ncol" \
  --truncate 5000
set +e
"${NC[@]}" scan-path "$OUT/events_trunc.ncol" items.x > /dev/null 2>&1; echo "  截断文件扫描退出码: $?"
: > "$OUT/zero.ncol"
"${NC[@]}" scan-path "$OUT/zero.ncol" items.x > /dev/null 2>&1; echo "  空文件扫描退出码: $?"
: > "$OUT/empty.jsonl"
set -e
"${NC[@]}" build --schema schemas/event.schema.json --input "$OUT/empty.jsonl" --output "$OUT/empty.ncol"
"${NC[@]}" scan-path "$OUT/empty.ncol" items.x --assemble --page-log /dev/null | tail -1

step "10. 根行边界恰逢换页（edge.jsonl, page-size=8）"
"${NC[@]}" scan-path "$OUT/edge.ncol" items.x --assemble --page-log /dev/null | tail -1

step "11. 峰值内存对比（大行窄投影 vs 行大小）"
"${NC[@]}" scan-path "$OUT/bigrow.ncol" items.x --eq 999999999 --memstats \
  --page-log "$OUT/big_eq.log" > /dev/null 2> "$OUT/big_eq.mem"
echo "  100 万元素行全量求值: $(cat "$OUT/big_eq.mem")"

step "12. 单元与端到端测试套件"
"$PY" -m unittest discover -s tests 2>&1 | tail -3

step "全部检查完成"
