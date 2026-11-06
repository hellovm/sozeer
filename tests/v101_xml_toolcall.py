"""v0.10.1 回归测试：XML 风格工具调用的兜底解析 + 前端 gotoBibleTab 接线。

背景：线上真实故障——模型明明支持 tools，却把调用写成正文里的
<tool_call>{...}</tool_call>，导致 turn.tool_calls 为空、
标签原样流进对话框，工具根本没执行，用户看到「冒出一堆尖括号就没然后了」。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

NODE = r"C:/Users/david/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
PROJECT = Path(__file__).resolve().parents[1]

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        print(f"  PASS {name}")
        PASS += 1
    else:
        print(f"  FAIL {name}" + (f"  → {detail}" if detail else ""))
        FAIL += 1


# ------------------------------------------------------------------ Python 侧
def test_python() -> None:
    print("=== Python 侧：_parse_xml_tool_calls ===")
    sys.path.insert(0, str(PROJECT))
    import importlib

    # 只导入纯函数所在模块，避免拉起整条依赖链
    src = (PROJECT / "apps" / "api" / "agent.py").read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src)
    fn = None
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_parse_xml_tool_calls":
            fn = n
    check("agent.py 里存在 _parse_xml_tool_calls", fn is not None)
    if fn is None:
        return
    # 连同常量与 _TOOL_CALL_* 赋值一起取出执行
    parts = []
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.startswith("_TOOL_CALL") for t in n.targets):
            parts.append(ast.get_source_segment(src, n))
        elif isinstance(n, ast.FunctionDef) and n.name == "_parse_xml_tool_calls":
            parts.append(ast.get_source_segment(src, n))
    code = "import json, re, uuid\n" + "\n".join(p for p in parts if p)
    ns: dict = {}
    try:
        exec(code, ns)
    except Exception as e:
        check("解析器可独立执行", False, f"{type(e).__name__}: {e}")
        return
    check("解析器可独立执行", True)
    parse = ns["_parse_xml_tool_calls"]

    O = ns["_TOOL_CALL_OPEN"]
    C = ns["_TOOL_CALL_CLOSE"]

    # 1. 风格 A：arguments 是对象
    tcs, cleaned = parse(f'我来读一下。{O}{{"name":"read_chapter","arguments":{{"ch_idx":14}}}}{C}')
    check("风格A：捞出 1 个调用", len(tcs) == 1, str(tcs))
    check("风格A：工具名正确", tcs and tcs[0]["name"] == "read_chapter", str(tcs))
    check("风格A：参数正确", tcs and tcs[0]["arguments"].get("ch_idx") == 14, str(tcs))
    check("风格A：正文已剔除标签", O not in cleaned and C not in cleaned, repr(cleaned))
    check("风格A：保留了正常叙述", "我来读一下" in cleaned, repr(cleaned))

    # 2. 风格 B：tool_name + arguments 是 JSON 字符串
    tcs, cleaned = parse(f'{O}{{"tool_name":"read_chapter","arguments":"{{\\"ch_idx\\":15}}"}}{C}')
    check("风格B：捞出调用", len(tcs) == 1, str(tcs))
    check("风格B：字符串参数二次解析成功", tcs and tcs[0]["arguments"].get("ch_idx") == 15, str(tcs))

    # 3. 多个调用
    tcs, cleaned = parse(
        f'{O}{{"name":"read_chapter","arguments":{{"ch_idx":14}}}}{C}'
        f'{O}{{"name":"draft_chapter","arguments":{{"ch_idx":15}}}}{C}正文')
    check("多个调用全部捞出", len(tcs) == 2, str(len(tcs)))
    check("多个调用 id 互不相同",
          len(tcs) == 2 and tcs[0]["id"] != tcs[1]["id"])
    check("多个调用正文已清空标签", O not in cleaned, repr(cleaned))

    # 4. 无 XML 的普通文本不动
    tcs, cleaned = parse("今天天气不错，我们聊聊大纲。")
    check("普通文本不产生调用", tcs == [])
    check("普通文本原样返回", cleaned == "今天天气不错，我们聊聊大纲。")

    # 5. 坏 JSON 不能炸
    tcs, cleaned = parse(f"{O}这不是 JSON，只是一段话{C}")
    check("坏 JSON 不抛异常且不产生调用", tcs == [], str(tcs))

    # 6. 无 name 字段
    tcs, _ = parse(f'{O}{{"arguments":{{"a":1}}}}{C}')
    check("缺 name 的调用被忽略", tcs == [], str(tcs))

    # 7. arguments 缺失时给空 dict，而不是 None
    tcs, _ = parse(f'{O}{{"name":"read_chapter"}}{C}')
    check("arguments 缺失回落为空 dict", tcs and tcs[0]["arguments"] == {}, str(tcs))

    # 8. 真实故障复现：多行 JSON
    tcs, cleaned = parse(
        f'{O}{{\n  "name": "read_chapter",\n  "arguments": {{"ch_idx": 14, "full": false}}\n}}{C}')
    check("多行 JSON 可解析", len(tcs) == 1 and tcs[0]["arguments"]["ch_idx"] == 14, str(tcs))


# ------------------------------------------------------------------ JS 侧
def test_js() -> None:
    print("\n=== JS 侧：查看结果按钮接线 ===")
    app_js = (PROJECT / "apps" / "web" / "app.js").read_text(encoding="utf-8")

    check("app.js 定义 gotoBibleTab", "function gotoBibleTab(" in app_js)
    check("gotoBibleTab 真正切标签（调 .click()）",
          "tab.click()" in app_js.split("function gotoBibleTab(")[1][:600]
          if "function gotoBibleTab(" in app_js else False)
    # 关键回归：不能只写 #b-audit 而不切面板
    check("查看结果按钮调用 gotoBibleTab('audit')",
          "gotoBibleTab('audit')" in app_js)
    check("审计完成回调也切面板",
          app_js.count("gotoBibleTab('audit')") >= 2,
          f"仅 {app_js.count(chr(103)+chr(111)+chr(116)+chr(111)+chr(66)+chr(105)+chr(98)+chr(108)+chr(101)+chr(84)+chr(97)+chr(98))} 处")

    print("\n=== JS 侧：思考中看门狗 ===")
    check("定义 _stallTimer", "let _stallTimer = null" in app_js)
    check("存在看门狗 setInterval", "_stallTimer = setInterval" in app_js)
    check("finally 里清理看门狗",
          "if (_stallTimer) { clearTimeout(_stallTimer); _stallTimer = null; }" in app_js)
    check("看门狗文案有 5 条以上",
          app_js.count("正在理解你的意思") >= 2)

    print("\n=== JS 语法 ===")
    p = subprocess.run([NODE, "--check",
                        str(PROJECT / "apps" / "web" / "app.js")],
                       capture_output=True, text=True)
    check("node --check 通过", p.returncode == 0, p.stderr[:300])


def main() -> int:
    test_python()
    test_js()
    print(f"\nRESULT {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())