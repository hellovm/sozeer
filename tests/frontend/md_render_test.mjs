// 从 app.js 抽出 mdInline/mdToHtml/splitRow 做真实渲染测试
import fs from 'fs';
const src = fs.readFileSync('apps/web/app.js', 'utf8');

function extract(name) {
  const re = new RegExp('^(function ' + name + '\\([\\s\\S]*?\\n\\})', 'm');
  const m = src.match(re);
  if (!m) throw new Error('not found: ' + name);
  return m[1];
}
// esc 依赖
const escFn = src.match(/^function esc\(s\) \{.*\}$/m)[0];
const code = [escFn, extract('mdInline'), extract('splitRow'), extract('mdToHtml')].join('\n');
const factory = new Function(code + '\nreturn { mdToHtml, mdInline };');
const { mdToHtml } = factory();

let pass = 0, fail = 0;
function t(name, cond, extra) {
  if (cond) { pass++; console.log('  ✓', name); }
  else { fail++; console.log('  ✗', name, extra || ''); }
}

console.log('== 标题 ==');
t('h2', mdToHtml('## 一、全局诊断').includes('<h2 class="md-h">一、全局诊断</h2>'));
t('h1', mdToHtml('# 标题').includes('<h1'));
t('h3', mdToHtml('### 三级').includes('<h3'));

console.log('== 加粗 / 斜体 / 行内码 ==');
t('bold', mdToHtml('**先不动稿**').includes('<strong>先不动稿</strong>'));
t('italic', mdToHtml('这是*重点*词').includes('<em>重点</em>'));
t('code', mdToHtml('用 `abc` 调用').includes('<code>abc</code>'));
t('bold+text', mdToHtml('**平均分：约 82.8 分**').includes('<strong>平均分：约 82.8 分</strong>'));

console.log('== 表格 ==');
const tb = mdToHtml('| 维度 | 问题 | 频率 |\n|---|---|---|\n| 叙事 | 被动 | 16/16 |\n| 逻辑 | 碾压 | 约 10 |');
t('table tag', tb.includes('<table class="md-table">'));
t('th count', (tb.match(/<th>/g) || []).length === 3);
t('td count', (tb.match(/<td>/g) || []).length === 6);
t('cell content', tb.includes('<td>16/16</td>'));

console.log('== 列表 ==');
t('ul', mdToHtml('- 甲\n- 乙').includes('<ul class="md-ul">'));
t('ul items', (mdToHtml('- 甲\n- 乙').match(/<li>/g) || []).length === 2);
t('ol', mdToHtml('1. 一\n2. 二').includes('<ol class="md-ol">'));

console.log('== 代码块 ==');
const cb = mdToHtml('```\nconst a = 1;\n```');
t('pre', cb.includes('<pre class="md-code">'));
t('code esc', cb.includes('const a = 1;'));

console.log('== 分隔线 / 引用 ==');
t('hr', mdToHtml('---').includes('md-hr'));
t('quote', mdToHtml('> 引用一句').includes('<blockquote'));

console.log('== XSS 防护 ==');
const xss = mdToHtml('<script>alert(1)</script>');
t('script escaped', !xss.includes('<script>') && xss.includes('&lt;script&gt;'));
const xss2 = mdToHtml('**<img src=x onerror=alert(1)>**');
t('img escaped', !/<img/.test(xss2) && xss2.includes('&lt;img'));
const xss3 = mdToHtml('| a | b |\n|---|---|\n| <b>x</b> | y |');
t('table cell escaped', !/<b>x<\/b>/.test(xss3));

console.log('== 真实审计回复片段 ==');
const real = '好的，我先把 16 章全过一遍审计。\n\n---\n\n## 一、全局诊断\n\n**平均分：约 82.8 分**\n\n| 维度 | 问题 |\n|---|---|\n| 叙事健康度 | 主角全程被动 |';
const rr = mdToHtml(real);
t('has h2', rr.includes('<h2'));
t('has strong', rr.includes('<strong>平均分'));
t('has table', rr.includes('<table'));
t('has hr', rr.includes('md-hr'));

console.log(`\n结果: ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
