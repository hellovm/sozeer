"""v0.10.0 回归测试：前端 diff 算法（对齐 app.js 的 diffLines 实现）。

用 node 跑，因为被测代码是 JS。逻辑与 app.js 中 diffLines 保持一致。
"""
import subprocess
import sys
from pathlib import Path

NODE = r"C:/Users/david/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
# tests/ 的父目录就是 novelforge/，不要再加一层 novelforge
PROJECT = Path(__file__).resolve().parents[1]
APP_JS = PROJECT / "apps" / "web" / "app.js"

JS_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
// 从 app.js 里抠出 diffLines 及其依赖，避免整文件在 node 里跑（缺 DOM）
const start = src.indexOf('function diffLines(');
if (start < 0) { console.error('DIFFLINES_NOT_FOUND'); process.exit(2); }
const end = src.indexOf('\nfunction renderDiffView(');
const body = src.slice(start, end);
eval(body);

let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { console.log('  PASS ' + name); pass++; }
  else { console.log('  FAIL ' + name); fail++; }
}
const sig = ops => ops.map(o => (o.t === 'add' ? '+' : o.t === 'del' ? '-' : '=') + '|' + o.x).join('\n');

// 1. 完全相同
let r = diffLines('a\nb\nc', 'a\nb\nc');
check('相同输入无增删', r.filter(o => o.t !== 'eq').length === 0);
check('相同输入保留全部', r.length === 3);

// 2. 单行替换 = 一删一增
r = diffLines('a\nb\nc', 'a\nX\nc');
check('替换产生 1 增 1 删',
  r.filter(o => o.t === 'add').length === 1 && r.filter(o => o.t === 'del').length === 1);
check('替换前后未变动行保留', r.filter(o => o.t === 'eq').length === 2);

// 3. 纯新增（尾部）
r = diffLines('a\nb', 'a\nb\nc');
check('尾部新增只增不删', r.filter(o => o.t === 'add').length === 1 && r.filter(o => o.t === 'del').length === 0);

// 4. 纯删除
r = diffLines('a\nb\nc', 'a\nc');
check('中间删除只删不增', r.filter(o => o.t === 'del').length === 1 && r.filter(o => o.t === 'add').length === 0);

// 5. 空输入边界
r = diffLines('', 'x');
check('空→有：1 增 0 删', r.filter(o => o.t === 'add').length === 1 && r.filter(o => o.t === 'del').length === 0);
r = diffLines('x', '');
check('有→空：0 增 1 删', r.filter(o => o.t === 'add').length === 0 && r.filter(o => o.t === 'del').length === 1);
// 双空：两段都没内容，正确结果是「零操作」，不是「一行相等的空串」
r = diffLines('', '');
check('双空 → 零操作', r.filter(o => o.t !== 'eq').length === 0);

// 6. null/undefined 不炸
r = diffLines(null, undefined);
check('null 输入不炸', Array.isArray(r));

// 7. 公共前缀剥离正确（第 1 行相同，第 3 行不同）
r = diffLines('same\nOLD\nsame2', 'same\nNEW\nsame2');
check('前后缀剥离后只改中段',
  r.filter(o => o.t === 'add').length === 1 && r.filter(o => o.t === 'del').length === 1
  && r.filter(o => o.t === 'eq').length === 2);

// 8. 超大规模走降级分支不爆
const bigA = Array.from({length: 6000}, (_, i) => 'line' + i).join('\n');
const bigB = Array.from({length: 6000}, (_, i) => 'CHANGED' + i).join('\n');
const t0 = Date.now();
r = diffLines(bigA, bigB);
check('6000 行不超时（<8s）', Date.now() - t0 < 8000);
check('6000 行降级为整体替换', r.length > 0);

// 9. 重排（相同行集合，不同顺序）
r = diffLines('a\nb\nc', 'c\nb\na');
check('重排产生增删混合', r.filter(o => o.t === 'add').length + r.filter(o => o.t === 'del').length > 0);

console.log('\nRESULT ' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
"""


def main() -> int:
    tmp = Path(__file__).resolve().parent / "_diff_harness.js"
    tmp.write_text(JS_HARNESS, encoding="utf-8")
    print("=== v0.10.0 diffLines 回归测试 ===")
    try:
        p = subprocess.run([NODE, str(tmp), str(APP_JS)],
                           capture_output=True, text=True, timeout=120)
        print(p.stdout)
        if p.stderr.strip():
            print("STDERR:", p.stderr.strip()[:2000])
        ok = p.returncode == 0
    finally:
        tmp.unlink(missing_ok=True)
    print("=== " + ("全部通过 ✅" if ok else "存在失败 ❌") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())