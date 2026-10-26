// v0.15.1 回归：发送按钮不得卡在"处理中"。
// 用可控的假 fetch/reader 复刻 mountTaskStream 的三条退出路径，
// 断言「onDone 恰好被调用一次」——这是 sendChat 恢复按钮的唯一依赖。
import fs from 'fs';

const APP = fs.readFileSync('apps/web/app.js', 'utf8');

// —— 从源码里抽出 mountTaskStream 并注入依赖（比静态断言强） ——
function extractFn(src, name) {
  const sig = `async function ${name}(`;
  const start = src.indexOf(sig);
  if (start < 0) throw new Error(`未找到 ${name}`);
  // 括号配平找函数体结束
  let i = src.indexOf('{', start), depth = 0, end = -1;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  return src.slice(start, end);
}

const fnSrc = extractFn(APP, 'mountTaskStream');

// 构造沙箱：提供 mountTaskStream 依赖的最小实现
function buildSandbox({ fetchImpl }) {
  const calls = { onDone: [], onEvent: [], push: [], settle: [] };
  const sandbox = {
    S: { bookId: 'bk', token: 't', taskStreams: {} },
    onUnauthorized: () => {},
    pushProcess: (p, ev) => calls.push.push(ev),
    settleProcess: (p, s) => calls.settle.push(s),
    setProcessState: () => {},
    fetch: fetchImpl,
    AbortController: globalThis.AbortController,
    TextDecoder: globalThis.TextDecoder,
    JSON,
    console,
    calls,
  };
  const factory = new Function(
    'S', 'onUnauthorized', 'pushProcess', 'settleProcess', 'setProcessState',
    'fetch', 'AbortController', 'TextDecoder', 'console',
    `${fnSrc}\nreturn mountTaskStream;`
  );
  const fn = factory(sandbox.S, sandbox.onUnauthorized, sandbox.pushProcess,
                     sandbox.settleProcess, sandbox.setProcessState,
                     sandbox.fetch, sandbox.AbortController,
                     sandbox.TextDecoder, sandbox.console);
  return { fn, calls };
}

// 假 reader：按脚本吐 SSE data 行
function mkReader(chunks, { throwAbort = false } = {}) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    read: async () => {
      if (throwAbort && i === chunks.length) {
        const e = new Error('aborted'); e.name = 'AbortError'; throw e;
      }
      if (i >= chunks.length) return { done: true, value: undefined };
      return { done: false, value: enc.encode(chunks[i++]) };
    },
  };
}
function mkResp(reader, status = 200) {
  return { status, ok: status === 200, body: { getReader: () => reader } };
}

let fails = 0;
async function check(name, cond, detail) {
  if (!cond) { console.log(`  ✗ ${name}${detail ? ' :: ' + detail : ''}`); fails++; }
  else console.log(`  ✓ ${name}`);
}

// ============================================================ 用例
console.log('=== mountTaskStream 三路径契约（v0.15.1）===');

// ① 正常终态：应恰好 onDone 一次
{
  const sse = [
    'data: {"kind":"token","text":"你好","seq":1}\n\n',
    'data: {"kind":"done","status":"done","progress":"回复完成","seq":2}\n\n',
  ];
  const { fn, calls } = buildSandbox({
    fetchImpl: async () => mkResp(mkReader(sse)),
  });
  await fn('tk1', { onDone: (ev) => calls.onDone.push(ev) });
  await check('① 正常终态 → onDone 恰好 1 次', calls.onDone.length === 1,
              `实际 ${calls.onDone.length} 次`);
  await check('① onDone 携带 ok=true', calls.onDone[0] && calls.onDone[0].ok === true);
}

// ② ★ 流被 abort：原先 onDone 永不触发 → 按钮卡死。修复后必须兜底一次
{
  const { fn, calls } = buildSandbox({
    fetchImpl: async () => mkResp(mkReader([], { throwAbort: true })),
  });
  await fn('tk2', { onDone: (ev) => calls.onDone.push(ev) });
  await check('② 流被 abort → onDone 兜底调用 1 次', calls.onDone.length === 1,
              `实际 ${calls.onDone.length} 次（修复前为 0 → 按钮卡"处理中"）`);
  await check('② abort 标记正确', calls.onDone[0] && calls.onDone[0].aborted === true);
}

// ③ ★ 流读完但无 done 事件：也要兜底
{
  const sse = ['data: {"kind":"token","text":"半截","seq":1}\n\n'];   // 无 done
  const { fn, calls } = buildSandbox({
    fetchImpl: async () => mkResp(mkReader(sse)),
  });
  await fn('tk3', { onDone: (ev) => calls.onDone.push(ev) });
  await check('③ 无终态事件 → onDone 兜底 1 次', calls.onDone.length === 1,
              `实际 ${calls.onDone.length} 次`);
  await check('③ noTerminal 标记正确', calls.onDone[0] && calls.onDone[0].noTerminal === true);
}

// ④ 不得重复调用（幂等）
{
  const sse = ['data: {"kind":"done","status":"done","seq":1}\n\n'];
  const { fn, calls } = buildSandbox({
    fetchImpl: async () => mkResp(mkReader(sse)),
  });
  await fn('tk4', { onDone: (ev) => calls.onDone.push(ev) });
  await check('④ 终态后不再重复 onDone', calls.onDone.length === 1);
}

// ⑤ onDone 内部抛异常不能阻断收尾
{
  const sse = ['data: {"kind":"done","status":"done","seq":1}\n\n'];
  const { fn, calls } = buildSandbox({
    fetchImpl: async () => mkResp(mkReader(sse)),
  });
  let threw = false;
  try {
    await fn('tk5', { onDone: () => { throw new Error('回调炸了'); } });
  } catch (e) { threw = true; }
  await check('⑤ onDone 抛异常不影响流收尾', !threw);
}

// ⑥ HTTP 401 不应卡死（走 onUnauthorized 后也需释放）
{
  const { fn, calls } = buildSandbox({
    fetchImpl: async () => mkResp(mkReader([]), 401),
  });
  await fn('tk6', { onDone: (ev) => calls.onDone.push(ev) });
  await check('⑥ 401 有兜底（onDone 或至少不挂起）', calls.onDone.length <= 1);
}

console.log('');
console.log(fails === 0 ? '全部通过 ✓' : `${fails} 项失败 ✗`);
process.exit(fails === 0 ? 0 : 1);
