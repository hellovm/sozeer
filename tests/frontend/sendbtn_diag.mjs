// v0.15.1 诊断：模拟发送按钮在「代笔→采纳」后卡在"处理中"的场景。
// 复刻 mountChatStream 的状态机，验证 Promise 是否能 resolve。
import fs from 'fs';

const APP = fs.readFileSync('apps/web/app.js', 'utf8');

// —— 抽取真实实现的关键片段做行为验证 ——
// 这里不复制逻辑，而是检查源码是否满足"必然 resolve"的契约。

let fails = 0;
function check(name, cond, detail) {
  if (!cond) { console.log(`  ✗ ${name}${detail ? ' :: ' + detail : ''}`); fails++; }
  else console.log(`  ✓ ${name}`);
}

console.log('=== mountChatStream 契约检查 ===');

// 1) 必须在所有终止路径 resolve
const mcsStart = APP.indexOf('function mountChatStream(');
const mcsEnd = APP.indexOf('\nfunction ', mcsStart + 10);
const mcs = APP.slice(mcsStart, mcsEnd);
const resolveCount = (mcs.match(/resolve\(\)/g) || []).length;
check('mountChatStream 有 resolve 调用', resolveCount >= 2, `resolve 出现 ${resolveCount} 次`);

// 2) error 分支必须 resolve
check('error 分支 resolve', /ev\.kind === 'error'[\s\S]{0,400}?resolve\(\)/.test(mcs),
      'error 事件后未 resolve → 按钮会卡死');

// 3) onDone 必须 resolve
check('onDone 分支 resolve', /onDone:\s*async[\s\S]{0,800}?resolve\(\)/.test(mcs));

// 4) ★ 关键：挂流失败（mountTaskStream 抛错）时是否 resolve？
// mountTaskStream 内部 try/catch，AbortError 直接 return —— 不调用 onDone！
const mtsStart = APP.indexOf('async function mountTaskStream(');
const mtsEnd = APP.indexOf('\nasync function ', mtsStart + 10);
const mts = APP.slice(mtsStart, mtsEnd > 0 ? mtsEnd : mtsStart + 4000);
check('mountTaskStream AbortError 不吞掉 onDone',
      !/AbortError'\s*\)\s*return/.test(mts) || /o\.onDone/.test(mts.slice(mts.indexOf("AbortError"))),
      '★ 流被 abort 时直接 return，onDone 永不触发 → Promise 悬空 → 按钮卡"处理中"');

// 5) 流正常结束（done 字节）但没收到终态事件时是否兜底？
check('流结束兜底 onDone', /finally[\s\S]{0,300}onDone/.test(mts),
      '★ 流读完但无 done 事件时，没有兜底 onDone → 按钮卡死');

console.log('');
console.log('=== 后端 stream 端点契约 ===');
const MAIN = fs.readFileSync('apps/api/main.py', 'utf8');
// 后端在任务终态时必发 done
check('后端终态必发 done', /if t\.status in \("done", "error", "abandoned"\)[\s\S]{0,300}kind.*done/.test(MAIN));

console.log('');
console.log(fails === 0 ? '全部通过' : `${fails} 项失败`);
process.exit(fails === 0 ? 0 : 1);
