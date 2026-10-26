import fs from 'fs';
const src = fs.readFileSync('apps/web/app.js', 'utf8');
function extract(name) {
  const re = new RegExp('^(function ' + name + '\\([\\s\\S]*?\\n\\})', 'm');
  return src.match(re)[1];
}
const escFn = src.match(/^function esc\(s\) \{.*\}$/m)[0];
const factory = new Function([escFn, extract('mdInline'), extract('splitRow'), extract('mdToHtml')].join('\n') + '\nreturn { mdToHtml };');
const { mdToHtml } = factory();

// 用户截图里的真实回复原文（从生产库拉取的完整片段）
const REAL = `好的，我先把 16 章全过一遍审计，再汇总建议。先跑前几章的七维审计。前 4 章出来了。继续跑 5–8 章。继续 9–12 章。继续 13–16 章。16 章全部审计完毕。下面按「章节」和「共性问题」两个维度给你一份汇总，**先不动稿**，你定方向我再改。

---

## 一、全局诊断

**平均分：约 82.8 分**（最高 87：1/2/12/15 章；最低 56：16 章质检、79：6/7/10 章）

**七维里最拖后腿的两项，16 章几乎章章都是它俩：**

| 维度 | 问题 | 出现频率 |
|---|---|---|
| **叙事健康度**（均 77） | 主角**全程被动**，被系统/被事件/被配角推着走，缺主动抉择节点 | 16/16 章 |
| **逻辑/实力体系** | 同境界被碾压、数值对不上、伪前情回溯 | 约 10 章 |

**最稳的：** 去AI味（人物口癖「崽」「大爷请」、社畜吐槽与修仙错位）和内容一致性——人设立得住，伏笔链没断。

## 二、必须优先修的「硬伤」（读者会当场算账 / 人设崩）

**① 任务二进度数值打架（最严重）** — 第 6 章写 0% → 第13章 11% → 第16章「百分之十二」跳「百分之十四」，但账本锁死第16章`;

const html = mdToHtml(REAL);
const checks = [
  ['标题渲染', (html.match(/<h2/g) || []).length === 2],
  ['加粗计数', (html.match(/<strong>/g) || []).length >= 4],
  ['表格渲染', (html.match(/<table/g) || []).length === 1],
  ['表格行数', (html.match(/<tr>/g) || []).length === 3],
  ['分隔线', (html.match(/md-hr/g) || []).length === 1],
  ['无裸 ## 残留', !/^##/m.test(html.replace(/<[^>]+>/g, '')) || !html.includes('## 一')],
  ['无裸 ** 残留', !html.includes('**')],
  ['无裸 |---| 残留', !html.includes('|---|')],
  ['段落数', (html.match(/<p class="md-p">/g) || []).length >= 4],
];
let ok = 0;
for (const [n, c] of checks) { console.log(c ? '  ✓' : '  ✗', n); if (c) ok++; }
console.log(`\n${ok}/${checks.length} 通过`);
console.log('\n--- 渲染片段预览（前 600 字符）---');
console.log(html.slice(0, 600));
process.exit(ok === checks.length ? 0 : 1);
